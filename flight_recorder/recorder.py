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
import re
import sqlite3
from pathlib import Path


#: Session ids end up in a URL path and inside ``onclick="selectSession('…')"``.
#: Generated labels (book/author names) must not smuggle in quotes, slashes or
#: whitespace — a single ``'`` in a title would silently break the viewer.
_TAG_BAD = re.compile(r"[\s/\\:*?\"<>|'`]+")


def session_tag(text: str, limit: int = 40) -> str:
    """Make a human-readable, viewer-safe session label (CJK kept as-is)."""
    tag = _TAG_BAD.sub("_", str(text or "").strip()).strip("_")
    return tag[:limit] or "unknown"


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
    char_count        INTEGER DEFAULT 0,
    -- Call metadata (added v2): which model answered, and how long it took
    model             TEXT,
    latency_ms        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ctx_session ON context_snapshots(session_id, turn_index);
"""

#: Columns added after the first version.  ``migrate=False`` recorders (the
#: Hermes DB, which we only read) must never ALTER someone else's file.
_META_COLS = (("model", "TEXT"), ("latency_ms", "INTEGER"))


class ContextRecorder:
    """Flight recorder: full request/response per ReAct round, persistent."""

    def __init__(self, db_path: Path, *, migrate: bool = True) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None
        self._migrate = migrate
        self._meta_cols = False
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
        cols = {r[1] for r in conn.execute("PRAGMA table_info(context_snapshots)")}
        missing = [c for c in _META_COLS if c[0] not in cols]
        if missing and self._migrate:
            for name, decl in missing:
                conn.execute(
                    f"ALTER TABLE context_snapshots ADD COLUMN {name} {decl}")
            cols = {r[1] for r in
                    conn.execute("PRAGMA table_info(context_snapshots)")}
        # Read-only recorders may point at an older schema — the SELECTs and
        # the INSERT adapt to whatever columns actually exist.
        self._meta_cols = all(name in cols for name, _ in _META_COLS)
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
        model: str = "",
        latency_ms: int | None = None,
    ) -> None:
        """Persist one complete ReAct round (input + output + tool results).

        ``tool_calls`` is the raw list from the LLM (``[{"id", "name", "arguments"}]``).
        ``tool_results`` is ``[{"name", "arguments", "result"}]`` after execution.
        Either may be empty/None when the round has no tool usage.

        ``model`` / ``latency_ms`` answer "哪个模型、跑了多久" — without them a
        prompt iteration has no baseline to compare against.
        """
        try:
            messages_json = json.dumps(messages, ensure_ascii=False)
            tc_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
            tr_json = json.dumps(tool_results, ensure_ascii=False) if tool_results else None
            char_count = len(system_prompt) + sum(
                len(m.get("content") or "") for m in messages
            )
            cols = ["session_id", "turn_index", "react_round", "mode", "book_id",
                    "system_prompt", "messages_json", "tools_count",
                    "response_text", "tool_calls_json", "tool_results_json",
                    "char_count"]
            vals: list = [
                session_id, turn_index, react_round, mode, book_id,
                system_prompt, messages_json, tools_count,
                response_text or "", tc_json, tr_json, char_count,
            ]
            if self._meta_cols:
                cols += ["model", "latency_ms"]
                vals += [model or "", latency_ms]
            conn = self._get_conn()
            conn.execute(
                f"INSERT INTO context_snapshots ({', '.join(cols)}) "
                f"VALUES ({', '.join(['?'] * len(cols))})",
                vals,
            )
            conn.commit()
        except Exception:
            pass  # recorder must never break the conversation

    def record_llm_event(
        self,
        event: dict,
        *,
        session_id: str,
        mode: str,
        book_id: str = "",
        turn_index: int = 0,
        react_round: int = 0,
    ) -> None:
        """Record one **non-ReAct** LLM call from a ``log_cb`` style event.

        Call sites that don't go through Runtime (authorgraph, writing, ...)
        would otherwise be invisible here.  The event dict carries
        ``{status, system, prompt, response, reasoning_content, finish_reason,
        error_detail}`` — the same shape the graph prompt modules pass to
        ``log_cb``.

        Reasoning is wrapped in ``<think>…</think>`` inside ``response_text``:
        that is the runtime's own convention (raw stream), and the viewer
        already renders such blocks as a collapsible thought section — so no
        schema change is needed and old rows keep working.

        Failure status is prefixed as a marker line so ``parse_failed`` /
        ``length`` truncation can be told apart from "the model had nothing
        to say" when reading the log later.
        """
        try:
            response = str(event.get("response") or "")
            reasoning = str(event.get("reasoning_content") or "")
            if reasoning:
                response = f"<think>\n{reasoning}\n</think>\n{response}"
            status = str(event.get("status") or "")
            if status and status != "ok":
                finish = str(event.get("finish_reason") or "")
                mark = f"[[status={status}" + (
                    f" finish_reason={finish}" if finish else "") + "]]"
                detail = str(event.get("error_detail") or "")
                if detail:
                    mark += f" {detail}"
                response = f"{mark}\n{response}" if response else mark
            self.record_round(
                session_id=session_id,
                turn_index=turn_index,
                react_round=react_round,
                system_prompt=str(event.get("system") or ""),
                messages=[{"role": "user",
                           "content": str(event.get("prompt") or "")}],
                tools_count=0,
                mode=mode,
                book_id=book_id,
                response_text=response,
                model=str(event.get("model") or ""),
                latency_ms=event.get("latency_ms"),
            )
        except Exception:
            pass  # recorder must never break the caller

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
        meta = ", model, latency_ms" if self._meta_cols else ""
        rows = conn.execute(
            "SELECT id, turn_index, react_round, mode, book_id, "
            "  created_at, tools_count, char_count, "
            "  length(response_text) as response_len, "
            "  length(tool_calls_json) as tool_calls_len, "
            f"  length(tool_results_json) as tool_results_len{meta} "
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
