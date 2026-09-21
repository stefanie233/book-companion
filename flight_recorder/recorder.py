"""Context flight recorder -- captures the complete request/response cycle
between the agent and the LLM on every ReAct round.

Records three layers per round:
  1. **Input**  -- system_prompt + messages (what the agent sent to the LLM)
  2. **Output** -- response_text (incl.  thinking blocks) + tool_calls
  3. **Tool results** -- executed tool name, arguments, and return value

Lifecycle: persistent.  All rounds are kept until manually deleted via
``clear()`` or the ``DELETE /api/context-log`` endpoint.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


_DDL = """
CREATE TABLE IF NOT EXISTS context_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    turn_index        INTEGER NOT NULL,
    react_round       INTEGER NOT NULL,
    mode              TEXT,
    book_id           TEXT,
    created_at        TEXT DEFAULT (datetime('now')),
    -- Input (sent to LLM)
    system_prompt     TEXT,
    messages_json     TEXT,
    tools_count       INTEGER DEFAULT 0,
    -- Output (LLM returned)
    response_text     TEXT,
    tool_calls_json   TEXT,
    -- Tool execution results
    tool_results_json TEXT,
    char_count        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ctx_session ON context_snapshots(session_id, turn_index);
"""


class ContextRecorder:
    """Flight recorder: full request/response per ReAct round, persistent."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    # ── internal ──────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_DDL)
        conn.commit()

    # ── public API ────────────────────────────────────────────────────

    def record_round(
        self,
        *,
        session_id: str,
        turn_index: int,
        react_round: int,
        system_prompt: str,
        messages: list[dict],
        tools_count: int = 0,
        mode: str = "",
        book_id: str = "",
        response_text: str = "",
        tool_calls: list[dict] | None = None,
        tool_results: list[dict] | None = None,
    ) -> None:
        """Persist one complete ReAct round (input + output + tool results).

        ``tool_calls`` is the raw list from the LLM (``[{"id", "name", "arguments"}]``).
        ``tool_results`` is ``[{"name", "arguments", "result"}]`` after execution.
        Either may be empty/None when the round has no tool usage.
        """
        try:
            messages_json = json.dumps(messages, ensure_ascii=False)
            tc_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
            tr_json = json.dumps(tool_results, ensure_ascii=False) if tool_results else None
            char_count = len(system_prompt) + sum(
                len(m.get("content") or "") for m in messages
            )
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO context_snapshots "
                "(session_id, turn_index, react_round, mode, book_id, "
                " system_prompt, messages_json, tools_count, "
                " response_text, tool_calls_json, tool_results_json, char_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, turn_index, react_round, mode, book_id,
                    system_prompt, messages_json, tools_count,
                    response_text or "", tc_json, tr_json, char_count,
                ),
            )
            conn.commit()
        except Exception:
            pass  # recorder must never break the conversation

    def list_sessions(self) -> list[dict]:
        """List sessions that have recorded rounds, with aggregate stats."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT session_id, "
            "  COUNT(*) as round_count, "
            "  MAX(turn_index) as last_turn, "
            "  MIN(created_at) as first_at, "
            "  MAX(created_at) as last_at, "
            "  SUM(char_count) as total_chars, "
            "  mode, book_id "
            "FROM context_snapshots GROUP BY session_id "
            "ORDER BY last_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_rounds(self, session_id: str) -> list[dict]:
        """List rounds for a session (metadata only, no large fields)."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, turn_index, react_round, mode, book_id, "
            "  created_at, tools_count, char_count, "
            "  length(response_text) as response_len, "
            "  length(tool_calls_json) as tool_calls_len, "
            "  length(tool_results_json) as tool_results_len "
            "FROM context_snapshots WHERE session_id = ? "
            "ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_round(self, round_id: int) -> dict | None:
        """Get full content of a single round."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM context_snapshots WHERE id = ?",
            (round_id,),
        ).fetchone()
        return dict(row) if row else None

    def clear(self, session_id: str | None = None) -> int:
        """Delete records.  If ``session_id`` is None, clears all.
        Returns number of deleted rows."""
        conn = self._get_conn()
        if session_id:
            cur = conn.execute(
                "DELETE FROM context_snapshots WHERE session_id = ?",
                (session_id,),
            )
        else:
            cur = conn.execute("DELETE FROM context_snapshots")
        conn.commit()
        return cur.rowcount

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
