"""Explorer — extraction LLM call for one batch of chunks.

One LLM call per batch (no tool calls during extraction).
Receives an existing-node index so it can create cross-batch edges and aliases.
Recorder handles dedup against existing graph via SQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

import aiohttp

from bookgraph.llm_call import (
    MAX_OUTPUT_BUDGET, cached_tokens, is_throttle, is_truncated, output_budget,
    reasoning_text, reasoning_tokens, retry_delay)
from config import Config
from llm_json import loads_tolerant
from provider_store import config_for
from storage.graph_db import GraphDB
from sub_agent.types import load_prompt

log = logging.getLogger("bookgraph")

# 提取器提示词在 ``prompts/subagent/book_extractor.md`` —— 这一层最需要反复改，
# 而 Python 字符串里的转义（JSON 示例的花括号尤其）正是最难改的地方。口吻仿
# ``book_classifier.md``：**教方法，不教答案**（旧提示词把「kind 可自由扩展」
# 这类路径依赖写死在代码里，等于让提取器用更少的信息重做已做过的判断）。
_EXTRACTOR_PROMPT_FALLBACK = """你是一个书籍正文提取器。你会收到一本书的正文片段
（若干 chunk），从中提取**关系** —— 实体是关系的副产品。直接产出 JSON。

铁律（完整提示词文件缺失时的最小集）：
1. 实体因做事而存在：写不出以它为端点的关系，就不要声明它。
2. 每条断言（节点 / 边 / 观察）都要给**逐字原文** evidence，必须能在本批正文里找到。
3. 关系要具体（mentors 优于 related_to）；是解读就标 event_type=perspective。
4. chunk_id 必须是任务描述里给出的实际 chunk_id。
5. 拿不准就报 merge_candidates，不要替读者决定。

直接输出 JSON，无额外文本：
{
  "new_nodes": [{"canonical_name","kind","why","evidence"}],
  "new_edges": [{"source","target","kind","chunk_id","event_type","subject","evidence","tags"}],
  "new_observations": [{"node_name","field","value","event_type","subject","evidence_chunk_id","evidence","tags"}],
  "new_aliases": [{"alias","canonical_name"}],
  "merge_candidates": [{"name_a","name_b","confidence","reason"}],
  "quotes": [{"character_name","quote_text","chunk_id","scene_context","related_nodes"}],
  "chunk_summaries": {"chunk_id": "这一块讲了什么（一句话，每块都要有）"},
  "summary": "..."
}
"""


def _load_extractor_prompt() -> str:
    """读提示词文件；缺失时退回最小集，并且**出声**（沉默不是成功）。"""
    text = load_prompt("book_extractor", _EXTRACTOR_PROMPT_FALLBACK)
    if text == _EXTRACTOR_PROMPT_FALLBACK:
        log.warning("prompts/subagent/book_extractor.md 缺失 —— 退到最小提示词，"
                    "提取质量会明显下降")
    return text



def _render_text_forms(text_forms: list[dict] | None) -> str:
    """把导入期判定的**文本形态**渲染成提取指令。

    措辞是**指令**而不是参考：「按此归属，不要重新判断」。理由是提取器只看得到
    块文本、看不到排版 —— 而这些形态是导入期逐段读过版式样张后判定的。让提取器
    重新判断，等于让它用更少的信息重做一遍已经做过的判断。

    这解决的是一个具体误判：窄门里书信（``p.calibre19``）与叙述正文
    （``p.calibre14``）段均长都是 70 字上下，统计上分不开；不告诉提取器，
    信里第一人称的口吻就会被归给叙述者。
    """
    rows = [f for f in (text_forms or [])
            if isinstance(f, dict) and str(f.get("selector") or "").strip()]
    if not rows:
        return ""
    lines = ["\n\n## 本书文本形态（导入期已逐段读过样张后判定，务必遵守）\n"]
    for f in rows:
        name = str(f.get("name") or "").strip() or "未命名形态"
        ev = str(f.get("evidence") or "").strip()
        lines.append(f"- `{f['selector']}` = **{name}**" + (f"（依据：{ev}）" if ev else ""))
    lines.append(
        "\n**这些形态已判定完毕，遇到时按此归属，不要重新判断。** 尤其注意："
        "书信 / 日记 / 引文里的第一人称，口吻属于**写它的角色**，不属于叙述者 —— "
        "不要把信里的话当成叙述者的自述。"
    )
    lines.append(
        "\n正文里每个段落前面可能出现 `«形态名»` 形式的**排版标注**（形如 "
        "`«书信·日记引文» 亲爱的杰罗姆：`）。那是导入期逐段读过版式后贴上的"
        "**位置标签**，不是原文的一部分 —— 引用原文时不要把它带进去。"
        "没有标注的段落，是导入期未判定的普通段落。"
    )
    return "\n".join(lines)


def build_explorer_prompt(book_context: dict | None,
                          text_forms: list[dict] | None = None) -> str:
    """Compose Explorer system prompt: base + book context + 文本形态指令。

    *text_forms* 来自导入期落库的 ``book_text_forms``（见 ``db.list_text_forms``）。
    这是「分类器 → 提取器」的回流：提取质量的提升不来自提取器变聪明，来自它的
    环境被标注了。
    """
    prompt = _load_extractor_prompt()
    if book_context:
        ctx_json = json.dumps(book_context, ensure_ascii=False, indent=2)
        prompt += ("\n\n## 本书世界观边界（由前置 agent 产出，全书固定）\n\n"
                   f"{ctx_json}\n")
    return prompt + _render_text_forms(text_forms)


def build_node_index(index_data: list[dict]) -> str:
    """Format node index data into compact text for Explorer task message."""
    if not index_data:
        return ""
    lines = ["已有实体索引（可在 new_edges 和 new_aliases 中引用）："]
    for n in index_data:
        alias_str = ""
        if n.get("aliases"):
            alias_str = " (别称: " + ", ".join(n["aliases"]) + ")"
        lines.append(f"  [{n['kind']}] {n['canonical_name']}{alias_str}")
    return "\n".join(lines)


def build_two_tier_node_index(index_map: dict | None) -> str:
    """Format two-tier node index into compact text for Explorer task message.

    index_map: {"compact": [{canonical_name, kind}],
                "detailed": [{canonical_name, kind, aliases}]}
    The compact tier names every entity without aliases (keeps the prompt
    small); the detailed tier adds aliases for the most relevant subset so
    the LLM can still map nicknames to canonical names.
    """
    if not index_map:
        return ""
    compact = index_map.get("compact") or []
    detailed = index_map.get("detailed") or []
    if not compact and not detailed:
        return ""
    lines = ["已有实体索引（可在 new_edges 和 new_aliases 中引用）："]
    lines.append("【全部实体】")
    for n in compact:
        lines.append(f"  [{n['kind']}] {n['canonical_name']}")
    if detailed:
        lines.append("【高相关实体（含别称）】")
        for n in detailed:
            alias_str = ""
            if n.get("aliases"):
                alias_str = " (别称: " + ", ".join(n["aliases"]) + ")"
            desc = (n.get("description") or "").strip()
            # 身份说明只给一行、最多 40 字 —— 索引是「认人」用的，不是传记。
            if len(desc) > 40:
                desc = desc[:40] + "…"
            desc_str = f" —— {desc}" if desc else ""
            lines.append(
                f"  [{n['kind']}] {n['canonical_name']}{alias_str}{desc_str}")
    return "\n".join(lines)


def build_task_message(
    book_title: str, book_author: str,
    chunks: list[dict],
    total: int, done_count: int, node_count: int,
    existing_nodes_text: str = "",
    scope_brief: str = "",
    feedback: list[str] | None = None,
) -> str:
    """Build the user task message for one batch.

    Args:
        chunks: list of {chunk_id, chapter_index, chapter_title, chunk_index, content}
        existing_nodes_text: pre-formatted node index from build_node_index()
        scope_brief: 叙事域简报（集类书才有，见 ``bookgraph/scope.py``）
        feedback: 回炉轮的缺陷清单（机械体检 + 证伪趟的质疑）
    """
    lines = [
        f"来源：《{book_title}》（作者：{book_author}）",
        f"全书共 {total} 个文本块。",
    ]
    if scope_brief:
        lines += ["", scope_brief]
    if feedback:
        lines += ["", "## 上一轮这批的问题（这一轮必须逐条处理）", ""]
        lines += [f"  {i}. {f}" for i, f in enumerate(feedback, 1)]
        lines += ["",
                  "对每一条：要么在正文里找到依据、把断言改对，要么**撤回**它。"
                  "撤回是允许的、也是被鼓励的 —— 少说、说准，比多说、说错强。"
                  "不要重复上一轮已经被指出来的错误。"]
    lines += [
        "",
        f"本批次共 {len(chunks)} 块：",
    ]
    for ch in chunks:
        lines.append(
            f"  - chunk_id={ch['chunk_id']} | 第 {ch['chapter_index'] + 1} 章"
            f"「{ch['chapter_title']}」| chunk {ch['chunk_index']}"
        )
    pct = int(done_count / total * 100) if total else 0
    lines.append("")
    lines.append(f"全书位置：{done_count}/{total}（{pct}%）")
    lines.append(f"当前图谱已记录 {node_count} 个实体节点。")
    if existing_nodes_text:
        lines.append("")
        lines.append(existing_nodes_text)
    lines.append("")
    lines.append(f"[以下是 {len(chunks)} 个 chunk 的正文，每块以 chunk_id 标注]")
    lines.append("")
    for ch in chunks:
        lines.append(f"=== chunk_id: {ch['chunk_id']} ===")
        lines.append(ch["content"])
        lines.append("")
    return "\n".join(lines)


def parse_explorer_json(content: str) -> dict | None:
    """Tolerant parse of Explorer's final JSON output.

    阶梯与裸引号修复统一在 ``llm_json.loads_tolerant``（旧阶梯实测 106 次调用
    失败 13 次，每次都整批节点/边/引文全丢）。
    """
    return loads_tolerant(content, expect=dict)


async def run_explorer(
    session: aiohttp.ClientSession,
    cfg: Config,
    db: GraphDB,
    book_id: str,
    book_title: str,
    book_author: str,
    chunks: list[dict],
    total: int,
    done_count: int,
    system_prompt: str,
    log_cb: Callable[[dict], None] | None = None,
    scope_brief: str = "",
    feedback: list[str] | None = None,
) -> tuple[dict | None, str | None]:
    """Run Explorer on one batch.

    Returns ``(parsed, None)`` on success; ``(None, error_type)`` on failure
    where error_type ∈ {"parse_failed", "timeout", "api_error",
    "exhausted_retries"}. Builds existing-node index so the LLM can create
    cross-batch edges and detect aliases against previously extracted
    entities.

    ``log_cb`` (optional) receives one dict per call: {status, prompt,
    response, latency_ms, attempt, max_attempts, tokens_in, tokens_out,
    error_detail, parsed, node_index} — used for LLM call logging.
    """
    cfg = config_for(cfg, "bookgraph.explorer")
    node_count = db.total_node_count(book_id)

    # Build node index: let the LLM see what's already in the graph.
    # Two-tier — full compact list (no aliases) + top-relevance detailed
    # subset (with aliases) — keeps the prompt small without hiding entities.
    #
    # **刻意不按叙事域过滤。** 曾经过滤过，代价是**复现实体被反复重命名**：
    # 提取第 7 篇时看不见第 1 篇已建的「我」，于是给同一个人另起了「史铁生」
    # —— 实测《我与地坛》的叙述者裂成三个节点。防串流靠的是「一批不跨篇」
    # 加域简报，不是靠把环境藏起来；藏起来只是让提取器**看不见自己已经做过
    # 什么**，那不是隔离，是失忆。
    chapter_indices = {ch["chapter_index"] for ch in chunks if "chapter_index" in ch}
    index_map = db.get_node_index_two_tier(
        book_id, chapter_indices
    ) if chapter_indices else None
    existing_nodes_text = build_two_tier_node_index(index_map)
    node_index_counts = (
        {"compact": len(index_map.get("compact") or []),
         "detailed": len(index_map.get("detailed") or [])}
        if index_map else None
    )

    user_msg = build_task_message(
        book_title, book_author, chunks, total, done_count, node_count,
        existing_nodes_text=existing_nodes_text, scope_brief=scope_brief,
        feedback=feedback,
    )
    full_prompt = f"[system]\n{system_prompt}\n\n[user]\n{user_msg}"
    body: dict = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "temperature": 0.0,
        # 输出预算按**总** token 算（推理 + 正文）。写死 16000 时实测 10 次调用
        # 挂 7 次：推理就烧掉 15k，正文一个字符轮不到，端点回 HTTP 200 + 空
        # content。见 ``bookgraph/llm_call``。下面每轮按需抬高。
        "max_tokens": output_budget(cfg),
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    def _emit_log(status: str, response: str, *, latency_ms: int | None,
                  attempt: int, tokens_in: int | None = None,
                  tokens_out: int | None = None,
                  tokens_cached: int | None = None,
                  reasoning_tokens: int | None = None,
                  reasoning_content: str = "",
                  error_detail: str | None = None,
                  parsed: dict | None = None) -> None:
        if log_cb is None:
            return
        try:
            log_cb({
                "status": status,
                "prompt": full_prompt,
                "response": response,
                "latency_ms": latency_ms,
                "attempt": attempt,
                "max_attempts": 3,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "tokens_cached": tokens_cached,
                "reasoning_tokens": reasoning_tokens,
                "reasoning_content": reasoning_content,
                "error_detail": error_detail,
                "parsed": parsed,
                "node_index": node_index_counts,
            })
        except Exception:
            log.warning("Explorer log_cb failed", exc_info=True)

    last_exc: Exception | None = None
    budget = output_budget(cfg)
    seen_truncated = False
    seen_parse_failed = False
    throttled = 0
    for attempt in range(8):
        # 常态 3 次（1 次 + 2 次重试）够用。**限流**要多等（8 次）；**撞上限**
        # 要给更多预算（5 次 —— 预算翻倍很快收敛，再多只是烧钱）。
        limit = 8 if throttled else (5 if seen_truncated else 3)
        if attempt >= limit:
            break
        if attempt > 0:
            await asyncio.sleep(retry_delay(attempt, throttled))
        t0 = time.monotonic()
        try:
            async with session.post(
                f"{cfg.api_base}/chat/completions",
                json=body, headers=headers,
                # Endpoint latency jitters widely (measured 15s-300s+ for the
                # same prompt). 300s left some batches consistently timing out;
                # 600s matches the scout budget.
                timeout=aiohttp.ClientTimeout(total=600),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"API {resp.status}: {err[:300]}")
                data = json.loads(await resp.text())
                content = data["choices"][0]["message"].get("content") or ""
                latency_ms = int((time.monotonic() - t0) * 1000)
                usage = data.get("usage") or {}
                cached = cached_tokens(data)
                think = reasoning_tokens(data)
                think_text = reasoning_text(data)
                if is_truncated(data):
                    # 撞上限 ≠ 模型说胡话。空 content 多半是推理吃光了额度，
                    # 所以**别当瞬时故障重试同样的预算** —— 加预算才有用。
                    seen_truncated = True
                    budget = min(budget * 2, MAX_OUTPUT_BUDGET)
                    log.warning(
                        "Explorer 撞上 %d token 上限（推理 %d，正文 %d 字）"
                        "—— 预算抬到 %d 重试",
                        body["max_tokens"], think, len(content), budget)
                    _emit_log("truncated", content, latency_ms=latency_ms,
                              attempt=attempt + 1,
                              tokens_in=usage.get("prompt_tokens"),
                              tokens_out=usage.get("completion_tokens"),
                              tokens_cached=cached, reasoning_tokens=think,
                              reasoning_content=think_text,
                              error_detail=f"finish_reason=length, "
                                           f"reasoning_tokens={think}")
                    body["max_tokens"] = budget
                    last_exc = RuntimeError("truncated at max_tokens")
                    continue
                parsed = parse_explorer_json(content)
                if parsed is not None:
                    counts = _extraction_counts(parsed)
                    _emit_log("ok", content, latency_ms=latency_ms,
                              attempt=attempt + 1,
                              tokens_in=usage.get("prompt_tokens"),
                              tokens_out=usage.get("completion_tokens"),
                              tokens_cached=cached, reasoning_tokens=think,
                              reasoning_content=think_text,
                              parsed=counts)
                    return parsed, None
                log.warning("Explorer returned unparseable content: %s",
                            content[:200])
                _emit_log("parse_failed", content, latency_ms=latency_ms,
                          attempt=attempt + 1,
                          tokens_in=usage.get("prompt_tokens"),
                          tokens_out=usage.get("completion_tokens"),
                          tokens_cached=cached, reasoning_tokens=think,
                          reasoning_content=think_text)
                # 两种都重试。
                # 空响应 = 网关瞬时故障（实测 HTTP 200 + content=""），同一条
                # 提示词重放就正常。
                # 非空畸形**也重试** —— 旧注释说「真格式错误，立即失败，不浪费
                # 重试」，但算错了账：模型输出是随机的，重放常常就对了（实测这
                # 一版模型 65 次里 2 次写出没收尾的字符串）；而**不重试的代价是
                # 整批 5-7 块全丢**，重试只花一次调用。解析失败本身罕见
                # （上一轮 157 次调用 0 次），所以重试不会变成常态浪费。
                seen_parse_failed = True
                last_exc = RuntimeError(
                    "unparseable response" if content.strip() else "empty response")
                log.warning("Explorer 响应无法解析（第 %d 次），重试", attempt + 1)
                continue
        except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError) as exc:
            last_exc = exc
            latency_ms = int((time.monotonic() - t0) * 1000)
            if is_throttle(exc):
                throttled += 1
            log.warning("Explorer attempt %d failed: %s", attempt + 1, exc)
            continue

    log.error("Explorer all retries exhausted: %s", last_exc)
    if seen_truncated:
        # 预算问题不是网络问题 —— 分开报，否则「加预算」这条线索永远看不到。
        error_type = "truncated"
    elif seen_parse_failed:
        # 同样要分开报：畸形响应是提示词/模型的问题，不是网络的问题。
        error_type = "parse_failed"
    elif isinstance(last_exc, asyncio.TimeoutError):
        error_type = "timeout"
    elif isinstance(last_exc, (RuntimeError, aiohttp.ClientError)):
        error_type = "api_error"
    else:
        error_type = "exhausted_retries"
    _emit_log(error_type, "", latency_ms=None, attempt=3,
              error_detail=str(last_exc) if last_exc else None)
    return None, error_type


def _extraction_counts(parsed: dict) -> dict:
    """Summarize an Explorer result into flat counts for logging."""
    return {
        "new_nodes": len(parsed.get("new_nodes") or []),
        "new_observations": len(parsed.get("new_observations") or []),
        "new_edges": len(parsed.get("new_edges") or []),
        "new_aliases": len(parsed.get("new_aliases") or []),
        "new_quotes": len(parsed.get("quotes") or []),
        "merge_candidates": len(parsed.get("merge_candidates") or []),
        # 简述覆盖率进调用台账：模型漏写时能从 log 看出来，不用翻正文。
        "chunk_summaries": len(
            parsed.get("chunk_summaries") or parsed.get("summaries") or {}),
    }
