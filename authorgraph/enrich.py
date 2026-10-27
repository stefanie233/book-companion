"""工序 6：生平补全 —— **薄封装 + 一条给 agent 用的旧校验器**。

2026-10-26 起，真正的补全在 ``authorgraph/sources/pipeline.py``：
发现 → 抓页面 → 一页一抽 → **逐字引文校验** → 落 source/evidence/record。
本模块只保留两件东西：

1. ``enrich_life()`` —— 脚本与 API 的旧入口，转发到新管线（签名不变）。
2. ``normalize_enrichment()`` —— **对话深挖通道**（``author_scout`` 子代理）
   仍在使用：子代理手里只有搜索结果摘要，没有页面正文，所以那一侧的证据
   只能校验 URL，不能校验引文。它保留 ``source='web'`` 的 URL 校验与
   ``model`` 降级，但不再承担「补全」的编排。

**两条通道落同一张表，却有不同的证据强度**：管线写 ``author_evidence``
（逐字引文），agent 通道写 URL 校验后的记录（``evidence_excerpt`` 是网址）。
以后 agent 通道也要升级到引文 —— 见 ``tools/author.py`` 的抓页工具。
"""

from __future__ import annotations

from typing import Any, Callable

import aiohttp

from config import Config
from storage.authors_db import RECORD_KINDS


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _year(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        y = int(v)
        return y if 1 <= y <= 2999 else None
    digits = "".join(ch for ch in _s(v) if ch.isdigit())
    if len(digits) >= 3:
        y = int(digits[:4])
        return y if 1 <= y <= 2999 else None
    return None


def _rows(v: Any) -> list[dict[str, Any]]:
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        vals = list(v.values())
        if vals and all(isinstance(x, dict) for x in vals):
            return list(vals)
        return [v]
    return []


def _same_site(url: str, allowed: set[str]) -> bool:
    """URL 是否**真的**来自这次搜索结果。

    比字符串相等宽一档：模型常把网址末尾的斜杠、查询串写丢，
    那是转录走形，不是伪造。但**域名必须对上** —— 域名对不上就是编的。
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return False
    if u in allowed:
        return True
    host = u.split("//", 1)[-1].split("/", 1)[0].lower()
    if not host:
        return False
    return any(a.split("//", 1)[-1].split("/", 1)[0].lower() == host
               for a in allowed)


def normalize_enrichment(raw: Any, hits: list[dict[str, Any]],
                         ) -> tuple[dict[str, Any], list[str]]:
    """把模型输出收成落库形状。返回 ``(result, defects)``。

    **出处校验是这里的全部价值**：声称 ``source='web'`` 却给不出这次
    搜索结果里的网址 → 降级成 ``model`` 并报 ``fabricated_source``。
    `model` 不可注入（``card.NON_INJECTABLE_SOURCES``），所以降级是
    **安全的那一边**，但它仍然是账上的一条，要报出来。

    注意：这是**弱证据通道**。能用页面正文的地方应走
    ``sources/verify.py`` 的逐字校验，而不是这里的域名比对。
    """
    defects: list[str] = []
    d = raw if isinstance(raw, dict) else {}
    allowed = {_s(h.get("url")).rstrip("/") for h in hits if _s(h.get("url"))}

    records: list[dict[str, Any]] = []
    for r in _rows(d.get("records")):
        st = _s(r.get("statement"))
        if not st:
            continue
        k = _s(r.get("kind"))
        if k not in RECORD_KINDS:
            if k:
                defects.append(f"kind 词表外：{k!r} → life_event")
            k = "life_event"
        src = _s(r.get("source")) or "model"
        url = _s(r.get("url"))
        if src == "web" and not _same_site(url, allowed):
            defects.append(
                f"fabricated_source：{st[:24]!r} 声称来自网页，"
                f"但 {url[:60]!r} 不在这次搜索结果里 → 降级为 model")
            src, url = "model", ""
        if src not in ("web", "model"):
            src = "model"
        records.append({
            "kind": k, "year": _year(r.get("year")),
            "year_end": _year(r.get("year_end")), "statement": st,
            "evidence_excerpt": url, "source": src,
        })

    conflicts = [_s(x) for x in (d.get("conflicts") or []) if _s(x)] \
        if isinstance(d.get("conflicts"), list) else []
    return {"records": records, "conflicts": conflicts}, defects


async def enrich_life(session: aiohttp.ClientSession, cfg: Config,
                      adb: Any, author: dict[str, Any], *,
                      max_results: int = 5,
                      facets: list[str] | None = None,
                      log_cb: Callable[[dict], None] | None = None
                      ) -> dict[str, Any]:
    """工序 6 的旧入口 —— 转发到新管线，签名与返回键保持兼容。

    新返回值多出 ``identity`` / ``queries`` / ``fetched`` / ``pages`` /
    ``coverage_after`` 等 manifest 字段；旧调用方读 ``hits/added/skipped/
    conflicts/warnings`` 仍然成立。
    """
    from authorgraph.sources.pipeline import enrich_author
    author = dict(author)
    books = [_s(b.get("book_title") or b.get("title"))
             for b in _rows(author.get("_books")) if b]
    if books:
        author["_book_titles"] = books
    return await enrich_author(
        session, cfg, adb, author, facets=facets,
        per_query=max(5, int(max_results)), log_cb=log_cb)
