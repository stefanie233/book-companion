"""One-off script: backfill import_log for books that lack one.

Without an import_log entry, the annotation system cannot resolve
stable_book_id → book_id, so every create-annotation request fails
with 404.  This script creates import_log rows for all books that
don't have one.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.db import BookDB, _now_ms
from storage.db_base import _new_id
from storage.models import ImportLogRow
from pathlib import Path


def backfill(db_path: str = "data/books.db"):
    db = BookDB(Path(db_path))

    # Find books missing an import_log entry
    existing = set(
        row["book_id"] for row in
        db.get_conn().execute("SELECT book_id FROM import_log WHERE in_library = 1").fetchall()
    )

    all_books = db.get_conn().execute("SELECT * FROM books").fetchall()
    missing = [b for b in all_books if b["id"] not in existing]

    if not missing:
        print("All books already have import_log entries. Nothing to do.")
        return

    print(f"Found {len(missing)} book(s) without import_log:")

    for book in missing:
        book_id = book["id"]
        title = book["title"] or ""
        author = book["author"] or ""
        source_path = book["source_path"] or ""
        source_filename = Path(source_path).name if source_path else f"{book_id}.epub"

        # Extract first chapter title and first paragraph
        ch_row = db.get_conn().execute(
            "SELECT title FROM book_chapters WHERE book_id=? ORDER BY chapter_index LIMIT 1",
            (book_id,),
        ).fetchone()
        first_chapter = ch_row["title"] if ch_row else ""

        ck_row = db.get_conn().execute(
            "SELECT content FROM book_chunks WHERE book_id=? ORDER BY chapter_index, chunk_index LIMIT 1",
            (book_id,),
        ).fetchone()
        first_paragraph = ck_row["content"][:200] if ck_row else ""

        # Minimal copyright card (full version needs raw chapters, not worth
        # reconstructing; this backfill is for annotation resolution, not
        # for LLM matching).
        import json
        copyright_card = json.dumps({
            "title": title,
            "author": author,
            "first_chapter": first_chapter,
            "first_paragraph": first_paragraph[:100],
            "source_filename": source_filename,
        }, ensure_ascii=False)

        stable_book_id = "sb-" + _new_id()
        now = _now_ms()

        db.insert_import_log(ImportLogRow(
            id=_new_id(),
            stable_book_id=stable_book_id,
            book_id=book_id,
            event_type="import",
            in_library=1,
            copyright_card=copyright_card,
            title=title,
            author=author,
            publisher=None,
            publish_year=None,
            isbn=None,
            first_chapter=first_chapter,
            first_paragraph=first_paragraph,
            source_filename=source_filename,
            import_count=1,
            time_created=now,
        ))

        print(f"  OK {title[:30]} -> {stable_book_id}")

    print("Done.")


if __name__ == "__main__":
    backfill()
