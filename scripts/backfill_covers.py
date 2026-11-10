"""Backfill cover images for existing EPUB books.

Run once after the cover-extraction feature is deployed.
Usage::

    python scripts/backfill_covers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Force UTF-8 on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import BookDB
from services.library_service import (
    cover_thumb_path,
    extract_and_save_cover,
    make_cover_thumb,
)


def main() -> None:
    project_dir = Path(__file__).resolve().parent.parent
    db_path = project_dir / "data" / "books.db"

    if not db_path.exists():
        print(f"Books DB not found: {db_path}")
        return

    db = BookDB(db_path)
    books = db.list_books(limit=1000)  # Get all books

    epub_books = [b for b in books if b.format.lower() == "epub"]
    print(f"Found {len(books)} total books, {len(epub_books)} EPUB files to process.\n")

    success = 0
    skipped = 0
    failed = 0

    for b in epub_books:
        bookbank = db._path.parent / "bookbank"
        # Check if cover already exists
        cover_path = next(
            (bookbank / f"{b.id}_cover.{ext}"
             for ext in ("jpg", "jpeg", "png", "gif", "webp", "svg")
             if (bookbank / f"{b.id}_cover.{ext}").exists()),
            None,
        )
        if cover_path is not None:
            # 老封面没有缩略图：顺手补一份（幂等，已存在就跳过）
            thumb = cover_thumb_path(cover_path)
            if cover_path.suffix.lower() != ".svg" and not thumb.exists():
                made = make_cover_thumb(cover_path)
                note = f"thumb -> {made.name}" if made else "thumb skipped (already small / no Pillow)"
            else:
                note = "cover + thumb already exist"
            print(f"  [skip] {b.title[:40]:40s}  ({note})")
            skipped += 1
            continue

        try:
            result = extract_and_save_cover(db, b.id)
            if result:
                print(f"  [OK]   {b.title[:40]:40s}  -> {result}")
                success += 1
            else:
                print(f"  [warn] {b.title[:40]:40s}  (no cover found in EPUB)")
                skipped += 1
        except Exception as exc:
            print(f"  [FAIL] {b.title[:40]:40s}  {exc}")
            failed += 1

    print(f"\nDone. {success} extracted, {skipped} skipped, {failed} failed.")


if __name__ == "__main__":
    main()
