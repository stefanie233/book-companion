"""事实补全管线 —— 计划 → 搜索 → 抓取 → 一页一抽 → 校验 → 合并 → 落库。

设计稿 03 §四。与旧 ``enrich_life`` 的区别不是「多抓了几页」，而是三件事：

1. **证据从 URL 升级成逐字引文。** 抓不到正文的来源只能做线索，
   不能写 ``source='web'``；抓到了也要引文能在正文里逐字找到。
2. **按 facet 补，不按一句 query 打天下。** 先身份、再书目、最后解释；
   身份不明时**不补生平**（同名会串账）。
3. **失败分类可分辨。** ``zero_results`` / ``fetch_blocked`` /
   ``quote_not_found`` / ``tier_too_low`` 各自计数，不压成一句「搜不到」。

触发：按需（``refresh_author.py --web`` / 作者页 / 对话深挖）。
**不挂书籍导入与图谱提取** —— 那是声音管线的事（设计稿 08）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import aiohttp

from authorgraph.identity import (decide, norm_name, score_candidate,
                                  wikidata_candidates)
from authorgraph.sources.extract import extract_page
from authorgraph.sources.fetch import fetch_page
from authorgraph.sources.merge import (classify, index_existing,
                                        merge_claims, norm_statement)
from authorgraph.sources.query_plan import coverage_snapshot, plan_queries
from authorgraph.sources.registry import (canonical_url, roles_of,
                                          source_kind_of, tier_of)
from authorgraph.sources.verify import verify_claim
from services.web_search_service import search as web_search
from storage.authors_db import SOURCE_TRUST, AuthorsDB

log = logging.getLogger("authorgraph")

#: 解释性陈述的目标（落 ``author_claims``），其余落 ``author_records``。
_CLAIM_TARGETS = ("theme", "reception", "poetics", "context")


def _new_manifest(author: dict[str, Any]) -> dict[str, Any]:
    return {
        "author_id": (author.get("author_id") or ""),
        "name": (author.get("canonical_name") or ""),
        "queries": [], "hits": 0, "fetched": 0, "blocked": 0,
        "fetch_attempts": 0,
        "claims_extracted": 0, "claims_verified": 0,
        "quote_not_found": 0, "year_mismatch": 0, "role_mismatch": 0,
        "tier_too_low": 0, "target_unknown": 0, "quote_empty": 0,
        "empty_statement": 0,
        "new_records": 0, "duplicate_records": 0, "claims": 0,
        "added": 0, "skipped": 0,
        "conflicts": [], "conflict_count": 0, "budget_exhausted": False,
        "pages": [], "warnings": [], "failures": [], "defects": [],
        "identity": {}, "status": "ok",
    }


def _source_by_canonical(adb: AuthorsDB, cu: str) -> dict[str, Any] | None:
    row = adb.get_conn().execute(
        "SELECT * FROM author_sources WHERE canonical_url=?", (cu,)).fetchone()
    return dict(row) if row else None


def _record_page_source(adb: AuthorsDB, page: dict[str, Any], *,
                        tier: int, kind: str, roles: tuple[str, ...],
                        status: str = "active") -> str:
    """先记来源再记证据 —— 抓取失败也要留痕（``status=blocked``）。"""
    sid = adb.upsert_source(
        url=page.get("url") or "",
        canonical_url=page.get("canonical_url") or "",
        title=page.get("title") or "",
        publisher=page.get("publisher") or "",
        source_tier=tier, source_kind=kind,
        language=page.get("language") or "",
        published_at=page.get("published_at") or "",
        content_hash=page.get("content_hash") or "",
        content_excerpt=page.get("content_excerpt") or "",
        status=status)
    if sid and roles:
        adb.add_source_roles(sid, roles)
    return sid


async def _resolve_identity(session: aiohttp.ClientSession, adb: AuthorsDB,
                            author: dict[str, Any]) -> dict[str, Any]:
    """身份不明时先查 ID。失败/无候选**不阻断**后面的补全（但要报）。"""
    aid = author.get("author_id") or ""
    name = (author.get("canonical_name") or "").strip()
    if not aid or not name:
        return {"status": "no_name"}
    if (author.get("identity_status") or "") == "resolved":
        return {"status": "resolved", "skipped": True,
                "identifiers": [i["value"] for i in adb.identifiers_of(aid)]}
    cands = await wikidata_candidates(session, name)
    if not cands:
        return {"status": "no_candidates",
                "note": "Wikidata 没返回候选（网络失败或查无此人）"}
    books = [str(b.get("title") or b.get("book_title") or "")
             for b in (author.get("_books") or [])]
    books += [str(t) for t in (author.get("_book_titles") or [])]
    scored = [(c, score_candidate(
        c, name=name, birth_year=author.get("birth_year"),
        death_year=author.get("death_year"), books=books,
        nationality=author.get("nationality") or "")) for c in cands]
    # 唯一候选且名字逐字相同 → 可以 resolved；这是打破「身份死锁」的口子
    # （没有生年 → 分数不够 → 永远 provisional → 永远不会去补生年）。
    unique_exact = len(cands) == 1 and norm_name(name) in (
        {norm_name(cands[0].label)}
        | {norm_name(a) for a in cands[0].aliases})
    d = decide(scored, unique_exact=unique_exact)
    out: dict[str, Any] = {"status": d.status, "score": d.score,
                           "margin": d.margin, "note": d.note}
    if d.candidate is not None:
        out["qid"] = d.candidate.value
        out["label"] = d.candidate.label
    if d.status == "resolved" and d.candidate is not None:
        adb.add_identifier(aid, "wikidata", d.candidate.value,
                           source="wikidata_api", confidence=d.score)
        fields: dict[str, Any] = {"identity_status": "resolved"}
        if not author.get("birth_year") and d.candidate.birth_year:
            fields["birth_year"] = d.candidate.birth_year
        if not author.get("death_year") and d.candidate.death_year:
            fields["death_year"] = d.candidate.death_year
        if not author.get("nationality") and d.candidate.nationality:
            fields["nationality"] = d.candidate.nationality
        if d.candidate.label and d.candidate.label != name:
            fields.setdefault("name_native", d.candidate.label)
        adb.set_identity_fields(aid, **fields)
    elif d.status == "conflict":
        adb.set_identity_fields(aid, identity_status="conflict")
    else:
        adb.set_identity_fields(aid, identity_status="provisional")
    return out


def _find_existing(index: dict[str, list[dict[str, Any]]],
                   claim: dict[str, Any]) -> dict[str, Any] | None:
    rows = index.get(norm_statement(claim.get("statement") or "")) or []
    if not rows:
        return None
    for r in rows:
        if ((r.get("kind") or "life_event") == claim.get("kind")
                and r.get("year") == claim.get("year")):
            return r
    return rows[0]


async def enrich_author(session: aiohttp.ClientSession, cfg: Any,
                        adb: AuthorsDB, author: dict[str, Any], *,
                        facets: list[str] | None = None,
                        max_queries: int = 6, max_pages: int = 12,
                        per_query: int = 8, force: bool = False,
                        log_cb: Callable[[dict], None] | None = None
                        ) -> dict[str, Any]:
    """补一位作者。返回 manifest（含旧 ``enrich_life`` 的兼容键）。"""
    m = _new_manifest(author)
    aid = (author.get("author_id") or "").strip()
    if not aid:
        m["status"] = "error"
        m["warnings"].append("没有 author_id —— 补全没有落点")
        return m

    m["identity"] = await _resolve_identity(session, adb, author)
    if m["identity"].get("status") == "conflict":
        m["status"] = "conflict"
        m["warnings"].append("身份候选冲突：已停止自动补全，需人工确认")
        adb.set_bio_status(aid, "error", "identity_conflict")
        return m

    coverage = coverage_snapshot(adb, aid)
    # 身份查询刚刚已经试过（Wikidata）；再只跑身份就会死锁，所以放行其余 facet，
    # 但身份查询仍排第一，且条目全部保留 provisional 标记。
    queries = plan_queries(author, coverage, facets=facets,
                           max_queries=max_queries, strict_identity=False)
    if (m["identity"].get("status") not in ("resolved", "skipped")
            and coverage.get("identity_status") != "resolved"):
        m["warnings"].append("身份未确认：本次补到的条目按「待确认」处理，"
                             "不要把别处的同名材料当成本人")
    m["queries"] = queries
    if not queries:
        m["status"] = "nothing_missing"
        return m

    existing_rows = adb.records_for(aid, limit=2000)
    existing_index = index_existing(existing_rows)
    prompt_existing = [r.get("statement") or "" for r in existing_rows[:60]
                       if r.get("statement")]
    seen: set[str] = set()
    verified: list[dict[str, Any]] = []

    for q in queries:
        if m["fetch_attempts"] >= max_pages:
            m["budget_exhausted"] = True
            break
        try:
            found = await web_search(session, cfg, q["query"],
                                     max_results=per_query)
        except Exception as exc:                      # noqa: BLE001
            m["failures"].append(f"search_failed:{q['facet']}:{exc}")
            continue
        m["warnings"].extend(found.get("warnings") or [])
        hits = found.get("hits") or []
        m["hits"] += len(hits)
        if not hits:
            m["failures"].append(f"zero_results:{q['facet']}")
            continue
        for h in hits:
            if m["fetch_attempts"] >= max_pages:
                m["budget_exhausted"] = True
                break
            url = (h.get("url") or "").strip()
            cu = canonical_url(url)
            if not cu or cu in seen:
                continue
            seen.add(cu)
            tier = tier_of(url)
            if tier >= 4:
                m["failures"].append(f"lead_only:{cu}")
                continue
            cached = _source_by_canonical(adb, cu)
            if cached and (cached.get("content_hash") or "") and not force:
                m["failures"].append(f"cached:{cu}")
                continue
            m["fetch_attempts"] += 1
            page = await fetch_page(session, url)
            kind = source_kind_of(url)
            roles = roles_of(kind, tier)
            if page.get("status") != "ok":
                m["blocked"] += 1
                m["failures"].append(f"fetch_{page.get('status')}:{cu}")
                _record_page_source(adb, page, tier=tier, kind=kind,
                                    roles=roles, status="blocked")
                continue
            sid = _record_page_source(adb, page, tier=tier, kind=kind,
                                      roles=roles)
            m["fetched"] += 1
            m["pages"].append({"url": url, "title": page.get("title") or "",
                               "tier": tier, "kind": kind})
            ex = await extract_page(session, cfg, page, author,
                                    existing=prompt_existing, log_cb=log_cb)
            m["defects"].extend(ex.get("defects") or [])
            for c in ex.get("claims") or []:
                m["claims_extracted"] += 1
                v = verify_claim(c, page, roles=roles, tier=tier)
                code = v.get("code") or ""
                if code in m:
                    m[code] += 1
                for d in v.get("defects") or []:
                    m["defects"].append(f"{cu}: {d}")
                if not v["ok"]:
                    m["failures"].append(f"{code or 'rejected'}:{cu}")
                    continue
                claim = v["claim"]
                claim["evidence"] = [{
                    "source_id": sid, "quote": claim["evidence_quote"],
                    "locator": claim.get("locator") or "",
                    "relation": "supports",
                    "confidence": claim.get("confidence") or 0.5}]
                verified.append(claim)
                m["claims_verified"] += 1

    added = dup = 0
    for c in merge_claims(verified):
        ev = c.get("evidence") or []
        target = c.get("target") or "biography"
        if target in _CLAIM_TARGETS:
            cid = adb.add_claim(aid, target, c["statement"],
                                year=c.get("year"),
                                confidence=c.get("confidence") or 0.5,
                                status="active")
            if cid:
                for e in ev:
                    adb.add_evidence(aid, "claim", cid, **e)
                m["claims"] += 1
            continue
        verdict = classify(c, existing_index)
        row = _find_existing(existing_index, c)
        if verdict == "duplicate":
            dup += 1
            if row:
                for e in ev:
                    adb.add_evidence(aid, "record", str(row["id"]), **e)
            continue
        if verdict == "conflict":
            m["conflicts"].append(
                f"「{c['statement']}」年份不一致：台账 {row.get('year') if row else '?'}"
                f" / 网页 {c.get('year')} —— 只报告，不覆盖")
            if row:
                for e in ev:
                    adb.add_evidence(aid, "record", str(row["id"]),
                                     **dict(e, relation="contradicts"))
            continue
        first = ev[0] if ev else {}
        rid = adb.add_record_ex(
            aid, c["kind"], c["statement"], year=c.get("year"),
            year_end=c.get("year_end"), evidence_excerpt=first.get("quote") or "",
            source="web", trust=SOURCE_TRUST.get("web"))
        if rid is not None:
            for e in ev:
                adb.add_evidence(aid, "record", str(rid), **e)
            added += 1

    m["added"], m["skipped"] = added, dup
    m["new_records"], m["duplicate_records"] = added, dup
    m["conflict_count"] = len(m["conflicts"])
    after = coverage_snapshot(adb, aid)
    m["coverage_after"] = after
    if not after.get("missing"):
        adb.set_bio_status(aid, "full")
    elif added or m["claims"] or m["fetched"]:
        adb.set_bio_status(aid, "partial")
    elif m["failures"]:
        adb.set_bio_status(aid, "error", m["failures"][0])
    if m["budget_exhausted"]:
        m["warnings"].append("达到抓取/查询预算，下次续跑")
    return m
