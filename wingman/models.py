"""Wingman database DDL and row dataclasses.

Active tables (4):
  1. tag_registry         — 标签注册表（folksonomy）
  2. memory_entries       — 持久化记忆条目
  3. memory_tags          — 记忆-标签多对多
  4. schema_version       — 迁移版本
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

DDL_TAG_REGISTRY = (
    "CREATE TABLE IF NOT EXISTS tag_registry ("
    "  id             TEXT PRIMARY KEY,"
    "  name           TEXT NOT NULL UNIQUE,"
    "  category       TEXT NOT NULL DEFAULT 'theme',"
    "  reply_style    TEXT NOT NULL DEFAULT '',"
    "  usage_count    INTEGER NOT NULL DEFAULT 0,"
    "  created_at     INTEGER NOT NULL"
    ");"
)

DDL_SCHEMA_VERSION = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    "  version     INTEGER PRIMARY KEY,"
    "  applied_at  INTEGER NOT NULL"
    ");"
)

DDL_MEMORY_ENTRIES = (
    "CREATE TABLE IF NOT EXISTS memory_entries ("
    "  id                TEXT PRIMARY KEY,"
    "  content           TEXT NOT NULL,"
    "  summary           TEXT NOT NULL DEFAULT '',"
    "  status            TEXT NOT NULL DEFAULT 'active',"
    "  visibility        TEXT NOT NULL DEFAULT 'normal',"
    "  cited_count       INTEGER NOT NULL DEFAULT 0,"
    "  last_cited_at     INTEGER,"
    "  source_session_id TEXT,"
    "  first_created_at  INTEGER NOT NULL,"
    "  last_updated_at   INTEGER NOT NULL,"
    "  created_at        INTEGER NOT NULL"
    ");"
)

DDL_MEMORY_TAGS = (
    "CREATE TABLE IF NOT EXISTS memory_tags ("
    "  memory_id      TEXT NOT NULL,"
    "  tag_id         TEXT NOT NULL,"
    "  PRIMARY KEY (memory_id, tag_id),"
    "  FOREIGN KEY (memory_id) REFERENCES memory_entries(id) ON DELETE CASCADE,"
    "  FOREIGN KEY (tag_id) REFERENCES tag_registry(id) ON DELETE CASCADE"
    ");"
)

DDL_VOICE_ROLES = (
    "CREATE TABLE IF NOT EXISTS voice_roles ("
    "  book_id         TEXT NOT NULL,"
    "  character_name  TEXT NOT NULL,"
    "  design_prompt   TEXT NOT NULL,"        # voicedesign 铸造用的 prompt
    "  ref_audio_path  TEXT NOT NULL,"        # 参考音频相对路径 (data/voice/refs/...)
    "  created_at      INTEGER NOT NULL,"
    "  updated_at      INTEGER NOT NULL,"
    "  PRIMARY KEY (book_id, character_name)"
    ");"
)

# tag_registry v6 extensions
_DDL_V6_TAG_BEHAVIOR = (
    "ALTER TABLE tag_registry ADD COLUMN behavior TEXT NOT NULL DEFAULT 'normal';"
)
_DDL_V6_TAG_BEHAVIOR_PROMPT = (
    "ALTER TABLE tag_registry ADD COLUMN behavior_prompt TEXT NOT NULL DEFAULT '';"
)
_DDL_V6_TAG_CITE_THRESHOLD = (
    "ALTER TABLE tag_registry ADD COLUMN cite_threshold INTEGER NOT NULL DEFAULT 10;"
)
_DDL_V6_TAG_DEFAULT_WEIGHT = (
    "ALTER TABLE tag_registry ADD COLUMN default_weight REAL NOT NULL DEFAULT 1.0;"
)

# memory_entries v9 extensions: L1 pipeline typed memories
_DDL_V9_MEM_TYPE = (
    "ALTER TABLE memory_entries ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'episodic';"
)
_DDL_V9_MEM_PRIORITY = (
    "ALTER TABLE memory_entries ADD COLUMN priority INTEGER NOT NULL DEFAULT 50;"
)
_DDL_V9_MEM_SCENE = (
    "ALTER TABLE memory_entries ADD COLUMN scene_name TEXT NOT NULL DEFAULT '';"
)

# memory_entries v10 extension: L2 scene consumption marker
#   scene_consumed: 1 = this L1 memory has been aggregated into an L2 scene block
#   (dedup update/merge creates new rows, so merged memories are naturally unconsumed)
_DDL_V10_MEM_SCENE_CONSUMED = (
    "ALTER TABLE memory_entries ADD COLUMN scene_consumed INTEGER NOT NULL DEFAULT 0;"
)

# v11: L3 pipeline — versioned persona storage (git-like) + pipeline checkpoint KV
#   persona_docs: 每次 L3 生成 = 一条 commit（version 递增，content 全量快照）
#     source = 'l3' (agent 提交) | 'user_overlay' (用户直接改 persona.md 被检测到)
#   pipeline_state: L3 触发状态（last_persona_time / memories_since / request_flag）
DDL_PERSONA_DOCS = (
    "CREATE TABLE IF NOT EXISTS persona_docs ("
    "  id             INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  version        INTEGER NOT NULL,"               # commit 序号（递增）
    "  content_md5    TEXT NOT NULL,"                  # blob 指纹（检测用户改动）
    "  content        TEXT NOT NULL,"                  # 完整快照（每版本全量存）
    "  source         TEXT NOT NULL DEFAULT 'l3',"     # 'l3' | 'user_overlay'
    "  created_at     TEXT NOT NULL"                   # ISO 时间戳（与 scene index 对齐）
    ");"
)
DDL_PIPELINE_STATE = (
    "CREATE TABLE IF NOT EXISTS pipeline_state ("
    "  key          TEXT PRIMARY KEY,"
    "  value        TEXT NOT NULL DEFAULT '',"
    "  updated_at   TEXT NOT NULL"
    ");"
)

# Pipeline checkpoint keys (persona / L3)
STATE_LAST_PERSONA_TIME = "last_persona_time"          # ISO — 变化场景检测基线
STATE_MEMORIES_SINCE_PERSONA = "memories_since_last_persona"  # P4 阈值计数
STATE_REQUEST_PERSONA_UPDATE = "request_persona_update"       # P1 标志: '1' | '0'
STATE_PERSONA_UPDATE_REASON = "persona_update_reason"         # P1 原因

ALL_MIGRATIONS: list[tuple[int, str, str | list[str]]] = [
    (1, "Initial schema: tag_registry + schema_version",
     [DDL_TAG_REGISTRY, DDL_SCHEMA_VERSION]),
    (6, "Memory system: memory_entries / memory_tags + tag_registry extensions",
     [
         DDL_MEMORY_ENTRIES,
         DDL_MEMORY_TAGS,
         _DDL_V6_TAG_BEHAVIOR,
         _DDL_V6_TAG_BEHAVIOR_PROMPT,
         _DDL_V6_TAG_CITE_THRESHOLD,
         _DDL_V6_TAG_DEFAULT_WEIGHT,
     ]),
    (7, "Add keywords column to memory_entries for structured content parsing",
     "ALTER TABLE memory_entries ADD COLUMN keywords TEXT NOT NULL DEFAULT '';"),
    (8, "Voice roles: character -> voicedesign prompt + ref_audio mapping",
     [DDL_VOICE_ROLES]),
    (9, "L1 pipeline: memory_type / priority / scene_name columns",
     [_DDL_V9_MEM_TYPE, _DDL_V9_MEM_PRIORITY, _DDL_V9_MEM_SCENE]),
    (10, "L2 pipeline: scene_consumed marker column",
     [_DDL_V10_MEM_SCENE_CONSUMED]),
    (11, "L3 pipeline: persona_docs versioned profile + pipeline_state checkpoint",
     [DDL_PERSONA_DOCS, DDL_PIPELINE_STATE]),
]

# ---------------------------------------------------------------------------
# Row dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TagRow:
    id: str
    name: str
    category: str = "theme"
    reply_style: str = ""
    usage_count: int = 0
    behavior: str = "normal"
    behavior_prompt: str = ""
    cite_threshold: int = 10
    default_weight: float = 1.0
    created_at: int = 0


@dataclass
class MemoryEntryRow:
    id: str
    content: str
    summary: str = ""
    keywords: str = ""
    status: str = "active"
    visibility: str = "normal"
    cited_count: int = 0
    last_cited_at: Optional[int] = None
    source_session_id: Optional[str] = None
    first_created_at: int = 0
    last_updated_at: int = 0
    created_at: int = 0
    # v9: L1 pipeline typed memories
    memory_type: str = "episodic"   # persona | episodic | instruction
    priority: int = 50              # 0-100 (-1 for strict global instructions)
    scene_name: str = ""            # L1 extraction scene name
    # v10: L2 pipeline marker
    scene_consumed: int = 0         # 1 = already aggregated into an L2 scene block


@dataclass
class PersonaDocRow:
    """One versioned persona snapshot (v11) — git-like commit row."""

    id: int = 0
    version: int = 1
    content_md5: str = ""
    content: str = ""
    source: str = "l3"              # 'l3' = agent 提交 | 'user_overlay' = 用户提交
    created_at: str = ""            # ISO 时间戳
