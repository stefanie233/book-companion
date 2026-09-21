r"""EPUB structural semantics → chapter_type 枚举的映射与冲突解析。

纯函数模块（零依赖），供 EpubReader（L1 结构信号解析）与
library_service 的 ``_classify_chapter``（L2 硬编码）共用。

章节类型最终收敛到 10 值枚举，消费端（api_server 的 ``_type_label``、
library_service 的 front/back 分组）依赖它，不要新增枚举值。
"""

from __future__ import annotations

# ── 10 值枚举 ─────────────────────────────────────────────────────────────

VALID_CHAPTER_TYPES = frozenset({
    "cover", "copyright", "toc", "preface", "body",
    "postscript", "appendix", "bibliography", "index", "notes",
})

# ── EPUB3 structural semantics (epub:type) → 枚举 ─────────────────────────
# 词表来源：EPUB 3 Structural Semantics Vocabulary。值可能带 doc- 前缀或
# 多 token（如 epub:type="frontmatter preface"），由 map_epub3_type 归一。
# frontmatter/backmatter 是容器类型，只在 spine 无更具体类型时作默认。

EPUB3_TYPE_TO_CHAPTER: dict[str, str] = {
    # 封面 / 版权 / 目录
    "cover": "cover",
    "halftitlepage": "cover",
    "titlepage": "cover",
    "copyright-page": "copyright",
    "colophon": "copyright",          # 语义近似"出版信息"，front 分组小瑕疵可接受
    "toc": "toc",
    "page-list": "toc",
    "loa": "toc",                     # list of audio
    "loi": "toc",                     # list of illustrations
    "lot": "toc",                     # list of tables
    # 前部
    "foreword": "preface",
    "preface": "preface",
    "introduction": "preface",
    "dedication": "preface",
    "epigraph": "preface",
    "acknowledgments": "preface",
    "frontmatter": "preface",         # 容器类型 → 默认 preface
    # 正文
    "text": "body",
    "chapter": "body",
    "volume": "body",
    "part": "body",
    "section": "body",
    # 后部
    "afterword": "postscript",
    "epilogue": "postscript",
    "backmatter": "postscript",       # 容器类型 → 默认 postscript
    "appendix": "appendix",
    "glossary": "appendix",
    "bibliography": "bibliography",
    "index": "index",
    "notes": "notes",
}

# ── EPUB2 <guide> reference type → 枚举 ────────────────────────────────────
# EPUB 2.0 guide 词表（小写无前缀）。

GUIDE_TYPE_TO_CHAPTER: dict[str, str] = {
    "cover": "cover",
    "title-page": "cover",
    "toc": "toc",
    "index": "index",
    "glossary": "appendix",
    "acknowledgements": "preface",
    "bibliography": "bibliography",
    "colophon": "copyright",
    "copyright-page": "copyright",
    "dedication": "preface",
    "epigraph": "preface",
    "foreword": "preface",
    "preface": "preface",
    "notes": "notes",
    "loi": "toc",
    "loa": "toc",
    "text": "body",
}

# ── EPUB3 landmarks <a epub:type> → 枚举 ──────────────────────────────────
# 与 EPUB3 内容词表同源，独立一份便于未来特判。

LANDMARK_TYPE_TO_CHAPTER: dict[str, str] = EPUB3_TYPE_TO_CHAPTER


def _norm_epub3(raw: str) -> str:
    """归一 epub:type 值：取首个 token、剥 doc- 前缀、小写。"""
    v = raw.strip().split()[0].lower()
    return v[4:] if v.startswith("doc-") else v


def map_epub3_type(t: str | None) -> str | None:
    """epub:type 值 → 枚举，未知返回 None。"""
    if not t:
        return None
    return EPUB3_TYPE_TO_CHAPTER.get(_norm_epub3(t))


def map_guide_type(t: str | None) -> str | None:
    """EPUB2 <guide> reference type → 枚举，未知返回 None。"""
    if not t:
        return None
    return GUIDE_TYPE_TO_CHAPTER.get(t.strip().lower())


def map_landmark_type(t: str | None) -> str | None:
    """EPUB3 landmarks 的 epub:type → 枚举，未知返回 None。"""
    return map_epub3_type(t)


def resolve_spine_semantic(
    epub_type: str | None,
    guide_type: str | None,
    landmark_type: str | None,
) -> tuple[str, str] | None:
    """合并三个信号，返回 (枚举, source)。

    优先级：内容 epub:type > EPUB2 guide > EPUB3 landmarks。
    理由：内容文档的 epub:type 由作者直接标注、粒度最细；
    landmarks 常含封面等噪点条目，guide 是策展过的列表。
    """
    for t, src, mapper in (
        (epub_type, "epub_type", map_epub3_type),
        (guide_type, "guide", map_guide_type),
        (landmark_type, "landmark", map_landmark_type),
    ):
        if t:
            m = mapper(t)
            if m:
                return m, src
    return None
