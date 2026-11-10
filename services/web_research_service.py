"""写作联网检索：DeepSeek Search / Fetch 原始 JSON API。

与 ``web_search_service.py`` 的区别：那条路走 Anthropic 兼容端点的原生
``web_search_20250305``，服务端执行、只回结果块；这里直接用 DeepSeek 的
``/v1/tools/search`` 与 ``/v1/tools/fetch``，拿到结构化的 title / url /
summary / content，适合给写作素材卡带出处与逐字引文。

两条硬边界：

1. **只认 DeepSeek 端点**：``api_base`` 的 host 必须落在 ``deepseek.com``；
   其它端点返回可读错误，不偷偷换供应商。
2. **结果是外部来源，不是正典**：调用方必须放在 ``external_reference`` 或
   ``素材/`` 里，不能直接当事实写进正文。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger("webresearch")

SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


def _s(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _clean(value: Any, limit: int) -> str:
    text = " ".join(_s(value).split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip("，。；、 ") + "…"


def deepseek_endpoint(cfg: Any) -> tuple[str, str] | None:
    """解析 DeepSeek 根地址与 key；不是 DeepSeek 端点返回 ``None``。"""
    if cfg is None:
        return None
    api = _s(getattr(cfg, "api_base", "")).rstrip("/")
    key = _s(getattr(cfg, "api_key", ""))
    if not (api and key):
        return None
    parsed = urlparse(api)
    host = (parsed.hostname or "").lower()
    if host != "deepseek.com" and not host.endswith(".deepseek.com"):
        return None
    return f"{parsed.scheme}://{parsed.netloc}", key


def _valid_url(url: str) -> bool:
    parsed = urlparse(_s(url))
    return (parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and not parsed.username and not parsed.password)


async def _post(session: aiohttp.ClientSession, root: str, path: str,
                key: str, body: dict[str, Any], *,
                timeout: float = 60.0) -> dict[str, Any]:
    """POST 一次；408/429/5xx 等 1.5 秒重试一次（文档要求的礼貌重试）。"""
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    last = ""
    for attempt in range(2):
        async with session.post(
            root + path, json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data if isinstance(data, dict) else {}
            last = (await resp.text())[:300]
            if resp.status not in RETRY_STATUS or attempt:
                break
        await asyncio.sleep(1.5)
    raise RuntimeError(f"DeepSeek 联网接口 HTTP 错误：{last}")


async def search_raw(session: aiohttp.ClientSession, cfg: Any, query: str, *,
                     count: int = 10, need_content: bool = False,
                     ori_question: str = "") -> dict[str, Any]:
    """Search 的原始 JSON —— 给需要自己归一化的调用方（作者层）。

    与 ``search()`` 的区别只有一条：**不截断、不排序、不包装成素材卡**。
    调用方必须自己守「结果只是线索，抓页才有证据」。
    """
    ep = deepseek_endpoint(cfg)
    if ep is None:
        raise RuntimeError(
            "DeepSeek 联网需要 deepseek.com 端点与 API Key；当前不是 DeepSeek")
    q = _s(query)
    if not q:
        raise RuntimeError("联网检索词不能为空")
    body: dict[str, Any] = {
        "query": q,
        "count": max(1, min(int(count), 50)),
        "need_content": bool(need_content),
    }
    if _s(ori_question):
        body["ori_question"] = _s(ori_question)[:128]
    root, key = ep
    return await _post(session, root, SEARCH_PATH, key, body)


async def fetch_raw(session: aiohttp.ClientSession, cfg: Any,
                    url: str) -> dict[str, Any]:
    """Fetch 的原始 JSON：``{title, content}``。只接受 http/https。"""
    ep = deepseek_endpoint(cfg)
    if ep is None:
        raise RuntimeError(
            "DeepSeek 抓页需要 deepseek.com 端点与 API Key；当前不是 DeepSeek")
    u = _s(url)
    if not _valid_url(u):
        raise RuntimeError("只抓 http/https 网页，且不能带用户名或密码")
    clean = urlparse(u)._replace(fragment="").geturl()
    root, key = ep
    return await _post(session, root, FETCH_PATH, key, {"url": clean})


def cards_from_results(results: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    """把 Search 的 ``results`` 归一成素材卡；URL 必须是真实结果。"""
    cards: list[dict[str, Any]] = []
    for row in results or []:
        if not isinstance(row, dict):
            continue
        url = _s(row.get("url"))
        if not _valid_url(url):
            continue
        content = _s(row.get("content"))
        summary = _s(row.get("summary"))
        snippet = _s(row.get("snippet"))
        excerpt = content or summary or snippet
        if not excerpt:
            continue
        try:
            score = float(row.get("rerank_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        try:
            authority = int(row.get("authority") or -1)
        except (TypeError, ValueError):
            authority = -1
        cards.append({
            "title": _s(row.get("title")) or url,
            "url": url,
            "site_name": _s(row.get("site_name")),
            "date": _s(row.get("date")),
            "authority": authority,
            "score": score,
            "excerpt": _clean(excerpt, 1200),
            "exact": bool(content),
            "kind": "网页正文" if content else "搜索摘要",
            "source": "web",
            "caution": "外部来源；引文以原文为准，不搬专名与设定",
        })
    cards.sort(key=lambda x: (-x["score"], -x["authority"]))
    return cards[:max(1, int(limit))]


async def search(cfg: Any, query: str, *, count: int = 5,
                 need_content: bool = True) -> dict[str, Any]:
    """DeepSeek Search：返回带出处与逐字片段的候选卡。"""
    ep = deepseek_endpoint(cfg)
    if ep is None:
        raise RuntimeError(
            "联网检索需要 DeepSeek 端点与 API Key；当前 api_base 不是 "
            "deepseek.com，或没有配 api_key")
    root, key = ep
    query = _s(query)
    if not query:
        raise RuntimeError("联网检索词不能为空")
    body = {"query": query, "count": max(1, min(int(count), 10)),
            "need_content": bool(need_content)}
    async with aiohttp.ClientSession() as session:
        data = await _post(session, root, SEARCH_PATH, key, body)
    cards = cards_from_results(data.get("results"), limit=count)
    return {
        "provider": "deepseek", "source": "web", "query": query,
        "cards": cards, "count": len(cards),
        "note": ("外部来源，只作参考；引文以 URL 原文为准，不搬专名与设定。"
                 if cards else "DeepSeek 搜索没有返回可用结果。"),
    }


async def fetch(cfg: Any, url: str) -> dict[str, Any]:
    """DeepSeek Fetch：抓单个网页，返回 Markdown 正文。"""
    ep = deepseek_endpoint(cfg)
    if ep is None:
        raise RuntimeError(
            "联网抓取需要 DeepSeek 端点与 API Key；当前 api_base 不是 "
            "deepseek.com，或没有配 api_key")
    url = _s(url)
    if not _valid_url(url):
        raise RuntimeError("只抓 http/https 网页，且不能带用户名或密码")
    # 搜索结果常带 #页/段落 片段；片段本来就不发给服务器，抓页前去掉。
    clean = urlparse(url)._replace(fragment="").geturl()
    root, key = ep
    async with aiohttp.ClientSession() as session:
        data = await _post(session, root, FETCH_PATH, key, {"url": clean})
    content = _s(data.get("content"))
    title = _s(data.get("title")) or url
    cards = [] if not content else [{
        "title": title,
        "url": url,
        "site_name": "",
        "date": "",
        "authority": -1,
        "score": 0.0,
        "excerpt": _clean(content, 2000),
        "exact": True,
        "kind": "整页",
        "source": "web",
        "caution": "外部来源；引文以原文为准，不搬专名与设定",
    }]
    return {
        "provider": "deepseek", "source": "web", "query": title,
        "cards": cards, "count": len(cards),
        "note": ("已抓到 Markdown 正文。" if cards
                 else "目标网页没有可提取的正文。"),
    }


def cards_markdown(query: str, cards: list[dict[str, Any]]) -> str:
    """把选中的联网卡写成素材文件正文（带 URL 与引文类型）。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 联网素材：{_s(query) or '（未命名）'}",
             f"> 来源：DeepSeek Search / Fetch；抓取时间：{stamp}",
             "> 外部来源，只借写法与细节；引用时以 URL 原文为准。"]
    for card in cards:
        lines.append("")
        lines.append(f"## {_s(card.get('title')) or _s(card.get('url'))}")
        lines.append(f"- URL：{_s(card.get('url'))}")
        meta = []
        if _s(card.get("site_name")):
            meta.append(f"站点：{_s(card.get('site_name'))}")
        if _s(card.get("date")):
            meta.append(f"日期：{_s(card.get('date'))}")
        if card.get("authority", -1) >= 0:
            meta.append(f"权威性：{card.get('authority')}")
        meta.append(f"引文类型：{_s(card.get('kind')) or '网页'}")
        lines.append("- " + "｜".join(meta))
        lines.append("")
        lines.append("> " + _clean(card.get("excerpt"), 1200))
    return "\n".join(lines).strip() + "\n"
