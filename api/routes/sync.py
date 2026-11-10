"""Koodo-style reading-position sync (the /api/sync fingerprint bridge)."""
from __future__ import annotations

from aiohttp import web

from api.book_helpers import (
    _adjacent_chapter, _find_book_by_path, _find_book_by_text,
)
from api.text_helpers import _as_int, _section_chapters, _type_label
from services.library_service import match_in_chapters, match_page_text



def _sync_trace(msg: str) -> None:
    """Append a reader-sync trace line — **默认关闭**的调试通道。

    阅读页每滚一次屏就写两行（recv + hit/miss）：两个多月 6500 行，
    实际读它的只有排查视窗对齐/定章那几次；常开就是纯噪声（决策 147）。排查时设
    ``BC_READER_SYNC_TRACE=1`` 再启服务。日志路径按仓库根解析，不跟 cwd 走。
    """
    try:
        import time as _t

        from log_lifecycle import append_line, log_dir, sync_trace_enabled
        if not sync_trace_enabled():
            return
        append_line(log_dir() / "reader_sync.log",
                    _t.strftime("%m-%d %H:%M:%S") + " " + msg)
    except Exception:
        pass


_LAST_SYNC_MISMATCH: dict[str, tuple] = {}


async def handle_sync(request: web.Request) -> web.Response:
    """Receive page-turn event from the reader frontend.

    POST body: {bookPath, bookId, pageText, focusText, cfi, topSection,
                focusSection, visibleSections, hintChapter, hintChunk}

    Position has two coordinates with different jobs:
      * ``topSection`` + ``pageText``  — restore anchor (viewport top)
      * ``focusSection`` + ``focusText`` — reading intent (largest visible
        section). The section maps to DB chapter(s); the text picks the
        chunk inside that scope. This is what prevents a viewport-top
        fingerprint at a chapter boundary from trapping progress in the
        previous chapter.

    Legacy clients that send only ``pageText`` keep the old fingerprint
    path unchanged.
    """
    db = request.app["db"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    page_text = body.get("pageText", "") or ""
    focus_text = body.get("focusText", "") or ""
    chapter_title = body.get("chapterTitle", "")
    book_path = body.get("bookPath", "")
    book_id = body.get("bookId", "")
    # epub.js CFI of the viewport top — the render layer's authoritative
    # coordinate. Stored alongside (chapter, chunk) so reopening the book
    # can restore via a single deterministic display(cfi) instead of
    # replaying the fingerprint anchor chain.
    cfi = body.get("cfi", "") or ""
    # Structural coordinates from the renderer. ``topSection`` is the
    # restore anchor (viewport top); ``focusSection`` is the section that
    # occupies most of the viewport (reading intent). They differ at a
    # chapter boundary — the exact case where the old fingerprint chain
    # trapped progress in the previous chapter.
    top_section = _as_int(body.get("topSection"))
    focus_section = _as_int(body.get("focusSection"))
    visible_sections = body.get("visibleSections") or []

    if not page_text and not focus_text:
        return web.json_response({"matched": False, "reason": "empty page text"})

    _sync_trace(
        f"recv book={book_id or book_path} record={body.get('record', True)} "
        f"cfi={cfi[:48]!r} top_sec={top_section} focus_sec={focus_section} "
        f"text={(page_text or focus_text)[:30]!r}")

    # Find book: native reader sends bookId directly; Koodo-style hooks
    # send bookPath (hashed to book_id) or fall back to text search.
    if book_id:
        if not db.get_book(book_id):
            _sync_trace(f"miss: book {book_id} not in library")
            return web.json_response(
                {"matched": False, "reason": "book not found in library"}
            )
    else:
        # Find book by file path → MD5
        book_id = _find_book_by_path(db, book_path)
        if not book_id:
            # Try matching across all books
            book_id = _find_book_by_text(db, page_text or focus_text)

        if not book_id:
            return web.json_response(
                {"matched": False, "reason": "book not found in library"}
            )

    # Fingerprint match. hint = reader's last known position: consecutive
    # scroll reports are always adjacent, so the matcher resolves repeated
    # short text to the nearest occurrence instead of the book's earliest
    # (which teleported progress backwards across chapters).
    hint = None
    _hint_ch = body.get("hintChapter")
    _hint_ck = body.get("hintChunk")
    if _hint_ch is not None:
        try:
            hint = (int(_hint_ch), int(_hint_ck) if _hint_ck is not None else None)
        except (TypeError, ValueError):
            hint = None

    # ── Structural chapter, textual chunk ──────────────────────────────
    # The renderer knows which epub section is on screen. Map that section
    # (spine position) to DB chapter(s), then let the text pick the chunk
    # inside that scope. The old path let the viewport-top fingerprint +
    # hint decide the *chapter*, which self-locks in the previous chapter
    # at every boundary (screen on ch24, progress stuck on ch23).
    focus_chapters = _section_chapters(db, book_id, focus_section)
    visible_chapters: list[int] = []
    if isinstance(visible_sections, list):
        for item in visible_sections:
            sec = _as_int(item.get("sectionIndex")) if isinstance(item, dict) else None
            for ch in _section_chapters(db, book_id, sec):
                if ch not in visible_chapters:
                    visible_chapters.append(ch)
    visible_chapters.sort()

    result = None
    source = "fingerprint"
    if focus_chapters:
        result = match_in_chapters(
            db, book_id, focus_chapters, focus_text or page_text, hint)
        if result:
            source = "structural"

    # Diagnostic: what would the viewport-top fingerprint have said? Only
    # computed when the two structural coordinates disagree, so the common
    # path stays one match.
    top_result = None
    if top_section is not None and focus_section is not None \
            and top_section != focus_section:
        top_result = match_page_text(
            db, book_id, chapter_title, page_text or focus_text, hint=hint)

    if result is None:
        # No spine mapping (legacy import) — still prefer the focus text:
        # the top fingerprint is the previous chapter's tail at a boundary,
        # while the focus text is what the reader is actually looking at.
        result = match_page_text(
            db, book_id, chapter_title, focus_text or page_text, hint=hint)
    if not result:
        _sync_trace(f"miss: no chunk match text={(page_text or focus_text)[:40]!r}")
        return web.json_response(
            {"matched": False, "reason": "no chunk match", "book_id": book_id}
        )
    result["chapter_source"] = source
    if focus_section is not None:
        result["section_index"] = focus_section

    mismatch = None
    if top_result and top_result.get("chapter_index") != result.get("chapter_index"):
        mismatch = {
            "top_section": top_section,
            "focus_section": focus_section,
            "top_chapter": top_result.get("chapter_index"),
            "top_chunk": top_result.get("chunk_index"),
            "intent_chapter": result.get("chapter_index"),
            "intent_chunk": result.get("chunk_index"),
            "visible_chapters": visible_chapters,
        }
        _mkey = (top_section, focus_section,
                 top_result.get("chapter_index"),
                 result.get("chapter_index"))
        if _LAST_SYNC_MISMATCH.get(book_id) != _mkey:
            _LAST_SYNC_MISMATCH[book_id] = _mkey
            _sync_trace(
                f"mismatch top_sec={top_section} focus_sec={focus_section} "
                f"top_ch={top_result.get('chapter_index')} "
                f"intent_ch={result.get('chapter_index')} "
                f"visible={visible_chapters} "
                f"fp={(page_text or focus_text)[:24]!r}")

    # Record position in reading session. The native reader reports on
    # every scroll — dedup identical positions so positions_json doesn't
    # bloat with consecutive duplicates.
    #
    # record=False: the reader's programmatic position restore only
    # refreshes the UI. Recording the restore landing spot (section top /
    # book-start fallback) would overwrite the real progress and loop
    # "reopen → back to the start".
    if body.get("record", True):
        session = db.get_active_session(book_id)
        if not session:
            session = db.start_reading_session(book_id)
        # 不再按 (chapter, chunk) 提前挡掉：行级锚（决策 143）会在同一个
        # chunk 内不断变化，record_position 自己按块原地更新，既不 append
        # 也不会把位置历史撑大。
        db.record_position(
            session.id, result["chapter_index"], result["chunk_index"], cfi
        )
    else:
        session = db.get_active_session(book_id)   # 仅用于返回 session_id
    session_id = session.id if session else ""
    _sync_trace(f"hit ch={result['chapter_index']} ck={result['chunk_index']} "
                f"src={source} "
                f"{'recorded' if session_id and body.get('record', True) else 'ui-only'}")

    # Get chapter type for breadcrumb
    ch = db.get_chapter(book_id, result["chapter_index"])
    chapter_type = ch.chapter_type if ch else "body"

    # Build breadcrumb
    all_chapters = db.get_chapters(book_id)
    prev_ch = _adjacent_chapter(all_chapters, result["chapter_index"], -1)
    next_ch = _adjacent_chapter(all_chapters, result["chapter_index"], +1)
    breadcrumb = f"{_type_label(chapter_type)} · "
    breadcrumb += f"前: {prev_ch.title if prev_ch else '无'} | "
    breadcrumb += f"后: {next_ch.title if next_ch else '无'}"

    return web.json_response({
        "matched": True,
        "position": {
            **result,
            "chapter_type": chapter_type,
            "breadcrumb": breadcrumb,
        },
        "structural": {
            "top_section": top_section,
            "focus_section": focus_section,
            "focus_chapters": focus_chapters,
            "visible_chapters": visible_chapters,
        },
        "mismatch": mismatch,
        "session_id": session_id,
    })
