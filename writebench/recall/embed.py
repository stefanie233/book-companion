"""doubao-embedding-vision 批量嵌入（火山 coding plan）。

单请求最多 10 条输入；超过会直接 400。失败重试 5 次，429 单独退避。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import aiohttp

from . import EMBED_MODEL

_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = Path(os.environ.get(
    "WB_PROVIDERS",
    str(_ROOT / "data" / "config" / "providers.json")))
#: agent plan 与 coding plan 都提供 doubao-embedding-vision；
#: coding plan 的 429 很密，索引默认走 agent plan，可用环境变量覆盖。
PROVIDER_ID = os.environ.get("WB_EMBED_PROVIDER", "")
MAX_BATCH = 10


def provider() -> dict[str, str]:
    data = json.loads(PROVIDERS.read_text(encoding="utf-8"))
    for item in data["providers"]:
        # 未设置 WB_EMBED_PROVIDER 时取 providers.json 的第一个连接。
        if not PROVIDER_ID or item.get("id") == PROVIDER_ID:
            return {"base_url": str(item["base_url"]).rstrip("/"),
                    "api_key": str(item["api_key"]),
                    "name": str(item.get("name") or "")}
    raise KeyError(PROVIDER_ID)


def normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [float(x) / norm for x in vec]


class EmbedError(RuntimeError):
    pass


class _Limiter:
    """把请求起点摊开：并发不等于同时打满，429 是账号级频率限制。"""

    def __init__(self, interval: float):
        self.interval = interval
        self.lock = asyncio.Lock()
        self.next_at = 0.0

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            await asyncio.sleep(delay)


async def _post(session: aiohttp.ClientSession, url: str,
                headers: dict[str, str], payload: dict[str, Any],
                *, retries: int = 5) -> dict[str, Any]:
    last = ""
    for attempt in range(retries):
        try:
            async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=180)) as resp:
                text = await resp.text()
                if resp.status == 200:
                    return json.loads(text)
                last = f"{resp.status} {text[:300]}"
                if resp.status == 429:
                    print(f"[embed] 429 retry {attempt + 1}", flush=True)
                if resp.status != 429 and resp.status < 500:
                    raise EmbedError(last)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep([15, 30, 60, 120, 240][min(attempt, 4)])
    raise EmbedError(last)


async def embed_texts(texts: Sequence[str], *, model: str = EMBED_MODEL,
                      batch: int = MAX_BATCH, concurrency: int = 3,
                      min_interval: float = 0.4,
                      progress: Callable[[int, int], None] | None = None,
                      on_batch: Callable[[int, list[list[float]]], None]
                      | None = None) -> list[list[float]]:
    """按输入顺序返回归一化向量。批次大小硬上限 10。

    ``on_batch`` 每批完成即回调，调用方可以立刻落库，避免中途 429
    把整轮已完成的工作全丢掉。
    """
    conf = provider()
    url = conf["base_url"] + "/embeddings"
    headers = {"Authorization": f"Bearer {conf['api_key']}",
               "Content-Type": "application/json"}
    batch = min(batch, MAX_BATCH)
    jobs = [(i, list(texts[i:i + batch]))
            for i in range(0, len(texts), batch)]
    out: list[list[float] | None] = [None] * len(texts)
    done = 0
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(concurrency)
    limiter = _Limiter(min_interval)

    async def one(start: int, chunk: list[str]) -> None:
        nonlocal done
        payload = {"model": model,
                   "input": [text.strip() or "。" for text in chunk]}
        async with sem:
            await limiter.wait()
            data = await _post(session, url, headers, payload)
        items = sorted(data.get("data") or [],
                       key=lambda item: int(item.get("index") or 0))
        if len(items) != len(chunk):
            raise EmbedError(f"batch {start}: got {len(items)}/{len(chunk)}")
        vectors = [normalize(item["embedding"]) for item in items]
        for offset, vec in enumerate(vectors):
            out[start + offset] = vec
        if on_batch:
            on_batch(start, vectors)
        async with lock:
            done += len(chunk)
            if progress:
                progress(done, len(texts))

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(one(i, chunk) for i, chunk in jobs), return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        raise EmbedError(f"{len(errors)} batches failed; first: {errors[0]}")
    if any(vec is None for vec in out):
        raise EmbedError("some vectors missing")
    return [vec for vec in out if vec is not None]
