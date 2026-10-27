"""实验台的轻量 OpenAI 兼容客户端：可控 temperature / max_tokens / 并发。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = Path(os.environ.get(
    "WB_PROVIDERS",
    str(_ROOT / "data" / "config" / "providers.json")))


def endpoint(name: str, model: str) -> dict[str, str]:
    data = json.loads(PROVIDERS.read_text(encoding="utf-8"))
    needle = name.lower()
    for provider in data["providers"]:
        hay = " ".join(str(provider.get(k) or "") for k in
                       ("name", "vendor", "id")).lower()
        if needle in hay:
            return {"base_url": str(provider["base_url"]).rstrip("/"),
                    "api_key": str(provider["api_key"]), "model": model}
    raise KeyError(name)


class Chat:
    def __init__(self, *, concurrency: int = 6, retries: int = 4):
        self.sem = asyncio.Semaphore(concurrency)
        self.retries = retries
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    async def complete(self, ep: dict[str, str], system: str, user: str, *,
                       temperature: float | None = None,
                       max_tokens: int | None = None,
                       timeout: int = 420,
                       json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "model": ep["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        last = ""
        for attempt in range(self.retries):
            if attempt:
                await asyncio.sleep([5, 20, 45][min(attempt - 1, 2)])
            try:
                async with self.sem:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                                ep["base_url"] + "/chat/completions",
                                json=body,
                                headers={"Authorization": "Bearer " + ep["api_key"]},
                                timeout=aiohttp.ClientTimeout(total=timeout)
                        ) as resp:
                            raw = await resp.text()
                            if resp.status != 200:
                                last = f"HTTP {resp.status}: {raw[:300]}"
                                continue
                            data = json.loads(raw)
                self.calls += 1
                usage = data.get("usage") or {}
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)
                return (data["choices"][0]["message"].get("content") or "")
            except Exception as exc:                 # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(f"chat failed after {self.retries}: {last}")
