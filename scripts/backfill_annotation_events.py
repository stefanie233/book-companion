"""Seed the annotation event journal from current annotation rows.

The journal starts recording from the moment the new code runs, so notes and
highlights created before that have no ``create`` event.  This script replays
what is knowable from ``annotations``:

* every current row gets one ``create`` event at ``time_created``;
* if ``time_updated > time_created`` it also gets one ``update`` event at
  ``time_updated`` (the snapshot is the row's *current* text — historical
  values were overwritten and cannot be reconstructed).

Deleted annotations are not recoverable: the row is physically gone, so no
backfill can invent them.  The journal covers deletions from now on.

Usage::

    python scripts/backfill_annotation_events.py           # dry run
    python scripts/backfill_annotation_events.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.annotation_events import record_annotation_event   # noqa: E402
from storage.db import BookDB                                    # noqa: E402


def _has_event(db: BookDB, annotation_id: str, event_type: str) -> bool:
    row = db.get_conn().execute(
        "SELECT 1 FROM annotation_events "
        "WHERE annotation_id = ? AND event_type = ? LIMIT 1;",
        (annotation_id, event_type),
    ).fetchone()
    return row is not None


def backfill(db_path: str = "data/books.db", *, apply: bool = True) -> None:
    db = BookDB(Path(db_path))
    db.initialize()

    rows = db.get_conn().execute("SELECT * FROM annotations;").fetchall()
    if not rows:
        print("No annotations in this library. Nothing to do.")
        return

    created = updated = skipped = failed = 0
    for raw in rows:
        ann = BookDB._row_to_annotation(raw)
        book = db.get_book(ann.book_id) if ann.book_id else None
        title = (book.title if book else "") or ann.book_id or "?"

        if not _has_event(db, ann.id, "create"):
            if apply:
                out = record_annotation_event(
                    db, ann, "create",
                    time_created=ann.time_created or None,
                )
                if out is None:
                    failed += 1
                    print(f"  FAIL create {ann.id} — journal insert failed")
                    continue
            created += 1
            print(f"  {'OK' if apply else 'DRY'} create {ann.id} 《{title}》")
        else:
            skipped += 1

        if ann.time_updated and ann.time_updated > ann.time_created:
            if not _has_event(db, ann.id, "update"):
                if apply:
                    out = record_annotation_event(
                        db, ann, "update", changed_fields=(),
                        time_created=ann.time_updated,
                    )
                    if out is None:
                        failed += 1
                        print(f"  FAIL update {ann.id} — journal insert failed")
                        continue
                updated += 1
                print(f"  {'OK' if apply else 'DRY'} update {ann.id} 《{title}》")

    print(f"\ncreate={created} update={updated} "
          f"already-has-create={skipped} failed={failed}")
    print("Done." if apply else "Dry run — pass --apply to write.")
    db.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/books.db")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry run)")
    args = ap.parse_args()
    backfill(args.db, apply=args.apply)


if __name__ == "__main__":
    main()
