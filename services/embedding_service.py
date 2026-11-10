"""Embedding 调用（决策 148）：走控制台绑定的 ``embedding`` 槽位。

与 ``writebench/recall/embed.py`` 的区别：那个是离线评测，硬编码了 provider
id 与路径；这里按调用点取**当前 agent 绑定的**连接，没有绑定就退回
``EMBEDDING_*`` 环境字段。两种情况都没有时直接报可读错误，不猜供应商。

模型按 OpenAI 兼容的 ``POST /embeddings`` 调：``{"model", "input": [...]}``，
响应 ``data[].embedding``。豆包 embedding 单请求最多 10 条，这里默认 10。
向量一律归一化：检索用点积即余弦，换模型也不用改打分。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any, Callable, Sequence

import aiohttp

from provider_store import (
    SLOT_EMBEDDING,
    ResolvedEndpoint,
    get_active_store,
)

log = logging.getLogger("embedding")

#: 豆包 embedding 单请求上限；OpenAI 兼容端更宽，取小的一边。
MAX_BATCH = 10
#: 一次嵌入只取头尾：开头定场景，结尾常有落点，中间对语义贡献小。
TEXT_LIMIT = 1800
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
BACKOFF = (10, 20, 40, 80, 160)


class EmbeddingError(RuntimeError):
    """可展示给作者的失败（没绑定、限流、返回形状不对）。"""


def _s(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _ready(ep: ResolvedEndpoint | None) -> bool:
    return bool(ep and _s(ep.base_url) and _s(ep.key) and _s(ep.model))


def resolve_endpoint(config: Any) -> ResolvedEndpoint:
    """当前 embedding 槽位的连接；无绑定则退回 ``config.embedding_*``。"""
    try:
        store = get_active_store(config)
        if store is not None:
            ep = store.resolve(SLOT_EMBEDDING, config)
            if _ready(ep):
                return ep
    except Exception as exc:                     # noqa: BLE001
        log.warning("embedding: console binding lookup failed: %s", exc)
    ep = ResolvedEndpoint(
        key=_s(getattr(config, "embedding_api_key", "")),
        base_url=_s(getattr(config, "embedding_base_url", "")),
        model=_s(getattr(config, "embedding_model", "")),
        source="env",
    )
    if _ready(ep):
        return ep
    raise EmbeddingError(
        "没有可用的 embedding：在控制台「模型绑定 → Embedding」选一条连接"
        "（带 embedding 模型），或在 .env 配 EMBEDDING_BASE_URL / "
        "EMBEDDING_API_KEY / EMBEDDING_MODEL")


def clip_for_embedding(text: str, *, limit: int = TEXT_LIMIT) -> str:
    """嵌入输入裁剪：短文本原样，长文本留头尾。"""
    text = " ".join(_s(text).split()) or "。"
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 360)] + " … " + text[-340:]


def normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec)) or 1.0
    return [float(x) / norm for x in vec]


class _Limiter:
    """摊开请求起点：并发不等于同时打满，限流是账号级的。"""

    def __init__(self, interval: float):
        self.interval = max(0.0, float(interval))
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
                headers: dict[str, str], payload: dict[str, Any], *,
                retries: int) -> dict[str, Any]:
    last = ""
    for attempt in range(max(1, retries)):
        try:
            async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise EmbeddingError(
                            f"embedding 返回的不是 JSON：{text[:200]}") from exc
                    return data if isinstance(data, dict) else {}
                last = f"{resp.status} {text[:300]}"
                if resp.status == 429:
                    log.warning("embedding: 429 retry %s", attempt + 1)
                if resp.status not in RETRY_STATUS:
                    raise EmbeddingError(f"embedding 接口 HTTP 错误：{last}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < retries:
            await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise EmbeddingError(f"embedding 接口失败（重试 {retries} 次）：{last}")


async def embed_texts(
        config: Any, texts: Sequence[str], *, model: str = "",
        batch: int = MAX_BATCH, concurrency: int = 2,
        min_interval: float = 0.5, retries: int = 4,
        progress: Callable[[int, int], None] | None = None,
        on_batch: Callable[[int, list[list[float]]], None] | None = None,
) -> list[list[float]]:
    """按输入顺序返回归一化向量。

    ``on_batch(start, vectors)`` 每批完成即回调，建索引时可以立刻落库，
    中途限流失败不会把整轮已完成的工作丢掉。
    """
    ep = resolve_endpoint(config)
    model = _s(model) or ep.model
    url = ep.base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {ep.key}",
               "Content-Type": "application/json"}
    inputs = [clip_for_embedding(t) for t in texts]
    if not inputs:
        return []
    batch = max(1, min(int(batch), MAX_BATCH))
    jobs = [(i, inputs[i:i + batch])
            for i in range(0, len(inputs), batch)]
    out: list[list[float] | None] = [None] * len(inputs)
    done = 0
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    limiter = _Limiter(min_interval)

    async def one(start: int, chunk: list[str]) -> None:
        nonlocal done
        payload = {"model": model, "input": chunk}
        async with sem:
            await limiter.wait()
            data = await _post(session, url, headers, payload,
                               retries=retries)
        items = sorted(data.get("data") or [],
                       key=lambda item: int(item.get("index") or 0))
        if len(items) != len(chunk):
            raise EmbeddingError(
                f"batch {start}: 返回 {len(items)}/{len(chunk)} 条向量")
        vectors = [normalize(item.get("embedding") or []) for item in items]
        if any(not vec for vec in vectors):
            raise EmbeddingError(f"batch {start}: 有向量为空")
        for offset, vec in enumerate(vectors):
            out[start + offset] = vec
        if on_batch:
            on_batch(start, vectors)
        async with lock:
            done += len(chunk)
            if progress:
                progress(done, len(inputs))

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(one(i, chunk) for i, chunk in jobs), return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        raise EmbeddingError(
            f"{len(errors)}/{len(jobs)} 批 embedding 失败；"
            f"首个：{errors[0]}") from errors[0]
    if any(vec is None for vec in out):
        raise EmbeddingError("有输入没有拿到向量")
    return [vec for vec in out if vec is not None]


async def embed_one(config: Any, text: str, *, model: str = "",
                    retries: int = 3) -> list[float]:
    vecs = await embed_texts(config, [text], model=model, batch=1,
                             concurrency=1, retries=retries)
    return vecs[0]
