"""统一的联网搜索：DeepSeek 原生 web_search 主路，Tavily 兜底。

为什么是这个顺序
----------------
· **DeepSeek 原生搜索** 是服务端工具（Anthropic Messages 兼容端点上的
  ``web_search_20250305``），不需要任何插件、不需要额外的搜索 key ——
  它就在模型端点里，是我们默认的联网方式。
· **Tavily** 是专用搜索 API，结果带正文片段，出处可逐条核对。原生搜索走不通
  （端点不是 DeepSeek / 模型名不对 / 调用失败）或零结果时兜底。留着它，但不
  再是主路。

⚠️ 原生搜索的两个硬条件（2026-10 实测）
--------------------------------------
1. **端点必须是 Anthropic Messages 兼容口**：``<base>/anthropic/v1/messages``。
   DeepSeek 官方是 ``https://api.deepseek.com/anthropic``；OpenAI 兼容的
   ``https://api.deepseek.com/v1`` 上没有这个工具。
2. **模型名必须是该端点认识的**（``deepseek-flash`` / ``deepseek-v4-pro``）。
   把 Claude Code 的 haiku 槽位映射成 ``glm-*`` 之类的名字会直接 400
   "The supported API model names are ..."，那不是搜索不可用，是模型名错了。

返回契约：``{"hits": [{title, url, content}], "answer": str,
"provider": "tavily"|"native"|"", "warnings": [str]}``。
``hits`` 的 URL 一律来自**真实搜索结果**（Tavily 的 results / 原生的
``web_search_tool_result`` 块），模型自己拼出来的网址不采信。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp

from llm_json import loads_tolerant

log = logging.getLogger("websearch")

TAVILY_URL = "https://api.tavily.com/search"

#: Anthropic Messages 兼容端点的服务端搜索工具。服务端执行，没有本地回调、
#: 没有第二次请求 —— 一次 POST 完成"搜索 → 读 → 写"。
NATIVE_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

#: 原生搜索没有独立的"结果正文"可拿（返回的是 encrypted_content），所以让
#: 模型把每次检索到的页面按 URL 摘要成 JSON。URL 仍以服务端返回的结果块为准。
NATIVE_PROMPT = """用 web_search 工具检索下面的问题，然后**只输出一个 JSON 对象**
（不要解释、不要代码围栏）：

{"results": [{"title": "页面标题", "url": "页面网址", "summary": "不超过 80 字"}]}

规则：
- url 必须来自这次搜索实际返回的页面，不要自己拼；
- summary 只写搜索结果里出现的内容；
- 最多 %(n)d 条；查不到就输出 {"results": []}。

问题：%(query)s"""


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _url_key(url: str) -> str:
    """URL 归一（去掉末尾斜杠、统一小写 host）—— 模型转写经常丢斜杠。"""
    u = _s(url).rstrip("/")
    if not u:
        return ""
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    return f"{host}{path}" if host else u.lower()


def native_endpoint(cfg: Any) -> tuple[str, str, str] | None:
    """解析原生搜索要用的 ``(base, key, model)``；不可用返回 ``None``。

    · ``native_search_base`` 显式配置优先（换别的兼容端点时用）；
    · 否则当 ``api_base`` 的 host 是 ``*.deepseek.com`` 时推导为
      ``<scheme>://<host>/anthropic``。
    """
    if cfg is None:
        return None
    base = _s(getattr(cfg, "native_search_base", "")).rstrip("/")
    api = _s(getattr(cfg, "api_base", "")).rstrip("/")
    if not base and api:
        u = urlparse(api)
        host = (u.hostname or "").lower()
        if host == "deepseek.com" or host.endswith(".deepseek.com"):
            base = f"{u.scheme}://{u.netloc}/anthropic"
    key = _s(getattr(cfg, "api_key", ""))
    # ``[1m]`` 这类客户端后缀不是模型名的一部分，原样发出去会 400。
    model = _s(getattr(cfg, "model", "")).split("[", 1)[0].strip()
    if not (base and key and model):
        return None
    return base, key, model


async def _tavily(session: aiohttp.ClientSession, cfg: Any, query: str, *,
                  max_results: int, deep_search: bool, topic: str) -> dict[str, Any]:
    key = _s(getattr(cfg, "search_api_key", ""))
    if not key:
        raise RuntimeError("没配 Tavily key（BOOK_COMPANION_SEARCH_API_KEY）")
    body = {
        "api_key": key,
        "query": query,
        "search_depth": "advanced" if deep_search else "basic",
        "topic": topic,
        "max_results": max(1, min(int(max_results), 20)),
        "include_answer": True,
        "include_raw_content": False,
    }
    async with session.post(
        TAVILY_URL, json=body, timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
        data = await resp.json()
    hits = [{"title": _s(r.get("title")), "url": _s(r.get("url")),
             "content": _s(r.get("content"))[:1200]}
            for r in (data.get("results") or []) if isinstance(r, dict) and _s(r.get("url"))]
    return {"hits": hits, "answer": _s(data.get("answer"))}


async def _native(session: aiohttp.ClientSession, cfg: Any, query: str, *,
                  max_results: int) -> list[dict[str, str]]:
    ep = native_endpoint(cfg)
    if ep is None:
        raise RuntimeError("当前端点不支持 DeepSeek 原生搜索"
                           "（需要 Anthropic 兼容端点 + DeepSeek 模型名）")
    base, key, model = ep
    body = {
        "model": model,
        "max_tokens": 2000,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": NATIVE_PROMPT % {
            "n": max(1, min(int(max_results), 10)), "query": query}}],
        "tools": [dict(NATIVE_SEARCH_TOOL, max_uses=2)],
    }
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {key}",
               "anthropic-version": "2023-06-01"}
    async with session.post(
        f"{base}/v1/messages", json=body, headers=headers,
        timeout=aiohttp.ClientTimeout(total=300),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
        data = await resp.json()

    return _hits_from_blocks(data.get("content") or [], max_results)


def _hits_from_blocks(blocks: list[Any], max_results: int) -> list[dict[str, str]]:
    """把一次原生搜索的 content 块收成 ``[{title, url, content}]``。

    安全边界：**URL 只认服务端 ``web_search_tool_result`` 块**。模型在正文
    JSON 里写的网址只能用来给同 URL 的条目补摘要，绝不能凭它新增一条 ——
    否则「伪造出处」会从模型侧绕回来。
    """
    hits: list[dict[str, str]] = []
    seen: set[str] = set()
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "web_search_tool_result":
            continue
        for item in (b.get("content") or []):
            if not isinstance(item, dict) or item.get("type") != "web_search_result":
                continue
            url = _s(item.get("url"))
            k = _url_key(url)
            if not url or k in seen:
                continue
            seen.add(k)
            hits.append({"title": _s(item.get("title")), "url": url, "content": ""})
    if not hits:
        return []

    # 模型把结果按 URL 摘要回来了 → 给对应条目补正文（对不上的丢弃）
    text = "".join(_s(b.get("text")) for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text")
    parsed = loads_tolerant(text, expect=dict) if text else None
    summaries: dict[str, str] = {}
    for r in ((parsed or {}).get("results") or []):
        if isinstance(r, dict) and _s(r.get("url")):
            summaries[_url_key(r["url"])] = _s(r.get("summary") or r.get("content"))
    for h in hits:
        h["content"] = summaries.get(_url_key(h["url"]), "")[:1200]
    # 服务端一次会返回十几条；调用方要的是 top-N，别把整屏塞进提示词。
    return hits[:max(1, min(int(max_results), 10))]


async def search(session: aiohttp.ClientSession | None, cfg: Any, query: str, *,
                 max_results: int = 5, deep_search: bool = False,
                 topic: str = "general") -> dict[str, Any]:
    """DeepSeek 原生主路 + Tavily 兜底。``session`` 为 ``None`` 时自建。"""
    notes: list[str] = []      # 只在完全没结果时才值得报的说明
    errors: list[str] = []     # 主路降级时总该报出来的
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        try:
            hits = await _native(session, cfg, query, max_results=max_results)
            if hits:
                return {"hits": hits, "answer": "", "provider": "native",
                        "warnings": errors}
            notes.append("DeepSeek 原生搜索零结果")
        except Exception as exc:                          # noqa: BLE001
            errors.append(f"DeepSeek 原生搜索失败：{exc}")

        if _s(getattr(cfg, "search_api_key", "")):
            try:
                got = await _tavily(session, cfg, query, max_results=max_results,
                                    deep_search=deep_search, topic=topic)
                if got["hits"]:
                    return {"hits": got["hits"], "answer": got["answer"],
                            "provider": "tavily", "warnings": errors}
                notes.append("Tavily 零结果")
            except Exception as exc:                      # noqa: BLE001
                errors.append(f"Tavily 失败：{exc}")
        else:
            notes.append("没配 Tavily key（兜底不可用）")
    finally:
        if own_session:
            await session.close()
    return {"hits": [], "answer": "", "provider": "",
            "warnings": errors + notes}
