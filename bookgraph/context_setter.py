"""ContextSetter — one-shot LLM call to set a book's world-view boundary.

Output is cached in graph_meta so resume doesn't re-run it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import aiohttp

from bookgraph.llm_call import DEFAULT_OUTPUT_BUDGET
from llm_json import loads_tolerant

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
5. **叙事结构**：这本书是**一个世界**，还是**几个世界**？给 `narrative_structure`：
   - `single`：一部连续的作品。长篇小说的各章、论著的各编、诗剧的各幕都是这一类
     —— 它们共享同一批人物、概念、事件，跨章连线是图谱的价值所在。
   - `collection`：短篇集 / 散文集 / 随笔集 / 多人合集。各篇自成一体，
     **只共享纸张，不共享人物**：第 3 篇里的人在第 1 篇里不存在。
   - 判据看**目录**（下面会给你）：篇名各自独立、彼此接不成一条连续情节 → collection；
     篇名是「第X章 / 第X部分 / 上篇 / 一」这类分章标记 → single。
   - 拿不准一律填 `single` —— 判成 single 只是不做特殊处理，判成 collection
     却会真的把书切开，代价不对称。

输出严格 JSON，无额外文本：
```json
{
    "world_view_boundary": "一段话：本书世界观内的实体范围 + 应排除的现实元数据",
    "genre": "novella|novel|nonfiction|poetry|...",
    "narrative_structure": "single|collection",
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
    unit_titles: list[str] | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any] | None:
    """One-shot call to set the book's world-view boundary.

    Returns the parsed JSON context, or None on failure.
    The caller is responsible for caching the result in graph_meta.

    ``log_cb`` (optional) receives one dict per call: {status, prompt,
    response, latency_ms, attempt, max_attempts, tokens_in, tokens_out,
    error_detail} — used for LLM call logging.

    ``unit_titles`` 是顶层正文单元的标题（= 目录）。原实现只给书名+作者，
    于是「这是集子还是长篇」这类问题只能靠猜；目录是判它唯一的硬证据。
    """
    user_msg = f"书名：《{book_title}》\n作者：{book_author}"
    titles = [t.strip() for t in (unit_titles or []) if (t or "").strip()]
    if titles:
        user_msg += ("\n\n目录（顶层单元，共 %d 个）：\n" % len(titles)
                     + "\n".join(f"  {i}. {t}" for i, t in enumerate(titles, 1)))
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
        "max_tokens": max_tokens or DEFAULT_OUTPUT_BUDGET,
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
    """Tolerant JSON parse — 阶梯与裸引号修复见 ``llm_json.loads_tolerant``。"""
    return loads_tolerant(content, expect=dict)
