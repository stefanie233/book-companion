"""Wingman database layer — zero external dependencies.

Operates on a dedicated memory.db, independent of the main session database.
Provides CRUD for memory_entries, memory_tags, and tag_registry.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wingman.models import (
    ALL_MIGRATIONS,
    DDL_SCHEMA_VERSION,
    MemoryEntryRow,
    PersonaDocRow,
    STATE_LAST_PERSONA_TIME,
    STATE_MEMORIES_SINCE_PERSONA,
    STATE_PERSONA_UPDATE_REASON,
    STATE_REQUEST_PERSONA_UPDATE,
    TagRow,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str = "") -> str:
    suffix = uuid.uuid4().hex[:16]
    return f"{prefix}-{suffix}" if prefix else suffix


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class WingmanDatabase:
    """SQLite wrapper for memory.db — memory_entries, memory_tags, tag_registry.

    Usage:
        wdb = WingmanDatabase(Path("data/memory.db"))
        wdb.initialize()
        wdb.insert_memory_entry_simple(content="...", summary="...", keywords="...")
    """

    def __init__(self, db_path: Path):
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

    # ── Connection lifecycle ────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
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

    def initialize(self) -> None:
        """Create tables and run pending migrations."""
        conn = self._get_conn()

        # Ensure schema_version exists (this DDL is applied first)
        conn.execute(DDL_SCHEMA_VERSION)

        # Check current version
        cur = conn.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        current_version = row[0] if row[0] is not None else 0

        # Run pending migrations
        for ver, desc, sql_list in ALL_MIGRATIONS:
            if ver > current_version:
                if isinstance(sql_list, str):
                    conn.executescript(sql_list)
                else:
                    for sql in sql_list:
                        conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (ver, _now_ms()),
                )
                conn.commit()


    @staticmethod
    def _row_to_tag(row: sqlite3.Row) -> TagRow:
        return TagRow(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            reply_style=row["reply_style"],
            usage_count=row["usage_count"],
            behavior=row["behavior"] if "behavior" in row.keys() else "normal",
            behavior_prompt=row["behavior_prompt"] if "behavior_prompt" in row.keys() else "",
            cite_threshold=row["cite_threshold"] if "cite_threshold" in row.keys() else 10,
            default_weight=row["default_weight"] if "default_weight" in row.keys() else 1.0,
            created_at=row["created_at"],
        )

    # ── Tags ────────────────────────────────────────────────────────────

    def insert_tag(self, row: TagRow) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO tag_registry (id, name, category, reply_style, "
            "usage_count, behavior, behavior_prompt, cite_threshold, "
            "default_weight, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row.id, row.name, row.category, row.reply_style,
             row.usage_count, row.behavior, row.behavior_prompt,
             row.cite_threshold, row.default_weight, row.created_at),
        )
        conn.commit()

    def get_tag_by_name(self, name: str) -> TagRow | None:
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM tag_registry WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_tag(row)

    def list_tags(self, category: str | None = None) -> list[TagRow]:
        conn = self._get_conn()
        if category:
            cur = conn.execute(
                "SELECT * FROM tag_registry WHERE category = ? ORDER BY usage_count DESC",
                (category,),
            )
        else:
            cur = conn.execute("SELECT * FROM tag_registry ORDER BY usage_count DESC")
        return [self._row_to_tag(r) for r in cur.fetchall()]

    def increment_tag_usage(self, tag_id: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE tag_registry SET usage_count = usage_count + 1 WHERE id = ?",
            (tag_id,),
        )
        conn.commit()


    def insert_memory_entry(self, row: MemoryEntryRow) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO memory_entries (id, content, summary, status, visibility, "
            "cited_count, last_cited_at, source_session_id, "
            "first_created_at, last_updated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row.id, row.content, row.summary, row.status, row.visibility,
             row.cited_count, row.last_cited_at, row.source_session_id,
             row.first_created_at, row.last_updated_at, row.created_at),
        )
        conn.commit()

    def insert_memory_entry_simple(
        self, content: str, *,
        summary: str = "", keywords: str = "",
        visibility: str = "normal", source_session_id: str | None = None,
        memory_type: str = "episodic", priority: int = 50,
        scene_name: str = "",
    ) -> str:
        """Insert a memory entry with L1 pipeline fields."""
        memory_id = _new_id("mem")
        now = _now_ms()
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO memory_entries (id, content, summary, keywords, status, "
            "visibility, cited_count, last_cited_at, source_session_id, "
            "first_created_at, last_updated_at, created_at, "
            "memory_type, priority, scene_name) "
            "VALUES (?, ?, ?, ?, 'active', ?, 0, NULL, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, content, summary, keywords,
             visibility, source_session_id, now, now, now,
             memory_type, priority, scene_name),
        )
        conn.commit()
        return memory_id

    def delete_memory_entry(self, memory_id: str) -> None:
        """Soft-delete a memory entry (set status='deleted'). Used by dedup merge/update."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE memory_entries SET status = 'deleted', last_updated_at = ? WHERE id = ?",
            (_now_ms(), memory_id),
        )
        conn.commit()

    def search_memories_by_similarity(
        self, query: str, limit: int = 5,
    ) -> list[MemoryEntryRow]:
        """Find similar memories for dedup candidate recall.

        Initial implementation: LIKE-based search across content.
        Phase 5 will replace with HRR vector similarity.
        """
        conn = self._get_conn()
        like_q = f"%{query}%"
        cur = conn.execute(
            "SELECT * FROM memory_entries "
            "WHERE (content LIKE ? OR keywords LIKE ?) "
            "AND status = 'active' "
            "ORDER BY priority DESC, last_updated_at DESC "
            "LIMIT ?",
            (like_q, like_q, limit),
        )
        return [self._row_to_memory_entry(r) for r in cur.fetchall()]

    def apply_dedup_decisions(
        self, decisions: list[dict], new_memories: list[dict],
    ) -> int:
        """Apply batch dedup decisions (store/skip/update/merge) to DB.

        Args:
            decisions: list of {record_id, action, target_ids, merged_content,
                                merged_type, merged_priority, merged_timestamps}
            new_memories: list of {record_id, content, type, priority,
                                   scene_name, source_message_ids, metadata}
        Returns number of memories written.
        """
        mem_by_id = {m["record_id"]: m for m in new_memories}
        written = 0
        for d in decisions:
            mem = mem_by_id.get(d["record_id"])
            if not mem:
                continue
            action = d.get("action", "store")
            if action == "skip":
                continue
            if action in ("update", "merge"):
                # Delete old records that are being replaced
                for tid in d.get("target_ids", []):
                    self.delete_memory_entry(tid)
                content = d.get("merged_content", mem["content"])
                mtype = d.get("merged_type", mem.get("type", "episodic"))
                priority = d.get("merged_priority", mem.get("priority", 50))
            else:  # store
                content = mem["content"]
                mtype = mem.get("type", "episodic")
                priority = mem.get("priority", 50)

            self.insert_memory_entry_simple(
                content,
                summary=mem.get("summary", ""),
                keywords=mem.get("keywords", ""),
                memory_type=mtype,
                priority=priority,
                scene_name=mem.get("scene_name", ""),
                source_session_id=mem.get("source_session_id"),
            )
            written += 1
        return written

    def get_memory_by_id(self, memory_id: str) -> MemoryEntryRow | None:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM memory_entries WHERE id = ?", (memory_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_memory_entry(row)

    def search_memories(
        self, query: str, limit: int = 5,
        exclude_visibility: list[str] | None = None,
    ) -> list[MemoryEntryRow]:
        """Keyword search across content and summary, with optional visibility filter."""
        conn = self._get_conn()
        exclude = exclude_visibility or []
        like_q = f"%{query}%"
        if exclude:
            placeholders = ",".join("?" * len(exclude))
            cur = conn.execute(
                f"SELECT * FROM memory_entries "
                f"WHERE (content LIKE ? OR summary LIKE ? OR keywords LIKE ?) "
                f"AND status = 'active' "
                f"AND visibility NOT IN ({placeholders}) "
                f"ORDER BY last_updated_at DESC "
                f"LIMIT ?",
                (like_q, like_q, like_q, *exclude, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM memory_entries "
                "WHERE (content LIKE ? OR summary LIKE ? OR keywords LIKE ?) "
                "AND status = 'active' "
                "ORDER BY last_updated_at DESC "
                "LIMIT ?",
                (like_q, like_q, like_q, limit),
            )
        return [self._row_to_memory_entry(r) for r in cur.fetchall()]

    def search_memories_by_keywords(
        self, keywords: list[str], limit: int = 5,
        exclude_visibility: list[str] | None = None,
    ) -> list[MemoryEntryRow]:
        """Multi-keyword search with deduplication and recency-based ranking."""
        conn = self._get_conn()
        exclude = exclude_visibility or []
        results: dict[str, MemoryEntryRow] = {}
        for kw in keywords[:5]:
            like_q = f"%{kw}%"
            if exclude:
                placeholders = ",".join("?" * len(exclude))
                cur = conn.execute(
                    f"SELECT * FROM memory_entries "
                    f"WHERE (content LIKE ? OR summary LIKE ? OR keywords LIKE ?) "
                    f"AND status = 'active' "
                    f"AND visibility NOT IN ({placeholders}) "
                    f"ORDER BY last_updated_at DESC "
                    f"LIMIT ?",
                    (like_q, like_q, like_q, *exclude, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM memory_entries "
                    "WHERE (content LIKE ? OR summary LIKE ? OR keywords LIKE ?) "
                    "AND status = 'active' "
                    "ORDER BY last_updated_at DESC "
                    "LIMIT ?",
                    (like_q, like_q, like_q, limit),
                )
            for row in cur.fetchall():
                mid = row["id"]
                if mid not in results:
                    results[mid] = self._row_to_memory_entry(row)
        return sorted(
            results.values(),
            key=lambda m: m.last_updated_at,
            reverse=True,
        )[:limit]

    def get_memories_by_tag(
        self, tag: str, limit: int = 10,
        exclude_visibility: list[str] | None = None,
    ) -> list[MemoryEntryRow]:
        """List memories under a given tag, ordered by weight."""
        conn = self._get_conn()
        exclude = exclude_visibility or []
        if exclude:
            placeholders = ",".join("?" * len(exclude))
            cur = conn.execute(
                f"SELECT me.* FROM memory_entries me "
                f"JOIN memory_tags mt ON me.id = mt.memory_id "
                f"JOIN tag_registry tr ON mt.tag_id = tr.id "
                f"WHERE tr.name = ? AND me.status = 'active' "
                f"AND me.visibility NOT IN ({placeholders}) "
                f"ORDER BY me.last_updated_at DESC "
                f"LIMIT ?",
                (tag, *exclude, limit),
            )
        else:
            cur = conn.execute(
                "SELECT me.* FROM memory_entries me "
                "JOIN memory_tags mt ON me.id = mt.memory_id "
                "JOIN tag_registry tr ON mt.tag_id = tr.id "
                "WHERE tr.name = ? AND me.status = 'active' "
                "ORDER BY me.last_updated_at DESC "
                "LIMIT ?",
                (tag, limit),
            )
        return [self._row_to_memory_entry(r) for r in cur.fetchall()]

    def get_memories_by_session(
        self, session_id: str, limit: int = 50,
    ) -> list[MemoryEntryRow]:
        """Return all memories extracted from a given session."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM memory_entries "
            "WHERE source_session_id = ? AND status = 'active' "
            "ORDER BY last_updated_at DESC "
            "LIMIT ?",
            (session_id, limit),
        )
        return [self._row_to_memory_entry(r) for r in cur.fetchall()]

    def increment_memory_cited(self, memory_id: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE memory_entries SET cited_count = cited_count + 1, "
            "last_cited_at = ? WHERE id = ?",
            (_now_ms(), memory_id),
        )
        conn.commit()

    # ── Memory Tags ────────────────────────────────────────────────────

    def insert_memory_tags(self, memory_id: str, tag_ids: list[str]) -> None:
        conn = self._get_conn()
        for tag_id in tag_ids:
            conn.execute(
                "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id) VALUES (?, ?)",
                (memory_id, tag_id),
            )
        conn.commit()

    def get_tags_for_memory(self, memory_id: str) -> list[TagRow]:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT t.* FROM tag_registry t "
            "JOIN memory_tags mt ON t.id = mt.tag_id "
            "WHERE mt.memory_id = ?",
            (memory_id,),
        )
        return [self._row_to_tag(r) for r in cur.fetchall()]

    def get_unconsumed_memories(
        self, limit: int = 50, exclude_visibility: list[str] | None = None,
    ) -> list[MemoryEntryRow]:
        """Get active L1 memories not yet consumed by the L2 scene pipeline.

        L2 extracts scene blocks from these, then marks them consumed via
        mark_scene_consumed().
        """
        conn = self._get_conn()
        exclude = exclude_visibility or []
        if exclude:
            placeholders = ",".join("?" * len(exclude))
            cur = conn.execute(
                f"SELECT * FROM memory_entries "
                f"WHERE status = 'active' AND scene_consumed = 0 "
                f"AND visibility NOT IN ({placeholders}) "
                f"ORDER BY created_at ASC LIMIT ?",
                (*exclude, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM memory_entries "
                "WHERE status = 'active' AND scene_consumed = 0 "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
        return [self._row_to_memory_entry(r) for r in cur.fetchall()]

    def mark_scene_consumed(self, memory_ids: list[str]) -> None:
        """Mark L1 memories as consumed by the L2 scene pipeline."""
        if not memory_ids:
            return
        conn = self._get_conn()
        placeholders = ",".join("?" * len(memory_ids))
        conn.execute(
            f"UPDATE memory_entries SET scene_consumed = 1, last_updated_at = ? "
            f"WHERE id IN ({placeholders})",
            (_now_ms(), *memory_ids),
        )
        conn.commit()

    def get_unconsumed_persona_memories(
        self,
        limit: int = 30,
        priority_floor: int = 60,
        exclude_visibility: list[str] | None = None,
    ) -> list[MemoryEntryRow]:
        """Get unconsumed high-priority persona/instruction memories for L3.

        L3 (persona) aggregates these — the life profile that L2 (per-book
        scenes) doesn't absorb. Memory rows stay searchable after consumption.
        """
        conn = self._get_conn()
        exclude = exclude_visibility or []
        if exclude:
            placeholders = ",".join("?" * len(exclude))
            cur = conn.execute(
                f"SELECT * FROM memory_entries "
                f"WHERE status = 'active' AND scene_consumed = 0 "
                f"AND memory_type IN ('persona', 'instruction') "
                f"AND priority >= ? "
                f"AND visibility NOT IN ({placeholders}) "
                f"ORDER BY priority DESC, created_at ASC LIMIT ?",
                (priority_floor, *exclude, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM memory_entries "
                "WHERE status = 'active' AND scene_consumed = 0 "
                "AND memory_type IN ('persona', 'instruction') "
                "AND priority >= ? "
                "ORDER BY priority DESC, created_at ASC LIMIT ?",
                (priority_floor, limit),
            )
        return [self._row_to_memory_entry(r) for r in cur.fetchall()]

    def get_active_memories_since(self, since_ms: int) -> list[MemoryEntryRow]:
        """Get active memories updated since a given timestamp."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM memory_entries "
            "WHERE status = 'active' AND last_updated_at >= ? "
            "AND visibility != 'forbidden' "
            "ORDER BY last_updated_at DESC",
            (since_ms,),
        )
        return [self._row_to_memory_entry(r) for r in cur.fetchall()]

    @staticmethod
    def _row_to_memory_entry(row: sqlite3.Row) -> MemoryEntryRow:
        return MemoryEntryRow(
            id=row["id"],
            content=row["content"],
            summary=row["summary"],
            keywords=row["keywords"],
            status=row["status"],
            visibility=row["visibility"],
            cited_count=row["cited_count"],
            last_cited_at=row["last_cited_at"],
            source_session_id=row["source_session_id"],
            first_created_at=row["first_created_at"],
            last_updated_at=row["last_updated_at"],
            created_at=row["created_at"],
            memory_type=row["memory_type"] if "memory_type" in row.keys() else "episodic",
            priority=row["priority"] if "priority" in row.keys() else 50,
            scene_name=row["scene_name"] if "scene_name" in row.keys() else "",
            scene_consumed=row["scene_consumed"] if "scene_consumed" in row.keys() else 0,
        )

    # ── Voice roles ─────────────────────────────────────────────────────
    # character -> voicedesign prompt + ref_audio mapping (v8)

    @staticmethod
    def _row_to_voice_role(row: sqlite3.Row) -> dict:
        return {
            "book_id": row["book_id"],
            "character_name": row["character_name"],
            "design_prompt": row["design_prompt"],
            "ref_audio_path": row["ref_audio_path"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_voice_role(self, book_id: str, character_name: str) -> dict | None:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM voice_roles WHERE book_id=? AND character_name=?",
            (book_id, character_name),
        )
        row = cur.fetchone()
        return self._row_to_voice_role(row) if row else None

    def upsert_voice_role(self, book_id: str, character_name: str,
                          design_prompt: str, ref_audio_path: str) -> None:
        conn = self._get_conn()
        now = _now_ms()
        conn.execute(
            "INSERT INTO voice_roles (book_id, character_name, design_prompt, "
            "ref_audio_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(book_id, character_name) DO UPDATE SET "
            "design_prompt=excluded.design_prompt, "
            "ref_audio_path=excluded.ref_audio_path, updated_at=excluded.updated_at",
            (book_id, character_name, design_prompt, ref_audio_path, now, now),
        )
        conn.commit()

    def list_voice_roles(self, book_id: str) -> list[dict]:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM voice_roles WHERE book_id=? ORDER BY updated_at DESC",
            (book_id,),
        )
        return [self._row_to_voice_role(r) for r in cur.fetchall()]

    # ── Persona docs (v11) — git-like versioned profile snapshots ──────
    # persona_docs: 每次 L3 生成 = 一条 commit（version 递增，content 全量快照）
    #   source = 'l3' (agent 提交) | 'user_overlay' (用户直接改 persona.md 被检测到)

    @staticmethod
    def _row_to_persona_doc(row: sqlite3.Row) -> PersonaDocRow:
        return PersonaDocRow(
            id=row["id"],
            version=row["version"],
            content_md5=row["content_md5"],
            content=row["content"],
            source=row["source"],
            created_at=row["created_at"],
        )

    def insert_persona_doc(self, content: str, source: str = "l3") -> int:
        """Insert a new persona snapshot (next version). Returns version number."""
        conn = self._get_conn()
        cur = conn.execute("SELECT COALESCE(MAX(version), 0) FROM persona_docs")
        version = (cur.fetchone()[0] or 0) + 1
        conn.execute(
            "INSERT INTO persona_docs (version, content_md5, content, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (version, _md5(content), content, source, _now_iso()),
        )
        conn.commit()
        return version

    def get_latest_persona_doc(self) -> PersonaDocRow | None:
        """Latest persona snapshot, or None if never generated."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM persona_docs ORDER BY version DESC LIMIT 1",
        )
        row = cur.fetchone()
        return self._row_to_persona_doc(row) if row else None

    def list_persona_docs(self, limit: int = 10) -> list[PersonaDocRow]:
        """Persona commit history (git log), newest first."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM persona_docs ORDER BY version DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_persona_doc(r) for r in cur.fetchall()]

    def delete_persona_docs(self) -> None:
        """Drop all persona history (admin/reset only)."""
        conn = self._get_conn()
        conn.execute("DELETE FROM persona_docs")
        conn.commit()

    # ── Pipeline checkpoint (v11) — L3 trigger state (KV) ──────────────

    def get_pipeline_state(self, key: str, default: str = "") -> str:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT value FROM pipeline_state WHERE key = ?", (key,),
        )
        row = cur.fetchone()
        return row["value"] if row else default

    def set_pipeline_state(self, key: str, value: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO pipeline_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, _now_iso()),
        )
        conn.commit()

    # ── Persona checkpoint helpers (mirror TencentDB CheckpointManager) ─

    def get_persona_checkpoint(self) -> dict[str, str]:
        """Read L3 trigger state."""
        return {
            "last_persona_time": self.get_pipeline_state(STATE_LAST_PERSONA_TIME),
            "memories_since_last_persona": int(
                self.get_pipeline_state(STATE_MEMORIES_SINCE_PERSONA, "0") or 0
            ),
            "request_persona_update": (
                self.get_pipeline_state(STATE_REQUEST_PERSONA_UPDATE, "0") == "1"
            ),
            "persona_update_reason": self.get_pipeline_state(
                STATE_PERSONA_UPDATE_REASON
            ),
        }

    def mark_persona_generated(self) -> None:
        """Record that L3 ran: bump last_persona_time, reset counter + P1 flag."""
        self.set_pipeline_state(STATE_LAST_PERSONA_TIME, _now_iso())
        self.set_pipeline_state(STATE_MEMORIES_SINCE_PERSONA, "0")
        self.set_pipeline_state(STATE_REQUEST_PERSONA_UPDATE, "0")
        self.set_pipeline_state(STATE_PERSONA_UPDATE_REASON, "")

    def increment_memories_since_persona(self, n: int = 1) -> None:
        """Count new L1 memories toward the P4 threshold."""
        current = int(self.get_pipeline_state(STATE_MEMORIES_SINCE_PERSONA, "0") or 0)
        self.set_pipeline_state(STATE_MEMORIES_SINCE_PERSONA, str(current + n))

    def set_persona_update_request(self, reason: str) -> None:
        """P1: agent explicitly requested a persona update."""
        self.set_pipeline_state(STATE_REQUEST_PERSONA_UPDATE, "1")
        self.set_pipeline_state(STATE_PERSONA_UPDATE_REASON, reason)
