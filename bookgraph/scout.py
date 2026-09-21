"""Scout — Phase 0 narrative analysis preceding the Explorer extraction loop.

Reads all chunks once (truncated) and produces a batch plan that groups
chunks by narrative unit rather than mechanical 5-chunk sliding window.
Also identifies key characters and extraction focus for each batch,
giving the Explorer richer context for each extraction pass.

**Content filtering**: Scout also acts as a gatekeeper — it identifies
chunks that shouldn't be extracted (foreign-language editions, appendices,
indexes, copyright boilerplate, etc.) so Explorer agents never waste
tokens on irrelevant material.

Use a cheaper/faster model if available — the scout task is semantic
grouping, not precise extraction.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

import aiohttp

from config import Config

log = logging.getLogger("bookgraph")

_SCOUT_SYSTEM_PROMPT = """你是一个书籍叙事结构分析器。你会收到一本书的**全部**文本块摘要，
你的任务不是提取实体，而是规划提取策略——按叙事单元重新组织批次，让后续的提取 agent
能获得更连贯的上下文。

分析维度：
1. **内容域识别与过滤**（最优先）：先扫描全书 chunk，识别**不属于主叙事**的内容块：
   - 外语版全文（如中文译本后面附的原文/其他语种版本）
   - 附录、索引、注释、参考文献
   - 版权声明、出版信息、译者序/跋（与正文无关时）
   - 目录页、广告页、空白/占位内容
   - 与小说叙事无关的尾注、致谢等

   将识别出的 chunk_id 全部列入 excluded_chunk_ids。
   在 content_warnings 中逐段说明排除理由。
   **排除的 chunk 不会进入批次规划，也不会被后续提取。**

2. **叙事单元划分**：将全书按情节发展划分为若干叙事单元（序幕/发展/转折/高潮/尾声，或更自然的划分）。
   每个单元标注 chunk_id 范围、章节范围、该单元的叙事功能。
3. **关键人物识别**：列出书中出场的主要人物（从chunk内容中观察到的具名人物），标注首次出场位置。
4. **批次规划**：将 chunk 重组为有意义的批次。原则：
   - 尽量保持叙事单元完整——同一场景/同一情节段落放在同一批次
   - 每批 4-8 个 chunk（依据叙事密度调整，对话密集区可少放，描述区可多放）
   - 标注每批的提取重点（如"本批关注 A/B 角色的情感互动""本批引入新地点 X"）
   - 保留原 chunk 的章节/顺序信息做参考，但允许跨连续章节合并
   - 不要拆分紧邻的对话场景
   - 排除 excluded_chunk_ids 中的 chunk 后，每个 chunk 必须出现且仅出现一次

直接输出 JSON，无额外文本，格式严格如下：
```json
{
    "book_overview": "一段话：这本书的叙事结构和核心冲突总结",
    "content_warnings": [
        {
            "chunk_ids": ["chunk_099", "chunk_100"],
            "chapter_range": [23, 24],
            "reason": "法语原文版全文，非中文译本正文",
            "suggestion": "skip"
        }
    ],
    "excluded_chunk_ids": ["chunk_099", "chunk_100"],
    "narrative_units": [
        {
            "label": "序幕：相遇",
            "chunk_ids": ["chunk_001", "chunk_002", "chunk_003"],
            "chapter_range": [1, 2],
            "rationale": "引入主角世界观，建立核心关系"
        }
    ],
    "key_characters": [
        {
            "name": "主角名",
            "first_appearance_chunk_id": "chunk_001",
            "role_hint": "第一人称叙述者/主角/..."
        }
    ],
    "batch_plan": [
        {
            "batch_index": 1,
            "chunk_ids": ["chunk_001", "chunk_002", "chunk_003"],
            "label": "第1-2章：相遇",
            "extraction_focus": "关注主角的出场方式和新环境的建立"
        }
    ],
    "summary": "全书共 N 个 chunk，排除 E 个，划分为 M 个叙事单元，K 个提取批次。"
}
```

注意：
- **内容过滤优先**：先识别并排除非叙事内容，再规划批次
- chunk_id 使用输入中标注的实际 chunk_id 字符串
- excluded_chunk_ids 中的 chunk 不得出现在 batch_plan 或 narrative_units 中
- 只输出 JSON，不要任何其他文字
"""


def build_scout_task(
    book_title: str, book_author: str,
    chunks: list[dict],
    total: int,
) -> str:
    """Build the user task message for scout — all chunks at once,
    each truncated to 500 chars to keep total token budget reasonable.
    """
    lines = [
        f"书名：《{book_title}》",
        f"作者：{book_author}",
        f"全书共 {total} 个文本块。",
        "",
        "[以下是全部 chunk 的摘要（每块最多 500 字），以 chunk_id 标注]",
        "",
    ]
    for ch in chunks:
        # Truncate each chunk to ~500 chars while preserving structural info
        content = (ch.get("content") or "")[:500]
        lines.append(
            f"=== chunk_id: {ch['chunk_id']} | "
            f"第 {ch['chapter_index'] + 1} 章「{ch['chapter_title']}」"
            f" | chunk {ch['chunk_index']} ==="
        )
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


def parse_scout_json(content: str) -> dict | None:
    """Tolerant parse of Scout's final JSON output.

    Same logic as ``parse_explorer_json`` — tries direct parse first,
    then code-fence extraction, then last-resort brace extraction.
    """
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


async def run_scout(
    session: aiohttp.ClientSession,
    cfg: Config,
    book_title: str,
    book_author: str,
    chunks: list[dict],
    total: int,
    log_cb: Callable[[dict], None] | None = None,
) -> dict | None:
    """Run Scout on the full book. Returns parsed JSON or None.

    Chunks are truncated to 500 chars each to keep the prompt manageable.
    For a 300-chunk book this is ~200 KB of text — fits in most model
    context windows.

    Returns a dict with keys: ``book_overview``, ``narrative_units``,
    ``key_characters``, ``batch_plan``, ``summary``.

    ``log_cb`` (optional) receives one dict per call: {status, prompt,
    response, latency_ms, attempt, max_attempts, tokens_in, tokens_out,
    error_detail} — used for LLM call logging.
    """
    user_msg = build_scout_task(book_title, book_author, chunks, total)
    full_prompt = f"[system]\n{_SCOUT_SYSTEM_PROMPT}\n\n[user]\n{user_msg}"
    body: dict = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": _SCOUT_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "temperature": 0.0,
        # See explorer.py — explicit output budget avoids truncation of the
        # plan JSON (scout scans the whole book, output can be large).
        "max_tokens": 16000,
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    def _emit_log(status: str, response: str, *, latency_ms: int,
                  tokens_in: int | None = None, tokens_out: int | None = None,
                  error_detail: str | None = None) -> None:
        if log_cb is None:
            return
        try:
            log_cb({
                "status": status,
                "prompt": full_prompt,
                "response": response,
                "latency_ms": latency_ms,
                "attempt": 1,
                "max_attempts": 1,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "error_detail": error_detail,
            })
        except Exception:
            log.warning("Scout log_cb failed", exc_info=True)

    log.info("Scout: analyzing %d chunks for《%s》...", total, book_title)
    t0 = time.monotonic()
    try:
        async with session.post(
            f"{cfg.api_base}/chat/completions",
            json=body, headers=headers,
            # Whole-book scan: on slow reasoning endpoints 300s is not enough
            # (measured ~220-300s+ with max_tokens raised). 600s gives the
            # plan JSON room to complete without timing out.
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                latency_ms = int((time.monotonic() - t0) * 1000)
                log.warning("Scout API %s: %s", resp.status, err[:300])
                _emit_log("api_error", "", latency_ms=latency_ms,
                          error_detail=f"API {resp.status}: {err[:300]}")
                return None
            data = json.loads(await resp.text())
            content = data["choices"][0]["message"].get("content") or ""
            latency_ms = int((time.monotonic() - t0) * 1000)
            usage = data.get("usage") or {}
            parsed = parse_scout_json(content)
            if parsed is not None:
                log.info(
                    "Scout: %d units, %d key characters, %d batches",
                    len(parsed.get("narrative_units", [])),
                    len(parsed.get("key_characters", [])),
                    len(parsed.get("batch_plan", [])),
                )
                _emit_log("ok", content, latency_ms=latency_ms,
                          tokens_in=usage.get("prompt_tokens"),
                          tokens_out=usage.get("completion_tokens"))
                return parsed
            log.warning("Scout returned unparseable content: %s", content[:200])
            _emit_log("parse_failed", content, latency_ms=latency_ms,
                      tokens_in=usage.get("prompt_tokens"),
                      tokens_out=usage.get("completion_tokens"))
            return None
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.warning("Scout failed: %s", exc)
        _emit_log("api_error", "", latency_ms=latency_ms,
                  error_detail=str(exc))
        return None
