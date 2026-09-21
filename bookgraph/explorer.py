"""Explorer — extraction LLM call for one batch of chunks.

One LLM call per batch (no tool calls during extraction).
Receives an existing-node index so it can create cross-batch edges and aliases.
Recorder handles dedup against existing graph via SQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable

import aiohttp

from config import Config
from storage.graph_db import GraphDB

log = logging.getLogger("bookgraph")

_BASE_EXPLORER_PROMPT = """你是一个书籍内容图谱提取器。你会收到一本书的正文片段（若干 chunk），
你的任务是从中提取实体和关系，以及角色的经典台词和观察事件，直接产出 JSON 结果。

提取规则：
1. 只提取具名实体——有明确名称的人物、地点、事件、组织等。
   人称代词（他/她/它）、泛指（有人/某个地方）不提取为独立实体。
2. 严格遵守下方"本书世界观边界"——只提取世界观内的实体，现实元数据不提取。
3. 实体的 kind 不限于预设列表。下方"建议 kind 分类"是默认词汇表，可自由扩展。
4. 事件节点：描述"发生了什么"，不需要逐句提取。一个事件 = 推动剧情的一个动作或变化。
5. 如果本批内发现两个不同名称可能指向同一实体，输出 merge_candidates
   （confidence + reason），不要硬合并——系统会在全部提取完成后做全局消歧。
6. 实体描述要简洁精准，1-2 句话点出该实体在故事中的身份/作用。
7. ★ 边的 source/target 可以是：
   a) 本批 new_nodes 中的 canonical_name
   b) 上方"已有实体索引"中的 canonical_name
   系统会自动解析为 node_id。优先连接到已有实体——图谱的完整性依赖于跨批次连线。
8. chunk_id 必须填任务描述里给出的实际 chunk_id 字符串。
9. 当前图谱已有 N 个实体节点（数量在任务描述里给出）。你产出的 new_nodes 会被系统去重——
   如果某个名字已在图谱中存在，系统会自动复用而非新建，你只管提取。

10. ★ 经典台词提取 ★：从正文中截取角色说的**原句**——有文学感、揭示性格、或推动情绪的关键台词。
   每个 quote 必须包含：
   - character_name: 说话的角色名（canonical_name）
   - quote_text: 从原文逐字摘录的台词（10-80 字为宜，太短无信息，太长非台词）
   - scene_context: 1 句简短场景描述（谁在什么情境下说的、对谁说）
   - related_nodes: 与此台词相关的实体 canonical_name 列表（关联角色、地点、事件）
   标准：每 5 个 chunk 产出 2-8 条 quotes。不要廉价对话，只要**能代表角色声音**的句子。
   注意：角色内心独白也算 quote，quote_text 不加引号。

11. ★ 观察事件（new_observations）★：对于 kind="character" 的实体，从正文中提取
   **带原文锚点的观察**，而非结论性的性格鉴定。每个 observation 包含：
   - node_name: 观察对象（canonical_name）
   - field: personality / appearance / motivation / role_in_story / speaking_style
   - value: 观察到的内容（如"暴躁但重情义""瘦高个戴金丝眼镜"）
   - event_type: fact / perspective / opinion
     · appearance（外貌）、role_in_story（叙事功能）多为 fact--客观可验证
     · personality（性格）、motivation（动机）多为 perspective--你的解读
     · opinion 仅当原文中某角色/叙述者明确表达过观点时使用
   - subject: 视角主体。fact 填 null；perspective 填"LLM提取"；opinion 填观点来源
   - evidence_chunk_id: 产生该观察的 chunk_id（原文锚点，必填）
   - tags: 主题标签列表（如"信仰冲突""诀别"），可自创，见规则 13

   ★ 核心原则：不下结论，只记观察。同一角色在不同章节可能有多个 personality 观察
   （如 ch3"暴躁"、ch10"重情义"），全部追加输出，系统不覆盖。让消费端综合判断。
   本段没观察到信息就不输出该 field，坚决不编造。

12. ★ 边的类型标注（event_type）★：每条 new_edges 必须标注 event_type：
   - fact: 客观关系。appears_in（出场于）/ mentions（提及）/ parent_of（亲属）/
     location_of（位于）/ causes（导致）/ wrote（写了）。subject 填 null。
   - perspective: 解读性关系。loves（爱）/ hates（恨）/ trusts（信任）/ fears（惧怕）/
     betrays（背叛）/ mentors（教导）/ protects（保护）/ resists（对抗）/ deceives（欺骗）/
     admires（仰慕）/ rivals（竞争）/ depends_on（依赖）。subject 填"LLM提取"。
   - opinion: 原文中某角色/叙述者明确主张的关系。subject 填主张者。
   也可自创 kind，但尽量具体而非笼统（"confidant" 比 "related_to" 好）。
   ★ 关系是视角时必须标 perspective 并填 subject，不要让视角冒充事实。

13. ★ 主题标签（tags）★：当某主题在正文中反复出现（如"信仰冲突""权力博弈""自我放逐"），
   可在 new_observations 和 new_edges 的 tags 中标注。标签名自由，但系统会按 parent_type
   归类到 fact/perspective/opinion（与所在事件的 event_type 一致）。标签用于后续主题聚合
   与召回，不要滥用--只标真正贯穿情节的主题。

14. ★ 已有实体索引使用规则 ★：
   - 如果本批正文提到了索引中已有的实体 -> 在 new_edges 中连接它，不要创建重复的 new_node
   - 如果发现某个名字是索引中某个实体的别称 -> 输出 new_alias（{alias, canonical_name}），而非 new_node
   - 跨章节的事件节点应连接到索引中的相关角色，即使角色不在本批正文中
   - 如果索引中某实体在本批出现了新的观察信息，仍可输出 new_observations（系统追加，不覆盖）
   - 索引中已有实体的新观察，node_name 填该实体的 canonical_name 即可

直接输出 JSON，无额外文本，格式严格如下：
```json
{
    "new_nodes": [
        {"canonical_name": "...", "kind": "...", "description": "...",
         "first_appearance_chunk_id": "..."}
    ],
    "new_observations": [
        {"node_name": "...", "field": "personality", "value": "...",
         "event_type": "perspective", "subject": "LLM提取",
         "evidence_chunk_id": "...", "tags": ["信仰冲突"]}
    ],
    "new_edges": [
        {"source": "...", "target": "...", "kind": "...", "chunk_id": "...",
         "event_type": "fact", "subject": null, "tags": []}
    ],
    "new_aliases": [
        {"alias": "...", "canonical_name": "..."}
    ],
    "merge_candidates": [
        {"name_a": "...", "name_b": "...", "confidence": 0.7, "reason": "..."}
    ],
    "quotes": [
        {"character_name": "...", "quote_text": "...", "scene_context": "...", "related_nodes": ["..."]}
    ],
    "summary": "本批 N 块完成。新建 X 实体，Y 条关系，Z 别名，Q 条台词，O 条观察。"
}
```
"""


def build_explorer_prompt(book_context: dict | None) -> str:
    """Compose Explorer system prompt: base + book-specific context block."""
    if not book_context:
        return _BASE_EXPLORER_PROMPT
    ctx_json = json.dumps(book_context, ensure_ascii=False, indent=2)
    return _BASE_EXPLORER_PROMPT + (
        "\n\n## 本书世界观边界（由前置 agent 产出，全书固定）\n\n"
        f"{ctx_json}\n"
    )


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
            lines.append(f"  [{n['kind']}] {n['canonical_name']}{alias_str}")
    return "\n".join(lines)


def build_task_message(
    book_title: str, book_author: str,
    chunks: list[dict],
    total: int, done_count: int, node_count: int,
    existing_nodes_text: str = "",
) -> str:
    """Build the user task message for one batch.

    Args:
        chunks: list of {chunk_id, chapter_index, chapter_title, chunk_index, content}
        existing_nodes_text: pre-formatted node index from build_node_index()
    """
    lines = [
        f"来源：《{book_title}》（作者：{book_author}）",
        f"全书共 {total} 个文本块。",
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
    """Tolerant parse of Explorer's final JSON output."""
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


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
    node_count = db.total_node_count(book_id)

    # Build node index: let the LLM see what's already in the graph.
    # Two-tier — full compact list (no aliases) + top-relevance detailed
    # subset (with aliases) — keeps the prompt small without hiding entities.
    chapter_indices = {ch["chapter_index"] for ch in chunks if "chapter_index" in ch}
    index_map = db.get_node_index_two_tier(book_id, chapter_indices) if chapter_indices else None
    existing_nodes_text = build_two_tier_node_index(index_map)
    node_index_counts = (
        {"compact": len(index_map.get("compact") or []),
         "detailed": len(index_map.get("detailed") or [])}
        if index_map else None
    )

    user_msg = build_task_message(
        book_title, book_author, chunks, total, done_count, node_count,
        existing_nodes_text=existing_nodes_text,
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
        # Explicit output budget: without it the endpoint's default caps the
        # answer (reasoning tokens eat the budget first), truncating the JSON
        # mid-array -> parse_failed. 16000 is verified enough for a 5-6 chunk
        # batch on the deepseek-v4-flash endpoint (finish_reason='stop').
        "max_tokens": 16000,
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    def _emit_log(status: str, response: str, *, latency_ms: int | None,
                  attempt: int, tokens_in: int | None = None,
                  tokens_out: int | None = None,
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
                "error_detail": error_detail,
                "parsed": parsed,
                "node_index": node_index_counts,
            })
        except Exception:
            log.warning("Explorer log_cb failed", exc_info=True)

    last_exc: Exception | None = None
    for attempt in range(3):  # 1 + 2 retries
        if attempt > 0:
            await asyncio.sleep(2 ** attempt)
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
                parsed = parse_explorer_json(content)
                if parsed is not None:
                    counts = _extraction_counts(parsed)
                    _emit_log("ok", content, latency_ms=latency_ms,
                              attempt=attempt + 1,
                              tokens_in=usage.get("prompt_tokens"),
                              tokens_out=usage.get("completion_tokens"),
                              parsed=counts)
                    return parsed, None
                log.warning("Explorer returned unparseable content: %s",
                            content[:200])
                _emit_log("parse_failed", content, latency_ms=latency_ms,
                          attempt=attempt + 1,
                          tokens_in=usage.get("prompt_tokens"),
                          tokens_out=usage.get("completion_tokens"))
                # Empty response = transient gateway fault (observed HTTP 200
                # with content=""). Retry it like a timeout — the same prompt
                # succeeds on replay. A non-empty malformed response is a real
                # format error and fails immediately (no wasted retries).
                if not content.strip():
                    last_exc = RuntimeError("empty response")
                    log.warning("Explorer empty response — retrying (transient)")
                    continue
                return None, "parse_failed"
        except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError) as exc:
            last_exc = exc
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.warning("Explorer attempt %d failed: %s", attempt + 1, exc)
            continue

    log.error("Explorer all retries exhausted: %s", last_exc)
    if isinstance(last_exc, asyncio.TimeoutError):
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
    }
