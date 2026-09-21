"""Shared SQLite infrastructure — connection management + migration engine.

Used by both :class:`BookDB` and :class:`SessionDB` as a base class.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str = "") -> str:
    suffix = uuid.uuid4().hex[:16]
    return f"{prefix}-{suffix}" if prefix else suffix


def _adapt_fields(row: Any) -> Any:
    """Set default timestamps if missing (for SessionRow)."""
    now = _now_ms()
    if not row.time_created:
        row.time_created = now
    if not row.time_updated:
        row.time_updated = now
    return row


class BaseDatabase:
    """Shared SQLite connection lifecycle + schema migration engine.

    Subclasses must define their own ``_MIGRATIONS`` list and call
    ``initialize()`` after construction.
    """

    _MIGRATIONS: list[tuple[int, str, str | list[str]]] = []

    def __init__(self, db_path: Path):
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

    # ── Connection lifecycle ──────────────────────────────────────────────

    def get_conn(self) -> sqlite3.Connection:
        """Public access to the SQLite connection.

        Creates the parent directory and opens the connection on first call.
        Enables WAL mode and foreign keys.
        """
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Migration engine ──────────────────────────────────────────────────

    def initialize(self) -> None:
        """Create tables and run pending migrations."""
        conn = self.get_conn()

        # Ensure schema version tracking table exists
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at INTEGER NOT NULL,"
            "  description TEXT"
            ");"
        )

        # Check current version
        cur = conn.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        current_version = row[0] if row[0] is not None else 0

        # Run pending migrations
        for ver, desc, sql in self._MIGRATIONS:
            if ver > current_version:
                stmts = [sql] if isinstance(sql, str) else sql
                for stmt in stmts:
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError as exc:
                        # A column may already exist in CREATE TABLE DDL
                        # while a historical migration ADDs it again; tolerate
                        # on fresh databases.
                        if "duplicate column name" not in str(exc):
                            raise
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (ver, _now_ms(), desc),
                )
        conn.commit()

    # ── SQL helpers (used by subclasses) ──────────────────────────────────

    @staticmethod
    def _extract_searchable_text(data_json: str, part_type: str) -> str:
        """Extract plain text from a part's JSON data for FTS5 indexing."""
        import json as _json
        try:
            obj = _json.loads(data_json)
        except (_json.JSONDecodeError, TypeError):
            return ""

        if part_type == "text":
            return obj.get("text", "")
        elif part_type == "thinking":
            return obj.get("thinking", "")
        elif part_type == "tool_use":
            name = obj.get("tool_name", "")
            inputs = obj.get("tool_input", {})
            if isinstance(inputs, dict):
                input_strs = [
                    str(v) for v in inputs.values()
                    if isinstance(v, str)
                ]
                return f"{name} {' '.join(input_strs)}" if input_strs else name
            return name
        elif part_type == "tool_result":
            return obj.get("tool_output", "")
        return ""
