"""Book-domain database — books, chapters, chunks, reading sessions, FTS5.

Owns ``books.db``.  All methods operate exclusively on book-domain tables.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from storage.db_base import BaseDatabase, _now_ms, _new_id
from storage.models import (
    ANNOTATION_INDEXES,
    ANNOTATION_V14_ALTERS,
    ANNOTATION_V16_ALTERS,
    BOOK_DDL,
    CHUNK_PARAGRAPH_ALTERS,
    CHUNK_SIG_ALTERS,
    CHUNK_SUMMARY_ALTERS,
    CREATE_ANNOTATIONS,
    CREATE_BOOK_CHUNKS,
    CREATE_BOOK_FTS,
    CREATE_BOOK_FTS_TRIGGERS,
    CREATE_BOOK_TEXT_FORMS,
    CREATE_CHAPTER_OVERRIDES,
    CREATE_IMPORT_LOG,
    CREATE_READING_SESSIONS,
    MIGRATE_FTS_TO_CHUNKS,
    ALTER_CHAPTERS_HEADING_TAG,
    ALTER_CHAPTERS_IN_TOC,
    ALTER_CHAPTERS_LEVEL,
    ALTER_CHAPTERS_PARENT_INDEX,
    ALTER_CHAPTERS_SPINE_INDEX,
    ALTER_CHAPTERS_TYPE,
    TABLE_ANNOTATIONS,
    TABLE_BOOK_CATEGORIES,
    TABLE_BOOK_CHAPTERS,
    TABLE_BOOK_CHUNKS,
    TABLE_BOOK_FTS,
    TABLE_BOOK_VERSIONS,
    TABLE_BOOKS,
    TABLE_BOOK_TEXT_FORMS,
    TABLE_CHAPTER_OVERRIDES,
    TABLE_IMPORT_LOG,
    TABLE_READING_SESSIONS,
    AnnotationRow,
    BookCategoryRow,
    BookChapterRow,
    BookChunkRow,
    BookRow,
    ChapterOverrideRow,
    ImportLogRow,
    ReadingSessionRow,
    VersionRow,
)


# ── Migrations (book-domain only) ──────────────────────────────────────────

_BOOK_MIGRATIONS: list[tuple[int, str, str | list[str]]] = [
    (1, "Book library: books, chapters, categories, FTS5", list(BOOK_DDL)),
    (2, "FTS5: switch to trigram tokenizer for CJK support", [
        f"DROP TRIGGER IF EXISTS book_chapters_ai;",
        f"DROP TRIGGER IF EXISTS book_chapters_ad;",
        f"DROP TRIGGER IF EXISTS book_chapters_au;",
        f"DROP TABLE IF EXISTS {TABLE_BOOK_FTS};",
        CREATE_BOOK_FTS,
        *CREATE_BOOK_FTS_TRIGGERS,
        f"INSERT INTO {TABLE_BOOK_FTS}({TABLE_BOOK_FTS}) VALUES('rebuild');",
    ]),
    (3, "Book version control: snapshot + changelog", [
        f"CREATE TABLE IF NOT EXISTS {TABLE_BOOK_VERSIONS} ("
        "  id TEXT PRIMARY KEY,"
        "  book_id TEXT NOT NULL,"
        "  chapter_index INTEGER NOT NULL,"
        "  operation TEXT NOT NULL,"
        "  old_char_count INTEGER NOT NULL,"
        "  new_char_count INTEGER NOT NULL,"
        "  snapshot_path TEXT,"
        "  content_hash TEXT,"
        "  timestamp INTEGER NOT NULL"
        ");",
        f"CREATE INDEX IF NOT EXISTS idx_versions_book_chapter "
        f"ON {TABLE_BOOK_VERSIONS}(book_id, chapter_index, timestamp DESC);",
    ]),
    (4, "Chapter type classification: front_matter/body/back_matter", [
        ALTER_CHAPTERS_TYPE,
    ]),
    (5, "Book chunks: paragraph-group reading units within chapters", [
        CREATE_BOOK_CHUNKS,
        f"CREATE INDEX IF NOT EXISTS idx_chunks_book_chapter "
        f"ON {TABLE_BOOK_CHUNKS}(book_id, chapter_index, chunk_index);",
    ]),
    (6, "Reading sessions: track user reading progress per book", [
        CREATE_READING_SESSIONS,
        f"CREATE INDEX IF NOT EXISTS idx_reading_sessions_book "
        f"ON {TABLE_READING_SESSIONS}(book_id, started_at DESC);",
    ]),
    (7, "Rebuild FTS5 on book_chunks instead of book_chapters",
     list(MIGRATE_FTS_TO_CHUNKS)),
    (13, "Annotation system: import_log + annotations tables",
     [CREATE_IMPORT_LOG, CREATE_ANNOTATIONS, *ANNOTATION_INDEXES]),
    (14, "Annotation v3: persistent coordinates + context_window + chunking_version",
     list(ANNOTATION_V14_ALTERS)),
    (15, "Book metadata: author nationality for LLM enrichment", [
        "ALTER TABLE books ADD COLUMN author_nationality TEXT NOT NULL DEFAULT ''",
    ]),
    (16, "Annotation v4: 收藏/批注二分 — style + cfi_range, 废弃旧 bookmark", [
        *ANNOTATION_V16_ALTERS,
    ]),
    (17, "Chunk paragraph identity: paragraph_count + offsets for stable anchoring",
     list(CHUNK_PARAGRAPH_ALTERS)),
    (18, "Chunk paragraph style sigs: per-paragraph visual signature (parallel to offsets)",
     list(CHUNK_SIG_ALTERS)),
    (19, "Chapter-type overrides: human review layer over import/AI chapter_type",
     [CREATE_CHAPTER_OVERRIDES]),
    # 版本号必须递增取新值：判定是 ver > current_version，编号重复会被静默跳过。
    # 默认值必须是常量 —— 非恒定默认值（strftime 之类）会让旧库直接打不开。
    (20, "Shelf: custom order + last opened timestamp", [
        "ALTER TABLE books ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE books ADD COLUMN last_opened_at INTEGER NOT NULL DEFAULT 0",
    ]),
    (21, "Text forms: 正文内部文本形态（导入分析子代理产出）",
     [CREATE_BOOK_TEXT_FORMS]),
    (22, "Import structure: 章出自哪个 spine 文件 + 在书目录树里的层级", [
        ALTER_CHAPTERS_SPINE_INDEX,
        ALTER_CHAPTERS_LEVEL,
        ALTER_CHAPTERS_PARENT_INDEX,
    ]),
    (23, "Heading structure: 标题标签名 + 是否书目录的正式条目", [
        ALTER_CHAPTERS_HEADING_TAG,
        ALTER_CHAPTERS_IN_TOC,
    ]),
    (24, "Chunk summaries: bookgraph 产出的每块一句话", list(CHUNK_SUMMARY_ALTERS)),
]


class BookDB(BaseDatabase):
    """SQLite database for the book library.

    Owns ``books.db``.  Provides CRUD for imported books, chapters,
    reading chunks, full-text search, version history, and reading
    progress tracking.

    Usage::

        db = BookDB(Path("data/books.db"))
        db.initialize()
        db.insert_book(BookRow(id="b-1", title="..."))
    """

    _MIGRATIONS = _BOOK_MIGRATIONS

    def insert_book(self, row: BookRow) -> str:
        """Insert or replace a book (same source_path = re-import)."""
        now = _now_ms()
        conn = self.get_conn()
        # INSERT OR REPLACE 是「先删再插」：没写进语句的列会掉回 DDL 默认值。
        # sort_order / last_opened_at 是用户侧状态（书架自定义排位、打开时间），
        # 重导入时必须沿用旧值，否则书会莫名跳到自定义顺序末尾、打开时间归零。
        prev = conn.execute(
            f"SELECT sort_order, last_opened_at FROM {TABLE_BOOKS} WHERE id=?;",
            (row.id,),
        ).fetchone()
        prev_sort, prev_opened = (prev[0], prev[1]) if prev else (0, 0)
        conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE_BOOKS}
                (id, title, author, author_nationality, format, source_path,
                 category_id, metadata_json, chapter_count, total_chars,
                 status, time_created, time_updated, sort_order, last_opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
            (row.id, row.title, row.author, row.author_nationality, row.format,
             row.source_path, row.category_id, row.metadata_json,
             row.chapter_count, row.total_chars, row.status,
             row.time_created or now, now,
             row.sort_order or prev_sort, prev_opened),
        )
        conn.commit()
        return row.id

    def insert_chapters(self, chapters: list[BookChapterRow]) -> int:
        """Batch insert chapters for a book. Clears old chapters first."""
        if not chapters:
            return 0
        conn = self.get_conn()
        conn.execute("BEGIN;")
        try:
            book_id = chapters[0].book_id
            conn.execute(
                f"DELETE FROM {TABLE_BOOK_CHAPTERS} WHERE book_id=?;", (book_id,)
            )
            for ch in chapters:
                conn.execute(
                    f"""INSERT INTO {TABLE_BOOK_CHAPTERS}
                        (id, book_id, chapter_index, title, content, char_count,
                         status, chapter_type, spine_index, level, parent_index,
                         heading_tag, in_toc)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                    (ch.id, ch.book_id, ch.chapter_index, ch.title,
                     ch.content, ch.char_count, ch.status, ch.chapter_type,
                     ch.spine_index, ch.level, ch.parent_index,
                     ch.heading_tag, ch.in_toc),
                )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        return len(chapters)

    def get_book(self, book_id: str) -> BookRow | None:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_BOOKS} WHERE id=?;", (book_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return BookRow(**dict(row))

    def list_books(self, *, category_id: str | None = None,
                   status: str | None = None,
                   limit: int = 100) -> list[BookRow]:
        conditions: list[str] = []
        params: list[Any] = []
        if category_id is not None:
            conditions.append("category_id=?")
            params.append(category_id)
        if status is not None:
            conditions.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_BOOKS} {where} "
            "ORDER BY time_updated DESC LIMIT ?;",
            params + [limit],
        )
        return [BookRow(**dict(r)) for r in cur.fetchall()]

    def set_book_order(self, ids: list[str]) -> int:
        """Overwrite the shelf custom order from an explicit id list.

        Position is the array index + 1, so 0 stays reserved for "never
        ranked" (a book that has never taken part in a manual arrangement).
        Unknown ids are harmless — the UPDATE simply matches no row.
        """
        if not ids:
            return 0
        conn = self.get_conn()
        before = conn.total_changes
        conn.execute("BEGIN;")
        try:
            for pos, book_id in enumerate(ids, start=1):
                conn.execute(
                    f"UPDATE {TABLE_BOOKS} SET sort_order=? WHERE id=?;",
                    (pos, book_id),
                )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        # 实际改到的行数（不存在的 id 匹配不到行，不该算进去）
        return conn.total_changes - before

    def touch_book_opened(self, book_id: str) -> None:
        """Stamp 'last opened' — called when a book is opened for reading."""
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_BOOKS} SET last_opened_at=? WHERE id=?;",
            (_now_ms(), book_id),
        )
        conn.commit()

    def get_chapters(self, book_id: str) -> list[BookChapterRow]:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_BOOK_CHAPTERS} WHERE book_id=? "
            "ORDER BY chapter_index ASC;",
            (book_id,),
        )
        rows = [BookChapterRow(**dict(r)) for r in cur.fetchall()]
        # 覆盖层：人手便利贴优先于导入/AI 猜测的原表类型。
        over = self._get_chapter_overrides(book_id)
        if over:
            for ch in rows:
                if ch.chapter_index in over:
                    ch.chapter_type = over[ch.chapter_index]
        return rows

    def get_chapter_source_types(self, book_id: str) -> dict[int, str]:
        """原表（导入/AI 猜的）chapter_type 映射 {chapter_index: type}。

        与 get_chapters 的区别：get_chapters 返回时把便利贴盖进 chapter_type，
        原表值就丢了；标注面板需要"撕便利贴后回落成什么"，从这里取原表。
        """
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT chapter_index, chapter_type FROM {TABLE_BOOK_CHAPTERS} "
            "WHERE book_id=?;", (book_id,)
        )
        return {r[0]: r[1] for r in cur.fetchall()}

    def get_chapter(self, book_id: str, chapter_index: int) -> BookChapterRow | None:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_BOOK_CHAPTERS} "
            "WHERE book_id=? AND chapter_index=?;",
            (book_id, chapter_index),
        )
        row = cur.fetchone()
        if row is None:
            return None
        ch = BookChapterRow(**dict(row))
        over = self._get_chapter_overrides(book_id)
        if chapter_index in over:
            ch.chapter_type = over[chapter_index]
        return ch

    def get_chapters_by_spine(self, book_id: str,
                              spine_index: int) -> list[BookChapterRow]:
        """Chapters produced by one EPUB spine item.

        A spine item can yield multiple DB chapters (split inside one XHTML
        file); legacy imports leave spine_index=-1. Callers use this as a
        *scope*, then refine with the text fingerprint.
        """
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_BOOK_CHAPTERS} "
            "WHERE book_id=? AND spine_index=? ORDER BY chapter_index ASC;",
            (book_id, int(spine_index)),
        )
        return [BookChapterRow(**dict(r)) for r in cur.fetchall()]

    def _get_chapter_overrides(self, book_id: str) -> dict[int, str]:
        """Load all overrides for a book as {chapter_index: chapter_type}."""
        conn = self.get_conn()
        try:
            cur = conn.execute(
                f"SELECT chapter_index, chapter_type FROM {TABLE_CHAPTER_OVERRIDES} "
                "WHERE book_id=?;", (book_id,)
            )
            return {r[0]: r[1] for r in cur.fetchall()}
        except sqlite3.OperationalError:
            # 旧库尚未跑到 v19 迁移时优雅降级：并无覆盖层可查
            return {}

    def get_chapter_override(self, book_id: str, chapter_index: int
                             ) -> ChapterOverrideRow | None:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_CHAPTER_OVERRIDES} "
            "WHERE book_id=? AND chapter_index=?;",
            (book_id, chapter_index),
        )
        row = cur.fetchone()
        return ChapterOverrideRow(**dict(row)) if row else None

    def list_chapter_overrides(self, book_id: str
                               ) -> dict[int, ChapterOverrideRow]:
        """Load ALL overrides for a book as {chapter_index: full row}.

        一次性拿全书便利贴（含 note/source/时间戳），供标注弹窗等
        需要查"哪几章被盖过"的消费端用；比逐章 get_chapter_override 少 1358 次查询。
        """
        return self._get_chapter_overrides_full(book_id)

    def _get_chapter_overrides_full(self, book_id: str
                                    ) -> dict[int, ChapterOverrideRow]:
        conn = self.get_conn()
        try:
            cur = conn.execute(
                f"SELECT * FROM {TABLE_CHAPTER_OVERRIDES} "
                "WHERE book_id=?;", (book_id,)
            )
            return {r["chapter_index"]: ChapterOverrideRow(**dict(r))
                    for r in cur.fetchall()}
        except sqlite3.OperationalError:
            # 旧库尚未跑到 v19 迁移时优雅降级：并无覆盖层可查
            return {}

    def set_chapter_override(self, book_id: str, chapter_index: int,
                             chapter_type: str, *, note: str = "",
                             source: str = "manual") -> None:
        """Upsert 覆盖层便利贴 — 人工或 agent 的判断，写入/更新即时生效。

        ``source`` 区分是谁盖的（'manual' 人手 / 'agent' 导入分析子代理），
        :meth:`clear_chapter_override` 可按来源单撕一层。

        agent 不覆盖人工：``source='agent'`` 撞上已有 'manual' 时整条跳过
        （upsert 的 DO UPDATE 带 WHERE），人手的判断始终压得住机器。
        """
        now = _now_ms()
        conn = self.get_conn()
        conn.execute(
            f"INSERT INTO {TABLE_CHAPTER_OVERRIDES} "
            "(book_id, chapter_index, chapter_type, note, source, "
            " time_created, time_updated) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(book_id, chapter_index) DO UPDATE SET "
            " chapter_type=excluded.chapter_type, note=excluded.note, "
            " source=excluded.source, time_updated=excluded.time_updated "
            f" WHERE NOT (excluded.source = 'agent' "
            f" AND {TABLE_CHAPTER_OVERRIDES}.source = 'manual');",
            (book_id, chapter_index, chapter_type, note, source, now, now),
        )
        conn.commit()

    def clear_chapter_override(self, book_id: str, chapter_index: int,
                               source: str | None = None) -> None:
        """Remove an override（撕便利贴，回落到原表类型）。

        ``source`` 非 None 时只删该来源（如 'manual'/'import'），None 删全部。
        """
        conn = self.get_conn()
        if source is None:
            conn.execute(
                f"DELETE FROM {TABLE_CHAPTER_OVERRIDES} "
                "WHERE book_id=? AND chapter_index=?;",
                (book_id, chapter_index),
            )
        else:
            conn.execute(
                f"DELETE FROM {TABLE_CHAPTER_OVERRIDES} "
                "WHERE book_id=? AND chapter_index=? AND source=?;",
                (book_id, chapter_index, source),
            )
        conn.commit()

    def clear_agent_layer(self, book_id: str) -> tuple[int, int]:
        """清掉该书 agent 落的全部覆盖层与文本形态，返回 (章节数, 形态数)。

        落库是**全量替换**语义：agent 每次跑出的判断集合未必相同，增量写会
        留下上一次的残影 —— 旧逻辑推断出的 body 条目就这样残留过。
        """
        conn = self.get_conn()
        n1 = conn.execute(
            f"DELETE FROM {TABLE_CHAPTER_OVERRIDES} "
            "WHERE book_id=? AND source='agent';", (book_id,)).rowcount
        n2 = conn.execute(
            f"DELETE FROM {TABLE_BOOK_TEXT_FORMS} "
            "WHERE book_id=? AND source='agent';", (book_id,)).rowcount
        conn.commit()
        return n1, n2

    # ── Text forms（正文内部的文本形态）──────────────────────────────────

    def set_text_form(self, book_id: str, selector: str, name: str,
                      evidence: str = "", *, source: str = "agent") -> None:
        """Upsert 一条文本形态。来源语义与章节覆盖层一致：agent 压不过人手。"""
        now = _now_ms()
        conn = self.get_conn()
        conn.execute(
            f"INSERT INTO {TABLE_BOOK_TEXT_FORMS} "
            "(book_id, selector, name, evidence, source, "
            " time_created, time_updated) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(book_id, selector) DO UPDATE SET "
            " name=excluded.name, evidence=excluded.evidence, "
            " source=excluded.source, time_updated=excluded.time_updated "
            f" WHERE NOT (excluded.source = 'agent' "
            f" AND {TABLE_BOOK_TEXT_FORMS}.source = 'manual');",
            (book_id, selector, name, evidence, source, now, now),
        )
        conn.commit()

    def list_text_forms(self, book_id: str) -> list[dict]:
        """该书判出的全部文本形态（旧库尚未迁移时优雅降级为空）。"""
        conn = self.get_conn()
        try:
            cur = conn.execute(
                f"SELECT * FROM {TABLE_BOOK_TEXT_FORMS} WHERE book_id=? "
                "ORDER BY time_created ASC;", (book_id,))
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

    def update_chapter(self, book_id: str, chapter_index: int,
                       content: str | None = None, char_count: int | None = None,
                       status: str | None = None, title: str | None = None) -> None:
        """Update a chapter. Only provided (non-None) fields are modified."""
        conn = self.get_conn()
        set_parts: list[str] = []
        params: list[Any] = []
        if content is not None:
            set_parts.append("content=?")
            params.append(content)
        if char_count is not None:
            set_parts.append("char_count=?")
            params.append(char_count)
        if status is not None:
            set_parts.append("status=?")
            params.append(status)
        if title is not None:
            set_parts.append("title=?")
            params.append(title)
        if not set_parts:
            return
        params.extend([book_id, chapter_index])
        conn.execute(
            f"UPDATE {TABLE_BOOK_CHAPTERS} SET {', '.join(set_parts)} "
            "WHERE book_id=? AND chapter_index=?;",
            params,
        )
        conn.commit()

    def update_book_status(self, book_id: str, status: str) -> None:
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_BOOKS} SET status=?, time_updated=? WHERE id=?;",
            (status, _now_ms(), book_id),
        )
        conn.commit()

    # ── Chunk CRUD ────────────────────────────────────────────────────────

    def insert_chunks(self, chunks: list[BookChunkRow]) -> int:
        """Batch insert chunks for a book. Clears old chunks first."""
        if not chunks:
            return 0
        conn = self.get_conn()
        conn.execute("BEGIN;")
        try:
            book_id = chunks[0].book_id
            # 重导 / 重切会先删后插，而简述是**付费产物**（bookgraph 一次
            # 提取才有一份）。坐标与正文逐字相同 = 同一块，直接沿用，
            # 否则重导一次全书简述全丢。
            prev = {
                (r[0], r[1]): (r[2] or "", r[3] or "")
                for r in conn.execute(
                    f"SELECT chapter_index, chunk_index, content, summary "
                    f"FROM {TABLE_BOOK_CHUNKS} WHERE book_id=?;",
                    (book_id,),
                )
            }
            conn.execute(
                f"DELETE FROM {TABLE_BOOK_CHUNKS} WHERE book_id=?;", (book_id,)
            )
            for ch in chunks:
                summary = ch.summary or ""
                if not summary:
                    old = prev.get((ch.chapter_index, ch.chunk_index))
                    if old and old[0] == ch.content:
                        summary = old[1]
                conn.execute(
                    f"""INSERT INTO {TABLE_BOOK_CHUNKS}
                        (id, book_id, chapter_index, chunk_index, content,
                         char_count, paragraph_count, paragraph_offsets,
                         paragraph_sigs, summary)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                    (ch.id, ch.book_id, ch.chapter_index, ch.chunk_index,
                     ch.content, ch.char_count,
                     ch.paragraph_count, ch.paragraph_offsets,
                     ch.paragraph_sigs, summary),
                )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        return len(chunks)

    def get_chunks(self, book_id: str,
                   chapter_index: int | None = None) -> list[BookChunkRow]:
        """Get all chunks for a book, optionally filtered by chapter."""
        conn = self.get_conn()
        if chapter_index is not None:
            cur = conn.execute(
                f"SELECT * FROM {TABLE_BOOK_CHUNKS} "
                "WHERE book_id=? AND chapter_index=? "
                "ORDER BY chapter_index ASC, chunk_index ASC;",
                (book_id, chapter_index),
            )
        else:
            cur = conn.execute(
                f"SELECT * FROM {TABLE_BOOK_CHUNKS} "
                "WHERE book_id=? "
                "ORDER BY chapter_index ASC, chunk_index ASC;",
                (book_id,),
            )
        return [BookChunkRow(**dict(r)) for r in cur.fetchall()]

    def get_chunk(self, book_id: str, chapter_index: int,
                  chunk_index: int = 0) -> BookChunkRow | None:
        """Get a single chunk by book + chapter + chunk index."""
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_BOOK_CHUNKS} "
            "WHERE book_id=? AND chapter_index=? AND chunk_index=?;",
            (book_id, chapter_index, chunk_index),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return BookChunkRow(**dict(row))

    def get_chunk_count(self, book_id: str,
                        chapter_index: int | None = None) -> int:
        """Count chunks for a book or chapter."""
        conn = self.get_conn()
        if chapter_index is not None:
            cur = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_BOOK_CHUNKS} "
                "WHERE book_id=? AND chapter_index=?;",
                (book_id, chapter_index),
            )
        else:
            cur = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_BOOK_CHUNKS} "
                "WHERE book_id=?;",
                (book_id,),
            )
        return cur.fetchone()[0]

    # ── Chunk summaries（bookgraph 产出的「一块一句话」）────────────────

    def set_chunk_summaries(self, items: dict[str, str]) -> int:
        """写一批 ``{chunk_id: 一句话}``。空串视为「没有」，不覆盖已有值。

        Bookgraph 每批提取完调用一次。**按 id 更新、不按坐标** —— 坐标
        是导出量，id 才是块的身份（重切后坐标会整体平移）。
        """
        pairs = [(text.strip(), cid)
                 for cid, text in (items or {}).items()
                 if cid and isinstance(text, str) and text.strip()]
        if not pairs:
            return 0
        conn = self.get_conn()
        conn.execute("BEGIN;")
        try:
            cur = conn.executemany(
                f"UPDATE {TABLE_BOOK_CHUNKS} SET summary=? "
                "WHERE id=? AND summary='';",
                pairs,
            )
            # rowcount 是**这条语句**的行数；total_changes 是连接级累计值
            # （CLAUDE.md「merge_nodes 的搬运范围」踩过：实测报出 4971）。
            written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        return written

    def get_chunks_missing_summary(self, book_id: str,
                                   limit: int | None = None) -> list[BookChunkRow]:
        """取还没有简述的块（补录脚本用），按阅读顺序。"""
        conn = self.get_conn()
        sql = (f"SELECT * FROM {TABLE_BOOK_CHUNKS} "
               "WHERE book_id=? AND (summary IS NULL OR summary='') "
               "ORDER BY chapter_index ASC, chunk_index ASC")
        params: list[Any] = [book_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [BookChunkRow(**dict(r)) for r in conn.execute(sql, params).fetchall()]

    def count_chunks_with_summary(self, book_id: str) -> tuple[int, int]:
        """``(有简述的块数, 总块数)`` —— 覆盖率是用来看的，不是猜的。"""
        conn = self.get_conn()
        row = conn.execute(
            f"SELECT SUM(CASE WHEN summary<>'' THEN 1 ELSE 0 END), COUNT(*) "
            f"FROM {TABLE_BOOK_CHUNKS} WHERE book_id=?;",
            (book_id,),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def delete_chunks_for_book(self, book_id: str) -> None:
        """Delete every chunk of a book.

        Needed because :meth:`insert_chunks` returns early on an empty list
        and so never clears anything — a rebuild that yields zero chunks
        would otherwise leave the old ones behind.
        """
        conn = self.get_conn()
        conn.execute(
            f"DELETE FROM {TABLE_BOOK_CHUNKS} WHERE book_id=?;", (book_id,))
        conn.commit()

    def delete_chunks_for_chapter(self, book_id: str, chapter_index: int) -> None:
        """Delete only the chunks belonging to one chapter.

        Used by ``_write_chapter_content`` to rebuild a single chapter's
        chunks without touching the rest of the book.
        """
        conn = self.get_conn()
        conn.execute(
            f"DELETE FROM {TABLE_BOOK_CHUNKS} "
            "WHERE book_id=? AND chapter_index=?;",
            (book_id, chapter_index),
        )
        conn.commit()

    def insert_chunks_for_chapter(self, chunks: list[BookChunkRow]) -> None:
        """Insert chunks for one chapter without clearing others.

        Caller must ensure ``chunks`` all share the same (book_id,
        chapter_index). Use :meth:`insert_chunks` for a full book
        rewrite (it clears old chunks first).
        """
        if not chunks:
            return
        conn = self.get_conn()
        conn.execute("BEGIN;")
        try:
            for ch in chunks:
                conn.execute(
                    f"""INSERT INTO {TABLE_BOOK_CHUNKS}
                        (id, book_id, chapter_index, chunk_index, content,
                         char_count, paragraph_count, paragraph_offsets,
                         paragraph_sigs, summary)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                    (ch.id, ch.book_id, ch.chapter_index, ch.chunk_index,
                     ch.content, ch.char_count,
                     ch.paragraph_count, ch.paragraph_offsets,
                     ch.paragraph_sigs, ch.summary or ""),
                )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    # ── Reading Session CRUD ────────────────────────────────────────────

    def start_reading_session(self, book_id: str) -> ReadingSessionRow:
        """Create a new reading session for a book."""
        import uuid as _uuid
        row = ReadingSessionRow(
            id=_uuid.uuid4().hex[:16],
            book_id=book_id,
            started_at=_now_ms(),
        )
        conn = self.get_conn()
        conn.execute(
            f"""INSERT INTO {TABLE_READING_SESSIONS}
                (id, book_id, started_at, positions_json, total_chunks_read)
                VALUES (?, ?, ?, ?, ?);""",
            (row.id, row.book_id, row.started_at,
             row.positions_json, row.total_chunks_read),
        )
        conn.commit()
        return row

    def get_active_session(self, book_id: str) -> ReadingSessionRow | None:
        """Get the most recent un-ended reading session for a book."""
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_READING_SESSIONS} "
            "WHERE book_id=? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1;",
            (book_id,),
        )
        row = cur.fetchone()
        return ReadingSessionRow(**dict(row)) if row else None

    def record_position(self, session_id: str, chapter_index: int,
                        chunk_index: int, cfi: str = "") -> None:
        """Append a position record to the session.

        *cfi* is the epub.js CFI of the viewport top — the render layer's
        own semantic coordinate. It is the authoritative restore anchor
        (``display(cfi)`` is deterministic), so the reader no longer has
        to replay the fingerprint anchor chain when reopening a book.
        Legacy records written before this field simply omit the key.
        """
        import json as _json
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT positions_json, total_chunks_read "
            f"FROM {TABLE_READING_SESSIONS} WHERE id=?;",
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        positions = _json.loads(row[0])
        record = {
            "chapter": chapter_index,
            "chunk": chunk_index,
            "ts": _now_ms(),
        }
        if cfi:
            record["cfi"] = cfi
        positions.append(record)
        conn.execute(
            f"UPDATE {TABLE_READING_SESSIONS} "
            "SET positions_json=?, total_chunks_read=? WHERE id=?;",
            (_json.dumps(positions, ensure_ascii=False),
             row[1] + 1, session_id),
        )
        conn.commit()

    def get_last_position(self, session_id: str) -> tuple[int, int] | None:
        """Return the last recorded (chapter, chunk) for a session, or None.

        Used to dedup position records from the native reader, which
        reports on every scroll.
        """
        import json as _json
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT positions_json FROM {TABLE_READING_SESSIONS} WHERE id=?;",
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        positions = _json.loads(row[0])
        if not positions:
            return None
        last = positions[-1]
        return (last.get("chapter"), last.get("chunk"))

    def end_reading_session(self, session_id: str) -> None:
        """Mark a reading session as ended."""
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_READING_SESSIONS} SET ended_at=? WHERE id=?;",
            (_now_ms(), session_id),
        )
        conn.commit()

    def search_books(self, query: str, limit: int = 20, *,
                     book_id: str | None = None,
                     chapter_index: int | None = None) -> list[dict]:
        """FTS5 full-text search across book chapters.

        Falls back to LIKE search when FTS5 returns no results (e.g. short
        CJK queries that trigram tokenizer can't index as 3-grams).

        When *book_id* is given, the search is scoped to that single book so
        that high-frequency single-character queries are not crowded out by
        matches from other books under the global LIMIT.

        When *chapter_index* is given, results are further scoped to that
        single chapter (used by the chapter-search mode).
        """
        conn = self.get_conn()
        results: list[dict] = []

        # trigram tokenizer needs >=3 chars to form a 3-gram; shorter queries
        # (single/double CJK chars) can never MATCH, so skip FTS5 entirely
        # and go straight to the LIKE fallback.
        use_fts = len(query) >= 3

        # Primary: FTS5 trigram search on book_chunks
        if use_fts:
            try:
                fts_where = f"{TABLE_BOOK_FTS} MATCH ?"
                fts_params: list[Any] = [query]
                if book_id:
                    fts_where += " AND ck.book_id = ?"
                    fts_params.append(book_id)
                if chapter_index is not None:
                    fts_where += " AND ck.chapter_index = ?"
                    fts_params.append(chapter_index)
                cur = conn.execute(
                    f"SELECT b.id as book_id, b.title as book_title, "
                    "ch.chapter_index, ch.title as chapter_title, "
                    "ck.chunk_index, ck.summary, "
                    f"snippet({TABLE_BOOK_FTS}, 0, '…', '…', '…', 40) as snippet "
                    f"FROM {TABLE_BOOK_FTS} "
                    f"JOIN {TABLE_BOOK_CHUNKS} ck ON {TABLE_BOOK_FTS}.rowid = ck.rowid "
                    f"JOIN {TABLE_BOOKS} b ON ck.book_id = b.id "
                    f"LEFT JOIN {TABLE_BOOK_CHAPTERS} ch "
                    f"  ON ck.book_id = ch.book_id AND ck.chapter_index = ch.chapter_index "
                    f"WHERE {fts_where} ORDER BY rank LIMIT ?;",
                    (*fts_params, limit),
                )
                results = [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError:
                pass

        # Fallback: LIKE search on chunks for short queries
        if not results:
            like_pattern = f"%{query}%"
            like_where = "ck.content LIKE ? OR ch.title LIKE ?"
            like_params: list[Any] = [like_pattern, like_pattern]
            if book_id:
                like_where = f"({like_where}) AND ck.book_id = ?"
                like_params.append(book_id)
            if chapter_index is not None:
                like_where = f"({like_where}) AND ck.chapter_index = ?"
                like_params.append(chapter_index)
            cur = conn.execute(
                f"SELECT b.id as book_id, b.title as book_title, "
                "ch.chapter_index, ch.title as chapter_title, "
                "ck.chunk_index, ck.summary, ck.content "
                f"FROM {TABLE_BOOK_CHUNKS} ck "
                f"JOIN {TABLE_BOOKS} b ON ck.book_id = b.id "
                f"LEFT JOIN {TABLE_BOOK_CHAPTERS} ch "
                f"  ON ck.book_id = ch.book_id AND ck.chapter_index = ch.chapter_index "
                f"WHERE {like_where} "
                "ORDER BY b.title, ck.chapter_index, ck.chunk_index LIMIT ?;",
                (*like_params, limit),
            )
            for r in cur.fetchall():
                d = dict(r)
                content = d.pop("content", "")
                pos = content.find(query)
                start = max(0, pos - 40)
                end = min(len(content), pos + len(query) + 40)
                d["snippet"] = f"…{content[start:end]}…"
                results.append(d)

        return results

    def count_hits_by_chapter(self, query: str, book_id: str) -> list[dict]:
        """Return per-chapter hit counts for *query* within a single book.

        Uses LIKE search so it works for all query lengths including single
        CJK characters.  Returns a list of dicts sorted by chapter_index::

            [{chapter_index, chapter_title, hit_count}, ...]

        where ``hit_count`` is the number of chunks in that chapter whose
        content contains the query string.
        """
        conn = self.get_conn()
        like_pattern = f"%{query}%"
        cur = conn.execute(
            f"SELECT ch.chapter_index, ch.title as chapter_title, "
            "COUNT(*) as hit_count "
            f"FROM {TABLE_BOOK_CHUNKS} ck "
            f"LEFT JOIN {TABLE_BOOK_CHAPTERS} ch "
            f"  ON ck.book_id = ch.book_id AND ck.chapter_index = ch.chapter_index "
            "WHERE ck.book_id = ? AND ck.content LIKE ? "
            "GROUP BY ch.chapter_index, ch.title "
            "HAVING hit_count > 0 "
            "ORDER BY ch.chapter_index ASC;",
            (book_id, like_pattern),
        )
        return [dict(r) for r in cur.fetchall()]

    def delete_book(self, book_id: str) -> None:
        conn = self.get_conn()
        conn.execute("PRAGMA foreign_keys=OFF;")
        conn.execute(f"DELETE FROM {TABLE_BOOK_CHUNKS} WHERE book_id=?;", (book_id,))
        conn.execute(f"DELETE FROM {TABLE_BOOK_CHAPTERS} WHERE book_id=?;", (book_id,))
        conn.execute(f"DELETE FROM {TABLE_READING_SESSIONS} WHERE book_id=?;", (book_id,))
        conn.execute(f"DELETE FROM {TABLE_BOOKS} WHERE id=?;", (book_id,))
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.commit()

    def update_book(self, book_id: str, **fields: Any) -> None:
        """Update book metadata fields (title, author, category_id, etc.)."""
        if not fields:
            return
        fields.setdefault("time_updated", _now_ms())
        conn = self.get_conn()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [book_id]
        conn.execute(
            f"UPDATE {TABLE_BOOKS} SET {set_clause} WHERE id=?;",
            values,
        )
        conn.commit()

    def delete_chapter(self, book_id: str, chapter_index: int) -> None:
        """Delete a single chapter. FTS is auto-synced by trigger."""
        conn = self.get_conn()
        conn.execute(
            f"DELETE FROM {TABLE_BOOK_CHAPTERS} WHERE book_id=? AND chapter_index=?;",
            (book_id, chapter_index),
        )
        conn.commit()

    # ── Book categories ────────────────────────────────────────────────────

    def insert_category(self, row: BookCategoryRow) -> str:
        conn = self.get_conn()
        conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE_BOOK_CATEGORIES}
                (id, name, parent_id, sort_order, time_created)
                VALUES (?, ?, ?, ?, ?);""",
            (row.id, row.name, row.parent_id, row.sort_order,
             row.time_created or _now_ms()),
        )
        conn.commit()
        return row.id

    def list_categories(self) -> list[BookCategoryRow]:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_BOOK_CATEGORIES} ORDER BY sort_order ASC;"
        )
        return [BookCategoryRow(**dict(r)) for r in cur.fetchall()]

    def update_category(self, category_id: str, **fields: Any) -> None:
        """Update category fields (name, sort_order, parent_id)."""
        if not fields:
            return
        conn = self.get_conn()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [category_id]
        conn.execute(
            f"UPDATE {TABLE_BOOK_CATEGORIES} SET {set_clause} WHERE id=?;",
            values,
        )
        conn.commit()

    def delete_category(self, category_id: str) -> None:
        """Delete a category. Books under it get category_id=NULL."""
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_BOOKS} SET category_id=NULL, time_updated=? "
            "WHERE category_id=?;",
            (_now_ms(), category_id),
        )
        conn.execute(
            f"DELETE FROM {TABLE_BOOK_CATEGORIES} WHERE id=?;",
            (category_id,),
        )
        conn.commit()

    # ── Version control ─────────────────────────────────────────────────────

    def insert_version(self, row: VersionRow) -> str:
        conn = self.get_conn()
        conn.execute(
            f"""INSERT INTO {TABLE_BOOK_VERSIONS}
                (id, book_id, chapter_index, operation,
                 old_char_count, new_char_count,
                 snapshot_path, content_hash, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);""",
            (row.id, row.book_id, row.chapter_index, row.operation,
             row.old_char_count, row.new_char_count,
             row.snapshot_path, row.content_hash, row.timestamp),
        )
        conn.commit()
        return row.id

    def list_versions(self, book_id: str, chapter_index: int | None = None,
                      limit: int = 50) -> list[VersionRow]:
        conn = self.get_conn()
        if chapter_index is not None:
            cur = conn.execute(
                f"SELECT * FROM {TABLE_BOOK_VERSIONS} "
                "WHERE book_id=? AND chapter_index=? "
                "ORDER BY timestamp DESC LIMIT ?;",
                (book_id, chapter_index, limit),
            )
        else:
            cur = conn.execute(
                f"SELECT * FROM {TABLE_BOOK_VERSIONS} "
                "WHERE book_id=? "
                "ORDER BY timestamp DESC LIMIT ?;",
                (book_id, limit),
            )
        return [VersionRow(**dict(r)) for r in cur.fetchall()]

    def get_version(self, version_id: str) -> VersionRow | None:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_BOOK_VERSIONS} WHERE id=?;", (version_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return VersionRow(**dict(row))

    # ── JSONL → SQLite migration ───────────────────────────────────────────

    def insert_category(self, row: BookCategoryRow) -> str:
        conn = self.get_conn()
        conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE_BOOK_CATEGORIES}
                (id, name, parent_id, sort_order, time_created)
                VALUES (?, ?, ?, ?, ?);""",
            (row.id, row.name, row.parent_id, row.sort_order,
             row.time_created or _now_ms()),
        )
        conn.commit()
        return row.id

    def list_categories(self) -> list[BookCategoryRow]:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_BOOK_CATEGORIES} ORDER BY sort_order ASC;"
        )
        return [BookCategoryRow(**dict(r)) for r in cur.fetchall()]

    # ── Import Log (lifecycle event log) ─────────────────────────────────────

    def insert_import_log(self, row: ImportLogRow) -> str:
        conn = self.get_conn()
        conn.execute(
            f"INSERT INTO {TABLE_IMPORT_LOG} "
            "(id, stable_book_id, book_id, event_type, in_library, copyright_card, "
            "title, author, publisher, publish_year, isbn, first_chapter, "
            "first_paragraph, source_filename, import_count, time_created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row.id, row.stable_book_id, row.book_id, row.event_type,
             row.in_library, row.copyright_card, row.title, row.author,
             row.publisher, row.publish_year, row.isbn, row.first_chapter,
             row.first_paragraph, row.source_filename, row.import_count,
             row.time_created),
        )
        conn.commit()
        return row.id

    def get_active_import_logs(self) -> list[ImportLogRow]:
        """Return all currently in-library stable identities."""
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_IMPORT_LOG} "
            f"WHERE in_library = 1 "
            f"ORDER BY title, author;"
        )
        return [self._row_to_import_log(r) for r in cur.fetchall()]

    def get_import_log_by_stable(self, stable_book_id: str) -> list[ImportLogRow]:
        """Return all lifecycle events for a given stable identity."""
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_IMPORT_LOG} "
            f"WHERE stable_book_id = ? "
            f"ORDER BY time_created DESC;",
            (stable_book_id,),
        )
        return [self._row_to_import_log(r) for r in cur.fetchall()]

    def get_import_log_by_book(self, book_id: str) -> ImportLogRow | None:
        """Find the stable identity for a currently-in-library book."""
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_IMPORT_LOG} "
            f"WHERE book_id = ? AND in_library = 1 "
            f"ORDER BY time_created DESC LIMIT 1;",
            (book_id,),
        )
        row = cur.fetchone()
        return self._row_to_import_log(row) if row else None

    def get_latest_import_for_stable(self, stable_book_id: str) -> ImportLogRow | None:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_IMPORT_LOG} "
            f"WHERE stable_book_id = ? "
            f"ORDER BY time_created DESC LIMIT 1;",
            (stable_book_id,),
        )
        row = cur.fetchone()
        return self._row_to_import_log(row) if row else None

    @staticmethod
    def _row_to_import_log(row: sqlite3.Row) -> ImportLogRow:
        return ImportLogRow(
            id=row["id"],
            stable_book_id=row["stable_book_id"],
            book_id=row["book_id"],
            event_type=row["event_type"],
            in_library=row["in_library"],
            copyright_card=row["copyright_card"],
            title=row["title"],
            author=row["author"],
            publisher=row["publisher"],
            publish_year=row["publish_year"],
            isbn=row["isbn"],
            first_chapter=row["first_chapter"],
            first_paragraph=row["first_paragraph"],
            source_filename=row["source_filename"],
            import_count=row["import_count"],
            time_created=row["time_created"],
        )

    # ── Annotations ──────────────────────────────────────────────────────────

    def insert_annotation(self, row: AnnotationRow) -> str:
        conn = self.get_conn()
        conn.execute(
            f"INSERT INTO {TABLE_ANNOTATIONS} "
            "(id, stable_book_id, book_id, chapter_index, chunk_index, "
            "selected_text, context_before, context_after, type, note_text, "
            "note_format, color, match_status, "
            "paragraph_index, offset_in_paragraph, "
            "chapter_offset_start, chapter_offset_end, "
            "context_window, source_session_id, chunking_version, "
            "style, cfi_range, "
            "time_created, time_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row.id, row.stable_book_id, row.book_id, row.chapter_index,
             row.chunk_index, row.selected_text, row.context_before,
             row.context_after, row.type, row.note_text, row.note_format,
             row.color, row.match_status,
             row.paragraph_index, row.offset_in_paragraph,
             row.chapter_offset_start, row.chapter_offset_end,
             row.context_window, row.source_session_id, row.chunking_version,
             row.style, row.cfi_range,
             row.time_created, row.time_updated),
        )
        conn.commit()
        return row.id

    def get_annotations_by_stable_book(
        self, stable_book_id: str, *, limit: int = 200,
    ) -> list[AnnotationRow]:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_ANNOTATIONS} "
            f"WHERE stable_book_id = ? "
            f"ORDER BY chapter_index, chunk_index "
            f"LIMIT ?;",
            (stable_book_id, limit),
        )
        return [self._row_to_annotation(r) for r in cur.fetchall()]

    def get_annotations_by_chunk(
        self, book_id: str, chapter_index: int, chunk_index: int,
    ) -> list[AnnotationRow]:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_ANNOTATIONS} "
            f"WHERE book_id = ? AND chapter_index = ? AND chunk_index = ? "
            f"ORDER BY time_created;",
            (book_id, chapter_index, chunk_index),
        )
        return [self._row_to_annotation(r) for r in cur.fetchall()]

    def get_annotations_by_type(
        self, annotation_type: str = "note", *, limit: int = 50, offset: int = 0,
    ) -> list[AnnotationRow]:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_ANNOTATIONS} "
            f"WHERE type = ? OR type = 'both' "
            f"ORDER BY time_updated DESC "
            f"LIMIT ? OFFSET ?;",
            (annotation_type, limit, offset),
        )
        return [self._row_to_annotation(r) for r in cur.fetchall()]

    def search_annotations(
        self,
        query_terms: list[str],
        *,
        types: tuple[str, ...] = ("highlight", "note"),
        limit: int = 10,
        current_stable_book_id: str | None = None,
    ) -> list[AnnotationRow]:
        """Search annotations by text across ALL books (全库召回).

        Terms are matched against selected_text / note_text / context_window
        via LIKE union. Results ordered by time_updated DESC, with the current
        book's rows boosted to the front (当前书加权) — supports cross-book
        comparative reading (跨书对照).

        No FTS on annotations yet — LIKE is fine while the table is small;
        add trigram FTS5 (like book_chunks) when it grows.
        """
        terms = [t for t in (query_terms or []) if t and len(t) >= 2]
        if not terms:
            return []
        type_groups = ["(type = ? OR type = 'both')" for _ in types]
        like_groups = [
            "(selected_text LIKE ? OR note_text LIKE ? OR context_window LIKE ?)"
            for _ in terms
        ]
        params: list = [*types, *[f"%{t}%" for t in terms for _ in range(3)]]
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_ANNOTATIONS} "
            f"WHERE ({' OR '.join(type_groups)}) "
            f"  AND ({' OR '.join(like_groups)}) "
            f"ORDER BY CASE WHEN stable_book_id = ? THEN 0 ELSE 1 END, "
            f"  time_updated DESC "
            f"LIMIT ?;",
            [*params, current_stable_book_id, limit],
        )
        return [self._row_to_annotation(r) for r in cur.fetchall()]

    def get_orphaned_annotations(
        self, stable_book_id: str,
    ) -> list[AnnotationRow]:
        conn = self.get_conn()
        cur = conn.execute(
            f"SELECT * FROM {TABLE_ANNOTATIONS} "
            f"WHERE stable_book_id = ? AND match_status = 'orphaned' "
            f"ORDER BY chapter_index, chunk_index;",
            (stable_book_id,),
        )
        return [self._row_to_annotation(r) for r in cur.fetchall()]

    def update_annotation(self, annotation_id: str, **kwargs) -> None:
        if not kwargs:
            return
        set_parts = [f"{k} = ?" for k in kwargs]
        values = list(kwargs.values())
        values.append(annotation_id)
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_ANNOTATIONS} "
            f"SET {', '.join(set_parts)}, time_updated = {_now_ms()} "
            f"WHERE id = ?;",
            values,
        )
        conn.commit()

    def delete_annotation(self, annotation_id: str) -> bool:
        conn = self.get_conn()
        cur = conn.execute(
            f"DELETE FROM {TABLE_ANNOTATIONS} WHERE id = ?;",
            (annotation_id,),
        )
        conn.commit()
        return cur.rowcount > 0

    def relink_annotation(
        self, annotation_id: str,
        book_id: str, chapter_index: int, chunk_index: int,
    ) -> None:
        conn = self.get_conn()
        conn.execute(
            f"UPDATE {TABLE_ANNOTATIONS} "
            f"SET book_id = ?, chapter_index = ?, chunk_index = ?, "
            f"match_status = 'linked', time_updated = {_now_ms()} "
            f"WHERE id = ?;",
            (book_id, chapter_index, chunk_index, annotation_id),
        )
        conn.commit()

    def orphan_annotations_for_book(self, book_id: str) -> int:
        conn = self.get_conn()
        cur = conn.execute(
            f"UPDATE {TABLE_ANNOTATIONS} "
            f"SET book_id = NULL, chunk_index = NULL, "
            f"match_status = 'orphaned', time_updated = {_now_ms()} "
            f"WHERE book_id = ?;",
            (book_id,),
        )
        conn.commit()
        return cur.rowcount

    @staticmethod
    def _row_to_annotation(row: sqlite3.Row) -> AnnotationRow:
        def _get(key: str, default=None):
            """Safe column access — returns default if column doesn't exist yet."""
            try:
                return row[key]
            except IndexError:
                return default

        return AnnotationRow(
            id=row["id"],
            stable_book_id=row["stable_book_id"],
            book_id=_get("book_id"),
            chapter_index=_get("chapter_index"),
            chunk_index=_get("chunk_index"),
            selected_text=_get("selected_text", ""),
            context_before=_get("context_before"),
            context_after=_get("context_after"),
            # v14 persistent coordinates
            paragraph_index=_get("paragraph_index"),
            offset_in_paragraph=_get("offset_in_paragraph"),
            chapter_offset_start=_get("chapter_offset_start"),
            chapter_offset_end=_get("chapter_offset_end"),
            context_window=_get("context_window"),
            source_session_id=_get("source_session_id"),
            chunking_version=_get("chunking_version", 1),
            # v16 native annotation layer
            style=_get("style"),
            cfi_range=_get("cfi_range"),
            # core fields
            type=_get("type", "highlight"),
            note_text=_get("note_text"),
            note_format=_get("note_format", "markdown"),
            color=_get("color", "#FFD54F"),
            match_status=_get("match_status", "linked"),
            time_created=_get("time_created", 0),
            time_updated=_get("time_updated", 0),
        )

