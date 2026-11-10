"""Text-position helpers: selection locating and chapter type labels.

Shared by the sync route, the chat route and the library handlers.
"""
from __future__ import annotations

import re

from services.library_service import match_in_chapters


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _section_chapters(db, book_id: str, section_index) -> list[int]:
    """Map an epub section (spine position) to DB chapter indices.

    Returns [] for legacy books whose ``spine_index`` was never populated
    (or a section produced no chapter) — callers then keep the old
    fingerprint path unchanged.
    """
    sec = _as_int(section_index)
    if not book_id or sec is None or sec < 0:
        return []
    try:
        rows = db.get_chapters_by_spine(book_id, sec)
    except Exception:
        return []
    return [int(r.chapter_index) for r in rows]


def _norm_match_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _locate_selection(db, book_id: str, text: str,
                      hint: tuple[int, int] | None):
    """Locate a selection's chunk when the client can't supply coordinates.

    Scoped FTS + a real containment check. Ambiguous short words (same word
    in several chapters) are left alone: raising the spoiler gate on a
    coincidence is worse than keeping the fitted position.
    """
    text = (text or "").strip()
    if not book_id or len(text) < 2:
        return None
    try:
        hits = db.search_books(text, limit=12, book_id=book_id)
    except Exception:
        return None
    needle = _norm_match_text(text)
    locs: list[tuple[int, int]] = []
    for h in hits or []:
        ch = _as_int(h.get("chapter_index"))
        ck = _as_int(h.get("chunk_index"))
        if ch is None or ck is None:
            continue
        try:
            chunk = db.get_chunk(book_id, ch, ck)
        except Exception:
            continue
        if chunk and needle in _norm_match_text(chunk.content):
            locs.append((ch, ck))
    if not locs:
        return None
    if len({c for c, _ in locs}) > 1 and len(text) < 4:
        return None
    if hint and hint[0] is not None:
        locs.sort(key=lambda x: (abs(x[0] - hint[0]),
                                 abs(x[1] - (hint[1] or 0))))
    return locs[0]


def _resolve_selection_position(db, book_id, body, position):
    """Return ``(chapter, chunk)`` for the selected book text.

    Precedence: explicit native section → explicit chapter/chunk → FTS on
    the selected text. The chunk may be None when only the chapter is known.
    """
    if not book_id:
        return None, None
    hint = None
    if isinstance(position, dict) and position:
        hch = _as_int(position.get("chapter"))
        if hch is not None:
            hint = (hch, _as_int(position.get("chunk")))

    # Probe = selection plus a short window around it when the client sent
    # one. A bare word can live in several chunks; the window picks the one
    # the reader actually selected.
    sel_text = (body.get("selection") or "").strip()
    probe = (body.get("selection_anchor_text") or sel_text).strip()

    sec = _as_int(body.get("selection_section"))
    if sec is not None and book_id:
        chapters = _section_chapters(db, book_id, sec)
        if chapters:
            ch = (min(chapters, key=lambda i: abs(i - hint[0]))
                  if hint else chapters[0])
            if probe and len(probe) >= 2:
                res = match_in_chapters(db, book_id, [ch], probe, hint)
                if not res and probe != sel_text:
                    res = match_in_chapters(db, book_id, [ch], sel_text, hint)
                if res:
                    return res["chapter_index"], res["chunk_index"]
            return ch, None

    ch = _as_int(body.get("selection_chapter"))
    ck = _as_int(body.get("selection_chunk"))
    if ch is not None:
        # Stale client coordinates (book switched mid-selection, chunk-mode
        # position lagging) must not anchor the context. Verify the text is
        # actually in the claimed chunk; if not, locate it by FTS.
        if ck is not None and probe:
            try:
                chunk = db.get_chunk(book_id, ch, ck)
            except Exception:
                chunk = None
            if chunk and _norm_match_text(probe) not in _norm_match_text(chunk.content):
                loc = _locate_selection(db, book_id, probe, hint)
                if not loc and probe != sel_text:
                    loc = _locate_selection(db, book_id, sel_text, hint)
                if loc:
                    return loc
        return ch, ck

    if (body.get("selection_source") or "book") != "chat":
        loc = _locate_selection(db, book_id, probe, hint)
        if not loc and probe != sel_text:
            loc = _locate_selection(db, book_id, sel_text, hint)
        if loc:
            return loc
    return None, None


_WRITING_SELECTION_LIMIT = 1200


def writing_selection_block(selection: str, source: str) -> str:
    """`<user-selection>` block for the writing page.

    Two sources, both page-local: ``writing`` is a passage the author selected
    in the chapter editor, ``chat`` is a sentence they quoted from the agent's
    previous reply. Neither is reading-page context.

    The editor passage is verbatim chapter text and the frontend flushes the
    editor before sending, so it is already on disk — that makes it usable as
    an ``anchor_quote`` for ``writing_replace_text`` and findable in
    ``writing_read_chapter``. Truncation is 1200 chars rather than the reading
    page's 200: a writing selection is usually a whole paragraph, and cutting
    at 200 would split the very passage the author wants rewritten.
    """
    body = (selection or "")[: _WRITING_SELECTION_LIMIT]
    if len(selection or "") > _WRITING_SELECTION_LIMIT:
        body += "…（已截断）"
    if source == "workspace":
        if body.endswith("/"):
            return (
                "<user-selection source=\"工作区文件夹\">\n"
                f"  作者把这个工作区文件夹加进了聊天：{body}\n"
                "  先 writing_docs(action=\"list\", path=…) 看里面有什么，"
                "需要时再逐份 read；不要假设内容。\n"
                "</user-selection>"
            )
        return (
            "<user-selection source=\"工作区文件\">\n"
            f"  作者把这份工作区文件加进了聊天：{body}\n"
            "  引用或修改它之前，先 writing_docs(action=\"read\", path=…) "
            "读正文；不要凭文件名猜内容。\n"
            "</user-selection>"
        )
    if source == "chat":
        return (
            "<user-selection source=\"对话引用\">\n"
            f"  作者引用了你之前说过的话: \"{body}\"\n"
            "</user-selection>"
        )
    return (
        "<user-selection source=\"写作正文\">\n"
        f"  作者在正文编辑器里选中了这一段（逐字，已随本轮落盘）: \"{body}\"\n"
        "  这是本轮要处理的对象：改动用它作 writing_replace_text 的"
        " anchor_quote，核对上下文用 writing_read_chapter 读本章。\n"
        "</user-selection>"
    )


def _type_label(chapter_type: str) -> str:
    """Human-readable label for chapter type."""
    labels = {
        "cover": "封面",
        "copyright": "版权",
        "toc": "目录",
        "preface": "前言",
        "body": "正文",
        "postscript": "后记",
        "appendix": "附录",
        "bibliography": "参考",
        "index": "索引",
        "notes": "注释",
    }
    return labels.get(chapter_type, chapter_type)
