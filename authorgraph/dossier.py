"""作者档案的**纯投影层**：数据库行 → API DTO。

三条纪律（设计稿 05 §三）：

1. **不调用 LLM。** 档案页只重排已有证据，不生成新句子。
2. **确定性。** 所有排序、分组、截断都不依赖字典迭代顺序以外的随机性。
3. **事实 / 声音 / 解释 / 来源分区。** 不把 records、claims、corpus、
   voice_traits 混成一个数组 —— 它们混在一起就会被读成同一种可信度。

剧透门全层只算一次（``gate_state``），所有区块共用它的返回值。
``book_id`` 不传是**显式的全集视角**，不是「门坏了」；两种状态在
``gate.mode`` 里必须能分辨。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

from authorgraph.coverage import coverage_block
from authorgraph.gate import gate_state
from authorgraph.scope import book_identity
from storage.authors_db import name_key

log = logging.getLogger("authorgraph")

SECTIONS = ("identity", "timeline", "details", "works", "voice",
            "relations", "themes", "sources")

#: 档案页的区块顺序（前端 tab 默认按这个顺序）。
SECTION_ORDER = ("timeline", "details", "works", "voice", "relations",
                 "themes", "sources")

_POSITION_ZH = {
    "narrator": "作者本人就是叙述者，非虚构",
    "avatar": "作者借一个角色说话（自传体小说一类）—— 语气是他的，经历未必",
    "editor": "作者是整理者/解说者，不是当事人",
    "recorder": "作者记录别人的事，自己不在场",
    "absent": "作者不在这本书里",
    "unknown": "作者的位置没判出来",
}

_SOURCE_LABEL = {"corpus": "书里", "web": "网页", "graph": "书库",
                 "model": "推测"}

_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 300.0          # 设计稿 05 §六：5 分钟


# ── 小工具 ────────────────────────────────────────────────────────────────

def _key(target_type: str, target_id: Any) -> tuple[str, str]:
    return (str(target_type), str(target_id))


def _ev_compact(e: dict[str, Any], target_statement: str = "") -> dict[str, Any]:
    return {
        "evidence_id": e.get("id") or "",
        "source_id": e.get("source_id") or "",
        "quote": e.get("quote") or "",
        "locator": e.get("locator") or "",
        "relation": e.get("relation") or "supports",
        "confidence": e.get("confidence", 0.5),
        "source_url": e.get("source_url") or e.get("canonical_url") or "",
        "source_title": e.get("source_title") or "",
        "source_tier": e.get("source_tier"),
        "source_kind": e.get("source_kind") or "",
        "source_status": e.get("source_status") or "",
        "target_type": e.get("target_type") or "",
        "target_id": e.get("target_id") or "",
        "target_statement": target_statement,
    }


def _is_verified(e: dict[str, Any]) -> bool:
    return ((e.get("relation") or "") == "supports"
            and bool((e.get("quote") or "").strip()))


def _normalize_sections(raw: str) -> set[str]:
    if not raw:
        return set(SECTIONS)
    wanted = {x.strip() for x in raw.split(",") if x.strip()}
    return (wanted & set(SECTIONS)) or set(SECTIONS)


def _resolve_stable(books_db: Any, book_id: str,
                    books: list[dict[str, Any]]) -> tuple[str, bool]:
    """``book_id`` → ``stable_book_id``，并说明**是否真的对上了**。

    ``book_identity`` 在拿不到 import_log 时会静默退回 ``book_id`` ——
    那种情况下 ``gate_state`` 一条都查不到，表现与「序跋真没年份」一模一样。
    所以这里多两步兜底，并把 ``mapped`` 显式返回。
    """
    if not book_id:
        return "", False
    stable = book_id
    mapped = False
    if books_db is not None:
        try:
            ident = book_identity(books_db, book_id)
            stable = ident.get("stable_book_id") or book_id
            mapped = stable != book_id
        except Exception:                                 # noqa: BLE001
            stable = book_id
    if not mapped:
        row = next((b for b in books
                    if (b.get("book_id") or "") == book_id
                    and (b.get("stable_book_id") or "")), None)
        if row:
            stable = row["stable_book_id"]
            mapped = True
    if not mapped and any((b.get("stable_book_id") or "") == book_id
                          for b in books):
        mapped = True
    return stable, mapped


def _choose_book_row(books: list[dict[str, Any]],
                     stable: str) -> dict[str, Any]:
    rows = [b for b in books if not stable or b.get("stable_book_id") == stable]
    rows = rows or books
    # 主作者优先；同书多行（合著/译者）时不能让译者行抢走位置。
    rows.sort(key=lambda b: (0 if b.get("role") == "author" else 1,
                             b.get("time_updated") or 0))
    return rows[0] if rows else {}


def _book_title(books_db: Any, book_id: str) -> str:
    if not book_id or books_db is None:
        return ""
    try:
        b = books_db.get_book(book_id)
        return (getattr(b, "title", "") or "") if b else ""
    except Exception:                                     # noqa: BLE001
        return ""


def _gate_block(adb: Any, author_id: str, book_id: str, stable: str,
                mapped: bool,
                max_chapter: int | None) -> tuple[dict[str, Any], bool]:
    """返回 ``(gate_dto, gated)``。``gated=True`` 表示 fail-closed。"""
    if not book_id:
        return ({"mode": "no_book", "max_chapter": None, "max_year": None,
                 "reason": "no_book", "dated": 0, "ahead": 0,
                 "note": "未指定书 —— 这是作者全集视角，不施剧透门。"}, False)
    if max_chapter is None:
        note = ("显式全集视角：不施剧透门。" if mapped else
                "书的 stable id 没解析出来；没有 max_chapter，所以按全集视角展示。")
        return ({"mode": "no_gate" if mapped else "unmapped",
                 "max_chapter": None, "max_year": None,
                 "reason": "no_gate" if mapped else "unmapped",
                 "dated": 0, "ahead": 0, "note": note}, False)
    if not mapped:
        return ({"mode": "unmapped", "max_chapter": max_chapter,
                 "max_year": None, "reason": "unmapped", "dated": 0,
                 "ahead": 0,
                 "note": ("这本书的 stable id 没解析出来，算不出读者此刻的时间点；"
                          "按 fail-closed 处理，不展示作者材料。")}, True)
    gs = gate_state(adb, author_id, stable, max_chapter)
    if gs.get("gate") is None:
        why = gs.get("reason") or "no_dated"
        note = ("这本书的序跋里没有带年份的语料，算不出读者此刻的时间点；"
                "按 fail-closed 处理，不展示作者材料。"
                if why == "no_dated" else
                f"带年份的材料全在你还没读到的位置（{gs.get('ahead', 0)} 条）。"
                "定位不到时间线就无法证明安全，所以一条都不展示。")
        return ({"mode": why, "max_chapter": max_chapter, "max_year": None,
                 "reason": why, "dated": gs.get("dated", 0),
                 "ahead": gs.get("ahead", 0), "note": note}, True)
    return ({"mode": "chapter", "max_chapter": max_chapter,
             "max_year": gs.get("gate"), "reason": "reader_position",
             "dated": gs.get("dated", 0), "ahead": gs.get("ahead", 0),
             "note": ""}, False)


# ── 区块 ──────────────────────────────────────────────────────────────────

def _identity_block(adb: Any, author: dict[str, Any]) -> dict[str, Any]:
    aid = author["author_id"]
    return {
        "author_id": aid,
        "canonical_name": author.get("canonical_name") or "",
        "name_native": author.get("name_native") or "",
        "birth_year": author.get("birth_year"),
        "death_year": author.get("death_year"),
        "birth_place": author.get("birth_place") or "",
        "death_place": author.get("death_place") or "",
        "nationality": author.get("nationality") or "",
        "primary_language": author.get("primary_language") or "",
        "era_note": author.get("era_note") or "",
        "identity_status": author.get("identity_status") or "provisional",
        "bio_status": author.get("bio_status") or "none",
        "identifiers": [
            {"scheme": i.get("scheme") or "", "value": i.get("value") or "",
             "source": i.get("source") or "",
             "confidence": i.get("confidence", 0.5)}
            for i in adb.identifiers_of(aid)
        ],
        "aliases": adb.aliases_of(aid),
    }


def _position_block(adb: Any, books_db: Any, author_id: str, books: list[dict],
                    book_id: str, stable: str) -> dict[str, Any]:
    titles = {b.get("stable_book_id"): _book_title(books_db, b.get("book_id") or "")
              for b in books}
    rows = []
    for b in books:
        st = b.get("stable_book_id") or ""
        rows.append({
            "book_id": b.get("book_id") or "",
            "stable_book_id": st,
            "title": titles.get(st) or "",
            "role": b.get("role") or "author",
            "position": b.get("position") or "unknown",
            "position_note": b.get("position_note") or "",
            "voice_status": b.get("voice_status") or "none",
            "voice_updated_at": b.get("voice_updated_at"),
        })
    sel = _choose_book_row(books, stable)
    out = {
        "book_id": sel.get("book_id") or book_id,
        "stable_book_id": sel.get("stable_book_id") or stable,
        "title": titles.get(sel.get("stable_book_id")) or "",
        "role": sel.get("role") or "author",
        "position": sel.get("position") or "unknown",
        "position_note": sel.get("position_note") or "",
        "position_label": _POSITION_ZH.get(sel.get("position") or "unknown", ""),
        "voice_status": sel.get("voice_status") or "none",
        "voice_updated_at": sel.get("voice_updated_at"),
        "books": rows,
    }
    return out


def _record_dto(r: dict[str, Any], ev: list[dict[str, Any]],
                verified: bool) -> dict[str, Any]:
    return {
        "id": r.get("id"),
        "year": r.get("year"),
        "year_end": r.get("year_end"),
        "statement": r.get("statement") or "",
        "kind": r.get("kind") or "",
        "source": r.get("source") or "model",
        "source_label": _SOURCE_LABEL.get(r.get("source") or "model", ""),
        "verified": bool(verified),
        "url": ((r.get("evidence_excerpt") or "")
                if (r.get("source") or "") == "web" else ""),
        "evidence": ev,
    }


def _timeline_block(records: list[dict[str, Any]], gate: dict[str, Any],
                    total: int, gated: bool) -> dict[str, Any]:
    work: list[dict[str, Any]] = []
    life: list[dict[str, Any]] = []
    undated: dict[str, int] = {"work": 0, "life": 0}
    undated_items: list[dict[str, Any]] = []
    for r in records:
        room = ("work" if r.get("kind") in ("work", "publication") else "life")
        if r.get("year") is None:
            undated[room] += 1
            if len(undated_items) < 60:
                undated_items.append({
                    "id": r.get("id"), "statement": r.get("statement") or "",
                    "kind": r.get("kind") or "", "room": room,
                    "source": r.get("source") or "",
                    "source_label": r.get("source_label") or "",
                    "verified": r.get("verified", False),
                })
            continue
        (work if room == "work" else life).append(r)
    years = [x["year"] for x in work + life if x.get("year") is not None]
    out: dict[str, Any] = {
        "span": {"min": min(years) if years else None,
                 "max": max(years) if years else None},
        "work": work,
        "life": life,
        "undated": undated,
        "undated_items": undated_items,
        "gated": gated,
    }
    if gate.get("mode") == "chapter":
        out["note"] = (f"只展示读者已读到的位置（第 {gate['max_chapter']} 章，"
                       f"截至 {gate['max_year']} 年）。")
    elif gated:
        out["note"] = gate.get("note") or "作者材料被剧透门挡住。"
    else:
        out["note"] = ""
    if not work and not life and not undated_items:
        out["note"] = out["note"] or "这位作者还没有任何带年份的条目。"
    if total > len(records):
        out["not_shown"] = max(0, total - len(records))
    return out


#: 书名号里的才是书名。``kind='work'`` 的编年条目经常是**写作事件**
#: （「开始发表作品」「参加编辑部聚会」），不是作品名 —— 直接当作品列，
#: 作品页会变成一条条句子。
_TITLE_RE = re.compile(r"《([^》]{1,60})》")


#: 书名号后面跟着这些词的，通常是**刊物/出版社**，不是作者的作品。
_ORG_TAIL = re.compile(
    r"^\s*(编辑部|杂志社|杂志|出版社|报社|丛书|文学杂志|文学期刊)")


def _titles_in(statement: str) -> list[str]:
    out: list[str] = []
    s = statement or ""
    for m in _TITLE_RE.finditer(s):
        t = m.group(1).strip()
        if _ORG_TAIL.match(s[m.end():m.end() + 8]):
            continue
        if t and t not in out:
            out.append(t)
    return out


def _works_block(records: list[dict[str, Any]], books: list[dict[str, Any]],
                 books_db: Any, stable: str) -> dict[str, Any]:
    """作品：本地书 + 编年条目里**书名号抽出的作品名**。

    没有书名号的 ``work/publication`` 条目是写作事件，不算作品 —— 它们
    已经在年谱的作品轨里，不在这里重复成一张「作品卡」。
    """
    local: dict[str, dict[str, Any]] = {}
    for b in books:
        st = b.get("stable_book_id") or ""
        title = _book_title(books_db, b.get("book_id") or "")
        key = name_key(title) if title else st
        item = local.get(key)
        if item is None:
            item = {
                "kind": "local", "title": title or st,
                "book_id": b.get("book_id") or "", "stable_book_id": st,
                "local": True, "year": None, "role": b.get("role") or "author",
                "position": b.get("position") or "unknown",
                "voice_status": b.get("voice_status") or "none",
                "source": "book", "source_label": "书库",
                "verified": False, "statement": "",
                "book_ids": [b.get("book_id")] if b.get("book_id") else [],
            }
            local[key] = item
        elif b.get("book_id") and b["book_id"] not in item["book_ids"]:
            item["book_ids"].append(b["book_id"])
    bib: dict[str, dict[str, Any]] = {}
    events = 0
    for r in records:
        if r.get("kind") not in ("work", "publication"):
            continue
        stmt = r.get("statement") or ""
        titles = _titles_in(stmt)
        if not titles:
            events += 1
            continue
        for title in titles:
            k = name_key(title)
            hit = local.get(k)
            if hit is not None:
                if hit.get("year") is None and r.get("year") is not None:
                    hit["year"] = r["year"]
                if r.get("verified"):
                    hit["verified"] = True
                continue
            cur = bib.get(k)
            if cur is not None:
                if cur.get("year") is None and r.get("year") is not None:
                    cur["year"] = r["year"]
                continue
            bib[k] = {
                "kind": "bibliography", "title": title, "book_id": "",
                "stable_book_id": "", "local": False, "year": r.get("year"),
                "role": "", "position": "", "voice_status": "",
                "source": r.get("source") or "",
                "source_label": r.get("source_label") or "",
                "verified": r.get("verified", False), "statement": stmt,
                "book_ids": [],
            }
    items = sorted(
        list(local.values()) + list(bib.values()),
        key=lambda x: (x.get("year") is None, x.get("year") or 0,
                       x.get("title") or ""))
    out = {
        "items": items,
        "counts": {"local": len(local), "bibliography": len(bib),
                   "total": len(items), "events": events},
    }
    if not items:
        out["note"] = ("还没有作品条目 —— 序跋采集与网页补全都可能补出书目。"
                       if not events else
                       f"有 {events} 条写作事件，但还没抽出作品名；"
                       "它们已经在年谱的作品轨里。")
    return out


def _pick_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按场景轮转取前 12 段，再按阅读顺序排 —— 既覆盖场景，又好读。"""
    order = ("opening", "transition", "dialogue", "description", "action",
             "introspection", "emotional_peak", "quiet", "argument",
             "landscape", "memory", "ending", "other")
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        scene = s.get("scene_type") or "other"
        groups.setdefault(scene, []).append(s)
    scenes = [x for x in order if x in groups] + [
        x for x in groups if x not in order]
    picked: list[dict[str, Any]] = []
    while len(picked) < 12:
        added = False
        for scene in scenes:
            bucket = groups.get(scene) or []
            if bucket:
                picked.append(bucket.pop(0))
                added = True
                if len(picked) >= 12:
                    break
        if not added:
            break
    picked.sort(key=lambda s: (s.get("chapter_index") is None,
                               s.get("chapter_index") or 0,
                               s.get("chunk_index") or 0,
                               s.get("id") or 0))
    return picked


def _sample_dto(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "corpus_id": s.get("id"),
        "excerpt": s.get("excerpt") or "",
        "scene_type": s.get("scene_type") or "other",
        "narrative_layer": s.get("narrative_layer") or "",
        "pov": s.get("pov") or "",
        "chapter_index": s.get("chapter_index"),
        "chunk_index": s.get("chunk_index"),
        "book_id": s.get("book_id") or "",
        "stable_book_id": s.get("stable_book_id") or "",
        "era_year": s.get("era_year"),
        "word_count": s.get("word_count") or 0,
        "source": {
            "source_id": s.get("source_id") or "",
            "tier": s.get("source_tier"),
            "kind": s.get("source_kind") or (
                "book" if s.get("stable_book_id") else ""),
            "url": s.get("canonical_url") or "",
            "title": s.get("source_title") or "",
        },
    }


def _voice_block(adb: Any, author_id: str, *, stable: str, book_id: str,
                 mapped: bool, gate: dict[str, Any], gated: bool,
                 samples_all: list[dict[str, Any]],
                 traits_all: list[dict[str, Any]],
                 coverage: dict[str, Any]) -> dict[str, Any]:
    max_chapter = gate.get("max_chapter")
    # 声音样本有 chapter_index，可以**按章直接施门**。年份门算不出来时
    # （序跋没抽到年份）仍然能安全展示读者已读章节里的正文 —— 那不是
    # 预测未来，是他刚读过的文字。records/claims 仍然 fail-closed。
    chapter_ok = bool(book_id and mapped and max_chapter is not None)
    if chapter_ok:
        visible = [s for s in samples_all
                   if isinstance(s.get("chapter_index"), int)
                   and s["chapter_index"] <= max_chapter]
    elif gated:
        visible = []
    else:
        visible = list(samples_all)
    blanked = gated and not chapter_ok
    hidden_gate = max(0, len(samples_all) - len(visible))
    shown = _pick_samples(visible)
    hidden_limit = max(0, len(visible) - len(shown))

    trait_items: list[dict[str, Any]] = []
    hidden_traits = 0
    for t in traits_all:
        ev_rows = adb.trait_evidence(t["trait_id"])
        if chapter_ok:
            ev_rows = [
                e for e in ev_rows
                if (e.get("stable_book_id") or "") != stable
                or (isinstance(e.get("chapter_index"), int)
                    and e["chapter_index"] <= max_chapter)
            ]
        elif gated:
            # 书都没对上时，这本书的证据一条都不能信；别的书不受影响。
            ev_rows = [e for e in ev_rows
                       if (e.get("stable_book_id") or "") != stable]
        if (book_id and mapped and (t.get("scope_ref") or "") == stable
                and not ev_rows):
            hidden_traits += 1
            continue
        ranked_evidence = [
            _ev_compact({
                "id": f"trait:{t['trait_id']}:{e.get('id')}",
                "quote": e.get("excerpt") or "",
                "source_id": e.get("source_id") or "",
                "source_url": e.get("canonical_url") or "",
                "source_title": e.get("source_title") or "",
                "source_tier": e.get("source_tier"),
                "source_kind": e.get("source_kind") or "",
                "relation": "supports",
                "target_type": "voice_trait",
                "target_id": t["trait_id"],
            })
            for e in ev_rows
        ]
        trait_items.append({
            "trait_id": t["trait_id"],
            "dimension": t["dimension"],
            "voice_domain": t.get("voice_domain") or "unknown",
            "value": t.get("value") or "",
            "statement": t.get("statement") or "",
            "strength": t.get("strength", 0.5),
            "confidence": t.get("confidence", 0.3),
            "status": t.get("status") or "provisional",
            "transferable": bool(t.get("transferable")),
            "scope_type": t.get("scope_type") or "work",
            "scope_ref": t.get("scope_ref") or "",
            "evidence_count": len(ev_rows),
            "evidence": ranked_evidence,
        })

    dims: dict[str, int] = {}
    for t in trait_items:
        dims[t["dimension"]] = dims.get(t["dimension"], 0) + 1
    status = "author"
    updated_at = None
    if book_id and mapped:
        st = adb.voice_status_of(author_id, stable)
        status = st.get("voice_status") or "none"
        updated_at = st.get("voice_updated_at")
    out: dict[str, Any] = {
        "status": status,
        "gated": blanked,
        "gate_mode": ("chapter_only" if gated and chapter_ok
                      else gate.get("mode")),
        "books_ready": coverage["counts"].get("books_ready", 0),
        "traits": trait_items,
        "samples": [_sample_dto(s) for s in shown],
        "dimensions": [{"dimension": d, "traits": n}
                       for d, n in sorted(dims.items())],
        "coverage": coverage,
        "not_shown": {
            "samples": hidden_gate + hidden_limit,
            "reason": ("reader_chapter" if hidden_gate else
                       ("limit" if hidden_limit else "")),
            "hidden_by_gate": hidden_gate,
            "hidden_by_limit": hidden_limit,
            "traits": hidden_traits,
        },
    }
    if updated_at is not None:
        out["updated_at"] = updated_at
    if not trait_items:
        if blanked:
            out["note"] = gate.get("note") or "作者材料被剧透门挡住。"
        elif gated:
            out["note"] = ("年份门算不出来，声音只按已读章节展示；"
                           "稳定的文风结论可能还在后面。")
        elif not samples_all:
            out["note"] = ("这本书还没有跑声音提取"
                           if book_id else "还没有声音样本。")
        else:
            out["note"] = "有声音样本，但还不足以稳定判断文风。"
    return out


def _relations_block(adb: Any, author_id: str, gate: dict[str, Any],
                     gated: bool, ev_by_target: dict) -> dict[str, Any]:
    all_rows = adb.relations_of(author_id)
    max_year = gate.get("max_year")
    if gated:
        rows = []
    elif max_year is not None:
        rows = [r for r in all_rows
                if isinstance(r.get("year_start"), int)
                and r["year_start"] <= max_year]
    else:
        rows = all_rows
    items = []
    for r in rows:
        ev = [_ev_compact(e, r.get("statement") or "")
              for e in ev_by_target.get(_key("relation", r["relation_id"]), [])]
        items.append({
            "relation_id": r["relation_id"],
            "relation_type": r.get("relation_type") or "other",
            "other_name": r.get("other_name") or "",
            "other_author_id": r.get("other_author_id") or "",
            "other_canonical_name": r.get("other_canonical") or "",
            "other_identity_status": r.get("other_identity_status") or "",
            "direction": r.get("direction") or "outgoing",
            "year_start": r.get("year_start"),
            "year_end": r.get("year_end"),
            "statement": r.get("statement") or "",
            "confidence": r.get("confidence", 0.3),
            "status": r.get("status") or "provisional",
            "evidence": ev,
        })
    out = {"items": items, "counts": {"total": len(all_rows),
                                      "shown": len(items)}}
    if not all_rows:
        out["note"] = ("关系还没提取 —— M2 只采事实与声音；"
                       "关系补全会在网页 facet 管线里跑。")
    elif not items:
        out["note"] = gate.get("note") or "关系条目都在读者还没读到的位置。"
    return out


def _claims_block(adb: Any, author_id: str, facet: str, gate: dict[str, Any],
                  gated: bool, ev_by_target: dict) -> list[dict[str, Any]]:
    claims = adb.claims_for(author_id, facet=facet, limit=200)
    max_year = gate.get("max_year")
    if gated:
        claims = []
    elif max_year is not None:
        claims = [c for c in claims
                  if isinstance(c.get("year"), int) and c["year"] <= max_year]
    out = []
    for c in claims:
        out.append({
            "claim_id": c["claim_id"],
            "facet": c.get("facet") or facet,
            "theme_key": c.get("theme_key") or "",
            "statement": c.get("statement") or "",
            "year": c.get("year"),
            "confidence": c.get("confidence", 0.3),
            "status": c.get("status") or "provisional",
            "evidence": [_ev_compact(e, c.get("statement") or "")
                         for e in ev_by_target.get(
                             _key("claim", c["claim_id"]), [])],
        })
    return out


def _sources_block(adb: Any, author_id: str, *, stable: str, book_id: str,
                   mapped: bool, gated: bool, max_chapter: int | None,
                   evidence: list[dict[str, Any]],
                   visible_targets: set[tuple[str, str]] | None,
                   statement_of: dict[tuple[str, str], str],
                   books_db: Any, books: list[dict[str, Any]],
                   limit: int = 60) -> dict[str, Any]:
    rows = adb.sources_of_author(author_id)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for e in evidence:
        sid = e.get("source_id") or ""
        if not sid:
            continue
        key = _key(e.get("target_type") or "", e.get("target_id") or "")
        if visible_targets is not None and key not in visible_targets:
            continue
        by_source.setdefault(sid, []).append(e)
    titles = {b.get("stable_book_id"): _book_title(books_db, b.get("book_id") or "")
              for b in books}
    items = []
    counts = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0, "legacy": 0,
              "total": len(rows)}
    hidden_quotes = 0
    for s in rows:
        ev = by_source.get(s["id"], [])
        shown_ev = [_ev_compact(e, statement_of.get(
            _key(e.get("target_type") or "", e.get("target_id") or ""), ""))
            for e in ev]
        hidden = max(0, int(s.get("evidence_count") or 0) - len(ev))
        hidden_quotes += hidden
        tier = int(s.get("source_tier") or 4)
        if tier <= 1:
            counts["tier1"] += 1
        elif tier == 2:
            counts["tier2"] += 1
        elif tier == 3:
            counts["tier3"] += 1
        else:
            counts["tier4"] += 1
        if s.get("source_kind") == "book":
            st = (s.get("canonical_url") or "").split(":", 1)[-1]
            local_title = titles.get(st) or ""
            if not local_title:
                local_title = _book_title(books_db, book_id) if mapped else ""
            title = local_title or s.get("title") or "书"
        else:
            title = s.get("title") or s.get("publisher") or ""
        quote_verified = sum(1 for e in ev if _is_verified(e))
        legacy = sum(1 for e in ev
                     if (e.get("relation") or "") == "legacy_url_only")
        counts["legacy"] += legacy
        items.append({
            "source_id": s["id"],
            "url": s.get("url") or "",
            "canonical_url": s.get("canonical_url") or "",
            "title": title,
            "publisher": s.get("publisher") or "",
            "tier": tier,
            "kind": s.get("source_kind") or "unknown",
            "language": s.get("language") or "",
            "status": s.get("status") or "active",
            "retrieved_at": s.get("retrieved_at"),
            "published_at": s.get("published_at") or "",
            "roles": s.get("roles") or [],
            "evidence_count": len(ev),
            "total_evidence": int(s.get("evidence_count") or 0),
            "quote_verified": quote_verified,
            "legacy_only": legacy,
            "hidden_quotes": hidden,
            "evidence": shown_ev,
        })
    items.sort(key=lambda x: (x["tier"], x["title"], x["source_id"]))
    shown = items[:limit]
    out: dict[str, Any] = {
        "counts": counts,
        "items": shown,
        "not_shown": max(0, len(items) - len(shown)),
        "hidden_quotes": hidden_quotes,
        "gated": bool(visible_targets is not None),
    }
    if not items:
        out["note"] = ("还没有来源台账 —— 序跋采集与网页补全都会写 source/evidence。"
                       if not gated else
                       gate_note(gated, max_chapter))
    return out


def gate_note(gated: bool, max_chapter: int | None) -> str:
    if gated and max_chapter is not None:
        return "来源条目被剧透门挡住：读者还没读到的引文不展示。"
    return "还没有来源台账。"


def _conflicts_block(evidence: list[dict[str, Any]],
                     visible_targets: set[tuple[str, str]] | None,
                     statement_of: dict[tuple[str, str], str]
                     ) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in evidence:
        if (e.get("relation") or "") != "contradicts":
            continue
        key = _key(e.get("target_type") or "", e.get("target_id") or "")
        if visible_targets is not None and key not in visible_targets:
            continue
        groups.setdefault(key, []).append(e)
    out = []
    for (tt, tid), rows in groups.items():
        same = [e for e in evidence
                if _key(e.get("target_type") or "", e.get("target_id") or "")
                == (tt, tid)]
        out.append({
            "target_type": tt,
            "target_id": tid,
            "statement": statement_of.get((tt, tid), ""),
            "contradicts": [_ev_compact(e, statement_of.get((tt, tid), ""))
                            for e in rows],
            "supports": [_ev_compact(e, statement_of.get((tt, tid), ""))
                         for e in same if _is_verified(e)],
        })
    out.sort(key=lambda x: (x["target_type"], x["target_id"]))
    return out


# ── 主投影 ────────────────────────────────────────────────────────────────

def _details_block(adb: Any, author_id: str, *, stable: str,
                   books_db: Any, gated: bool,
                   max_chapter: int | None) -> dict[str, Any]:
    """观察与钩子 —— 纹理层。**按章施门，fail-closed。**"""
    obs = adb.observations_for(author_id, status="accepted", limit=1000)
    hooks = adb.hooks_for(author_id, status="open", limit=500)
    if stable:
        obs = [o for o in obs if (o.get("stable_book_id") or "") == stable]
        hooks = [h for h in hooks
                 if (h.get("stable_book_id") or "") == stable]

    def _visible(ch: Any) -> bool:
        # 指定了阅读进度就只放已读章节；没有章号的一律不放（证明不了安全）。
        if max_chapter is None:
            return not gated
        if ch is None:
            return False
        return int(ch) <= int(max_chapter)

    total_obs = len(obs)
    obs = [o for o in obs if _visible(o.get("chapter_index"))]
    hooks = [h for h in hooks if _visible(h.get("chapter_index"))]
    titles: dict[str, str] = {}
    for b in adb.books_of(author_id):
        sb = b.get("stable_book_id") or ""
        if sb:
            titles[sb] = _book_title(books_db, b.get("book_id") or "")
    items = [{
        "observation_id": o.get("observation_id"),
        "topic": o.get("topic") or "other",
        "statement": o.get("statement") or "",
        "quote": o.get("quote") or "",
        "basis": o.get("basis") or "",
        "basis_quote": o.get("basis_quote") or "",
        "speaker": o.get("speaker") or "",
        "use": o.get("use") or "dossier",
        "confidence": o.get("confidence") or "low",
        "stable_book_id": o.get("stable_book_id") or "",
        "book_title": titles.get(o.get("stable_book_id") or "", ""),
        "chapter_index": o.get("chapter_index"),
        "chunk_id": o.get("chunk_id") or "",
    } for o in obs]
    hook_items = [{
        "hook_id": h.get("hook_id"),
        "topic": h.get("topic") or "other",
        "quote": h.get("quote") or "",
        "known": h.get("known") or "",
        "missing": h.get("missing") or "",
        "why": h.get("why") or "",
        "suggested_action": h.get("suggested_action") or "web_search",
        "stable_book_id": h.get("stable_book_id") or "",
        "book_title": titles.get(h.get("stable_book_id") or "", ""),
        "chapter_index": h.get("chapter_index"),
    } for h in hooks]
    note = ""
    if gated and max_chapter is None:
        note = "阅读进度不明，纹理与钩子暂不展示。"
    elif total_obs > len(items):
        note = f"{total_obs - len(items)} 条观察在未读章节，已挡住。"
    return {"items": items, "hooks": hook_items,
            "counts": {"observations": len(items), "hooks": len(hook_items)},
            "note": note}


def build_dossier(adb: Any, *, author_id: str, book_id: str = "",
                  max_chapter: int | None = None, books_db: Any = None,
                  sections: Iterable[str] | str | None = None,
                  use_cache: bool = True) -> dict[str, Any]:
    """一个作者的完整档案投影。**不 raise 业务错误**，用 ``found``/``note``。"""
    if isinstance(sections, str):
        want = _normalize_sections(sections)
    else:
        want = set(sections or SECTIONS) & set(SECTIONS) or set(SECTIONS)
    key = (author_id, book_id, max_chapter, frozenset(want))
    if use_cache:
        hit = _CACHE.get(key)
        if hit and (time.time() - hit[0]) < _CACHE_TTL:
            return hit[1]

    author = adb.get_author(author_id)
    if not author:
        return {"found": False, "author_id": author_id,
                "error": "author_not_found",
                "note": "作者层还没有这个人 —— 先跑序跋采集。"}

    books = adb.books_of(author_id)
    stable, mapped = _resolve_stable(books_db, book_id, books)
    gate, gated = _gate_block(adb, author_id, book_id, stable, mapped,
                              max_chapter)

    stats = adb.stats(author_id)
    all_records = adb.records_for(author_id, limit=2000)
    records = ([] if gated else adb.records_for(
        author_id, max_year=gate.get("max_year"), limit=1000))
    evidence = adb.evidence_for_author(author_id)
    ev_by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
    statement_of: dict[tuple[str, str], str] = {}
    for e in evidence:
        k = _key(e.get("target_type") or "", e.get("target_id") or "")
        ev_by_target.setdefault(k, []).append(e)
    for r in all_records:
        statement_of[_key("record", r.get("id"))] = r.get("statement") or ""
    verified_records = {
        str(e.get("target_id")) for e in evidence
        if (e.get("target_type") or "") == "record" and _is_verified(e)
    }

    # 时间轴记录挂证据；来源抽屉点卡片时不用再发一次请求。
    rec_items = []
    for r in records:
        k = _key("record", r.get("id"))
        rec_items.append(_record_dto(
            r, [_ev_compact(e, r.get("statement") or "")
                for e in ev_by_target.get(k, [])],
            str(r.get("id")) in verified_records))

    claims_theme = _claims_block(adb, author_id, "theme", gate, gated,
                                 ev_by_target)
    claims_reception = _claims_block(adb, author_id, "reception", gate, gated,
                                     ev_by_target)
    for c in claims_theme + claims_reception:
        statement_of[_key("claim", c["claim_id"])] = c["statement"]

    relations = _relations_block(adb, author_id, gate, gated, ev_by_target)
    for r in relations["items"]:
        statement_of[_key("relation", r["relation_id"])] = r["statement"]

    works = _works_block(records, books, books_db, stable)
    samples_all = adb.voice_samples(
        author_id,
        stable_book_id=(stable if book_id and mapped else ""),
        limit=1000)
    traits_all = adb.voice_traits(
        author_id, statuses=("active", "provisional"), limit=500)
    vcov = adb.voice_coverage(author_id)
    coverage = coverage_block(
        records=stats.get("records", 0),
        records_with_evidence=len(verified_records),
        works=len(works["items"]),
        voice_samples=vcov.get("voice_corpus", 0),
        scenes=vcov.get("scenes", 0),
        scene_types={s.get("scene_type") for s in samples_all
                     if s.get("scene_type")},
        relations=sum(1 for r in adb.relations_of(author_id)
                      if (r.get("status") or "") in ("active", "confirmed")),
        verified_sources=sum(1 for s in adb.sources_of_author(author_id)
                             if int(s.get("quote_verified") or 0) > 0),
        sources=len(adb.sources_of_author(author_id)),
        traits=vcov.get("traits", 0),
        dimensions=vcov.get("dimensions", 0),
    )
    coverage["counts"]["books_ready"] = vcov.get("books_ready", 0)

    voice = _voice_block(
        adb, author_id, stable=stable, book_id=book_id, mapped=mapped,
        gate=gate, gated=gated, samples_all=samples_all,
        traits_all=traits_all, coverage=coverage)
    voice["books"] = [{
        "stable_book_id": b.get("stable_book_id") or "",
        "book_id": b.get("book_id") or "",
        "title": _book_title(books_db, b.get("book_id") or ""),
        "voice_status": b.get("voice_status") or "none",
        "position": b.get("position") or "unknown",
    } for b in books]

    # 来源/冲突的可见目标：门开着时只算展示出来的引文。
    if max_chapter is None:
        visible_targets: set[tuple[str, str]] | None = None
    else:
        visible_targets = {_key("record", r["id"]) for r in rec_items}
        visible_targets |= {_key("claim", c["claim_id"])
                            for c in claims_theme + claims_reception}
        visible_targets |= {_key("relation", r["relation_id"])
                            for r in relations["items"]}
        # 声音行按章施门，所以即使年份门算不出来，已读章节的样本仍可见。
        visible_targets |= {_key("corpus", s["corpus_id"])
                            for s in voice["samples"]}
        # 身份字段不随章节变，始终可见。
        visible_targets |= {k for k in ev_by_target
                            if k[0] == "author_field"}
    sources = _sources_block(
        adb, author_id, stable=stable, book_id=book_id, mapped=mapped,
        gated=gated, max_chapter=max_chapter, evidence=evidence,
        visible_targets=visible_targets, statement_of=statement_of,
        books_db=books_db, books=books)
    conflicts = _conflicts_block(evidence, visible_targets, statement_of)

    themes = claims_theme
    reception = claims_reception
    details = _details_block(
        adb, author_id, stable=stable if mapped else "", books_db=books_db,
        gated=gated, max_chapter=max_chapter)
    out: dict[str, Any] = {
        "found": True,
        "author": _identity_block(adb, author),
        "position": _position_block(adb, books_db, author_id, books, book_id,
                                    stable),
        "coverage": coverage,
        "gate": gate,
        "not_shown": {
            "records": max(0, stats.get("records", 0) - len(rec_items)),
            # 指定了书就按这本书算；没指定才按作者全部声音语料算。
            "corpus": (int(voice["not_shown"]["samples"])
                       if book_id and mapped else
                       max(0, vcov.get("voice_corpus", 0)
                           - len(voice["samples"]))),
            "note": "含读者还没读到的、无年的、超出上限的",
        },
        "conflicts": conflicts,
        "sections": [s for s in SECTION_ORDER if s in want],
        "notes": {},
    }
    if "timeline" in want:
        out["timeline"] = _timeline_block(
            rec_items, gate, stats.get("records", 0), gated)
    if "details" in want:
        out["details"] = details
    if "works" in want:
        out["works"] = works["items"]
        out["works_meta"] = {"counts": works["counts"],
                             "note": works.get("note", "")}
    if "voice" in want:
        out["voice"] = voice
    if "relations" in want:
        out["relations"] = relations["items"]
        out["relations_meta"] = {"counts": relations["counts"],
                                 "note": relations.get("note", "")}
    if "themes" in want:
        out["themes"] = themes
        out["reception"] = reception
        if not themes and not reception:
            out["notes"]["themes"] = (
                "还没有主题/接受史陈述 —— 它们来自网页补全的解释层，"
                "不是序跋采集的事实层。")
    if "sources" in want:
        out["sources"] = sources
    if not out["sections"]:
        out["sections"] = list(SECTION_ORDER)

    if use_cache:
        _CACHE[key] = (time.time(), out)
        # 简单的上界：作者数不多，但别让长期运行的进程无限长。
        if len(_CACHE) > 256:
            for k in list(_CACHE)[:64]:
                _CACHE.pop(k, None)
    return out


def clear_cache() -> None:
    """写操作后清缓存。TTL 只是兜底，不是正确性依赖。"""
    _CACHE.clear()


def author_list(adb: Any, *, q: str = "", limit: int = 20, offset: int = 0,
                mode: str = "auto") -> dict[str, Any]:
    """作者搜索/列表。默认**前缀匹配**，``mode=fuzzy`` 才做子串。"""
    rows = adb.list_authors(limit=5000)
    qk = name_key(q) if q else ""
    matched: list[dict[str, Any]] = []
    for a in rows:
        keys = [name_key(a.get("canonical_name") or "")]
        keys += [name_key(x) for x in adb.aliases_of(a["author_id"])]
        keys = [k for k in keys if k]
        if not qk:
            matched.append(a)
            continue
        if mode == "exact":
            hit = any(k == qk for k in keys)
        elif mode == "fuzzy":
            hit = any(qk in k for k in keys)
        else:
            hit = any(k.startswith(qk) for k in keys)
        if hit:
            matched.append(a)
    matched.sort(key=lambda a: (a.get("canonical_name") or "").casefold())
    total = len(matched)
    page = matched[max(0, offset):max(0, offset) + max(1, limit)]
    items = []
    for a in page:
        aid = a["author_id"]
        st = adb.stats(aid)
        books = adb.books_of(aid)
        vcov = adb.voice_coverage(aid)
        works = len({(b.get("stable_book_id") or "")
                     for b in books if b.get("book_id")})
        cov = coverage_block(
            records=st.get("records", 0),
            records_with_evidence=len({
                str(e.get("target_id"))
                for e in adb.evidence_for_author(aid)
                if (e.get("target_type") or "") == "record" and _is_verified(e)
            }),
            works=works,
            voice_samples=vcov.get("voice_corpus", 0),
            scenes=vcov.get("scenes", 0),
            relations=sum(1 for r in adb.relations_of(aid)
                          if (r.get("status") or "") in ("active", "confirmed")),
            verified_sources=sum(1 for s in adb.sources_of_author(aid)
                                 if int(s.get("quote_verified") or 0) > 0),
            sources=len(adb.sources_of_author(aid)),
            traits=vcov.get("traits", 0),
            dimensions=vcov.get("dimensions", 0),
        )
        items.append({
            "author_id": aid,
            "canonical_name": a.get("canonical_name") or "",
            "name_native": a.get("name_native") or "",
            "birth_year": a.get("birth_year"),
            "death_year": a.get("death_year"),
            "nationality": a.get("nationality") or "",
            "identity_status": a.get("identity_status") or "provisional",
            "book_count": len(books),
            "corpus": st.get("corpus", 0),
            "records": st.get("records", 0),
            # 写作透镜的可选性：有已就绪的书 + 可迁移的声音才算「能用」。
            "voice_ready": bool(vcov.get("books_ready")),
            "voice_samples": vcov.get("voice_corpus", 0),
            "voice_traits": vcov.get("traits", 0),
            "coverage": {k: cov[k] for k in
                         ("facts", "works", "voice", "relations", "sources",
                          "overall")},
            "missing": cov["missing"],
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


__all__ = ["SECTIONS", "SECTION_ORDER", "build_dossier", "author_list",
           "clear_cache"]
