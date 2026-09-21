"""Session-domain database — sessions, messages, parts, FTS5 search.

Owns ``session.db``.  All methods operate exclusively on session-domain tables.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from storage.db_base import BaseDatabase, _now_ms, _adapt_fields
from storage.models import (
    ALTER_PARTS_SEARCHABLE_TEXT,
    ALTER_SESSIONS_CHARACTER_NAME,
    ALTER_SESSIONS_FORK_POINT_INDEX,
    ALTER_SESSIONS_KEYWORDS,
    ALTER_SESSIONS_REVIEW_COUNT,
    ALTER_SESSIONS_TIME_REVIEWED,
    ALL_DDL,
    CREATE_ARCHIVABLE_INDEX,
    CREATE_SESSION_FTS,
    CREATE_SESSION_FTS_TRIGGERS,
    CREATE_UNREVIEWED_INDEX,
    REBUILD_SESSION_FTS,
    TABLE_MESSAGES,
    TABLE_PARTS,
    TABLE_SESSION_FTS,
    TABLE_SESSIONS,
    _BACKFILL_PARTS_SEARCHABLE,
    CREATE_LLM_USAGE_INDEXES,
    CREATE_LLM_USAGE_LOG,
    TABLE_LLM_USAGE_LOG,
    MessageRow,
    PartRow,
    SessionRow,
)

# 用量统计时排除的测试模型前缀——靶场/冒烟测试绑 fake-provider 产生的
# fake-* 调用不入真实热力图与汇总（原始行保留，仅统计侧过滤）。
TEST_MODEL_PREFIXES = ("fake-",)


def _usage_test_model_filter() -> str:
    """SQL 片段：排除 TEST_MODEL_PREFIXES 开头的模型行。"""
    if not TEST_MODEL_PREFIXES:
        return "1=1"
    return "(" + " AND ".join(f"model NOT LIKE '{p}%'" for p in TEST_MODEL_PREFIXES) + ")"


# ── Migrations (session-only) ─────────────────────────────────────────────

_SESSION_MIGRATIONS: list[tuple[int, str, str | list[str]]] = [
    (1, "Initial schema: sessions, messages, parts", list(ALL_DDL)),
    (2, "Session FTS5: trigram full-text search over parts.searchable_text", [
        ALTER_PARTS_SEARCHABLE_TEXT,
        _BACKFILL_PARTS_SEARCHABLE,
        CREATE_SESSION_FTS,
        *CREATE_SESSION_FTS_TRIGGERS,
        REBUILD_SESSION_FTS,
    ]),
    (3, "Archivable sessions index for data cleaning pipeline", [
        CREATE_ARCHIVABLE_INDEX,
    ]),
    (4, "Review stamp + keywords for session review pipeline", [
        ALTER_SESSIONS_TIME_REVIEWED,
        ALTER_SESSIONS_KEYWORDS,
        CREATE_UNREVIEWED_INDEX,
    ]),
    (5, "Review count for re-review tracking", [
        ALTER_SESSIONS_REVIEW_COUNT,
    ]),
    (9, "Character name for role-play session grouping", [
        ALTER_SESSIONS_CHARACTER_NAME,
    ]),
    (10, "Fork point index for branch switcher", [
        ALTER_SESSIONS_FORK_POINT_INDEX,
    ]),
    (11, "LLM usage log for console analytics", [
        CREATE_LLM_USAGE_LOG,
        *CREATE_LLM_USAGE_INDEXES,
    ]),
]


class SessionDB(BaseDatabase):
    """SQLite database for conversation sessions, messages, and parts.

    Owns ``session.db``.  Provides CRUD for sessions, full-text search
    over message content, and the review/tagging pipeline.

    Usage::

        db = SessionDB(Path("data/sessions.db"))
        db.initialize()
        sid = db.insert_session(SessionRow(id="s-1"))
    """

    _MIGRATIONS = _SESSION_MIGRATIONS

    # ── Session CRUD ───────────────────────────────────────────────────────

    def insert_session(self, row: SessionRow) -> str:
        row = _adapt_fields(row)
        conn = self.get_conn()
        conn.execute(
            f"""INSERT INTO {TABLE_SESSIONS}
                (id, project_id, parent_id, title, agent, model, mode,
                 character_name,
                 compaction_count, message_count, time_created, time_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
            (row.id, row.project_id, row.parent_id, row.title,
             row.agent, row.model, row.mode,
             row.character_name or "",
             row.compaction_count, row.message_count,
             row.time_created, row.time_updated),
        )
        conn.commit()
        return row.id

    def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields.setdefault("time_updated", _now_ms())
        conn = self.get_conn()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [session_id]
        conn.execute(
            f"UPDATE {TABLE_SESSIONS} SET {set_clause} WHERE id=?;",
            values,
        )
        conn.commit()

    def delete_session(self, session_id: str) -> None:
        conn = self.get_conn()
        conn.execute("PRAGMA foreign_keys=OFF;")
        conn.execute(
            f"UPDATE {TABLE_SESSIONS} SET parent_id=NULL WHERE parent_id=?;",
            (session_id,),
        )
        conn.execute(f"DELETE FROM {TABLE_PARTS} WHERE session_id=?;", (session_id,))
        conn.execute(f"DELETE FROM {TABLE_MESSAGES} WHERE session_id=?;", (session_id,))
        conn.execute(f"DELETE FROM {TABLE_SESSIONS} WHERE id=?;", (session_id,))
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.commit()

    def get_session(self, session_id: str) -> SessionRow | None:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_SESSIONS} WHERE id=?;", (session_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return SessionRow(**dict(row))

    def list_sessions(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        root_only: bool = False,
        search: str | None = None,
        project_id: str | None = None,
        character_name: str | None = None,
        start: int | None = None,
        include_archived: bool = False,
    ) -> list[SessionRow]:
        conn = self.get_conn()
        conditions: list[str] = []
        params: list[Any] = []

        if not include_archived:
            conditions.append("time_archived IS NULL")
        if root_only:
            conditions.append("parent_id IS NULL")
        if search:
            conditions.append("title LIKE ?")
            params.append(f"%{search}%")
        if project_id is not None:
            conditions.append("project_id=?")
            params.append(project_id)
        if character_name is not None:
            conditions.append("character_name=?")
            params.append(character_name)
        if start is not None:
            conditions.append("time_updated >= ?")
            params.append(start)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT * FROM {TABLE_SESSIONS} {where} "
            f"ORDER BY time_updated DESC LIMIT ? OFFSET ?;"
        )
        params.extend([limit, offset])
        cur = conn.execute(sql, params)
        return [SessionRow(**dict(r)) for r in cur.fetchall()]

    def list_children(self, parent_id: str, *, include_archived: bool = False) -> list[SessionRow]:
        """List direct child sessions (forks) of a parent, oldest first."""
        conn = self.get_conn()
        conditions: list[str] = ["parent_id=?"]
        params: list[Any] = [parent_id]
        if not include_archived:
            conditions.append("time_archived IS NULL")
        where = " AND ".join(conditions)
        sql = (
            f"SELECT * FROM {TABLE_SESSIONS} WHERE {where} "
            f"ORDER BY time_created ASC;"
        )
        cur = conn.execute(sql, params)
        return [SessionRow(**dict(r)) for r in cur.fetchall()]

    def count_children_batch(self, parent_ids: list[str]) -> dict[str, int]:
        """Batch count direct children for multiple parents (avoids N+1)."""
        if not parent_ids:
            return {}
        conn = self.get_conn()
        placeholders = ",".join("?" * len(parent_ids))
        cur = conn.execute(
            f"SELECT parent_id, COUNT(*) AS cnt FROM {TABLE_SESSIONS} "
            f"WHERE parent_id IN ({placeholders}) AND time_archived IS NULL "
            f"GROUP BY parent_id;",
            parent_ids,
        )
        return {row["parent_id"]: row["cnt"] for row in cur.fetchall()}

    def search_session_content(
        self, query: str, limit: int = 10, include_archived: bool = False,
    ) -> list[dict]:
        conn = self.get_conn()
        results: list[dict] = []

        safe = query.translate(str.maketrans("", "", '*"()'))
        fts_query = f'"{safe}"' if " " in safe else safe

        archive_filter = "" if include_archived else "AND s.time_archived IS NULL"
        try:
            cur = conn.execute(
                f"SELECT m.session_id, s.title, s.mode, s.time_updated, "
                f"p.type, m.role, "
                f"snippet({TABLE_SESSION_FTS}, 0, '…', '…', '', 40) as snippet "
                f"FROM {TABLE_SESSION_FTS} "
                f"JOIN {TABLE_PARTS} p ON {TABLE_SESSION_FTS}.rowid = p.rowid "
                f"JOIN {TABLE_MESSAGES} m ON p.message_id = m.id "
                f"JOIN {TABLE_SESSIONS} s ON m.session_id = s.id "
                f"WHERE {TABLE_SESSION_FTS} MATCH ? {archive_filter} "
                f"ORDER BY rank LIMIT ?;",
                (fts_query, limit * 3),
            )
            results = [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            pass

        if not results:
            like_pattern = f"%{query}%"
            cur = conn.execute(
                f"SELECT DISTINCT m.session_id, s.title, s.mode, s.time_updated, "
                f"p.data, p.type, m.role "
                f"FROM {TABLE_PARTS} p "
                f"JOIN {TABLE_MESSAGES} m ON p.message_id = m.id "
                f"JOIN {TABLE_SESSIONS} s ON m.session_id = s.id "
                f"WHERE p.searchable_text LIKE ? {archive_filter} "
                f"ORDER BY s.time_updated DESC "
                f"LIMIT ?",
                (like_pattern, limit * 3),
            )
            for r in cur.fetchall():
                d = dict(r)
                raw = d.pop("data", "")
                try:
                    parsed = json.loads(raw)
                    text = (parsed.get("text", "")
                            or parsed.get("tool_output", "")
                            or "")
                except (json.JSONDecodeError, KeyError):
                    text = raw
                pos = text.find(query)
                start = max(0, pos - 40)
                end = min(len(text), pos + len(query) + 40)
                d["snippet"] = f"…{text[start:end]}…"
                results.append(d)

        seen: set[str] = set()
        merged: list[dict] = []
        for r in results:
            sid = r["session_id"]
            snippet = r.get("snippet", "")
            if sid in seen:
                if snippet and len(merged[-1].get("snippets", [])) < 3:
                    merged[-1].setdefault("snippets", []).append(snippet)
                continue
            seen.add(sid)
            if len(merged) >= limit:
                break
            merged.append({
                "session_id": sid,
                "title": r.get("title") or "无标题",
                "mode": r.get("mode", ""),
                "time_updated": r["time_updated"],
                "snippets": [snippet] if snippet else [],
            })

        return merged

    # ── Data cleaning / archiving ─────────────────────────────────────────

    def archive_session(self, session_id: str) -> None:
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_SESSIONS} SET time_archived = ? WHERE id = ?",
            (_now_ms(), session_id),
        )
        conn.commit()

    def prune_large_parts(self, session_id: str, max_chars: int = 5000) -> int:
        import hashlib
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT id, data FROM {TABLE_PARTS} "
            f"WHERE session_id = ? AND type = 'tool_result' "
            f"AND LENGTH(data) > ?",
            (session_id, max_chars),
        )
        rows = cur.fetchall()
        modified = 0
        for part_id, data in rows:
            if len(data) <= max_chars:
                continue
            content_hash = hashlib.md5(data.encode("utf-8")).hexdigest()[:12]
            placeholder = json.dumps({
                "type": "tool_result",
                "tool_output": f"[content truncated: {content_hash}]",
                "_pruned": True,
            })
            conn.execute(
                f"UPDATE {TABLE_PARTS} SET data = ?, searchable_text = ? "
                f"WHERE id = ?",
                (placeholder, f"[content truncated: {content_hash}]", part_id),
            )
            modified += 1
        if modified:
            conn.commit()
        return modified

    def list_archivable_sessions(
        self, older_than_ms: int, limit: int = 50,
    ) -> list[SessionRow]:
        conn = self.get_conn()
        threshold = _now_ms() - older_than_ms
        cur = conn.execute(
            f"SELECT * FROM {TABLE_SESSIONS} "
            f"WHERE time_updated < ? AND time_archived IS NULL "
            f"ORDER BY time_updated ASC LIMIT ?",
            (threshold, limit),
        )
        return [SessionRow(**dict(r)) for r in cur.fetchall()]

    def mark_reviewed(self, session_id: str, keywords: str = "", *,
                      append_keywords: bool = True) -> None:
        conn = self.get_conn()
        now = _now_ms()

        if append_keywords:
            cur = conn.execute(
                f"SELECT keywords FROM {TABLE_SESSIONS} WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if row and row["keywords"] and keywords:
                existing = set(kw.strip() for kw in row["keywords"].split(",") if kw.strip())
                new = set(kw.strip() for kw in keywords.split(",") if kw.strip())
                keywords = ", ".join(sorted(existing | new))
            elif row and row["keywords"]:
                keywords = row["keywords"]

        conn.execute(
            f"UPDATE {TABLE_SESSIONS} SET "
            f"time_reviewed = ?, review_count = review_count + 1, keywords = ? "
            f"WHERE id = ?",
            (now, keywords, session_id),
        )
        conn.commit()

    def mark_tbd(self, session_id: str) -> None:
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_SESSIONS} SET time_reviewed = 0 WHERE id = ?",
            (session_id,),
        )
        conn.commit()

    def get_unreviewed_sessions(self, limit: int = 50) -> list[SessionRow]:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_SESSIONS} "
            f"WHERE time_reviewed IS NULL AND time_archived IS NULL "
            f"ORDER BY time_updated ASC LIMIT ?",
            (limit,),
        )
        return [SessionRow(**dict(r)) for r in cur.fetchall()]

    def search_sessions_by_keywords(
        self, keywords: list[str], limit: int = 5,
    ) -> list[SessionRow]:
        conn = self.get_conn()
        results: dict[str, SessionRow] = {}
        for kw in keywords[:5]:
            cur = conn.execute(
                f"SELECT * FROM {TABLE_SESSIONS} "
                f"WHERE keywords LIKE ? "
                f"AND time_archived IS NULL "
                f"ORDER BY time_updated DESC "
                f"LIMIT ?",
                (f"%{kw}%", limit),
            )
            for r in cur.fetchall():
                sid = r["id"]
                if sid not in results:
                    results[sid] = SessionRow(**dict(r))
        return sorted(
            results.values(),
            key=lambda s: s.time_updated,
            reverse=True,
        )[:limit]

    def get_message_count(self, session_id: str) -> int:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT COUNT(*) FROM {TABLE_MESSAGES} WHERE session_id=?;",
            (session_id,),
        )
        return cur.fetchone()[0]

    # ── Message CRUD ───────────────────────────────────────────────────────

    def insert_message(self, row: MessageRow) -> str:
        conn = self.get_conn()
        conn.execute(
            f"""INSERT INTO {TABLE_MESSAGES}
                (id, session_id, role, usage, time_created)
                VALUES (?, ?, ?, ?, ?);""",
            (row.id, row.session_id, row.role, row.usage,
             row.time_created or _now_ms()),
        )
        return row.id

    def insert_part(self, row: PartRow) -> str:
        conn = self.get_conn()
        searchable = self._extract_searchable_text(row.data, row.type)
        conn.execute(
            f"""INSERT INTO {TABLE_PARTS}
                (id, message_id, session_id, type, data, searchable_text, time_created)
                VALUES (?, ?, ?, ?, ?, ?, ?);""",
            (row.id, row.message_id, row.session_id, row.type, row.data,
             searchable, row.time_created or _now_ms()),
        )
        return row.id

    def load_messages(self, session_id: str) -> list[dict]:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_MESSAGES} WHERE session_id=? "
            "ORDER BY time_created ASC, id ASC;",
            (session_id,),
        )
        messages = []
        for mrow in cur.fetchall():
            msg = dict(mrow)
            pcur = conn.execute(
                f"SELECT * FROM {TABLE_PARTS} WHERE message_id=? "
                "ORDER BY time_created ASC, id ASC;",
                (mrow["id"],),
            )
            msg["parts"] = [dict(p) for p in pcur.fetchall()]
            messages.append(msg)
        return messages

    def delete_messages(self, session_id: str) -> None:
        conn = self.get_conn()
        conn.execute(f"DELETE FROM {TABLE_PARTS} WHERE session_id=?;", (session_id,))
        conn.execute(f"DELETE FROM {TABLE_MESSAGES} WHERE session_id=?;", (session_id,))
        conn.commit()

    # ── LLM usage log (console 用量分析) ────────────────────────────────────

    def insert_usage_log(
        self,
        *,
        ts: int,
        model: str = "",
        source: str = "chat",
        tokens_in: int = 0,
        tokens_out: int = 0,
        session_id: str = "",
        status: str = "ok",
        dedup_key: str | None = None,
    ) -> bool:
        """Append one usage row. Best-effort: never raises (logging must not
        break the calling pipeline). Returns True when a row was inserted."""
        try:
            conn = self.get_conn()
            cur = conn.execute(
                f"""INSERT OR IGNORE INTO {TABLE_LLM_USAGE_LOG}
                    (ts, model, source, tokens_in, tokens_out,
                     session_id, status, dedup_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                (int(ts), model, source, int(tokens_in), int(tokens_out),
                 session_id, status, dedup_key, _now_ms()),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            return False

    def usage_daily_by_model(self, start_ms: int, end_ms: int) -> list[dict]:
        """本地日 × 模型交叉聚合 —— 用量总览(统计卡/堆叠柱/模型卡)共用一份数据。"""
        conn = self.get_conn()
        cur = conn.execute(
            f"""SELECT date(ts/1000, 'unixepoch', 'localtime') AS day,
                       model,
                       SUM(tokens_in)  AS tokens_in,
                       SUM(tokens_out) AS tokens_out,
                       COUNT(*)        AS calls,
                       SUM(status='error') AS errors
                FROM {TABLE_LLM_USAGE_LOG}
                WHERE ts >= ? AND ts < ? AND {_usage_test_model_filter()}
                GROUP BY day, model
                ORDER BY day, calls DESC;""",
            (start_ms, end_ms),
        )
        return [dict(r) for r in cur.fetchall()]

    def usage_totals_since(self, start_ms: int, end_ms: int) -> dict:
        """Totals plus a per-source breakdown between the epoch-ms bounds."""
        conn = self.get_conn()
        row = conn.execute(
            f"""SELECT SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out,
                       COUNT(*) AS calls
                FROM {TABLE_LLM_USAGE_LOG}
                WHERE ts >= ? AND ts < ? AND {_usage_test_model_filter()};""",
            (start_ms, end_ms),
        ).fetchone()
        cur = conn.execute(
            f"""SELECT source, SUM(tokens_in) AS tokens_in,
                       SUM(tokens_out) AS tokens_out, COUNT(*) AS calls
                FROM {TABLE_LLM_USAGE_LOG}
                WHERE ts >= ? AND ts < ? AND {_usage_test_model_filter()}
                GROUP BY source ORDER BY calls DESC;""",
            (start_ms, end_ms),
        )
        return {
            "tokens_in": row["tokens_in"] or 0,
            "tokens_out": row["tokens_out"] or 0,
            "calls": row["calls"] or 0,
            "by_source": [dict(r) for r in cur.fetchall()],
        }
