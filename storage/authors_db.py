"""AuthorGraph database — 作者台账，**全库一个文件**，不随书分。

Owns ``data/authors.db``。主键是 ``author_id``，**不是** ``book_id``：
2026-09-20 的删库重导换过 16 本书的 ``book_id``，键挂书上这一层会一起断。
书与作者的关系在 ``author_books`` 里，用 ``stable_book_id`` 做锚（重导不变），
``book_id`` 只作**软引用**（可能悬空，只用来 join 当前库）。

两条贯穿本库的原则（见 ``docs/authorgraph/README.md``）：

1. **证据落库，导出量现算。** 语料与编年条目是资产，落这里；锚点解析、
   ``position`` 过滤、正名显示是**看法**，消费时算 —— 和「实体重要级不落库」
   「``first_appearance_chunk_id`` 不让模型猜」是同一条。
2. **不复制正文。** ``author_corpus`` 只存**块指针 + 逐字 excerpt**，
   正文的唯一真相在 ``book_chunks``。复制一份会在重导/用户改正文时与源分叉
   —— 与 ``graph_chunks`` 累积陈旧行是同一个坑。

本库**只存**，不做 LLM 调用。生成侧见 ``authorgraph/``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from storage.db_base import BaseDatabase, _now_ms

log = logging.getLogger("authorgraph")


# ── Table names ───────────────────────────────────────────────────────────

TABLE_AUTHORS = "authors"
TABLE_ALIASES = "author_aliases"
TABLE_BOOKS = "author_books"
TABLE_CORPUS = "author_corpus"
TABLE_RECORDS = "author_records"


# ── 词表（写在 schema 注释里，也在这里钉一份给校验用）────────────────────

#: 作者在一本书里占的位置。判据不在书里（见设计稿 §四），模糊一律判 ``avatar``。
POSITIONS = ("narrator", "avatar", "editor", "recorder", "absent", "unknown")

#: 一段话是谁在说。与 ``position`` 是两个字段 —— 混一个就会把叙述者的话
#: 当成作者自述（``position=narrator`` 的书里，序跋仍是 ``speaker=author``）。
SPEAKERS = ("author", "via_character", "editor", "other")

#: 人与书的关系。**「被解读的对象」不进这张表** —— 老子对《道德经》解读本
#: 是**题材**，他属于 BookGraph 的实体，不属于这里。
BOOK_ROLES = ("author", "co-author", "translator", "editor")

#: 来源。``corpus``/``graph``/``web`` 指得回出处，可展示；
#: ``model`` 是模型世界知识，**只可展示，绝不入注入路径**。
SOURCES = ("corpus", "graph", "web", "model")

#: 编年条目的种类。
RECORD_KINDS = ("life_event", "work", "publication", "relation")

#: 各来源的初始信任分。对话/模型来源低于文本来源 —— 抄
#: ``incremental.DIALOGUE_TRUST_SCORE`` 的先例（那里 0.3 vs 0.5）。
SOURCE_TRUST = {"corpus": 0.5, "graph": 0.5, "web": 0.4, "model": 0.2}


def _new_id() -> str:
    return f"au-{uuid.uuid4().hex[:16]}"


def _evidence_id(target_type: str, target_id: str, source_id: str,
                 quote: str) -> str:
    """证据 id 由四元组派生 —— 同一引文重抓得到同一个 id（幂等）。"""
    key = "|".join((target_type, target_id, source_id or "", quote or ""))
    return f"ev-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _trait_id(author_id: str, dimension: str, voice_domain: str,
              scope_type: str, scope_ref: str, statement: str) -> str:
    """trait 的身份：**同一句话**重跑得到同一个 id。

    语句不进 id 就分不出「同一维度的两种说法」（设计稿 04 §六.4
    要求说法不同就保留两条）；语句进 id 又会让改一个字变成新 trait ——
    两者权衡后选**保留**：宁可多一条，也不要把两条不同的判断悄悄合并。
    重跑稳定靠「内容 hash + prompt_version 命中就跳过」保证，不靠 id。
    """
    key = "|".join((author_id, dimension, voice_domain, scope_type,
                    scope_ref, " ".join(statement.split())))
    return f"vt-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


# ── 名字归一化（工序 1 的最小可用版）─────────────────────────────────────
#
# 实测：`马歇尔·卢森堡`(U+00B7 间隔号) 与 `马歇尔•卢森堡`(U+2022 项目符号)
# 是同一人被拆成**两行** —— 序跋里的写法不同而已。不归一的话，
# 同一个人会有两套语料、两套编年、两个位置判定，而且**互相看不见**。
#
# 各种「点」统一成一个：间隔号 / 项目符号 / 片假名中点 / 连字点 / 点号。
_PUNCT_FOLD = str.maketrans({
    "•": "·", "・": "·", "‧": "·", "∙": "·", "⋅": "·", "．": "·",
})


def name_key(name: str) -> str:
    """名字的**匹配键**：抹掉标点、空白、全半角、大小写差异。

    ``canonical_name`` 存**原样**（第一个见到的写法），匹配用这个键 ——
    与「词表外原样保留、不硬塞兜底值」是同一条：归一的是**匹配**，
    不是**存储**。显示名该长什么样由人说了算，不该被机器改掉。
    """
    s = unicodedata.normalize("NFKC", name or "").strip()
    s = s.translate(_PUNCT_FOLD)
    s = "".join(ch for ch in s if not ch.isspace())
    return s.casefold()


# ── DDL ───────────────────────────────────────────────────────────────────

AUTHORS_DDL_V1 = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_AUTHORS} (
        author_id       TEXT PRIMARY KEY,
        canonical_name  TEXT NOT NULL,
        nationality     TEXT NOT NULL DEFAULT '',
        birth_year      INTEGER,
        death_year      INTEGER,
        era_note        TEXT NOT NULL DEFAULT '',
        bio_status      TEXT NOT NULL DEFAULT 'none',
        needs_refresh   INTEGER NOT NULL DEFAULT 0,
        last_refresh_at INTEGER,
        time_created    INTEGER NOT NULL,
        time_updated    INTEGER NOT NULL,
        UNIQUE(canonical_name)
    );
    """,

    # 别名表在 ``alias_name`` 上**全局唯一** —— 这是归一化的关键：一本书署
    # 「张爱玲」、另一本署「张煐」，两条 alias 撞到同一个 author_id 才说明是
    # 同一个人。带 book 维度的唯一约束会让「换个说法」变成查不到 → 又建一个。
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_ALIASES} (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        alias_name  TEXT NOT NULL,
        author_id   TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
        source      TEXT NOT NULL DEFAULT '',
        UNIQUE(alias_name)
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_aauthor ON {TABLE_ALIASES}(author_id);",

    # ``stable_book_id`` 做锚（重导不变）；``book_id`` 软引用可悬空。
    # 一个作者对一本书可以有多行（role 不同）：合著者、译者、编者各一行 ——
    # 所以主键是三元组，不是 (author_id, book_id)。
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_BOOKS} (
        author_id        TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
        stable_book_id   TEXT NOT NULL,
        book_id          TEXT,
        role             TEXT NOT NULL DEFAULT 'author',
        position         TEXT NOT NULL DEFAULT 'unknown',
        position_source  TEXT NOT NULL DEFAULT '',
        position_note    TEXT NOT NULL DEFAULT '',
        time_updated     INTEGER NOT NULL,
        PRIMARY KEY (author_id, stable_book_id, role)
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_abook_stable ON {TABLE_BOOKS}(stable_book_id);",
    f"CREATE INDEX IF NOT EXISTS idx_abook_book ON {TABLE_BOOKS}(book_id);",

    # ── 证据 1：语料（作者第一人称说过的原话，指向 book_chunks）──────────
    #
    # ``chunk_id`` 是**软引用**：重导会换主键，所以它可空、且必须能自愈。
    # 自愈材料是 ``excerpt``（逐字）—— 拿它回 ``book_chunks`` 重新定位，
    # 与 ``relink_chunks`` 的内容指纹优先同一套。**跨库的 authors.db 进不了
    # 书内的 relink 事务**，所以自愈是读时算的，不是重导时重指的。
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_CORPUS} (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        author_id       TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
        stable_book_id  TEXT NOT NULL,
        book_id         TEXT,
        chunk_id        TEXT,
        chapter_index   INTEGER,
        chunk_index     INTEGER,
        excerpt         TEXT NOT NULL,
        role            TEXT NOT NULL DEFAULT 'author',
        speaker         TEXT NOT NULL DEFAULT 'author',
        text_kind       TEXT NOT NULL DEFAULT '',
        era_year        INTEGER,
        era_note        TEXT NOT NULL DEFAULT '',
        source          TEXT NOT NULL DEFAULT 'corpus',
        trust           REAL NOT NULL DEFAULT 0.5,
        time_created    INTEGER NOT NULL,
        UNIQUE(stable_book_id, chunk_id, excerpt)
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_acorpus_author ON {TABLE_CORPUS}(author_id);",
    f"CREATE INDEX IF NOT EXISTS idx_acorpus_stable ON {TABLE_CORPUS}(stable_book_id);",
    f"CREATE INDEX IF NOT EXISTS idx_acorpus_speaker ON {TABLE_CORPUS}(author_id, speaker);",
    f"CREATE INDEX IF NOT EXISTS idx_acorpus_era ON {TABLE_CORPUS}(author_id, era_year);",

    # ── 证据 2：编年条目（作者身上/笔下发生过什么）──────────────────────
    #
    # ``year`` 可空 —— 编年抽不出来是常态，空年条目仍是有价值的陈述，
    # 只是**不参与剧透门计算**（门靠年，无年就无门 → fail-closed 不展示）。
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_RECORDS} (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        author_id       TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
        kind            TEXT NOT NULL,
        year            INTEGER,
        year_end        INTEGER,
        statement       TEXT NOT NULL,
        evidence_excerpt TEXT NOT NULL DEFAULT '',
        stable_book_id  TEXT,
        chunk_id        TEXT,
        source          TEXT NOT NULL DEFAULT 'model',
        trust           REAL NOT NULL DEFAULT 0.2,
        time_created    INTEGER NOT NULL
    );
    """,
    f"CREATE INDEX IF NOT EXISTS idx_arec_author ON {TABLE_RECORDS}(author_id, kind);",
    f"CREATE INDEX IF NOT EXISTS idx_arec_year ON {TABLE_RECORDS}(author_id, year);",
]

#: 证件号方案（``author_identifiers.scheme``）。外部 ID 是身份消歧的锚。
IDENTIFIER_SCHEMES = ("wikidata", "viaf", "isni", "openlibrary", "nlc",
                      "loc", "bnfa", "goodreads", "official_site")

#: 证据目标类型（``author_evidence.target_type``）。
EVIDENCE_TARGETS = ("record", "corpus", "relation", "claim",
                    "voice_trait", "author_field")

#: 证据关系。``legacy_url_only`` 只给迁移来的旧数据：有网址、没正文引文。
EVIDENCE_RELATIONS = ("supports", "contradicts", "contextualizes",
                      "legacy_url_only")

#: 事实补全状态（``authors.bio_status``）。
BIO_STATUSES = ("none", "partial", "full", "stale", "error")

#: 身份状态。**默认 provisional，不自动升级** —— 同名误合并是不可逆的。
IDENTITY_STATUSES = ("resolved", "provisional", "conflict")

#: 声音提取状态（``author_books.voice_status``，M2 用）。
VOICE_STATUSES = ("none", "pending", "running", "ready", "partial",
                  "failed", "stale", "skipped")

#: 结构化关系 / 解释性陈述的词表。
RELATION_TYPES = ("mentor", "student", "friend", "family", "spouse",
                  "lover", "rival", "influence", "translator", "publisher",
                  "editor", "collaborator", "colleague", "other")
CLAIM_FACETS = ("theme", "reception", "poetics", "context")

#: 文风维度（设计稿 04，固定 10 个）。
VOICE_DIMENSIONS = ("narrative_distance", "sentence_rhythm",
                    "paragraph_shape", "dialogue_density",
                    "figurative_language", "emotional_expression",
                    "information_reveal", "lexical_texture",
                    "humor_irony", "closure_pattern")
VOICE_DOMAINS = ("fiction", "nonfiction", "paratext", "public",
                 "mixed", "unknown")

#: 叙事层（``author_corpus.narrative_layer``）。``paratext`` 是序跋；
#: 声音管线的正文写 ``narration/dialogue``。**写作透镜只认 narration/essay**。
NARRATIVE_LAYERS = ("paratext", "narration", "dialogue",
                    "interview", "lecture", "essay")

#: 场景词表（设计稿 04 §三.4，12 类 + other）。
SCENE_TYPES = ("opening", "transition", "dialogue", "description", "action",
               "introspection", "emotional_peak", "quiet", "argument",
               "landscape", "memory", "ending", "other")

#: trait 生命周期。``superseded`` 不删除 —— 证据链要能追溯。
VOICE_TRAIT_STATUSES = ("active", "provisional", "superseded", "rejected")


AUTHORS_DDL_V2 = [
    # ── 身份字段（设计稿 02 §三.1）──────────────────────────────────────
    "ALTER TABLE authors ADD COLUMN name_native TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE authors ADD COLUMN birth_place TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE authors ADD COLUMN death_place TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE authors ADD COLUMN primary_language TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE authors ADD COLUMN identity_status TEXT NOT NULL DEFAULT 'provisional';",
    "ALTER TABLE authors ADD COLUMN bio_error TEXT NOT NULL DEFAULT '';",
    # ── 书级声音状态（M2 写入，这里先把列准备好）──────────────────────
    "ALTER TABLE author_books ADD COLUMN voice_status TEXT NOT NULL DEFAULT 'none';",
    "ALTER TABLE author_books ADD COLUMN voice_content_hash TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE author_books ADD COLUMN voice_updated_at INTEGER;",

    # ── 身份锚点 ────────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS author_identifiers (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        author_id     TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
        scheme        TEXT NOT NULL,
        value         TEXT NOT NULL,
        source        TEXT NOT NULL DEFAULT '',
        confidence    REAL NOT NULL DEFAULT 0.5,
        time_created  INTEGER NOT NULL,
        UNIQUE(scheme, value)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_aid_author ON author_identifiers(author_id);",

    # ── 来源台账 ────────────────────────────────────────────────────────
    # ``canonical_url`` 是去重键；书籍来源用 ``book:<stable_book_id>``。
    f"""
    CREATE TABLE IF NOT EXISTS author_sources (
        id              TEXT PRIMARY KEY,
        url             TEXT NOT NULL DEFAULT '',
        canonical_url   TEXT NOT NULL DEFAULT '',
        title           TEXT NOT NULL DEFAULT '',
        publisher       TEXT NOT NULL DEFAULT '',
        source_tier     INTEGER NOT NULL DEFAULT 4,
        source_kind     TEXT NOT NULL DEFAULT 'unknown',
        language        TEXT NOT NULL DEFAULT '',
        published_at    TEXT NOT NULL DEFAULT '',
        retrieved_at    INTEGER,
        content_hash    TEXT NOT NULL DEFAULT '',
        content_excerpt TEXT NOT NULL DEFAULT '',
        license_note    TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'active',
        time_created    INTEGER NOT NULL,
        time_updated    INTEGER NOT NULL,
        UNIQUE(canonical_url)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_asrc_tier ON author_sources(source_tier, source_kind);",

    f"""
    CREATE TABLE IF NOT EXISTS author_source_roles (
        source_id  TEXT NOT NULL REFERENCES author_sources(id) ON DELETE CASCADE,
        role       TEXT NOT NULL,
        PRIMARY KEY(source_id, role)
    );
    """,

    # ── 逐字证据 ────────────────────────────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS author_evidence (
        id            TEXT PRIMARY KEY,
        author_id     TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
        target_type   TEXT NOT NULL,
        target_id     TEXT NOT NULL,
        source_id     TEXT REFERENCES author_sources(id) ON DELETE SET NULL,
        quote         TEXT NOT NULL DEFAULT '',
        locator       TEXT NOT NULL DEFAULT '',
        relation      TEXT NOT NULL DEFAULT 'supports',
        confidence    REAL NOT NULL DEFAULT 0.5,
        time_created  INTEGER NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_aev_target ON author_evidence(target_type, target_id);",
    "CREATE INDEX IF NOT EXISTS idx_aev_author ON author_evidence(author_id);",
    "CREATE INDEX IF NOT EXISTS idx_aev_source ON author_evidence(source_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_aev_quote ON author_evidence("
    "target_type, target_id, source_id, quote);",

    # ── 结构化关系（P1，先建表不消费）──────────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS author_relations (
        relation_id     TEXT PRIMARY KEY,
        author_id       TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
        other_name      TEXT NOT NULL,
        other_author_id TEXT REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE SET NULL,
        relation_type   TEXT NOT NULL DEFAULT 'other',
        direction       TEXT NOT NULL DEFAULT 'outgoing',
        year_start      INTEGER,
        year_end        INTEGER,
        statement       TEXT NOT NULL,
        confidence      REAL NOT NULL DEFAULT 0.3,
        status          TEXT NOT NULL DEFAULT 'provisional',
        time_created    INTEGER NOT NULL,
        time_updated    INTEGER NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_arel_author ON author_relations(author_id, relation_type);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_arel_unique ON author_relations("
    "author_id, other_name, relation_type, COALESCE(year_start, -1));",

    # ── 解释性陈述：主题 / 接受史 / 诗学 / 背景 ─────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS author_claims (
        claim_id      TEXT PRIMARY KEY,
        author_id     TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
        facet         TEXT NOT NULL,
        theme_key     TEXT NOT NULL DEFAULT '',
        statement     TEXT NOT NULL,
        year          INTEGER,
        confidence    REAL NOT NULL DEFAULT 0.3,
        status        TEXT NOT NULL DEFAULT 'provisional',
        time_created  INTEGER NOT NULL,
        time_updated  INTEGER NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_aclaim_author ON author_claims(author_id, facet);",
    "CREATE INDEX IF NOT EXISTS idx_aclaim_theme ON author_claims(theme_key);",

    # ── 声音账本（M2 写，先把形状定下来）──────────────────────────────
    f"""
    CREATE TABLE IF NOT EXISTS author_voice_traits (
        trait_id      TEXT PRIMARY KEY,
        author_id     TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
        dimension     TEXT NOT NULL,
        voice_domain  TEXT NOT NULL DEFAULT 'unknown',
        value         TEXT NOT NULL DEFAULT '',
        statement     TEXT NOT NULL,
        strength      REAL NOT NULL DEFAULT 0.5,
        scope_type    TEXT NOT NULL DEFAULT 'author',
        scope_ref     TEXT NOT NULL DEFAULT '',
        transferable  INTEGER NOT NULL DEFAULT 1,
        confidence    REAL NOT NULL DEFAULT 0.3,
        status        TEXT NOT NULL DEFAULT 'provisional',
        revision      TEXT NOT NULL DEFAULT '',
        time_created  INTEGER NOT NULL,
        time_updated  INTEGER NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_avt_author ON author_voice_traits(author_id, dimension);",
    "CREATE INDEX IF NOT EXISTS idx_avt_scope ON author_voice_traits(author_id, scope_type, scope_ref);",
    f"""
    CREATE TABLE IF NOT EXISTS author_voice_trait_evidence (
        trait_id    TEXT NOT NULL REFERENCES author_voice_traits(trait_id) ON DELETE CASCADE,
        corpus_id   INTEGER NOT NULL REFERENCES {TABLE_CORPUS}(id) ON DELETE CASCADE,
        relation    TEXT NOT NULL DEFAULT 'supports',
        note        TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(trait_id, corpus_id, relation)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS author_voice_runs (
        run_id            TEXT PRIMARY KEY,
        author_id         TEXT NOT NULL REFERENCES authors(author_id) ON DELETE CASCADE,
        book_id           TEXT,
        stable_book_id    TEXT,
        extraction_run_id TEXT NOT NULL DEFAULT '',
        scout_plan_hash   TEXT NOT NULL DEFAULT '',
        sample_ids_json   TEXT NOT NULL DEFAULT '[]',
        model             TEXT NOT NULL DEFAULT '',
        prompt_version    TEXT NOT NULL DEFAULT '',
        corpus_count      INTEGER NOT NULL DEFAULT 0,
        status            TEXT NOT NULL DEFAULT 'running',
        error_code        TEXT NOT NULL DEFAULT '',
        manifest          TEXT NOT NULL DEFAULT '{}',
        started_at        INTEGER NOT NULL,
        finished_at       INTEGER
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_avrun_book ON author_voice_runs(book_id, status);",
]

#: ``author_corpus`` 重建后的形状。旧表 ``stable_book_id NOT NULL`` 装不下
#: 外部访谈/演讲 —— 那是设计稿 02 §三.6 的硬理由，不是审美。
CORPUS_V2_DDL = f"""
CREATE TABLE author_corpus_v2 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id       TEXT NOT NULL REFERENCES {TABLE_AUTHORS}(author_id) ON DELETE CASCADE,
    source_id       TEXT REFERENCES author_sources(id) ON DELETE SET NULL,
    stable_book_id  TEXT,
    book_id         TEXT,
    chunk_id        TEXT,
    chapter_index   INTEGER,
    chunk_index     INTEGER,
    excerpt         TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'author',
    speaker         TEXT NOT NULL DEFAULT 'author',
    narrative_layer TEXT NOT NULL DEFAULT 'paratext',
    text_kind       TEXT NOT NULL DEFAULT '',
    scene_type      TEXT NOT NULL DEFAULT '',
    pov             TEXT NOT NULL DEFAULT '',
    scene_note      TEXT NOT NULL DEFAULT '',
    period          TEXT NOT NULL DEFAULT '',
    word_count      INTEGER NOT NULL DEFAULT 0,
    era_year        INTEGER,
    era_note        TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'corpus',
    trust           REAL NOT NULL DEFAULT 0.5,
    time_created    INTEGER NOT NULL
);
"""

#: 书内行与外部行**分开约束**：同一本书不同 chunk 允许出现相同句子，
#: 外部来源则一段话只应有一条（设计稿 02 §三.6 的注）。
CORPUS_V2_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_acorpus_author ON author_corpus(author_id);",
    "CREATE INDEX IF NOT EXISTS idx_acorpus_stable ON author_corpus(stable_book_id);",
    "CREATE INDEX IF NOT EXISTS idx_acorpus_speaker ON author_corpus(author_id, speaker);",
    "CREATE INDEX IF NOT EXISTS idx_acorpus_era ON author_corpus(author_id, era_year);",
    "CREATE INDEX IF NOT EXISTS idx_acorpus_scene ON author_corpus(author_id, scene_type);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_acorpus_book ON author_corpus("
    "source_id, COALESCE(chunk_id,''), excerpt) "
    "WHERE source_id IS NOT NULL AND stable_book_id IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_acorpus_external ON author_corpus("
    "source_id, excerpt) WHERE source_id IS NOT NULL AND stable_book_id IS NULL;",
    # **旧写入路径的幂等键。** ``add_corpus()``（序跋采集）不带 source_id，
    # 重建后旧的表级 UNIQUE 随旧表一起没了 —— 少了这条，重跑就会不断加料。
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_acorpus_legacy ON author_corpus("
    "stable_book_id, chunk_id, excerpt) WHERE source_id IS NULL;",
]


# ── Path resolution ───────────────────────────────────────────────────────

def resolve_authors_db_path(config: Any = None, root: Path | None = None) -> Path:
    """作者库路径。**全库一个文件**，与书无关 —— 所以没有 book_id 参数。

    与 ``resolve_graph_db_path`` 同一形状（那个要修 book_id→stable 的命名分裂，
    这里没有分裂可言：库不随书走）。
    """
    if root is not None:
        base = Path(root)
    else:
        # 调用方常把 Config 直接传进来；``project_dir`` 才是指向本仓库的
        # 那一个（测试也靠它指到临时目录，避免写真实作者库）。
        project = getattr(config, "project_dir", "")
        base = Path(project) if project else Path(__file__).resolve().parent.parent
    return base / "data" / "authors.db"


class AuthorsDB(BaseDatabase):
    """作者台账。只存不判 —— 判定在 ``authorgraph/``。"""

    _MIGRATIONS: list[tuple[int, str, str | list[str]]] = [
        (1, "author ledger v1: authors/aliases/books/corpus/records",
         AUTHORS_DDL_V1),
        (2, "author dossier v2: identity/source/evidence/voice schema",
         AUTHORS_DDL_V2),
    ]

    def __init__(self, db_path: Path | str):
        super().__init__(Path(db_path))
        self.initialize()

    def initialize(self) -> None:
        """v1/v2 DDL 走通用迁移引擎；corpus 重建走 Python（要备份与事务）。"""
        super().initialize()
        self._migrate_corpus_v2()

    # ── v3：author_corpus 重建 + 旧数据回填 ───────────────────────────────
    #
    # 为什么不在 ``_MIGRATIONS`` 里写 SQL：重建是「备份 → 建新表 → 搬行 →
    # 删旧表 → 改名 → 建索引」六步，引擎逐条执行且**不保证事务**，中途
    # 断掉会留下没有 author_corpus 的库。这里用 Python 包成一个事务，
    # 失败整步回滚，schema_version 也就不会记上。

    def _migrate_corpus_v2(self) -> None:
        conn = self.get_conn()
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        if (row[0] or 0) >= 3:
            return
        cols = {r[1] for r in conn.execute("PRAGMA table_info(author_corpus)")}
        if "narrative_layer" not in cols or "source_id" not in cols:
            self._backup_file()
            with conn:                                # 事务：失败整步回滚
                conn.execute("DROP TABLE IF EXISTS author_corpus_v1_backup")
                conn.execute("CREATE TABLE author_corpus_v1_backup "
                             "AS SELECT * FROM author_corpus")
                conn.execute("DROP TABLE IF EXISTS author_corpus_v2")
                conn.execute(CORPUS_V2_DDL)
                conn.execute(
                    "INSERT INTO author_corpus_v2 ("
                    " id, author_id, source_id, stable_book_id, book_id,"
                    " chunk_id, chapter_index, chunk_index, excerpt, role,"
                    " speaker, narrative_layer, text_kind, scene_type, pov,"
                    " scene_note, period, word_count, era_year, era_note,"
                    " source, trust, time_created)"
                    " SELECT id, author_id, NULL, stable_book_id, book_id,"
                    " chunk_id, chapter_index, chunk_index, excerpt, role,"
                    " speaker, 'paratext', text_kind, '', '', '', '',"
                    " length(excerpt), era_year, era_note, source, trust,"
                    " time_created FROM author_corpus")
                conn.execute("DROP TABLE author_corpus")
                conn.execute("ALTER TABLE author_corpus_v2 "
                             "RENAME TO author_corpus")
                for ddl in CORPUS_V2_INDEXES:
                    conn.execute(ddl)
        self._backfill_v2(conn)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at, description)"
            " VALUES (3, ?, ?)",
            (_now_ms(), "author corpus v2: rebuild + source backfill"))
        conn.commit()

    def _backup_file(self) -> None:
        """迁移前留一份可打开的文件级备份。失败只喊，不阻断启动。"""
        try:
            import shutil
            import time as _t
            self.get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE);")
            out = self._path.parent / "backups"
            out.mkdir(parents=True, exist_ok=True)
            dst = out / f"{self._path.stem}-v1-{_t.strftime('%Y%m%d-%H%M%S')}.db"
            shutil.copy2(self._path, dst)
            log.warning("authors.db v1 备份：%s", dst)
        except Exception as exc:                      # noqa: BLE001
            log.warning("authors.db 备份失败（继续迁移）：%s", exc)

    def _backfill_v2(self, conn: Any) -> None:
        """把旧行接上来源与证据。**只填空，不改旧值** —— 旧数据是资产。"""
        try:
            from authorgraph.sources.registry import (
                canonical_url, roles_of, source_id_for, source_kind_of,
                tier_of)
        except Exception:                             # noqa: BLE001
            canonical_url = lambda u: (u or "").rstrip("/")   # noqa: E731
            roles_of = lambda k, t: ()                        # noqa: E731
            source_id_for = (lambda k, prefix="src":                # noqa: E731
                             f"{prefix}-{hashlib.sha1(str(k).encode()).hexdigest()[:16]}")
            source_kind_of = lambda u: "unknown"              # noqa: E731
            tier_of = lambda u: 4                             # noqa: E731

        now = _now_ms()
        # 1) 每本有语料/记录的书一条来源（canonical_url = book:<stable>）
        stables = {r[0] for r in conn.execute(
            "SELECT stable_book_id FROM ("
            " SELECT stable_book_id FROM author_books"
            " UNION SELECT stable_book_id FROM author_corpus"
            " UNION SELECT stable_book_id FROM author_records)"
            " WHERE stable_book_id IS NOT NULL AND stable_book_id<>''")}
        for stable in stables:
            if not stable:
                continue
            sid = source_id_for(f"book:{stable}")
            conn.execute(
                "INSERT OR IGNORE INTO author_sources (id, url, canonical_url,"
                " title, publisher, source_tier, source_kind, language,"
                " published_at, retrieved_at, content_hash, content_excerpt,"
                " license_note, status, time_created, time_updated)"
                " VALUES (?, '', ?, '', '', 1, 'book', '', '', NULL, '', '',"
                " 'short_quote_only', 'active', ?, ?)",
                (sid, f"book:{stable}", now, now))
            for role in roles_of("book", 1):
                conn.execute("INSERT OR IGNORE INTO author_source_roles"
                             " (source_id, role) VALUES (?, ?)", (sid, role))
            conn.execute(
                "UPDATE author_corpus SET source_id=?,"
                " word_count=length(excerpt),"
                " narrative_layer=COALESCE(NULLIF(narrative_layer,''),'paratext')"
                " WHERE stable_book_id=? AND (source_id IS NULL"
                " OR word_count=0)", (sid, stable))
        # 2) 旧的 web 记录：只有网址、没有正文引文 → legacy_url_only
        for r in conn.execute(
                "SELECT id, author_id, evidence_excerpt FROM author_records"
                " WHERE source='web' AND evidence_excerpt LIKE 'http%'"):
            url = (r["evidence_excerpt"] or "").strip()
            cu = canonical_url(url)
            if not cu:
                continue
            sid = source_id_for(cu)
            tier = tier_of(url)
            kind = source_kind_of(url)
            conn.execute(
                "INSERT OR IGNORE INTO author_sources (id, url, canonical_url,"
                " title, publisher, source_tier, source_kind, language,"
                " published_at, retrieved_at, content_hash, content_excerpt,"
                " license_note, status, time_created, time_updated)"
                " VALUES (?, ?, ?, '', '', ?, ?, '', '', NULL, '', '',"
                " 'short_quote_only', 'active', ?, ?)",
                (sid, url, cu, tier, kind, now, now))
            for role in roles_of(kind, tier):
                conn.execute("INSERT OR IGNORE INTO author_source_roles"
                             " (source_id, role) VALUES (?, ?)", (sid, role))
            conn.execute(
                "INSERT OR IGNORE INTO author_evidence (id, author_id,"
                " target_type, target_id, source_id, quote, locator, relation,"
                " confidence, time_created) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_evidence_id("record", str(r["id"]), sid, ""),
                 r["author_id"], "record", str(r["id"]), sid, "", "",
                 "legacy_url_only", 0.4, now))

    # ── 作者 ──────────────────────────────────────────────────────────────

    def upsert_author(self, canonical_name: str, *,
                      nationality: str = "", birth_year: int | None = None,
                      death_year: int | None = None,
                      era_note: str = "") -> str:
        """按**正名**建或取作者。名字为空则返回空串（不建无名作者）。

        ``birth_year``/``death_year`` 只在**给了值**时覆盖 —— 传 None 是
        「这次不知道」，不是「改成不知道」。否则一次不带生卒的采集会把
        已经查到的年份抹掉。
        """
        name = (canonical_name or "").strip()
        if not name:
            return ""
        conn = self.get_conn()
        now = _now_ms()
        row = self._find_by_key(name)
        if row:
            aid = row["author_id"]
            if row["canonical_name"] != name:
                # 归一化键相同但写法不同 → 记成别名。
                # 正名**不动**：显示名该长什么样由人说了算，
                # 机器只负责认出「这是同一个人」。
                self.add_alias(name, aid, source="variant")
            sets, vals = ["time_updated=?"], [now]
            if nationality:
                sets.append("nationality=?"); vals.append(nationality)
            if birth_year is not None:
                sets.append("birth_year=?"); vals.append(birth_year)
            if death_year is not None:
                sets.append("death_year=?"); vals.append(death_year)
            if era_note:
                sets.append("era_note=?"); vals.append(era_note)
            vals.append(aid)
            conn.execute(
                f"UPDATE {TABLE_AUTHORS} SET {', '.join(sets)} "
                f"WHERE author_id=?", vals)
            conn.commit()
            return aid
        aid = _new_id()
        conn.execute(
            f"INSERT INTO {TABLE_AUTHORS} (author_id, canonical_name, nationality,"
            f" birth_year, death_year, era_note, bio_status, time_created,"
            f" time_updated) VALUES (?,?,?,?,?,?,'none',?,?)",
            (aid, name, nationality, birth_year, death_year, era_note, now, now))
        conn.commit()
        return aid

    def _find_by_key(self, name: str) -> dict[str, Any] | None:
        """按**归一化键**找作者。

        在 Python 里扫全表，不是 SQL —— 作者表是几十行的量级，而
        **一套归一化实现胜过两套**：若把 REPLACE 链写进 SQL，
        迁移里的那份和写入路径的那份迟早会漂。表真长大了再说。
        """
        key = name_key(name)
        if not key:
            return None
        for r in self.get_conn().execute(
                f"SELECT * FROM {TABLE_AUTHORS}"):
            if name_key(r["canonical_name"]) == key:
                return dict(r)
        return None

    def add_alias(self, alias_name: str, author_id: str, source: str = "") -> bool:
        """登记别名。**撞名不抢** —— 已属于别人时返回 False 并记日志。

        抢名会让「张爱玲」被后写的另一本书挪到别人名下，而 ``UNIQUE``
        撞了直接 raise 会让整趟采集挂掉。两个都不对，所以吞掉并上报。
        """
        name = (alias_name or "").strip()
        if not name or not author_id:
            return False
        conn = self.get_conn()
        try:
            conn.execute(
                f"INSERT OR IGNORE INTO {TABLE_ALIASES} (alias_name, author_id,"
                f" source) VALUES (?,?,?)", (name, author_id, source))
            conn.commit()
        except Exception as exc:                       # noqa: BLE001
            log.warning("author alias insert failed (%s): %s", name, exc)
            return False
        row = conn.execute(
            f"SELECT author_id FROM {TABLE_ALIASES} WHERE alias_name=?",
            (name,)).fetchone()
        if row and row["author_id"] != author_id:
            log.warning("alias '%s' 已属于 %s，未改指 %s",
                        name, row["author_id"], author_id)
            return False
        return True

    def find_author(self, name: str) -> dict[str, Any] | None:
        """按名字找作者。**读取路径的三档**，与 ``GraphDB.locate_node`` 同形：

        精确正名 → 精确别名 → 子串（按语料条数多的优先）。

        第三档是兜底，会误中（「张爱玲」是「张爱玲」的子串，也是
        「张爱玲研究」的子串），所以**只用于读取**。写入路径一律用
        ``upsert_author`` 的精确匹配 —— 那里退到子串会把两个人并成一个，
        是数据损坏（见 CLAUDE.md「读取路径的名字解析」）。
        """
        q = (name or "").strip()
        if not q:
            return None
        conn = self.get_conn()
        key = name_key(q)
        row = self._find_by_key(q)                      # 精确正名（含标点变体）
        if row:
            return row
        for r in conn.execute(                           # 精确别名
                f"SELECT a.*, al.alias_name AS _al FROM {TABLE_ALIASES} al"
                f" JOIN {TABLE_AUTHORS} a ON a.author_id=al.author_id"):
            if name_key(r["_al"]) == key:
                d = dict(r)
                d.pop("_al", None)
                return d
        best: dict[str, Any] | None = None               # 子串兜底
        best_n = -1
        for r in conn.execute(
                f"SELECT a.*, (SELECT COUNT(*) FROM {TABLE_CORPUS} c"
                f"   WHERE c.author_id=a.author_id) AS _n FROM {TABLE_AUTHORS} a"):
            if key and key in name_key(r["canonical_name"]) and r["_n"] > best_n:
                best, best_n = dict(r), r["_n"]
        if best is not None:
            best.pop("_n", None)
        return best

    def get_author(self, author_id: str) -> dict[str, Any] | None:
        """按主键取。**比按名字取准** —— 名字会随去重/改名漂移，id 不会。

        扮演会话存的就是 id（``sessions.role_ref``），所以这条路径要走得通；
        走不通就只能退回名字解析，而那正是「同名两个人」出没的地方。
        """
        if not author_id:
            return None
        row = self.get_conn().execute(
            f"SELECT * FROM {TABLE_AUTHORS} WHERE author_id=?",
            (author_id,)).fetchone()
        return dict(row) if row else None

    def list_authors(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        """全量作者行（档案页搜索用）。库小，过滤留给投影层。"""
        rows = self.get_conn().execute(
            f"SELECT * FROM {TABLE_AUTHORS} ORDER BY canonical_name LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        return [dict(r) for r in rows]

    def aliases_of(self, author_id: str) -> list[str]:
        rows = self.get_conn().execute(
            f"SELECT alias_name FROM {TABLE_ALIASES} WHERE author_id=?"
            f" ORDER BY alias_name", (author_id,)).fetchall()
        return [r["alias_name"] for r in rows]

    # ── 人-书关系 ─────────────────────────────────────────────────────────

    def link_book(self, author_id: str, stable_book_id: str, *,
                  book_id: str | None = None, role: str = "author",
                  position: str | None = None, position_source: str = "",
                  position_note: str = "") -> None:
        """登记「这个人在这一本书里是什么角色」。

        ``position`` 传 None 表示**这次不判定**（保留旧值）；传了值才写。
        理由与 ``upsert_author`` 的生卒年一样：一次不判定的采集不该把
        上一轮判出来的位置抹成 unknown。
        """
        if not author_id or not stable_book_id:
            return
        conn = self.get_conn()
        now = _now_ms()
        r = role if role in BOOK_ROLES else "author"
        row = conn.execute(
            f"SELECT position FROM {TABLE_BOOKS} WHERE author_id=? AND"
            f" stable_book_id=? AND role=?", (author_id, stable_book_id, r)).fetchone()
        if row:
            sets = ["time_updated=?", "book_id=?"]
            vals: list[Any] = [now, book_id]
            if position is not None:
                sets += ["position=?", "position_source=?", "position_note=?"]
                vals += [position, position_source, position_note]
            vals += [author_id, stable_book_id, r]
            conn.execute(
                f"UPDATE {TABLE_BOOKS} SET {', '.join(sets)} WHERE author_id=?"
                f" AND stable_book_id=? AND role=?", vals)
        else:
            conn.execute(
                f"INSERT INTO {TABLE_BOOKS} (author_id, stable_book_id, book_id,"
                f" role, position, position_source, position_note, time_updated)"
                f" VALUES (?,?,?,?,?,?,?,?)",
                (author_id, stable_book_id, book_id, r,
                 position or "unknown", position_source, position_note, now))
        conn.commit()

    def books_of(self, author_id: str) -> list[dict[str, Any]]:
        """这个作者挂着的所有书（含 role / position）。"""
        rows = self.get_conn().execute(
            f"SELECT * FROM {TABLE_BOOKS} WHERE author_id=?"
            f" ORDER BY time_updated DESC", (author_id,)).fetchall()
        return [dict(r) for r in rows]

    def authors_of_book(self, stable_book_id: str) -> list[dict[str, Any]]:
        """这本书挂着的所有人（作者/合著/译者/编者各一行）。"""
        rows = self.get_conn().execute(
            f"SELECT b.*, a.canonical_name, a.nationality, a.birth_year,"
            f" a.death_year FROM {TABLE_BOOKS} b JOIN {TABLE_AUTHORS} a"
            f" ON a.author_id=b.author_id WHERE b.stable_book_id=?"
            f" ORDER BY b.role", (stable_book_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── 证据 1：语料 ──────────────────────────────────────────────────────

    def book_source_id(self, stable_book_id: str) -> str:
        """书对应的来源行（没有就建）。v3 起每条语料都要能指回来源。"""
        cu = f"book:{stable_book_id}"
        row = self.get_conn().execute(
            "SELECT id FROM author_sources WHERE canonical_url=?", (cu,)).fetchone()
        if row:
            return row["id"]
        return self.upsert_source(
            canonical_url=cu, source_kind="book", source_tier=1,
            license_note="short_quote_only")

    def add_corpus(self, author_id: str, stable_book_id: str, excerpt: str, *,
                   chunk_id: str | None = None, book_id: str | None = None,
                   chapter_index: int | None = None,
                   chunk_index: int | None = None, role: str = "author",
                   speaker: str = "author", text_kind: str = "",
                   era_year: int | None = None, era_note: str = "",
                   source: str = "corpus",
                   trust: float | None = None) -> bool:
        """记一条**书内**语料。返回 False = 已存在（幂等，不是失败）。

        幂等键是 ``(author_id, stable_book_id, chunk_id, excerpt)`` ——
        不依赖 ``source_id``，因为 v3 之前写入的行没有来源，而重建后它们
        都有了；按行身份查重才能让「重跑」在迁移前后行为一致。

        已存在时**只补缺失的坐标**：剧透门按章号算，早期落下的 NULL 坐标
        不补的话，「修好了」就是一句空话。
        """
        ex = (excerpt or "").strip()
        if not author_id or not stable_book_id or not ex:
            return False
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT id FROM {TABLE_CORPUS} WHERE author_id=?"
            " AND stable_book_id=? AND IFNULL(chunk_id,'')=?"
            " AND excerpt=? LIMIT 1",
            (author_id, stable_book_id, chunk_id or "", ex)).fetchone()
        if row:
            conn.execute(
                f"UPDATE {TABLE_CORPUS} SET"
                f" chapter_index=COALESCE(chapter_index, ?),"
                f" chunk_index=COALESCE(chunk_index, ?),"
                f" chunk_id=COALESCE(NULLIF(chunk_id,''), ?),"
                f" era_year=COALESCE(era_year, ?)"
                f" WHERE id=?",
                (chapter_index, chunk_index, chunk_id, era_year, row["id"]))
            conn.commit()
            return False
        self.add_corpus_v2(
            author_id, ex, source_id=self.book_source_id(stable_book_id),
            stable_book_id=stable_book_id, book_id=book_id, chunk_id=chunk_id,
            chapter_index=chapter_index, chunk_index=chunk_index, role=role,
            speaker=speaker, narrative_layer="paratext", text_kind=text_kind,
            era_year=era_year, era_note=era_note, source=source, trust=trust)
        conn.commit()
        return True

    def add_corpus_v2(self, author_id: str, excerpt: str, *,
                      source_id: str | None = None,
                      stable_book_id: str | None = None,
                      book_id: str | None = None,
                      chunk_id: str | None = None,
                      chapter_index: int | None = None,
                      chunk_index: int | None = None,
                      role: str = "author", speaker: str = "author",
                      narrative_layer: str = "paratext",
                      text_kind: str = "", scene_type: str = "",
                      pov: str = "", scene_note: str = "", period: str = "",
                      era_year: int | None = None, era_note: str = "",
                      source: str = "corpus",
                      trust: float | None = None) -> int | None:
        """v2 语料写入：书内段落与外部访谈共用一条路。

        幂等键分两种（见 ``CORPUS_V2_INDEXES``）：书内行按
        ``(source_id, chunk_id, excerpt)``，外部行按 ``(source_id, excerpt)``。
        已存在时返回**已有 id**，方便把 trait 证据挂上去。
        """
        ex = (excerpt or "").strip()
        if not author_id or not ex:
            return None
        src = source if source in SOURCES else "model"
        t = SOURCE_TRUST.get(src, 0.2) if trust is None else float(trust)
        conn = self.get_conn()
        # **先查再插**：``INSERT OR IGNORE`` 也会推进 AUTOINCREMENT 序列，
        # 于是「重跑复用旧语料」会让下一次新行的 id 跳号（实测 e2e：本该
        # 7 起跳到了 13）。id 是 trait 证据的锚，不该随重跑次数漂移。
        if stable_book_id:
            row = conn.execute(
                f"SELECT id FROM {TABLE_CORPUS} WHERE author_id=?"
                f" AND stable_book_id=? AND IFNULL(chunk_id,'')=?"
                f" AND excerpt=? ORDER BY id LIMIT 1",
                (author_id, stable_book_id, chunk_id or "", ex)).fetchone()
        else:
            row = conn.execute(
                f"SELECT id FROM {TABLE_CORPUS} WHERE author_id=?"
                f" AND stable_book_id IS NULL AND IFNULL(source_id,'')=?"
                f" AND excerpt=? ORDER BY id LIMIT 1",
                (author_id, source_id or "", ex)).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {TABLE_CORPUS} (author_id, source_id,"
            f" stable_book_id, book_id, chunk_id, chapter_index, chunk_index,"
            f" excerpt, role, speaker, narrative_layer, text_kind, scene_type,"
            f" pov, scene_note, period, word_count, era_year, era_note,"
            f" source, trust, time_created)"
            f" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (author_id, source_id, stable_book_id, book_id, chunk_id,
             chapter_index, chunk_index, ex, role, speaker, narrative_layer,
             text_kind, scene_type, pov, scene_note, period, len(ex),
             era_year, era_note, src, t, _now_ms()))
        conn.commit()
        if cur.rowcount > 0 and cur.lastrowid:
            return int(cur.lastrowid)
        if stable_book_id:
            row = conn.execute(
                f"SELECT id FROM {TABLE_CORPUS} WHERE author_id=?"
                f" AND stable_book_id=? AND IFNULL(chunk_id,'')=?"
                f" AND excerpt=? ORDER BY id LIMIT 1",
                (author_id, stable_book_id, chunk_id or "", ex)).fetchone()
        else:
            row = conn.execute(
                f"SELECT id FROM {TABLE_CORPUS} WHERE author_id=?"
                f" AND stable_book_id IS NULL AND IFNULL(source_id,'')=?"
                f" AND excerpt=? ORDER BY id LIMIT 1",
                (author_id, source_id or "", ex)).fetchone()
        return int(row["id"]) if row else None

    def corpus_for(self, author_id: str, *, speaker: str | None = None,
                   text_kind: str | None = None,
                   narrative_layer: str | None = None,
                   scene_type: str | None = None,
                   source: str | None = None,
                   max_era_year: int | None = None,
                   limit: int = 200) -> list[dict[str, Any]]:
        """取语料。``max_era_year`` 是**剧透门的入口**（读者读到某年之前）。

        无年的条目在给了 ``max_era_year`` 时**一律不返回** —— fail-closed，
        与剧透门「无锚点一律不展示」同一条。
        """
        sql = f"SELECT * FROM {TABLE_CORPUS} WHERE author_id=?"
        args: list[Any] = [author_id]
        if speaker:
            sql += " AND speaker=?"; args.append(speaker)
        if text_kind:
            sql += " AND text_kind=?"; args.append(text_kind)
        if narrative_layer:
            sql += " AND narrative_layer=?"; args.append(narrative_layer)
        if scene_type:
            sql += " AND scene_type=?"; args.append(scene_type)
        if source:
            sql += " AND source=?"; args.append(source)
        if max_era_year is not None:
            sql += " AND era_year IS NOT NULL AND era_year<=?"
            args.append(max_era_year)
        sql += " ORDER BY era_year IS NULL, era_year, chapter_index, chunk_index"
        sql += " LIMIT ?"; args.append(limit)
        return [dict(r) for r in self.get_conn().execute(sql, args).fetchall()]

    # ── 证据 2：编年条目 ──────────────────────────────────────────────────

    def add_record(self, author_id: str, kind: str, statement: str, *,
                   year: int | None = None, year_end: int | None = None,
                   evidence_excerpt: str = "",
                   stable_book_id: str | None = None,
                   chunk_id: str | None = None, source: str = "model",
                   trust: float | None = None) -> bool:
        """记一条关于作者的事实。返回 False = 已存在（幂等）。"""
        _rid, created = self._upsert_record(
            author_id, kind, statement, year=year, year_end=year_end,
            evidence_excerpt=evidence_excerpt, stable_book_id=stable_book_id,
            chunk_id=chunk_id, source=source, trust=trust)
        return created

    def add_record_ex(self, author_id: str, kind: str, statement: str, *,
                      year: int | None = None, year_end: int | None = None,
                      evidence_excerpt: str = "",
                      stable_book_id: str | None = None,
                      chunk_id: str | None = None, source: str = "model",
                      trust: float | None = None) -> int | None:
        """同 ``add_record``，但返回 ``record.id`` —— 证据要挂在这上面。

        已存在时返回**已有行的 id**（不是 None）：调用方要拿它去补证据，
        “这条早就记过”不等于“这条不用挂出处”。
        """
        rid, _created = self._upsert_record(
            author_id, kind, statement, year=year, year_end=year_end,
            evidence_excerpt=evidence_excerpt, stable_book_id=stable_book_id,
            chunk_id=chunk_id, source=source, trust=trust)
        return rid

    def _upsert_record(self, author_id: str, kind: str, statement: str, *,
                       year: int | None = None, year_end: int | None = None,
                       evidence_excerpt: str = "",
                       stable_book_id: str | None = None,
                       chunk_id: str | None = None, source: str = "model",
                       trust: float | None = None) -> tuple[int | None, bool]:
        """返回 ``(record_id, 是否新建)`` —— 布尔语义与 id 语义的唯一实现。"""
        st = (statement or "").strip()
        if not author_id or not st:
            return None, False
        src = source if source in SOURCES else "model"
        t = SOURCE_TRUST.get(src, 0.2) if trust is None else float(trust)
        k = kind if kind in RECORD_KINDS else "life_event"
        conn = self.get_conn()
        dup = conn.execute(
            f"SELECT id FROM {TABLE_RECORDS} WHERE author_id=? AND kind=?"
            f" AND statement=? AND IFNULL(year,-1)=IFNULL(?,-1)",
            (author_id, k, st, year)).fetchone()
        if dup:
            return int(dup["id"]), False
        cur = conn.execute(
            f"INSERT INTO {TABLE_RECORDS} (author_id, kind, year, year_end,"
            f" statement, evidence_excerpt, stable_book_id, chunk_id, source,"
            f" trust, time_created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (author_id, k, year, year_end, st, evidence_excerpt,
             stable_book_id, chunk_id, src, t, _now_ms()))
        conn.commit()
        return (int(cur.lastrowid) if cur.lastrowid else None), True

    def records_for(self, author_id: str, *, kinds: list[str] | None = None,
                    max_year: int | None = None,
                    min_trust: float = 0.0,
                    limit: int = 200) -> list[dict[str, Any]]:
        """取编年条目。``max_year`` 是**剧透门在作者层的落点**。

        与语料一样 fail-closed：给了 ``max_year`` 时**无年的条目不返回** ——
        定位不到时间线就无法证明安全，这是正确的代价，但要能计数报出来
        （见 ``stats`` 的 ``undated``）。
        """
        sql = f"SELECT * FROM {TABLE_RECORDS} WHERE author_id=? AND trust>=?"
        args: list[Any] = [author_id, min_trust]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        if max_year is not None:
            sql += " AND year IS NOT NULL AND year<=?"; args.append(max_year)
        sql += " ORDER BY year IS NULL, year, id LIMIT ?"; args.append(limit)
        return [dict(r) for r in self.get_conn().execute(sql, args).fetchall()]

    # ── 身份锚点 ──────────────────────────────────────────────────────────

    def add_identifier(self, author_id: str, scheme: str, value: str, *,
                       source: str = "", confidence: float = 0.5) -> bool:
        """登记外部 ID。``(scheme, value)`` 已属于别人时**不抢**，返回 False。

        抢名会把两个人并成一个，且不可逆 —— 与 ``add_alias`` 同一条纪律。
        """
        if not author_id or not scheme or not value:
            return False
        if scheme not in IDENTIFIER_SCHEMES:
            log.warning("unknown identifier scheme: %s", scheme)
            return False
        row = self.get_conn().execute(
            "SELECT author_id FROM author_identifiers WHERE scheme=? AND value=?",
            (scheme, value)).fetchone()
        if row:
            if row["author_id"] != author_id:
                log.warning("identifier %s:%s 已属于 %s，未改指 %s",
                            scheme, value, row["author_id"], author_id)
                return False
            return True
        conn = self.get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO author_identifiers (author_id, scheme,"
            " value, source, confidence, time_created) VALUES (?,?,?,?,?,?)",
            (author_id, scheme, value, source, float(confidence), _now_ms()))
        conn.commit()
        return True

    def identifiers_of(self, author_id: str) -> list[dict[str, Any]]:
        rows = self.get_conn().execute(
            "SELECT * FROM author_identifiers WHERE author_id=?"
            " ORDER BY confidence DESC, id", (author_id,)).fetchall()
        return [dict(r) for r in rows]

    def find_by_identifier(self, scheme: str, value: str) -> dict[str, Any] | None:
        row = self.get_conn().execute(
            "SELECT a.* FROM author_identifiers i JOIN authors a"
            " ON a.author_id=i.author_id WHERE i.scheme=? AND i.value=?",
            (scheme, value)).fetchone()
        return dict(row) if row else None

    # ── 来源台账 ──────────────────────────────────────────────────────────

    def upsert_source(self, *, url: str = "", canonical_url: str = "",
                      title: str = "", publisher: str = "",
                      source_tier: int = 4, source_kind: str = "unknown",
                      language: str = "", published_at: str = "",
                      content_hash: str = "", content_excerpt: str = "",
                      license_note: str = "short_quote_only",
                      status: str = "active") -> str:
        """按 ``canonical_url`` 幂等写入来源，返回 source_id。

        重抓同一页只更新元数据，**不新建行** —— 否则证据会挂到不同 id 上，
        “同一来源支撑了几条”永远数不准。
        """
        from authorgraph.sources.registry import canonical_url as _canon
        from authorgraph.sources.registry import source_id_for
        cu = (canonical_url or _canon(url) or url).strip()
        if not cu:
            return ""
        sid = source_id_for(cu)
        now = _now_ms()
        conn = self.get_conn()
        conn.execute(
            "INSERT INTO author_sources (id, url, canonical_url, title,"
            " publisher, source_tier, source_kind, language, published_at,"
            " retrieved_at, content_hash, content_excerpt, license_note,"
            " status, time_created, time_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(canonical_url) DO UPDATE SET"
            "  url=excluded.url, title=excluded.title,"
            "  publisher=excluded.publisher, source_tier=excluded.source_tier,"
            "  source_kind=excluded.source_kind, language=excluded.language,"
            "  published_at=excluded.published_at,"
            "  retrieved_at=excluded.retrieved_at,"
            "  content_hash=excluded.content_hash,"
            "  content_excerpt=excluded.content_excerpt,"
            "  status=excluded.status, time_updated=excluded.time_updated",
            (sid, url, cu, title, publisher, int(source_tier), source_kind,
             language, published_at, now, content_hash, content_excerpt,
             license_note, status, now, now))
        conn.commit()
        row = conn.execute("SELECT id FROM author_sources WHERE canonical_url=?",
                           (cu,)).fetchone()
        return row["id"] if row else sid

    def add_source_roles(self, source_id: str, roles: tuple[str, ...] | list[str]) -> None:
        conn = self.get_conn()
        for role in roles or ():
            conn.execute("INSERT OR IGNORE INTO author_source_roles"
                         " (source_id, role) VALUES (?, ?)", (source_id, role))
        conn.commit()

    def source_roles(self, source_id: str) -> list[str]:
        return [r["role"] for r in self.get_conn().execute(
            "SELECT role FROM author_source_roles WHERE source_id=?",
            (source_id,)).fetchall()]

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        row = self.get_conn().execute(
            "SELECT * FROM author_sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def sources_of_author(self, author_id: str) -> list[dict[str, Any]]:
        """这个作者有证据落点的来源，带证据计数与角色。

        ``quote_verified`` 只数 ``supports`` 且引文非空的行 —— 旧版迁移来的
        ``legacy_url_only`` 不算。档案页据此区分「已核验」与「只有网址」。
        """
        rows = self.get_conn().execute(
            "SELECT s.*, COUNT(DISTINCT e.id) AS evidence_count,"
            " SUM(CASE WHEN e.relation='supports' AND e.quote<>''"
            "     THEN 1 ELSE 0 END) AS quote_verified,"
            " SUM(CASE WHEN e.relation='legacy_url_only'"
            "     THEN 1 ELSE 0 END) AS legacy_only,"
            " (SELECT GROUP_CONCAT(r.role, ',') FROM author_source_roles r"
            "  WHERE r.source_id=s.id) AS roles_csv"
            " FROM author_sources s JOIN author_evidence e"
            " ON e.source_id=s.id"
            " WHERE e.author_id=?"
            " GROUP BY s.id"
            " ORDER BY s.source_tier, s.title, s.id", (author_id,)).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["roles"] = [x for x in (d.pop("roles_csv") or "").split(",") if x]
            d["evidence_count"] = int(d.get("evidence_count") or 0)
            d["quote_verified"] = int(d.get("quote_verified") or 0)
            d["legacy_only"] = int(d.get("legacy_only") or 0)
            out.append(d)
        return out

    # ── 逐字证据 ──────────────────────────────────────────────────────────

    def add_evidence(self, author_id: str, target_type: str, target_id: str, *,
                     source_id: str | None = None, quote: str = "",
                     locator: str = "", relation: str = "supports",
                     confidence: float = 0.5) -> str:
        """挂一条证据。``supports/contradicts`` 的 ``quote`` 必须非空。"""
        q = (quote or "").strip()
        if relation in ("supports", "contradicts") and not q:
            log.warning("evidence without quote rejected: %s/%s",
                        target_type, target_id)
            return ""
        if target_type not in EVIDENCE_TARGETS:
            log.warning("unknown evidence target_type: %s", target_type)
            return ""
        rel = relation if relation in EVIDENCE_RELATIONS else "supports"
        eid = _evidence_id(target_type, str(target_id), source_id or "", q)
        conn = self.get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO author_evidence (id, author_id,"
            " target_type, target_id, source_id, quote, locator, relation,"
            " confidence, time_created) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, author_id, target_type, str(target_id), source_id, q,
             locator or "", rel, float(confidence), _now_ms()))
        conn.commit()
        return eid

    def evidence_for(self, target_type: str, target_id: str) -> list[dict[str, Any]]:
        rows = self.get_conn().execute(
            "SELECT e.*, s.url AS source_url, s.canonical_url,"
            " s.title AS source_title, s.source_tier, s.source_kind,"
            " s.status AS source_status FROM author_evidence e"
            " LEFT JOIN author_sources s ON s.id=e.source_id"
            " WHERE e.target_type=? AND e.target_id=?"
            " ORDER BY e.relation, e.id", (target_type, str(target_id))).fetchall()
        return [dict(r) for r in rows]

    def evidence_for_author(self, author_id: str) -> list[dict[str, Any]]:
        rows = self.get_conn().execute(
            "SELECT * FROM author_evidence WHERE author_id=?"
            " ORDER BY time_created DESC, id", (author_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── 刷新标记（作者级）────────────────────────────────────────────────
    #
    # 书级采集（读序跋）挂在导入后、按块幂等；但**作者级**的活（身份归一化、
    # 编年合并、生平补全）是跨书累积的 —— 导入第 3 本张爱玲，前两本的编年
    # 也要重算。所以这里只标脏，重算由 ``scripts/refresh_author.py`` 独立跑。

    def mark_dirty(self, author_id: str) -> None:
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_AUTHORS} SET needs_refresh=1, time_updated=?"
            f" WHERE author_id=?", (_now_ms(), author_id))
        conn.commit()

    def mark_dirty_by_book(self, stable_book_id: str) -> int:
        """这本书挂着的所有作者标脏。返回标了几个。"""
        rows = self.get_conn().execute(
            f"SELECT DISTINCT author_id FROM {TABLE_BOOKS} WHERE stable_book_id=?",
            (stable_book_id,)).fetchall()
        for r in rows:
            self.mark_dirty(r["author_id"])
        return len(rows)

    def dirty_authors(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.get_conn().execute(
            f"SELECT * FROM {TABLE_AUTHORS} WHERE needs_refresh=1"
            f" ORDER BY time_updated LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def clear_dirty(self, author_id: str) -> None:
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_AUTHORS} SET needs_refresh=0, last_refresh_at=?"
            f" WHERE author_id=?", (_now_ms(), author_id))
        conn.commit()

    # ── 解释性陈述（主题/接受史/诗学/背景）────────────────────────────────

    def add_claim(self, author_id: str, facet: str, statement: str, *,
                  theme_key: str = "", year: int | None = None,
                  confidence: float = 0.3,
                  status: str = "provisional") -> str | None:
        """记一条解释性陈述。返回 claim_id；同作者同 facet 同文去重。"""
        st = (statement or "").strip()
        if not author_id or not st or facet not in CLAIM_FACETS:
            return None
        conn = self.get_conn()
        dup = conn.execute(
            "SELECT claim_id FROM author_claims WHERE author_id=? AND facet=?"
            " AND statement=?", (author_id, facet, st)).fetchone()
        if dup:
            return dup["claim_id"]
        cid = f"cl-{_new_id().split('-', 1)[1]}"
        now = _now_ms()
        conn.execute(
            "INSERT INTO author_claims (claim_id, author_id, facet, theme_key,"
            " statement, year, confidence, status, time_created, time_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, author_id, facet, theme_key or "", st, year,
             float(confidence), status, now, now))
        conn.commit()
        return cid

    def claims_for(self, author_id: str, *, facet: str | None = None,
                   limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM author_claims WHERE author_id=?"
        args: list[Any] = [author_id]
        if facet:
            sql += " AND facet=?"; args.append(facet)
        sql += " ORDER BY year IS NULL, year, claim_id LIMIT ?"; args.append(limit)
        return [dict(r) for r in self.get_conn().execute(sql, args).fetchall()]

    # ── 结构化关系 ────────────────────────────────────────────────────────

    def add_relation(self, author_id: str, other_name: str, *,
                     relation_type: str = "other", other_author_id: str = "",
                     direction: str = "outgoing", year_start: int | None = None,
                     year_end: int | None = None, statement: str = "",
                     confidence: float = 0.3,
                     status: str = "provisional") -> str | None:
        """写一个人际/影响/出版关系。幂等键与 ``ux_arel_unique`` 一致。"""
        name = (other_name or "").strip()
        if not author_id or not name:
            return None
        rtype = relation_type if relation_type in RELATION_TYPES else "other"
        conn = self.get_conn()
        row = conn.execute(
            "SELECT relation_id FROM author_relations WHERE author_id=?"
            " AND other_name=? AND relation_type=?"
            " AND COALESCE(year_start, -1)=?",
            (author_id, name, rtype, year_start if year_start is not None else -1)
        ).fetchone()
        now = _now_ms()
        if row:
            conn.execute(
                "UPDATE author_relations SET other_author_id=?, direction=?,"
                " year_end=?, statement=?, confidence=?, status=?,"
                " time_updated=? WHERE relation_id=?",
                (other_author_id or None, direction, year_end,
                 (statement or "").strip(), float(confidence), status, now,
                 row["relation_id"]))
            conn.commit()
            return row["relation_id"]
        rid = f"rel-{uuid.uuid4().hex[:16]}"
        conn.execute(
            "INSERT INTO author_relations (relation_id, author_id, other_name,"
            " other_author_id, relation_type, direction, year_start, year_end,"
            " statement, confidence, status, time_created, time_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, author_id, name, other_author_id or None, rtype, direction,
             year_start, year_end, (statement or "").strip(),
             float(confidence), status, now, now))
        conn.commit()
        return rid

    def relations_of(self, author_id: str) -> list[dict[str, Any]]:
        """一跳关系，带对方作者行（若对方也在作者层）。"""
        rows = self.get_conn().execute(
            "SELECT r.*, a.canonical_name AS other_canonical,"
            " a.identity_status AS other_identity_status,"
            " a.nationality AS other_nationality,"
            " a.birth_year AS other_birth_year,"
            " a.death_year AS other_death_year"
            " FROM author_relations r LEFT JOIN authors a"
            " ON a.author_id=r.other_author_id"
            " WHERE r.author_id=?"
            " ORDER BY r.relation_type, r.year_start IS NULL, r.year_start,"
            " r.other_name", (author_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── 身份字段 / 状态机 ─────────────────────────────────────────────────

    def set_identity_fields(self, author_id: str, *,
                            name_native: str | None = None,
                            birth_place: str | None = None,
                            death_place: str | None = None,
                            primary_language: str | None = None,
                            nationality: str | None = None,
                            birth_year: int | None = None,
                            death_year: int | None = None,
                            identity_status: str | None = None) -> None:
        """回填身份字段。**只写给了的** —— None 是「这次不知道」。

        生卒冲突不走这里：调用方发现冲突应写 ``identity_status='conflict'``，
        而不是挑一个值覆盖旧值（设计稿 03 §五）。
        """
        sets: list[str] = []
        vals: list[Any] = []
        for col, val in (("name_native", name_native),
                         ("birth_place", birth_place),
                         ("death_place", death_place),
                         ("primary_language", primary_language),
                         ("nationality", nationality),
                         ("birth_year", birth_year),
                         ("death_year", death_year)):
            if val is None:
                continue
            if isinstance(val, str) and not val.strip():
                continue
            sets.append(f"{col}=?"); vals.append(val)
        if identity_status in IDENTITY_STATUSES:
            sets.append("identity_status=?"); vals.append(identity_status)
        if not sets:
            return
        sets.append("time_updated=?"); vals += [_now_ms(), author_id]
        conn = self.get_conn()
        conn.execute(f"UPDATE {TABLE_AUTHORS} SET {', '.join(sets)}"
                     " WHERE author_id=?", vals)
        conn.commit()

    def set_bio_status(self, author_id: str, status: str,
                       error: str = "") -> None:
        """``none/partial/full/stale/error`` + 最近一次失败原因（短句）。"""
        st = status if status in BIO_STATUSES else "partial"
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_AUTHORS} SET bio_status=?, bio_error=?,"
            " time_updated=? WHERE author_id=?",
            (st, (error or "")[:300], _now_ms(), author_id))
        conn.commit()

    def set_voice_status(self, author_id: str, stable_book_id: str,
                         status: str, *, content_hash: str = "",
                         role: str = "") -> None:
        """书级声音状态。M2 的触发器只改这一列，不碰语料。

        ``role`` 留空 = 这个人在本书上的所有关系行一起写（通常只有一行）。
        不按 role 过滤是因为合著者可能只以 ``co-author`` 挂书，按
        ``role='author'`` 更新会静默 0 行。
        """
        st = status if status in VOICE_STATUSES else "none"
        conn = self.get_conn()
        sql = (f"UPDATE {TABLE_BOOKS} SET voice_status=?,"
               " voice_content_hash=COALESCE(NULLIF(?, ''), voice_content_hash),"
               " voice_updated_at=? WHERE author_id=? AND stable_book_id=?")
        args: list[Any] = [st, content_hash, _now_ms(), author_id,
                           stable_book_id]
        if role:
            sql += " AND role=?"
            args.append(role)
        conn.execute(sql, args)
        conn.commit()

    def voice_status_of(self, author_id: str,
                        stable_book_id: str) -> dict[str, Any]:
        row = self.get_conn().execute(
            f"SELECT voice_status, voice_content_hash, voice_updated_at"
            f" FROM {TABLE_BOOKS} WHERE author_id=? AND stable_book_id=?"
            f" ORDER BY role LIMIT 1",
            (author_id, stable_book_id)).fetchone()
        return dict(row) if row else {"voice_status": "none",
                                      "voice_content_hash": "",
                                      "voice_updated_at": None}

    # ── 声音账本（M2）────────────────────────────────────────────────────
    #
    # 这一节是**唯一**写 ``author_voice_*`` 的地方。生成侧
    # （``authorgraph/voice/``）不认识 SQL，库不认识模型输出。

    def voice_corpus_of_book(self, author_id: str, stable_book_id: str,
                             *, layers: tuple[str, ...] | None = None
                             ) -> list[dict[str, Any]]:
        """一本书已有的声音语料（默认只要正文层，不含序跋）。"""
        wanted = layers or ("narration", "dialogue", "essay")
        ph = ",".join("?" * len(wanted))
        rows = self.get_conn().execute(
            f"SELECT * FROM {TABLE_CORPUS} WHERE author_id=?"
            f" AND stable_book_id=? AND narrative_layer IN ({ph})"
            f" ORDER BY id", (author_id, stable_book_id, *wanted)).fetchall()
        return [dict(r) for r in rows]

    def voice_corpus_for_scene(self, author_id: str, *,
                               layers: tuple[str, ...] = ("narration", "essay"),
                               scene_type: str = "", period: str = "",
                               limit: int = 40) -> list[dict[str, Any]]:
        """写作透镜的候选语料（只读）。**只取正文层**，序跋/访谈进不来。"""
        want = layers or ("narration", "essay")
        ph = ",".join("?" * len(want))
        sql = (f"SELECT c.*, s.source_tier, s.canonical_url"
               f" FROM {TABLE_CORPUS} c LEFT JOIN author_sources s"
               f" ON s.id=c.source_id"
               f" WHERE c.author_id=? AND c.narrative_layer IN ({ph})")
        args: list[Any] = [author_id, *want]
        if scene_type:
            sql += " AND c.scene_type=?"; args.append(scene_type)
        if period:
            sql += " AND c.period=?"; args.append(period)
        sql += (" ORDER BY CASE WHEN c.scene_type=? THEN 0 ELSE 1 END,"
                " c.era_year IS NULL, c.era_year, c.id LIMIT ?")
        args += [scene_type or "", int(limit)]
        return [dict(r) for r in self.get_conn().execute(sql, args).fetchall()]

    def voice_samples(self, author_id: str, *,
                      stable_book_id: str = "",
                      layers: tuple[str, ...] = ("narration", "essay"),
                      limit: int = 1000) -> list[dict[str, Any]]:
        """档案页/透镜用的声音样本（只读）。带来源元数据，不含序跋。

        门不在这里施 —— 调用方拿 ``chapter_index`` 自己判，因为「读到第几章」
        是读者上下文，不是库的属性。
        """
        want = layers or ("narration", "essay")
        ph = ",".join("?" * len(want))
        sql = (f"SELECT c.*, s.source_tier, s.canonical_url,"
               f" s.source_kind, s.title AS source_title,"
               f" s.status AS source_status"
               f" FROM {TABLE_CORPUS} c LEFT JOIN author_sources s"
               f" ON s.id=c.source_id"
               f" WHERE c.author_id=? AND c.narrative_layer IN ({ph})")
        args: list[Any] = [author_id, *want]
        if stable_book_id:
            sql += " AND c.stable_book_id=?"; args.append(stable_book_id)
        sql += (" ORDER BY c.scene_type, c.chapter_index IS NULL,"
                " c.chapter_index, c.chunk_index, c.id LIMIT ?")
        args.append(max(1, int(limit)))
        return [dict(r) for r in self.get_conn().execute(sql, args).fetchall()]

    def clear_book_voice_corpus(self, stable_book_id: str,
                                author_id: str = "") -> dict[str, int]:
        """清掉这本书的**声音语料**（序跋保留），并让失去证据的 trait 退役。

        ``--force`` 只清本书 —— 与 ``clear_book`` 同一条纪律。不一样的是
        这里**不能连序跋一起删**：序跋是事实采集与扮演的输入，重跑声音
        不该把它抹掉。给了 ``author_id`` 就只清这个人的（合著者共用一本书）。
        """
        conn = self.get_conn()
        sql = (f"DELETE FROM {TABLE_CORPUS} WHERE stable_book_id=?"
               f" AND narrative_layer<>'paratext'")
        args: list[Any] = [stable_book_id]
        if author_id:
            sql += " AND author_id=?"
            args.append(author_id)
        cur = conn.execute(sql, args)
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        # 证据随语料级联删除；只剩空壳的 trait 不能继续冒充「有证据」。
        cur2 = conn.execute(
            "UPDATE author_voice_traits SET status='superseded',"
            " time_updated=? WHERE status IN ('active','provisional')"
            " AND trait_id NOT IN (SELECT DISTINCT trait_id"
            " FROM author_voice_trait_evidence)", (_now_ms(),))
        orphaned = cur2.rowcount if cur2.rowcount and cur2.rowcount > 0 else 0
        conn.commit()
        return {"corpus": removed, "orphaned_traits": orphaned}

    def update_corpus_meta(self, corpus_id: int, *, scene_type: str = "",
                           pov: str = "", period: str = "",
                           scene_note: str = "") -> None:
        """模型对样本的纠正（场景/视角/时期）回写到语料行。

        只写**非空**字段 —— 模型没说的不等于模型说「没有」。
        """
        sets: list[str] = []
        vals: list[Any] = []
        if scene_type:
            sets.append("scene_type=?")
            vals.append(scene_type if scene_type in SCENE_TYPES else "other")
        if pov:
            sets.append("pov=?"); vals.append(pov[:16])
        if period:
            sets.append("period=?"); vals.append(period[:32])
        if scene_note:
            sets.append("scene_note=?"); vals.append(scene_note[:300])
        if not sets:
            return
        vals.append(int(corpus_id))
        conn = self.get_conn()
        conn.execute(f"UPDATE {TABLE_CORPUS} SET {', '.join(sets)}"
                     " WHERE id=?", vals)
        conn.commit()

    def add_voice_trait(self, author_id: str, dimension: str, value: str,
                        statement: str, *, voice_domain: str = "unknown",
                        strength: float = 0.5,
                        scope_type: str = "work", scope_ref: str = "",
                        transferable: bool = True, confidence: float = 0.3,
                        status: str = "provisional", revision: str = "",
                        evidence_ids: tuple[int, ...] = ()) -> str | None:
        """写一条文风特征（幂等：同一句话重跑只补证据、不新增行）。

        新 ``revision`` 会把**同一维度/同一范围**的旧 trait 标成
        ``superseded`` —— 只改状态、不删行，证据链要能追溯。
        """
        st = (statement or "").strip()
        if dimension not in VOICE_DIMENSIONS or not st:
            return None
        domain = voice_domain if voice_domain in VOICE_DOMAINS else "unknown"
        stat = status if status in VOICE_TRAIT_STATUSES else "provisional"
        tid = _trait_id(author_id, dimension, domain, scope_type,
                        scope_ref, st)
        now = _now_ms()
        conn = self.get_conn()
        row = conn.execute(
            "SELECT trait_id FROM author_voice_traits WHERE trait_id=?",
            (tid,)).fetchone()
        if row:
            conn.execute(
                "UPDATE author_voice_traits SET value=?, strength=?,"
                " transferable=?, confidence=?, status=?, revision=?,"
                " time_updated=? WHERE trait_id=?",
                (value, float(strength), 1 if transferable else 0,
                 float(confidence), stat, revision, now, tid))
        else:
            conn.execute(
                "INSERT INTO author_voice_traits (trait_id, author_id,"
                " dimension, voice_domain, value, statement, strength,"
                " scope_type, scope_ref, transferable, confidence, status,"
                " revision, time_created, time_updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, author_id, dimension, domain, value, st, float(strength),
                 scope_type, scope_ref, 1 if transferable else 0,
                 float(confidence), stat, revision, now, now))
        if revision:
            conn.execute(
                "UPDATE author_voice_traits SET status='superseded',"
                " time_updated=? WHERE author_id=? AND dimension=?"
                " AND voice_domain=? AND scope_type=? AND scope_ref=?"
                " AND trait_id<>? AND status IN ('active','provisional')"
                " AND revision<>?",
                (now, author_id, dimension, domain, scope_type, scope_ref,
                 tid, revision))
        for cid in evidence_ids:
            conn.execute(
                "INSERT OR IGNORE INTO author_voice_trait_evidence"
                " (trait_id, corpus_id, relation) VALUES (?,?,'supports')",
                (tid, int(cid)))
        conn.commit()
        return tid

    def add_trait_evidence(self, trait_id: str, corpus_id: int,
                           relation: str = "supports", note: str = "") -> None:
        conn = self.get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO author_voice_trait_evidence"
            " (trait_id, corpus_id, relation, note) VALUES (?,?,?,?)",
            (trait_id, int(corpus_id), relation, note[:300]))
        conn.commit()

    def voice_traits(self, author_id: str, *,
                     statuses: tuple[str, ...] = ("active",),
                     domains: tuple[str, ...] | None = None,
                     transferable_only: bool = False,
                     scope_type: str = "", scope_ref: str = "",
                     limit: int = 200) -> list[dict[str, Any]]:
        """读 trait。默认只读 ``active`` —— ``superseded`` 是账，不是结论。"""
        sql = "SELECT * FROM author_voice_traits WHERE author_id=?"
        args: list[Any] = [author_id]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if domains:
            sql += f" AND voice_domain IN ({','.join('?' * len(domains))})"
            args += list(domains)
        if transferable_only:
            sql += " AND transferable=1"
        if scope_type:
            sql += " AND scope_type=?"; args.append(scope_type)
        if scope_ref:
            sql += " AND scope_ref=?"; args.append(scope_ref)
        sql += " ORDER BY confidence DESC, strength DESC, dimension LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.get_conn().execute(sql, args).fetchall()]

    def trait_evidence(self, trait_id: str) -> list[dict[str, Any]]:
        rows = self.get_conn().execute(
            "SELECT e.relation, e.note, c.*, s.source_tier, s.canonical_url,"
            " s.source_kind, s.title AS source_title,"
            " s.status AS source_status"
            f" FROM author_voice_trait_evidence e"
            f" JOIN {TABLE_CORPUS} c ON c.id=e.corpus_id"
            " LEFT JOIN author_sources s ON s.id=c.source_id"
            " WHERE e.trait_id=? ORDER BY c.id", (trait_id,)).fetchall()
        return [dict(r) for r in rows]

    def start_voice_run(self, author_id: str, *, book_id: str = "",
                        stable_book_id: str = "", extraction_run_id: str = "",
                        scout_plan_hash: str = "", sample_ids: list[int] | None = None,
                        model: str = "", prompt_version: str = ""
                        ) -> str:
        rid = f"vr-{uuid.uuid4().hex[:16]}"
        conn = self.get_conn()
        now = _now_ms()
        # 上一次进程被杀会留下 ``running`` 的孤儿行。先收尸再开工，
        # 否则「正在跑」会一直挂在台账上，也没法判断上次是不是中途断了。
        conn.execute(
            "UPDATE author_voice_runs SET status='failed',"
            " error_code='interrupted', finished_at=?"
            " WHERE author_id=? AND IFNULL(stable_book_id,'')=?"
            " AND status='running'",
            (now, author_id, stable_book_id or ""))
        conn.execute(
            "INSERT INTO author_voice_runs (run_id, author_id, book_id,"
            " stable_book_id, extraction_run_id, scout_plan_hash,"
            " sample_ids_json, model, prompt_version, status, started_at)"
            " VALUES (?,?,?,?,?,?,?,?,?, 'running', ?)",
            (rid, author_id, book_id or None, stable_book_id or None,
             extraction_run_id, scout_plan_hash,
             json.dumps(sample_ids or [], ensure_ascii=False),
             model, prompt_version, _now_ms()))
        conn.commit()
        return rid

    def set_voice_run_samples(self, run_id: str,
                              corpus_ids: list[int]) -> None:
        """语料落库后回填样本 id —— run 要能追到「看了哪几段」。"""
        conn = self.get_conn()
        conn.execute(
            "UPDATE author_voice_runs SET sample_ids_json=?,"
            " corpus_count=? WHERE run_id=?",
            (json.dumps(list(corpus_ids), ensure_ascii=False),
             len(corpus_ids), run_id))
        conn.commit()

    def finish_voice_run(self, run_id: str, status: str, *,
                         error_code: str = "", corpus_count: int = 0,
                         manifest: dict[str, Any] | None = None) -> None:
        st = status if status in VOICE_STATUSES else "failed"
        conn = self.get_conn()
        conn.execute(
            "UPDATE author_voice_runs SET status=?, error_code=?,"
            " corpus_count=?, manifest=?, finished_at=? WHERE run_id=?",
            (st, (error_code or "")[:80], int(corpus_count),
             json.dumps(manifest or {}, ensure_ascii=False), _now_ms(), run_id))
        conn.commit()

    def last_voice_run(self, author_id: str,
                       stable_book_id: str) -> dict[str, Any] | None:
        row = self.get_conn().execute(
            "SELECT * FROM author_voice_runs WHERE author_id=?"
            " AND stable_book_id=? ORDER BY started_at DESC LIMIT 1",
            (author_id, stable_book_id)).fetchone()
        return dict(row) if row else None

    def voice_status_summary(self, author_id: str) -> dict[str, Any]:
        """作者级声音状态 —— 写作透镜只认 ``ready/partial``（设计稿 06 §五）。

        一本书失败不等于全部失败：按优先级汇总，并保留每本书的计数，
        调用方能说清「为什么不能用」。
        """
        rows = self.get_conn().execute(
            f"SELECT voice_status, COUNT(*) AS n FROM {TABLE_BOOKS}"
            " WHERE author_id=? GROUP BY voice_status", (author_id,)).fetchall()
        counts = {str(r[0] or "none"): int(r[1] or 0) for r in rows}
        if counts.get("ready"):
            status = "ready"
        elif counts.get("partial"):
            status = "partial"
        elif counts.get("running") or counts.get("pending"):
            status = "pending"
        elif counts.get("failed"):
            status = "failed"
        elif counts.get("stale"):
            status = "stale"
        else:
            status = "none"
        return {"status": status, "counts": counts}

    def voice_coverage(self, author_id: str) -> dict[str, Any]:
        """声音覆盖率 —— 状态判定用，也给人看。

        ``scenes`` / ``dimensions`` 是**不同值**的个数，不是行数：
        同一个场景抽了十段仍然只覆盖一类。
        """
        conn = self.get_conn()
        def _one(sql: str, *a: Any) -> int:
            r = conn.execute(sql, a).fetchone()
            return int(r[0]) if r and r[0] is not None else 0
        return {
            "voice_corpus": _one(
                f"SELECT COUNT(*) FROM {TABLE_CORPUS} WHERE author_id=?"
                " AND narrative_layer IN ('narration','dialogue','essay')",
                author_id),
            "scenes": _one(
                f"SELECT COUNT(DISTINCT scene_type) FROM {TABLE_CORPUS}"
                f" WHERE author_id=? AND scene_type<>''"
                " AND narrative_layer IN ('narration','dialogue','essay')",
                author_id),
            "traits": _one(
                "SELECT COUNT(*) FROM author_voice_traits WHERE author_id=?"
                " AND status='active'", author_id),
            "dimensions": _one(
                "SELECT COUNT(DISTINCT dimension) FROM author_voice_traits"
                " WHERE author_id=? AND status='active'", author_id),
            "books_ready": _one(
                f"SELECT COUNT(*) FROM {TABLE_BOOKS} WHERE author_id=?"
                " AND voice_status IN ('ready','partial')", author_id),
        }

    # ── 体检 ──────────────────────────────────────────────────────────────

    def stats(self, author_id: str) -> dict[str, Any]:
        """一个作者的台账概览。**含缺陷计数** —— 沉默不是成功。

        ``undated`` 是**无年条目数**：它们在有门的场合一条都出不去，
        所以必须报出来，否则「作者层怎么这么空」查不出原因。
        ``by_source`` 里 ``model`` 的条数是**不可注入**的那部分。
        """
        conn = self.get_conn()
        def _one(sql: str, *a: Any) -> int:
            r = conn.execute(sql, a).fetchone()
            return int(r[0]) if r and r[0] is not None else 0
        by_speaker = {r["speaker"]: r["n"] for r in conn.execute(
            f"SELECT speaker, COUNT(*) n FROM {TABLE_CORPUS}"
            f" WHERE author_id=? GROUP BY speaker", (author_id,)).fetchall()}
        by_source = {r["source"]: r["n"] for r in conn.execute(
            f"SELECT source, COUNT(*) n FROM {TABLE_CORPUS}"
            f" WHERE author_id=? GROUP BY source", (author_id,)).fetchall()}
        return {
            "author_id": author_id,
            "corpus": _one(f"SELECT COUNT(*) FROM {TABLE_CORPUS} WHERE author_id=?",
                           author_id),
            "records": _one(f"SELECT COUNT(*) FROM {TABLE_RECORDS} WHERE author_id=?",
                            author_id),
            "undated": _one(
                f"SELECT COUNT(*) FROM {TABLE_RECORDS}"
                f" WHERE author_id=? AND year IS NULL", author_id),
            "unanchored": _one(
                f"SELECT COUNT(*) FROM {TABLE_CORPUS}"
                f" WHERE author_id=? AND (chunk_id IS NULL OR chunk_id='')",
                author_id),
            "books": _one(f"SELECT COUNT(*) FROM {TABLE_BOOKS} WHERE author_id=?",
                          author_id),
            "aliases": _one(f"SELECT COUNT(*) FROM {TABLE_ALIASES} WHERE author_id=?",
                            author_id),
            "evidence": _one("SELECT COUNT(*) FROM author_evidence"
                             " WHERE author_id=?", author_id),
            "sources": _one("SELECT COUNT(DISTINCT source_id) FROM"
                            " author_evidence WHERE author_id=?"
                            " AND source_id IS NOT NULL", author_id),
            "identifiers": _one("SELECT COUNT(*) FROM author_identifiers"
                                " WHERE author_id=?", author_id),
            "voice_corpus": _one(
                f"SELECT COUNT(*) FROM {TABLE_CORPUS} WHERE author_id=?"
                " AND narrative_layer<>'paratext'", author_id),
            "by_speaker": by_speaker,
            "by_source": by_source,
        }

    def clear_book(self, stable_book_id: str) -> dict[str, int]:
        """清掉**这一本书**贡献的语料与编年。返回清了多少。

        为什么需要它：``UNIQUE(stable_book_id, chunk_id, excerpt)`` 只挡得住
        **逐字相同**的重复，而模型每轮挑的句子不一样（哪些句子值得收是判断）。
        于是反复重跑会**不断加料**而不是重做 —— 实测两轮下来语料 72 → 122。
        那些行内容上都是真的（逐字校验全过），但台账会失真：
        「我们一共知道多少」变成一个随重跑次数增长的数。

        所以 ``--force`` 走这条路：先清后采 = 真正的重跑。
        只清这一本书的，不碰这个作者的别的书 —— 跨书累积是作者级的事，
        不该被书级重跑抹掉。
        """
        conn = self.get_conn()
        out: dict[str, int] = {}
        for tbl in (TABLE_CORPUS, TABLE_RECORDS):
            cur = conn.execute(
                f"DELETE FROM {tbl} WHERE stable_book_id=?", (stable_book_id,))
            out[tbl] = cur.rowcount
        conn.commit()
        return out

    def dedupe_authors(self, *, dry_run: bool = False) -> list[dict[str, Any]]:
        """把**归一化键相同**的作者合成一个。返回合并台账。

        抄 ``GraphDB.merge_nodes`` 那条教训：**必须搬全部引用表** ——
        只搬一部分会让另一半数据指向一个不存在的 id，**永久失联**。
        计数用 ``cursor.rowcount``，**不用** ``conn.total_changes``
        （那是连接级累计值，实测报出过 4971 这种假数）。

        保留哪一个：语料条数多的（信息量大）；同分则保留先建的
        （``time_created`` 小的）。被并掉的那个正名**降为别名**，
        于是老写法仍然查得到。
        """
        conn = self.get_conn()
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in conn.execute(f"SELECT * FROM {TABLE_AUTHORS}"):
            groups.setdefault(name_key(r["canonical_name"]), []).append(dict(r))
        ledger: list[dict[str, Any]] = []
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            counts = {r["author_id"]: conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_CORPUS} WHERE author_id=?",
                (r["author_id"],)).fetchone()[0] for r in rows}
            rows.sort(key=lambda r: (-counts[r["author_id"]],
                                     r["time_created"] or 0))
            keep, drops = rows[0], rows[1:]
            moved = {"books": 0, "corpus": 0, "records": 0, "aliases": 0}
            for d in drops:
                did, kid = d["author_id"], keep["author_id"]
                if dry_run:
                    ledger.append({"key": key, "keep": keep["canonical_name"],
                                   "drop": d["canonical_name"]})
                    continue
                # 被并掉的正名降为别名 —— 老写法仍查得到
                self.add_alias(d["canonical_name"], kid, source="merged")
                for tbl, col in ((TABLE_BOOKS, "author_id"),
                                 (TABLE_CORPUS, "author_id"),
                                 (TABLE_RECORDS, "author_id")):
                    cur = conn.execute(
                        f"UPDATE OR IGNORE {tbl} SET {col}=? WHERE {col}=?",
                        (kid, did))
                    moved[{"author_books": "books", "author_corpus": "corpus",
                           "author_records": "records"}[tbl]] += cur.rowcount
                # 撞了唯一键没搬过去的（同名同书同 role）—— 留着就是垃圾
                for tbl in (TABLE_BOOKS, TABLE_CORPUS, TABLE_RECORDS):
                    conn.execute(f"DELETE FROM {tbl} WHERE author_id=?", (did,))
                conn.execute(f"DELETE FROM {TABLE_ALIASES} WHERE author_id=?",
                             (did,))
                conn.execute(f"DELETE FROM {TABLE_AUTHORS} WHERE author_id=?",
                             (did,))
                ledger.append({"key": key, "keep": keep["canonical_name"],
                               "keep_id": kid, "drop": d["canonical_name"],
                               "drop_id": did, **moved})
            if not dry_run:
                conn.commit()
        return ledger

    def counts(self) -> dict[str, int]:
        """全库计数（给体检脚本/测试用）。"""
        conn = self.get_conn()
        out: dict[str, int] = {}
        for t in (TABLE_AUTHORS, TABLE_ALIASES, TABLE_BOOKS,
                  TABLE_CORPUS, TABLE_RECORDS):
            r = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            out[t] = int(r[0]) if r else 0
        return out

