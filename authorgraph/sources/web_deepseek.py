"""DeepSeek Search / Fetch —— 作者层的联网观测通道。

和 ``fetch.py`` 的关系：那边是「自己抓 HTML、自己抽正文」，这边是
「让 DeepSeek 抓、拿回 Markdown」。两条通道产出的 ``page`` 形状一致，
验证层（``verify.quote_located``）不用改。

三条硬边界（与写作侧的 ``web_research_service`` 同源）：

1. **只认 DeepSeek 端点。** 不是 ``deepseek.com`` 就报错，不偷偷换供应商。
2. **Search 的结果只是线索。** ``summary/snippet`` 不能当引文；要引就
   ``fetch`` 出正文，再逐字比对。
3. **Markdown 必须洗成纯文本。** 引文比对只抹空白与全半角，``**`` 和
   ``#`` 会留在正文里，导致一句真话对不上 —— 所以抓回来先去标记。
"""

from __future__ import annotations

import hashlib
import logging
import re
from types import SimpleNamespace
from typing import Any

import aiohttp

from authorgraph.sources.registry import canonical_url, host_of
from services import web_research_service as _web

log = logging.getLogger("authorgraph")

MAX_TEXT_CHARS = 8000


def resolve(cfg: Any) -> Any | None:
    """找一条真的能联网的 DeepSeek 连接。

    主模型不一定绑在 DeepSeek 上（控制台按调用点绑定，写作/作者链路可能
    用火山或别家），但 DeepSeek 的 Search/Fetch 只认自家端点。所以：

    1. 当前 ``cfg`` 本身就是 ``deepseek.com`` → 直接用；
    2. 否则去控制台连接里找一条 DeepSeek 连接（不静默换供应商，
       找不到就返回 ``None``，调用方回退原生搜索 + 本地抓页）。

    联网通道与写作侧共用同一张连接表，但 **key 不落日志、不回显**。
    """
    if cfg is None:
        return None
    # 主配置连 key 都没有 = 没配置过（测试/裸 dataclass）。此时不去翻控制台
    # 连接表，否则「零网络零 token」的替身测试会被真实连接顶掉。
    if not str(getattr(cfg, "api_key", "") or "").strip():
        return None
    if _web.deepseek_endpoint(cfg) is not None:
        return cfg
    try:
        from provider_store import ProviderStore, get_active_store

        path = getattr(cfg, "providers_path", None)
        store = (ProviderStore(path) if path
                 else get_active_store(cfg))
        if store is None:
            return None
        for provider in store.list_providers():
            url = str(provider.get("base_url") or "").strip()
            if "deepseek.com" not in url:
                continue
            cand = SimpleNamespace(
                api_base=url,
                api_key=store.reveal_key(str(provider.get("id") or "")))
            if _web.deepseek_endpoint(cand) is not None:
                return cand
    except Exception as exc:                            # noqa: BLE001
        log.warning("authorgraph: deepseek provider lookup failed: %s", exc)
    return None


def available(cfg: Any) -> bool:
    """只看**给定配置本身**，不翻控制台连接表。

    控制台连接由入口（脚本/管线/钩子闭合）显式 ``resolve()`` 后再传进来；
    模块级函数不偷偷换供应商，替身测试也不会被真实连接顶掉。
    """
    return _web.deepseek_endpoint(cfg) is not None


def _s(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def markdown_to_text(md: str) -> str:
    """把 DeepSeek 返回的 Markdown 洗成可逐字比对的纯文本。

    **不做语义改写**：只去掉标记，保留原字。这样 ``quote_located`` 的
    空白归一仍然成立，也不会把「加粗」当成原文的一部分。
    """
    text = md or ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s{0,3}\d+\.\s+", "", text, flags=re.M)
    text = re.sub(r"\*\*|__|~~", "", text)
    text = re.sub(r"(?<!\w)[*_]([^*_\n]+)[*_](?!\w)", r"\1", text)
    # 中文紧贴强调符号时上面的边界断言不生效；剩余星号一律去掉，
    # 它们只会让逐字引文对不上，不承载正文语义。
    text = text.replace("*", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _lang(text: str) -> str:
    cjk = sum(1 for ch in text[:2000] if "一" <= ch <= "鿿")
    return "zh" if cjk > 40 else "en"


def _hits(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Search 结果归一。**没有 URL 的一律丢掉** —— 无法回抓就不是线索。"""
    out: list[dict[str, Any]] = []
    for row in (data.get("results") or []):
        if not isinstance(row, dict):
            continue
        url = _s(row.get("url"))
        if not url.startswith(("http://", "https://")):
            continue
        try:
            score = float(row.get("rerank_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        try:
            authority = int(row.get("authority") or -1)
        except (TypeError, ValueError):
            authority = -1
        out.append({
            "url": url,
            "title": _s(row.get("title")) or url,
            "snippet": _s(row.get("snippet")),
            "summary": _s(row.get("summary")),
            "content": _s(row.get("content")),
            "site_name": _s(row.get("site_name")),
            "date": _s(row.get("date")),
            "authority": authority,
            "score": score,
        })
    return out


async def search(session: aiohttp.ClientSession, cfg: Any, query: str, *,
                 count: int = 8, need_content: bool = False,
                 ori_question: str = "") -> dict[str, Any]:
    """Search：只产线索。``need_content=True`` 时正文也**只作预览**。"""
    data = await _web.search_raw(
        session, cfg, query, count=count, need_content=need_content,
        ori_question=ori_question)
    return {
        "provider": "deepseek", "query": query,
        "hits": _hits(data), "warnings": [],
        "note": "搜索结果是线索；引文必须来自 fetch 到的正文。",
    }


async def fetch_page(session: aiohttp.ClientSession, cfg: Any, url: str, *,
                     max_chars: int = MAX_TEXT_CHARS) -> dict[str, Any]:
    """抓一页，返回与 ``authorgraph.sources.fetch.fetch_page`` 同形状。"""
    out: dict[str, Any] = {
        "url": url, "canonical_url": canonical_url(url), "title": "",
        "publisher": host_of(url), "language": "", "main_text": "",
        "content_hash": "", "published_at": "", "status": "error",
        "http_status": 0, "error": "",
    }
    try:
        data = await _web.fetch_raw(session, cfg, url)
    except Exception as exc:                          # noqa: BLE001
        out["error"] = str(exc)[:200]
        return out
    text = markdown_to_text(_s(data.get("content")))
    if not text:
        out["status"], out["error"] = "empty", "no text extracted"
        return out
    out.update({
        "title": _s(data.get("title")) or url,
        "publisher": host_of(url),
        "language": _lang(text),
        "main_text": text[:max_chars],
        "status": "ok",
        "http_status": 200,
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    })
    excerpt = text[:2000]
    cut = excerpt.rfind("\n")
    out["content_excerpt"] = excerpt[:cut] if cut > 600 else excerpt
    return out


def provider(cfg: Any) -> tuple[Any, Any] | None:
    """返回与 ``pipeline.enrich_author`` 同签名的 ``(search, fetch)``。

    ``cfg`` 必须是**已经解析过的** DeepSeek 配置（入口先调 ``resolve()``）。
    不是 DeepSeek 端点就返回 ``None``，调用方回退到原有的
    ``web_search_service`` + 本地 ``fetch_page``。
    """
    if not available(cfg):
        return None

    async def _search(session: aiohttp.ClientSession, cfg_: Any, query: str,
                      *, max_results: int = 8, **_: Any) -> dict[str, Any]:
        return await search(session, cfg, query, count=max_results)

    async def _fetch(session: aiohttp.ClientSession, url: str,
                     **_: Any) -> dict[str, Any]:
        return await fetch_page(session, cfg, url)

    return _search, _fetch
