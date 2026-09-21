"""SQLite schema constants and lightweight row dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Table names ────────────────────────────────────────────────────────────

TABLE_SESSIONS = "sessions"
TABLE_MESSAGES = "messages"
TABLE_PARTS = "parts"
TABLE_BOOK_CATEGORIES = "book_categories"
TABLE_BOOKS = "books"
TABLE_BOOK_CHAPTERS = "book_chapters"
TABLE_BOOK_CHUNKS = "book_chunks"
TABLE_READING_SESSIONS = "reading_sessions"
TABLE_BOOK_FTS = "book_fts"
TABLE_SESSION_FTS = "session_fts"
TABLE_IMPORT_LOG = "import_log"
TABLE_ANNOTATIONS = "annotations"

# ── DDL ────────────────────────────────────────────────────────────────────

CREATE_SESSIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SESSIONS} (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL DEFAULT '',
    parent_id       TEXT REFERENCES {TABLE_SESSIONS}(id) ON DELETE SET NULL,
    title           TEXT,
    agent           TEXT,
    model           TEXT,
    mode            TEXT NOT NULL DEFAULT 'immersive',
    compaction_count INTEGER NOT NULL DEFAULT 0,
    message_count   INTEGER NOT NULL DEFAULT 0,
    time_created    INTEGER NOT NULL,
    time_updated    INTEGER NOT NULL,
    time_archived   INTEGER,
    fork_point_index INTEGER
);
"""

CREATE_MESSAGES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MESSAGES} (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES {TABLE_SESSIONS}(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    usage           TEXT,
    time_created    INTEGER NOT NULL
);
"""

CREATE_PARTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PARTS} (
    id              TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL REFERENCES {TABLE_MESSAGES}(id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL REFERENCES {TABLE_SESSIONS}(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    data            TEXT NOT NULL DEFAULT '""',
    time_created    INTEGER NOT NULL
);
"""

# ── Session FTS5 (full-text search over parts content) ─────────────────────

# Add searchable_text column to parts (migration — idempotent via IF NOT EXISTS
# pattern, but we use ALTER TABLE with a try/catch in the migration runner)
ALTER_PARTS_SEARCHABLE_TEXT = f"""
ALTER TABLE {TABLE_PARTS} ADD COLUMN searchable_text TEXT NOT NULL DEFAULT '';
"""

# FTS5 virtual table — external content over parts.rowid
CREATE_SESSION_FTS = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE_SESSION_FTS} USING fts5(
    searchable_text,
    content='{TABLE_PARTS}',
    content_rowid='rowid',
    tokenize='trigram'
);
"""

# Triggers keep session_fts in sync with parts.searchable_text
CREATE_SESSION_FTS_TRIGGERS = [
    f"""
    CREATE TRIGGER IF NOT EXISTS parts_ai AFTER INSERT ON {TABLE_PARTS} BEGIN
        INSERT INTO {TABLE_SESSION_FTS}(rowid, searchable_text)
        VALUES (new.rowid, new.searchable_text);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS parts_ad AFTER DELETE ON {TABLE_PARTS} BEGIN
        INSERT INTO {TABLE_SESSION_FTS}({TABLE_SESSION_FTS}, rowid, searchable_text)
        VALUES ('delete', old.rowid, old.searchable_text);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS parts_au AFTER UPDATE ON {TABLE_PARTS} BEGIN
        INSERT INTO {TABLE_SESSION_FTS}({TABLE_SESSION_FTS}, rowid, searchable_text)
        VALUES ('delete', old.rowid, old.searchable_text);
        INSERT INTO {TABLE_SESSION_FTS}(rowid, searchable_text)
        VALUES (new.rowid, new.searchable_text);
    END;
    """,
]

# Backfill: extract searchable_text from existing parts.data JSON,
# then rebuild the FTS5 index.
_BACKFILL_PARTS_SEARCHABLE = f"""
UPDATE {TABLE_PARTS} SET searchable_text =
    CASE
        WHEN type = 'text' THEN COALESCE(json_extract(data, '$.text'), '')
        WHEN type = 'thinking' THEN COALESCE(json_extract(data, '$.thinking'), '')
        WHEN type = 'tool_use' THEN COALESCE(json_extract(data, '$.tool_name'), '')
        WHEN type = 'tool_result' THEN COALESCE(json_extract(data, '$.tool_output'), '')
        ELSE ''
    END
WHERE searchable_text = '';
"""

REBUILD_SESSION_FTS = f"""
INSERT INTO {TABLE_SESSION_FTS}({TABLE_SESSION_FTS}) VALUES('rebuild');
"""

# Migration v6: partial index for efficient archival queries
CREATE_ARCHIVABLE_INDEX = f"""
CREATE INDEX IF NOT EXISTS idx_sessions_archivable
ON {TABLE_SESSIONS}(time_updated, time_archived)
WHERE time_archived IS NULL;
"""

# Migration v7: review stamp + keywords columns for session review
ALTER_SESSIONS_TIME_REVIEWED = f"""
ALTER TABLE {TABLE_SESSIONS} ADD COLUMN time_reviewed INTEGER;
"""

ALTER_SESSIONS_KEYWORDS = f"""
ALTER TABLE {TABLE_SESSIONS} ADD COLUMN keywords TEXT NOT NULL DEFAULT '';
"""

CREATE_UNREVIEWED_INDEX = f"""
CREATE INDEX IF NOT EXISTS idx_sessions_unreviewed
ON {TABLE_SESSIONS}(time_updated)
WHERE time_reviewed IS NULL AND time_archived IS NULL;
"""

# Migration v8: review_count column for re-review tracking
ALTER_SESSIONS_REVIEW_COUNT = f"""
ALTER TABLE {TABLE_SESSIONS} ADD COLUMN review_count INTEGER NOT NULL DEFAULT 0;
"""

# Migration v9: character_name column for role-play session grouping
ALTER_SESSIONS_CHARACTER_NAME = f"""
ALTER TABLE {TABLE_SESSIONS} ADD COLUMN character_name TEXT NOT NULL DEFAULT '';
"""

# Migration v10: fork_point_index - 分支点消息序号,供前端渲染分支切换器
ALTER_SESSIONS_FORK_POINT_INDEX = f"""
ALTER TABLE {TABLE_SESSIONS} ADD COLUMN fork_point_index INTEGER;
"""

ALTER_CHAPTERS_TYPE = f"""
ALTER TABLE {TABLE_BOOK_CHAPTERS} ADD COLUMN chapter_type TEXT NOT NULL DEFAULT 'body';
"""

# ── Indexes ─────────────────────────────────────────────────────────────────

CREATE_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS idx_messages_session ON {TABLE_MESSAGES}(session_id);",
    f"CREATE INDEX IF NOT EXISTS idx_parts_message ON {TABLE_PARTS}(message_id);",
    f"CREATE INDEX IF NOT EXISTS idx_parts_session ON {TABLE_PARTS}(session_id);",
    f"CREATE INDEX IF NOT EXISTS idx_sessions_parent ON {TABLE_SESSIONS}(parent_id);",
    f"CREATE INDEX IF NOT EXISTS idx_sessions_updated ON {TABLE_SESSIONS}(time_updated);",
    f"CREATE INDEX IF NOT EXISTS idx_sessions_project ON {TABLE_SESSIONS}(project_id);",
]

ALL_DDL = [CREATE_SESSIONS, CREATE_MESSAGES, CREATE_PARTS] + CREATE_INDEXES

# ── LLM usage log (console 用量分析; created by SessionDB migration v11) ────

TABLE_LLM_USAGE_LOG = "llm_usage_log"

CREATE_LLM_USAGE_LOG = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LLM_USAGE_LOG} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'chat',
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    session_id  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'ok',
    dedup_key   TEXT UNIQUE,
    created_at  INTEGER NOT NULL
);
"""

CREATE_LLM_USAGE_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON {TABLE_LLM_USAGE_LOG}(ts);",
    f"CREATE INDEX IF NOT EXISTS idx_llm_usage_src_ts ON {TABLE_LLM_USAGE_LOG}(source, ts);",
]

# ── Book library DDL ─────────────────────────────────────────────────────────

CREATE_BOOK_CATEGORIES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BOOK_CATEGORIES} (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    parent_id       TEXT REFERENCES {TABLE_BOOK_CATEGORIES}(id) ON DELETE SET NULL,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    time_created    INTEGER NOT NULL
);
"""

CREATE_BOOKS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BOOKS} (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    author          TEXT,
    author_nationality TEXT NOT NULL DEFAULT '',
    format          TEXT NOT NULL DEFAULT 'epub',
    source_path     TEXT NOT NULL,
    category_id     TEXT REFERENCES {TABLE_BOOK_CATEGORIES}(id) ON DELETE SET NULL,
    metadata_json   TEXT,
    chapter_count   INTEGER NOT NULL DEFAULT 0,
    total_chars     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'raw',
    time_created    INTEGER NOT NULL,
    time_updated    INTEGER NOT NULL
);
"""

CREATE_BOOK_CHAPTERS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BOOK_CHAPTERS} (
    id              TEXT PRIMARY KEY,
    book_id         TEXT NOT NULL REFERENCES {TABLE_BOOKS}(id) ON DELETE CASCADE,
    chapter_index   INTEGER NOT NULL,
    title           TEXT,
    content         TEXT NOT NULL DEFAULT '',
    char_count      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'raw',
    UNIQUE(book_id, chapter_index)
);
"""

# Paragraph chunks within a chapter — the actual reading unit.
CREATE_BOOK_CHUNKS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BOOK_CHUNKS} (
    id                 TEXT PRIMARY KEY,
    book_id            TEXT NOT NULL REFERENCES {TABLE_BOOKS}(id) ON DELETE CASCADE,
    chapter_index      INTEGER NOT NULL,
    chunk_index        INTEGER NOT NULL,
    content            TEXT NOT NULL DEFAULT '',
    char_count         INTEGER NOT NULL DEFAULT 0,
    paragraph_count    INTEGER NOT NULL DEFAULT 0,
    paragraph_offsets  TEXT NOT NULL DEFAULT '[]',
    paragraph_sigs     TEXT NOT NULL DEFAULT '[]',
    UNIQUE(book_id, chapter_index, chunk_index)
);
"""

# Migration v17: chunk 段落身份 —— 段落数 + 每段偏移 [[start,len],...] 相对 chunk content。
# 恒定默认值（0 / '[]'），旧库走 ALTER 无重建；消费端遇空列用 _extract_paragraphs 低损回填。
CHUNK_PARAGRAPH_ALTERS = [
    f"ALTER TABLE {TABLE_BOOK_CHUNKS} "
    "ADD COLUMN paragraph_count INTEGER NOT NULL DEFAULT 0;",
    f"ALTER TABLE {TABLE_BOOK_CHUNKS} "
    "ADD COLUMN paragraph_offsets TEXT NOT NULL DEFAULT '[]';",
]

# Migration v18: chunk 段落体例签名 —— 每段一个样式签名（与 paragraph_offsets 平行）。
# 消费端按索引对齐：sigs[k] ↔ offsets[k] ↔ content 第 k 段。
CHUNK_SIG_ALTERS = [
    f"ALTER TABLE {TABLE_BOOK_CHUNKS} "
    "ADD COLUMN paragraph_sigs TEXT NOT NULL DEFAULT '[]';",
]

# Reading sessions — track user reading progress per book.
CREATE_READING_SESSIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_READING_SESSIONS} (
    id              TEXT PRIMARY KEY,
    book_id         TEXT NOT NULL REFERENCES {TABLE_BOOKS}(id) ON DELETE CASCADE,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    positions_json  TEXT NOT NULL DEFAULT '[]',
    total_chunks_read INTEGER NOT NULL DEFAULT 0
);
"""

# FTS5 virtual table for full-text search (external content table)
CREATE_BOOK_FTS = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE_BOOK_FTS} USING fts5(
    title,
    content,
    content='{TABLE_BOOK_CHAPTERS}',
    content_rowid='rowid',
    tokenize='trigram'
);
"""

# Triggers to keep FTS5 in sync with book_chapters (legacy, kept for migration)
CREATE_BOOK_FTS_TRIGGERS_OLD = [
    f"""
    CREATE TRIGGER IF NOT EXISTS book_chapters_ai AFTER INSERT ON {TABLE_BOOK_CHAPTERS} BEGIN
        INSERT INTO {TABLE_BOOK_FTS}(rowid, title, content)
        VALUES (new.rowid, new.title, new.content);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS book_chapters_ad AFTER DELETE ON {TABLE_BOOK_CHAPTERS} BEGIN
        INSERT INTO {TABLE_BOOK_FTS}({TABLE_BOOK_FTS}, rowid, title, content)
        VALUES ('delete', old.rowid, old.title, old.content);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS book_chapters_au AFTER UPDATE ON {TABLE_BOOK_CHAPTERS} BEGIN
        INSERT INTO {TABLE_BOOK_FTS}({TABLE_BOOK_FTS}, rowid, title, content)
        VALUES ('delete', old.rowid, old.title, old.content);
        INSERT INTO {TABLE_BOOK_FTS}(rowid, title, content)
        VALUES (new.rowid, new.title, new.content);
    END;
    """,
]

# Original triggers variable — used by existing DDL code
CREATE_BOOK_FTS_TRIGGERS = CREATE_BOOK_FTS_TRIGGERS_OLD

# FTS5 on book_chunks — the primary search index.
CREATE_BOOK_CHUNKS_FTS = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE_BOOK_FTS} USING fts5(
    content,
    content='{TABLE_BOOK_CHUNKS}',
    content_rowid='rowid',
    tokenize='trigram'
);
"""

# Triggers to keep FTS5 in sync with book_chunks
CREATE_BOOK_CHUNKS_FTS_TRIGGERS = [
    f"""
    CREATE TRIGGER IF NOT EXISTS book_chunks_ai AFTER INSERT ON {TABLE_BOOK_CHUNKS} BEGIN
        INSERT INTO {TABLE_BOOK_FTS}(rowid, content)
        VALUES (new.rowid, new.content);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS book_chunks_ad AFTER DELETE ON {TABLE_BOOK_CHUNKS} BEGIN
        INSERT INTO {TABLE_BOOK_FTS}({TABLE_BOOK_FTS}, rowid, content)
        VALUES ('delete', old.rowid, old.content);
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS book_chunks_au AFTER UPDATE ON {TABLE_BOOK_CHUNKS} BEGIN
        INSERT INTO {TABLE_BOOK_FTS}({TABLE_BOOK_FTS}, rowid, content)
        VALUES ('delete', old.rowid, old.content);
        INSERT INTO {TABLE_BOOK_FTS}(rowid, content)
        VALUES (new.rowid, new.content);
    END;
    """,
]

# SQL to migrate FTS5 from book_chapters → book_chunks
MIGRATE_FTS_TO_CHUNKS = [
    "DROP TRIGGER IF EXISTS book_chapters_ai;",
    "DROP TRIGGER IF EXISTS book_chapters_ad;",
    "DROP TRIGGER IF EXISTS book_chapters_au;",
    "DROP TRIGGER IF EXISTS book_chunks_ai;",
    "DROP TRIGGER IF EXISTS book_chunks_ad;",
    "DROP TRIGGER IF EXISTS book_chunks_au;",
    f"DROP TABLE IF EXISTS {TABLE_BOOK_FTS};",
    CREATE_BOOK_CHUNKS_FTS,
    *CREATE_BOOK_CHUNKS_FTS_TRIGGERS,
    f"INSERT INTO {TABLE_BOOK_FTS}({TABLE_BOOK_FTS}) VALUES('rebuild');",
]

BOOK_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS idx_books_category ON {TABLE_BOOKS}(category_id);",
    f"CREATE INDEX IF NOT EXISTS idx_books_status ON {TABLE_BOOKS}(status);",
    f"CREATE INDEX IF NOT EXISTS idx_chapters_book ON {TABLE_BOOK_CHAPTERS}(book_id);",
    f"CREATE INDEX IF NOT EXISTS idx_chunks_book_chapter "
    f"ON {TABLE_BOOK_CHUNKS}(book_id, chapter_index, chunk_index);",
    f"CREATE INDEX IF NOT EXISTS idx_reading_sessions_book "
    f"ON {TABLE_READING_SESSIONS}(book_id, started_at DESC);",
    f"CREATE INDEX IF NOT EXISTS idx_categories_parent ON {TABLE_BOOK_CATEGORIES}(parent_id);",
]

# ── Version control ──────────────────────────────────────────────────────────

TABLE_BOOK_VERSIONS = "book_versions"

CREATE_BOOK_VERSIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_BOOK_VERSIONS} (
    id              TEXT PRIMARY KEY,
    book_id         TEXT NOT NULL REFERENCES {TABLE_BOOKS}(id) ON DELETE CASCADE,
    chapter_index   INTEGER NOT NULL,
    operation       TEXT NOT NULL,
    old_char_count  INTEGER NOT NULL,
    new_char_count  INTEGER NOT NULL,
    snapshot_path   TEXT,
    content_hash    TEXT,
    timestamp       INTEGER NOT NULL
);
"""

BOOK_VERSION_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS idx_versions_book_chapter "
    f"ON {TABLE_BOOK_VERSIONS}(book_id, chapter_index, timestamp DESC);",
]

# ── Chapter-type overrides（人工便利贴 · 覆盖层）────────────────────────────
# 不动 books 原表的 chapter_type（导入/AI 猜测的结果照存），只在这里记"人手改过
# 什么"。读取时人手优先。重导只刷新原表，便利贴不丢。表名"overrides"而非
# "corrections"：不只是纠错，也允许人主动把某章标成"阅读时跳过"等语义。

TABLE_CHAPTER_OVERRIDES = "book_chapter_overrides"

CREATE_CHAPTER_OVERRIDES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_CHAPTER_OVERRIDES} (
    book_id         TEXT NOT NULL REFERENCES {TABLE_BOOKS}(id) ON DELETE CASCADE,
    chapter_index   INTEGER NOT NULL,
    chapter_type    TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'manual',
    time_created    INTEGER NOT NULL,
    time_updated    INTEGER NOT NULL,
    PRIMARY KEY (book_id, chapter_index)
);
"""

# ── Annotation system DDL ──────────────────────────────────────────────────

CREATE_IMPORT_LOG = f"""
CREATE TABLE IF NOT EXISTS {TABLE_IMPORT_LOG} (
    id               TEXT PRIMARY KEY,
    stable_book_id   TEXT NOT NULL,
    book_id          TEXT,
    event_type       TEXT NOT NULL,
    in_library       INTEGER NOT NULL,
    copyright_card   TEXT NOT NULL DEFAULT '{{}}',
    title            TEXT NOT NULL DEFAULT '',
    author           TEXT NOT NULL DEFAULT '',
    publisher        TEXT,
    publish_year     TEXT,
    isbn             TEXT,
    first_chapter    TEXT,
    first_paragraph  TEXT,
    source_filename  TEXT NOT NULL DEFAULT '',
    import_count     INTEGER NOT NULL DEFAULT 1,
    time_created     INTEGER NOT NULL
);
"""

CREATE_ANNOTATIONS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ANNOTATIONS} (
    id                TEXT PRIMARY KEY,
    stable_book_id    TEXT NOT NULL,
    book_id           TEXT,
    chapter_index     INTEGER,
    chunk_index       INTEGER,
    selected_text     TEXT NOT NULL,
    context_before    TEXT,
    context_after     TEXT,
    type              TEXT NOT NULL DEFAULT 'highlight',
    note_text         TEXT,
    note_format       TEXT NOT NULL DEFAULT 'markdown',
    color             TEXT NOT NULL DEFAULT '#FFD54F',
    match_status      TEXT NOT NULL DEFAULT 'linked',
    time_created      INTEGER NOT NULL,
    time_updated      INTEGER NOT NULL
);
"""

ANNOTATION_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS idx_import_log_stable "
    f"ON {TABLE_IMPORT_LOG}(stable_book_id);",
    f"CREATE INDEX IF NOT EXISTS idx_import_log_in_library "
    f"ON {TABLE_IMPORT_LOG}(in_library);",
    f"CREATE INDEX IF NOT EXISTS idx_import_log_title "
    f"ON {TABLE_IMPORT_LOG}(title, author);",
    f"CREATE INDEX IF NOT EXISTS idx_annotations_stable "
    f"ON {TABLE_ANNOTATIONS}(stable_book_id);",
    f"CREATE INDEX IF NOT EXISTS idx_annotations_book "
    f"ON {TABLE_ANNOTATIONS}(book_id);",
    f"CREATE INDEX IF NOT EXISTS idx_annotations_match "
    f"ON {TABLE_ANNOTATIONS}(match_status);",
    f"CREATE INDEX IF NOT EXISTS idx_annotations_type "
    f"ON {TABLE_ANNOTATIONS}(type);",
    f"CREATE INDEX IF NOT EXISTS idx_annotations_chunk "
    f"ON {TABLE_ANNOTATIONS}(book_id, chapter_index, chunk_index);",
]

BOOK_DDL = [
    CREATE_BOOK_CATEGORIES,
    CREATE_BOOKS,
    CREATE_BOOK_CHAPTERS,
    CREATE_BOOK_CHUNKS,
    CREATE_READING_SESSIONS,
    CREATE_BOOK_FTS,
    *CREATE_BOOK_FTS_TRIGGERS,
    *BOOK_INDEXES,
    CREATE_BOOK_VERSIONS,
    *BOOK_VERSION_INDEXES,
    CREATE_IMPORT_LOG,
    CREATE_ANNOTATIONS,
    *ANNOTATION_INDEXES,
]


# ── Row dataclasses ─────────────────────────────────────────────────────────

@dataclass
class SessionRow:
    """Lightweight row representation — no messages attached."""
    id: str
    project_id: str = ""
    parent_id: str | None = None
    title: str | None = None
    agent: str | None = None
    model: str | None = None
    mode: str = "immersive"
    compaction_count: int = 0
    message_count: int = 0
    time_created: int = 0
    time_updated: int = 0
    time_archived: int | None = None
    time_reviewed: int | None = None
    review_count: int = 0
    keywords: str = ""
    character_name: str = ""
    fork_point_index: int | None = None


@dataclass
class MessageRow:
    """A message row, optionally with its parts (not loaded by default)."""
    id: str
    session_id: str
    role: str
    usage: str | None = None  # JSON blob
    time_created: int = 0


@dataclass
class PartRow:
    """A single content part within a message."""
    id: str
    message_id: str
    session_id: str
    type: str
    data: str = '""'  # JSON blob
    time_created: int = 0


# ── Book library dataclasses ──────────────────────────────────────────────────

@dataclass
class BookCategoryRow:
    """A tree node in the book classification hierarchy."""
    id: str
    name: str
    parent_id: str | None = None
    sort_order: int = 0
    time_created: int = 0


@dataclass
class BookRow:
    """Metadata for an imported book."""
    id: str
    title: str
    author: str | None = None
    author_nationality: str = ""
    format: str = "epub"
    source_path: str = ""
    category_id: str | None = None
    metadata_json: str | None = None
    chapter_count: int = 0
    total_chars: int = 0
    status: str = "raw"
    time_created: int = 0
    time_updated: int = 0


@dataclass
class BookChapterRow:
    """A single chapter of cleaned text within a book."""
    id: str
    book_id: str
    chapter_index: int = 0
    title: str | None = None
    content: str = ""
    char_count: int = 0
    status: str = "raw"
    chapter_type: str = "body"


@dataclass
class ChapterOverrideRow:
    """Human-set chapter_type override（覆盖层便利贴）。

    规则：display 时人手优先 —— get_chapters/get_chapter 返回的
    BookChapterRow.chapter_type 会被覆盖层盖掉（见 storage/db.py）。
    原表 chapter_type 不动，重导/重清洗不丢便利贴。
    """
    book_id: str
    chapter_index: int
    chapter_type: str
    note: str = ""
    source: str = "manual"
    time_created: int = 0
    time_updated: int = 0


@dataclass
class BookChunkRow:
    """A paragraph-group chunk within a chapter — the reading unit."""
    id: str
    book_id: str
    chapter_index: int = 0
    chunk_index: int = 0
    content: str = ""
    char_count: int = 0
    paragraph_count: int = 0
    paragraph_offsets: str = "[]"   # JSON [[start,len],...] 每段相对 content 的偏移
    paragraph_sigs: str = "[]"      # JSON [sig,...] 每段体例签名（与 offsets 平行对齐）


@dataclass
class ReadingSessionRow:
    """A user reading session for a book."""
    id: str
    book_id: str
    started_at: int = 0
    ended_at: int | None = None
    positions_json: str = "[]"
    total_chunks_read: int = 0


@dataclass
class VersionRow:
    """A snapshot version of a chapter before an edit operation."""
    id: str
    book_id: str
    chapter_index: int = 0
    operation: str = ""        # 'edit_chapter' | 'replace' | 'delete_chapter'
    old_char_count: int = 0
    new_char_count: int = 0
    snapshot_path: str | None = None  # relative path into data/versions/
    content_hash: str | None = None   # MD5 of snapshot content
    timestamp: int = 0


# ── Annotation system dataclasses ──────────────────────────────────────────────

@dataclass
class ImportLogRow:
    """A lifecycle event for a book (import or delete)."""
    id: str
    stable_book_id: str
    book_id: str | None = None
    event_type: str = "import"
    in_library: int = 1
    copyright_card: str = "{}"
    title: str = ""
    author: str = ""
    publisher: str | None = None
    publish_year: str | None = None
    isbn: str | None = None
    first_chapter: str | None = None
    first_paragraph: str | None = None
    source_filename: str = ""
    import_count: int = 1
    time_created: int = 0


# Migration v14: persistent annotation coordinates for re-import recovery
ANNOTATION_V14_ALTERS = [
    "ALTER TABLE annotations ADD COLUMN paragraph_index INTEGER;",
    "ALTER TABLE annotations ADD COLUMN offset_in_paragraph INTEGER;",
    "ALTER TABLE annotations ADD COLUMN chapter_offset_start INTEGER;",
    "ALTER TABLE annotations ADD COLUMN chapter_offset_end INTEGER;",
    "ALTER TABLE annotations ADD COLUMN context_window TEXT;",
    "ALTER TABLE annotations ADD COLUMN source_session_id TEXT;",
    "ALTER TABLE annotations ADD COLUMN chunking_version INTEGER NOT NULL DEFAULT 1;",
]

# Migration v16: 收藏/批注二分（native EPUB 标注层）
# - style: 收藏样式枚举（首版仅 'underline'，预留 wave/highlight/color）；
#   type='highlight' 语义重定义为「收藏」（原高亮），批注仍为 type='note'
# - cfi_range: EPUB CFI 精确定位——回放下划线/批注箭头 + 书签跳转的主定位
# - type='bookmark'（旧红色高亮形态的收藏）已废弃，迁移时清除
ANNOTATION_V16_ALTERS = [
    "ALTER TABLE annotations ADD COLUMN style TEXT;",
    "ALTER TABLE annotations ADD COLUMN cfi_range TEXT;",
    "DELETE FROM annotations WHERE type = 'bookmark';",
]

@dataclass
class AnnotationRow:
    """A user annotation — highlight, note, or both."""
    id: str
    stable_book_id: str
    book_id: str | None = None
    chapter_index: int | None = None
    chunk_index: int | None = None
    selected_text: str = ""
    context_before: str | None = None
    context_after: str | None = None
    # v14: persistent coordinates for re-import recovery
    paragraph_index: int | None = None
    offset_in_paragraph: int | None = None
    chapter_offset_start: int | None = None
    chapter_offset_end: int | None = None
    context_window: str | None = None
    source_session_id: str | None = None
    chunking_version: int = 1
    # v16: native EPUB 标注层——cfi_range 主定位（回放+跳转），style 收藏样式
    style: str | None = None
    cfi_range: str | None = None
    # original fields
    type: str = "highlight"
    note_text: str | None = None
    note_format: str = "markdown"
    color: str = "#FFD54F"
    match_status: str = "linked"
    time_created: int = 0
    time_updated: int = 0
