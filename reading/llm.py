"""阅读域的通用 LLM 调用（地图合成、引文打标共用）。

非流式：服务端生成完之前一个字节都不发，所以只设总预算、不设空闲上限。
失败重试 3 次（429 走退避），飞行记录仪可选，usage 走 usage_log。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from bookgraph.llm_call import (
    is_truncated, reasoning_text, retry_delay, usage_tokens,
)

log = logging.getLogger("reading.llm")


def _record_usage(data: dict, model: str, source: str) -> None:
    try:
        import usage_log
        pin, pout = usage_tokens(data)
        usage_log.record(model=model, tokens_in=pin, tokens_out=pout,
                         source=source)
    except Exception:
        pass


async def call_chat(cfg: Any, messages: list[dict], *, max_tokens: int,
                    source: str = "reading.llm", mode: str = "",
                    temperature: float = 0.2, recorder: Any = None,
                    session_id: str = "", turn_index: int = 0,
                    book_id: str = "") -> str:
    """发一次非流式 chat.completions，返回正文（失败抛异常）。"""
    base = str(getattr(cfg, "api_base", "")
               or getattr(cfg, "api_base_url", "")).rstrip("/")
    key = str(getattr(cfg, "api_key", ""))
    model = str(getattr(cfg, "model", ""))
    if not base or not key:
        raise RuntimeError("缺少 api_base / api_key")
    body = {"model": model, "messages": messages,
            "temperature": temperature, "stream": False,
            "max_tokens": max_tokens}
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=300, sock_read=None)
    last: Exception | None = None
    async with aiohttp.ClientSession(timeout=timeout) as http:
        for attempt in range(3):
            t0 = time.monotonic()
            try:
                async with http.post(f"{base}/chat/completions", json=body,
                                     headers=headers) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(f"API {resp.status}: {raw[:200]}")
                    data = json.loads(raw)
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                text = str(msg.get("content") or "").strip()
                _record_usage(data, model, source)
                if recorder is not None:
                    try:
                        think = reasoning_text(data)
                        reply = (f"<think>\n{think}\n</think>\n"
                                 if think else "") + text
                        recorder.record_round(
                            session_id=session_id, turn_index=turn_index,
                            react_round=attempt + 1,
                            system_prompt=str(messages[0].get("content") or ""),
                            messages=messages[1:], tools_count=0,
                            mode=mode or source, book_id=book_id,
                            response_text=reply, model=model,
                            latency_ms=int((time.monotonic() - t0) * 1000))
                    except Exception:
                        log.warning("record call failed", exc_info=True)
                if is_truncated(data) and attempt < 2:
                    body["max_tokens"] = min(body["max_tokens"] * 2, 8_000)
                    raise RuntimeError("输出被截断，抬预算重试")
                return text
            except Exception as exc:                          # noqa: BLE001
                last = exc
                if attempt < 2:
                    delay = retry_delay(attempt, 0)
                    log.warning("call %d failed: %s; sleep %.1fs",
                                attempt + 1, exc, delay)
                    await asyncio.sleep(delay)
    raise RuntimeError(f"LLM 调用失败：{last}")
