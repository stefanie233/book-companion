"""ContextSetter — one-shot LLM call to set a book's world-view boundary.

Output is cached in graph_meta so resume doesn't re-run it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import aiohttp

log = logging.getLogger("bookgraph")

CONTEXT_SETTER_PROMPT = """你是一个书籍世界观边界设定者。根据书名和作者，
判断这本书的类型、世界观范围、应该提取和不应提取的实体类别。

你的输出会作为图谱提取 agent 的前置约束，告诉它"哪些实体算、哪些不算"。

考虑维度：
1. **世界观边界**：哪些是故事世界内的实体（应提取），哪些是现实元数据（不应提取）。
   - 小说类：角色/地点/事件/组织/象征物等属于世界观内；作者/译者/出版社/出版信息不属于。
   - 非虚构类：作者本人、书中讨论的人物/事件/概念都属于；纯印刷元数据不属于。
   - 诗集类：诗中人物/地点/意象属于；诗人本人的传记信息视情况而定。
2. **建议的 kind 分类**：基于书的类型，给出适合的实体分类（character/location/event/
   concept/symbol/theme 等）。LLM 可自由扩展，但这是默认词汇表。
3. **基调与主题**：一两句告诉提取 agent 这本书大致在讲什么，
   提高它对相关概念的敏感度。注意：这是基调提示，不是要提取的内容。
4. **明确排除的实体类别**：列出本书明确不应提取的类别。

输出严格 JSON，无额外文本：
```json
{
    "world_view_boundary": "一段话：本书世界观内的实体范围 + 应排除的现实元数据",
    "genre": "novella|novel|nonfiction|poetry|...",
    "tone_and_themes": "一段话：基调与主题提示",
    "suggested_kinds": ["character", "location", "event", "..."],
    "entities_to_exclude": ["real_world_author", "translator", "publisher", "..."]
}
```
"""


async def run_context_setter(
    session: aiohttp.ClientSession,
    api_key: str,
    api_base: str,
    model: str,
    book_title: str,
    book_author: str,
    timeout: int = 90,
    log_cb: Callable[[dict], None] | None = None,
) -> dict[str, Any] | None:
    """One-shot call to set the book's world-view boundary.

    Returns the parsed JSON context, or None on failure.
    The caller is responsible for caching the result in graph_meta.

    ``log_cb`` (optional) receives one dict per call: {status, prompt,
    response, latency_ms, attempt, max_attempts, tokens_in, tokens_out,
    error_detail} — used for LLM call logging.
    """
    user_msg = f"书名：《{book_title}》\n作者：{book_author}"
    full_prompt = f"[system]\n{CONTEXT_SETTER_PROMPT}\n\n[user]\n{user_msg}"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": CONTEXT_SETTER_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "temperature": 0.0,
        # Explicit output budget (see explorer.py) — small response here but
        # keeps behaviour uniform and avoids any default-cap truncation.
        "max_tokens": 16000,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
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
            log.warning("ContextSetter log_cb failed", exc_info=True)

    t0 = time.monotonic()
    try:
        async with session.post(
            f"{api_base}/chat/completions",
            json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                latency_ms = int((time.monotonic() - t0) * 1000)
                log.warning("ContextSetter API %s: %s", resp.status, err[:200])
                _emit_log("api_error", "", latency_ms=latency_ms,
                          error_detail=f"API {resp.status}: {err[:200]}")
                return None
            data = json.loads(await resp.text())
            content = data["choices"][0]["message"].get("content") or ""
            latency_ms = int((time.monotonic() - t0) * 1000)
            usage = data.get("usage") or {}
            parsed = _parse_json(content)
            if parsed is not None:
                _emit_log("ok", content, latency_ms=latency_ms,
                          tokens_in=usage.get("prompt_tokens"),
                          tokens_out=usage.get("completion_tokens"))
            else:
                _emit_log("parse_failed", content, latency_ms=latency_ms,
                          tokens_in=usage.get("prompt_tokens"),
                          tokens_out=usage.get("completion_tokens"))
            return parsed
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.warning("ContextSetter failed: %s", exc)
        _emit_log("api_error", "", latency_ms=latency_ms, error_detail=str(exc))
        return None


def _parse_json(content: str) -> dict | None:
    """Tolerant JSON parse: direct → code fence → first { to last }."""
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    import re
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
