"""WriteGraph 存储层 —— ``data/writing.db``，写作状态的**唯一写入口**。

三条纪律（见 ``docs/writegraph/README.md``）：

1. **投影不是真源。** 上下文卡、故事地图、章节队列都是派生物，
   冲突时以本库的账本为准。
2. **位置决定可见性。** 事实带 ``[valid_from, valid_until]`` 闭区间，
   求值只看当前写作位置；答不出就不展示（fail-closed）。
3. **证据必须逐字。** ``evidence_text`` 必须能在来源章节正文里原样找到；
   对不上整份 delta 拒绝，不降级入库。

本模块**只存不判**：delta 的校验与归约在 ``writegraph/state.py``（纯函数），
按位置求值在 ``writegraph/facts.py``，五类义务在 ``writegraph/promises.py``。
与 ``AuthorsDB`` 同一分工：判定在域包，落库在这里。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from storage.db_base import BaseDatabase, _now_ms

# ── 词表（schema 注释里也钉一份，供校验与展示复用）──────────────────────

WORK_STATUSES = ("planning", "writing", "paused", "finished")

CHAPTER_STATUSES = ("planned", "drafting", "drafted", "reviewed", "final", "stale")

FACT_SUBJECT_TYPES = ("character", "faction", "location", "item", "rule", "relation")

FACT_SOURCES = ("author", "delta", "derived", "import")

UNKNOWN_REASONS = ("nonexistent", "unclaimed", "before_start", "voided")

PROMISE_KINDS = ("foreshadow", "hook", "reader_promise", "debt")

PROMISE_STATUSES = ("planned", "planted", "reinforced", "deferred",
                    "resolved", "abandoned")

PROMISE_LAYERS = ("main", "sub", "easter")

SNAPSHOT_REASONS = ("manual", "before_write", "before_revision",
                    "before_restore", "milestone")

#: 写作 git 的提交类型。``checkpoint`` 是切分支/回滚前的自动提交；
#: ``restore`` 是回滚产生的**新**提交（永不改写历史）。
COMMIT_KINDS = ("manual", "auto", "checkpoint", "restore", "before_restore")

COMMIT_AUTHORS = ("user", "agent", "system")

DELIVERY_STATUSES = ("blocked", "inconclusive", "pass", "revise")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def split_chunks(text: str, *, size: int = 800) -> list[str]:
    """按空行优先、硬切兜底地分段。空行是作者的结构信号，尽量尊重它。"""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
            continue
        if buf:
            chunks.append(buf)
        while len(para) > size:
            chunks.append(para[:size])
            para = para[size:]
        buf = para
    if buf:
        chunks.append(buf)
    return chunks


# ── DDL v1 ────────────────────────────────────────────────────────────────
# 迁移规则：ADD COLUMN 只用恒定默认值；时间戳由写入口显式写入。

WRITING_DDL_V1: list[str] = [
    # ── 作品、卷、章、正文版本、场景 ──
    """
    CREATE TABLE IF NOT EXISTS works (
        id               TEXT PRIMARY KEY,
        title            TEXT NOT NULL,
        genre            TEXT NOT NULL DEFAULT '',
        premise          TEXT NOT NULL DEFAULT '',
        narrative_pov    TEXT NOT NULL DEFAULT '',
        style_profile    TEXT NOT NULL DEFAULT '{}',
        target_chars     INTEGER NOT NULL DEFAULT 0,
        status           TEXT NOT NULL DEFAULT 'planning',
        time_created     INTEGER NOT NULL,
        time_updated     INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS volumes (
        id               TEXT PRIMARY KEY,
        work_id          TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
        volume_index     INTEGER NOT NULL,
        title            TEXT NOT NULL DEFAULT '',
        plan             TEXT NOT NULL DEFAULT '{}',
        UNIQUE(work_id, volume_index)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS chapters (
        id                   TEXT PRIMARY KEY,
        work_id              TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
        volume_id            TEXT REFERENCES volumes(id) ON DELETE SET NULL,
        chapter_index        INTEGER NOT NULL,
        title                TEXT NOT NULL DEFAULT '',
        content              TEXT NOT NULL DEFAULT '',
        word_count           INTEGER NOT NULL DEFAULT 0,
        content_hash         TEXT NOT NULL DEFAULT '',
        status               TEXT NOT NULL DEFAULT 'planned',
        time_created         INTEGER NOT NULL,
        time_updated         INTEGER NOT NULL,
        UNIQUE(work_id, chapter_index)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS chapter_versions (
        id             TEXT PRIMARY KEY,
        chapter_id     TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        version        INTEGER NOT NULL,
        content        TEXT NOT NULL,
        content_hash   TEXT NOT NULL,
        reason         TEXT NOT NULL,
        snapshot_id    TEXT,
        label          TEXT NOT NULL DEFAULT '',
        time_created   INTEGER NOT NULL,
        UNIQUE(chapter_id, version)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS scenes (
        id             TEXT PRIMARY KEY,
        chapter_id     TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        scene_index    INTEGER NOT NULL,
        title          TEXT NOT NULL DEFAULT '',
        goal           TEXT NOT NULL DEFAULT '',
        conflict       TEXT NOT NULL DEFAULT '',
        reveal         TEXT NOT NULL DEFAULT '',
        emotion_beat   TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT 'planned',
        UNIQUE(chapter_id, scene_index)
    );
    """,
    # ── 章节合同 ──
    """
    CREATE TABLE IF NOT EXISTS chapter_contracts (
        chapter_id        TEXT PRIMARY KEY REFERENCES chapters(id) ON DELETE CASCADE,
        purpose           TEXT NOT NULL DEFAULT '',
        exclusive_event   TEXT NOT NULL DEFAULT '',
        ending_state      TEXT NOT NULL DEFAULT '',
        next_entry_state  TEXT NOT NULL DEFAULT '',
        story_time        TEXT NOT NULL DEFAULT '',
        target_chars      INTEGER NOT NULL DEFAULT 0,
        must_hit_now      TEXT NOT NULL DEFAULT '[]',
        must_preserve     TEXT NOT NULL DEFAULT '[]',
        forbidden_cross   TEXT NOT NULL DEFAULT '[]',
        source            TEXT NOT NULL DEFAULT 'planned',
        time_updated      INTEGER NOT NULL
    );
    """,
    # ── 位置化事实 ──
    # 未知也是一等公民：value / unknown_reason 二选一。两条 CHECK 必须分开写
    # —— ``(value IS NULL) <> (unknown_reason IS NULL)`` 在全 NULL 时求值为
    # NULL，而 SQLite 的 CHECK 只拒绝 FALSE，会静默放过「既不知道也不说」。
    """
    CREATE TABLE IF NOT EXISTS facts (
        id                  TEXT PRIMARY KEY,
        work_id             TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
        subject_type        TEXT NOT NULL,
        subject_ref         TEXT NOT NULL,
        field               TEXT NOT NULL,
        value               TEXT,
        unknown_reason      TEXT,
        valid_from_chapter  INTEGER NOT NULL,
        valid_until_chapter INTEGER,
        source_chapter      INTEGER NOT NULL,
        source_kind         TEXT NOT NULL DEFAULT 'delta',
        evidence_text       TEXT NOT NULL DEFAULT '',
        confidence          REAL NOT NULL DEFAULT 0.5,
        time_created        INTEGER NOT NULL,
        CHECK (value IS NOT NULL OR unknown_reason IS NOT NULL),
        CHECK (value IS NULL OR unknown_reason IS NULL),
        CHECK (valid_until_chapter IS NULL
               OR valid_until_chapter >= valid_from_chapter)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_facts_lookup
        ON facts(work_id, subject_ref, field, valid_from_chapter);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_facts_current
        ON facts(work_id, field, valid_until_chapter);
    """,
    # ── 承诺（伏笔/钩子/读者期待/欠债）──
    """
    CREATE TABLE IF NOT EXISTS promises (
        id                      TEXT PRIMARY KEY,
        work_id                 TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
        title                   TEXT NOT NULL,
        summary                 TEXT NOT NULL DEFAULT '',
        kind                    TEXT NOT NULL DEFAULT 'foreshadow',
        layer                   TEXT NOT NULL DEFAULT 'main',
        weight                  INTEGER NOT NULL DEFAULT 5,
        status                  TEXT NOT NULL DEFAULT 'planned',
        planned_plant_chapter   INTEGER,
        planted_chapter         INTEGER,
        earliest_reveal_chapter INTEGER,
        planned_payoff_chapter  INTEGER,
        payoff_chapter          INTEGER,
        last_touched_chapter    INTEGER,
        evidence_text           TEXT NOT NULL DEFAULT '',
        time_created            INTEGER NOT NULL,
        time_updated            INTEGER NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_promises_status
        ON promises(work_id, status, planned_payoff_chapter);
    """,
    """
    CREATE TABLE IF NOT EXISTS promise_edges (
        id          TEXT PRIMARY KEY,
        work_id     TEXT NOT NULL,
        src_id      TEXT NOT NULL REFERENCES promises(id) ON DELETE CASCADE,
        dst_id      TEXT NOT NULL REFERENCES promises(id) ON DELETE CASCADE,
        edge_kind   TEXT NOT NULL,
        UNIQUE(work_id, src_id, dst_id, edge_kind)
    );
    """,
    # ── 结算与快照 ──
    """
    CREATE TABLE IF NOT EXISTS chapter_summaries (
        chapter_id       TEXT PRIMARY KEY REFERENCES chapters(id) ON DELETE CASCADE,
        summary          TEXT NOT NULL DEFAULT '',
        key_events       TEXT NOT NULL DEFAULT '[]',
        state_delta      TEXT NOT NULL DEFAULT '{}',
        mood             TEXT NOT NULL DEFAULT '',
        chapter_type     TEXT NOT NULL DEFAULT '',
        source_hash      TEXT NOT NULL DEFAULT '',
        time_created     INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id             TEXT PRIMARY KEY,
        work_id        TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
        chapter_index  INTEGER NOT NULL,
        content_hash   TEXT NOT NULL,
        ledger_json    TEXT NOT NULL,
        reason         TEXT NOT NULL,
        time_created   INTEGER NOT NULL
    );
    """,
    # ── 审稿与修订 ──
    """
    CREATE TABLE IF NOT EXISTS review_runs (
        id              TEXT PRIMARY KEY,
        chapter_id      TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        content_hash    TEXT NOT NULL,
        domains_json    TEXT NOT NULL DEFAULT '[]',
        quality_score   REAL,
        coverage        REAL,
        gate_status     TEXT NOT NULL DEFAULT 'pass',
        delivery_status TEXT NOT NULL DEFAULT 'inconclusive',
        status          TEXT NOT NULL DEFAULT 'current',
        model           TEXT NOT NULL DEFAULT '',
        time_created    INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS review_findings (
        id          TEXT PRIMARY KEY,
        run_id      TEXT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
        severity    TEXT NOT NULL,
        category    TEXT NOT NULL,
        location    TEXT NOT NULL DEFAULT '',
        evidence    TEXT NOT NULL DEFAULT '',
        issue       TEXT NOT NULL,
        fix_hint    TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'open'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS revisions (
        id            TEXT PRIMARY KEY,
        chapter_id    TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        run_id        TEXT,
        anchor_start  INTEGER NOT NULL,
        anchor_end    INTEGER NOT NULL,
        anchor_quote  TEXT NOT NULL,
        action        TEXT NOT NULL,
        replacement   TEXT NOT NULL,
        diff_json     TEXT NOT NULL DEFAULT '{}',
        status        TEXT NOT NULL DEFAULT 'proposed',
        base_hash     TEXT NOT NULL,
        time_created  INTEGER NOT NULL,
        time_updated  INTEGER NOT NULL
    );
    """,
    # ── 段级索引与 FTS ──
    """
    CREATE TABLE IF NOT EXISTS chapter_chunks (
        id             TEXT PRIMARY KEY,
        chapter_id     TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        scene_id       TEXT,
        chunk_index    INTEGER NOT NULL,
        content        TEXT NOT NULL,
        summary        TEXT NOT NULL DEFAULT '',
        start_offset   INTEGER NOT NULL DEFAULT 0,
        end_offset     INTEGER NOT NULL DEFAULT 0,
        content_hash   TEXT NOT NULL DEFAULT '',
        UNIQUE(chapter_id, chunk_index)
    );
    """,
    # external content 的列名必须是 id（= chapter_chunks.id），沿用图谱库教训。
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS writing_chunks_fts USING fts5(
        id, content, summary,
        content='chapter_chunks', content_rowid='rowid',
        tokenize='trigram'
    );
    """,
    # external content 必须配三条触发器，否则 FTS 与主表会**静默**不同步
    # （图谱库踩过一次：搜索少结果，但没有任何报错）。
    """
    CREATE TRIGGER IF NOT EXISTS writing_chunks_ai
    AFTER INSERT ON chapter_chunks BEGIN
        INSERT INTO writing_chunks_fts(rowid, id, content, summary)
        VALUES (NEW.rowid, NEW.id, NEW.content, NEW.summary);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS writing_chunks_ad
    AFTER DELETE ON chapter_chunks BEGIN
        INSERT INTO writing_chunks_fts(writing_chunks_fts, rowid, id, content, summary)
        VALUES ('delete', OLD.rowid, OLD.id, OLD.content, OLD.summary);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS writing_chunks_au
    AFTER UPDATE ON chapter_chunks BEGIN
        INSERT INTO writing_chunks_fts(writing_chunks_fts, rowid, id, content, summary)
        VALUES ('delete', OLD.rowid, OLD.id, OLD.content, OLD.summary);
        INSERT INTO writing_chunks_fts(rowid, id, content, summary)
        VALUES (NEW.rowid, NEW.id, NEW.content, NEW.summary);
    END;
    """,
    # ── 装配账与运行记录 ──
    """
    CREATE TABLE IF NOT EXISTS context_manifests (
        id            TEXT PRIMARY KEY,
        chapter_id    TEXT NOT NULL,
        run_id        TEXT NOT NULL DEFAULT '',
        blocks_json   TEXT NOT NULL,
        total_tokens  INTEGER NOT NULL DEFAULT 0,
        time_created  INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS retrieval_traces (
        id            TEXT PRIMARY KEY,
        chapter_id    TEXT NOT NULL,
        query_digest  TEXT NOT NULL,
        candidates    INTEGER NOT NULL DEFAULT 0,
        selected_ids  TEXT NOT NULL DEFAULT '[]',
        scores_json   TEXT NOT NULL DEFAULT '{}',
        engine        TEXT NOT NULL DEFAULT 'fts5+graph',
        time_cost_ms  INTEGER NOT NULL DEFAULT 0,
        time_created  INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS writing_runs (
        id            TEXT PRIMARY KEY,
        work_id       TEXT NOT NULL,
        chapter_id    TEXT,
        kind          TEXT NOT NULL,
        status        TEXT NOT NULL,
        stage         TEXT NOT NULL DEFAULT '',
        input_hash    TEXT NOT NULL DEFAULT '',
        error         TEXT NOT NULL DEFAULT '',
        time_created  INTEGER NOT NULL,
        time_updated  INTEGER NOT NULL
    );
    """,
]

#: v2：紧张度曲线。放在独立表，正文与账本回滚时不必带着它一起回退。
WRITING_DDL_V2: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS chapter_tensions (
        chapter_id    TEXT PRIMARY KEY REFERENCES chapters(id) ON DELETE CASCADE,
        work_id       TEXT NOT NULL,
        value         INTEGER NOT NULL,
        source        TEXT NOT NULL DEFAULT 'ai',   -- ai | user
        note          TEXT NOT NULL DEFAULT '',
        time_updated  INTEGER NOT NULL,
        CHECK (value >= 0 AND value <= 100),
        CHECK (source IN ('ai', 'user'))
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tensions_work
        ON chapter_tensions(work_id);
    """,
]

#: v3：写作 git —— 项目级提交图、分支引用、当前 HEAD。
#: 内容是 ``chapter_versions`` 的指针，不在 commit 里复制正文。
WRITING_DDL_V3: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS commits (
        id             TEXT PRIMARY KEY,
        work_id        TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
        parent_id      TEXT,
        branch_name    TEXT NOT NULL DEFAULT 'main',
        kind           TEXT NOT NULL DEFAULT 'manual',
        author         TEXT NOT NULL DEFAULT 'user',
        message        TEXT NOT NULL DEFAULT '',
        chapter_index  INTEGER NOT NULL DEFAULT 0,
        tree_json      TEXT NOT NULL DEFAULT '[]',
        tree_hash      TEXT NOT NULL DEFAULT '',
        ledger_json    TEXT NOT NULL DEFAULT '{}',
        ledger_hash    TEXT NOT NULL DEFAULT '',
        time_created   INTEGER NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_commits_work
        ON commits(work_id, time_created DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_commits_branch
        ON commits(work_id, branch_name, time_created DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS refs (
        work_id      TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        kind         TEXT NOT NULL DEFAULT 'branch',
        commit_id    TEXT NOT NULL,
        time_created INTEGER NOT NULL,
        time_updated INTEGER NOT NULL,
        PRIMARY KEY (work_id, name)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS work_heads (
        work_id      TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
        branch_name  TEXT NOT NULL DEFAULT 'main',
        commit_id    TEXT,
        time_updated INTEGER NOT NULL
    );
    """,
]


#: v4：把「提交图」补成真正的图。
#: - ``parent2_id``：合并提交的第二个父节点。普通提交为空串。
#: - ``head_log``：HEAD / 分支指针每次移动留一条，等价于 reflog。
#: 只做 ADD COLUMN + CREATE TABLE，旧库无需回填。
WRITING_DDL_V4: list[str] = [
    "ALTER TABLE commits ADD COLUMN parent2_id TEXT NOT NULL DEFAULT ''",
    """
    CREATE TABLE IF NOT EXISTS head_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        work_id        TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
        branch_name    TEXT NOT NULL DEFAULT '',
        commit_id      TEXT,
        from_commit_id TEXT,
        reason         TEXT NOT NULL DEFAULT '',
        time_created   INTEGER NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_head_log_work
        ON head_log(work_id, id DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_refs_kind
        ON refs(work_id, kind, name);
    """,
]


#: v5：agent 的问题／建议日志。
#: 不是聊天记录——是可检索、可关单的「功能反馈」，由 agent 在自测或日常
#: 使用中写入；作者也能在版本面板里看到。
WRITING_DDL_V5: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS writing_feedback (
        id           TEXT PRIMARY KEY,
        work_id      TEXT NOT NULL DEFAULT '',
        source       TEXT NOT NULL DEFAULT 'agent',
        kind         TEXT NOT NULL DEFAULT 'issue',
        area         TEXT NOT NULL DEFAULT 'other',
        severity     TEXT NOT NULL DEFAULT 'normal',
        title        TEXT NOT NULL,
        body         TEXT NOT NULL DEFAULT '',
        context_json TEXT NOT NULL DEFAULT '{}',
        status       TEXT NOT NULL DEFAULT 'open',
        time_created INTEGER NOT NULL,
        CHECK (kind IN ('issue', 'suggestion', 'question')),
        CHECK (severity IN ('low', 'normal', 'high')),
        CHECK (status IN ('open', 'acknowledged', 'fixed', 'wontfix'))
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_feedback_work
        ON writing_feedback(work_id, time_created DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_feedback_status
        ON writing_feedback(status, time_created DESC);
    """,
]


#: v6：版本行分「暂存稿」与「保存点」。
#: - ``draft``：编辑器自动保存 / 动作前落盘 / 手动暂存写的；
#: - ``save``：手动保存、agent 写稿、合并、回滚等产生的。
#: 版本面板看的是提交，不是版本行；这一列只回答两个问题：
#: 「当前正文是不是只暂存过」「回到上次保存要回哪一版」。
#: 旧行一律按 ``save`` 处理（默认值），不需要回填。
WRITING_DDL_V6: list[str] = [
    "ALTER TABLE chapter_versions ADD COLUMN kind TEXT NOT NULL DEFAULT 'save'",
    """
    CREATE INDEX IF NOT EXISTS idx_versions_kind
        ON chapter_versions(chapter_id, kind, version DESC);
    """,
]


#: v7：写作台「置顶」。只影响选书列表的排序，不参与写作 git。
#: 取消置顶时不碰 time_updated，让作品回到按活动时间的自然位置。
WRITING_DDL_V7: list[str] = [
    "ALTER TABLE works ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
]


class WritingDB(BaseDatabase):
    """写作库 ``data/writing.db``。"""

    _MIGRATIONS: list[tuple[int, str, str | list[str]]] = [
        (1, "writegraph v1: works/ledger/ops", WRITING_DDL_V1),
        (2, "writegraph v2: tension curve", WRITING_DDL_V2),
        (3, "writegraph v3: commits / refs / work_heads", WRITING_DDL_V3),
        (4, "writegraph v4: merge parents / reflog / ref kinds", WRITING_DDL_V4),
        (5, "writegraph v5: agent feedback log", WRITING_DDL_V5),
        (6, "writegraph v6: draft vs save versions", WRITING_DDL_V6),
        (7, "writegraph v7: pinned works", WRITING_DDL_V7),
    ]

    def __init__(self, db_path: Path | str):
        super().__init__(Path(db_path))
        self.initialize()

    # ── 行读取 ────────────────────────────────────────────────────────────

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
        return [dict(r) for r in rows]

    # ── 作品 / 卷 / 章 ────────────────────────────────────────────────────

    def create_work(self, title: str, *, genre: str = "", premise: str = "",
                    narrative_pov: str = "", target_chars: int = 0,
                    style_profile: Mapping[str, Any] | None = None) -> str:
        work_id = _new_id("wk")
        now = _now_ms()
        self.get_conn().execute(
            "INSERT INTO works (id, title, genre, premise, narrative_pov,"
            " style_profile, target_chars, status, time_created, time_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (work_id, title, genre, premise, narrative_pov,
             _json(dict(style_profile or {})), target_chars, "planning",
             now, now),
        )
        self.get_conn().commit()
        return work_id

    def delete_work(self, work_id: str) -> None:
        """删作品。正文/版本/提交/账本靠外键级联；三张无外键的表手动清。"""
        conn = self.get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("context_manifests", "retrieval_traces"):
                conn.execute(
                    f"DELETE FROM {table} WHERE chapter_id IN"
                    " (SELECT id FROM chapters WHERE work_id=?)", (work_id,))
            conn.execute("DELETE FROM writing_runs WHERE work_id=?", (work_id,))
            conn.execute("DELETE FROM works WHERE id=?", (work_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def get_work(self, work_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute("SELECT * FROM works WHERE id=?", (work_id,))
        return self._row(cur.fetchone())

    def update_work_title(self, work_id: str, title: str) -> None:
        self.get_conn().execute(
            "UPDATE works SET title=?, time_updated=? WHERE id=?",
            (title, _now_ms(), work_id))
        self.get_conn().commit()

    def set_work_pinned(self, work_id: str, pinned: bool) -> None:
        """置顶只改排序，不算一次写作活动（不动 time_updated）。"""
        self.get_conn().execute(
            "UPDATE works SET pinned=? WHERE id=?",
            (1 if pinned else 0, work_id))
        self.get_conn().commit()

    def update_work_style_profile(self, work_id: str,
                                  profile: Mapping[str, Any]) -> None:
        """只改 ``style_profile``；``hard_rules`` / ``taboos`` 等键由调用方保留。"""
        self.get_conn().execute(
            "UPDATE works SET style_profile=?, time_updated=? WHERE id=?",
            (_json(dict(profile or {})), _now_ms(), work_id))
        self.get_conn().commit()

    def add_volume(self, work_id: str, volume_index: int, *,
                   title: str = "", plan: Mapping[str, Any] | None = None) -> str:
        volume_id = _new_id("vol")
        self.get_conn().execute(
            "INSERT INTO volumes (id, work_id, volume_index, title, plan)"
            " VALUES (?,?,?,?,?)",
            (volume_id, work_id, volume_index, title, _json(dict(plan or {}))),
        )
        self.get_conn().commit()
        return volume_id

    def add_chapter(self, work_id: str, chapter_index: int, *,
                    title: str = "", volume_id: str | None = None) -> str:
        chapter_id = _new_id("ch")
        now = _now_ms()
        self.get_conn().execute(
            "INSERT INTO chapters (id, work_id, volume_id, chapter_index,"
            " title, status, time_created, time_updated)"
            " VALUES (?,?,?,?,?,'planned',?,?)",
            (chapter_id, work_id, volume_id, chapter_index, title, now, now),
        )
        self.get_conn().commit()
        return chapter_id

    def get_chapter(self, work_id: str, chapter_index: int) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM chapters WHERE work_id=? AND chapter_index=?",
            (work_id, chapter_index),
        )
        return self._row(cur.fetchone())

    def get_chapter_by_id(self, chapter_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM chapters WHERE id=?", (chapter_id,))
        return self._row(cur.fetchone())

    def update_chapter_title(self, chapter_id: str, title: str) -> None:
        self.get_conn().execute(
            "UPDATE chapters SET title=?, time_updated=? WHERE id=?",
            (title, _now_ms(), chapter_id))
        self.get_conn().commit()

    # ── 章节合同 ──────────────────────────────────────────────────────────

    _CONTRACT_JSON = ("must_hit_now", "must_preserve", "forbidden_cross")

    def set_contract(self, chapter_id: str, **fields: Any) -> None:
        """写合同。列表字段接受 list 或 JSON 字符串，落库一律 JSON。"""
        allowed = {
            "purpose", "exclusive_event", "ending_state", "next_entry_state",
            "story_time", "target_chars", *self._CONTRACT_JSON,
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown contract fields: {sorted(unknown)}")
        for key in self._CONTRACT_JSON:
            val = fields.get(key)
            if isinstance(val, (list, tuple)):
                fields[key] = _json([str(x) for x in val])
            elif val is not None and not isinstance(val, str):
                raise TypeError(f"{key} must be list or JSON string")

        cols = ["chapter_id", *fields, "time_updated"]
        vals = [chapter_id, *fields.values(), _now_ms()]
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols[1:])
        self.get_conn().execute(
            f"INSERT INTO chapter_contracts ({','.join(cols)})"
            f" VALUES ({placeholders})"
            f" ON CONFLICT(chapter_id) DO UPDATE SET {updates}",
            vals,
        )
        self.get_conn().commit()

    def get_contract(self, chapter_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM chapter_contracts WHERE chapter_id=?", (chapter_id,))
        row = self._row(cur.fetchone())
        if row is None:
            return None
        for key in self._CONTRACT_JSON:
            try:
                row[key] = json.loads(row.get(key) or "[]")
            except (json.JSONDecodeError, TypeError):
                row[key] = []
        return row

    # ── 账本读取（求值在 writegraph.facts / promises）────────────────────

    def get_facts(self, work_id: str, *,
                  up_to_chapter: int | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT * FROM facts WHERE work_id=?"
               " AND valid_from_chapter <= COALESCE(?, 9223372036854775807)"
               " ORDER BY subject_ref, field, valid_from_chapter, id")
        cur = self.get_conn().execute(sql, (work_id, up_to_chapter))
        return self._rows(cur.fetchall())

    def summaries_for_work(self, work_id: str) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT s.*, c.chapter_index FROM chapter_summaries s"
            " JOIN chapters c ON c.id = s.chapter_id"
            " WHERE c.work_id=? ORDER BY c.chapter_index", (work_id,))
        return self._rows(cur.fetchall())

    def get_promises(self, work_id: str) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT * FROM promises WHERE work_id=? ORDER BY id", (work_id,))
        return self._rows(cur.fetchall())

    def get_promise(self, promise_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM promises WHERE id=?", (promise_id,))
        return self._row(cur.fetchone())

    def recent_summaries(self, work_id: str, before_chapter: int,
                         limit: int = 3) -> list[dict[str, Any]]:
        """取 ``before_chapter`` 之前最近 N 章的摘要，按章号升序返回。"""
        cur = self.get_conn().execute(
            "SELECT s.*, c.chapter_index, c.title AS chapter_title"
            " FROM chapter_summaries s"
            " JOIN chapters c ON c.id = s.chapter_id"
            " WHERE c.work_id=? AND c.chapter_index < ?"
            " ORDER BY c.chapter_index DESC LIMIT ?",
            (work_id, before_chapter, limit),
        )
        return list(reversed(self._rows(cur.fetchall())))

    # ── 作品列表与段级检索 ────────────────────────────────────────────────

    def list_works(self) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT w.*, COUNT(c.id) AS chapter_count,"
            " COALESCE(SUM(c.word_count), 0) AS total_words,"
            " MAX(c.time_updated) AS last_activity"
            " FROM works w LEFT JOIN chapters c ON c.work_id = w.id"
            " GROUP BY w.id ORDER BY w.pinned DESC,"
            " COALESCE(MAX(c.time_updated), w.time_updated) DESC")
        return self._rows(cur.fetchall())

    def list_chapters(self, work_id: str) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT id, volume_id, chapter_index, title, word_count, status,"
            " content_hash, time_updated FROM chapters"
            " WHERE work_id=? ORDER BY chapter_index", (work_id,))
        return self._rows(cur.fetchall())

    def list_volumes(self, work_id: str) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT * FROM volumes WHERE work_id=? ORDER BY volume_index",
            (work_id,))
        return self._rows(cur.fetchall())

    def list_scenes(self, chapter_id: str) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT * FROM scenes WHERE chapter_id=? ORDER BY scene_index",
            (chapter_id,))
        return self._rows(cur.fetchall())

    def list_scenes_for_work(self, work_id: str) -> list[dict[str, Any]]:
        """整本的场景一次取回（树视图用）：避免按章 N+1 查询。"""
        cur = self.get_conn().execute(
            "SELECT s.*, c.id AS chapter_id FROM scenes s"
            " JOIN chapters c ON c.id = s.chapter_id"
            " WHERE c.work_id=? ORDER BY c.chapter_index, s.scene_index",
            (work_id,))
        return self._rows(cur.fetchall())

    # ── 紧张度（P3）──────────────────────────────────────────────────────

    def set_tension(self, work_id: str, chapter_id: str, value: int, *,
                    source: str = "user", note: str = "") -> dict[str, Any]:
        """用户锚点永不被 AI 覆盖；AI 值可以重复刷新。"""
        value = int(value)
        if not 0 <= value <= 100:
            raise ValueError("紧张度必须在 0–100")
        if source not in ("ai", "user"):
            raise ValueError("source 只能是 ai / user")
        conn = self.get_conn()
        row = conn.execute(
            "SELECT source, value FROM chapter_tensions WHERE chapter_id=?",
            (chapter_id,)).fetchone()
        if row is not None and row["source"] == "user" and source == "ai":
            return {"chapter_id": chapter_id, "value": row["value"],
                    "source": "user", "skipped": True}
        conn.execute(
            "INSERT INTO chapter_tensions (chapter_id, work_id, value, source,"
            " note, time_updated) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(chapter_id) DO UPDATE SET value=excluded.value,"
            " source=excluded.source, note=excluded.note,"
            " time_updated=excluded.time_updated",
            (chapter_id, work_id, value, source, note, _now_ms()))
        conn.commit()
        return {"chapter_id": chapter_id, "value": value, "source": source,
                "skipped": False}

    def get_tensions(self, work_id: str) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT * FROM chapter_tensions WHERE work_id=?"
            " ORDER BY chapter_id", (work_id,))
        return self._rows(cur.fetchall())

    def search_chunks(self, work_id: str, query: str, *,
                      before_chapter: int | None = None,
                      limit: int = 60) -> list[dict[str, Any]]:
        """作品原文召回。``before_chapter`` 为闭区间上界（含该章）。"""
        if not (query or "").strip():
            return []
        upper = 9223372036854775807 if before_chapter is None else before_chapter
        cur = self.get_conn().execute(
            "SELECT c.id, c.chapter_id, c.chunk_index, c.content, c.summary,"
            " ch.chapter_index, bm25(writing_chunks_fts) AS rank"
            " FROM writing_chunks_fts"
            " JOIN chapter_chunks c ON c.rowid = writing_chunks_fts.rowid"
            " JOIN chapters ch ON ch.id = c.chapter_id"
            " WHERE writing_chunks_fts MATCH ?"
            " AND ch.work_id=? AND ch.chapter_index <= ?"
            " ORDER BY rank LIMIT ?",
            (query, work_id, upper, limit))
        return self._rows(cur.fetchall())

    # ── 写作 git：树 / 账本快照 ──────────────────────────────────────────

    def current_tree(self, work_id: str) -> list[dict[str, Any]]:
        """当前工作区：每章 + 最新章节版本指针。空章也会出现（version 0）。"""
        cur = self.get_conn().execute(
            "SELECT ch.id AS chapter_id, ch.chapter_index, ch.title, ch.status,"
            " ch.volume_id, ch.content_hash, ch.word_count,"
            " cv.id AS version_id, cv.version AS version"
            " FROM chapters ch"
            " LEFT JOIN chapter_versions cv ON cv.id = ("
            "   SELECT id FROM chapter_versions WHERE chapter_id=ch.id"
            "   ORDER BY version DESC LIMIT 1)"
            " WHERE ch.work_id=? ORDER BY ch.chapter_index",
            (work_id,))
        return self._rows(cur.fetchall())

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM chapter_versions WHERE id=?", (version_id,))
        return self._row(cur.fetchone())

    def snapshot_ledger(self, work_id: str) -> dict[str, list[dict[str, Any]]]:
        """联合账本快照：合同 + 事实 + 承诺 + 承诺边。"""
        conn = self.get_conn()
        contracts = conn.execute(
            "SELECT cc.* FROM chapter_contracts cc"
            " JOIN chapters ch ON ch.id=cc.chapter_id"
            " WHERE ch.work_id=? ORDER BY cc.chapter_id", (work_id,)).fetchall()
        facts = conn.execute(
            "SELECT * FROM facts WHERE work_id=? ORDER BY id",
            (work_id,)).fetchall()
        promises = conn.execute(
            "SELECT * FROM promises WHERE work_id=? ORDER BY id",
            (work_id,)).fetchall()
        edges = conn.execute(
            "SELECT * FROM promise_edges WHERE work_id=? ORDER BY id",
            (work_id,)).fetchall()
        return {
            "contracts": self._rows(contracts),
            "facts": self._rows(facts),
            "promises": self._rows(promises),
            "promise_edges": self._rows(edges),
        }

    @staticmethod
    def _table_columns(conn: Any, table: str) -> list[str]:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]

    def _restore_rows(self, conn: Any, table: str,
                      rows: Iterable[Mapping[str, Any]]) -> None:
        allowed = self._table_columns(conn, table)
        for row in rows:
            keys = [k for k in row if k in allowed]
            if not keys:
                continue
            cols = ",".join(keys)
            marks = ",".join("?" for _ in keys)
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})",
                [row[k] for k in keys])

    def replace_ledger(self, work_id: str,
                       ledger: Mapping[str, Any]) -> None:
        """用快照整体替换本作品的账本（回滚/切分支用）。"""
        conn = self.get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM promise_edges WHERE work_id=?", (work_id,))
            conn.execute("DELETE FROM promises WHERE work_id=?", (work_id,))
            conn.execute("DELETE FROM facts WHERE work_id=?", (work_id,))
            conn.execute(
                "DELETE FROM chapter_contracts WHERE chapter_id IN"
                " (SELECT id FROM chapters WHERE work_id=?)", (work_id,))
            self._restore_rows(conn, "chapter_contracts",
                               ledger.get("contracts") or [])
            self._restore_rows(conn, "facts", ledger.get("facts") or [])
            self._restore_rows(conn, "promises", ledger.get("promises") or [])
            self._restore_rows(conn, "promise_edges",
                               ledger.get("promise_edges") or [])
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── 写作 git：提交 / 分支 / HEAD ─────────────────────────────────────

    def create_commit(self, work_id: str, *, tree: Iterable[Mapping[str, Any]],
                      ledger: Mapping[str, Any], parent_id: str | None = None,
                      parent2_id: str | None = None,
                      branch_name: str = "main", kind: str = "manual",
                      author: str = "user", message: str = "",
                      chapter_index: int = 0,
                      move_head: bool = True,
                      log_from: str | None = None) -> dict[str, Any]:
        """写一条提交并（默认）推进分支与 HEAD。内容是版本指针，不复制正文。

        ``move_head=False`` 用于暂存（stash）这类「只入图、不动指针」的快照。
        """
        from writegraph.versioning import normalize_tree, tree_hash, ledger_hash
        norm = normalize_tree(tree)
        th = tree_hash(norm)
        lh = ledger_hash(ledger)
        commit_id = _new_id("cm")
        now = _now_ms()
        conn = self.get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO commits (id, work_id, parent_id, parent2_id,"
                " branch_name, kind, author, message, chapter_index,"
                " tree_json, tree_hash, ledger_json, ledger_hash,"
                " time_created)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (commit_id, work_id, parent_id, parent2_id or "",
                 branch_name, kind, author, message, int(chapter_index or 0),
                 _json(norm), th, _json(dict(ledger)), lh, now))
            if move_head:
                conn.execute(
                    "INSERT INTO refs (work_id, name, kind, commit_id,"
                    " time_created, time_updated) VALUES (?,?,'branch',?,?,?)"
                    " ON CONFLICT(work_id, name) DO UPDATE SET"
                    " commit_id=excluded.commit_id,"
                    " time_updated=excluded.time_updated",
                    (work_id, branch_name, commit_id, now, now))
                conn.execute(
                    "INSERT INTO work_heads (work_id, branch_name, commit_id,"
                    " time_updated) VALUES (?,?,?,?)"
                    " ON CONFLICT(work_id) DO UPDATE SET"
                    " branch_name=excluded.branch_name,"
                    " commit_id=excluded.commit_id,"
                    " time_updated=excluded.time_updated",
                    (work_id, branch_name, commit_id, now))
                conn.execute(
                    "INSERT INTO head_log (work_id, branch_name, commit_id,"
                    " from_commit_id, reason, time_created)"
                    " VALUES (?,?,?,?,?,?)",
                    (work_id, branch_name, commit_id,
                     parent_id if log_from is None else log_from, kind, now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_commit(commit_id) or {}

    def get_commit(self, commit_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM commits WHERE id=?", (commit_id,))
        return self._row(cur.fetchone())

    def list_commits(self, work_id: str, *, branch: str | None = None,
                     limit: int = 100,
                     exclude_kinds: Iterable[str] = ()) -> list[dict[str, Any]]:
        sql = "SELECT * FROM commits WHERE work_id=?"
        params: list[Any] = [work_id]
        if branch:
            sql += " AND branch_name=?"
            params.append(branch)
        kinds = [k for k in exclude_kinds if k]
        if kinds:
            sql += f" AND kind NOT IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        sql += " ORDER BY time_created DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        return self._rows(self.get_conn().execute(sql, params).fetchall())

    def get_ref(self, work_id: str, name: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM refs WHERE work_id=? AND name=?", (work_id, name))
        return self._row(cur.fetchone())

    def upsert_ref(self, work_id: str, name: str, commit_id: str, *,
                   kind: str = "branch") -> None:
        now = _now_ms()
        self.get_conn().execute(
            "INSERT INTO refs (work_id, name, kind, commit_id,"
            " time_created, time_updated) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(work_id, name) DO UPDATE SET"
            " commit_id=excluded.commit_id,"
            " time_updated=excluded.time_updated",
            (work_id, name, kind, commit_id, now, now))
        self.get_conn().commit()

    def list_branches(self, work_id: str) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT r.name, r.kind, r.commit_id, r.time_updated, c.message,"
            " c.time_created FROM refs r"
            " LEFT JOIN commits c ON c.id=r.commit_id"
            " WHERE r.work_id=? AND r.kind='branch' ORDER BY r.name",
            (work_id,))
        return self._rows(cur.fetchall())

    def list_refs(self, work_id: str, *,
                  kind: str | None = None) -> list[dict[str, Any]]:
        """分支 / 标签 / 暂存的统一读取。按更新时间倒序。"""
        sql = ("SELECT r.name, r.kind, r.commit_id, r.time_created,"
               " r.time_updated, c.message, c.kind AS commit_kind,"
               " c.author, c.time_created AS commit_time"
               " FROM refs r LEFT JOIN commits c ON c.id=r.commit_id"
               " WHERE r.work_id=?")
        params: list[Any] = [work_id]
        if kind:
            sql += " AND r.kind=?"
            params.append(kind)
        sql += " ORDER BY r.time_updated DESC"
        return self._rows(self.get_conn().execute(sql, params).fetchall())

    def rename_ref(self, work_id: str, old: str, new: str) -> None:
        """重命名引用。分支还要同步提交上的 branch_name 与当前 HEAD。"""
        conn = self.get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.get_ref(work_id, old)
            if row is None:
                raise ValueError(f"引用不存在：{old}")
            conn.execute(
                "UPDATE refs SET name=?, time_updated=? WHERE work_id=? AND name=?",
                (new, _now_ms(), work_id, old))
            if row.get("kind") == "branch":
                conn.execute(
                    "UPDATE commits SET branch_name=? WHERE work_id=?"
                    " AND branch_name=?", (new, work_id, old))
                conn.execute(
                    "UPDATE work_heads SET branch_name=? WHERE work_id=?"
                    " AND branch_name=?", (new, work_id, old))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def delete_ref(self, work_id: str, name: str) -> None:
        self.get_conn().execute(
            "DELETE FROM refs WHERE work_id=? AND name=?", (work_id, name))
        self.get_conn().commit()

    def log_head(self, work_id: str, *, branch_name: str,
                 commit_id: str | None, from_commit_id: str | None,
                 reason: str) -> None:
        self.get_conn().execute(
            "INSERT INTO head_log (work_id, branch_name, commit_id,"
            " from_commit_id, reason, time_created) VALUES (?,?,?,?,?,?)",
            (work_id, branch_name or "", commit_id, from_commit_id or "",
             reason or "", _now_ms()))
        self.get_conn().commit()

    def list_head_log(self, work_id: str,
                      limit: int = 50) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT * FROM head_log WHERE work_id=? ORDER BY id DESC LIMIT ?",
            (work_id, int(limit)))
        return self._rows(cur.fetchall())

    # ── agent 反馈日志 ────────────────────────────────────────────────────

    def add_feedback(self, *, work_id: str = "", source: str = "agent",
                     kind: str = "issue", area: str = "other",
                     severity: str = "normal", title: str = "",
                     body: str = "", context: Mapping[str, Any] | None = None,
                     ) -> dict[str, Any]:
        from writegraph.versioning import canonical
        feedback_id = _new_id("fb")
        self.get_conn().execute(
            "INSERT INTO writing_feedback (id, work_id, source, kind, area,"
            " severity, title, body, context_json, status, time_created)"
            " VALUES (?,?,?,?,?,?,?,?,?,'open',?)",
            (feedback_id, work_id or "", source, kind, area, severity,
             title, body, canonical(dict(context or {})), _now_ms()))
        self.get_conn().commit()
        return self.get_feedback(feedback_id) or {}

    def get_feedback(self, feedback_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM writing_feedback WHERE id=?", (feedback_id,))
        return self._row(cur.fetchone())

    def list_feedback(self, *, work_id: str = "", status: str = "",
                      limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM writing_feedback WHERE 1=1"
        params: list[Any] = []
        if work_id:
            sql += " AND work_id=?"
            params.append(work_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY time_created DESC LIMIT ?"
        params.append(int(limit))
        return self._rows(self.get_conn().execute(sql, params).fetchall())

    def set_feedback_status(self, feedback_id: str,
                            status: str) -> dict[str, Any] | None:
        if status not in ("open", "acknowledged", "fixed", "wontfix"):
            raise ValueError(f"未知状态：{status}")
        self.get_conn().execute(
            "UPDATE writing_feedback SET status=? WHERE id=?",
            (status, feedback_id))
        self.get_conn().commit()
        return self.get_feedback(feedback_id)

    def get_head(self, work_id: str) -> dict[str, Any]:
        cur = self.get_conn().execute(
            "SELECT * FROM work_heads WHERE work_id=?", (work_id,))
        row = self._row(cur.fetchone())
        return row or {"work_id": work_id, "branch_name": "main",
                       "commit_id": None, "time_updated": 0}

    def set_head(self, work_id: str, branch_name: str,
                 commit_id: str | None) -> None:
        self.get_conn().execute(
            "INSERT INTO work_heads (work_id, branch_name, commit_id,"
            " time_updated) VALUES (?,?,?,?)"
            " ON CONFLICT(work_id) DO UPDATE SET"
            " branch_name=excluded.branch_name,"
            " commit_id=excluded.commit_id,"
            " time_updated=excluded.time_updated",
            (work_id, branch_name, commit_id, _now_ms()))
        self.get_conn().commit()

    def set_chapter_meta(self, chapter_id: str, *, title: str,
                         status: str, volume_id: str | None = None) -> None:
        self.get_conn().execute(
            "UPDATE chapters SET title=?, status=?, volume_id=?,"
            " time_updated=? WHERE id=?",
            (title, status, volume_id, _now_ms(), chapter_id))
        self.get_conn().commit()

    def delete_summary(self, chapter_id: str) -> None:
        self.get_conn().execute(
            "DELETE FROM chapter_summaries WHERE chapter_id=?", (chapter_id,))
        self.get_conn().commit()

    # ── 事务 A：正文 ──────────────────────────────────────────────────────

    def save_draft(self, work_id: str, chapter_id: str, content: str, *,
                   reason: str = "draft",
                   snapshot_id: str | None = None,
                   kind: str = "save") -> dict[str, Any]:
        """保存正文：先落版本，再换当前正文与段级索引，同一事务。

        ``kind`` 区分暂存与保存：``draft`` 是编辑器自动落盘，
        ``save`` 是作者按了保存或 agent 真正改了稿。内容没变时不会新增版本；
        若这次是「保存」而最新一版还只是暂存稿，就把它就地升成保存点。
        """
        if kind not in ("draft", "save"):
            raise ValueError(f"未知版本类型：{kind}")
        conn = self.get_conn()
        now = _now_ms()
        digest = text_hash(content)
        parts = split_chunks(content)

        conn.execute("BEGIN IMMEDIATE")
        try:
            # 幂等：自动保存会反复提交同一份正文。内容没变就不新增版本，
            # 否则每次停顿都会在版本表里留一行噪声。
            current = conn.execute(
                "SELECT content_hash, word_count FROM chapters"
                " WHERE id=? AND work_id=?", (chapter_id, work_id)).fetchone()
            if current is not None and current[0] == digest:
                latest = conn.execute(
                    "SELECT id, version, kind FROM chapter_versions"
                    " WHERE chapter_id=? ORDER BY version DESC LIMIT 1",
                    (chapter_id,)).fetchone()
                if latest is not None:
                    latest_kind = str(latest[2] or "save")
                    if kind == "save" and latest_kind != "save":
                        conn.execute(
                            "UPDATE chapter_versions SET kind='save' WHERE id=?",
                            (latest[0],))
                        latest_kind = "save"
                    conn.commit()
                    return {"version": int(latest[1]),
                            "version_id": latest[0],
                            "content_hash": digest,
                            "chunks": len(parts),
                            "word_count": int(current[1] or len(content)),
                            "kind": latest_kind,
                            "unchanged": True}
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM chapter_versions"
                " WHERE chapter_id=?", (chapter_id,)).fetchone()
            version = int(row[0]) + 1
            version_id = _new_id("cv")
            conn.execute(
                "INSERT INTO chapter_versions (id, chapter_id, version, content,"
                " content_hash, reason, snapshot_id, time_created, kind)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (version_id, chapter_id, version, content, digest, reason,
                 snapshot_id, now, kind))
            conn.execute(
                "UPDATE chapters SET content=?, content_hash=?, word_count=?,"
                " status='drafted', time_updated=? WHERE id=? AND work_id=?",
                (content, digest, len(content), now, chapter_id, work_id))
            # 正文变了，旧审稿结论立刻失效 —— 不靠 UI 用 hash 比对才发现。
            conn.execute(
                "UPDATE review_runs SET status='stale'"
                " WHERE chapter_id=? AND content_hash != ? AND status='current'",
                (chapter_id, digest))
            conn.execute("DELETE FROM chapter_chunks WHERE chapter_id=?",
                         (chapter_id,))
            cursor = 0
            for idx, part in enumerate(parts):
                start = content.find(part, cursor)
                if start < 0:
                    start = cursor
                end = start + len(part)
                cursor = end
                conn.execute(
                    "INSERT INTO chapter_chunks (id, chapter_id, chunk_index,"
                    " content, start_offset, end_offset, content_hash)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (_new_id("ck"), chapter_id, idx, part, start, end,
                     text_hash(part)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"version": version, "version_id": version_id,
                "content_hash": digest, "chunks": len(parts),
                "word_count": len(content), "kind": kind, "unchanged": False}

    def latest_saved_version(self, chapter_id: str) -> dict[str, Any] | None:
        """这一章最近一次「保存点」（不含暂存稿）。没保存过就返回 None。"""
        cur = self.get_conn().execute(
            "SELECT * FROM chapter_versions WHERE chapter_id=? AND kind='save'"
            " ORDER BY version DESC LIMIT 1", (chapter_id,))
        return self._row(cur.fetchone())

    # ── 审稿与修订 ────────────────────────────────────────────────────────

    def mark_reviews_stale(self, chapter_id: str) -> None:
        self.get_conn().execute(
            "UPDATE review_runs SET status='stale'"
            " WHERE chapter_id=? AND status='current'", (chapter_id,))
        self.get_conn().commit()

    def save_review(self, chapter_id: str, content_hash: str, *,
                    model: str, result: Mapping[str, Any]) -> str:
        conn = self.get_conn()
        run_id = _new_id("rv")
        now = _now_ms()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE review_runs SET status='stale'"
                " WHERE chapter_id=? AND status='current'", (chapter_id,))
            conn.execute(
                "INSERT INTO review_runs (id, chapter_id, content_hash,"
                " domains_json, quality_score, coverage, gate_status,"
                " delivery_status, status, model, time_created)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, chapter_id, content_hash,
                 _json(result.get("criteria") or []),
                 result.get("quality_score"), result.get("coverage"),
                 result.get("gate_status"), result.get("delivery_status"),
                 "current", model, now))
            for item in result.get("findings") or []:
                conn.execute(
                    "INSERT INTO review_findings (id, run_id, severity,"
                    " category, location, evidence, issue, fix_hint, status)"
                    " VALUES (?,?,?,?,?,?,?,?, 'open')",
                    (_new_id("fd"), run_id, item.get("severity"),
                     item.get("category"), item.get("location", ""),
                     item.get("evidence", ""), item.get("issue", ""),
                     item.get("fix_hint", "")))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return run_id

    def get_review(self, chapter_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM review_runs WHERE chapter_id=?"
            " ORDER BY time_created DESC LIMIT 1", (chapter_id,))
        run = self._row(cur.fetchone())
        if run is None:
            return None
        rows = self.get_conn().execute(
            "SELECT * FROM review_findings WHERE run_id=? ORDER BY"
            " CASE severity WHEN 'S1' THEN 1 WHEN 'S2' THEN 2"
            " WHEN 'S3' THEN 3 ELSE 4 END, id", (run["id"],))
        run["findings"] = self._rows(rows.fetchall())
        try:
            run["criteria"] = json.loads(run.get("domains_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            run["criteria"] = []
        return run

    def create_revision(self, chapter_id: str, *, run_id: str | None,
                        anchor_quote: str, replacement: str,
                        action: str = "custom") -> dict[str, Any]:
        conn = self.get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT content, content_hash FROM chapters WHERE id=?",
                (chapter_id,)).fetchone()
            if row is None:
                raise ValueError("章节不存在")
            content = row["content"] or ""
            if not anchor_quote or content.count(anchor_quote) != 1:
                raise ValueError("锚点必须在正文中恰好出现一次")
            start = content.find(anchor_quote)
            rev_id = _new_id("re")
            now = _now_ms()
            conn.execute(
                "INSERT INTO revisions (id, chapter_id, run_id, anchor_start,"
                " anchor_end, anchor_quote, action, replacement, status,"
                " base_hash, time_created, time_updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (rev_id, chapter_id, run_id, start, start + len(anchor_quote),
                 anchor_quote, action, replacement, "proposed",
                 row["content_hash"], now, now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_revision(rev_id) or {}

    def get_finding(self, finding_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM review_findings WHERE id=?", (finding_id,))
        return self._row(cur.fetchone())

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        cur = self.get_conn().execute(
            "SELECT * FROM revisions WHERE id=?", (revision_id,))
        return self._row(cur.fetchone())

    def list_revisions(self, chapter_id: str) -> list[dict[str, Any]]:
        cur = self.get_conn().execute(
            "SELECT * FROM revisions WHERE chapter_id=?"
            " ORDER BY time_created DESC", (chapter_id,))
        return self._rows(cur.fetchall())

    def reject_revision(self, revision_id: str) -> None:
        self.get_conn().execute(
            "UPDATE revisions SET status='rejected', time_updated=?"
            " WHERE id=? AND status='proposed'", (_now_ms(), revision_id))
        self.get_conn().commit()

    def apply_revision(self, revision_id: str, *,
                       expected_hash: str | None = None) -> dict[str, Any]:
        """应用提案：唯一锚点 + 乐观锁，缺一不可。"""
        rev = self.get_revision(revision_id)
        if rev is None:
            raise ValueError("修订不存在")
        if rev["status"] != "proposed":
            raise ValueError(f"修订状态为 {rev['status']}，不能应用")
        chapter = self.get_chapter_by_id(rev["chapter_id"])
        if chapter is None:
            raise ValueError("章节不存在")
        current_hash = str(chapter.get("content_hash") or "")
        if current_hash != rev["base_hash"]:
            raise ValueError("正文已变化，修订已过期（乐观锁）")
        if expected_hash and expected_hash != current_hash:
            raise ValueError("正文哈希不匹配")
        content = str(chapter.get("content") or "")
        quote = str(rev["anchor_quote"])
        if content.count(quote) != 1:
            raise ValueError("锚点已不唯一，拒绝应用")

        new_content = content.replace(quote, str(rev["replacement"]), 1)
        saved = self.save_draft(chapter["work_id"], chapter["id"], new_content,
                                reason="ai_revision")
        conn = self.get_conn()
        now = _now_ms()
        conn.execute("UPDATE revisions SET status='applied', time_updated=?"
                     " WHERE id=?", (now, revision_id))
        conn.execute("UPDATE revisions SET status='rejected', time_updated=?"
                     " WHERE chapter_id=? AND status='proposed' AND id!=?",
                     (now, chapter["id"], revision_id))
        conn.execute("UPDATE review_runs SET status='stale'"
                     " WHERE chapter_id=? AND status='current'",
                     (chapter["id"],))
        conn.commit()
        return {"revision": self.get_revision(revision_id), **saved,
                "content": new_content}

    def apply_delta(self, work_id: str, chapter_id: str, chapter_index: int, *,
                    delta: Mapping[str, Any], content_hash: str = "",
                    source_chapter: int | None = None) -> dict[str, Any]:
        """把**已校验**的 delta 落库。校验在 ``writegraph.state.validate_delta``。

        整个结算是一个事务：任何一条写失败都回滚，账本不会停在半路。
        事实只关区间、不改旧值；承诺 id 由这里分配（模型只给临时 ref）。
        """
        conn = self.get_conn()
        now = _now_ms()
        src_ch = int(source_chapter if source_chapter is not None else chapter_index)
        promise_ids: dict[str, str] = {}
        written: dict[str, int] = {"facts": 0, "promises": 0}

        conn.execute("BEGIN IMMEDIATE")
        try:
            for op in delta.get("fact_ops", []):
                self._apply_fact_op(conn, work_id, src_ch, op)
                written["facts"] += 1

            for op in delta.get("promise_ops", []):
                self._apply_promise_op(conn, work_id, chapter_index, op,
                                       promise_ids, now)
                written["promises"] += 1

            summary = str(delta.get("chapter_summary") or "").strip()
            if summary:
                conn.execute(
                    "INSERT INTO chapter_summaries"
                    " (chapter_id, summary, key_events, state_delta, source_hash,"
                    "  time_created) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(chapter_id) DO UPDATE SET"
                    " summary=excluded.summary, key_events=excluded.key_events,"
                    " state_delta=excluded.state_delta,"
                    " source_hash=excluded.source_hash,"
                    " time_created=excluded.time_created",
                    (chapter_id, summary,
                     _json(list(delta.get("key_events") or [])),
                     _json(dict(delta)), content_hash, now),
                )
            conn.execute(
                "UPDATE chapters SET status='drafted', time_updated=?"
                " WHERE id=? AND status IN ('planned','drafting','stale')",
                (now, chapter_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return {"ok": True, "written": written, "promise_ids": promise_ids}

    @staticmethod
    def _apply_fact_op(conn: Any, work_id: str, source_chapter: int,
                       op: Mapping[str, Any]) -> None:
        """单条事实操作。区间是闭区间，``set`` 自动把旧值关到 ``K-1``。"""
        kind = op.get("op")
        subject_ref = str(op.get("subject_ref") or "")
        field = str(op.get("field") or "")
        if not subject_ref or not field:
            raise ValueError("fact op 缺 subject_ref / field")

        if kind == "close":
            until = int(op["until_chapter"])
            conn.execute(
                "UPDATE facts SET valid_until_chapter=?"
                " WHERE work_id=? AND subject_ref=? AND field=?"
                " AND valid_from_chapter <= ?"
                " AND (valid_until_chapter IS NULL OR valid_until_chapter > ?)",
                (until, work_id, subject_ref, field, until, until),
            )
            return

        if kind not in ("set", "unknown"):
            raise ValueError(f"unknown fact op: {kind!r}")

        since = int(op.get("since_chapter", source_chapter))
        # 旧值只关区间，绝不 UPDATE value；since=0 时没有「之前」。
        if since > 0:
            conn.execute(
                "UPDATE facts SET valid_until_chapter=?"
                " WHERE work_id=? AND subject_ref=? AND field=?"
                " AND valid_from_chapter < ?"
                " AND (valid_until_chapter IS NULL OR valid_until_chapter >= ?)",
                (since - 1, work_id, subject_ref, field, since, since),
            )
        if kind == "set":
            value, reason = str(op.get("value") or ""), None
        else:
            value, reason = None, str(op.get("unknown_reason") or "unclaimed")

        conn.execute(
            "INSERT INTO facts (id, work_id, subject_type, subject_ref, field,"
            " value, unknown_reason, valid_from_chapter, source_chapter,"
            " source_kind, evidence_text, confidence, time_created)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("ft"), work_id, str(op.get("subject_type") or "character"),
             subject_ref, field, value, reason, since, source_chapter,
             str(op.get("source_kind") or "delta"),
             str(op.get("evidence_text") or ""),
             float(op.get("confidence", 0.5)), _now_ms()),
        )

    @staticmethod
    def _apply_promise_op(conn: Any, work_id: str, chapter: int,
                          op: Mapping[str, Any], ids: dict[str, str],
                          now: int) -> None:
        """承诺状态机。语义照 inkos：mention 不算推进，defer 不更新 touched。"""
        kind = op.get("op")
        ref = str(op.get("id") or op.get("ref") or "")

        if kind == "plant":
            real = _new_id("pm")
            if ref:
                ids[ref] = real
            conn.execute(
                "INSERT INTO promises (id, work_id, title, summary, kind, layer,"
                " weight, status, planned_plant_chapter, planted_chapter,"
                " earliest_reveal_chapter, planned_payoff_chapter,"
                " last_touched_chapter, evidence_text, time_created, time_updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (real, work_id, str(op.get("title") or "未命名承诺"),
                 str(op.get("summary") or ""),
                 str(op.get("kind") or "foreshadow"),
                 str(op.get("layer") or "main"),
                 int(op.get("weight", 5)), "planted",
                 op.get("planned_plant_chapter", chapter),
                 chapter, op.get("earliest_reveal_chapter"),
                 op.get("planned_payoff_chapter"), chapter,
                 str(op.get("evidence_text") or ""), now, now),
            )
            return

        real = ids.get(ref, ref)
        if not real:
            raise ValueError("promise op 缺 id")
        if kind in ("advance", "reinforce"):
            # planned/deferred 第一次推进 = 已埋；其余 = 加强。
            conn.execute(
                "UPDATE promises SET"
                " status=CASE WHEN status IN ('planned','deferred')"
                "             THEN 'planted' ELSE 'reinforced' END,"
                " planted_chapter=COALESCE(planted_chapter, ?),"
                " last_touched_chapter=?, time_updated=?"
                " WHERE id=? AND work_id=?"
                " AND status NOT IN ('resolved','abandoned')",
                (chapter, chapter, now, real, work_id),
            )
        elif kind == "defer":
            conn.execute(
                "UPDATE promises SET status='deferred', time_updated=?"
                " WHERE id=? AND work_id=?"
                " AND status NOT IN ('resolved','abandoned')",
                (now, real, work_id),
            )
        elif kind == "resolve":
            conn.execute(
                "UPDATE promises SET status='resolved', payoff_chapter=?,"
                " last_touched_chapter=?, time_updated=?"
                " WHERE id=? AND work_id=?"
                " AND status NOT IN ('resolved','abandoned')",
                (chapter, chapter, now, real, work_id),
            )
        elif kind == "abandon":
            conn.execute(
                "UPDATE promises SET status='abandoned', time_updated=?"
                " WHERE id=? AND work_id=?"
                " AND status NOT IN ('resolved','abandoned')",
                (now, real, work_id),
            )
        elif kind == "mention":
            return  # 提到不算推进，不更新 touched —— 刻意空转
        else:
            raise ValueError(f"unknown promise op: {kind!r}")


