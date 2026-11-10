"""Append-only journal for user note/highlight changes.

``annotations`` stores only the final state: an update overwrites the old
value and a delete is a physical ``DELETE``.  This module writes one journal
row at the moment a user creates / edits / removes a note or highlight, so
the agent can answer "what did I change or delete recently?" — the same
shape as the book lifecycle log (decision 170), scoped to annotations.

The write is deliberately best-effort: a failed journal insert must never
break the user's note action.  A warning is logged instead.
"""
from __future__ import annotations

import json
import logging

from storage.db import BookDB, _now_ms, _new_id
from storage.models import AnnotationEventRow, AnnotationRow

logger = logging.getLogger("annotations")

VALID_EVENT_TYPES = frozenset({"create", "update", "delete"})


def record_annotation_event(
    db: BookDB,
    annotation: AnnotationRow,
    event_type: str,
    *,
    changed_fields: list[str] | tuple[str, ...] = (),
    time_created: int | None = None,
) -> AnnotationEventRow | None:
    """Append one journal row for *annotation*.

    ``time_created`` lets the backfill script replay historical timestamps;
    live callers omit it and get ``now``.  Returns the row, or ``None`` when
    the insert failed (already logged).
    """
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(f"unknown annotation event type: {event_type}")

    try:
        book_title = ""
        if annotation.book_id:
            book = db.get_book(annotation.book_id)
            if book is not None:
                book_title = book.title or ""

        row = AnnotationEventRow(
            id=_new_id("ann-ev"),
            annotation_id=annotation.id,
            stable_book_id=annotation.stable_book_id,
            book_id=annotation.book_id,
            book_title=book_title,
            event_type=event_type,
            annotation_type=annotation.type or "",
            style=annotation.style,
            selected_text=annotation.selected_text or "",
            note_text=annotation.note_text,
            changed_fields=json.dumps(list(changed_fields), ensure_ascii=False),
            chapter_index=annotation.chapter_index,
            chunk_index=annotation.chunk_index,
            time_created=int(time_created) if time_created is not None else _now_ms(),
        )
        db.insert_annotation_event(row)
        return row
    except Exception:
        logger.warning(
            "Failed to record annotation event %s for %s",
            event_type, getattr(annotation, "id", "?"), exc_info=True,
        )
        return None
