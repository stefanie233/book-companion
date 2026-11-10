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
    ALTER_SESSIONS_AUTO_MODE_SWITCH,
    ALTER_SESSIONS_ROLE_KIND,
    ALTER_SESSIONS_ROLE_REF,
    ALTER_SESSIONS_TIME_REVIEWED,
    ALTER_SESSIONS_WORK_ID,
    ALTER_SESSIONS_WRITING_MODE,
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

#: 事件式压缩的 checkpoint。摘要必须逐字稳定——两次压缩之间
#: 它进每次请求的前缀，任何重写都会让缓存从摘要处整段失效。
_CREATE_SESSION_COMPACTIONS = """
CREATE TABLE IF NOT EXISTS session_compactions (
    session_id     TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    raw_count      INTEGER NOT NULL,
    summary        TEXT NOT NULL,
    dropped_count  INTEGER NOT NULL DEFAULT 0,
    tokens_before  INTEGER NOT NULL DEFAULT 0,
    tokens_after   INTEGER NOT NULL DEFAULT 0,
    trigger        TEXT NOT NULL DEFAULT 'auto',
    time_created   INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""

#: 身份资料（称呼 / 性别 / agent 名字）的变更证据。append-only：
#: 每改一个字段一行，带改它的会话与时间，便于回答「什么时候知道的」。
_CREATE_IDENTITY_EVENTS = """
CREATE TABLE IF NOT EXISTS identity_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    field         TEXT NOT NULL,
    old_value     TEXT NOT NULL DEFAULT '',
    new_value     TEXT NOT NULL DEFAULT '',
    session_id    TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'agent',
    time_created  INTEGER NOT NULL
);
"""

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
    (12, "Playable-role kind: character (graph) vs author (authors.db)", [
        ALTER_SESSIONS_ROLE_KIND,
        ALTER_SESSIONS_ROLE_REF,
    ]),
    (13, "Session compaction checkpoints (event compaction)", [
        _CREATE_SESSION_COMPACTIONS,
        "CREATE INDEX IF NOT EXISTS idx_scomp_session "
        "ON session_compactions(session_id, seq);",
    ]),
    (14, "Writing sessions bound to a work (work_id)", [
        ALTER_SESSIONS_WORK_ID,
        "CREATE INDEX IF NOT EXISTS idx_sessions_work "
        "ON sessions(work_id);",
    ]),
    (15, "Writing sub-mode (standard/detail) + autonomous switch permission", [
        ALTER_SESSIONS_WRITING_MODE,
        ALTER_SESSIONS_AUTO_MODE_SWITCH,
    ]),
    (16, "Identity change journal: which session/turn produced a name/gender", [
        _CREATE_IDENTITY_EVENTS,
        "CREATE INDEX IF NOT EXISTS idx_identity_events_time "
        "ON identity_events(time_created);",
        "CREATE INDEX IF NOT EXISTS idx_identity_events_session "
        "ON identity_events(session_id);",
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
                (id, project_id, work_id, writing_mode, auto_mode_switch,
                 parent_id, title, agent, model, mode,
                 character_name, role_kind, role_ref, fork_point_index,
                 compaction_count, message_count, time_created, time_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
            (row.id, row.project_id, row.work_id or "",
             row.writing_mode or "standard", int(row.auto_mode_switch or 0),
             row.parent_id, row.title,
             row.agent, row.model, row.mode,
             row.character_name or "",
             row.role_kind or "character", row.role_ref or "",
             row.fork_point_index,
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
        mode: str | None = None,
        exclude_mode: str | None = None,
        work_id: str | None = None,
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
        # 会话按页面分线（决策 84）：写作页只取写作会话，其它页排除它们。
        if mode is not None:
            conditions.append("mode=?")
            params.append(mode)
        if exclude_mode is not None:
            conditions.append("mode<>?")
            params.append(exclude_mode)
        if work_id is not None:
            conditions.append("work_id=?")
            params.append(work_id)
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
        mode: str | None = None, exclude_mode: str | None = None,
    ) -> list[dict]:
        conn = self.get_conn()
        results: list[dict] = []

        safe = query.translate(str.maketrans("", "", '*"()'))
        fts_query = f'"{safe}"' if " " in safe else safe

        archive_filter = "" if include_archived else "AND s.time_archived IS NULL"
        # 搜索也要按页面的会话线过滤：写作会话不能出现在主页/阅读页的
        # 结果里，反之亦然（页签已经藏了，搜索不能成为后门）。
        line_filter = ""
        bare_filter = ""
        line_params: list[Any] = []
        if mode is not None:
            line_filter += "AND s.mode=? "
            bare_filter += "AND s.mode=? "
            line_params.append(mode)
        if exclude_mode is not None:
            line_filter += "AND s.mode<>? "
            bare_filter += "AND s.mode<>? "
            line_params.append(exclude_mode)
        try:
            cur = conn.execute(
                f"SELECT m.session_id, s.title, s.mode, s.work_id, s.time_updated, "
                f"p.type, m.role, "
                f"snippet({TABLE_SESSION_FTS}, 0, '…', '…', '', 40) as snippet "
                f"FROM {TABLE_SESSION_FTS} "
                f"JOIN {TABLE_PARTS} p ON {TABLE_SESSION_FTS}.rowid = p.rowid "
                f"JOIN {TABLE_MESSAGES} m ON p.message_id = m.id "
                f"JOIN {TABLE_SESSIONS} s ON m.session_id = s.id "
                f"WHERE {TABLE_SESSION_FTS} MATCH ? {archive_filter} "
                f"{line_filter}"
                f"ORDER BY rank LIMIT ?;",
                (fts_query, *line_params, limit * 3),
            )
            results = [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            pass

        if not results:
            like_pattern = f"%{query}%"
            cur = conn.execute(
                f"SELECT DISTINCT m.session_id, s.title, s.mode, s.work_id, "
                f"s.time_updated, p.data, p.type, m.role "
                f"FROM {TABLE_PARTS} p "
                f"JOIN {TABLE_MESSAGES} m ON p.message_id = m.id "
                f"JOIN {TABLE_SESSIONS} s ON m.session_id = s.id "
                f"WHERE p.searchable_text LIKE ? {archive_filter} "
                f"{bare_filter}"
                f"ORDER BY s.time_updated DESC "
                f"LIMIT ?",
                (like_pattern, *line_params, limit * 3),
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
                "work_id": r.get("work_id", "") or "",
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
        # Tie-break on rowid, not id: ids are random UUIDs, so sorting equal
        # timestamps by id scrambles the order. Fork copies used to be
        # stamped all at once (same millisecond), which made branch history
        # load out of order.
        cur = conn.execute(
            f"SELECT * FROM {TABLE_MESSAGES} WHERE session_id=? "
            "ORDER BY time_created ASC, rowid ASC;",
            (session_id,),
        )
        messages = []
        for mrow in cur.fetchall():
            msg = dict(mrow)
            pcur = conn.execute(
                f"SELECT * FROM {TABLE_PARTS} WHERE message_id=? "
                "ORDER BY time_created ASC, rowid ASC;",
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

    # ── Compaction checkpoints ────────────────────────────────────────────

    def insert_compaction(
        self,
        session_id: str,
        *,
        seq: int,
        raw_count: int,
        summary: str,
        dropped_count: int = 0,
        tokens_before: int = 0,
        tokens_after: int = 0,
        trigger: str = "auto",
    ) -> None:
        """写一条压缩 checkpoint。``seq`` 单调递增，最新一条即当前前缀。"""
        conn = self.get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO session_compactions
               (session_id, seq, raw_count, summary, dropped_count,
                tokens_before, tokens_after, trigger, time_created)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);""",
            (session_id, int(seq), int(raw_count), summary or "",
             int(dropped_count), int(tokens_before), int(tokens_after),
             trigger or "auto", _now_ms()),
        )
        conn.commit()

    def latest_compaction(self, session_id: str) -> dict[str, Any] | None:
        """最新 checkpoint；没有返回 None。"""
        row = self.get_conn().execute(
            "SELECT * FROM session_compactions WHERE session_id=? "
            "ORDER BY seq DESC LIMIT 1;",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

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

    # ── Identity change journal ────────────────────────────────────────────

    def insert_identity_event(
        self,
        field: str,
        old_value: str,
        new_value: str,
        *,
        session_id: str = "",
        source: str = "agent",
        time_created: int | None = None,
    ) -> None:
        """Append one identity change.

        ``session_id`` 是「从哪次对话里得到这条信息」的证据；设置页改的
        没有会话，留空。append-only，不更新、不删除。
        """
        conn = self.get_conn()
        conn.execute(
            """INSERT INTO identity_events
               (field, old_value, new_value, session_id, source, time_created)
               VALUES (?, ?, ?, ?, ?, ?);""",
            (field, old_value or "", new_value or "", session_id or "",
             source or "agent", time_created or _now_ms()),
        )
        conn.commit()

    def list_identity_events(
        self,
        *,
        field: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Page through identity changes, newest first. Returns ``(rows, total)``."""
        where: list[str] = []
        params: list[Any] = []
        if field:
            where.append("field = ?")
            params.append(field)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        conn = self.get_conn()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM identity_events{where_sql};", params
        ).fetchone()["n"]
        cur = conn.execute(
            f"""SELECT id, field, old_value, new_value, session_id, source,
                       time_created
                FROM identity_events{where_sql}
                ORDER BY time_created DESC, id DESC
                LIMIT ? OFFSET ?;""",
            [*params, max(1, int(limit)), max(0, int(offset))],
        )
        return [dict(r) for r in cur.fetchall()], int(total)
