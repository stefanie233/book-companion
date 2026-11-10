"""One-off scripts for the book lifecycle log (``import_log``).

Two jobs:

1. ``backfill`` — create an identity row for books that have no active
   (``in_library=1``) import_log entry.  Without one, the annotation system
   cannot resolve ``stable_book_id`` → ``book_id`` and every
   create-annotation request fails with 404.
2. ``--refresh-current`` — append a missing ``import`` event for books
   whose ``books.time_created`` is newer than their latest active import
   event.  This repairs the historical hole left by import paths that did
   not write the log (the structured split path / batch re-imports): the
   book was re-imported, but the log still points at the original import.

Usage::

    python scripts/backfill_import_log.py                    # dry run (both checks)
    python scripts/backfill_import_log.py --apply            # create missing identity rows
    python scripts/backfill_import_log.py --refresh-current --apply
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.db import BookDB, _now_ms
from storage.db_base import _new_id
from storage.models import ImportLogRow


def _first_source(db: BookDB, book_id: str) -> tuple[str, str]:
    """First chapter title + first chunk's first 200 chars."""
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
    return first_chapter, first_paragraph


def _books_missing_identity(db: BookDB) -> list:
    active = set(
        row["book_id"] for row in
        db.get_conn().execute(
            "SELECT book_id FROM import_log WHERE in_library = 1"
        ).fetchall()
    )
    all_books = db.get_conn().execute("SELECT * FROM books").fetchall()
    return [b for b in all_books if b["id"] not in active]


def backfill(db_path: str = "data/books.db", *, apply: bool = True) -> None:
    db = BookDB(Path(db_path))
    db.initialize()
    missing = _books_missing_identity(db)
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
        first_chapter, first_paragraph = _first_source(db, book_id)

        # Minimal copyright card (full version needs raw chapters, not worth
        # reconstructing; this backfill is for annotation resolution, not
        # for LLM matching).
        copyright_card = json.dumps({
            "title": title,
            "author": author,
            "first_chapter": first_chapter,
            "first_paragraph": first_paragraph[:100],
            "source_filename": source_filename,
        }, ensure_ascii=False)

        stable_book_id = "sb-" + _new_id()
        row = ImportLogRow(
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
            time_created=_now_ms(),
        )
        print(f"  {'OK' if apply else 'DRY'} {title[:30]} -> {stable_book_id}")
        if apply:
            db.insert_import_log(row)

    print("Done." if apply else "Dry run — pass --apply to write.")
    db.close()


def refresh_current(db_path: str = "data/books.db", *, apply: bool = True) -> None:
    """Append the missing re-import event for currently-in-library books.

    A book whose ``books.time_created`` is newer than its latest active
    import event was re-imported by a path that did not write the log.
    Append an ``import`` event dated at the book row's timestamp, reusing
    the active stable identity and incrementing ``import_count``.
    """
    db = BookDB(Path(db_path))
    db.initialize()
    rows = db.get_conn().execute(
        """
        SELECT b.id, b.title, b.author, b.time_created,
               (SELECT MAX(time_created) FROM import_log il
                 WHERE il.book_id = b.id AND il.in_library = 1) AS last_import
        FROM books b
        """
    ).fetchall()
    stale = [r for r in rows if not r["last_import"] or r["last_import"] < r["time_created"]]

    if not stale:
        print("All current books have an import event at or after their row timestamp.")
        return

    print(f"Found {len(stale)} book(s) re-imported without a log entry:")
    for r in stale:
        book_id, title = r["id"], r["title"] or ""
        log = db.get_import_log_by_book(book_id)
        if not log:
            print(f"  SKIP {title[:30]} — no active identity (run the plain backfill first)")
            continue
        first_chapter, first_paragraph = _first_source(db, book_id)
        row = ImportLogRow(
            id=_new_id(),
            stable_book_id=log.stable_book_id,
            book_id=book_id,
            event_type="import",
            in_library=1,
            copyright_card=log.copyright_card,
            title=title,
            author=r["author"] or log.author,
            publisher=log.publisher,
            publish_year=log.publish_year,
            isbn=log.isbn,
            first_chapter=first_chapter or log.first_chapter,
            first_paragraph=first_paragraph or log.first_paragraph,
            source_filename=log.source_filename,
            import_count=log.import_count + 1,
            time_created=int(r["time_created"]),
        )
        print(f"  {'OK' if apply else 'DRY'} {title[:30]} — import event at "
              f"{r['time_created']} (count {row.import_count})")
        if apply:
            db.insert_import_log(row)

    print("Done." if apply else "Dry run — pass --apply to write.")
    db.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/books.db")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry run)")
    ap.add_argument("--refresh-current", action="store_true",
                    help="only repair missing re-import events for current books")
    args = ap.parse_args()
    if args.refresh_current:
        refresh_current(args.db, apply=args.apply)
    else:
        backfill(args.db, apply=args.apply)


if __name__ == "__main__":
    main()
