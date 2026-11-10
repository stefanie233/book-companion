"""用联网闭合一个钩子 —— 只写「页面正文里逐字对得上」的答案。

钩子是任务，不是事实。这里做完三件事：

1. 先用 ``plan_queries`` 让模型把钩子转成短检索词（候选专名可用，
   结论不许写进去），再用这些词去搜索（DeepSeek 优先，回退原生）；
2. 抓页，一页一次问「这一页补上了吗」；
3. 引文逐字命中才落库，然后关闭钩子；否则保持 open。

与 ``pipeline.enrich_author`` 的分工：那边补 facet（生平/书目/关系），
这边只回答**已经在书里冒出来、但没说清**的那一个问题。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

import aiohttp

import usage_log
from bookgraph.llm_call import (finish_reason, output_budget, reasoning_text,
                                usage_tokens)
from config import Config
from llm_json import loads_tolerant
from provider_store import config_for
from storage.authors_db import RECORD_KINDS, SOURCE_TRUST

log = logging.getLogger("authorgraph")

_FALLBACK = """你在读一个网页，回答一个具体问题。只认这一页正文。
严格 JSON：{"answered":true,"statement":"","evidence_quote":"",
"kind":"life_event|work|publication|relation","year":null,"note":""}
evidence_quote 必须是正文里的连续逐字片段；答不上来就 answered=false。"""

_QUERY_FALLBACK = """把钩子转成 1-2 组短检索词（关键词，不是问句），只输出
{"queries":[{"hook_id":"","queries":["作者名 关键词"]}]}，不要补答案。"""


def hook_prompt() -> str:
    from sub_agent.types import load_prompt
    return load_prompt("author_hook", _FALLBACK)


def query_prompt() -> str:
    from sub_agent.types import load_prompt
    return load_prompt("author_query", _QUERY_FALLBACK)


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def build_hook_msg(author: dict[str, Any], hook: dict[str, Any],
                   page: dict[str, Any]) -> str:
    name = _s(author.get("canonical_name")) or "(未知)"
    return "\n".join([
        "# 作者", f"  {name}", "",
        "# 钩子",
        f"  已知：{_s(hook.get('known')) or '(未给)'}",
        f"  缺什么：{_s(hook.get('missing'))}",
        f"  原话：{_s(hook.get('quote'))[:500]}",
        "", "# 这一页",
        f"  标题：{_s(page.get('title')) or '(无题)'}",
        f"  网址：{_s(page.get('url'))}",
        f"  发布者：{_s(page.get('publisher')) or '(未知)'}",
        "", "## 正文", _s(page.get("main_text")), "",
    ])


def _year(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        y = int(v)
        return y if 1000 <= y <= 2999 else None
    digits = "".join(ch for ch in _s(v) if ch.isdigit())
    if len(digits) >= 3:
        y = int(digits[:4])
        return y if 1000 <= y <= 2999 else None
    return None


async def _ask_json(session: aiohttp.ClientSession, cfg: Config, *,
                    system_prompt: str, user_msg: str, author_id: str,
                    log_cb: Callable[[dict], None] | None = None
                    ) -> dict[str, Any] | None:
    """一次严格 JSON 调用。``None`` = 调用失败/解析失败。"""
    cfg = config_for(cfg, "authorgraph.enrich")
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_msg}],
        "stream": False, "temperature": 0.1,
        "max_tokens": output_budget(cfg),
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json"}
    t0 = time.monotonic()

    def _emit(status: str, **kw: Any) -> None:
        usage_log.record(
            source="authorgraph", model=cfg.model,
            tokens_in=int(kw.get("tokens_in") or 0),
            tokens_out=int(kw.get("tokens_out") or 0), status=status,
            ref_id=author_id)
        if log_cb:
            log_cb({"status": status, "system": system_prompt,
                    "prompt": user_msg, "response": kw.get("response") or "",
                    "reasoning_content": kw.get("reasoning") or "",
                    "finish_reason": kw.get("finish") or "",
                    "error_detail": kw.get("error_detail") or "",
                    "model": cfg.model,
                    "latency_ms": int((time.monotonic() - t0) * 1000)})

    try:
        async with session.post(
            f"{cfg.api_base}/chat/completions", json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            if resp.status != 200:
                _emit("api_error", error_detail=(await resp.text())[:300])
                return None
            data = json.loads(await resp.text())
            content = data["choices"][0]["message"].get("content") or ""
            reasoning = reasoning_text(data)
            finish = finish_reason(data)
            tokens_in, tokens_out = usage_tokens(data)
            parsed = loads_tolerant(content, expect=dict)
            if not parsed:
                _emit("parse_failed", response=content, reasoning=reasoning,
                      finish=finish, tokens_in=tokens_in,
                      tokens_out=tokens_out)
                return None
            _emit("ok", response=content, reasoning=reasoning, finish=finish,
                  tokens_in=tokens_in, tokens_out=tokens_out)
            return parsed
    except Exception as exc:                            # noqa: BLE001
        log.warning("hook ask failed: %s", exc)
        _emit("exception", error_detail=str(exc)[:300])
        return None


async def ask_page(session: aiohttp.ClientSession, cfg: Config,
                   author: dict[str, Any], hook: dict[str, Any],
                   page: dict[str, Any], *,
                   log_cb: Callable[[dict], None] | None = None
                   ) -> dict[str, Any] | None:
    """问一页。返回已解析的 JSON；``None`` = 调用失败。"""
    return await _ask_json(
        session, cfg, system_prompt=hook_prompt(),
        user_msg=build_hook_msg(author, hook, page),
        author_id=_s(author.get("author_id")), log_cb=log_cb)


def build_query_msg(author: dict[str, Any],
                    hooks: list[dict[str, Any]]) -> str:
    name = _s(author.get("canonical_name")) or "(未知)"
    lines = ["# 作者", f"  {name}", "", "# 待检索的钩子"]
    for h in hooks:
        lines.extend([
            f"## {_s(h.get('hook_id'))}",
            f"  已知：{_s(h.get('known')) or '(未给)'}",
            f"  缺什么：{_s(h.get('missing'))}",
            f"  原话：{_s(h.get('quote'))[:300]}",
            "",
        ])
    return "\n".join(lines)


def _clean_queries(raw: Any, name: str) -> list[str]:
    """检索词归一：去问句、去重、带作者名、限长。"""
    out: list[str] = []
    seen: set[str] = set()
    for q in raw if isinstance(raw, list) else []:
        q = " ".join(_s(q).split())
        if not q or len(q) > 60:
            continue
        if name and name not in q:
            q = f"{name} {q}"
        k = q.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(q)
        if len(out) >= 2:
            break
    return out


async def plan_queries(session: aiohttp.ClientSession, cfg: Config,
                       author: dict[str, Any],
                       hooks: list[dict[str, Any]], *,
                       log_cb: Callable[[dict], None] | None = None
                       ) -> dict[str, list[str]]:
    """一次调用给所有钩子出检索词；失败返回空表，调用方回退长问句。"""
    if not hooks:
        return {}
    got = await _ask_json(
        session, cfg, system_prompt=query_prompt(),
        user_msg=build_query_msg(author, hooks),
        author_id=_s(author.get("author_id")), log_cb=log_cb)
    if not got:
        return {}
    name = _s(author.get("canonical_name"))
    known = {_s(h.get("hook_id")) for h in hooks}
    out: dict[str, list[str]] = {}
    for row in (got.get("queries") or []):
        if not isinstance(row, dict):
            continue
        hid = _s(row.get("hook_id"))
        if hid not in known:
            continue
        qs = _clean_queries(row.get("queries"), name)
        if qs:
            out[hid] = qs
    return out


def _provider(cfg: Any) -> tuple[Any, Any]:
    from authorgraph.sources.web_deepseek import provider, resolve
    prov = provider(resolve(cfg))
    if prov:
        return prov
    from authorgraph.sources.fetch import fetch_page
    from services.web_search_service import search as native_search
    return native_search, fetch_page


async def answer_hook(session: aiohttp.ClientSession, cfg: Config,
                      adb: Any, author: dict[str, Any],
                      hook: dict[str, Any], *, max_pages: int = 5,
                      queries: list[str] | None = None,
                      search_fn: Callable[..., Any] | None = None,
                      fetch_fn: Callable[..., Any] | None = None,
                      ask_fn: Callable[..., Any] | None = None,
                      log_cb: Callable[[dict], None] | None = None
                      ) -> dict[str, Any]:
    """尝试用网页闭合一条钩子。**命中引文才关闭**，否则保持 open。

    ``queries`` 来自 ``plan_queries``（模型按训练知识出的短检索词）；没给就
    退回「作者名 + 缺口问句」。多组检索词的结果按 canonical URL 去重后
    一起排队，抓页总数有上限。
    """
    from authorgraph.sources.registry import (canonical_url, roles_of,
                                              source_kind_of, tier_of)
    from authorgraph.sources.verify import quote_located

    hid = _s(hook.get("hook_id"))
    aid = _s(author.get("author_id"))
    if not hid or not aid:
        return {"closed": False, "reason": "hook/author 缺 id"}
    if search_fn is None or fetch_fn is None:
        search_fn, fetch_fn = _provider(cfg)
    if ask_fn is None:
        ask_fn = ask_page
    name = _s(author.get("canonical_name"))
    wanted = [q for q in (queries or []) if _s(q)]
    # 模型出的检索词可能漏掉原标题里的专名；再补一条「作者 + 缺口首句」，
    # 这条是确定性的，不依赖模型这次想起了什么。
    head = re.split(r"[、，。；：？！,.;:?!]", _s(hook.get("missing")))[0][:20]
    # 「柏大夫是谁」→「柏大夫」：问句尾巴会把搜索引擎带偏。
    head = re.sub(r"(是谁|是什么|叫什么|是哪一年|是什么时候|姓名|名字|"
                  r"怎么样|怎么回事)$", "", head).strip()
    if head:
        short = f"{name} {head}".strip()
        if short.lower() not in {q.lower() for q in wanted}:
            # 插在模型第一组之后：抓页总数有上限，兜底词不能被挤到队尾。
            wanted.insert(1 if wanted else 0, short)
    if not wanted:
        wanted = [f"{name} {_s(hook.get('missing'))}".strip()]

    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    search_errors: list[str] = []
    for q in wanted:
        try:
            found = await search_fn(session, cfg, q, max_results=max_pages)
        except Exception as exc:                        # noqa: BLE001
            search_errors.append(str(exc)[:120])
            continue
        for h in ((found or {}).get("hits") or []):
            cu = canonical_url(_s(h.get("url")))
            if not cu or cu in seen:
                continue
            seen.add(cu)
            hits.append(h)
    stats = {"queries": len(wanted), "hits": len(hits), "tried": 0,
             "fetched": 0, "answered": 0, "quote_fail": 0, "ask_fail": 0}
    if search_errors:
        stats["search_errors"] = search_errors[:3]
    if not hits and search_errors:
        return {"closed": False, "stats": stats,
                "reason": f"search_failed:{search_errors[0]}"}
    capped = max(1, max_pages) * 2
    for h in hits:
        if stats["tried"] >= capped:
            break
        url = _s(h.get("url"))
        cu = canonical_url(url)
        if not cu:
            continue
        tier = tier_of(url)
        if tier >= 4:
            continue
        stats["tried"] += 1
        page = await fetch_fn(session, url)
        if page.get("status") != "ok":
            continue
        stats["fetched"] += 1
        parsed = await ask_fn(session, cfg, author, hook, page,
                              log_cb=log_cb)
        if not parsed:
            stats["ask_fail"] += 1
            continue
        if not parsed.get("answered"):
            continue
        stats["answered"] += 1
        quote = _s(parsed.get("evidence_quote"))
        statement = _s(parsed.get("statement"))
        if not statement:
            continue
        if not quote or not quote_located(quote, page.get("main_text") or ""):
            stats["quote_fail"] += 1
            continue
        kind = _s(parsed.get("kind"))
        kind = kind if kind in RECORD_KINDS else "life_event"
        sid = adb.upsert_source(
            url=url, canonical_url=cu, title=_s(page.get("title")),
            publisher=_s(page.get("publisher")), source_tier=tier,
            source_kind=source_kind_of(url), language=_s(page.get("language")),
            published_at=_s(page.get("published_at")),
            content_hash=_s(page.get("content_hash")),
            content_excerpt=_s(page.get("content_excerpt")))
        adb.add_source_roles(sid, roles_of(source_kind_of(url), tier))
        rid = adb.add_record_ex(
            aid, kind, statement,
            year=_year(parsed.get("year")), evidence_excerpt=quote,
            source="web", trust=SOURCE_TRUST.get("web"))
        ev = ""
        if rid is not None:
            ev = adb.add_evidence(
                aid, "record", str(rid), source_id=sid, quote=quote,
                locator=_s(parsed.get("note")), relation="supports",
                confidence=0.7)
        ev_hook = adb.add_evidence(
            aid, "hook", hid, source_id=sid, quote=quote,
            locator=_s(parsed.get("note")), relation="supports",
            confidence=0.7)
        adb.close_hook(hid, closed_by=ev or ev_hook)
        return {"closed": True, "hook_id": hid, "url": url,
                "statement": statement, "stats": stats}
    return {"closed": False, "hook_id": hid, "stats": stats,
            "reason": "这一轮没有页面能把钩子补上"}
