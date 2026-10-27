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
from dataclasses import dataclass, field
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
TABLE_NOTES = "graph_notes"


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

# FTS5 + triggers (v3 corrected: columns strictly aligned with the nodes table)
#
# 列名必须是 ``id``（= ``nodes.id``），**不能叫 ``node_id``**。external-content
# 的 FTS5 表在查询时按**列名**回 content 表取值，声明一个 content 表上不存在的
# 列名会让整张表**任何查询都抛** ``no such column: T.node_id`` —— 连
# ``SELECT COUNT(*)`` 都不行。实测后果：``search_nodes`` / ``search_all`` 的
# FTS 分支**永远**抛异常掉进 LIKE 兜底，前缀匹配与 BM25 从未生效，而 docstring
# 一直写着「FTS5 prefix match」。（触发器写的是 ``NEW.id``，所以索引侧一直是对的
# —— 只有查询侧坏。这个不对称正是它藏得住的原因。）
GRAPH_FTS_DDL = [
    f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
        id, canonical_name, kind, description,
        content='{TABLE_NODES}', content_rowid='rowid'
    );
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON {TABLE_NODES} BEGIN
        INSERT INTO nodes_fts(rowid, id, canonical_name, kind, description)
        VALUES (NEW.rowid, NEW.id, NEW.canonical_name, NEW.kind, NEW.description);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON {TABLE_NODES} BEGIN
        INSERT INTO nodes_fts(nodes_fts, rowid, id, canonical_name, kind, description)
        VALUES ('delete', OLD.rowid, OLD.id, OLD.canonical_name, OLD.kind, OLD.description);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON {TABLE_NODES} BEGIN
        INSERT INTO nodes_fts(nodes_fts, rowid, id, canonical_name, kind, description)
        VALUES ('delete', OLD.rowid, OLD.id, OLD.canonical_name, OLD.kind, OLD.description);
        INSERT INTO nodes_fts(rowid, id, canonical_name, kind, description)
        VALUES (NEW.rowid, NEW.id, NEW.canonical_name, NEW.kind, NEW.description);
    END;
    """,
]


# ── Anchor relink report ──────────────────────────────────────────────────

# 两行并一行时，状态取「更好」的那个。done 是已经花过 token 的成果，
# 不能被一次重导静默抹成 pending —— 那会让续跑白跑一遍（或反过来，
# 把没提过的块标成已提过而永远不再提）。
_STATUS_RANK = {"done": 3, "excluded": 2, "failed": 1, "pending": 0}


_BRACKETS = (("《", "》"), ("〈", "〉"), ("「", "」"))


def _bracket_variant(name: str) -> str:
    """``《圣经》`` → ``圣经``；``圣经`` → ``《圣经》``。其它原样返回。

    只在带/不带书名号之间来回换一次，不做任何别的归一 —— 这条规则要能一眼
    看懂，才敢让它合并节点。
    """
    s = (name or "").strip()
    for left, right in _BRACKETS:
        if s.startswith(left) and s.endswith(right) and len(s) > 2:
            return s[1:-1].strip()
    if s:
        left, right = _BRACKETS[0]
        return f"{left}{s}{right}"
    return ""


def _better_status(a: str | None, b: str | None) -> str:
    a, b = a or "pending", b or "pending"
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b


@dataclass
class RelinkReport:
    """``relink_chunks`` 的结果。每一条被拒绝的记录都要能说出为什么。"""

    book_id: str = ""
    total: int = 0                  # 该书的 graph_chunks 行数
    already_ok: int = 0             # 锚点本来就对
    by_hash: int = 0                # 按内容指纹重连
    by_coord: int = 0               # 按 (chapter, chunk) 坐标重连
    merged: int = 0                 # 重连到一条已存在的行（内容合并）
    ambiguous: list[dict] = field(default_factory=list)   # 多个候选 → 拒绝
    unresolved: list[dict] = field(default_factory=list)  # 找不到 → 拒绝
    deduped_quotes: int = 0         # 重连后与已有引文重复而被丢弃
    deduped_edges: int = 0          # 同上，边
    nodes_relinked: int = 0         # 被改指到新块的节点首现锚点
    obs_reanchored: int = 0         # 重算了 anchor_chapter 的观察
    status_kept: int = 0            # 合并两行时保住了 done 状态
    applied: bool = False           # dry-run 时为 False

    @property
    def relinked(self) -> int:
        return self.by_hash + self.by_coord

    @property
    def resolved(self) -> int:
        return self.already_ok + self.relinked

    def summary(self) -> str:
        return (f"锚点重连 {self.book_id}: {self.resolved}/{self.total} 可用"
                f"（原本就对 {self.already_ok}、指纹 {self.by_hash}、"
                f"坐标 {self.by_coord}、合并 {self.merged}）；"
                f"拒绝 歧义 {len(self.ambiguous)} / 找不到 {len(self.unresolved)}"
                + (f"；节点首现 {self.nodes_relinked} 观察锚 {self.obs_reanchored}"
                   if (self.nodes_relinked or self.obs_reanchored) else "")
                + (f"；去重 引文 {self.deduped_quotes} 边 {self.deduped_edges}"
                   if (self.deduped_quotes or self.deduped_edges) else "")
                + ("" if self.applied else "（dry-run，未写入）"))


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
        (8, "Verbatim evidence: evidence_text on observations + edges", [
            # 断言要能**指回原文**。以前 observations.evidence 存的是 chunk_id
            # （锚点），edges 连锚点都没有 —— 于是「这句断言有原文依据吗」这个
            # 问题在库层面根本问不出来。常量默认值，不踩 migration 陷阱。
            f"ALTER TABLE {TABLE_OBSERVATIONS} ADD COLUMN evidence_text TEXT DEFAULT '';",
            f"ALTER TABLE {TABLE_EDGES} ADD COLUMN evidence_text TEXT DEFAULT '';",
        ]),
        (9, "Reader notes: 读者笔记（agent 自己的笔记本）", [
            # 见 docs/reader-notes-design.md。与 books.db 的 annotations 是
            # 两回事，**不要合并** —— 那张表是用户对正文的批注，而且已经被
            # recall 的 user-notes / user-highlights 两个通道消费，混表会让
            # agent 的笔记漏进 <user-notes> 喂回给它自己，绕成一个环。
            #
            # node_id 可空：读者笔记不一定挂在实体上（「这里的节奏真好」）。
            # chapter_index 刻意不落库 —— 查询时 join graph_chunks 现算，
            # 少一个会随重导过期的东西。
            f"""CREATE TABLE IF NOT EXISTS {TABLE_NOTES} (
                id          TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                node_id     TEXT,
                chunk_id    TEXT NOT NULL,
                quote_text  TEXT NOT NULL DEFAULT '',
                text        TEXT NOT NULL DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            );""",
            f"CREATE INDEX IF NOT EXISTS idx_notes_node ON {TABLE_NOTES}(project_id, node_id);",
            f"CREATE INDEX IF NOT EXISTS idx_notes_chunk ON {TABLE_NOTES}(chunk_id);",
        ]),
        (10, "修复 nodes_fts：列名 node_id 在 content 表上不存在，整张 FTS 不可查", [
            # 见 GRAPH_FTS_DDL 的注释。建表语句在 GRAPH_FTS_DDL 里只有一份，
            # 这里直接复用（迁移 1 对**新库**建的就是修好的版本，本迁移对它是
            # 空操作；对**旧库**才是真正的重建）。
            "DROP TRIGGER IF EXISTS nodes_ai;",
            "DROP TRIGGER IF EXISTS nodes_ad;",
            "DROP TRIGGER IF EXISTS nodes_au;",
            "DROP TABLE IF EXISTS nodes_fts;",
            *GRAPH_FTS_DDL,
            # 触发器只对**此后**的增删改生效，存量行要靠 rebuild 补索引。
            "INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild');",
        ]),
        (11, "LLM 调用台账：记前缀缓存命中与推理 token", [
            # 命中率取决于**前缀是否稳定**，不是端点随机行为 —— 不记这一列，
            # 「为什么缓存命中率低」永远只能靠猜。
            f"ALTER TABLE {TABLE_LLM_LOGS} ADD COLUMN tokens_cached INTEGER;",
            # 推理 token 单列记：输出总量里有多少是思考、多少是正文，
            # 决定了优化该砍提示词还是该砍推理。
            f"ALTER TABLE {TABLE_LLM_LOGS} ADD COLUMN reasoning_tokens INTEGER;",
        ]),
        (12, "边去重：occurrences 计数列（同一条持久关系不再按块重复落库）", [
            # 关系**词表化**之后，边表达的是「跨场景成立的关系」，一句话就是
            # 一条。可写入路径仍按 (src, tgt, kind, chunk_id) 唯一 —— 于是
            # 「杰罗姆 loves 阿莉莎」在第 13/14/16/17/18/19/20 章各落一条。
            # 实测窄门 241 条边里 89 条（37%）是同一关系的第 N 份拷贝，
            # 去重后 152 条，gold 召回一条不少。多出来的那一列记住「被重复
            # 观察了多少次」—— 角色卡的排序要用它，去掉就只剩「谁先被写」。
            f"ALTER TABLE {TABLE_EDGES} ADD COLUMN occurrences INTEGER NOT NULL DEFAULT 1;",
        ]),
        (13, "飞行记录仪：存思维链正文（reasoning_content）", [
            # 只记 reasoning_tokens 等于只记了「想了多久」，没记「想了什么」。
            # 实测一次提取调用 62% 的输出是思考，出错的那一步恰恰在思考之外
            # （抄错一个代词），而要看懂它得靠正文。**推理模型才有这个字段**，
            # 非推理模型存空串 —— 不假装有。
            f"ALTER TABLE {TABLE_LLM_LOGS} ADD COLUMN reasoning_content TEXT;",
        ]),
    ]

    def __init__(self, db_path):
        super().__init__(db_path)
        self.initialize()

    # ── Write: nodes ──────────────────────────────────────────────────────

    def upsert_node(self, book_id: str, name: str, kind: str,
                    description: str | None = None,
                    first_chunk_id: str | None = None,
                    overwrite_description: bool = False,
                    personality: str | None = None,
                    appearance: str | None = None,
                    motivation: str | None = None,
                    role_in_story: str | None = None,
                    speaking_style: str | None = None) -> str:
        """Insert or update node. Returns node_id.

        Resolution order:
        1. If name matches a registered alias → update the canonical node
        2. If name matches an existing canonical_name+kind → update it
        2b. If name matches an existing canonical_name under **another** kind
            → reuse it (同名即同实体)
        3. Otherwise → insert new node

        **2b 是「同一实体裂成多节点」的根治**（实测：孔子×4、精神分裂症×3、
        苏联×2 —— 都是同一个名字配了不同的 kind 串）。提示词会漂，SQL 不会，
        所以这条去重落在这里而不是提示词里。chapter 节点排除在外：章节标题
        与实体重名是常事，不能把实体并进章节节点。

        ``description`` **默认只填空、不覆盖**（``overwrite_description=True``
        才覆盖）。理由：批次提取器写的 `why` 是**本批视角**的一句话（实测有
        「本块未出场，但…」），逐批覆盖会让身份说明永远停在最后一章的临时
        判断上；而 ``format_role_kernel`` 把它当角色身份的第一行。先写者赢
        （骨架趟 → 首个批次），后来的批次只能补空，不能改写。
        """
        conn = self.get_conn()

        # Step 1: check aliases — if name is someone's alias, use canonical node
        # 别名在 (project_id, alias_name) 上唯一，所以**不按 kind 过滤** ——
        # 带上 kind 只会让「提取器换了个 kind 说法」变成查不到 → 又新建一个节点。
        alias_row = conn.execute(
            f"SELECT a.node_id FROM {TABLE_ALIASES} a "
            f"JOIN {TABLE_NODES} n ON n.id = a.node_id "
            "WHERE a.project_id=? AND a.alias_name=? AND n.project_id=? LIMIT 1",
            (book_id, name, book_id),
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
                    if col == "description" and not overwrite_description:
                        updates.append(f"{col}=COALESCE(NULLIF({col},''),?)")
                    else:
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
                    if col == "description" and not overwrite_description:
                        updates.append(f"{col}=COALESCE(NULLIF({col},''),?)")
                    else:
                        updates.append(f"{col}=?")
                    params.append(val)
            if updates:
                params.append(existing["id"])
                conn.execute(
                    f"UPDATE {TABLE_NODES} SET {', '.join(updates)} WHERE id=?",
                    params,
                )
            return existing["id"]

        # Step 2b: 同名、别的 kind —— 还是同一个实体，复用而不是新建。
        same_name = conn.execute(
            f"SELECT id FROM {TABLE_NODES} "
            "WHERE project_id=? AND canonical_name=? AND kind!='chapter' "
            "ORDER BY (kind=?) DESC, id LIMIT 1",
            (book_id, name, kind),
        ).fetchone()
        if same_name:
            return same_name["id"]

        # Step 2c: **书名号变体**。《圣经》与 圣经、《如胶似漆》与 如胶似漆
        # 是同一个实体 —— 实测窄门一本里就同时出现（两个 work 节点）。
        # 只在 **kind 相同** 时合并：书名号在有些书里是「作品 vs 概念」的
        # 分界线（《窄门》works / 窄门 concept），跨 kind 合并会把它们压成
        # 一个。生成两个变体各查一次，不做模糊匹配 —— 模糊匹配是另一类事故。
        variant = _bracket_variant(name)
        if variant and variant != name and kind != "chapter":
            twin = conn.execute(
                f"SELECT id FROM {TABLE_NODES} "
                "WHERE project_id=? AND canonical_name=? AND kind=? LIMIT 1",
                (book_id, variant, kind),
            ).fetchone()
            if twin:
                return twin["id"]

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
                 tags: list[str] | None = None,
                 evidence_text: str = "") -> bool:
        """Insert edge. Returns True if inserted, False if duplicate.

        event_type: fact / perspective / opinion (default fact).
        subject: whose perspective for non-fact edges (e.g. "LLM提取").
        tags: open folksonomy tags (agent-registered themes).
        evidence_text: 支撑这条关系的**逐字原文**（不是 chunk_id）。

        **同一条关系只落一行。** 键是 (src, tgt, kind, event_type[, subject])：
        `event_type` 进键，是因为「他是她哥哥」（fact）和「她觉得他是她哥哥」
        （perspective）是两句不同的话，不能合并；非事实边再带上 `subject`，
        因为「杰罗姆认为甲爱乙」与「丙认为甲爱乙」也是两句不同的话。重复
        写入时只 `occurrences += 1`，并把锚点挪到**更早**的那一块：剧透门
        要的是「读者最早能在哪里知道这件事」，不是「最晚被提到的那次」。

        旧实现按 (…, chunk_id) 唯一，而持久关系的本质就是**每一批都会
        再看见它一次** —— 实测窄门 241 条边里 89 条是同一关系的拷贝
        （「杰罗姆 loves 阿莉莎」7 条、「朱莉叶特→阿莉莎 cares_for」
        5 条），图就退化成了事件日志。
        """
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT e.id, e.occurrences, "
            f"  c_old.chapter_index AS old_ch, c_new.chapter_index AS new_ch "
            f"FROM {TABLE_EDGES} e "
            f"LEFT JOIN {TABLE_GRAPH_CHUNKS} c_old ON c_old.chunk_id = e.chunk_id "
            f"LEFT JOIN {TABLE_GRAPH_CHUNKS} c_new ON c_new.chunk_id = ? "
            f"WHERE e.project_id=? AND e.source_id=? AND e.target_id=? "
            f"  AND e.kind=? AND e.event_type=? "
            f"  AND (e.event_type='fact' OR COALESCE(e.subject,'')=?)",
            (chunk_id, book_id, source_id, target_id, kind, event_type,
             (subject or "") if event_type != "fact" else ""),
        ).fetchone()
        if row is not None:
            old_ch, new_ch = row["old_ch"], row["new_ch"]
            # 无锚点是最弱的一种 —— 有锚点的这次应当把它顶掉。
            take = chunk_id is not None and (
                old_ch is None or (new_ch is not None and new_ch < old_ch))
            if take:
                conn.execute(
                    f"UPDATE {TABLE_EDGES} SET occurrences = occurrences + 1, "
                    f"  chunk_id = ?, evidence_text = CASE WHEN ? != '' "
                    f"    THEN ? ELSE evidence_text END WHERE id = ?",
                    (chunk_id, evidence_text, evidence_text, row["id"]),
                )
            else:
                conn.execute(
                    f"UPDATE {TABLE_EDGES} SET occurrences = occurrences + 1 "
                    f"WHERE id = ?", (row["id"],),
                )
            return False
        try:
            conn.execute(
                f"INSERT INTO {TABLE_EDGES}"
                "(source_id, target_id, kind, chunk_id, event_type, subject, "
                "tags, evidence_text, project_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (source_id, target_id, kind, chunk_id, event_type, subject,
                 json.dumps(tags or [], ensure_ascii=False), evidence_text, book_id),
            )
            return True
        except sqlite3.IntegrityError:
            # (…, chunk_id) 唯一约束仍在：同一块里同一关系写两遍。
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

    def add_unresolved(self, book_id: str, reference_name: str,
                       reference_kind: str = "", chunk_id: str = "",
                       candidates: list[str] | None = None) -> None:
        """记一条**解析不到节点**的引用。

        边、观察、别名、引文的关联节点都可能指向一个图谱里不存在的名字。
        以前这四处都是 ``log.debug`` 一句就丢了 —— 那正是「沉默不是成功」：
        图谱少了一条边，日志里查不出来。表早就建好了（v1 DDL），一直空着。
        """
        self.get_conn().execute(
            f"INSERT INTO {TABLE_UNRESOLVED}"
            "(reference_name, reference_kind, chunk_id, candidates, project_id) "
            "VALUES(?,?,?,?,?)",
            (reference_name, reference_kind, chunk_id,
             json.dumps(candidates or [], ensure_ascii=False), book_id),
        )

    def count_unresolved(self, book_id: str) -> int:
        return self.get_conn().execute(
            f"SELECT COUNT(*) FROM {TABLE_UNRESOLVED} WHERE project_id=?",
            (book_id,)).fetchone()[0]

    # ── 读者笔记（见 docs/reader-notes-design.md）──────────────────────────

    def add_note(self, book_id: str, chunk_id: str, quote_text: str,
                 text: str, node_id: str = "") -> str:
        """落一条读者笔记。``node_id`` 可空 —— 笔记不一定挂在实体上。"""
        nid = f"note-{uuid.uuid4().hex[:16]}"
        self.get_conn().execute(
            f"INSERT INTO {TABLE_NOTES}"
            "(id, project_id, node_id, chunk_id, quote_text, text) "
            "VALUES(?,?,?,?,?,?)",
            (nid, book_id, node_id or None, chunk_id, quote_text, text),
        )
        return nid

    def count_notes(self, book_id: str) -> int:
        return self.get_conn().execute(
            f"SELECT COUNT(*) FROM {TABLE_NOTES} WHERE project_id=?",
            (book_id,)).fetchone()[0]

    def get_notes(self, book_id: str, *, node_id: str | None = None,
                  chapter_index: int | None = None) -> list[dict]:
        """读笔记，按章号排序。

        ``chapter_index`` **现算**（join ``graph_chunks``），不落库 ——
        落库就会随重导过期。披露门的章号下界过滤走 ``chapter_index``。
        """
        sql = (f"SELECT n.*, c.chapter_index AS chapter_index "
               f"FROM {TABLE_NOTES} n "
               f"LEFT JOIN {TABLE_GRAPH_CHUNKS} c ON c.chunk_id = n.chunk_id "
               f"WHERE n.project_id=?")
        args: list[Any] = [book_id]
        if node_id is not None:
            sql += " AND n.node_id=?"
            args.append(node_id)
        if chapter_index is not None:
            sql += " AND c.chapter_index<=?"
            args.append(chapter_index)
        sql += " ORDER BY c.chapter_index, n.created_at"
        return [dict(r) for r in self.get_conn().execute(sql, args)]

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
        evidence_text: str = "",
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
        evidence_text: 支撑这条观察的**逐字原文**。``evidence`` 是锚点
        （chunk_id），两个字段各司其职 —— 只有锚点问不出「它是不是编的」。
        """
        conn = self.get_conn()
        obs_id = f"obs-{uuid.uuid4().hex[:12]}"
        actual_trust = trust_score if trust_score is not None else 0.5
        try:
            conn.execute(
                f"INSERT INTO {TABLE_OBSERVATIONS}"
                "(id, project_id, node_id, field, value, event_type, "
                "subject, anchor_chapter, evidence, tags, source, trust_score, "
                "evidence_text, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (obs_id, book_id, node_id, field, value, event_type,
                 subject, anchor_chapter, evidence,
                 json.dumps(tags or [], ensure_ascii=False), source, actual_trust,
                 evidence_text),
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

        Incremental trust model with temporal decay:
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

        Returns {edges_moved, aliases_moved, obs_moved, notes_moved,
        removed_name, kept_name}.
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
        obs_moved = 0
        notes_moved = 0

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
            cur = conn.execute(
                f"INSERT OR IGNORE INTO {TABLE_ALIASES} (alias_name, node_id, project_id) VALUES(?,?,?)",
                (removed_name, keep_id, book_id),
            )
            aliases_moved += max(cur.rowcount, 0)

            # 3. Move all aliases from remove to keep
            alias_rows = conn.execute(
                f"SELECT alias_name FROM {TABLE_ALIASES} WHERE node_id=? AND project_id=?",
                (remove_id, book_id),
            ).fetchall()
            for ar in alias_rows:
                cur = conn.execute(
                    f"UPDATE OR IGNORE {TABLE_ALIASES} SET node_id=? "
                    "WHERE alias_name=? AND project_id=? AND node_id=?",
                    (keep_id, ar["alias_name"], book_id, remove_id),
                )
                aliases_moved += max(cur.rowcount, 0)

            # 4. Move observations & notes
            #    以前只搬边和别名 —— 被并掉那个节点上的**观察会指向一个不存在的
            #    id**，永久失联（观察是角色内核的唯一来源，等于把画像删了）。
            #    读者笔记同理。
            for tbl, attr in ((TABLE_OBSERVATIONS, "obs"), (TABLE_NOTES, "notes")):
                # 用 cursor.rowcount，**不是** conn.total_changes —— 后者是
                # 「连接打开以来的累计改动数」，拿它当单条语句的结果会得到
                # 荒谬的数字（实测一本只有 10 条观察的书报出 obs_moved=4971）。
                cur = conn.execute(
                    f"UPDATE {tbl} SET node_id=? WHERE node_id=? AND project_id=?",
                    (keep_id, remove_id, book_id),
                )
                if attr == "obs":
                    obs_moved += cur.rowcount
                else:
                    notes_moved += cur.rowcount

            # 5. 首现块取**章号更早**的那个（keep 没有才无条件继承）。
            #    first_appearance 是导出量，合并后取晚的那个会让实体在索引里
            #    显得「出现得更迟」，就近档选不到它。
            cids = conn.execute(
                f"SELECT id, first_appearance_chunk_id AS c FROM {TABLE_NODES} "
                "WHERE id IN (?,?) AND project_id=?",
                (keep_id, remove_id, book_id),
            ).fetchall()
            by_id = {r["id"]: r["c"] for r in cids}
            keep_c, rem_c = by_id.get(keep_id), by_id.get(remove_id)
            if rem_c and (not keep_c or (
                    (self._chapter_of_chunk(rem_c) if rem_c else None) is not None
                    and (self._chapter_of_chunk(keep_c) is None
                         or self._chapter_of_chunk(rem_c) < self._chapter_of_chunk(keep_c)))):
                conn.execute(f"UPDATE {TABLE_NODES} SET first_appearance_chunk_id=? "
                             "WHERE id=?", (rem_c, keep_id))

            # 6. Delete remove node
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
                "obs_moved": obs_moved, "notes_moved": notes_moved,
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

    # ── Anchor relink ─────────────────────────────────────────────────────

    def relink_chunks(self, book_id: str, current: list[dict],
                      *, dry_run: bool = False) -> RelinkReport:
        """把该书的图谱锚点重连到**当前**的 ``book_chunks`` 上。

        重导会重新生成 ``book_chunks.id``，而图谱的边/引文/观察都指着旧 id ——
        实测 9 本书里 8 本交集为 0，97% 的节点因此定位不到章（阅读侧剧透门失效）。

        *current* 每项：``{"chunk_id", "chapter_index", "chunk_index", "content_hash"}``。

        匹配顺序：**内容指纹优先，坐标兜底**。指纹是内容同一性的证据；坐标只在
        指纹落空时用（正文被 strip 掉一两个字符就会换指纹，坐标仍对得上）。

        拒绝而非猜测：一个旧块对应多个候选 → 记 ``ambiguous`` 不写；找不到 →
        记 ``unresolved`` 不写。**沉默不是成功** —— 拒绝的每一条都在报告里。
        """
        conn = self.get_conn()
        report = RelinkReport(book_id=book_id)
        rows = conn.execute(
            f"SELECT chunk_id, chapter_index, chunk_index, content_hash "
            f"FROM {TABLE_GRAPH_CHUNKS} WHERE book_id=?", (book_id,)
        ).fetchall()
        report.total = len(rows)
        if not rows:
            return report

        cur_ids = {c["chunk_id"] for c in current}
        by_hash: dict[str, list[str]] = {}
        by_coord: dict[tuple[int, int], str] = {}
        for c in current:
            by_hash.setdefault(c["content_hash"], []).append(c["chunk_id"])
            by_coord[(c["chapter_index"], c["chunk_index"])] = c["chunk_id"]

        occupied = {r["chunk_id"] for r in rows}
        target: dict[str, str] = {}
        for r in rows:
            old = r["chunk_id"]
            if old in cur_ids:
                report.already_ok += 1
                continue
            cands = by_hash.get(r["content_hash"]) or []
            if len(cands) == 1:
                target[old] = cands[0]
                report.by_hash += 1
            elif len(cands) > 1:
                report.ambiguous.append({
                    "chunk_id": old, "reason": "内容指纹命中多个候选",
                    "candidates": cands[:5]})
            elif (r["chapter_index"], r["chunk_index"]) in by_coord:
                target[old] = by_coord[(r["chapter_index"], r["chunk_index"])]
                report.by_coord += 1
            else:
                report.unresolved.append({
                    "chunk_id": old, "chapter_index": r["chapter_index"],
                    "chunk_index": r["chunk_index"], "reason": "指纹与坐标都对不上"})

        # 落到一条已存在的图谱行上 = 内容被合并了 → 合并两行，不丢
        report.merged = sum(1 for new in target.values() if new in occupied)
        if dry_run:
            return report

        coord_of = {c["chunk_id"]: (c["chapter_index"], c["chunk_index"])
                    for c in current}
        claimed: set[str] = set()
        for old, new in target.items():
            for table, col in ((TABLE_EDGES, "chunk_id"),
                               (TABLE_QUOTES, "chunk_id"),
                               (TABLE_OBSERVATIONS, "evidence")):
                before = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (old,)
                ).fetchone()[0]
                cur = conn.execute(
                    f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (new, old))
                dropped = before - cur.rowcount
                if col == "chunk_id" and table == TABLE_QUOTES:
                    report.deduped_quotes += dropped
                elif col == "chunk_id":
                    report.deduped_edges += dropped

            # 节点首现锚点也要跟着走 —— 它同样指着一个旧 id。不重指的话，
            # 「点节点跳原文」与按章过滤（剧透门）在这批节点上仍是坏的。
            report.nodes_relinked += conn.execute(
                f"UPDATE {TABLE_NODES} SET first_appearance_chunk_id=? "
                "WHERE first_appearance_chunk_id=? AND project_id=?",
                (new, old, book_id),
            ).rowcount

            # 观察的 anchor_chapter 是**从旧块算出来的章号**。块换了章号就可能
            # 变了（重导重切过章），不重算就等于把观察挂在错的章上。
            if new in coord_of:
                report.obs_reanchored += conn.execute(
                    f"UPDATE {TABLE_OBSERVATIONS} SET anchor_chapter=? "
                    "WHERE evidence=? AND project_id=?",
                    (coord_of[new][0], new, book_id),
                ).rowcount

            if new in occupied or new in claimed:
                # 两行并一行：状态取**更好**的那个（done > failed > pending），
                # 否则一次重导就会把「已提取」静默降级成「没提取」→ 白跑一遍。
                old_st = conn.execute(
                    f"SELECT extract_status FROM {TABLE_GRAPH_CHUNKS} "
                    "WHERE chunk_id=?", (old,)).fetchone()
                new_st = conn.execute(
                    f"SELECT extract_status FROM {TABLE_GRAPH_CHUNKS} "
                    "WHERE chunk_id=?", (new,)).fetchone()
                keep = _better_status(old_st[0] if old_st else None,
                                      new_st[0] if new_st else None)
                if new_st and keep != new_st[0]:
                    conn.execute(
                        f"UPDATE {TABLE_GRAPH_CHUNKS} SET extract_status=? "
                        "WHERE chunk_id=?", (keep, new))
                    report.status_kept += 1
                conn.execute(f"DELETE FROM {TABLE_GRAPH_CHUNKS} WHERE chunk_id=?", (old,))
            else:
                conn.execute(f"UPDATE {TABLE_GRAPH_CHUNKS} SET chunk_id=? "
                             f"WHERE chunk_id=?", (new, old))
                claimed.add(new)
        conn.commit()
        report.applied = True
        return report

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
        tokens_out: int | None = None, tokens_cached: int | None = None,
        reasoning_tokens: int | None = None, status: str = "ok",
        error_detail: str | None = None,
        prompt: str = "", response: str = "",
        reasoning_content: str = "",
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
                " tokens_in, tokens_out, tokens_cached, reasoning_tokens, "
                " status, error_detail, prompt, response, reasoning_content) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, book_id, phase, batch_id, batch_index,
                 json.dumps(chunk_ids, ensure_ascii=False) if chunk_ids else None,
                 total_chunks, model, attempt, max_attempts, latency_ms,
                 tokens_in, tokens_out, tokens_cached, reasoning_tokens,
                 status, error_detail, prompt, response, reasoning_content),
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

    _LOCATE_COLS = ("id, canonical_name, kind, description, first_appearance_chunk_id")

    # 搜索结果的统一排序：**精确名（正名或别名）优先** → 度数 → 名字定序。
    # 以前这两处**没有 ORDER BY**，LIMIT 取的是任意几行：实测
    # ``search_all('母亲', limit=2)`` 返回「你母亲 + 八子的母亲」，真正的
    # 「母亲」（度 78）排第 4 被截掉 —— 而 ``_tool_graph_context`` 正是
    # ``limit=2``，于是 `graph_context("母亲和地坛有什么关系")` 挑出的入口
    # 实体是两个不相干的母亲。同一个问题在 ``get_node_detail`` 那边是对的
    # （它按度数排），**两个入口答案不一致**才是真问题。
    # 需要两个 ``query`` 参数（正名精确、别名精确各一个）。
    _SEARCH_ORDER = (
        " ORDER BY (n.canonical_name = ? OR EXISTS ("
        f"   SELECT 1 FROM {TABLE_ALIASES} ax "
        "    WHERE ax.node_id = n.id AND ax.project_id = n.project_id "
        "      AND ax.alias_name = ?)) DESC, "
        " (SELECT COUNT(*) FROM edges e "
        "  WHERE e.source_id = n.id OR e.target_id = n.id) DESC, "
        " n.canonical_name "
    )

    def locate_node(self, book_id: str, name: str) -> sqlite3.Row | None:
        """按**用户给的名字**定位节点：精确别名 → 精确正名 → 子串（度数优先）。

        与 ``name_to_node_id`` 的分工：那个是**写入**路径的解析器，只做精确匹配
        —— 那里退到子串会把「母亲」并进「你母亲」，是数据损坏。读取路径反过来：
        用户的输入天生模糊，要尽量找到东西，所以末档退到子串。

        为什么必须有这一档：库里的别名（``graph_aliases``）**写入路径认、读取
        路径一处都没认**。实测《我与地坛》34 条别名里，``史铁生`` 是叙述者
        「我」的别名（度 315）、``小公园`` 是地坛、``白鸡脖`` 是 B 校长 ——
        而 ``graph_node("史铁生")`` 直接返回「未找到实体」。读者用作者名问话，
        六条工具路径一条都够不着。
        """
        name = (name or "").strip()
        if not name:
            return None
        conn = self.get_conn()
        # 1+2. 精确别名 / 精确正名（复用写入路径的解析器，保证两处顺序一致）
        nid = self.name_to_node_id(book_id, name)
        if nid:
            row = conn.execute(
                f"SELECT {self._LOCATE_COLS} FROM {TABLE_NODES} "
                "WHERE id=? AND project_id=?", (nid, book_id),
            ).fetchone()
            if row:
                return row
        # 3. 子串，度数优先（原有行为，保持不变）
        return conn.execute(
            f"SELECT {self._LOCATE_COLS} FROM {TABLE_NODES} "
            "WHERE project_id = ? AND canonical_name LIKE ? "
            "ORDER BY (SELECT COUNT(*) FROM edges "
            "          WHERE source_id = nodes.id OR target_id = nodes.id) DESC "
            "LIMIT 1",
            (book_id, f"%{name}%"),
        ).fetchone()

    def known_names(self, book_id: str) -> set[str]:
        """库里**已经能解析到节点**的所有名字（canonical + 别名）。

        体检时用来分辨两种「被引用却没声明」：一种是真悬空（要回炉），
        一种是提取器引用了旧实体（完全正常，提示词就是这么教的）。
        """
        conn = self.get_conn()
        names = {r["canonical_name"] for r in conn.execute(
            f"SELECT canonical_name FROM {TABLE_NODES} "
            "WHERE project_id=? AND kind!='chapter'", (book_id,))}
        names |= {r["alias_name"] for r in conn.execute(
            f"SELECT alias_name FROM {TABLE_ALIASES} WHERE project_id=?",
            (book_id,))}
        names.discard(None)
        return names

    def node_kind(self, book_id: str, name: str) -> str | None:
        """这个名字现在在图谱里是什么 kind（按 ``upsert_node`` 的解析顺序）。

        用来发现 **kind 漂移**：提取器这批说「孔子」是 ``person``，上一批说
        是 ``character`` —— 节点是同一个（SQL 层已按同名复用），但说法在漂。
        漂移本身要报出来，否则词表悄悄发散没人知道。
        """
        node_id = self.name_to_node_id(book_id, name)
        if not node_id:
            return None
        row = self.get_conn().execute(
            f"SELECT kind FROM {TABLE_NODES} WHERE id=? AND project_id=?",
            (node_id, book_id),
        ).fetchone()
        return row["kind"] if row else None

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
                "WHERE n.project_id=? AND n.kind!='chapter' "
                "  AND (nodes_fts MATCH ? OR n.canonical_name LIKE ? "
                f"       OR EXISTS (SELECT 1 FROM {TABLE_ALIASES} a "
                "                  WHERE a.node_id = n.id "
                "                    AND a.project_id = n.project_id "
                "                    AND a.alias_name LIKE ?)) "
            )
            params: list[Any] = [book_id, f"{query}*", f"%{query}%", f"%{query}%"]
            if kind:
                sql += " AND n.kind=?"
                params.append(kind)
            if chapter_to is not None:
                # 无章号 = 定位不到时间线 = 无法证明它在读者进度内。
                # 与 get_node_detail / get_observations / search_observations
                # 同一条规矩（fail-closed）。实测代价极小：排除章节节点后
                # 全库只有 6/4190（0.1%）节点首现块解析不到。
                sql += " AND gc.chapter_index IS NOT NULL AND gc.chapter_index <= ?"
                params.append(chapter_to)
            sql += self._SEARCH_ORDER
            params.extend([query, query])
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
                "WHERE n.project_id=? AND n.kind!='chapter' "
                "  AND (n.canonical_name LIKE ? "
                f"      OR EXISTS (SELECT 1 FROM {TABLE_ALIASES} a "
                "                 WHERE a.node_id = n.id "
                "                   AND a.project_id = n.project_id "
                "                   AND a.alias_name LIKE ?)) "
            )
            params = [book_id, f"%{query}%", f"%{query}%"]
            if kind:
                sql += " AND n.kind=?"
                params.append(kind)
            if chapter_to is not None:
                sql += " AND gc.chapter_index IS NOT NULL AND gc.chapter_index <= ?"
                params.append(chapter_to)
            sql += self._SEARCH_ORDER
            params.extend([query, query])
            sql += " LIMIT ?"
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
                "WHERE n.project_id = ? AND n.kind!='chapter' "
                "  AND (nodes_fts MATCH ? OR n.canonical_name LIKE ? "
                f"       OR EXISTS (SELECT 1 FROM {TABLE_ALIASES} a "
                "                  WHERE a.node_id = n.id "
                "                    AND a.project_id = n.project_id "
                "                    AND a.alias_name LIKE ?)) "
            )
            params: list[Any] = [book_id, f"{query}*", f"%{query}%", f"%{query}%"]
            if max_chapter is not None:
                sql += " AND gc.chapter_index IS NOT NULL AND gc.chapter_index <= ?"
                params.append(max_chapter)
            sql += self._SEARCH_ORDER
            params.extend([query, query])
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
                "WHERE n.project_id = ? AND n.kind!='chapter' "
                "  AND (n.canonical_name LIKE ? "
                f"      OR EXISTS (SELECT 1 FROM {TABLE_ALIASES} a "
                "                 WHERE a.node_id = n.id "
                "                   AND a.project_id = n.project_id "
                "                   AND a.alias_name LIKE ?)) "
            )
            params = [book_id, f"%{query}%", f"%{query}%"]
            if max_chapter is not None:
                sql += " AND gc.chapter_index IS NOT NULL AND gc.chapter_index <= ?"
                params.append(max_chapter)
            sql += self._SEARCH_ORDER
            params.extend([query, query])
            sql += " LIMIT ?"
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
                                detailed_cap: int = 24,
                                scope_chunk_ids: set[str] | None = None) -> dict:
        """Two-tier node index for Explorer task messages.

        Tier 1 "compact": ALL non-chapter entities as [canonical_name, kind],
        NO aliases — the LLM knows every entity exists without the prompt
        ballooning with alias strings.
        Tier 2 "detailed": top-relevance non-chapter entities WITH aliases;
        relevance = edge degree (in+out) + observation count, with characters
        whose first appearance is within ±2 chapters of this batch pinned to
        the front.

        就近那一档**只钉 character，这是量出来的，不是随手写的**。曾改成
        「钉所有 kind」（理由是《我二十一岁那年》的 ``7号病室``/``4号病房``
        拿不到名额），实测**更差**：尺子是「那 24 个名额里有几个是**该章正文
        真的出现、且确实带别名**的实体」——原版 46、改后 39。原因是硬分档让
        就近的非人物把名额占满，``我``（8 章）、``地坛``（3 章）这些**有别名
        的枢纽**被挤出去；它们原本靠第二关键字（全局度数）进榜。也试过「就近
        度数×2 / ×4」的加成式（43 / 47），相对原版没有像样的增益，不值得为
        一个实体引入魔法系数。

        *scope_chunk_ids* 限定「只看这个叙事域里出现过的实体」（见
        ``bookgraph/scope.py``）。集类书里各篇自成一体，把别的篇的实体摆到
        提取器面前就是邀请它跨篇连线。**单域（长篇）时传 None**，行为与
        改动前逐字一致。

        Returns {"compact": [...], "detailed": [...]}.
        """
        conn = self.get_conn()

        # 域过滤片段。*scope_chunk_ids* 为 None = 不按域过滤（单域/长篇的
        # 老路径）；给了但为空 = 这个域里一个实体都没有，直接返回空索引 ——
        # 不能当成「不过滤」，那会让空域悄悄看见全书。
        if scope_chunk_ids is not None and not scope_chunk_ids:
            return {"compact": [], "detailed": []}
        scope_ph, scope_params = "", []
        if scope_chunk_ids is not None and len(scope_chunk_ids) <= 500:
            scope_params = sorted(scope_chunk_ids)
            scope_ph = (" AND first_appearance_chunk_id IN ("
                        + ",".join("?" * len(scope_params)) + ")")
        scope_ph_n = scope_ph.replace("first_appearance_chunk_id",
                                      "n.first_appearance_chunk_id")

        # ── Tier 1: compact full list (name + kind only) ──────────────────
        compact = [
            {"canonical_name": r["canonical_name"], "kind": r["kind"]}
            for r in conn.execute(
                f"SELECT canonical_name, kind FROM {TABLE_NODES} "
                "WHERE project_id=? AND kind!='chapter'" + scope_ph +
                " ORDER BY (kind='character') DESC, kind, canonical_name "
                "LIMIT ?",
                [book_id] + scope_params + [compact_cap],
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
            f"SELECT n.id, n.canonical_name, n.kind, n.description, "
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
            "WHERE n.project_id=? AND n.kind!='chapter'" + scope_ph_n +
            " ORDER BY (n.kind='character' AND nearby) DESC, relevance DESC, "
            "n.kind, n.canonical_name "
            "LIMIT ?",
            [book_id] + nearby_params + [book_id] + scope_params + [detailed_cap],
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
             "aliases": alias_map.get(r["id"], []),
             # 身份说明：一句话「它在这本书里做了什么」。索引里只有名字时，
             # 模型判断不了「冯母」是不是「冯世杰的母亲」—— 那不是提取器的
             # 错，是环境没告诉它。
             "description": (r["description"] or "").strip()}
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

        # Resolve chunk_id -> (chapter_index, chunk_index) in one batch.
        # chunk_index 也带上：前端节点「跳转原文」要落到段落，只给章号会
        # 停在章首（与 reader 的 (chapter, chunk) 语义坐标一致）。
        chunk_coord: dict[str, tuple[int, int]] = {}
        if all_chunk_ids:
            cid_list = list(all_chunk_ids)
            cid_placeholders = ",".join("?" * len(cid_list))
            chunk_rows = conn.execute(
                f"SELECT chunk_id, chapter_index, chunk_index FROM {TABLE_GRAPH_CHUNKS} "
                f"WHERE chunk_id IN ({cid_placeholders})",
                cid_list,
            ).fetchall()
            chunk_coord = {r["chunk_id"]: (r["chapter_index"], r["chunk_index"])
                           for r in chunk_rows}

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

        # Annotate nodes with chapter/chunk (first appearance) + aliases
        nodes = []
        for n in nodes_raw:
            cid = n.pop("first_appearance_chunk_id", None)
            coord = chunk_coord.get(cid) if cid else None
            n["chapter"] = coord[0] if coord else None
            n["chunk"] = coord[1] if coord else None
            n["aliases"] = aliases_by_node.get(n["id"], [])
            nodes.append(n)

        # Annotate edges with chapter, then merge by unordered pair
        edge_groups: dict[tuple[str, str], list[dict]] = {}
        for er in edge_rows_raw:
            cid = er.get("chunk_id")
            pair = tuple(sorted([er["source_id"], er["target_id"]]))
            coord = chunk_coord.get(cid) if cid else None
            edge_groups.setdefault(pair, []).append({
                "kind": er["kind"],
                "chapter": coord[0] if coord else None,
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

        Returns ``(seen, future_count, unanchored_count)``. Each
        observation dict has: field, value, event_type, subject, chapter,
        evidence, tags. Seen observations are sorted by (field, chapter).

        ``future`` = 章号在进度之后；``unanchored`` = **章号为 NULL**
        （老提取器不要求锚点，实测《地下室手记》19/19 条如此）。两者都
        不展示，但**分开计数** —— 前者是「还不能说」，后者是「说不清」，
        混在一起报会让人以为是剧透门在正常工作。
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
        unanchored = 0
        for r in rows:
            ch = r["anchor_chapter"]
            entry = {
                "field": r["field"],
                "value": r["value"],
                "event_type": r["event_type"],
                "subject": r["subject"],
                "chapter": ch,
                "evidence": r["evidence"],
                "tags": json.loads(r["tags"] or "[]"),
                "trust_score": r["trust_score"],
                "updated_at": r["updated_at"],
                "source": r["source"] or "book",
            }
            if max_chapter is None or (ch is not None and ch <= max_chapter):
                seen.append(entry)
            elif ch is None:
                # 无锚点 = 定位不到时间线 = **无法证明**它在读者进度之内。
                # 旧行为是当「已见」放行（fail-open），剧透门对这类观察等于没有。
                # 现在只计数不展示 —— 不是丢弃，是「说不清就先不说」。
                unanchored += 1
            else:
                future_count += 1
        return seen, future_count, unanchored

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

        # Filter by max_chapter (spoiler gating) and collect。
        # ``chapter is None`` 也**滤掉** —— 与 get_node_detail / get_observations
        # 同一条规矩：定位不到时间线就无法证明安全。旧行为是放行（fail-open）。
        results: list[dict] = []
        for r in rows:
            chapter = r["anchor_chapter"]
            if max_chapter is not None and (chapter is None or chapter > max_chapter):
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

        Locates the node via ``locate_node`` (exact alias → exact
        canonical name → substring, most-connected first). Returns a
        flat dict ready for ``formatter.format_node`` - a flat view,
        not a visualization-oriented subgraph with neighbor grouping.

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

        # ── 1. Locate：精确别名 → 精确正名 → 子串（度数优先）─────────────
        row = self.locate_node(book_id, name)

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
            f"  e.occurrences AS occ, "
            f"  CASE WHEN e.source_id = ? THEN 'out' ELSE 'in' END AS direction, "
            f"  n_other.id AS other_id, "
            f"  n_other.canonical_name AS other_name, "
            f"  n_other.kind AS other_kind "
            f"FROM {TABLE_EDGES} e "
            f"JOIN {TABLE_NODES} n_other "
            f"  ON n_other.id = CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END "
            f"WHERE (e.source_id = ? OR e.target_id = ?) AND e.project_id = ? "
            f"ORDER BY e.chunk_id",
            (node_id, node_id, node_id, node_id, book_id),
        ).fetchall()

        # **同一条关系会被多个批次各写一遍**。写入侧现在按
        # (src, tgt, kind, event_type) 只留一行（`add_edge` 记 occurrences），
        # 这里是**旧库**的兜底：迁移 12 之前的行仍是每块一条，且
        # `event_type` 不同的同一条关系仍会分成两行（fact 与 perspective
        # 是两句不同的话）。所以仍按 (方向, 对方, 关系词) 合并 ——
        # 详情页要的是「他和谁是什么关系」，不是「这句话被说了几次」。
        merged: dict[tuple, dict] = {}
        unanchored_rel = 0
        timeline: set[int] = set()
        for r in edge_rows:
            ch = self._chapter_of_chunk(r["chunk_id"])
            if ch is not None:
                timeline.add(ch)
            key = (r["direction"], r["other_id"], r["kind"])
            cur = merged.get(key)
            if cur is None:
                merged[key] = {
                    "kind": r["kind"],
                    "direction": r["direction"],
                    "other_id": r["other_id"],
                    "other_name": r["other_name"],
                    "other_kind": r["other_kind"],
                    "chapter": ch,
                    "event_type": r["event_type"] or "fact",
                    "subject": r["subject"],
                    "occurrences": r["occ"] or 1,
                }
            else:
                cur["occurrences"] += r["occ"] or 1
                if ch is not None and (cur["chapter"] is None or ch < cur["chapter"]):
                    cur["chapter"] = ch

        # 对方节点的度数 —— 角色卡的排序要用它（「和阿贝尔的关系」比和
        # 一个只出现一次的路人更值得占一个名额）。
        other_ids = sorted({v["other_id"] for v in merged.values()
                            if v.get("other_id")})
        degree: dict[str, int] = {}
        # SQLite 的绑定参数上限是 999（一条查询里 IN 出现两次），超了就跳过
        # 度数这一项 —— 排序会退化到「次数 + 章号」，但不会把详情页打挂。
        if other_ids and len(other_ids) <= 400:
            marks = ",".join("?" * len(other_ids))
            for d in conn.execute(
                f"SELECT other_id, COUNT(*) AS c FROM ("
                f"  SELECT source_id AS other_id FROM {TABLE_EDGES} "
                f"  WHERE project_id=? AND source_id IN ({marks}) "
                f"  UNION ALL "
                f"  SELECT target_id AS other_id FROM {TABLE_EDGES} "
                f"  WHERE project_id=? AND target_id IN ({marks})"
                f") GROUP BY other_id",
                [book_id, *other_ids, book_id, *other_ids],
            ).fetchall():
                degree[d["other_id"]] = d["c"]

        relations: list[dict] = []
        future_rel_count = 0
        for entry in merged.values():
            ch = entry["chapter"]
            entry["other_degree"] = degree.get(entry.get("other_id"), 0)
            entry.pop("other_id", None)
            if max_chapter is None or (ch is not None and ch <= max_chapter):
                relations.append(entry)
            elif ch is None:
                unanchored_rel += 1      # 见 get_observations 的说明
            else:
                future_rel_count += 1

        # Stable sort: seen relations by chapter (None last)
        relations.sort(key=lambda e: (e["chapter"] is None, e["chapter"] or 0))

        if first_ch is not None:
            timeline.add(first_ch)

        # ── 3. Quotes (character only) → split seen vs. future ──────────
        quotes: list[dict] = []
        future_q_count = 0
        unanchored_q = 0
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
                if max_chapter is None or (ch is not None and ch <= max_chapter):
                    quotes.append(entry)
                elif ch is None:
                    unanchored_q += 1    # 见 get_observations 的说明
                else:
                    future_q_count += 1

            quotes.sort(key=lambda e: (e["chapter"] is None, e["chapter"] or 0))

        # ── 4. Observations (event model) -> split seen vs. future ──────
        observations, future_obs_count, unanchored_obs = self.get_observations(
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
            # 章号为 NULL 的条目（老提取器不要求锚点）。与 future_* 分开报 ——
            # 前者是「还不能说」，后者是「说不清」，混在一起会让人以为
            # 剧透门在正常工作。
            "unanchored_observations_count": unanchored_obs,
            "unanchored_relations_count": unanchored_rel,
            "unanchored_quotes_count": unanchored_q,
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
            # 与 get_node_detail 共用同一个解析器 —— 别名/精确/子串三档，
            # 否则 graph_trace("史铁生", "地坛") 会因为别名不认而直接报未找到。
            row = self.locate_node(book_id, name)
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

