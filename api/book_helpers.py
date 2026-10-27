"""Book lookup / page-navigation helpers shared by sync, books and annotations."""
from __future__ import annotations

from pathlib import Path


def _get_stable_id(db, book_id: str) -> str | None:
    """Resolve stable_book_id for a book, or None if no import_log."""
    log = db.get_import_log_by_book(book_id)
    return log.stable_book_id if log else None



# ── Chunk navigation helpers ────────────────────────────────────────────


def _find_prev_chunk(db, book_id: str, chapter: int, chunk: int) -> dict | None:
    """Find the chunk before (chapter, chunk), crossing chapter boundaries."""
    if chunk > 0:
        # Same chapter, previous chunk
        if db.get_chunk(book_id, chapter, chunk - 1):
            return {"chapter": chapter, "chunk": chunk - 1}
        return None
    # First chunk of chapter — go to previous chapter's last chunk
    if chapter <= 0:
        return None
    prev_total = db.get_chunk_count(book_id, chapter - 1)
    if prev_total == 0:
        return None
    return {"chapter": chapter - 1, "chunk": prev_total - 1}


def _find_next_chunk(db, book_id: str, chapter: int, chunk: int) -> dict | None:
    """Find the chunk after (chapter, chunk), crossing chapter boundaries."""
    total = db.get_chunk_count(book_id, chapter)
    if chunk + 1 < total:
        # Same chapter, next chunk
        return {"chapter": chapter, "chunk": chunk + 1}
    # Last chunk of chapter — go to next chapter's first chunk
    next_total = db.get_chunk_count(book_id, chapter + 1)
    if next_total == 0:
        return None
    return {"chapter": chapter + 1, "chunk": 0}


# ── Helpers ────────────────────────────────────────────────────────────


def _find_book_by_path(db, book_path: str) -> str | None:
    """Match a file path to an imported book via MD5."""
    if not book_path:
        return None
    try:
        from services.library_service import _hash_file
        book_id = _hash_file(Path(book_path))
        book = db.get_book(book_id)
        return book.id if book else None
    except Exception:
        return None


def _find_book_by_text(db, page_text: str) -> str | None:
    """Fuzzy-find a book by searching page text across all books."""
    try:
        results = db.search_books(page_text[:80], limit=1)
        return results[0]["book_id"] if results else None
    except Exception:
        return None


def _adjacent_chapter(chapters, current_idx: int, delta: int):
    """Get the previous or next chapter by index."""
    for ch in chapters:
        if ch.chapter_index == current_idx + delta:
            return ch
    return None
