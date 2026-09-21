"""BookGraph database — knowledge graph for book content.

Owns ``graph.db``. Stores entities (nodes), relationships (edges),
aliases, extraction progress, and merge candidates.

Schema follows BookGraph-Loop-Engineering-Plan-v3 §3.1.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from storage.db_base import BaseDatabase, _now_ms

log = logging.getLogger("bookgraph")


# ── Table names ───────────────────────────────────────────────────────────

TABLE_NODES = "nodes"
TABLE_EDGES = "edges"
TABLE_ALIASES = "graph_aliases"
TABLE_GRAPH_CHUNKS = "graph_chunks"
TABLE_MERGE_CANDIDATES = "graph_merge_candidates"
TABLE_UNRESOLVED = "graph_unresolved"
TABLE_PROGRESS = "graph_progress"
TABLE_META = "graph_meta"
TABLE_LLM_LOGS = "graph_llm_logs"
TABLE_QUOTES = "graph_quotes"
TABLE_OBSERVATIONS = "graph_observations"
TABLE_TAG_REGISTRY = "graph_tag_registry"


# ── DDL ───────────────────────────────────────────────────────────────────

GRAPH_DDL = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NODES} (
        id                      TEXT PRIMARY KEY,
        canonical_name          TEXT NOT NULL,
        kind                    TEXT NOT NULL,
        description             TEXT,
        first_appearance_chunk_id TEXT,
        project_id              TEXT NOT NULL,
        created_at              TEXT DEFAULT (datetime('now')),
        UNIQUE(project_id, canonical_name, kind)
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_nodes_project ON {TABLE_NODES}(project_id);",
    f"CREATE INDEX IF NOT EXISTS idx_nodes_kind ON {TABLE_NODES}(project_id, kind);",

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_EDGES} (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id       TEXT NOT NULL REFERENCES {TABLE_NODES}(id) ON DELETE CASCADE,
        target_id       TEXT NOT NULL REFERENCES {TABLE_NODES}(id) ON DELETE CASCADE,
        kind            TEXT NOT NULL,
        chunk_id        TEXT,
        metadata        TEXT DEFAULT '{{}}',
        project_id      TEXT NOT NULL,
        UNIQUE(project_id, source_id, target_id, kind, chunk_id)
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_edges_source ON {TABLE_EDGES}(source_id);",
    f"CREATE INDEX IF NOT EXISTS idx_edges_target ON {TABLE_EDGES}(target_id);",
    f"CREATE INDEX IF NOT EXISTS idx_edges_kind ON {TABLE_EDGES}(project_id, kind);",
    f"CREATE INDEX IF NOT EXISTS idx_edges_chunk ON {TABLE_EDGES}(chunk_id);",

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_ALIASES} (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        alias_name  TEXT NOT NULL,
        node_id     TEXT NOT NULL REFERENCES {TABLE_NODES}(id) ON DELETE CASCADE,
        project_id  TEXT NOT NULL,
        UNIQUE(project_id, alias_name)
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_aliases_node ON {TABLE_ALIASES}(node_id);",

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_GRAPH_CHUNKS} (
        chunk_id          TEXT PRIMARY KEY,
        book_id           TEXT NOT NULL,
        chapter_index     INTEGER NOT NULL,
        chunk_index       INTEGER NOT NULL,
        content_hash      TEXT NOT NULL,
        extract_count     INTEGER DEFAULT 0,
        extract_status    TEXT DEFAULT 'pending',
        last_extract_at   TEXT,
        last_node_count   INTEGER
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_gc_book ON {TABLE_GRAPH_CHUNKS}(book_id);",
    f"CREATE INDEX IF NOT EXISTS idx_gc_chapter ON {TABLE_GRAPH_CHUNKS}(book_id, chapter_index);",
    f"CREATE INDEX IF NOT EXISTS idx_gc_status ON {TABLE_GRAPH_CHUNKS}(book_id, extract_status);",

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_MERGE_CANDIDATES} (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id_a   TEXT,
        name_a      TEXT,
        name_b      TEXT,
        confidence  REAL NOT NULL,
        reason      TEXT,
        status      TEXT DEFAULT 'pending',
        project_id  TEXT NOT NULL
    );
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_UNRESOLVED} (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        reference_name  TEXT NOT NULL,
        reference_kind  TEXT,
        chunk_id        TEXT,
        candidates      TEXT,
        project_id      TEXT NOT NULL
    );
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_PROGRESS} (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id     TEXT NOT NULL,
        batch_id    TEXT NOT NULL,
        step_index  INTEGER NOT NULL,
        step_label  TEXT,
        status      TEXT NOT NULL,
        detail      TEXT,
        updated_at  TEXT DEFAULT (datetime('now'))
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_progress_book ON {TABLE_PROGRESS}(book_id, updated_at);",

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_META} (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TEXT DEFAULT (datetime('now'))
    );
    """,

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_QUOTES} (
        id              TEXT PRIMARY KEY,
        character_name  TEXT NOT NULL,
        quote_text      TEXT NOT NULL,
        scene_context   TEXT,
        chunk_id        TEXT,
        batch_id        TEXT,
        related_nodes   TEXT DEFAULT '[]',
        project_id      TEXT NOT NULL,
        created_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(project_id, chunk_id, quote_text)
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_quotes_char ON {TABLE_QUOTES}(project_id, character_name);",
    f"CREATE INDEX IF NOT EXISTS idx_quotes_chunk ON {TABLE_QUOTES}(chunk_id);",

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_OBSERVATIONS} (
        id              TEXT PRIMARY KEY,
        project_id      TEXT NOT NULL,
        node_id         TEXT NOT NULL REFERENCES {TABLE_NODES}(id) ON DELETE CASCADE,
        field           TEXT NOT NULL,
        value           TEXT NOT NULL,
        event_type      TEXT NOT NULL DEFAULT 'perspective',
        subject         TEXT,
        anchor_chapter  INTEGER,
        evidence        TEXT,
        tags            TEXT DEFAULT '[]',
        observed_at     TEXT DEFAULT (datetime('now'))
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_obs_node ON {TABLE_OBSERVATIONS}(node_id, field, anchor_chapter);",
    f"CREATE INDEX IF NOT EXISTS idx_obs_project ON {TABLE_OBSERVATIONS}(project_id);",

    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_TAG_REGISTRY} (
        id              TEXT PRIMARY KEY,
        project_id      TEXT NOT NULL,
        name            TEXT NOT NULL,
        parent_type     TEXT NOT NULL,
        usage_count     INTEGER DEFAULT 0,
        created_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(project_id, name)
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_tagreg_project ON {TABLE_TAG_REGISTRY}(project_id, parent_type);",
]

# FTS5 + triggers (v3 corrected: columns strictly aligned, per CodeGraph schema)
GRAPH_FTS_DDL = [
    f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
        node_id, canonical_name, kind, description,
        content='{TABLE_NODES}', content_rowid='rowid'
    );
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON {TABLE_NODES} BEGIN
        INSERT INTO nodes_fts(rowid, node_id, canonical_name, kind, description)
        VALUES (NEW.rowid, NEW.id, NEW.canonical_name, NEW.kind, NEW.description);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON {TABLE_NODES} BEGIN
        INSERT INTO nodes_fts(nodes_fts, rowid, node_id, canonical_name, kind, description)
        VALUES ('delete', OLD.rowid, OLD.id, OLD.canonical_name, OLD.kind, OLD.description);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON {TABLE_NODES} BEGIN
        INSERT INTO nodes_fts(nodes_fts, rowid, node_id, canonical_name, kind, description)
        VALUES ('delete', OLD.rowid, OLD.id, OLD.canonical_name, OLD.kind, OLD.description);
        INSERT INTO nodes_fts(rowid, node_id, canonical_name, kind, description)
        VALUES (NEW.rowid, NEW.id, NEW.canonical_name, NEW.kind, NEW.description);
    END;
    """,
]


# ── GraphDB ───────────────────────────────────────────────────────────────

class GraphDB(BaseDatabase):
    """Knowledge graph database for book content.

    Schema per BookGraph-Loop-Engineering-Plan-v3 §3.1.
    Uses ``project_id`` = ``book_id`` to link with books.db.
    """

    _MIGRATIONS: list[tuple[int, str, str | list[str]]] = [
        (1, "BookGraph v3 initial schema", list(GRAPH_DDL) + list(GRAPH_FTS_DDL)),
        (2, "Add graph_quotes table", [
            f"""CREATE TABLE IF NOT EXISTS {TABLE_QUOTES} (
                id              TEXT PRIMARY KEY,
                character_name  TEXT NOT NULL,
                quote_text      TEXT NOT NULL,
                scene_context   TEXT,
                chunk_id        TEXT,
                batch_id        TEXT,
                related_nodes   TEXT DEFAULT '[]',
                project_id      TEXT NOT NULL,
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(project_id, chunk_id, quote_text)
            );""",
            f"CREATE INDEX IF NOT EXISTS idx_quotes_char ON {TABLE_QUOTES}(project_id, character_name);",
            f"CREATE INDEX IF NOT EXISTS idx_quotes_chunk ON {TABLE_QUOTES}(chunk_id);",
        ]),
        (3, "Add 5 structured character fields to graph_nodes", [
            f"ALTER TABLE {TABLE_NODES} ADD COLUMN personality TEXT;",
            f"ALTER TABLE {TABLE_NODES} ADD COLUMN appearance TEXT;",
            f"ALTER TABLE {TABLE_NODES} ADD COLUMN motivation TEXT;",
            f"ALTER TABLE {TABLE_NODES} ADD COLUMN role_in_story TEXT;",
            f"ALTER TABLE {TABLE_NODES} ADD COLUMN speaking_style TEXT;",
        ]),
        (4, "Event model: observations + tag registry + edges event_type/subject/tags", [
            f"ALTER TABLE {TABLE_EDGES} ADD COLUMN event_type TEXT DEFAULT 'fact';",
            f"ALTER TABLE {TABLE_EDGES} ADD COLUMN subject TEXT;",
            f"ALTER TABLE {TABLE_EDGES} ADD COLUMN tags TEXT DEFAULT '[]';",
            f"""CREATE TABLE IF NOT EXISTS {TABLE_OBSERVATIONS} (
                id              TEXT PRIMARY KEY,
                project_id      TEXT NOT NULL,
                node_id         TEXT NOT NULL REFERENCES {TABLE_NODES}(id) ON DELETE CASCADE,
                field           TEXT NOT NULL,
                value           TEXT NOT NULL,
                event_type      TEXT NOT NULL DEFAULT 'perspective',
                subject         TEXT,
                anchor_chapter  INTEGER,
                evidence        TEXT,
                tags            TEXT DEFAULT '[]',
                observed_at     TEXT DEFAULT (datetime('now'))
            );""",
            f"CREATE INDEX IF NOT EXISTS idx_obs_node ON {TABLE_OBSERVATIONS}(node_id, field, anchor_chapter);",
            f"CREATE INDEX IF NOT EXISTS idx_obs_project ON {TABLE_OBSERVATIONS}(project_id);",
            f"""CREATE TABLE IF NOT EXISTS {TABLE_TAG_REGISTRY} (
                id              TEXT PRIMARY KEY,
                project_id      TEXT NOT NULL,
                name            TEXT NOT NULL,
                parent_type     TEXT NOT NULL,
                usage_count     INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(project_id, name)
            );""",
            f"CREATE INDEX IF NOT EXISTS idx_tagreg_project ON {TABLE_TAG_REGISTRY}(project_id, parent_type);",
        ]),
        (5, "Observation evolution: trust_score + temporal_decay + feedback + FTS5", [
            f"ALTER TABLE {TABLE_OBSERVATIONS} ADD COLUMN trust_score REAL DEFAULT 0.5;",
            # NOTE: ADD COLUMN with a non-constant default (datetime('now')) fails
            # on tables that already have rows ("non-constant default"). Add the
            # column without a default, backfill from observed_at, and let
            # add_observation() write updated_at explicitly.
            f"ALTER TABLE {TABLE_OBSERVATIONS} ADD COLUMN updated_at TEXT;",
            f"UPDATE {TABLE_OBSERVATIONS} SET updated_at = COALESCE(observed_at, datetime('now')) WHERE updated_at IS NULL;",
            f"ALTER TABLE {TABLE_OBSERVATIONS} ADD COLUMN retrieval_count INTEGER DEFAULT 0;",
            f"ALTER TABLE {TABLE_OBSERVATIONS} ADD COLUMN helpful_count INTEGER DEFAULT 0;",
            # FTS5 over observation text (replaces LIKE in graph_search)
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
                value, field, evidence,
                content='{TABLE_OBSERVATIONS}', content_rowid='rowid',
                tokenize='trigram'
            );
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS observations_fts_ai AFTER INSERT ON {TABLE_OBSERVATIONS} BEGIN
                INSERT INTO observations_fts(rowid, value, field, evidence)
                VALUES (NEW.rowid, NEW.value, NEW.field, NEW.evidence);
            END;
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS observations_fts_ad AFTER DELETE ON {TABLE_OBSERVATIONS} BEGIN
                INSERT INTO observations_fts(observations_fts, rowid, value, field, evidence)
                VALUES ('delete', OLD.rowid, OLD.value, OLD.field, OLD.evidence);
            END;
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS observations_fts_au AFTER UPDATE ON {TABLE_OBSERVATIONS} BEGIN
                INSERT INTO observations_fts(observations_fts, rowid, value, field, evidence)
                VALUES ('delete', OLD.rowid, OLD.value, OLD.field, OLD.evidence);
                INSERT INTO observations_fts(rowid, value, field, evidence)
                VALUES (NEW.rowid, NEW.value, NEW.field, NEW.evidence);
            END;
            """,
            # Backfill FTS5 index for existing observations
            f"INSERT INTO observations_fts(rowid, value, field, evidence) SELECT rowid, value, field, evidence FROM {TABLE_OBSERVATIONS};",
        ]),
        (6, "Observation source: distinguish book extraction vs dialogue extraction", [
            f"ALTER TABLE {TABLE_OBSERVATIONS} ADD COLUMN source TEXT DEFAULT 'book';",
        ]),
        (7, "Run scoping + LLM call logs", [
            # NOTE: ADD COLUMN with a non-constant default fails on tables that
            # already have rows. run_id uses a literal default so existing
            # graph_progress rows backfill to '' (legacy / no run).
            f"ALTER TABLE {TABLE_PROGRESS} ADD COLUMN run_id TEXT DEFAULT '';",
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_LLM_LOGS} (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        TEXT NOT NULL DEFAULT '',
                book_id       TEXT NOT NULL,
                phase         TEXT NOT NULL,
                batch_id      TEXT NOT NULL DEFAULT '',
                batch_index   INTEGER NOT NULL DEFAULT 0,
                chunk_ids     TEXT,
                total_chunks  INTEGER NOT NULL DEFAULT 0,
                model         TEXT,
                attempt       INTEGER NOT NULL DEFAULT 1,
                max_attempts  INTEGER NOT NULL DEFAULT 1,
                latency_ms    INTEGER,
                tokens_in     INTEGER,
                tokens_out    INTEGER,
                status        TEXT,
                error_detail  TEXT,
                prompt        TEXT,
                response      TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            );
            """,
            f"CREATE INDEX IF NOT EXISTS idx_llmlog_run ON {TABLE_LLM_LOGS}(run_id, id);",
            f"CREATE INDEX IF NOT EXISTS idx_llmlog_book ON {TABLE_LLM_LOGS}(book_id, id);",
        ]),
    ]

    def __init__(self, db_path):
        super().__init__(db_path)
        self.initialize()

    # ── Write: nodes ──────────────────────────────────────────────────────

    def upsert_node(self, book_id: str, name: str, kind: str,
                    description: str | None = None,
                    first_chunk_id: str | None = None,
                    personality: str | None = None,
                    appearance: str | None = None,
                    motivation: str | None = None,
                    role_in_story: str | None = None,
                    speaking_style: str | None = None) -> str:
        """Insert or update node. Returns node_id.

        Resolution order:
        1. If name matches a registered alias → update the canonical node
        2. If name matches an existing canonical_name+kind → update it
        3. Otherwise → insert new node
        """
        conn = self.get_conn()

        # Step 1: check aliases — if name is someone's alias, use canonical node
        alias_row = conn.execute(
            f"SELECT a.node_id FROM {TABLE_ALIASES} a "
            f"JOIN {TABLE_NODES} n ON n.id = a.node_id "
            "WHERE a.project_id=? AND a.alias_name=? AND n.project_id=? AND n.kind=? LIMIT 1",
            (book_id, name, book_id, kind),
        ).fetchone()
        if alias_row:
            existing_id = alias_row["node_id"]
            updates: list[str] = []
            params: list[str] = []
            for col, val in [
                ("description", description),
                ("personality", personality),
                ("appearance", appearance),
                ("motivation", motivation),
                ("role_in_story", role_in_story),
                ("speaking_style", speaking_style),
            ]:
                if val is not None:
                    updates.append(f"{col}=?")
                    params.append(val)
            if updates:
                params.append(existing_id)
                conn.execute(
                    f"UPDATE {TABLE_NODES} SET {', '.join(updates)} WHERE id=?",
                    params,
                )
            return existing_id

        # Step 2: canonical_name + kind match
        existing = conn.execute(
            f"SELECT id FROM {TABLE_NODES} "
            "WHERE project_id=? AND canonical_name=? AND kind=?",
            (book_id, name, kind),
        ).fetchone()
        if existing:
            # Update non-None fields
            updates: list[str] = []
            params: list[str] = []
            for col, val in [
                ("description", description),
                ("personality", personality),
                ("appearance", appearance),
                ("motivation", motivation),
                ("role_in_story", role_in_story),
                ("speaking_style", speaking_style),
            ]:
                if val is not None:
                    updates.append(f"{col}=?")
                    params.append(val)
            if updates:
                params.append(existing["id"])
                conn.execute(
                    f"UPDATE {TABLE_NODES} SET {', '.join(updates)} WHERE id=?",
                    params,
                )
            return existing["id"]
        node_id = f"node-{uuid.uuid4().hex[:12]}"
        conn.execute(
            f"INSERT INTO {TABLE_NODES}(id, canonical_name, kind, description, "
            "personality, appearance, motivation, role_in_story, speaking_style, "
            "first_appearance_chunk_id, project_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (node_id, name, kind, description,
             personality, appearance, motivation, role_in_story, speaking_style,
             first_chunk_id, book_id),
        )
        return node_id

    def init_chapter_nodes(self, book_id: str,
                           chapters: list[tuple[int, str]]) -> dict[int, str]:
        """Create chapter nodes. Returns {chapter_index: node_id}.

        Args:
            chapters: list of (chapter_index, chapter_title)
        """
        conn = self.get_conn()
        seen: dict[int, str] = {}
        for ch_idx, title in chapters:
            if ch_idx in seen:
                continue
            node_id = f"chap-{uuid.uuid4().hex[:12]}"
            conn.execute(
                f"INSERT OR IGNORE INTO {TABLE_NODES}"
                "(id, canonical_name, kind, project_id) VALUES(?,?,?,?)",
                (node_id, title, "chapter", book_id),
            )
            # Re-query to get the actual id (in case of IGNORE hit)
            row = conn.execute(
                f"SELECT id FROM {TABLE_NODES} "
                "WHERE project_id=? AND canonical_name=? AND kind=?",
                (book_id, title, "chapter"),
            ).fetchone()
            seen[ch_idx] = row["id"] if row else node_id
        self.get_conn().commit()
        return seen

    # ── Write: edges / aliases ────────────────────────────────────────────

    def add_edge(self, book_id: str, source_id: str, target_id: str,
                 kind: str, chunk_id: str | None = None,
                 event_type: str = "fact", subject: str | None = None,
                 tags: list[str] | None = None) -> bool:
        """Insert edge. Returns True if inserted, False if duplicate.

        event_type: fact / perspective / opinion (default fact).
        subject: whose perspective for non-fact edges (e.g. "LLM提取").
        tags: open folksonomy tags (agent-registered themes).
        """
        conn = self.get_conn()
        try:
            conn.execute(
                f"INSERT INTO {TABLE_EDGES}"
                "(source_id, target_id, kind, chunk_id, event_type, subject, tags, project_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (source_id, target_id, kind, chunk_id, event_type, subject,
                 json.dumps(tags or [], ensure_ascii=False), book_id),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def add_alias(self, book_id: str, alias: str, node_id: str) -> bool:
        conn = self.get_conn()
        try:
            conn.execute(
                f"INSERT INTO {TABLE_ALIASES}"
                "(alias_name, node_id, project_id) VALUES(?,?,?)",
                (alias, node_id, book_id),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def add_quote(
        self,
        book_id: str,
        character_name: str,
        quote_text: str,
        scene_context: str = "",
        chunk_id: str = "",
        batch_id: str = "",
        related_nodes: list[str] | None = None,
    ) -> bool:
        """Insert a character quote. Idempotent on (project_id, chunk_id, quote_text)."""
        import uuid as _uuid
        conn = self.get_conn()
        try:
            conn.execute(
                f"INSERT INTO {TABLE_QUOTES}"
                "(id, character_name, quote_text, scene_context, chunk_id, "
                "batch_id, related_nodes, project_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"quote-{_uuid.uuid4().hex[:12]}",
                    character_name,
                    quote_text,
                    scene_context,
                    chunk_id,
                    batch_id,
                    json.dumps(related_nodes or [], ensure_ascii=False),
                    book_id,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def add_merge_candidate(self, book_id: str, name_a: str, name_b: str,
                            confidence: float, reason: str,
                            node_id_a: str | None = None):
        conn = self.get_conn()
        conn.execute(
            f"INSERT INTO {TABLE_MERGE_CANDIDATES}"
            "(node_id_a, name_a, name_b, confidence, reason, project_id) "
            "VALUES(?,?,?,?,?,?)",
            (node_id_a, name_a, name_b, confidence, reason, book_id),
        )

    # ── Write: observations (event model) ──────────────────────────────────

    def add_observation(
        self,
        book_id: str,
        node_id: str,
        field: str,
        value: str,
        event_type: str = "perspective",
        subject: str | None = None,
        anchor_chapter: int | None = None,
        evidence: str | None = None,
        tags: list[str] | None = None,
        source: str = "book",
        trust_score: float | None = None,
    ) -> bool:
        """Insert an observation event for a node.

        Observations replace the old structured-field columns
        (personality/motivation/...) with an append-only event stream.
        Each observation is a (field, value) tied to an anchor chapter
        with an event_type (fact/perspective/opinion) and evidence.

        No dedup - every observation is appended. The consumer decides
        how to aggregate (latest / most-frequent / full evolution).

        source: 'book' (Scout/Explorer from原文) or 'dialogue' (从对话推断).
        trust_score: None uses DB default (0.5). Dialogue-sourced observations
        should pass a lower value (e.g. 0.3).
        """
        conn = self.get_conn()
        obs_id = f"obs-{uuid.uuid4().hex[:12]}"
        actual_trust = trust_score if trust_score is not None else 0.5
        try:
            conn.execute(
                f"INSERT INTO {TABLE_OBSERVATIONS}"
                "(id, project_id, node_id, field, value, event_type, "
                "subject, anchor_chapter, evidence, tags, source, trust_score, "
                "updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (obs_id, book_id, node_id, field, value, event_type,
                 subject, anchor_chapter, evidence,
                 json.dumps(tags or [], ensure_ascii=False), source, actual_trust),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def record_observation_feedback(
        self,
        observation_id: str,
        helpful: bool,
    ) -> dict:
        """Record feedback for an observation, adjusting trust_score.

        Incremental trust model (adapted from Holographic memory):
        - helpful=True  -> helpful_count += 1, trust += 0.05 (max 1.0)
        - helpful=False -> trust -= 0.05 (min 0.0)
        - retrieval_count += 1 on every feedback call
        - updated_at refreshed to now

        Returns the updated {trust_score, helpful_count, retrieval_count}.
        """
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT trust_score, retrieval_count, helpful_count "
            f"FROM {TABLE_OBSERVATIONS} WHERE id=?",
            (observation_id,),
        ).fetchone()
        if row is None:
            return {"error": f"observation not found: {observation_id}"}

        trust = float(row["trust_score"])
        retrieval = int(row["retrieval_count"])
        helpful_count = int(row["helpful_count"])

        if helpful:
            helpful_count += 1
            trust = min(1.0, trust + 0.05)
        else:
            trust = max(0.0, trust - 0.05)
        retrieval += 1

        conn.execute(
            f"UPDATE {TABLE_OBSERVATIONS} "
            "SET trust_score=?, helpful_count=?, retrieval_count=?, "
            "updated_at=datetime('now') WHERE id=?",
            (trust, helpful_count, retrieval, observation_id),
        )
        conn.commit()
        return {
            "trust_score": trust,
            "helpful_count": helpful_count,
            "retrieval_count": retrieval,
        }

    def register_tag(self, book_id: str, name: str, parent_type: str) -> str:
        """Register or reuse a folksonomy tag. Returns tag_id.

        If the tag name exists for this project, increments usage_count
        and returns existing id. Otherwise creates a new tag.
        parent_type must be one of: fact / perspective / opinion.
        """
        conn = self.get_conn()
        existing = conn.execute(
            f"SELECT id FROM {TABLE_TAG_REGISTRY} "
            "WHERE project_id=? AND name=?",
            (book_id, name),
        ).fetchone()
        if existing:
            conn.execute(
                f"UPDATE {TABLE_TAG_REGISTRY} SET usage_count=usage_count+1 "
                "WHERE id=?",
                (existing["id"],),
            )
            return existing["id"]
        tag_id = f"tag-{uuid.uuid4().hex[:12]}"
        conn.execute(
            f"INSERT INTO {TABLE_TAG_REGISTRY}"
            "(id, project_id, name, parent_type, usage_count) "
            "VALUES(?,?,?,?,0)",
            (tag_id, book_id, name, parent_type),
        )
        return tag_id

    # ── Merge execution ────────────────────────────────────────────────────

    def merge_nodes(self, book_id: str, keep_id: str, remove_id: str) -> dict:
        """Merge remove_id into keep_id atomically.

        Returns {edges_moved, aliases_moved, removed_name, kept_name}.
        """
        conn = self.get_conn()
        keep_row = conn.execute(
            f"SELECT canonical_name FROM {TABLE_NODES} WHERE id=? AND project_id=?",
            (keep_id, book_id),
        ).fetchone()
        remove_row = conn.execute(
            f"SELECT canonical_name FROM {TABLE_NODES} WHERE id=? AND project_id=?",
            (remove_id, book_id),
        ).fetchone()
        if not keep_row or not remove_row:
            raise ValueError(f"Node not found: keep={keep_row}, remove={remove_row}")
        kept_name = keep_row["canonical_name"]
        removed_name = remove_row["canonical_name"]
        edges_moved = 0
        aliases_moved = 0

        try:
            conn.execute("BEGIN")
            # 1. Move edges: re-point source/target from remove to keep
            for col in ("source_id", "target_id"):
                other_col = "target_id" if col == "source_id" else "source_id"
                rows = conn.execute(
                    f"SELECT id, {other_col}, kind FROM {TABLE_EDGES} "
                    f"WHERE {col}=? AND project_id=?",
                    (remove_id, book_id),
                ).fetchall()
                for er in rows:
                    dup = conn.execute(
                        f"SELECT id FROM {TABLE_EDGES} "
                        f"WHERE {col}=? AND {other_col}=? AND kind=? AND project_id=? AND id!=?",
                        (keep_id, er[other_col], er["kind"], book_id, er["id"]),
                    ).fetchone()
                    if dup:
                        conn.execute(f"DELETE FROM {TABLE_EDGES} WHERE id=?", (er["id"],))
                    else:
                        conn.execute(f"UPDATE {TABLE_EDGES} SET {col}=? WHERE id=?", (keep_id, er["id"]))
                        edges_moved += 1

            # 2. Add remove's canonical_name as alias of keep
            conn.execute(
                f"INSERT OR IGNORE INTO {TABLE_ALIASES} (alias_name, node_id, project_id) VALUES(?,?,?)",
                (removed_name, keep_id, book_id),
            )
            if conn.total_changes > 0:
                aliases_moved += 1

            # 3. Move all aliases from remove to keep
            alias_rows = conn.execute(
                f"SELECT alias_name FROM {TABLE_ALIASES} WHERE node_id=? AND project_id=?",
                (remove_id, book_id),
            ).fetchall()
            for ar in alias_rows:
                conn.execute(
                    f"UPDATE OR IGNORE {TABLE_ALIASES} SET node_id=? "
                    "WHERE alias_name=? AND project_id=? AND node_id=?",
                    (keep_id, ar["alias_name"], book_id, remove_id),
                )
                if conn.total_changes > 0:
                    aliases_moved += 1

            # 4. Delete remove node
            conn.execute(f"DELETE FROM {TABLE_NODES} WHERE id=? AND project_id=?", (remove_id, book_id))

            # 5. Mark merge_candidates
            conn.execute(
                f"UPDATE {TABLE_MERGE_CANDIDATES} SET status='applied' "
                "WHERE project_id=? AND status='pending' AND (name_a=? OR name_b=?)",
                (book_id, removed_name, removed_name),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return {"edges_moved": edges_moved, "aliases_moved": aliases_moved,
                "removed_name": removed_name, "kept_name": kept_name}

    def auto_merge(self, book_id: str, min_confidence: float = 0.9) -> int:
        """Auto-merge high-confidence candidates. Returns merge count."""
        conn = self.get_conn()
        candidates = conn.execute(
            f"SELECT id, name_a, name_b, confidence FROM {TABLE_MERGE_CANDIDATES} "
            "WHERE project_id=? AND status='pending' AND confidence >= ? ORDER BY confidence DESC",
            (book_id, min_confidence),
        ).fetchall()
        merged = 0
        for c in candidates:
            id_a = self.name_to_node_id(book_id, c["name_a"])
            id_b = self.name_to_node_id(book_id, c["name_b"])
            if not id_a or not id_b or id_a == id_b:
                conn.execute(f"UPDATE {TABLE_MERGE_CANDIDATES} SET status='skipped' WHERE id=?", (c["id"],))
                continue
            try:
                self.merge_nodes(book_id, id_a, id_b)
                merged += 1
                conn.execute(f"UPDATE {TABLE_MERGE_CANDIDATES} SET status='applied' WHERE id=?", (c["id"],))
            except Exception:
                conn.execute(f"UPDATE {TABLE_MERGE_CANDIDATES} SET status='failed' WHERE id=?", (c["id"],))
        return merged

    def get_merge_candidates(self, book_id: str, status: str | None = None,
                             limit: int = 100) -> list[dict]:
        """List merge candidates with resolved node IDs."""
        conn = self.get_conn()
        if status:
            rows = conn.execute(
                f"SELECT * FROM {TABLE_MERGE_CANDIDATES} "
                "WHERE project_id=? AND status=? ORDER BY confidence DESC LIMIT ?",
                (book_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {TABLE_MERGE_CANDIDATES} "
                "WHERE project_id=? ORDER BY confidence DESC LIMIT ?",
                (book_id, limit),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["node_id_a"] = self.name_to_node_id(book_id, d["name_a"])
            d["node_id_b"] = self.name_to_node_id(book_id, d["name_b"])
            result.append(d)
        return result

    # ── Write: chunk status ───────────────────────────────────────────────

    def register_chunk(self, chunk_id: str, book_id: str,
                       chapter_index: int, chunk_index: int,
                       content_hash: str):
        """Idempotent: insert if not exists, preserve status on conflict."""
        conn = self.get_conn()
        conn.execute(
            f"INSERT OR IGNORE INTO {TABLE_GRAPH_CHUNKS}"
            "(chunk_id, book_id, chapter_index, chunk_index, "
            "content_hash, extract_status) VALUES(?,?,?,?,?, 'pending')",
            (chunk_id, book_id, chapter_index, chunk_index, content_hash),
        )

    def mark_chunk_done(self, chunk_id: str, node_count: int):
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_GRAPH_CHUNKS} SET "
            "extract_status='done', extract_count=extract_count+1, "
            "last_extract_at=datetime('now'), last_node_count=? "
            "WHERE chunk_id=?",
            (node_count, chunk_id),
        )

    def mark_chunk_failed(self, chunk_id: str, error_reason: str | None = None):
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_GRAPH_CHUNKS} SET extract_status='failed' "
            "WHERE chunk_id=?",
            (chunk_id,),
        )
        if error_reason:
            conn.execute(
                f"INSERT INTO {TABLE_META}(key, value, updated_at) "
                "VALUES(?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=datetime('now')",
                (f"failed_reason_{chunk_id}", error_reason),
            )

    def mark_chunk_excluded(self, chunk_id: str, reason: str | None = None):
        """Mark a chunk as excluded from extraction (e.g. foreign language,
        appendix, index — detected by Scout content filtering)."""
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_GRAPH_CHUNKS} SET extract_status='excluded' "
            "WHERE chunk_id=?",
            (chunk_id,),
        )
        if reason:
            conn.execute(
                f"INSERT INTO {TABLE_META}(key, value, updated_at) "
                "VALUES(?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=datetime('now')",
                (f"excluded_reason_{chunk_id}", reason),
            )

    def reset_failed_chunks(self, book_id: str) -> int:
        """Reset failed chunks to pending. Returns count reset."""
        conn = self.get_conn()
        cur = conn.execute(
            f"UPDATE {TABLE_GRAPH_CHUNKS} SET extract_status='pending' "
            "WHERE book_id=? AND extract_status='failed'",
            (book_id,),
        )
        return cur.rowcount

    def get_failed_chunk_reasons(self, book_id: str) -> list[dict]:
        """Return failed chunks with persisted error reasons, ordered by chapter.

        LEFT JOINs graph_meta (failed_reason_{chunk_id}) so chunks whose
        failure predates the reason feature come back with error_reason=None.
        """
        conn = self.get_conn()
        rows = conn.execute(
            f"SELECT gc.chunk_id, gc.chapter_index, gc.chunk_index, "
            f"m.value AS error_reason "
            f"FROM {TABLE_GRAPH_CHUNKS} gc "
            f"LEFT JOIN {TABLE_META} m ON m.key = 'failed_reason_' || gc.chunk_id "
            "WHERE gc.book_id=? AND gc.extract_status='failed' "
            "ORDER BY gc.chapter_index, gc.chunk_index",
            (book_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Write: progress ───────────────────────────────────────────────────

    def record_progress(self, book_id: str, batch_id: str,
                        step_index: int, step_label: str | None,
                        status: str, detail: str | None = None,
                        run_id: str = ""):
        conn = self.get_conn()
        conn.execute(
            f"INSERT INTO {TABLE_PROGRESS}"
            "(book_id, batch_id, step_index, step_label, status, detail, run_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (book_id, batch_id, step_index, step_label, status, detail, run_id),
        )
        conn.commit()  # commit immediately so SSE pollers see it

    def record_event(self, book_id: str, event_type: str,
                     step_label: str | None = None,
                     detail: str | None = None,
                     run_id: str = ""):
        """Record a phase-level event (context_set, scout_done, etc.) into
        graph_progress so late-joining SSE connections can replay via DB.

        Uses ``batch_id`` to store the event_type since it's TEXT.
        ``step_index`` is set to 0 (not applicable for phase events).
        """
        conn = self.get_conn()
        conn.execute(
            f"INSERT INTO {TABLE_PROGRESS}"
            "(book_id, batch_id, step_index, step_label, status, detail, run_id) "
            "VALUES(?,?,0,?,'done',?,?)",
            (book_id, event_type, step_label, detail, run_id),
        )
        conn.commit()

    # ── Write: LLM call logs ──────────────────────────────────────────────

    def record_llm_call(
        self, book_id: str, run_id: str, phase: str,
        batch_id: str = "", batch_index: int = 0,
        chunk_ids: list[str] | None = None, total_chunks: int = 0,
        model: str = "", attempt: int = 1, max_attempts: int = 1,
        latency_ms: int | None = None, tokens_in: int | None = None,
        tokens_out: int | None = None, status: str = "ok",
        error_detail: str | None = None,
        prompt: str = "", response: str = "",
    ) -> int:
        """Persist one LLM call (full prompt/response) for audit/monitoring.

        Returns the new row id, or -1 on failure (logging must never break
        extraction).
        """
        try:
            conn = self.get_conn()
            cur = conn.execute(
                f"INSERT INTO {TABLE_LLM_LOGS}"
                "(run_id, book_id, phase, batch_id, batch_index, chunk_ids, "
                " total_chunks, model, attempt, max_attempts, latency_ms, "
                " tokens_in, tokens_out, status, error_detail, prompt, response) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, book_id, phase, batch_id, batch_index,
                 json.dumps(chunk_ids, ensure_ascii=False) if chunk_ids else None,
                 total_chunks, model, attempt, max_attempts, latency_ms,
                 tokens_in, tokens_out, status, error_detail, prompt, response),
            )
            conn.commit()
            return int(cur.lastrowid)
        except Exception:
            log.exception("record_llm_call failed")
            return -1

    # ── Read: runs / LLM logs ─────────────────────────────────────────────

    def get_latest_run(self, book_id: str) -> tuple[str, int] | None:
        """Return (run_id, run_start_row_id) of the most recent run_start
        event, or None if this book has never had a run_start row."""
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT run_id, id FROM {TABLE_PROGRESS} "
            "WHERE book_id=? AND batch_id='run_start' "
            "ORDER BY id DESC LIMIT 1",
            (book_id,),
        ).fetchone()
        if row is None or not row["run_id"]:
            return None
        return row["run_id"], int(row["id"])

    def list_llm_runs(self) -> list[dict]:
        """Aggregate LLM call logs grouped by run_id, newest first."""
        conn = self.get_conn()
        rows = conn.execute(
            f"SELECT run_id, book_id, "
            "  MIN(created_at) AS first_at, MAX(created_at) AS last_at, "
            "  COUNT(*) AS call_count, "
            "  SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_count, "
            "  SUM(CASE WHEN status='ok' THEN 0 ELSE 1 END) AS fail_count, "
            "  GROUP_CONCAT(DISTINCT phase) AS phases "
            f"FROM {TABLE_LLM_LOGS} "
            "WHERE run_id != '' "
            "GROUP BY run_id "
            "ORDER BY last_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["ok_count"] = d["ok_count"] or 0
            d["fail_count"] = d["fail_count"] or 0
            d["phases"] = (d["phases"] or "").split(",")
            out.append(d)
        return out

    def list_llm_calls(self, run_id: str, phase: str | None = None,
                       limit: int = 200) -> list[dict]:
        """List calls for a run (metadata only — no full prompt/response)."""
        conn = self.get_conn()
        sql = (
            f"SELECT id, run_id, book_id, phase, batch_id, batch_index, "
            "  total_chunks, model, attempt, max_attempts, latency_ms, "
            "  tokens_in, tokens_out, status, error_detail, created_at "
            f"FROM {TABLE_LLM_LOGS} WHERE run_id=? "
        )
        params: list[Any] = [run_id]
        if phase:
            sql += " AND phase=?"
            params.append(phase)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_llm_call(self, call_id: int) -> dict | None:
        """Full LLM call row (incl. prompt/response)."""
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT * FROM {TABLE_LLM_LOGS} WHERE id=?",
            (call_id,),
        ).fetchone()
        return dict(row) if row else None

    def delete_llm_run(self, run_id: str) -> int:
        """Delete all LLM call logs for a run. Returns deleted row count."""
        conn = self.get_conn()
        cur = conn.execute(
            f"DELETE FROM {TABLE_LLM_LOGS} WHERE run_id=?",
            (run_id,),
        )
        conn.commit()
        return int(cur.rowcount)

    # ── Write: meta ───────────────────────────────────────────────────────

    def set_meta(self, key: str, value: str):
        conn = self.get_conn()
        conn.execute(
            f"INSERT INTO {TABLE_META}(key, value, updated_at) "
            "VALUES(?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=datetime('now')",
            (key, value),
        )
        conn.commit()

    # ── Read: chunk status ────────────────────────────────────────────────

    def done_count(self, book_id: str) -> int:
        conn = self.get_conn()
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_GRAPH_CHUNKS} "
            "WHERE book_id=? AND extract_status='done'",
            (book_id,),
        ).fetchone()["c"]

    def pending_count(self, book_id: str) -> int:
        conn = self.get_conn()
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_GRAPH_CHUNKS} "
            "WHERE book_id=? AND extract_status='pending'",
            (book_id,),
        ).fetchone()["c"]

    def failed_count(self, book_id: str) -> int:
        conn = self.get_conn()
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_GRAPH_CHUNKS} "
            "WHERE book_id=? AND extract_status='failed'",
            (book_id,),
        ).fetchone()["c"]

    def excluded_count(self, book_id: str) -> int:
        conn = self.get_conn()
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_GRAPH_CHUNKS} "
            "WHERE book_id=? AND extract_status='excluded'",
            (book_id,),
        ).fetchone()["c"]

    def total_chunks(self, book_id: str) -> int:
        conn = self.get_conn()
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_GRAPH_CHUNKS} WHERE book_id=?",
            (book_id,),
        ).fetchone()["c"]

    def is_chunk_done(self, chunk_id: str) -> bool:
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT extract_status FROM {TABLE_GRAPH_CHUNKS} WHERE chunk_id=?",
            (chunk_id,),
        ).fetchone()
        return row is not None and row["extract_status"] in ("done", "excluded")

    def get_pending_chunk_ids(self, book_id: str, limit: int = 0) -> list[str]:
        conn = self.get_conn()
        sql = (
            f"SELECT chunk_id FROM {TABLE_GRAPH_CHUNKS} "
            "WHERE book_id=? AND extract_status='pending' "
            "ORDER BY chapter_index, chunk_index"
        )
        params: list[Any] = [book_id]
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        return [r["chunk_id"] for r in conn.execute(sql, params).fetchall()]

    # ── Read: nodes ───────────────────────────────────────────────────────

    def total_node_count(self, book_id: str) -> int:
        conn = self.get_conn()
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_NODES} WHERE project_id=?",
            (book_id,),
        ).fetchone()["c"]

    def name_to_node_id(self, book_id: str, name: str) -> str | None:
        """Resolve name to node_id. Checks aliases first, then canonical_name."""
        conn = self.get_conn()
        # First: check if name is a registered alias → return canonical node
        row = conn.execute(
            f"SELECT a.node_id FROM {TABLE_ALIASES} a "
            f"JOIN {TABLE_NODES} n ON n.id = a.node_id "
            "WHERE a.project_id=? AND a.alias_name=? AND n.project_id=? LIMIT 1",
            (book_id, name, book_id),
        ).fetchone()
        if row:
            return row["node_id"]
        # Fallback: canonical_name match
        row = conn.execute(
            f"SELECT id FROM {TABLE_NODES} "
            "WHERE project_id=? AND canonical_name=? LIMIT 1",
            (book_id, name),
        ).fetchone()
        return row["id"] if row else None

    def get_node(self, book_id: str, node_id: str) -> dict | None:
        """Full node detail + direct edges (trail)."""
        conn = self.get_conn()
        node = conn.execute(
            f"SELECT id, canonical_name, kind, description FROM {TABLE_NODES} "
            "WHERE id=? AND project_id=?",
            (node_id, book_id),
        ).fetchone()
        if node is None:
            return None
        aliases = [r["alias_name"] for r in conn.execute(
            f"SELECT alias_name FROM {TABLE_ALIASES} WHERE node_id=?",
            (node_id,),
        ).fetchall()]
        out_edges = conn.execute(
            f"SELECT e.kind, n.canonical_name AS other_name, n.kind AS other_kind, "
            f"e.chunk_id FROM {TABLE_EDGES} e "
            f"JOIN {TABLE_NODES} n ON n.id=e.target_id "
            "WHERE e.source_id=? AND e.project_id=?",
            (node_id, book_id),
        ).fetchall()
        in_edges = conn.execute(
            f"SELECT e.kind, n.canonical_name AS other_name, n.kind AS other_kind, "
            f"e.chunk_id FROM {TABLE_EDGES} e "
            f"JOIN {TABLE_NODES} n ON n.id=e.source_id "
            "WHERE e.target_id=? AND e.project_id=?",
            (node_id, book_id),
        ).fetchall()
        edges = []
        for r in out_edges:
            edges.append({"kind": r["kind"], "direction": "out",
                          "other_node_name": r["other_name"],
                          "other_node_kind": r["other_kind"],
                          "chunk_id": r["chunk_id"]})
        for r in in_edges:
            edges.append({"kind": r["kind"], "direction": "in",
                          "other_node_name": r["other_name"],
                          "other_node_kind": r["other_kind"],
                          "chunk_id": r["chunk_id"]})
        return {
            "node_id": node["id"],
            "canonical_name": node["canonical_name"],
            "kind": node["kind"],
            "description": node["description"],
            "aliases": aliases,
            "edges": edges,
        }

    def search_nodes(self, book_id: str, query: str,
                     kind: str | None = None,
                     chapter_to: int | None = None,
                     limit: int = 5) -> list[dict]:
        """FTS5 prefix match → LIKE substring fallback."""
        conn = self.get_conn()
        # Try FTS5 first, fall back to LIKE on error (e.g. content= mismatch)
        try:
            sql = (
                f"SELECT n.id, n.canonical_name, n.kind, n.description, "
                f"gc.chapter_index AS first_chapter "
                f"FROM nodes_fts fts "
                f"JOIN {TABLE_NODES} n ON n.rowid=fts.rowid "
                f"LEFT JOIN {TABLE_GRAPH_CHUNKS} gc "
                f"  ON gc.chunk_id=n.first_appearance_chunk_id "
                "WHERE n.project_id=? "
                "  AND (nodes_fts MATCH ? OR n.canonical_name LIKE ?) "
            )
            params: list[Any] = [book_id, f"{query}*", f"%{query}%"]
            if kind:
                sql += " AND n.kind=?"
                params.append(kind)
            if chapter_to is not None:
                sql += " AND (gc.chapter_index IS NULL OR gc.chapter_index <= ?)"
                params.append(chapter_to)
            sql += " LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            # FTS content table mismatch — fall back to LIKE-only search
            sql = (
                f"SELECT n.id, n.canonical_name, n.kind, n.description, "
                f"gc.chapter_index AS first_chapter "
                f"FROM {TABLE_NODES} n "
                f"LEFT JOIN {TABLE_GRAPH_CHUNKS} gc "
                f"  ON gc.chunk_id=n.first_appearance_chunk_id "
                "WHERE n.project_id=? "
                "  AND n.canonical_name LIKE ? "
            )
            params = [book_id, f"%{query}%"]
            if kind:
                sql += " AND n.kind=?"
                params.append(kind)
            if chapter_to is not None:
                sql += " AND (gc.chapter_index IS NULL OR gc.chapter_index <= ?)"
                params.append(chapter_to)
            sql += " ORDER BY n.canonical_name LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        return [{
            "node_id": r["id"],
            "canonical_name": r["canonical_name"],
            "kind": r["kind"],
            "first_appearance_chapter": r["first_chapter"],
            "description": r["description"],
        } for r in rows]

    # ── Role-play tool queries ─────────────────────────────────────────────

    def search_all(self, book_id: str, query: str,
                   max_chapter: int | None = None,
                   limit: int = 10) -> list[dict]:
        """Cross-kind entity search with structured field enrichment.

        Searches across all node kinds (character, event, place, concept, …).
        Returns name, kind, description, first_chapter, plus kind-specific
        structured fields (personality, motivation, speaking_style for
        characters; appearance for characters/places).

        Uses FTS5 prefix-match with LIKE fallback (same pattern as
        ``search_nodes``).
        """
        conn = self.get_conn()
        try:
            sql = (
                "SELECT n.id, n.canonical_name, n.kind, n.description, "
                "gc.chapter_index AS first_chapter "
                "FROM nodes_fts fts "
                f"JOIN {TABLE_NODES} n ON n.rowid = fts.rowid "
                f"LEFT JOIN {TABLE_GRAPH_CHUNKS} gc "
                "  ON gc.chunk_id = n.first_appearance_chunk_id "
                "WHERE n.project_id = ? "
                "  AND (nodes_fts MATCH ? OR n.canonical_name LIKE ?) "
            )
            params: list[Any] = [book_id, f"{query}*", f"%{query}%"]
            if max_chapter is not None:
                sql += (
                    " AND (gc.chapter_index IS NULL "
                    "      OR gc.chapter_index <= ?)"
                )
                params.append(max_chapter)
            sql += " LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            sql = (
                "SELECT n.id, n.canonical_name, n.kind, n.description, "
                "gc.chapter_index AS first_chapter "
                f"FROM {TABLE_NODES} n "
                f"LEFT JOIN {TABLE_GRAPH_CHUNKS} gc "
                "  ON gc.chunk_id = n.first_appearance_chunk_id "
                "WHERE n.project_id = ? "
                "  AND n.canonical_name LIKE ? "
            )
            params = [book_id, f"%{query}%"]
            if max_chapter is not None:
                sql += (
                    " AND (gc.chapter_index IS NULL "
                    "      OR gc.chapter_index <= ?)"
                )
                params.append(max_chapter)
            sql += " ORDER BY n.canonical_name LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()

        results: list[dict] = []
        for r in rows:
            entry: dict = {
                "name": r["canonical_name"],
                "kind": r["kind"],
                "description": r["description"] or "",
                "first_appearance_chapter": r["first_chapter"],
            }
            results.append(entry)
        return results

    def get_quotes_by_character(self, book_id: str, character_name: str,
                                max_chapter: int | None = None,
                                limit: int = 20) -> list[dict]:
        """Return quotes spoken by or referencing a character.

        Filtered by *max_chapter* for spoiler-free recall.
        """
        conn = self.get_conn()
        sql = (
            f"SELECT quote_text, scene_context, chunk_id "
            f"FROM {TABLE_QUOTES} "
            "WHERE project_id = ? AND character_name LIKE ? "
        )
        params: list[Any] = [book_id, f"%{character_name}%"]
        if max_chapter is not None:
            sql += (
                "AND (chunk_id IS NULL OR chunk_id IN "
                f"(SELECT chunk_id FROM {TABLE_GRAPH_CHUNKS} "
                "WHERE chapter_index <= ?)) "
            )
            params.append(max_chapter)
        sql += "ORDER BY chunk_id LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [{
            "text": r["quote_text"],
            "context": r["scene_context"] or "",
            "chapter": self._chapter_of_chunk(r["chunk_id"]),
        } for r in rows]

    def list_entities(self, book_id: str, kind: str,
                      chapter_to: int | None = None,
                      limit: int = 50) -> list[dict]:
        conn = self.get_conn()
        sql = (
            f"SELECT n.id, n.canonical_name, "
            f"gc.chapter_index AS first_chapter, "
            f"(SELECT COUNT(*) FROM {TABLE_EDGES} e "
            f"  WHERE e.source_id=n.id OR e.target_id=n.id) AS app_count "
            f"FROM {TABLE_NODES} n "
            f"LEFT JOIN {TABLE_GRAPH_CHUNKS} gc "
            f"  ON gc.chunk_id=n.first_appearance_chunk_id "
            "WHERE n.project_id=? AND n.kind=?"
        )
        params: list[Any] = [book_id, kind]
        if chapter_to is not None:
            sql += " AND (gc.chapter_index IS NULL OR gc.chapter_index <= ?)"
            params.append(chapter_to)
        sql += " ORDER BY app_count DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [{
            "node_id": r["id"],
            "canonical_name": r["canonical_name"],
            "first_appearance_chapter": r["first_chapter"],
            "appearance_count": r["app_count"],
        } for r in rows]

    def get_aliases(self, book_id: str,
                    node_id: str | None = None,
                    name: str | None = None) -> dict:
        conn = self.get_conn()
        if node_id:
            node = conn.execute(
                f"SELECT id, canonical_name, kind FROM {TABLE_NODES} "
                "WHERE id=? AND project_id=?",
                (node_id, book_id),
            ).fetchone()
            if node is None:
                return {"node_id": None, "aliases": [], "is_existing_alias": False}
            aliases = [r["alias_name"] for r in conn.execute(
                f"SELECT alias_name FROM {TABLE_ALIASES} WHERE node_id=?",
                (node_id,),
            ).fetchall()]
            return {"node_id": node["id"],
                    "canonical_name": node["canonical_name"],
                    "kind": node["kind"],
                    "aliases": aliases, "is_existing_alias": False}
        if name:
            alias_row = conn.execute(
                f"SELECT a.node_id, n.canonical_name, n.kind "
                f"FROM {TABLE_ALIASES} a "
                f"JOIN {TABLE_NODES} n ON n.id=a.node_id "
                "WHERE a.alias_name=? AND a.project_id=?",
                (name, book_id),
            ).fetchone()
            if alias_row:
                aliases = [r["alias_name"] for r in conn.execute(
                    f"SELECT alias_name FROM {TABLE_ALIASES} WHERE node_id=?",
                    (alias_row["node_id"],),
                ).fetchall()]
                return {"node_id": alias_row["node_id"],
                        "canonical_name": alias_row["canonical_name"],
                        "kind": alias_row["kind"],
                        "aliases": aliases, "is_existing_alias": True}
            return {"node_id": None, "aliases": [], "is_existing_alias": False}
        return {"node_id": None, "aliases": [], "is_existing_alias": False}

    # ── Read: meta / progress ─────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT value FROM {TABLE_META} WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def get_recent_progress(self, book_id: str, limit: int = 50) -> list[dict]:
        """Recent progress events for SSE polling."""
        conn = self.get_conn()
        rows = conn.execute(
            f"SELECT id, batch_id, step_index, step_label, status, detail, "
            f"run_id, updated_at FROM {TABLE_PROGRESS} "
            "WHERE book_id=? ORDER BY id DESC LIMIT ?",
            (book_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_latest_progress_id(self, book_id: str) -> int:
        """Highest progress.id for this book (for change detection)."""
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT COALESCE(MAX(id), 0) AS m FROM {TABLE_PROGRESS} "
            "WHERE book_id=?",
            (book_id,),
        ).fetchone()
        return row["m"]

    # ── Read: stats ───────────────────────────────────────────────────────

    def get_stats(self, book_id: str) -> dict:
        conn = self.get_conn()
        node_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_NODES} WHERE project_id=?",
            (book_id,),
        ).fetchone()["c"]
        edge_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_EDGES} WHERE project_id=?",
            (book_id,),
        ).fetchone()["c"]
        alias_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_ALIASES} WHERE project_id=?",
            (book_id,),
        ).fetchone()["c"]
        merge_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_MERGE_CANDIDATES} "
            "WHERE project_id=? AND status='pending'",
            (book_id,),
        ).fetchone()["c"]
        kind_breakdown = {
            r["kind"]: r["c"] for r in conn.execute(
                f"SELECT kind, COUNT(*) AS c FROM {TABLE_NODES} "
                "WHERE project_id=? GROUP BY kind ORDER BY c DESC",
                (book_id,),
            ).fetchall()
        }
        return {
            "nodes": node_count,
            "edges": edge_count,
            "aliases": alias_count,
            "merge_candidates": merge_count,
            "kinds": kind_breakdown,
            "chunks_done": self.done_count(book_id),
            "chunks_total": self.total_chunks(book_id),
            "chunks_failed": self.failed_count(book_id),
            "chunks_excluded": self.excluded_count(book_id),
        }

    def get_nodes_by_kind(self, book_id: str, kind: str | None = None,
                          limit: int = 100) -> list[dict]:
        """List nodes, optionally filtered by kind.  Includes structured fields."""
        conn = self.get_conn()
        cols = ("id, canonical_name, kind, description, "
                "personality, appearance, motivation, role_in_story, speaking_style")
        if kind:
            sql = (
                f"SELECT {cols} FROM {TABLE_NODES} "
                "WHERE project_id=? AND kind=? ORDER BY canonical_name LIMIT ?"
            )
            params = [book_id, kind, limit]
        else:
            sql = (
                f"SELECT {cols} FROM {TABLE_NODES} "
                "WHERE project_id=? AND kind!='chapter' ORDER BY kind, canonical_name LIMIT ?"
            )
            params = [book_id, limit]
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_node_index_data(self, book_id: str,
                            chapter_indices: set[int] | None = None,
                            max_entries: int = 100) -> list[dict]:
        """Build compact node index for Explorer task messages.

        Selection priority:
        1. All character nodes (always)
        2. Entities first appearing in/near given chapter_indices
        3. Recently created entities
        4. Other non-chapter nodes (up to max_entries)

        Returns [{canonical_name, kind, aliases: [str]}], sorted by priority.
        """
        conn = self.get_conn()
        selected: dict[str, dict] = {}  # canonical_name -> {kind, aliases}

        # Priority 1: all characters
        char_rows = conn.execute(
            f"SELECT canonical_name, kind, id FROM {TABLE_NODES} "
            "WHERE project_id=? AND kind='character' ORDER BY canonical_name",
            (book_id,),
        ).fetchall()
        for r in char_rows:
            selected[r["canonical_name"]] = {"canonical_name": r["canonical_name"],
                                              "kind": r["kind"], "aliases": [], "_id": r["id"]}

        # Priority 2: entities near given chapters
        if chapter_indices:
            ci_list = list(chapter_indices)
            # Expand ±2 chapters
            expanded = set(ci_list)
            for ci in ci_list:
                expanded.add(ci - 2); expanded.add(ci - 1)
                expanded.add(ci + 1); expanded.add(ci + 2)
            if expanded:
                cid_placeholders = ",".join("?" * len(expanded))
                nearby_rows = conn.execute(
                    f"SELECT DISTINCT n.canonical_name, n.kind, n.id FROM {TABLE_NODES} n "
                    f"JOIN {TABLE_GRAPH_CHUNKS} gc ON n.first_appearance_chunk_id = gc.chunk_id "
                    f"WHERE n.project_id=? AND n.kind!='character' AND n.kind!='chapter' "
                    f"AND gc.chapter_index IN ({cid_placeholders}) "
                    "ORDER BY n.kind, n.canonical_name",
                    [book_id] + list(expanded),
                ).fetchall()
                for r in nearby_rows:
                    if r["canonical_name"] not in selected:
                        selected[r["canonical_name"]] = {
                            "canonical_name": r["canonical_name"],
                            "kind": r["kind"], "aliases": [], "_id": r["id"],
                        }

        # Priority 3: recently created (by rowid descending)
        recent_rows = conn.execute(
            f"SELECT canonical_name, kind, id FROM {TABLE_NODES} "
            "WHERE project_id=? AND kind!='chapter' AND kind!='character' "
            "ORDER BY rowid DESC LIMIT 30",
            (book_id,),
        ).fetchall()
        for r in recent_rows:
            if r["canonical_name"] not in selected:
                selected[r["canonical_name"]] = {
                    "canonical_name": r["canonical_name"],
                    "kind": r["kind"], "aliases": [], "_id": r["id"],
                }

        # Priority 4: fill up to max_entries with remaining non-chapter nodes
        if len(selected) < max_entries:
            remaining = conn.execute(
                f"SELECT canonical_name, kind, id FROM {TABLE_NODES} "
                "WHERE project_id=? AND kind!='chapter' AND kind!='character' "
                "ORDER BY kind, canonical_name "
                "LIMIT ?",
                (book_id, max_entries),
            ).fetchall()
            for r in remaining:
                if r["canonical_name"] not in selected and len(selected) < max_entries:
                    selected[r["canonical_name"]] = {
                        "canonical_name": r["canonical_name"],
                        "kind": r["kind"], "aliases": [], "_id": r["id"],
                    }

        # Batch-load aliases for all selected nodes
        all_ids = [v["_id"] for v in selected.values()]
        if all_ids:
            pid_placeholders = ",".join("?" * len(all_ids))
            alias_rows = conn.execute(
                f"SELECT node_id, alias_name FROM {TABLE_ALIASES} "
                f"WHERE node_id IN ({pid_placeholders}) AND project_id=?",
                all_ids + [book_id],
            ).fetchall()
            alias_map: dict[str, list[str]] = {}
            for ar in alias_rows:
                alias_map.setdefault(ar["node_id"], []).append(ar["alias_name"])
            for v in selected.values():
                v["aliases"] = alias_map.get(v["_id"], [])

        # Sort: character first → kind alpha → name alpha
        def _sort_key(d):
            return (0 if d["kind"] == "character" else 1, d["kind"], d["canonical_name"])

        result = sorted(selected.values(), key=_sort_key)
        # Strip internal _id
        for d in result:
            d.pop("_id", None)
        return result[:max_entries]

    def get_node_index_two_tier(self, book_id: str,
                                chapter_indices: set[int] | None = None,
                                compact_cap: int = 300,
                                detailed_cap: int = 24) -> dict:
        """Two-tier node index for Explorer task messages.

        Tier 1 "compact": ALL non-chapter entities as [canonical_name, kind],
        NO aliases — the LLM knows every entity exists without the prompt
        ballooning with alias strings.
        Tier 2 "detailed": top-relevance non-chapter entities WITH aliases;
        relevance = edge degree (in+out) + observation count, with characters
        whose first appearance is within ±2 chapters of this batch pinned to
        the front.

        Returns {"compact": [...], "detailed": [...]}.
        """
        conn = self.get_conn()

        # ── Tier 1: compact full list (name + kind only) ──────────────────
        compact = [
            {"canonical_name": r["canonical_name"], "kind": r["kind"]}
            for r in conn.execute(
                f"SELECT canonical_name, kind FROM {TABLE_NODES} "
                "WHERE project_id=? AND kind!='chapter' "
                "ORDER BY (kind='character') DESC, kind, canonical_name "
                "LIMIT ?",
                (book_id, compact_cap),
            ).fetchall()
        ]

        # ── Tier 2: detailed top-relevance with aliases ───────────────────
        nearby_chapters: list[int] = []
        if chapter_indices:
            nearby_set: set[int] = set()
            for ci in chapter_indices:
                nearby_set.update((ci - 2, ci - 1, ci, ci + 1, ci + 2))
            nearby_chapters = sorted(nearby_set)
        # Sentinel chapter_index (never valid) so the IN-list stays well-formed
        nearby_placeholder = ",".join("?" * len(nearby_chapters)) if nearby_chapters else "?"
        nearby_params = nearby_chapters if nearby_chapters else [-1]

        detailed_rows = conn.execute(
            f"SELECT n.id, n.canonical_name, n.kind, "
            f"(SELECT COUNT(*) FROM {TABLE_EDGES} e "
            f" WHERE (e.source_id = n.id OR e.target_id = n.id) "
            f" AND e.project_id = n.project_id) "
            f"+ (SELECT COUNT(*) FROM {TABLE_OBSERVATIONS} o WHERE o.node_id = n.id)"
            " AS relevance, "
            f"(EXISTS(SELECT 1 FROM {TABLE_GRAPH_CHUNKS} gc "
            f" WHERE gc.chunk_id = n.first_appearance_chunk_id "
            f" AND gc.book_id = ? AND gc.chapter_index IN ({nearby_placeholder})))"
            " AS nearby "
            f"FROM {TABLE_NODES} n "
            "WHERE n.project_id=? AND n.kind!='chapter' "
            "ORDER BY (n.kind='character' AND nearby) DESC, relevance DESC, "
            "n.kind, n.canonical_name "
            "LIMIT ?",
            [book_id] + nearby_params + [book_id, detailed_cap],
        ).fetchall()

        # Batch-load aliases for the detailed set
        all_ids = [r["id"] for r in detailed_rows]
        alias_map: dict[str, list[str]] = {}
        if all_ids:
            pid_ph = ",".join("?" * len(all_ids))
            for ar in conn.execute(
                f"SELECT node_id, alias_name FROM {TABLE_ALIASES} "
                f"WHERE node_id IN ({pid_ph}) AND project_id=?",
                all_ids + [book_id],
            ).fetchall():
                alias_map.setdefault(ar["node_id"], []).append(ar["alias_name"])

        detailed = [
            {"canonical_name": r["canonical_name"], "kind": r["kind"],
             "aliases": alias_map.get(r["id"], [])}
            for r in detailed_rows
        ]

        return {"compact": compact, "detailed": detailed}

    def get_full_graph(self, book_id: str, kinds: list[str] | None = None,
                       limit: int = 500) -> dict:
        """Return {nodes, edges} for full-graph visualization.

        Only returns edges where both endpoints are in the fetched node set,
        avoiding Cartesian explosion on the wire.
        """
        conn = self.get_conn()

        cols = ("id, canonical_name, kind, description, "
                "personality, appearance, motivation, role_in_story, speaking_style, "
                "first_appearance_chunk_id")

        if kinds:
            placeholders = ",".join("?" * len(kinds))
            node_rows = conn.execute(
                f"SELECT {cols} FROM {TABLE_NODES} "
                f"WHERE project_id=? AND kind IN ({placeholders}) "
                "ORDER BY kind, canonical_name LIMIT ?",
                [book_id] + kinds + [limit],
            ).fetchall()
        else:
            node_rows = conn.execute(
                f"SELECT {cols} FROM {TABLE_NODES} "
                "WHERE project_id=? AND kind!='chapter' "
                "ORDER BY kind, canonical_name LIMIT ?",
                [book_id, limit],
            ).fetchall()

        nodes_raw = [dict(r) for r in node_rows]
        all_chunk_ids: set[str] = set()
        for n in nodes_raw:
            cid = n.get("first_appearance_chunk_id")
            if cid:
                all_chunk_ids.add(cid)
        node_ids = {r["id"] for r in node_rows}
        edge_rows_raw: list[dict] = []
        if node_ids:
            id_list = list(node_ids)
            placeholders = ",".join("?" * len(id_list))
            edge_rows_raw = [
                dict(r) for r in conn.execute(
                    f"SELECT e.source_id, e.target_id, e.kind, e.chunk_id "
                    f"FROM {TABLE_EDGES} e "
                    f"WHERE e.project_id=? "
                    f"  AND e.source_id IN ({placeholders}) "
                    f"  AND e.target_id IN ({placeholders}) "
                    f"LIMIT ?",
                    [book_id] + id_list + id_list + [limit * 3],
                ).fetchall()
            ]
            for er in edge_rows_raw:
                cid = er.get("chunk_id")
                if cid:
                    all_chunk_ids.add(cid)

        # Resolve chunk_id -> chapter_index in one batch
        chunk_to_chapter: dict[str, int] = {}
        if all_chunk_ids:
            cid_list = list(all_chunk_ids)
            cid_placeholders = ",".join("?" * len(cid_list))
            chunk_rows = conn.execute(
                f"SELECT chunk_id, chapter_index FROM {TABLE_GRAPH_CHUNKS} "
                f"WHERE chunk_id IN ({cid_placeholders})",
                cid_list,
            ).fetchall()
            chunk_to_chapter = {r["chunk_id"]: r["chapter_index"] for r in chunk_rows}

        # Batch-load aliases for all fetched nodes
        aliases_by_node: dict[str, list[str]] = {}
        if node_ids:
            alias_rows = conn.execute(
                f"SELECT node_id, alias_name FROM {TABLE_ALIASES} WHERE project_id=?",
                (book_id,),
            ).fetchall()
            for ar in alias_rows:
                nid = ar["node_id"]
                if nid in node_ids:
                    aliases_by_node.setdefault(nid, []).append(ar["alias_name"])

        # Annotate nodes with chapter + aliases
        nodes = []
        for n in nodes_raw:
            cid = n.pop("first_appearance_chunk_id", None)
            n["chapter"] = chunk_to_chapter.get(cid) if cid else None
            n["aliases"] = aliases_by_node.get(n["id"], [])
            nodes.append(n)

        # Annotate edges with chapter, then merge by unordered pair
        edge_groups: dict[tuple[str, str], list[dict]] = {}
        for er in edge_rows_raw:
            cid = er.get("chunk_id")
            pair = tuple(sorted([er["source_id"], er["target_id"]]))
            edge_groups.setdefault(pair, []).append({
                "kind": er["kind"],
                "chapter": chunk_to_chapter.get(cid) if cid else None,
            })

        edges: list[dict] = []
        for (a, b), group in edge_groups.items():
            kinds = list(dict.fromkeys(g["kind"] for g in group if g["kind"]))
            chapters = sorted(set(g["chapter"] for g in group if g["chapter"] is not None))
            edges.append({
                "id": f"merged-{a[:8]}-{b[:8]}",
                "source_id": a,
                "target_id": b,
                "kind": " · ".join(kinds) if kinds else "",
                "kinds": kinds,
                "chapter": chapters[0] if chapters else None,
                "chapters": chapters,
                "edge_count": len(group),
            })

        return {"nodes": nodes, "edges": edges}

    # ── Character subgraph (2-hop graph traversal) ───────────────────────────

    def get_observations(
        self,
        book_id: str,
        node_id: str,
        max_chapter: int | None = None,
    ) -> tuple[list[dict], int]:
        """Fetch observations for a node, split by max_chapter.

        Returns (seen_observations, future_count). Each observation dict
        has: field, value, event_type, subject, chapter, evidence, tags.
        Seen observations are sorted by (field, chapter). Future items
        contribute only a count (spoiler-gated).
        """
        conn = self.get_conn()
        rows = conn.execute(
            f"SELECT field, value, event_type, subject, anchor_chapter, "
            f"evidence, tags, trust_score, updated_at, source FROM {TABLE_OBSERVATIONS} "
            "WHERE project_id=? AND node_id=? "
            "ORDER BY field, anchor_chapter",
            (book_id, node_id),
        ).fetchall()
        seen: list[dict] = []
        future_count = 0
        for r in rows:
            entry = {
                "field": r["field"],
                "value": r["value"],
                "event_type": r["event_type"],
                "subject": r["subject"],
                "chapter": r["anchor_chapter"],
                "evidence": r["evidence"],
                "tags": json.loads(r["tags"] or "[]"),
                "trust_score": r["trust_score"],
                "updated_at": r["updated_at"],
                "source": r["source"] or "book",
            }
            if max_chapter is None or r["anchor_chapter"] is None or r["anchor_chapter"] <= max_chapter:
                seen.append(entry)
            else:
                future_count += 1
        return seen, future_count

    def search_observations(
        self,
        book_id: str,
        query: str,
        limit: int = 5,
        max_chapter: int | None = None,
    ) -> list[dict]:
        """Search observations by FTS5 (trigram) or LIKE fallback.

        Used for hybrid prefetch - proactively inject relevant observations
        into context based on the user's query, even when the model doesn't
        explicitly call graph_search.

        Returns list of dicts: field, value, event_type, subject, chapter,
        trust_score, source. Ranked by FTS5 BM25 relevance.
        """
        conn = self.get_conn()
        query = query.strip()
        if not query:
            return []

        rows: list = []
        use_fts = len(query) >= 3
        if use_fts:
            try:
                rows = conn.execute(
                    "SELECT o.field, o.value, o.event_type, o.subject, "
                    "o.anchor_chapter, o.trust_score, o.source "
                    "FROM observations_fts fts "
                    f"JOIN {TABLE_OBSERVATIONS} o ON o.rowid = fts.rowid "
                    "WHERE observations_fts MATCH ? AND o.project_id=? "
                    "ORDER BY bm25(observations_fts) LIMIT ?",
                    (query, book_id, limit * 3),
                ).fetchall()
            except Exception:
                rows = []
            if not rows:
                use_fts = False

        if not use_fts:
            # LIKE fallback for short queries or FTS5 misses
            like_pattern = f"%{query}%"
            rows = conn.execute(
                f"SELECT field, value, event_type, subject, anchor_chapter, "
                f"trust_score, source FROM {TABLE_OBSERVATIONS} "
                "WHERE project_id=? AND value LIKE ? LIMIT ?",
                (book_id, like_pattern, limit * 3),
            ).fetchall()

        # Filter by max_chapter (spoiler gating) and collect
        results: list[dict] = []
        for r in rows:
            chapter = r["anchor_chapter"]
            if max_chapter is not None and chapter is not None and chapter > max_chapter:
                continue
            results.append({
                "field": r["field"],
                "value": r["value"],
                "event_type": r["event_type"],
                "subject": r["subject"],
                "chapter": chapter,
                "trust_score": float(r["trust_score"] or 0.5),
                "source": r["source"] or "book",
            })
            if len(results) >= limit:
                break
        return results

    def get_node_detail(
        self,
        book_id: str,
        name: str,
        max_chapter: int | None = None,
    ) -> dict:
        """Fetch a single node's full detail for LLM consumption.

        Locates the node by substring match on ``canonical_name`` (LIKE),
        preferring the one with the most edges. Returns a flat dict
        ready for ``formatter.format_node`` - a flat view, not a
        visualization-oriented subgraph with neighbor grouping.

        Division of labor:
          - This method: fetches ALL edges/quotes (unfiltered), then
            splits in Python by *max_chapter* into seen vs. future.
            Seen items go in ``relations`` / ``quotes``; future items
            contribute only counts (``future_relations_count`` /
            ``future_quotes_count``) - the spoiler-gated summary, so
            the LLM knows "there's more" without seeing the detail.
          - formatter: presentation only (no filtering, no counting).

        Works for any node kind (character / event / location / …).
        Quotes are only fetched for characters (``graph_quotes`` is
        character-scoped).
        """
        conn = self.get_conn()

        # ── 1. Locate the node (prefer most-connected match) ────────────
        row = conn.execute(
            f"SELECT id, canonical_name, kind, description, "
            f"first_appearance_chunk_id "
            f"FROM {TABLE_NODES} "
            "WHERE project_id = ? AND canonical_name LIKE ? "
            "ORDER BY "
            "  (SELECT COUNT(*) FROM edges "
            "   WHERE source_id = nodes.id OR target_id = nodes.id) DESC "
            "LIMIT 1",
            (book_id, f"%{name}%"),
        ).fetchone()

        if row is None:
            return {"error": f"未找到实体: {name}"}

        node_id = row["id"]
        first_ch = self._chapter_of_chunk(row["first_appearance_chunk_id"])

        aliases = [r["alias_name"] for r in conn.execute(
            f"SELECT alias_name FROM {TABLE_ALIASES} WHERE node_id = ?",
            (node_id,),
        ).fetchall()]

        # ── 2. All edges (unfiltered) → split seen vs. future ───────────
        edge_rows = conn.execute(
            f"SELECT e.kind, e.chunk_id, e.event_type, e.subject, "
            f"  CASE WHEN e.source_id = ? THEN 'out' ELSE 'in' END AS direction, "
            f"  n_other.canonical_name AS other_name, "
            f"  n_other.kind AS other_kind "
            f"FROM {TABLE_EDGES} e "
            f"JOIN {TABLE_NODES} n_other "
            f"  ON n_other.id = CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END "
            f"WHERE (e.source_id = ? OR e.target_id = ?) AND e.project_id = ? "
            f"ORDER BY e.chunk_id",
            (node_id, node_id, node_id, node_id, book_id),
        ).fetchall()

        relations: list[dict] = []
        future_rel_count = 0
        timeline: set[int] = set()
        for r in edge_rows:
            ch = self._chapter_of_chunk(r["chunk_id"])
            if ch is not None:
                timeline.add(ch)
            entry = {
                "kind": r["kind"],
                "direction": r["direction"],
                "other_name": r["other_name"],
                "other_kind": r["other_kind"],
                "chapter": ch,
                "event_type": r["event_type"] or "fact",
                "subject": r["subject"],
            }
            if max_chapter is None or ch is None or ch <= max_chapter:
                relations.append(entry)
            else:
                future_rel_count += 1

        # Stable sort: seen relations by chapter (None last)
        relations.sort(key=lambda e: (e["chapter"] is None, e["chapter"] or 0))

        if first_ch is not None:
            timeline.add(first_ch)

        # ── 3. Quotes (character only) → split seen vs. future ──────────
        quotes: list[dict] = []
        future_q_count = 0
        if row["kind"] == "character":
            search_terms = [row["canonical_name"]] + aliases
            term_clauses = " OR ".join(["character_name LIKE ?"] * len(search_terms))
            quote_rows = conn.execute(
                f"SELECT quote_text, scene_context, chunk_id "
                f"FROM {TABLE_QUOTES} "
                f"WHERE project_id = ? AND ({term_clauses}) "
                f"ORDER BY chunk_id",
                [book_id] + [f"%{t}%" for t in search_terms],
            ).fetchall()

            for qr in quote_rows:
                ch = self._chapter_of_chunk(qr["chunk_id"])
                entry = {
                    "text": qr["quote_text"],
                    "context": qr["scene_context"] or "",
                    "chapter": ch,
                }
                if max_chapter is None or ch is None or ch <= max_chapter:
                    quotes.append(entry)
                else:
                    future_q_count += 1

            quotes.sort(key=lambda e: (e["chapter"] is None, e["chapter"] or 0))

        # ── 4. Observations (event model) -> split seen vs. future ──────
        observations, future_obs_count = self.get_observations(
            book_id, node_id, max_chapter,
        )

        return {
            "node_id": node_id,
            "canonical_name": row["canonical_name"],
            "kind": row["kind"],
            "description": row["description"],
            "aliases": aliases,
            "first_appearance_chapter": first_ch,
            "observations": observations,
            "relations": relations,
            "quotes": quotes,
            "chapter_timeline": sorted(timeline),
            "future_observations_count": future_obs_count,
            "future_relations_count": future_rel_count,
            "future_quotes_count": future_q_count,
        }

    def find_path(
        self,
        book_id: str,
        from_name: str,
        to_name: str,
        max_chapter: int | None = None,
        max_depth: int = 4,
    ) -> dict:
        """Find the shortest relation path between two entities (BFS).

        Used by graph_trace and baked into graph_context. Loads the
        whole graph (name map + edges + chunk->chapter) in three
        queries, then BFS in memory - fast enough for book-scale
        graphs (hundreds to low-thousands of nodes).

        Edges are filtered by *max_chapter* (spoiler-aware). Each hop
        carries the edge's original source/target names (not the BFS
        traversal direction), so "A loves B" never flips to "B loves A".

        Returns ``{from, to, hops: [{from, kind, to, chapter}, ...]}``;
        empty ``hops`` with a ``note`` if no path within max_depth.
        """
        from collections import deque

        conn = self.get_conn()

        def _locate(name: str) -> tuple[str, str] | None:
            row = conn.execute(
                f"SELECT id, canonical_name FROM {TABLE_NODES} "
                "WHERE project_id = ? AND canonical_name LIKE ? "
                "ORDER BY (SELECT COUNT(*) FROM edges "
                "          WHERE source_id = nodes.id OR target_id = nodes.id) DESC "
                "LIMIT 1",
                (book_id, f"%{name}%"),
            ).fetchone()
            return (row["id"], row["canonical_name"]) if row else None

        src = _locate(from_name)
        dst = _locate(to_name)
        if src is None or dst is None:
            missing = from_name if src is None else to_name
            return {"error": f"未找到实体: {missing}"}

        from_id, from_canon = src
        to_id, to_canon = dst
        if from_id == to_id:
            return {"from": from_canon, "to": to_canon, "hops": []}

        # Load name map + chunk->chapter map (two queries)
        name_map = {r["id"]: r["canonical_name"] for r in conn.execute(
            f"SELECT id, canonical_name FROM {TABLE_NODES} WHERE project_id = ?",
            (book_id,),
        ).fetchall()}
        chunk_chapter = {r["chunk_id"]: r["chapter_index"] for r in conn.execute(
            f"SELECT chunk_id, chapter_index FROM {TABLE_GRAPH_CHUNKS}"
        ).fetchall()}

        # Load edges (one query), build undirected adjacency. Each adj
        # entry carries the edge's original source/target so hops show
        # the true relation direction regardless of BFS traversal way.
        edge_rows = conn.execute(
            f"SELECT source_id, target_id, kind, chunk_id FROM {TABLE_EDGES} "
            "WHERE project_id = ?",
            (book_id,),
        ).fetchall()
        adj: dict[str, list[tuple[str, str, str, str, int | None]]] = {}
        for r in edge_rows:
            ch = chunk_chapter.get(r["chunk_id"])
            if max_chapter is not None and ch is not None and ch > max_chapter:
                continue
            src_id, tgt_id, kind = r["source_id"], r["target_id"], r["kind"]
            adj.setdefault(src_id, []).append((tgt_id, src_id, tgt_id, kind, ch))
            adj.setdefault(tgt_id, []).append((src_id, src_id, tgt_id, kind, ch))

        # BFS
        visited = {from_id}
        queue: deque = deque([(from_id, [])])
        while queue:
            node_id, path = queue.popleft()
            if node_id == to_id:
                return {"from": from_canon, "to": to_canon, "hops": path}
            if len(path) >= max_depth:
                continue
            for neighbor_id, edge_src, edge_tgt, kind, ch in adj.get(node_id, []):
                if neighbor_id in visited:
                    continue
                visited.add(neighbor_id)
                hop = {
                    "from": name_map.get(edge_src, "?"),
                    "kind": kind,
                    "to": name_map.get(edge_tgt, "?"),
                    "chapter": ch,
                }
                queue.append((neighbor_id, path + [hop]))

        return {
            "from": from_canon,
            "to": to_canon,
            "hops": [],
            "note": f"未找到路径(≤{max_depth} 跳)",
        }

    def _chapter_of_chunk(self, chunk_id: str | None) -> int | None:
        """Return the chapter_index for a chunk_id, or None."""
        if chunk_id is None:
            return None
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT chapter_index FROM {TABLE_GRAPH_CHUNKS} WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        return row["chapter_index"] if row else None


# ── Path resolution (shared by API + CLI) ──────────────────────────────────
#
# Historical dual-naming hazard: graph files were created under either
# stable_book_id naming (``graph_sb-*.db``, via import_log) or raw book_id
# naming (``graph_<content-hash>.db``, pre-log fallback).  A book whose
# data lives under the legacy name becomes invisible once it gains an
# import_log.  These helpers centralise "which file actually holds the
# data" so every reader and writer agrees on one path.


def graph_db_has_data(path: Path, book_id: str) -> bool:
    """True if an existing graph DB actually holds rows for ``book_id``.

    Opens an existing file only — never creates.  Used to tell a legacy
    book_id-named shell (empty DDL skeleton) apart from a file that
    carries real data, without fabricating a new file.
    """
    gdb = None
    try:
        gdb = GraphDB(path)
        stats = gdb.get_stats(book_id)
        return (stats.get("nodes", 0) > 0
                or stats.get("chunks_done", 0) > 0
                or stats.get("chunks_failed", 0) > 0)
    except Exception:
        log.debug("graph has-data check failed for %s", path.name, exc_info=True)
        return False
    finally:
        if gdb is not None:
            gdb.close()


def resolve_graph_db_path(config, db, book_id: str) -> Path:
    """Pick the graph DB path that actually holds (or should hold) data.

    Priority: stable_book_id naming when it exists → legacy book_id-named
    file when it carries real data (migrated in place to stable naming) →
    fresh stable_book_id path.  Never creates a file here.

    Heals the historical naming split: when a book gains an import_log but
    its only real data lives in a pre-log book_id-named file, that file is
    renamed (atomic, same volume) to the stable name so every later read
    and write lands on the authoritative path.  Books without an
    import_log resolve to book_id naming — that IS their identity until
    one exists.
    """
    sb_path = None
    if db is not None:
        log_row = db.get_import_log_by_book(book_id)
        if log_row and log_row.stable_book_id:
            sb_path = config.graph_db_path(log_row.stable_book_id)
    bid_path = config.graph_db_path(book_id)
    if sb_path is None:
        # No stable identity yet — book_id naming IS the identity for now.
        return bid_path
    if sb_path.exists():
        return sb_path
    if bid_path.exists() and graph_db_has_data(bid_path, book_id):
        try:
            bid_path.rename(sb_path)
            log.warning("Graph migrated %s -> %s", bid_path.name, sb_path.name)
            return sb_path
        except OSError:
            # File busy (open elsewhere) — keep using the legacy file; the
            # next resolution attempt will retry the rename.
            return bid_path
    return sb_path

