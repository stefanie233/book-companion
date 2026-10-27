"""Annotation panel endpoints: chapter-type overrides + notes/highlights."""
from __future__ import annotations

from aiohttp import web

from api.text_helpers import _type_label
from file_readers.epub_semantics import VALID_CHAPTER_TYPES



# ── Chapter type overrides（覆盖层便利贴 · 标注面板）───────────────────────


async def handle_list_chapter_types(request: web.Request) -> web.Response:
    """List all chapters with effective type + override marker for the tagging panel.

    Lightweight (no chunk content). Each entry carries:
      type          — effective type (override already applied by get_chapters)
      overridden    — whether a human sticky-note exists
      original_type — raw (AI-guessed/import) type → what clearing falls back to
      note / source — sticky-note metadata, '' when not overridden
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    book = db.get_book(book_id)
    if not book:
        return web.json_response({"error": "Book not found"}, status=404)

    chapters = db.get_chapters(book_id)
    source_types = db.get_chapter_source_types(book_id)
    overrides = db.list_chapter_overrides(book_id)

    out = []
    for ch in chapters:
        ov = overrides.get(ch.chapter_index)
        out.append({
            "chapter_index": ch.chapter_index,
            "title": ch.title or "",
            "char_count": ch.char_count or 0,
            "type": ch.chapter_type,                       # 已盖便利贴
            "overridden": ov is not None,
            "original_type": source_types.get(ch.chapter_index, "body"),
            "note": ov.note if ov else "",
            "source": ov.source if ov else "",
        })
    return web.json_response({
        "book": {"id": book.id, "title": book.title},
        "chapters": out,
    })


async def handle_set_override(request: web.Request) -> web.Response:
    """Stick a human override（贴便利贴/更新）on one chapter's type."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    try:
        index = int(request.match_info["index"])
    except ValueError:
        return web.json_response({"error": "chapter index must be an integer"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    ctype = (body.get("chapter_type") or "").strip()
    note = (body.get("note") or "").strip()
    if ctype not in VALID_CHAPTER_TYPES:
        return web.json_response(
            {"error": f"invalid chapter_type {ctype!r}; valid: {sorted(VALID_CHAPTER_TYPES)}"},
            status=400,
        )
    if not db.get_book(book_id):
        return web.json_response({"error": "Book not found"}, status=404)

    db.set_chapter_override(book_id, index, ctype, note=note)
    return web.json_response({
        "set": True,
        "book_id": book_id,
        "chapter_index": index,
        "chapter_type": ctype,
        "note": note,
        "label": _type_label(ctype),
    })


async def handle_clear_override(request: web.Request) -> web.Response:
    """Remove a human override（撕便利贴），回落原表类型."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    try:
        index = int(request.match_info["index"])
    except ValueError:
        return web.json_response({"error": "chapter index must be an integer"}, status=400)
    if not db.get_book(book_id):
        return web.json_response({"error": "Book not found"}, status=404)

    fallback = db.get_chapter_source_types(book_id).get(index, "body")
    db.clear_chapter_override(book_id, index)
    return web.json_response({
        "cleared": True,
        "book_id": book_id,
        "chapter_index": index,
        "original_type": fallback,
        "label": _type_label(fallback),
    })


# ── Annotations ──────────────────────────────────────────────────────────


async def handle_list_annotations(request: web.Request) -> web.Response:
    """List annotations with three query modes.

    ?stable_book_id=X            — all annotations for a book
    ?stable_book_id=X&chunk=Z    — annotations for a specific chunk
    ?type=note                   — all notes (cross-book)
    """
    db = request.app["db"]
    stable_book_id = request.query.get("stable_book_id", "")
    chunk_str = request.query.get("chunk")
    ann_type = request.query.get("type", "")

    if ann_type in ("note", "highlight"):
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        rows = db.get_annotations_by_type(ann_type, limit=limit, offset=offset)
        return web.json_response({
            "annotations": [_annotation_to_dict(r) for r in rows],
            "count": len(rows),
        })

    if not stable_book_id:
        return web.json_response({"error": "stable_book_id or type=note/highlight required"}, status=400)

    if chunk_str is not None:
        # Chunk-level: need book_id from import_log
        try:
            chunk_index = int(chunk_str)
        except ValueError:
            return web.json_response({"error": "chunk must be an integer"}, status=400)
        import_log = db.get_latest_import_for_stable(stable_book_id)
        if not import_log or not import_log.book_id:
            return web.json_response({"annotations": [], "count": 0})
        chapter_index = int(request.query.get("chapter", "0"))
        rows = db.get_annotations_by_chunk(import_log.book_id, chapter_index, chunk_index)
    else:
        rows = db.get_annotations_by_stable_book(stable_book_id)

    return web.json_response({
        "annotations": [_annotation_to_dict(r) for r in rows],
        "count": len(rows),
    })


async def handle_create_annotation(request: web.Request) -> web.Response:
    """Create a new annotation (highlight, note, or both)."""
    db = request.app["db"]
    from storage.models import AnnotationRow
    from storage.db import _now_ms, _new_id
    from services.library_service import compute_annotation_offsets

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    book_id = str(body.get("book_id", "")).strip()
    if not book_id:
        return web.json_response({"error": "book_id is required"}, status=400)

    # Resolve stable_book_id from import_log
    import_log = db.get_import_log_by_book(book_id)
    if not import_log:
        return web.json_response({"error": f"No import_log found for book_id={book_id}"}, status=404)
    stable_book_id = import_log.stable_book_id

    selected_text = str(body.get("selected_text", "")).strip()
    if not selected_text or len(selected_text) < 2:
        return web.json_response({"error": "selected_text is required (min 2 chars)"}, status=400)

    ann_type = str(body.get("type", "highlight"))
    # v16: type 二分——highlight=收藏（下划线），note=批注；bookmark 废弃
    if ann_type not in ("highlight", "note"):
        ann_type = "highlight"

    color = str(body.get("color", "#FFD54F"))
    note_text = str(body.get("note_text", "")) if body.get("note_text") else None
    # v16: native 标注层——收藏样式（首版仅 underline）+ CFI 精确定位
    style = str(body.get("style", "underline")) if ann_type == "highlight" else None
    cfi_range = str(body.get("cfi_range", "")) or None

    chapter_index = body.get("chapter_index")
    chunk_index = body.get("chunk_index")

    # Compute persistent coordinates
    context_before = str(body.get("context_before", ""))[:40] or ""
    context_after = str(body.get("context_after", ""))[:40] or ""
    offsets = compute_annotation_offsets(
        db, book_id, chapter_index, chunk_index,
        selected_text, context_before, context_after,
    )

    now = _now_ms()
    row = AnnotationRow(
        id=_new_id("ann"),
        stable_book_id=stable_book_id,
        book_id=book_id,
        chapter_index=chapter_index,
        chunk_index=chunk_index,
        selected_text=selected_text,
        context_before=context_before or None,
        context_after=context_after or None,
        # v14 persistent coordinates
        paragraph_index=offsets.get("paragraph_index"),
        offset_in_paragraph=offsets.get("offset_in_paragraph"),
        chapter_offset_start=offsets.get("chapter_offset_start"),
        chapter_offset_end=offsets.get("chapter_offset_end"),
        context_window=offsets.get("context_window"),
        source_session_id=None,  # populated when linked to a conversation session
        chunking_version=1,
        # core fields
        type=ann_type,
        note_text=note_text,
        note_format=str(body.get("note_format", "markdown")),
        color=color,
        style=style,
        cfi_range=cfi_range,
        match_status="linked",
        time_created=now,
        time_updated=now,
    )
    ann_id = db.insert_annotation(row)

    return web.json_response({"id": ann_id, "stable_book_id": stable_book_id}, status=201)


async def handle_update_annotation(request: web.Request) -> web.Response:
    """Update an annotation (color, note_text, type, style, cfi_range)."""
    db = request.app["db"]
    ann_id = request.match_info["ann_id"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    allowed = {"color", "note_text", "note_format", "type", "style", "cfi_range"}
    updates = {k: v for k, v in body.items() if k in allowed and v is not None}
    if not updates:
        return web.json_response({"error": "No valid fields to update"}, status=400)
    if "type" in updates and updates["type"] not in ("highlight", "note"):
        return web.json_response({"error": "type must be highlight or note"}, status=400)

    db.update_annotation(ann_id, **updates)
    return web.json_response({"updated": True, "id": ann_id})


async def handle_delete_annotation(request: web.Request) -> web.Response:
    """Delete an annotation permanently."""
    db = request.app["db"]
    ann_id = request.match_info["ann_id"]

    deleted = db.delete_annotation(ann_id)
    if not deleted:
        return web.json_response({"error": "Annotation not found"}, status=404)

    return web.json_response({"deleted": True, "id": ann_id})




def _annotation_to_dict(r) -> dict:
    return {
        "id": r.id,
        "stable_book_id": r.stable_book_id,
        "book_id": r.book_id,
        "chapter_index": r.chapter_index,
        "chunk_index": r.chunk_index,
        "selected_text": r.selected_text,
        "context_before": r.context_before,
        "context_after": r.context_after,
        # v14 persistent coordinates
        "paragraph_index": r.paragraph_index,
        "offset_in_paragraph": r.offset_in_paragraph,
        "chapter_offset_start": r.chapter_offset_start,
        "chapter_offset_end": r.chapter_offset_end,
        "context_window": r.context_window,
        "source_session_id": r.source_session_id,
        "chunking_version": r.chunking_version,
        # v16 native annotation layer
        "style": r.style,
        "cfi_range": r.cfi_range,
        # core fields
        "type": r.type,
        "note_text": r.note_text,
        "note_format": r.note_format,
        "color": r.color,
        "match_status": r.match_status,
        "time_created": r.time_created,
        "time_updated": r.time_updated,
    }
