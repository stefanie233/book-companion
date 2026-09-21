r"""One-shot migration: split book-companion.db into books.db + sessions.db.

After this script:
  - ``data/books.db``    — book-domain tables (empty, all books were deleted)
  - ``data/sessions.db`` — session-domain tables (14 sessions, 141 messages, 242 parts)

The old ``data/book-companion.db`` is kept as backup.  Delete it manually
after verifying the new databases work correctly.

Usage::

    python scripts/migrate_split.py   # run from anywhere, paths are derived
"""

import sqlite3
import shutil
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OLD_DB = DATA_DIR / "book-companion.db"
BOOKS_DB = DATA_DIR / "books.db"
SESSIONS_DB = DATA_DIR / "sessions.db"

# ── Session-domain tables ──────────────────────────────────────────────────

SESSION_TABLES = [
    "sessions",
    "messages",
    "parts",
    "session_fts",
]

SESSION_INDEXES = [
    "idx_messages_session",
    "idx_parts_message",
    "idx_parts_session",
    "idx_sessions_parent",
    "idx_sessions_updated",
    "idx_sessions_project",
    "idx_sessions_archivable",
    "idx_sessions_unreviewed",
]

SESSION_TRIGGERS = [
    "parts_ai",
    "parts_ad",
    "parts_au",
]

# ── Book-domain tables (staying in books.db) ───────────────────────────────

BOOK_TABLES = [
    "books",
    "book_categories",
    "book_chapters",
    "book_chunks",
    "reading_sessions",
    "book_versions",
    "book_fts",
]

BOOK_INDEXES = [
    "idx_books_category",
    "idx_books_status",
    "idx_chapters_book",
    "idx_chunks_book_chapter",
    "idx_reading_sessions_book",
    "idx_categories_parent",
    "idx_versions_book_chapter",
]

BOOK_TRIGGERS = [
    "book_chunks_ai",
    "book_chunks_ad",
    "book_chunks_au",
]


def main():
    if not OLD_DB.exists():
        print(f"ERROR: {OLD_DB} not found — nothing to migrate")
        sys.exit(1)

    # ── Step 1: Copy old DB → books.db ─────────────────────────────────
    print(f"1. Copying {OLD_DB.name} → {BOOKS_DB.name} ...")
    if BOOKS_DB.exists():
        print(f"   WARNING: {BOOKS_DB.name} already exists, overwriting")
        BOOKS_DB.unlink()
    shutil.copy2(OLD_DB, BOOKS_DB)
    print(f"   Done ({BOOKS_DB.stat().st_size / 1024:.0f} KB)")

    # ── Step 2: Create sessions.db ──────────────────────────────────────
    print(f"2. Creating {SESSIONS_DB.name} ...")
    if SESSIONS_DB.exists():
        print(f"   WARNING: {SESSIONS_DB.name} already exists, overwriting")
        SESSIONS_DB.unlink()

    # Attach both databases
    src = sqlite3.connect(str(OLD_DB))
    src.execute("PRAGMA journal_mode=WAL")
    dst = sqlite3.connect(str(SESSIONS_DB))
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA foreign_keys=ON")

    # Copy schema_version table structure
    dst.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at INTEGER NOT NULL,"
        "  description TEXT"
        ");"
    )

    # Copy session tables' SQL from old DB (schema only)
    for table in SESSION_TABLES:
        cur = src.execute(
            f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?;",
            (table,),
        )
        row = cur.fetchone()
        if row and row[0]:
            dst.execute(row[0])
            print(f"   Created table: {table}")

    # Copy session indexes
    for idx in SESSION_INDEXES:
        cur = src.execute(
            f"SELECT sql FROM sqlite_master WHERE type='index' AND name=?;",
            (idx,),
        )
        row = cur.fetchone()
        if row and row[0]:
            try:
                dst.execute(row[0])
            except sqlite3.OperationalError:
                pass  # Index may reference dropped columns

    # Copy session triggers
    for trigger in SESSION_TRIGGERS:
        cur = src.execute(
            f"SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?;",
            (trigger,),
        )
        row = cur.fetchone()
        if row and row[0]:
            dst.execute(row[0])
            print(f"   Created trigger: {trigger}")

    # Copy data
    dst.execute("PRAGMA foreign_keys=OFF;")
    for table in SESSION_TABLES:
        cur = src.execute(f"SELECT COUNT(*) FROM {table};")
        count = cur.fetchone()[0]
        if count == 0:
            print(f"   Table {table}: 0 rows (skipped)")
            continue

        # Get column names
        col_cur = src.execute(f"SELECT * FROM {table} LIMIT 0;")
        cols = [d[0] for d in col_cur.description]
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)

        # Copy row by row
        cur = src.execute(f"SELECT * FROM {table};")
        rows = cur.fetchall()
        for row in rows:
            dst.execute(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders});",
                row,
            )
        dst.commit()
        print(f"   Table {table}: {count} rows copied")

    # Copy schema_version entries for session migrations (v1, v3, v5, v6, v7, v8)
    session_versions = {1, 3, 5, 6, 7, 8}
    cur = src.execute(
        "SELECT version, applied_at, description FROM schema_version "
        "WHERE version IN ({})".format(",".join("?" for _ in session_versions)),
        tuple(session_versions),
    )
    for ver, ts, desc in cur.fetchall():
        dst.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at, description) "
            "VALUES (?, ?, ?);",
            (ver, ts, desc),
        )
    dst.execute("PRAGMA foreign_keys=ON;")
    dst.commit()
    print(f"   schema_version entries copied")

    src.close()
    dst.close()
    print(f"   Done ({SESSIONS_DB.stat().st_size / 1024:.0f} KB)")

    # ── Step 3: Clean up books.db — drop session tables ────────────────
    print(f"3. Cleaning {BOOKS_DB.name} ...")
    bdb = sqlite3.connect(str(BOOKS_DB))
    bdb.execute("PRAGMA foreign_keys=OFF;")

    for trigger in SESSION_TRIGGERS:
        bdb.execute(f"DROP TRIGGER IF EXISTS {trigger};")
    for table in SESSION_TABLES:
        bdb.execute(f"DROP TABLE IF EXISTS {table};")
    for idx in SESSION_INDEXES:
        bdb.execute(f"DROP INDEX IF EXISTS {idx};")

    # Update schema_version — remove session migration records
    bdb.execute(
        "DELETE FROM schema_version WHERE version IN ({})".format(
            ",".join("?" for _ in session_versions)
        ),
        tuple(session_versions),
    )

    bdb.execute("PRAGMA foreign_keys=ON;")
    bdb.commit()

    # Verify table count
    cur = bdb.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
    )
    remaining = [r[0] for r in cur.fetchall()]
    print(f"   Remaining tables: {remaining}")
    bdb.close()

    # ── Step 4: Summary ─────────────────────────────────────────────────
    print()
    print("=" * 50)
    print("Migration complete!")
    print(f"  {BOOKS_DB.name}: {BOOKS_DB.stat().st_size / 1024:.0f} KB")
    print(f"  {SESSIONS_DB.name}: {SESSIONS_DB.stat().st_size / 1024:.0f} KB")
    print(f"  {OLD_DB.name}: kept as backup — delete after verification")
    print()
    print("Next steps:")
    print("  1. Run: python main.py")
    print("  2. Verify sessions load correctly (/sessions)")
    print("  3. Import a book and verify it appears")
    print("  4. Delete old DB: rm data/book-companion.db")


if __name__ == "__main__":
    main()
