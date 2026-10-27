"""Book library service — import, clean, search.

Sits between tools.py and storage/db.py. Orchestrates EPUB/MOBI parsing,
content cleaning pipelines, and full-text search.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from file_readers.epub_semantics import VALID_CHAPTER_TYPES
from storage.db import BookDB
from storage.db_base import _now_ms, _new_id
from storage.models import (
    AnnotationRow, BookCategoryRow, BookChapterRow, BookChunkRow, BookRow,
    ImportLogRow, VersionRow,
)


def _hash_file(filepath: Path) -> str:
    """Stable book ID from file content (MD5).

    Same file content = same ID regardless of filename or location.
    Different translations/editions = different IDs.
    """
    hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


def _bookbank_dir(db: BookDB) -> Path:
    """Resolve data/bookbank/ directory, creating it if needed."""
    data_dir = db._path.parent
    bookbank = data_dir / "bookbank"
    bookbank.mkdir(parents=True, exist_ok=True)
    return bookbank


def _now_ms() -> int:
    return int(time.time() * 1000)


def extract_and_save_cover(db: BookDB, book_id: str) -> str | None:
    """Extract cover from bookbank EPUB copy, save alongside it.

    Returns the cover filename (e.g. ``abc123_cover.jpeg``) or None.
    Only works for EPUB files; MOBI/other formats return None.
    """
    from file_readers.epub_reader import EpubReader

    book = db.get_book(book_id)
    if not book or book.format.lower() != "epub":
        return None

    bookbank = _bookbank_dir(db)
    epub_path = bookbank / f"{book_id}.epub"
    if not epub_path.exists():
        return None

    try:
        reader = EpubReader(epub_path)
        result = reader.extract_cover()
        if not result:
            return None
        image_bytes, mime_type = result
        ext = _mime_to_ext(mime_type)
        cover_filename = f"{book_id}_cover.{ext}"
        cover_path = bookbank / cover_filename
        cover_path.write_bytes(image_bytes)
        return cover_filename
    except Exception:
        return None


def _mime_to_ext(mime_type: str) -> str:
    """Map MIME type to file extension."""
    mime_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/svg+xml": "svg",
    }
    return mime_map.get(mime_type, "jpg")


# ── Import ────────────────────────────────────────────────────────────────

# Chapter type classification — TOC label → chapter_type.
# Anchored matching instead of substring matching: the bare "序" only hits
# an exact-title match, so "序章"/"序曲" (a novel's first chapter) no longer
# false-posit to preface.  Multi-char keywords use prefix anchoring to keep
# "前言：为什么要读这本书"-style subtitled headings working, plus suffix
# rules for titles carrying leading modifiers ("洪峰《瀚海》序", "第一版 跋").
_STRONG_PREFIX: tuple[tuple[str, str], ...] = (
    ("主要参考文献", "bibliography"),
    ("参考书目", "bibliography"),
    ("参考文献", "bibliography"),
    ("出版后记", "postscript"),
    ("再版后记", "postscript"),
    ("译后记", "postscript"),
    ("编后记", "postscript"),
    ("前言", "preface"),
    ("序言", "preface"),
    ("自序", "preface"),
    ("代序", "preface"),
    ("原序", "preface"),
    ("译者序", "preface"),
    ("译序", "preface"),
    ("总序", "preface"),
    ("再版序", "preface"),
    ("修订序", "preface"),
    ("推荐序", "preface"),
    ("新版序", "preface"),
    ("增订序", "preface"),
    ("编者序", "preface"),
    ("作者序", "preface"),
    ("出版者序", "preface"),
    ("中文版序", "preface"),
    ("英文版序言", "preface"),
    ("译者前言", "preface"),
    ("译本前言", "preface"),
    ("再版前言", "preface"),
    ("引言", "preface"),
    ("绪论", "preface"),
    ("导言", "preface"),
    ("导论", "preface"),
    ("后记", "postscript"),
    ("尾声", "postscript"),
    ("结语", "postscript"),
    ("附录", "appendix"),
    ("索引", "index"),
    ("注释", "notes"),
    ("总目录", "toc"),
    )

# 以"序"结尾但并非前言的常见词（后缀规则黑名单）
_NOT_PREFACE_SUFFIX: frozenset[str] = frozenset({
    "程序", "顺序", "秩序", "次序", "词序",
    "工序", "时序", "排序", "逆序", "序号",
})

_EXACT_TITLES: dict[str, str] = {
    "封面": "cover",
    "扉页": "cover",
    "书名页": "cover",
    "版权": "copyright",
    "版权页": "copyright",
    "版权信息": "copyright",
    "版权声明": "copyright",
    "目录": "toc",
    "目次": "toc",
    "序": "preface",      # 裸"序"精确命中；"序章"/"序曲"不命中（修复假阳性）
    "跋": "postscript",   # 精确表，避免前缀命中"跋涉"
    "注": "notes",
    "尾注": "notes",
}

# 英文开卷词 → preface（大小写不敏感；TOC 常直接写原版 "Dedication"/"Epigraph"）。
# 边界控制在"完全等于"或"后跟冒号/分隔"，避免 "Dedicated to…" 之类误配。
_OPENING_LABELS: frozenset[str] = frozenset({"dedication", "epigraph"})


def _match_label(t: str) -> tuple[str, bool]:
    """Match a normalized label, returning (chapter_type, is_exact).

    ``is_exact=True`` means the label came from the exact-title table and is
    exempt from positional checks.  Order: prefix anchor → suffix rules →
    exact table.
    """
    for kw, ct in _STRONG_PREFIX:
        if t.startswith(kw):
            return ct, False
    if len(t) >= 2 and t.endswith(("序言", "前言", "自序")):
        return "preface", False
    if t.endswith("后记"):
        return "postscript", False
    if len(t) >= 2 and t.endswith("序") and t not in _NOT_PREFACE_SUFFIX:
        return "preface", False
    if len(t) >= 2 and t.endswith("跋"):
        return "postscript", False
    if "代跋" in t:
        return "postscript", False
    # 无歧义后部名词的后缀规则（"《史铁生作品全编》索引" 等带前置修饰的标题）
    if t.endswith(("索引", "附录")):
        return "index" if t.endswith("索引") else "appendix", False
    if t.endswith("注释"):
        return "notes", False
    if t.endswith("参考书目"):
        return "bibliography", False
    # 英文开卷词（大小写不敏感）：dedication 献词 / epigraph 题词 → preface。
    # is_exact=True：语义自明，不受半本位置降级影响。
    t_low = t.lower()
    if any(t_low == kw or t_low.startswith(kw + ":") or t_low.startswith(kw + "：")
           for kw in _OPENING_LABELS):
        return "preface", True
    if t in _EXACT_TITLES:
        return _EXACT_TITLES[t], True
    return "body", False


def _in_first_half(index: int, total: int) -> bool:
    """True when *index* is in the book's first half (or position unknown)."""
    return total <= 0 or index / total < 0.5


def _classify_chapter(
    title: str | None,
    *,
    index: int = 0,
    total: int = 0,
    structural_type: str | None = None,
) -> str:
    """Classify a chapter by its TOC label.

    Returns one of: cover, copyright, toc, preface, body,
    postscript, appendix, bibliography, index, notes.

    Priority:
      1. *structural_type* (L1/L3 signal) — a valid non-body enum wins
         directly; ``"body"`` carries no information and falls through to
         the label rules below.
      2. Label keywords: prefix-anchored multi-char hints, suffix rules for
         modifier-prefixed titles, then exact-title matches for ambiguous
         single chars ("序", "跋", "注").
      3. Positional prior: a non-exact preface in the book's SECOND half is
         demoted to body; a non-exact postscript in the FIRST half likewise.
         Half-based (not a tight 5%/10% zone) so genuine front matter in
         short books (~10–20 chapters) is not punished.  Exact-title matches
         ("序", "跋") are unambiguous and stay put; cover/copyright/toc/
         appendix/… keep their label meaning anywhere.
    ``total <= 0`` (unknown chapter count) skips positional checks, matching
    the pre-change behaviour for any caller that omits index/total.
    """
    # "body" 是默认值，本身不含信息 —— 若让它短路，EPUB2 guide 的
    # type="text"（"正文起点"标记）会把扉页/版权页一起压成正文，白白丢掉
    # 标签里明确写着的 "扉页"/"版权页"。只有非 body 的结构信号才足以定论。
    if (structural_type and structural_type in VALID_CHAPTER_TYPES
            and structural_type != "body"):
        return structural_type
    if not title:
        return "body"
    # Strip ALL whitespace incl. full-width space U+3000 ("自　序" → "自序")
    t = re.sub(r"\s+", "", title.strip())
    if not t:
        return "body"
    ctype, is_exact = _match_label(t)
    # Positional checks only run when the chapter count is known (total > 0);
    # with the default 0/0 the pre-change behaviour (no demotion) is kept.
    if total > 0:
        if ctype == "preface" and not is_exact and not _in_first_half(index, total):
            return "body"
        if ctype == "postscript" and not is_exact and _in_first_half(index, total):
            return "body"
    return ctype


def _read_mobi_chapters(reader) -> list[tuple[str, list[str]]]:
    """Read a MOBI book using NCX filepos anchors for precise chapter splitting.

    Delegates to :meth:`MobiReader.extract_structured` which does a single
    extraction, parses NCX + OPF, and splits the HTML at filepos boundaries.
    """
    structured = reader.extract_structured()
    return structured.chapters


def _label_with_ncx(
    segments: list[tuple[str, str]],
    ncx_titles: list[str],
) -> list[tuple[str, str]]:
    """Replace regex-detected headings with NCX labels where possible.

    For each segment, if its heading can be matched to an NCX title
    (by substring or fuzzy match), use the NCX label instead.
    """
    result: list[tuple[str, str]] = []
    ncx_used: set[int] = set()

    for seg_heading, seg_text in segments:
        best_ncx_idx = -1
        best_score = 0

        for i, ncx_title in enumerate(ncx_titles):
            if i in ncx_used:
                continue
            # Score: how well does the NCX title match the segment heading?
            score = _title_match_score(seg_heading, ncx_title)
            if score > best_score:
                best_score = score
                best_ncx_idx = i

        if best_ncx_idx >= 0 and best_score >= 2:
            ncx_used.add(best_ncx_idx)
            result.append((ncx_titles[best_ncx_idx], seg_text))
        else:
            result.append((seg_heading, seg_text))

    return result


def _title_match_score(a: str, b: str) -> int:
    """Score how well two chapter titles match. Higher = better match.

    Returns: 3 = exact match, 2 = one contains the other,
    1 = partial overlap, 0 = no match.
    """
    a_clean = a.strip().lstrip("# ").strip()
    b_clean = b.strip()
    if a_clean == b_clean:
        return 3
    if a_clean in b_clean or b_clean in a_clean:
        return 2
    # Partial: check character overlap
    common = sum(1 for c in a_clean if c in b_clean)
    if common >= min(len(a_clean), len(b_clean)) // 2:
        return 1
    return 0


def _extract_mobi_metadata(path: str) -> dict[str, str] | None:
    """Deprecated — MOBI metadata is now extracted via OPF XML in
    :meth:`MobiReader.extract_structured`.  Kept as a no-op for
    backward compatibility; always returns None.
    """
    return None


def _extract_ncx_titles(reader) -> list[str] | None:
    """Deprecated — NCX titles are now extracted via XML parsing in
    :func:`file_readers.epub_reader._parse_ncx_anchors`.  Kept as a
    no-op for backward compatibility; always returns None.
    """
    return None


def import_book(db: BookDB, path: str, category_id: str | None = None) -> tuple[BookRow, bool]:
    """Parse an EPUB/MOBI file and store all chapters in the database.

    Returns the BookRow for the imported book. Chapters are stored with
    status='raw'. Cleaning is triggered separately via a post-import hook.

    Deduplication: book_id is the MD5 of the file content. Same file
    re-imported under a different name → skipped. Same title, different
    translation → different content hash → both imported.
    """
    from file_readers import open_book

    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Book not found: {path}")

    book_id = _hash_file(source)
    now = _now_ms()

    # Content-hash dedup — already imported?
    existing = db.get_book(book_id)
    if existing:
        # Still try to recover orphaned annotations (e.g. user deleted
        # the book, re-imported the same file → same content hash).
        import_log = db.get_import_log_by_book(book_id)
        if import_log:
            try:
                result = recover_orphaned_annotations(
                    db, import_log.stable_book_id, book_id,
                )
                if result["recovered"] > 0:
                    import logging
                    logging.getLogger("library").info(
                        "Re-import dedup: recovered %d orphaned annotations for %s",
                        result["recovered"], import_log.stable_book_id,
                    )
            except Exception:
                pass
        return existing, False

    # Copy to bookbank for self-contained storage
    bookbank = _bookbank_dir(db)
    local_copy = bookbank / f"{book_id}{source.suffix}"
    if not local_copy.exists():
        shutil.copy2(source, local_copy)

    reader = open_book(path)
    try:
        info_text = reader.get_info()
        metadata = _parse_info(info_text, source)

        # Enrich metadata from reader's OPF parse (publisher, date, isbn, etc.)
        if hasattr(reader, '_metadata'):
            for key in ('publisher', 'date', 'identifier', 'language'):
                val = reader._metadata.get(key, '').strip()
                if val and key not in metadata:
                    metadata[key] = val
            if metadata.get('identifier'):
                metadata['isbn'] = metadata['identifier']

        is_mobi = source.suffix.lower() == '.mobi'

        # MOBI: use extract_structured() for both metadata AND chapters
        if is_mobi:
            structured = reader.extract_structured()
            metadata["title"] = structured.title or metadata["title"]
            metadata["author"] = structured.author or metadata["author"]
            chapters_raw = structured.chapters
        else:
            # EPUB: iterate over ALL spine items (not just TOC entries).
            # TOC labels are used when available; non-TOC spine items get
            # filename-based labels. This correctly handles books where
            # the NCX is incomplete or doesn't cover every spine item.
            chapters_raw = []
            for spine_idx in range(len(reader._spine_ids)):
                try:
                    paras = reader.read_paragraphs_by_spine(spine_idx)
                except Exception:
                    paras = ["(failed to read chapter)"]
                # Skip pages that produce no readable paragraphs
                # (cover images, blank separators, etc.)
                if not paras:
                    continue
                all_text = "".join(paras).strip()
                if len(all_text) < 100:
                    continue
                label = reader.get_chapter_label(spine_idx)
                chapters_raw.append((label, paras))

        # Build chapter rows (navigation metadata) + chunk rows (reading units)
        total_chars = 0
        chapter_rows: list[BookChapterRow] = []
        chunk_rows: list[BookChunkRow] = []

        for ch_idx, (label, paras_or_text) in enumerate(chapters_raw):
            # Normalize to paragraph list
            if isinstance(paras_or_text, str):
                paragraphs = _extract_paragraphs(paras_or_text)
            else:
                paragraphs = paras_or_text

            # Full chapter text — lives in chapter.content AND is split
            # into chunks. Both stay in sync via _write_chapter_content
            # later (clean/replace/etc.). At import time we just build both.
            chapter_text = "\n\n".join(paragraphs)

            # Chunk paragraphs into ~800-char blocks
            chunk_texts = _chunk_paragraphs(paragraphs)

            ch_id = _new_id()
            ch_char_count = len(chapter_text)
            total_chars += ch_char_count

            chapter_rows.append(BookChapterRow(
                id=ch_id,
                book_id=book_id,
                chapter_index=ch_idx,
                title=label if label else f"Chapter {ch_idx + 1}",
                content=chapter_text,
                char_count=ch_char_count,
                status="raw",
                chapter_type=_classify_chapter(
                    label, index=ch_idx, total=len(chapters_raw),
                ),
            ))

            for ck_idx, ck_text in enumerate(chunk_texts):
                chunk_rows.append(BookChunkRow(
                    id=_new_id(),
                    book_id=book_id,
                    chapter_index=ch_idx,
                    chunk_index=ck_idx,
                    content=ck_text,
                    char_count=len(ck_text),
                ))

        book_row = BookRow(
            id=book_id,
            title=metadata["title"],
            author=metadata["author"],
            format=source.suffix.lstrip(".").lower(),
            source_path=str(local_copy.resolve()),
            category_id=category_id,
            metadata_json=None,
            chapter_count=len(chapter_rows),
            total_chars=total_chars,
            status="raw",
            time_created=now,
            time_updated=now,
        )

        db.insert_book(book_row)
        db.insert_chapters(chapter_rows)
        db.insert_chunks(chunk_rows)

        # ── Write import_log (lifecycle event) ──────────────────────────
        copyright_card = _build_copyright_card(
            metadata, chapters_raw, source.name,
        )
        stable_book_id, confidence, import_count = _match_stable_identity_sync(
            db, metadata["title"], metadata.get("author", ""), source.name,
        )
        now_ms = _now_ms()
        db.insert_import_log(ImportLogRow(
            id=_new_id(),
            stable_book_id=stable_book_id,
            book_id=book_id,
            event_type="import",
            in_library=1,
            copyright_card=copyright_card,
            title=metadata["title"],
            author=metadata.get("author", ""),
            publisher=metadata.get("publisher"),
            publish_year=metadata.get("publish_year"),
            isbn=metadata.get("isbn"),
            first_chapter=metadata.get("first_chapter_title"),
            first_paragraph=metadata.get("first_paragraph"),
            source_filename=source.name,
            import_count=import_count,
            time_created=now_ms,
        ))

        # ── Recover orphaned annotations from previous import ─────────
        if confidence != "no_match":
            try:
                result = recover_orphaned_annotations(
                    db, stable_book_id, book_id,
                )
                if result["recovered"] > 0:
                    import logging
                    logging.getLogger("library").info(
                        "Recovered %d orphaned annotations for %s",
                        result["recovered"], stable_book_id,
                    )
            except Exception:
                pass  # Recovery is best-effort; don't block import

        # Extract cover image (fire-and-forget; non-blocking for import)
        try:
            extract_and_save_cover(db, book_id)
        except Exception:
            pass

        return book_row, True

    finally:
        if hasattr(reader, 'close'):
            reader.close()


def _parse_info(info_text: str, source: Path) -> dict[str, str]:
    """Extract title and author from get_info() output."""
    lines = info_text.split("\n")
    title = source.stem
    author = "Unknown"
    for line in lines:
        if line.startswith("📖 ") or line.startswith("📖"):
            title = line.lstrip("📖 ").strip() or title
        elif line.startswith("✍️  ") or line.startswith("✍️ "):
            author = line.lstrip("✍️  ✍️ ").strip() or author
    return {"title": title, "author": author}


# ── Cleaning ───────────────────────────────────────────────────────────────

# Default cleaning rules — overridable via skill file
DEFAULT_CLEANING_RULES = {
    "remove_repeated_headers": True,
    "remove_page_numbers": True,
    "collapse_blank_lines": True,
    "strip_trailing_punctuation_clutter": True,
}


def clean_book(db: BookDB, book_id: str,
               rules: dict | None = None) -> dict:
    """Apply cleaning rules to all raw chapters in a book.

    Returns a summary dict: {book_id, cleaned_count, total_chars_before,
    total_chars_after, chapters[]}.
    """
    rules = rules or DEFAULT_CLEANING_RULES
    chapters = db.get_chapters(book_id)
    if not chapters:
        return {"error": "Book not found", "book_id": book_id}

    total_before = 0
    total_after = 0
    cleaned: list[dict] = []

    for ch in chapters:
        original = ch.content
        total_before += len(original)
        cleaned_text = _apply_rules(original, rules)
        char_count = len(cleaned_text)
        total_after += char_count

        db.update_chapter(book_id, ch.chapter_index, cleaned_text, char_count, "clean")
        cleaned.append({
            "chapter_index": ch.chapter_index,
            "title": ch.title,
            "chars_before": len(original),
            "chars_after": char_count,
            "removed": len(original) - char_count,
        })

    db.update_book_status(book_id, "clean")

    return {
        "book_id": book_id,
        "cleaned_count": len(cleaned),
        "total_chars_before": total_before,
        "total_chars_after": total_after,
        "chapters": cleaned,
    }


def _apply_rules(text: str, rules: dict) -> str:
    """Apply a set of cleaning rules to chapter text."""

    if rules.get("remove_repeated_headers"):
        text = _remove_repeated_lines(text)

    if rules.get("remove_page_numbers"):
        text = _remove_page_numbers(text)

    if rules.get("collapse_blank_lines"):
        text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)

    if rules.get("strip_trailing_punctuation_clutter"):
        text = _strip_clutter(text)

    return text.strip()


def _remove_repeated_lines(text: str) -> str:
    """Remove lines that repeat suspiciously often (headers/footers).

    A line appearing more than once every 3 unique lines is considered
    a header/footer artifact and removed entirely.
    """
    lines = text.split("\n")
    if len(lines) < 10:
        return text

    # Count non-empty line frequencies
    from collections import Counter
    non_empty = [l.strip() for l in lines if l.strip()]
    counts = Counter(non_empty)
    total_unique = len(counts)

    threshold = max(3, len(lines) / total_unique * 2) if total_unique > 0 else 999
    suspicious = {line for line, c in counts.items()
                  if c >= threshold and len(line) < 80}

    if not suspicious:
        return text

    return "\n".join(
        "" if l.strip() in suspicious else l
        for l in lines
    )


def _remove_page_numbers(text: str) -> str:
    """Remove standalone lines that are purely a page number."""
    return re.sub(r"^\s*\d{1,4}\s*$", "", text, flags=re.MULTILINE)


def _strip_clutter(text: str) -> str:
    """Remove common ebook artifacts."""
    # Multiple consecutive punctuation marks
    text = re.sub(r"[\.\*\-_]{3,}", "", text)
    # Zero-width characters
    text = text.replace("​", "").replace("‌", "").replace("‍", "")
    text = text.replace("﻿", "")
    return text


# ── Chapter splitting ──────────────────────────────────────────────────────

# Chapter heading patterns for auto-splitting single-chapter books (MOBI etc.)
_PRIMARY_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十百千\d]+章[^\n]*", re.MULTILINE),
    re.compile(r"^第[一二三四五六七八九十百千\d]+节[^\n]*", re.MULTILINE),
    re.compile(r"^第[一二三四五六七八九十百千\d]+回[^\n]*", re.MULTILINE),
    re.compile(r"^(?:Chapter|Part)\s+\d+[^\n]*", re.MULTILINE | re.IGNORECASE),
]

_SECONDARY_PATTERNS = [
    re.compile(r"^附录[一二三四五六七八九十\d]*[^\n]*", re.MULTILINE),
    re.compile(r"^(?:后记|尾声|跋|参考书目|参考文献|索引)[^\n]*", re.MULTILINE),
]

_TIGHT_GAP = 80  # chars: closer than this = part of same TOC cluster


def _detect_chapter_splits(text: str) -> list[tuple[str, str]]:
    """Pure function: detect chapter boundaries in text.

    Returns list of (title, content) pairs. Handles TOC filtering,
    front matter labeling, and secondary back-matter splitting.
    """
    # Find primary chapter heading candidates
    candidates: list[tuple[int, str]] = []
    seen_spans: set[tuple[int, int]] = set()
    for pat in _PRIMARY_PATTERNS:
        for m in pat.finditer(text):
            span = (m.start(), m.end())
            if span not in seen_spans:
                seen_spans.add(span)
                candidates.append((m.start(), m.group().strip()))

    if not candidates:
        return [("", text)]  # single chapter fallback

    candidates.sort(key=lambda x: x[0])

    # Filter TOC clusters by local density:
    # TOC entry = has >= 2 neighbours within _TIGHT_GAP chars
    def _nearby_count(idx: int) -> int:
        pos = candidates[idx][0]
        cnt = 0
        j = idx - 1
        while j >= 0 and pos - candidates[j][0] <= _TIGHT_GAP:
            cnt += 1
            j -= 1
        j = idx + 1
        while j < len(candidates) and candidates[j][0] - pos <= _TIGHT_GAP:
            cnt += 1
            j += 1
        return cnt

    real_headings: list[tuple[int, str]] = []
    for idx, (pos, heading) in enumerate(candidates):
        if _nearby_count(idx) < 2:
            real_headings.append((pos, heading))

    if not real_headings:
        return [("", text)]

    # Build chapter segments
    segments: list[tuple[str, str]] = []

    # Front matter
    first_pos = real_headings[0][0]
    if first_pos > 0:
        front_text = text[:first_pos].strip()
        if len(front_text) > 100:
            segments.append(("前言", front_text))

    # Body chapters
    for idx, (pos, heading) in enumerate(real_headings):
        start = pos
        end = real_headings[idx + 1][0] if idx + 1 < len(real_headings) else len(text)
        body = text[start:end].strip()
        segments.append((heading, body))

    # Secondary split on the last chapter for 附录 / 后记 etc.
    if len(segments) > 1:
        last_title, last_content = segments[-1]
        sub_candidates: list[tuple[int, str]] = []
        sub_seen: set[tuple[int, int]] = set()
        for pat in _SECONDARY_PATTERNS:
            for m in pat.finditer(last_content):
                span = (m.start(), m.end())
                if span not in sub_seen:
                    sub_seen.add(span)
                    sub_candidates.append((m.start(), m.group().strip()))

        if sub_candidates:
            sub_candidates.sort(key=lambda x: x[0])
            segments.pop()
            prev_end = 0
            for i, (spos, sheading) in enumerate(sub_candidates):
                if spos > prev_end:
                    seg = last_content[prev_end:spos].strip()
                    if seg:
                        label = "后记" if i == 0 else last_title
                        segments.append((label, seg))
                seg_end = sub_candidates[i + 1][0] if i + 1 < len(sub_candidates) else len(last_content)
                seg = last_content[spos:seg_end].strip()
                if seg:
                    segments.append((sheading, seg))
                prev_end = seg_end
            if prev_end < len(last_content):
                remaining = last_content[prev_end:].strip()
                if remaining:
                    segments.append(("后记", remaining))

    return segments


def split_book_chapters(db: BookDB, book_id: str,
                        force: bool = False,
                        dry_run: bool = False) -> dict:
    """Detect chapter boundaries and split a book into multiple chapters.

    Only operates on single-chapter books unless force=True.
    Set dry_run=True to preview without modifying the database.
    """
    book = db.get_book(book_id)
    if book is None:
        return {"error": f"Book not found: {book_id}"}

    if book.chapter_count != 1 and not force:
        return {
            "skipped": True,
            "reason": f"Book has {book.chapter_count} chapters already. "
                      f"Use force=True to re-split.",
        }

    chapters = db.get_chapters(book_id)
    if not chapters:
        return {"error": "Book has no chapters"}

    text = chapters[0].content
    if not text.strip():
        return {"error": "Chapter content is empty"}

    segments = _detect_chapter_splits(text)
    if len(segments) == 1 and not segments[0][0]:
        return {"error": "No chapter headings detected. Try manual splitting."}

    new_chapters: list[BookChapterRow] = []
    for idx, (title, content) in enumerate(segments):
        new_chapters.append(BookChapterRow(
            id=_new_id(), book_id=book_id, chapter_index=idx,
            title=title, content=content,
            char_count=len(content), status="raw",
        ))

    if dry_run:
        return {
            "dry_run": True,
            "book_id": book_id,
            "title": book.title,
            "original_chars": len(text),
            "new_chapter_count": len(new_chapters),
            "chapters": [
                {"index": ch.chapter_index, "title": ch.title,
                 "chars": ch.char_count}
                for ch in new_chapters
            ],
        }

    db.insert_chapters(new_chapters)
    db.update_book_status(book_id, "raw")

    return {
        "book_id": book_id,
        "title": book.title,
        "original_chars": len(text),
        "new_chapter_count": len(new_chapters),
        "chapters": [
            {"index": ch.chapter_index, "title": ch.title,
             "chars": ch.char_count}
            for ch in new_chapters
        ],
    }


# ── Paragraph chunking ────────────────────────────────────────────────────

# Natural break markers — paragraph-level delimiters that indicate a
# logical pause in the text. We prefer to break chunks at these points.
_NATURAL_BREAK_RE = re.compile(
    r"^(?:\*{3,}|-{3,}|~{3,})$"          # scene separators: ***, ---, ~~~
    r"|^\d{4}年\d{1,2}月"                 # date markers: 2021年3月
    r"|^#\s+\S.*"                          # markdown headings
)
_NATURAL_BREAK_MAX_LEN = 80


def _is_natural_break(para: str) -> bool:
    """Return True if this paragraph is a natural break point.

    Natural breaks include scene separators (``***``), date markers
    (``2021年3月``), and short section headings. Chunks prefer to end
    at these points rather than mid-flow.
    """
    stripped = para.strip()
    if not stripped:
        return True
    if len(stripped) > _NATURAL_BREAK_MAX_LEN:
        return False
    return bool(_NATURAL_BREAK_RE.match(stripped))


def _extract_paragraphs(text: str) -> list[str]:
    """Split plain text into paragraphs on blank lines.

    Fallback for text that has already been cleaned of HTML tags.
    """
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _chunk_paragraph_groups(
    paragraphs: list[str],
    target: int = 800,
    min_chunk: int = 400,
    max_chunk: int = 1100,
) -> list[list[str]]:
    """Group paragraphs into fixed-size chunks without breaking paragraphs.

    Uses a greedy adaptive algorithm:
    - Accumulates paragraphs until the chunk reaches ~target chars.
    - Breaks at natural break-points when the chunk is full enough.
    - Never splits a paragraph — paragraph atomicity is guaranteed.
    - The last chunk may be smaller than *min_chunk* (natural remainder).

    Returns per-chunk *paragraph lists* (not joined text) so callers can
    keep paragraph boundaries for identity/anchoring.

    Args:
        paragraphs: List of paragraph strings.
        target: Target character count per chunk (default 800).
        min_chunk: Minimum chars before a chunk is "full enough" to break.
        max_chunk: Maximum chars before a forced break.

    Returns:
        List of chunks, each a list of paragraph strings.
    """
    if not paragraphs:
        return []

    groups: list[list[str]] = []
    buf: list[str] = []
    buf_size = 0

    for p in paragraphs:
        p_size = len(p)
        is_break = _is_natural_break(p)

        # Rule 1: current chunk is full enough + next paragraph won't fit → break
        if buf_size + p_size > max_chunk and buf_size >= min_chunk:
            groups.append(buf)
            buf = []
            buf_size = 0

        # Rule 2: natural break point + we have enough content → break,
        #         then start a new chunk with the break marker itself
        elif is_break and buf_size >= min_chunk:
            groups.append(buf)
            buf = [p]
            buf_size = p_size
            continue

        buf.append(p)
        buf_size += p_size

        # Rule 3: single paragraph exceeds max — keep it whole
        if buf_size >= max_chunk and len(buf) == 1:
            groups.append(buf)
            buf = []
            buf_size = 0

    # Flush remainder (b ≤ a)
    if buf:
        groups.append(buf)

    # Post-process: merge undersized tail chunk into previous
    if len(groups) >= 2:
        tail_chars = sum(len(p) for p in groups[-1])
        if tail_chars < min_chunk // 2:
            groups[-2].extend(groups[-1])
            groups.pop()

    return groups


def _chunk_paragraphs(
    paragraphs: list[str],
    target: int = 800,
    min_chunk: int = 400,
    max_chunk: int = 1100,
) -> list[str]:
    """Group paragraphs into chunks and return the joined chunk texts.

    Thin wrapper over :func:`_chunk_paragraph_groups` for callers that only
    need chunk text and not paragraph boundaries.
    """
    return [
        "\n\n".join(g)
        for g in _chunk_paragraph_groups(
            paragraphs, target=target, min_chunk=min_chunk, max_chunk=max_chunk,
        )
    ]


def _paragraph_offsets(
    chunk_text: str, paragraphs: list[str],
) -> list[list[int]]:
    """Locate each paragraph's ``[start, len]`` within ``chunk_text``.

    ``chunk_text`` is ``"\\n\\n".join(paragraphs)``; offsets are found by
    scanning forward from the previous paragraph's end. Guarantees
    ``chunk_text[start:start + len] == paragraph`` for every entry.
    """
    offsets: list[list[int]] = []
    pos = 0
    for p in paragraphs:
        start = chunk_text.find(p, pos)
        offsets.append([start, len(p)])
        pos = start + len(p)
    return offsets


# ── Chapter content / chunk sync ───────────────────────────────────────────


def _build_chunks_for_chapter(
    book_id: str, chapter_index: int, content: str,
) -> list[BookChunkRow]:
    """Split chapter text into chunk rows with paragraph identity.

    The single source of truth for turning a chapter's full text into
    ``BookChunkRow`` objects. Used by import, clean, replace, rollback,
    and split so every path produces identical chunking.

    Since v17, each chunk records its paragraph structure
    (``paragraph_count`` + ``paragraph_offsets``) as a stable snapshot so
    consumers (anchoring, retrieval) reference paragraphs without
    re-deriving boundaries.
    """
    paragraphs = _extract_paragraphs(content)
    groups = _chunk_paragraph_groups(paragraphs)
    rows: list[BookChunkRow] = []
    for ck_idx, chunk_paras in enumerate(groups):
        ck_text = "\n\n".join(chunk_paras)
        rows.append(
            BookChunkRow(
                id=_new_id(),
                book_id=book_id,
                chapter_index=chapter_index,
                chunk_index=ck_idx,
                content=ck_text,
                char_count=len(ck_text),
                paragraph_count=len(chunk_paras),
                paragraph_offsets=json.dumps(
                    _paragraph_offsets(ck_text, chunk_paras),
                    ensure_ascii=False,
                ),
            )
        )
    return rows


def _write_chapter_content(
    db: BookDB, book_id: str, chapter_index: int,
    content: str, status: str | None = None,
) -> None:
    """Write chapter.content AND rebuild its chunks in one transaction.

    Guarantees chapter.content and book_chunks stay in sync. All content
    mutations (clean / replace / rollback / edit) must go through here.

    Args:
        status: If given, also updates the chapter status.
    """
    chunk_rows = _build_chunks_for_chapter(book_id, chapter_index, content)

    # chapter row update
    update_kwargs: dict = {"content": content, "char_count": len(content)}
    if status is not None:
        update_kwargs["status"] = status
    db.update_chapter(book_id, chapter_index, **update_kwargs)

    # chunks: delete then re-insert for this chapter only
    db.delete_chunks_for_chapter(book_id, chapter_index)
    db.insert_chunks_for_chapter(chunk_rows)


# ── Chunk recompute（只重算块，不碰章）─────────────────────────────────────

_WS_ANY_RE = re.compile(r"\s+")


def reuse_chunk_ids(new_rows: list[BookChunkRow],
                    old_chunks: list[BookChunkRow]) -> dict:
    """重导时给**内容没变**的块复用旧主键。纯函数，就地改 ``new_rows`` 的 id。

    为什么必须做：``graph.db`` 把 ``graph_chunks.chunk_id`` 当锚点，边、引文、
    观察、节点首现全挂在它上面。重导换一批新 id，这些锚点就集体悬空 ——
    实测 9 本里 8 本交集为 0（人类简史 122/312、大棋局 55/144 事后也救不回来，
    因为章都重切了）。

    判据只有一条：**同坐标 + 逐字节相同**。重导的两边都是同一个文件解析出来的，
    内容没变就是同一个块。

    **不做** ``_plan_chunk_rebuild`` 的「空白等价」那一档：那一档是为了不把用户
    改过的正文按新解析结果覆盖；这里两边同源，多一档只会把**真的变了**的块也
    钉在旧主键上，反而把新内容塞进旧身份。
    """
    old_by_key = {(c.chapter_index, c.chunk_index): c for c in old_chunks}
    reused = fresh = 0
    new_keys: set[tuple[int, int]] = set()
    for row in new_rows:
        key = (row.chapter_index, row.chunk_index)
        new_keys.add(key)
        prev = old_by_key.get(key)
        if prev is not None and prev.content == row.content:
            row.id = prev.id
            reused += 1
        else:
            fresh += 1
    disappeared = sorted(k for k in old_by_key if k not in new_keys)
    return {"reused": reused, "fresh": fresh,
            "old": len(old_chunks), "disappeared": disappeared}


def _plan_chunk_rebuild(
    book_id: str,
    chapters: list[BookChapterRow],
    old_chunks: list[BookChunkRow],
) -> tuple[list[BookChunkRow], dict]:
    """纯函数：按章正文重算块，主键能复用就复用。不写库。

    分块走 :func:`_build_chunks_for_chapter` —— 与导入完全同一个算法，所以
    对未经改动的书，重算结果是恒等的（全库实测 4447 块逐字节相同）。

    主键策略分三档，中间那档是必需的：

    * 同坐标 + **逐字节相同** → 复用整行（含 ``id`` 与 ``paragraph_sigs``）
    * 同坐标 + **规范化等价**（去掉全部空白后相同）→ **整行原样保留**
    * 其余 → 新行、新主键

    第二档挡的是既有正文污染：``_extract_paragraphs`` 会对每段 ``strip()``，
    而 ``大棋局`` 的正文里带 ``\\r`` 残字、``地下室手记`` 的居中段带
    ``_format_html`` 给的两个空格缩进前缀 —— 直接按新结果覆盖会**静默改写
    这 7 章正文**，并让 4 个块换主键（图谱变孤儿）。等价即不动。

    换主键是有代价的：图谱库把 ``graph_chunks.chunk_id`` 当主键，边、引文、
    角色首次出场都挂在它上面。所以 ``new_pk`` 与 ``disappeared`` 必须报出来。
    """
    old_by_key = {(c.chapter_index, c.chunk_index): c for c in old_chunks}
    new_rows: list[BookChunkRow] = []
    new_keys: set[tuple[int, int]] = set()
    identical_pk = equivalent_kept = new_pk = 0

    for ch in chapters:
        for row in _build_chunks_for_chapter(book_id, ch.chapter_index, ch.content):
            key = (row.chapter_index, row.chunk_index)
            new_keys.add(key)
            prev = old_by_key.get(key)
            if prev is None:
                new_pk += 1
            elif prev.content == row.content:
                row.id = prev.id
                row.paragraph_sigs = prev.paragraph_sigs
                identical_pk += 1
            elif _WS_ANY_RE.sub("", prev.content) == _WS_ANY_RE.sub("", row.content):
                # 只差空白 —— 保留旧行原样，一个字节都不改
                new_rows.append(prev)
                equivalent_kept += 1
                continue
            else:
                new_pk += 1
            new_rows.append(row)

    disappeared = sorted(k for k in old_by_key if k not in new_keys)
    stats = {
        "book_id": book_id,
        "chapters": len(chapters),
        "old_chunks": len(old_chunks),
        "new_chunks": len(new_rows),
        "identical_pk": identical_pk,
        "equivalent_kept": equivalent_kept,
        "new_pk": new_pk,
        "disappeared": disappeared,
    }
    return new_rows, stats


def recompute_book_chunks(db: BookDB, book_id: str, *, dry_run: bool = True) -> dict:
    """只重算某本书的 ``book_chunks``，**完全不碰 ``book_chapters``**。

    章号不变，所以批注（坐标在章级）、阅读进度、图谱的章级引用都不受影响；
    块级只有 ``new_pk`` / ``disappeared`` 那部分会漂。

    这是独立通道，**没有**接进 clean / edit / replace / rollback / split /
    remove 这六条会改章正文的路径（那六条的脱节见
    ``docs/import-subagent/README.md`` 的「已知漂移」一节）。
    """
    chapters = db.get_chapters(book_id)
    old_chunks = db.get_chunks(book_id)
    new_rows, stats = _plan_chunk_rebuild(book_id, chapters, old_chunks)
    stats["dry_run"] = dry_run
    if dry_run:
        return stats
    if new_rows:
        db.insert_chunks(new_rows)          # 先删全书再插 + 事务 + FTS 触发器同步
    elif old_chunks:
        db.delete_chunks_for_book(book_id)  # insert_chunks([]) 会空转，不删旧块
    return stats


# ── Content Fingerprint Matching ────────────────────────────────────────


def _fingerprint_variants(page_text: str) -> list[str]:
    """Build whitespace-normalized fingerprint variants for chunk matching.

    The viewport-top extractor can prepend things the stored chunk never
    carries (a bare page number, ``第一章　乐园``, or a bare ``二十四章``
    heading). Each variant removes one of those prefixes; all are tried
    against the DB content. Variants are only added when enough text
    remains, so stripping can't degenerate into a meaningless substring.
    """
    fingerprint = _norm_ws(page_text)[:80]
    variants = [fingerprint]

    # Leading page number / punctuation ("1 我是一个…", "3、要知道…").
    fp_stripped = re.sub(r"^[\d\s.、,，:：\-—–]+", "", fingerprint)
    if len(fp_stripped) >= 20 and fp_stripped != fingerprint:
        variants.append(fp_stripped)

    # "第<num>[章节卷篇回] <title>" — heading + short title.
    heading_re = re.compile(
        r"^第[0-9一二三四五六七八九十百千零两]+[章节卷篇回]\s*[^\s]{0,12}\s*"
    )
    # "二十四章 " — number heading without 第 (e.g. 道德经 chapter titles
    # live in the EPUB heading but not in the stored chunk body). Strip only
    # the marker — never the following 12 chars, which may be body text.
    number_heading_re = re.compile(
        r"^(?:第)?[0-9一二三四五六七八九十百千零两]+[章节卷篇回]\s*"
    )
    for base in (fingerprint, fp_stripped):
        for rx in (heading_re, number_heading_re):
            stripped = rx.sub("", base)
            if stripped != base and len(stripped) >= 20 \
                    and stripped not in variants:
                variants.append(stripped)
    return variants


def match_page_text(
    db: BookDB,
    book_id: str,
    chapter_title: str = "",
    page_text: str = "",
    hint: tuple[int, int] | None = None,
) -> dict | None:
    """Match visible page text to a specific chunk in the database.

    Uses a multi-layer match:
    1. chapter_title / hint → chapter_index (narrow search scope)
    2. page_text fingerprint (first 80 chars) → chunk_index
    3. Fallback: scan remaining chapters closest-first from *hint*
    4. Last resort: 40-char short fingerprint

    *hint* is the reader's last known (chapter, chunk). The native reader
    scrolls continuously and reports on every tick, so consecutive reports
    are always near each other — searching outward from the hint picks the
    RIGHT occurrence when short text repeats across the book (a bare
    ``indexOf`` over the whole book returns the earliest occurrence, which
    teleported the reader backwards across chapters: observed ch5 → ch3 →
    ch1 jumps in reader_sync.log).

    Returns None if no match found.
    """
    if not page_text.strip():
        return None

    # Whitespace-normalize both sides before comparing: DB chunk content
    # joins paragraphs with "\\n\\n" while browser-rendered text uses
    # single newlines/spaces — raw substring matching would miss every
    # fingerprint spanning a paragraph boundary (native reader).
    # Prefix variants (page number / chapter heading) are shared with
    # ``match_in_chapters`` — see :func:`_fingerprint_variants`.
    fp_variants = _fingerprint_variants(page_text)

    # Layer 1: find chapter by title substring match; hint overrides it.
    candidates = db.get_chapters(book_id)
    if not candidates:
        return None

    matched_chapter = 0
    if chapter_title:
        for ch in candidates:
            if ch.title and (chapter_title in ch.title or ch.title in chapter_title):
                matched_chapter = ch.chapter_index
                break
    if hint and hint[0] is not None:
        matched_chapter = hint[0]

    # Scan order for the fallback layers: start at the hint chapter and
    # fan outward, so a fingerprint that repeats across the book resolves
    # to the occurrence nearest the reader's current position instead of
    # the earliest one.
    def _chapter_scan_order() -> list:
        idxs = [ch.chapter_index for ch in candidates]
        if hint and hint[0] is not None:
            idxs.sort(key=lambda i: abs(i - hint[0]))
        return idxs

    def _to_chunks(ch_idx: int) -> list:
        chs = db.get_chunks(book_id, ch_idx)
        if hint and hint[0] == ch_idx and hint[1] is not None:
            # Same chapter → prefer the chunk nearest the hint.
            chs = sorted(chs, key=lambda c: abs(c.chunk_index - hint[1]))
        return chs

    # Layer 2: scan chunks in the matched/hint chapter for fingerprint
    chunks = db.get_chunks(book_id, matched_chapter)
    hit = _find_chunk_by_fp(chunks, fp_variants)
    if hit:
        return {
            "book_id": book_id,
            "chapter_index": hit.chapter_index,
            "chunk_index": hit.chunk_index,
            "chapter_title": next(
                (ch.title for ch in candidates
                 if ch.chapter_index == hit.chapter_index), ""
            ),
            "total_chunks": len(chunks),
        }

    # Layer 3: fallback — search remaining chapters, nearest-first from the
    # hint. Unconditional: the native reader sends an empty chapter_title
    # (pins matched_chapter to 0), and a former `matched_chapter != 0`
    # guard skipped this scan for exactly that case, degrading every miss
    # straight to the 40-char layer.
    for ch_idx in _chapter_scan_order():
        if ch_idx == matched_chapter:
            continue
        ch_chunks = _to_chunks(ch_idx)
        hit = _find_chunk_by_fp(ch_chunks, fp_variants)
        if hit:
            return {
                "book_id": book_id,
                "chapter_index": hit.chapter_index,
                "chunk_index": hit.chunk_index,
                "chapter_title": next(
                    (ch.title for ch in candidates
                     if ch.chapter_index == ch_idx), ""
                ),
                "total_chunks": len(ch_chunks),
            }

    # Layer 4: try shorter fingerprint (same nearest-first order)
    short_variants = []
    for v in fp_variants:
        sv = v[:40]
        if sv and sv not in short_variants:
            short_variants.append(sv)
    for ch_idx in _chapter_scan_order():
        ch_chunks = _to_chunks(ch_idx)
        hit = _find_chunk_by_fp(ch_chunks, short_variants)
        if hit:
            return {
                "book_id": book_id,
                "chapter_index": hit.chapter_index,
                "chunk_index": hit.chunk_index,
                "chapter_title": next(
                    (ch.title for ch in candidates
                     if ch.chapter_index == ch_idx), ""
                ),
                "total_chunks": len(ch_chunks),
            }

    return None


def match_in_chapters(
    db: BookDB,
    book_id: str,
    chapters: list[int],
    page_text: str,
    hint: tuple[int, int] | None = None,
) -> dict | None:
    """Fingerprint-match inside an explicit chapter scope.

    Used when the renderer supplies a structural coordinate (epub section /
    spine index): the section decides the chapter **set**, the text decides
    the **chunk**. Deliberately separate from :func:`match_page_text` — that
    function lets *hint* override the chapter, which is exactly how a
    viewport-top fingerprint at a chapter boundary traps progress in the
    previous chapter (observed: screen on ch24, position stuck on ch23).

    Returns None if the fingerprint doesn't match any scoped chapter.
    """
    if not page_text.strip() or not chapters:
        return None
    variants = _fingerprint_variants(page_text)
    order = list(dict.fromkeys(int(c) for c in chapters))
    if hint and hint[0] is not None:
        order.sort(key=lambda i: abs(i - hint[0]))

    titles = {ch.chapter_index: ch.title for ch in db.get_chapters(book_id)}

    def _result(hit, total: int) -> dict:
        return {
            "book_id": book_id,
            "chapter_index": hit.chapter_index,
            "chunk_index": hit.chunk_index,
            "chapter_title": titles.get(hit.chapter_index, ""),
            "total_chunks": total,
        }

    for ch_idx in order:
        chunks = db.get_chunks(book_id, ch_idx)
        hit = _find_chunk_by_fp(chunks, variants)
        if hit:
            return _result(hit, len(chunks))

    # Last resort: 40-char fingerprint, same scope.
    short = list(dict.fromkeys(v[:40] for v in variants if len(v) >= 20))
    if short:
        for ch_idx in order:
            chunks = db.get_chunks(book_id, ch_idx)
            hit = _find_chunk_by_fp(chunks, short)
            if hit:
                return _result(hit, len(chunks))
    return None


def _fp_hit(variants: list[str], content: str) -> bool:
    """True if any fingerprint variant is a substring of the chunk."""
    norm = _norm_ws(content)
    return any(v in norm for v in variants)


def _find_chunk_by_fp(chunks: list, variants: list[str]):
    """在块序列里按指纹找块；命中返回该块，否则 None。

    两级判据：
    1. 单块包含 —— 常规情形（滚动中视口顶落在块内部）。
    2. **跨块拼接** —— 视口顶文本横跨相邻两块。这不是异常：`jumpToAnchor`
       刻意把目标块的**块首**对齐到视口顶（还留 24px 边距），顶行天然就是
       「上一块的尾巴 + 这一块的开头」。单块包含判据对这种情况**恒 miss**，
       于是跳转落点被判成"未命中"：示数不回刷、进度不落库，退回跳转前的
       旧章（实测《窄门》ch20 ck5 的跳转：指纹 = ck4 尾「…5月27日」+ ck5
       首「为何要欺骗自己呢？…」，ck4 与 ck5 都不含整串）。
       拼接后定位偏移、再映射回所在块，命中的是**包含视口顶那一段**的块。
    """
    for ck in chunks:
        if _fp_hit(variants, ck.content):
            return ck
    if len(chunks) < 2:
        return None
    norm = [_norm_ws(c.content) for c in chunks]
    joined = " ".join(norm)
    for v in variants:
        at = joined.find(v)
        if at < 0:
            continue
        acc = 0
        for c, txt in zip(chunks, norm):
            end = acc + len(txt)
            if acc <= at <= end:
                return c
            acc = end + 1                    # +1：拼接用的那个空格
    return None


_NORM_WS = re.compile(r"\s+")


def _norm_ws(s: str) -> str:
    """Collapse all whitespace runs to single spaces (for fingerprinting)."""
    return _NORM_WS.sub(" ", s or "").strip()


def get_book_context(db: BookDB, book_id: str) -> dict:
    """Build a structured 'reading map' for a book.

    Returns front_matter / body / back_matter grouping with chunk counts.
    """
    book = db.get_book(book_id)
    if not book:
        return {"error": "Book not found"}

    chapters = db.get_chapters(book_id)

    front: list[dict] = []
    body: list[dict] = []
    back: list[dict] = []

    for ch in chapters:
        chunk_count = db.get_chunk_count(book_id, ch.chapter_index)
        entry = {
            "title": ch.title,
            "chapter_index": ch.chapter_index,
            "chunks": chunk_count,
            "type": ch.chapter_type,
        }
        if ch.chapter_type in ("cover", "copyright", "toc", "preface"):
            front.append(entry)
        elif ch.chapter_type in ("postscript", "appendix", "bibliography", "index", "notes"):
            back.append(entry)
        else:
            body.append(entry)

    # Get active reading session
    session = db.get_active_session(book_id)
    progress = None
    if session and session.positions_json:
        import json
        positions = json.loads(session.positions_json)
        if positions:
            last = positions[-1]
            progress = {
                "chapter": last["chapter"],
                "chunk": last["chunk"],
            }
            # CFI is the render layer's authoritative restore anchor
            # (display(cfi) is deterministic). Absent on records written
            # before the field existed — the reader then falls back to
            # the chunk-anchor chain.
            if last.get("cfi"):
                progress["cfi"] = last["cfi"]

    # Look up stable_book_id for annotation linking
    stable_book_id = None
    import_log = db.get_import_log_by_book(book_id)
    if import_log:
        stable_book_id = import_log.stable_book_id

    return {
        "book": {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "author_nationality": book.author_nationality,
            "format": book.format,
            "chapter_count": book.chapter_count,
            "total_chars": book.total_chars,
        },
        "structure": {
            "front_matter": front,
            "body": body,
            "back_matter": back,
        },
        "reading_progress": progress,
        "stable_book_id": stable_book_id,
    }


# ── Search ────────────────────────────────────────────────────────────────

def search_library(db: BookDB, query: str, limit: int = 20) -> list[dict]:
    """Full-text search across all imported books."""
    return db.search_books(query, limit=limit)


def search_sentences(
    db: BookDB, book_id: str, query: str, *, limit: int = 30,
) -> list[dict]:
    """Search a book for sentences containing *query*, returning full sentences.

    Each result is one atomic sentence (split on 。！？.!?\\n) that contains
    the query string.  Results are ordered by appearance (front → back).
    """
    import re

    # Sentence boundary pattern (Chinese + English)
    SENT_END = re.compile(r'([。！？.!?\n])')

    # Search across book_chunks, scoped to this book so that high-frequency
    # single-character queries are not crowded out by matches from other
    # books under the global LIMIT.
    raw_results = db.search_books(query, limit=limit * 2, book_id=book_id)

    sentences: list[dict] = []
    query_lower = query.lower()

    for r in raw_results:
        chapter_index = r.get("chapter_index", 0)
        chunk_index = r.get("chunk_index", 0)
        chapter_title = r.get("chapter_title") or ""

        # Get the full chunk content (FTS5 snippet is truncated)
        chunk = db.get_chunk(book_id, chapter_index, chunk_index)
        if not chunk:
            continue
        text = chunk.content

        # Split into atomic sentences
        parts = SENT_END.split(text)
        # Recombine: parts alternate between [text, delimiter, text, delimiter, ...]
        buf = ""
        for part in parts:
            buf += part
            if SENT_END.match(part) and buf.strip():
                sent = buf.strip()
                if query_lower in sent.lower() and len(sent) >= len(query):
                    sentences.append({
                        "sentence": sent,
                        "chapter_index": chapter_index,
                        "chunk_index": chunk_index,
                        "chapter_title": chapter_title,
                        "match_start": sent.lower().find(query_lower),
                        "match_end": sent.lower().find(query_lower) + len(query),
                    })
                buf = ""
        # Don't forget trailing text without ending delimiter
        if buf.strip() and query_lower in buf.lower():
            sentences.append({
                "sentence": buf.strip(),
                "chapter_index": chapter_index,
                "chunk_index": chunk_index,
                "chapter_title": chapter_title,
                "match_start": buf.lower().find(query_lower),
                "match_end": buf.lower().find(query_lower) + len(query),
            })

        if len(sentences) >= limit:
            break

    return sentences[:limit]


def search_chapter_overview(
    db: BookDB, book_id: str, query: str,
) -> list[dict]:
    """Return per-chapter hit counts for *query* within *book_id*.

    Each dict has: chapter_index, chapter_title, hit_count.
    Sorted by chapter_index ascending.  Uses LIKE search so it works for
    all query lengths including single CJK characters.
    """
    return db.count_hits_by_chapter(query, book_id)


def search_chapter_sentences(
    db: BookDB, book_id: str, query: str, chapter_index: int | None = None,
    *, offset: int = 0, limit: int = 20,
) -> dict:
    """Search for sentences containing *query*.

    When *chapter_index* is given, searches within that single chapter.
    When *chapter_index* is None, searches the entire book in natural
    reading order (chapter_index ASC, chunk_index ASC) — used for the
    sequential full-book search mode.

    Returns a dict with:
        - sentences: list of sentence dicts (paginated by offset/limit)
        - total: total matching sentences
        - has_more: whether more pages exist
    """
    import re

    SENT_END = re.compile(r'([。！？.!?\n])')

    # Fetch all matching chunks (no LIMIT — need full count for pagination)
    raw_results = db.search_books(
        query, limit=9999, book_id=book_id, chapter_index=chapter_index,
    )

    all_sentences: list[dict] = []
    query_lower = query.lower()

    for r in raw_results:
        chunk_index = r.get("chunk_index", 0)
        ch_idx = r.get("chapter_index", chapter_index or 0)
        chapter_title = r.get("chapter_title") or ""

        chunk = db.get_chunk(book_id, ch_idx, chunk_index)
        if not chunk:
            continue
        text = chunk.content

        # Split into atomic sentences
        parts = SENT_END.split(text)
        buf = ""
        for part in parts:
            buf += part
            if SENT_END.match(part) and buf.strip():
                sent = buf.strip()
                if query_lower in sent.lower() and len(sent) >= len(query):
                    all_sentences.append({
                        "sentence": sent,
                        "chapter_index": ch_idx,
                        "chunk_index": chunk_index,
                        "chapter_title": chapter_title,
                        "match_start": sent.lower().find(query_lower),
                        "match_end": sent.lower().find(query_lower) + len(query),
                    })
                buf = ""
        # Trailing text without ending delimiter
        if buf.strip() and query_lower in buf.lower():
            all_sentences.append({
                "sentence": buf.strip(),
                "chapter_index": ch_idx,
                "chunk_index": chunk_index,
                "chapter_title": chapter_title,
                "match_start": buf.lower().find(query_lower),
                "match_end": buf.lower().find(query_lower) + len(query),
            })

    total = len(all_sentences)
    page = all_sentences[offset:offset + limit]

    return {
        "sentences": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
    }


# ── Summary ────────────────────────────────────────────────────────────────

def format_import_summary(book: BookRow) -> str:
    """Human-readable import summary."""
    return (
        f"📖 {book.title}\n"
        f"✍️  {book.author or 'Unknown'}\n"
        f"📄 {book.chapter_count} chapters, {book.total_chars:,} chars\n"
        f"📁 {book.format.upper()} → {book.id}\n"
        f"🏷  status: {book.status}"
    )


# ── Version control helpers ───────────────────────────────────────────────────


def _versions_dir(db: BookDB) -> Path:
    """Resolve data/versions/ directory, creating it if needed."""
    vdir = db._path.parent / "versions"
    vdir.mkdir(parents=True, exist_ok=True)
    return vdir


def _snapshot_path(db: BookDB, book_id: str, chapter_index: int,
                   timestamp: int, hash_prefix: str) -> str:
    """Build relative snapshot path. Returns data/versions/... as str."""
    filename = f"{timestamp}_{hash_prefix}.txt"
    return f"versions/{book_id}/{chapter_index}/{filename}"


def _save_snapshot(db: BookDB, book_id: str, chapter_index: int,
                   operation: str, content: str,
                   new_chars: int = 0) -> str | None:
    """Save a pre-edit snapshot of chapter content to disk + DB.

    Called before edit_chapter / replace_in_chapter / remove_chapter.
    Returns version_id, or None if content is empty.
    """
    if not content:
        return None

    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
    now = _now_ms()
    vid = _new_id()
    hash_prefix = content_hash[:6]

    # Write snapshot file
    vdir = _versions_dir(db)
    ch_dir = vdir / book_id / str(chapter_index)
    ch_dir.mkdir(parents=True, exist_ok=True)
    snapshot_file = ch_dir / f"{now}_{hash_prefix}.txt"
    snapshot_file.write_text(content, encoding="utf-8")

    rel_path = f"versions/{book_id}/{chapter_index}/{snapshot_file.name}"

    row = VersionRow(
        id=vid,
        book_id=book_id,
        chapter_index=chapter_index,
        operation=operation,
        old_char_count=len(content),
        new_char_count=new_chars,
        snapshot_path=rel_path,
        content_hash=content_hash,
        timestamp=now,
    )
    db.insert_version(row)
    return vid


# ── Editing ─────────────────────────────────────────────────────────────────

def edit_book(db: BookDB, book_id: str, **fields) -> BookRow:
    """Update book metadata with validation.

    Editable fields: title, author, category_id, status, metadata_json.
    Returns the updated BookRow.
    """
    book = db.get_book(book_id)
    if book is None:
        raise ValueError(f"Book not found: {book_id}")

    allowed = {"title", "author", "category_id", "status", "metadata_json"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        raise ValueError("No valid fields to update")

    if "title" in updates and not updates["title"].strip():
        raise ValueError("Title cannot be empty")

    db.update_book(book_id, **updates)
    updated = db.get_book(book_id)
    assert updated is not None
    return updated


def edit_chapter(db: BookDB, book_id: str, chapter_index: int,
                 content: str | None = None,
                 title: str | None = None) -> dict:
    """Edit a chapter's content and/or title.

    Automatically recalculates the book's total_chars and time_updated.
    Returns a summary dict with before/after stats.
    """
    chapter = db.get_chapter(book_id, chapter_index)
    if chapter is None:
        raise ValueError(f"Chapter {chapter_index} not found in book {book_id}")

    if content is None and title is None:
        raise ValueError("At least one of content or title must be provided")

    old_chars = chapter.char_count
    updates: dict = {}
    summary_before: dict = {
        "title": chapter.title,
        "char_count": chapter.char_count,
    }

    if content is not None:
        updates["content"] = content
        updates["char_count"] = len(content)
    if title is not None:
        updates["title"] = title

    # Snapshot before edit
    _save_snapshot(db, book_id, chapter_index, "edit_chapter",
                   chapter.content, new_chars=updates.get("char_count", chapter.char_count))

    db.update_chapter(book_id, chapter_index, **updates)

    # Recalculate book total_chars
    chapters = db.get_chapters(book_id)
    new_total = sum(ch.char_count for ch in chapters)
    db.update_book(book_id, total_chars=new_total)

    return {
        "book_id": book_id,
        "chapter_index": chapter_index,
        "before": summary_before,
        "after": {
            "title": title if title is not None else chapter.title,
            "char_count": len(content) if content is not None else old_chars,
        },
        "book_total_chars": new_total,
    }


def remove_chapter(db: BookDB, book_id: str,
                   chapter_index: int) -> dict:
    """Delete a single chapter and update book stats.

    Does NOT reindex remaining chapters — the gap in chapter_index is
    intentional (simpler, and ordering still works via ORDER BY).
    """
    chapter = db.get_chapter(book_id, chapter_index)
    if chapter is None:
        raise ValueError(f"Chapter {chapter_index} not found in book {book_id}")

    book = db.get_book(book_id)
    assert book is not None

    # Snapshot before deletion (new_chars=0 because chapter will be gone)
    _save_snapshot(db, book_id, chapter_index, "delete_chapter",
                   chapter.content, new_chars=0)

    db.delete_chapter(book_id, chapter_index)

    new_count = book.chapter_count - 1
    new_chars = book.total_chars - chapter.char_count
    db.update_book(book_id, chapter_count=new_count, total_chars=new_chars)

    return {
        "book_id": book_id,
        "removed": {
            "chapter_index": chapter_index,
            "title": chapter.title,
            "char_count": chapter.char_count,
        },
        "book_after": {
            "chapter_count": new_count,
            "total_chars": new_chars,
        },
    }


def delete_book(db: BookDB, book_id: str) -> dict:
    """Delete an entire book and all its chapters from the library.

    Also removes the local copy from data/bookbank/.
    Returns a summary so the caller can confirm what was deleted.
    """
    book = db.get_book(book_id)
    if book is None:
        raise ValueError(f"Book not found: {book_id}")

    summary = {
        "book_id": book.id,
        "title": book.title,
        "author": book.author,
        "format": book.format,
        "chapter_count": book.chapter_count,
        "total_chars": book.total_chars,
    }

    # ── Write delete event to import_log BEFORE deleting book ──────────
    import_log = db.get_import_log_by_book(book_id)
    if import_log:
        now_ms = _now_ms()
        db.insert_import_log(ImportLogRow(
            id=_new_id(),
            stable_book_id=import_log.stable_book_id,
            book_id=book_id,
            event_type="delete",
            in_library=0,
            copyright_card=import_log.copyright_card,
            title=import_log.title,
            author=import_log.author,
            publisher=import_log.publisher,
            publish_year=import_log.publish_year,
            isbn=import_log.isbn,
            first_chapter=import_log.first_chapter,
            first_paragraph=import_log.first_paragraph,
            source_filename=import_log.source_filename,
            import_count=import_log.import_count,
            time_created=now_ms,
        ))
        orphaned = db.orphan_annotations_for_book(book_id)
        summary["annotations_orphaned"] = orphaned

    db.delete_book(book_id)

    # Clean up bookbank local copy
    bookbank = _bookbank_dir(db)
    for ext in (".epub", ".mobi", ".txt"):
        local_copy = bookbank / f"{book_id}{ext}"
        if local_copy.exists():
            local_copy.unlink()
            summary["local_copy_removed"] = str(local_copy)

    return summary


# ── Replace ──────────────────────────────────────────────────────────────────

def replace_in_chapter(db: BookDB, book_id: str, chapter_index: int,
                       old_text: str, new_text: str = "",
                       mode: str = "literal",
                       dry_run: bool = True,
                       context: int = 50) -> dict:
    """Find-and-replace within a chapter — like WPS/Word replace.

    Args:
        book_id: Book to operate on
        chapter_index: 0-based chapter index
        old_text: Text to find (literal string or regex pattern)
        new_text: Replacement text (default: empty = delete matches)
        mode: 'literal' (default) — exact string match
              'regex'   — Python regex pattern (re.sub)
        dry_run: True = preview only, False = apply changes
        context: Chars of surrounding context in preview snippets (default 50).
                 Use 200+ when you need to verify larger surrounding text,
                 or a small value like 20 for a quick scan.

    Returns:
        dict with count, preview snippets, and char stats.
    """
    chapter = db.get_chapter(book_id, chapter_index)
    if chapter is None:
        raise ValueError(f"Chapter {chapter_index} not found in book {book_id}")

    if not old_text:
        raise ValueError("old_text cannot be empty")

    if context < 10:
        context = 10  # floor to avoid empty snippets

    content = chapter.content

    # ── Find all matches ──────────────────────────────────────────────
    if mode == "literal":
        # Find all literal occurrences
        positions: list[int] = []
        idx = 0
        while True:
            idx = content.find(old_text, idx)
            if idx == -1:
                break
            positions.append(idx)
            idx += len(old_text)
        match_count = len(positions)
    elif mode == "regex":
        try:
            compiled = re.compile(old_text)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc
        matches = list(compiled.finditer(content))
        positions = [m.start() for m in matches]
        match_count = len(matches)
    else:
        raise ValueError(f"Unknown mode: {mode}. Use 'literal' or 'regex'.")

    # ── Build preview snippets ────────────────────────────────────────
    previews: list[dict] = []
    for i, pos in enumerate(positions):
        if mode == "literal":
            match_text = old_text
            match_len = len(old_text)
        else:
            match_text = matches[i].group()
            match_len = len(match_text)

        # Before snippet: original text around the match
        before_snippet = (
            content[max(0, pos - context):pos]
            + match_text
            + content[pos + match_len:pos + match_len + context]
        )
        # After snippet: manually swap matched portion, preserving positions
        after_snippet = (
            content[max(0, pos - context):pos]
            + new_text
            + content[pos + match_len:pos + match_len + context]
        )

        previews.append({
            "position": pos,
            "before": f"…{before_snippet}…",
            "after": f"…{after_snippet}…",
            "match": match_text,
        })

    # ── Compute replacement result ────────────────────────────────────
    if mode == "literal":
        new_content = content.replace(old_text, new_text)
    else:
        new_content = compiled.sub(new_text, content)

    new_char_count = len(new_content)

    # Apply to DB only if not dry_run
    if not dry_run:
        _save_snapshot(db, book_id, chapter_index, "replace",
                       chapter.content, new_chars=new_char_count)

        db.update_chapter(book_id, chapter_index, content=new_content,
                          char_count=new_char_count)

        # Recalculate book total_chars
        chapters = db.get_chapters(book_id)
        new_total = sum(ch.char_count for ch in chapters)
        db.update_book(book_id, total_chars=new_total)

    return {
        "book_id": book_id,
        "chapter_index": chapter_index,
        "chapter_title": chapter.title,
        "mode": mode,
        "dry_run": dry_run,
        "count": match_count,
        "preview": previews[:10],  # cap at 10 previews to avoid huge output
        "preview_capped": match_count > 10,
        "total_matches": match_count,
        "chars_before": chapter.char_count,
        "chars_after": new_char_count,
        "chars_delta": new_char_count - chapter.char_count,
    }


# ── Categories ──────────────────────────────────────────────────────────────

def manage_categories(db: BookDB, action: str, **kwargs) -> dict:
    """Unified category management: create, update, delete, list.

    Args:
        action: 'create', 'update', 'delete', or 'list'
        **kwargs: category_id, name, parent_id, sort_order (varies by action)

    Returns:
        dict with action-specific result.
    """
    if action == "create":
        name = kwargs.get("name", "").strip()
        if not name:
            raise ValueError("name is required for create")
        cat = BookCategoryRow(
            id=_new_id(),
            name=name,
            parent_id=kwargs.get("parent_id"),
            sort_order=kwargs.get("sort_order", 0),
            time_created=_now_ms(),
        )
        db.insert_category(cat)
        return {"action": "create", "category": {
            "id": cat.id, "name": cat.name,
            "parent_id": cat.parent_id, "sort_order": cat.sort_order,
        }}

    if action == "update":
        cat_id = kwargs.get("category_id", "")
        if not cat_id:
            raise ValueError("category_id is required for update")
        fields = {}
        if "name" in kwargs and kwargs["name"] is not None:
            fields["name"] = kwargs["name"].strip()
        if "sort_order" in kwargs:
            fields["sort_order"] = kwargs["sort_order"]
        if "parent_id" in kwargs:
            fields["parent_id"] = kwargs["parent_id"]
        if not fields:
            raise ValueError("No fields to update")
        db.update_category(cat_id, **fields)
        return {"action": "update", "category_id": cat_id, "updated_fields": list(fields.keys())}

    if action == "delete":
        cat_id = kwargs.get("category_id", "")
        if not cat_id:
            raise ValueError("category_id is required for delete")
        db.delete_category(cat_id)
        return {"action": "delete", "category_id": cat_id,
                "note": "Books under this category have been uncategorized"}

    if action == "list":
        cats = db.list_categories()
        return {
            "action": "list",
            "count": len(cats),
            "categories": [
                {"id": c.id, "name": c.name, "parent_id": c.parent_id,
                 "sort_order": c.sort_order}
                for c in cats
            ],
        }

    raise ValueError(f"Unknown action: {action}. Valid: create, update, delete, list")


# ── Version control ──────────────────────────────────────────────────────────

def list_versions(db: BookDB, book_id: str,
                  chapter_index: int | None = None,
                  limit: int = 50) -> list[dict]:
    """List version history for a book or a specific chapter."""
    rows = db.list_versions(book_id, chapter_index=chapter_index, limit=limit)
    return [
        {
            "version_id": r.id,
            "book_id": r.book_id,
            "chapter_index": r.chapter_index,
            "operation": r.operation,
            "old_char_count": r.old_char_count,
            "new_char_count": r.new_char_count,
            "snapshot_path": r.snapshot_path,
            "content_hash": r.content_hash,
            "timestamp": r.timestamp,
        }
        for r in rows
    ]


def rollback_chapter(db: BookDB, version_id: str,
                     dry_run: bool = True) -> dict:
    """Restore a chapter to a previous version from snapshot.

    Args:
        version_id: The version to rollback to
        dry_run: True = preview only, False = apply

    Returns:
        dict with rollback summary.
    """
    version = db.get_version(version_id)
    if version is None:
        raise ValueError(f"Version not found: {version_id}")

    if not version.snapshot_path:
        raise ValueError(f"Version {version_id} has no snapshot file")

    # Read snapshot content
    snapshot_file = db._path.parent / version.snapshot_path
    if not snapshot_file.exists():
        raise ValueError(f"Snapshot file missing: {snapshot_file}")

    old_content = snapshot_file.read_text(encoding="utf-8")
    old_chars = len(old_content)

    # Get current chapter
    chapter = db.get_chapter(version.book_id, version.chapter_index)
    if chapter is None:
        raise ValueError(
            f"Chapter {version.chapter_index} not found in book {version.book_id}. "
            f"It may have been deleted — manual recovery required."
        )

    current_content = chapter.content
    current_chars = len(current_content)

    if not dry_run:
        # Save a snapshot of current state first (so rollback is reversible)
        _save_snapshot(db, version.book_id, version.chapter_index,
                       "rollback", current_content, new_chars=old_chars)

        db.update_chapter(version.book_id, version.chapter_index,
                          content=old_content, char_count=old_chars)

        chapters = db.get_chapters(version.book_id)
        new_total = sum(ch.char_count for ch in chapters)
        db.update_book(version.book_id, total_chars=new_total)

    return {
        "version_id": version_id,
        "book_id": version.book_id,
        "chapter_index": version.chapter_index,
        "operation": version.operation,
        "dry_run": dry_run,
        "current_chars": current_chars,
        "restored_chars": old_chars,
        "chars_delta": old_chars - current_chars,
        "snapshot_hash": version.content_hash,
    }


# ── Annotation system: copyright card + stable identity matching ─────────────


def recover_orphaned_annotations(
    db: BookDB, stable_book_id: str, new_book_id: str,
) -> dict:
    """Re-link orphaned annotations after a book re-import.

    Called from ``import_book`` after a matched re-import. Uses the
    persistent coordinates (paragraph_index, offset_in_paragraph) stored
    in v14 to re-locate annotations in the newly imported chapter text.

    Recovery strategy (ordered by precision):
        1. **Exact coordinate**: paragraph_index + offset_in_paragraph →
           locate the same paragraph in the new chapter text, verify
           selected_text matches at that offset.
        2. **FTS5 fallback**: full-text search for selected_text within
           the same chapter (±5 paragraph window).
        3. **Give up**: keep match_status='orphaned' (context_window-based
           fuzzy matching reserved for future implementation).

    Args:
        db: BookDB instance.
        stable_book_id: The stable identity linking old and new imports.
        new_book_id: The newly imported book's content-hash ID.

    Returns:
        dict with ``{recovered: N, still_orphaned: M}``.
    """
    orphans = db.get_orphaned_annotations(stable_book_id)
    if not orphans:
        return {"recovered": 0, "still_orphaned": 0}

    recovered = 0
    still_orphaned = 0

    for ann in orphans:
        chapter_idx = ann.chapter_index
        if chapter_idx is None:
            still_orphaned += 1
            continue

        # Get the new chapter text
        chapter = db.get_chapter(new_book_id, chapter_idx)
        if not chapter or not chapter.content:
            still_orphaned += 1
            continue

        content = chapter.content
        paragraphs = _extract_paragraphs(content)
        relinked = False

        # ── Strategy 1: Exact coordinate match ─────────────────────────
        if ann.paragraph_index is not None and ann.offset_in_paragraph is not None:
            try:
                para = paragraphs[ann.paragraph_index]
                offset = ann.offset_in_paragraph
                sel_len = len(ann.selected_text)
                if offset + sel_len <= len(para):
                    candidate = para[offset:offset + sel_len]
                    if candidate == ann.selected_text:
                        chunk_idx = _find_chunk_for_offset(
                            db, new_book_id, chapter_idx, paragraphs,
                            ann.paragraph_index, ann.offset_in_paragraph,
                        )
                        db.relink_annotation(
                            ann.id, new_book_id, chapter_idx,
                            chunk_idx if chunk_idx is not None else 0,
                        )
                        recovered += 1
                        relinked = True
            except (IndexError, Exception):
                pass

        # ── Strategy 2: FTS5 search ────────────────────────────────────
        if not relinked:
            results = db.search_books(ann.selected_text, limit=10)
            for r in results:
                if r.get("book_id") != new_book_id:
                    continue
                r_ch = r.get("chapter_index")
                r_ck = r.get("chunk_index")
                if r_ch is None or r_ck is None:
                    continue
                # Verify the text exists in the chunk
                ck = db.get_chunk(new_book_id, r_ch, r_ck)
                if ck and ann.selected_text in ck.content:
                    db.relink_annotation(
                        ann.id, new_book_id, r_ch, r_ck,
                    )
                    recovered += 1
                    relinked = True
                    break

        if not relinked:
            still_orphaned += 1

    return {"recovered": recovered, "still_orphaned": still_orphaned}


def _find_chunk_for_offset(
    db: BookDB, book_id: str, chapter_index: int,
    paragraphs: list[str], para_index: int, offset_in_para: int,
) -> int | None:
    """Find which chunk a paragraph+offset falls into.

    Re-runs ``_chunk_paragraphs`` on the same paragraphs to determine
    chunk boundaries, then scans for the paragraph by index.
    """
    chunk_texts = _chunk_paragraphs(paragraphs)
    para_counter = 0
    for ck_idx, ck_text in enumerate(chunk_texts):
        ck_paras = _extract_paragraphs(ck_text)
        next_counter = para_counter + len(ck_paras)
        if para_counter <= para_index < next_counter:
            return ck_idx
        para_counter = next_counter
    return None


def _build_copyright_card(
    metadata: dict[str, str],
    chapters_raw: list[tuple[str, list[str] | str]],
    source_filename: str,
) -> str:
    """Build a JSON copyright card from parsed metadata and chapter content.

    This is the raw data that LLM matching (Steps 1-2) uses.  Step 0
    (filename matching) doesn't need it, but we always store it so the
    data is available when LLM matching is triggered later.
    """
    import json

    # First chapter info
    first_chapter_title = ""
    first_paragraph = ""
    for label, paras in chapters_raw:
        if isinstance(paras, str):
            text = paras
        else:
            text = "\n".join(paras)
        if len(text.strip()) >= 50:
            first_chapter_title = label or ""
            # Truncate to ~100 chars for the card
            first_paragraph = text.strip()[:120]
            break

    return json.dumps({
        "title": metadata.get("title", ""),
        "author": metadata.get("author", ""),
        "translator": metadata.get("translator"),
        "publisher": metadata.get("publisher"),
        "publish_year": metadata.get("publish_year"),
        "isbn": metadata.get("isbn"),
        "first_chapter_title": first_chapter_title,
        "first_paragraph": first_paragraph,
        "source_filename": source_filename,
    }, ensure_ascii=False)


def _match_stable_identity_sync(
    db: BookDB,
    title: str,
    author: str,
    source_filename: str,
) -> tuple[str, str, int]:
    """Step 0 — filename-based matching (no LLM).

    Compares the source filename against active import_log records.
    Returns (stable_book_id, confidence, import_count).

    If no match is found, generates a NEW stable_book_id and returns
    confidence='no_match'.  Steps 1-2 (LLM-based) can be triggered
    asynchronously later to refine the match.
    """
    from storage.db_base import _new_id as _gen_id

    def _make_stable_id() -> str:
        return "sb-" + _gen_id()

    filename_stem = Path(source_filename).stem.lower().strip()

    active_logs = db.get_active_import_logs()
    for log in active_logs:
        log_title = (log.title or "").lower().strip()
        log_filename = (log.source_filename or "").lower().strip()
        log_stem = Path(log_filename).stem if log_filename else ""

        # Exact filename stem match
        if filename_stem and log_stem and filename_stem == log_stem:
            return (log.stable_book_id, "high", log.import_count + 1)

        # Filename stem contained in title or vice versa
        if filename_stem and log_title:
            if filename_stem in log_title or log_title in filename_stem:
                return (log.stable_book_id, "high", log.import_count + 1)

        # Title exact match with similar filename
        title_norm = title.lower().strip()
        if title_norm and log_title and title_norm == log_title:
            return (log.stable_book_id, "medium", log.import_count + 1)

    # No match — generate new stable identity
    return (_make_stable_id(), "no_match", 1)


# ── Annotation offset computation ───────────────────────────────────────────


def compute_annotation_offsets(
    db: BookDB, book_id: str, chapter_index: int,
    chunk_index: int, selected_text: str,
    context_before: str, context_after: str,
) -> dict:
    """Compute persistent coordinates for an annotation within chapter text.

    Uses the same ``_extract_paragraphs`` function that chunking uses,
    guaranteeing paragraph-index consistency between annotation coords
    and chunk boundaries.

    Args:
        db: BookDB instance.
        book_id: Current book ID (content hash).
        chapter_index: 0-based chapter index.
        chunk_index: Chunk index (for logging only — coordinates are chapter-level).
        selected_text: The user-selected text.
        context_before: Up to 40 chars before the selection (from frontend).
        context_after: Up to 40 chars after the selection (from frontend).

    Returns:
        dict with keys: paragraph_index, offset_in_paragraph,
        chapter_offset_start, chapter_offset_end, context_window.
        All values are None if the chapter text couldn't be found or
        the selected_text couldn't be located.
    """
    # 1. Get full chapter text
    chapter = db.get_chapter(book_id, chapter_index)
    if not chapter or not chapter.content:
        return {
            "paragraph_index": None,
            "offset_in_paragraph": None,
            "chapter_offset_start": None,
            "chapter_offset_end": None,
            "context_window": None,
        }

    content = chapter.content

    # 2. Locate selected_text in chapter content
    # Best: context_before + selected_text + context_after as a search key
    search_key = (context_before or "") + selected_text + (context_after or "")
    pos = content.find(search_key)
    if pos < 0 and context_before:
        pos = content.find((context_before or "") + selected_text)
    if pos < 0 and context_after:
        pos = content.find(selected_text + (context_after or ""))
    if pos < 0:
        pos = content.find(selected_text)

    if pos < 0:
        return {
            "paragraph_index": None,
            "offset_in_paragraph": None,
            "chapter_offset_start": None,
            "chapter_offset_end": None,
            "context_window": None,
        }

    # Adjust pos to point to selected_text start (search_key may include
    # context_before, so we find selected_text within the match window)
    if context_before:
        match_end = pos + len(search_key)
        sel_pos = content.find(selected_text, pos, match_end)
        if sel_pos >= 0:
            pos = sel_pos

    chapter_offset_start = pos
    chapter_offset_end = pos + len(selected_text)

    # 3. Compute paragraph_index and offset_in_paragraph
    paragraphs = _extract_paragraphs(content)

    # Walk paragraphs tracking cumulative position in content
    scan_pos = 0
    paragraph_index = None
    offset_in_paragraph = None
    for p_idx, para in enumerate(paragraphs):
        para_pos = content.find(para, scan_pos)
        if para_pos < 0:
            continue
        para_end = para_pos + len(para)
        if para_pos <= chapter_offset_start < para_end:
            paragraph_index = p_idx
            offset_in_paragraph = chapter_offset_start - para_pos
            break
        scan_pos = para_end

    # 4. Extract context_window — 150 chars before and after
    ctx_start = max(0, chapter_offset_start - 150)
    ctx_end = min(len(content), chapter_offset_end + 150)
    context_window = content[ctx_start:ctx_end]

    return {
        "paragraph_index": paragraph_index,
        "offset_in_paragraph": offset_in_paragraph,
        "chapter_offset_start": chapter_offset_start,
        "chapter_offset_end": chapter_offset_end,
        "context_window": context_window,
    }
