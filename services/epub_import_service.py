"""EPUB import orchestration — skeleton extraction + selector-based splitting.

Thin orchestration layer between the HTML-level tools in
:mod:`file_readers.html_skeleton` and the database-level storage in
:mod:`storage.db`.  Called by the ``epub_import`` tool registered in
:mod:`tools`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from file_readers import open_book
from file_readers.epub_reader import (
    EpubReader, _clean_html, _decode, _format_html, _format_html_styled,
)
from file_readers.epub_semantics import VALID_CHAPTER_TYPES
from file_readers.html_skeleton import extract_html_skeleton, split_html_by_heading
from services.library_service import (
    _bookbank_dir,
    _chunk_paragraph_groups,
    _chunk_paragraphs,
    _classify_chapter,
    _hash_file,
    _new_id,
    _now_ms,
    _paragraph_offsets,
    _title_match_score,
)
from storage.db import BookDB
from storage.db import (
    TABLE_BOOK_CHAPTERS,
    TABLE_BOOK_CHUNKS,
)
from storage.models import BookChapterRow, BookChunkRow, BookRow


# ── Step 1: skeleton extraction ──────────────────────────────────────────


def extract_epub_skeleton(path: str) -> dict:
    """Extract a structural summary and HTML skeleton from an EPUB.

    Opens the EPUB, reads OPF/NCX metadata, and returns the first
    meaningful spine item's HTML as a compact skeleton that an LLM can
    analyse to determine the heading pattern.

    Returns a dict with keys:
        * **title** — book title from OPF metadata
        * **author** — creator from OPF metadata
        * **spine_count** — number of spine items
        * **toc** — list of ``{"index": int, "label": str}``
        * **skeleton** — compact HTML skeleton string
        * **source_spine_index** — which spine item the skeleton came from
    """
    reader = open_book(path)
    try:
        if not isinstance(reader, EpubReader):
            return {"error": "Only EPUB files are supported by epub_import. "
                             "Use import_book for MOBI files."}

        reader._ensure_loaded()

        # Find first meaningful spine item (skip cover / blank pages)
        source_spine_idx = -1
        raw_html = ""
        for si in range(len(reader._spine_ids)):
            try:
                html = reader._read_spine_html(si)
            except Exception:
                continue
            # Check for actual text content (beyond just tags)
            import re
            text = re.sub(r"<[^>]+>", "", html).strip()
            if len(text) >= 200:
                source_spine_idx = si
                raw_html = html
                break

        skeleton = ""
        if raw_html:
            skeleton = extract_html_skeleton(raw_html)
        elif len(reader._spine_ids) > 0:
            # All spine items are tiny — just use the first one
            try:
                html = reader._read_spine_html(0)
                skeleton = extract_html_skeleton(html)
                source_spine_idx = 0
            except Exception:
                pass

        # Build TOC summary
        toc = []
        for i, entry in enumerate(reader._toc):
            toc.append({
                "index": i,
                "label": entry.get("label", ""),
            })

        # Get metadata
        title = reader._metadata.get("title", Path(path).stem)
        author = reader._metadata.get("creator", "Unknown")

        return {
            "title": title,
            "author": author,
            "filename": Path(path).stem,
            "spine_count": len(reader._spine_ids),
            "toc": toc,
            "skeleton": skeleton,
            "source_spine_index": source_spine_idx,
            # L1 structural signals (guide/landmarks are parsed with the
            # OPF/nav — zero extra I/O; content epub:type only for candidates)
            "structural": {
                "guide": reader.guide_type_map(),
                "landmarks": reader.landmark_type_map(),
            },
            # L3 candidate digest for the agent to classify inline
            "candidates": _extract_candidates(reader),
        }
    finally:
        if hasattr(reader, "close"):
            reader.close()


def _extract_candidates(reader: EpubReader) -> list[dict]:
    """Build the L3 classification digest: spine_index + label + ~200-char
    preview for front/back matter candidates.

    Candidate set = first 3 + last 3 spine items + any whose label or
    guide/landmark signal looks non-body (all zero-I/O).  Content ``epub:type``
    is only scanned for the resulting candidate set (reusing the preview
    read) — never the whole book, keeping Step 1 cheap.  Spine items whose
    structural signal resolves to a non-body type are also kept.
    """
    n = len(reader._spine_ids)
    if n == 0:
        return []

    suspicious: set[int] = set()
    for si in range(n):
        label = reader.get_chapter_label(si)
        if _classify_chapter(label, index=si, total=n) != "body":
            suspicious.add(si)
        if reader.guide_type_for_spine(si) or reader.landmark_type_for_spine(si):
            suspicious.add(si)

    candidates: list[dict] = []
    for si in sorted(set(range(min(3, n))) | set(range(max(0, n - 3), n)) | suspicious):
        label = reader.get_chapter_label(si)
        preview = ""
        try:
            html = reader._read_spine_html(si)
            preview = _clean_html(html)[:200]
            # Content-level epub:type — reuse the preview read, no re-read
            epub_t = reader.scan_spine_epub_type(si, html)
            if epub_t and epub_t != "body":
                candidates.append({
                    "spine_index": si, "label": label,
                    "preview": preview, "structural": epub_t,
                })
                continue
        except Exception:
            pass
        candidates.append({"spine_index": si, "label": label, "preview": preview})
    return candidates


# ── Step 2: selector-based import ────────────────────────────────────────


def import_epub_with_split(
    path: str,
    method: str,
    selector: str,
    db: BookDB,
    *,
    category_id: str | None = None,
    book_id: str | None = None,
    chapter_types: str = "",
    metadata_title: str | None = None,
    metadata_author: str | None = None,
    metadata_nationality: str | None = None,
) -> tuple[BookRow, bool]:
    """Import an EPUB by splitting each spine item at heading boundaries.

    Uses the LLM-chosen *method* and *selector* to detect chapter headings
    mechanically — no further LLM calls are made.

    Args:
        path: Path to the EPUB file.
        method: ``"h_tags"``, ``"font_size"``, or ``"css_class"``.
        selector: Method-specific value (font size number, CSS class name,
            or ``""`` for *h_tags*).
        db: BookDB instance.
        category_id: Optional category for organisation.
        book_id: When provided, force-reimport over an existing book
            (deletes old chapters/chunks first).  When omitted, normal
            MD5-based dedup applies.
        chapter_types: Optional L3 classification from the outer agent, a
            JSON string mapping ``spine_index`` to a chapter_type enum value
            (e.g. ``'{"0":"preface","4":"postscript"}'``).  Invalid JSON or
            values are silently dropped for that key.  When provided it
            overrides the L1 structural signals.
        metadata_title / metadata_author / metadata_nationality: Optional
            cleaned book metadata from the outer agent (see Step 1
            ``filename``).  Non-empty title/author override the OPF values;
            empty or None fall back to OPF → filename.

    Returns:
        ``(BookRow, is_new)`` — *is_new* is ``False`` when the book was
        already imported and *book_id* was not provided.
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Book not found: {path}")

    computed_id = _hash_file(source)
    now = _now_ms()
    is_reimport = book_id is not None

    # ── Re-import path: delete old chapters/chunks ────────────────────
    if is_reimport:
        _delete_book_content(db, book_id)
        target_id = book_id
    else:
        # Dedup check
        existing = db.get_book(computed_id)
        if existing:
            return existing, False
        target_id = computed_id

    # ── Copy to bookbank ─────────────────────────────────────────────
    bookbank = _bookbank_dir(db)
    local_copy = bookbank / f"{target_id}{source.suffix}"
    if not local_copy.exists():
        shutil.copy2(source, local_copy)

    reader = open_book(path)
    try:
        if not isinstance(reader, EpubReader):
            raise ValueError("import_epub_with_split only supports EPUB files")

        reader._ensure_loaded()

        # ── L3: parse agent-provided classification (defensive) ───────
        agent_map: dict[int, str] = {}
        if chapter_types:
            try:
                parsed = json.loads(chapter_types)
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        si = int(k)  # JSON keys are always strings
                        if isinstance(v, str) and v in VALID_CHAPTER_TYPES:
                            agent_map[si] = v
            except (ValueError, TypeError, json.JSONDecodeError):
                agent_map = {}

        # ── Split every spine item and collect ────────────────────────
        # Each entry: (label, paragraphs, spine_idx, is_first_section)
        # paragraphs: list[{"text": str, "sig": str}] — styled paragraphs
        # (sig = 体例签名, 从源 HTML 当下抓到, 随段落流同行)
        chapters_raw: list[tuple[str, list[dict], int, bool]] = []
        # L1: spine_idx → resolved structural type (content epub:type +
        # guide + landmarks), scanned from the html already read below.
        structural_map: dict[int, str] = {}

        for spine_idx in range(len(reader._spine_ids)):
            try:
                raw_html = reader._read_spine_html(spine_idx)
            except Exception:
                chapters_raw.append((f"(failed to read spine[{spine_idx}])", [], spine_idx, True))
                continue

            # L1 structural semantics — content epub:type scanned from the
            # html we just read (cached), guide/landmarks from OPF/nav parse.
            reader.scan_spine_epub_type(spine_idx, raw_html)
            struct = reader.semantic_type_for_spine(spine_idx)
            if struct:
                structural_map[spine_idx] = struct[0]

            # Split at heading boundaries
            sections = split_html_by_heading(raw_html, method, selector)

            # Get the default label for this spine item (NCX fallback)
            default_label = reader.get_chapter_label(spine_idx)

            for sec_idx, (heading_text, body_html, _matched_tag) in enumerate(sections):
                # Format body HTML to styled paragraphs: [{"text", "sig"}, …]
                # styled 变体在清洗同时保留每段样式签名（体例随源 HTML 一起走）。
                if body_html.strip():
                    body_paras = _format_html_styled(body_html)
                else:
                    body_paras = []

                # Skip empty sections.
                # 独立短页(封面/版权等占位) = 整文件切出 1 段且极短 → 跳过。
                # 多段结构里的嵌套标题(h2"第一章"→h3"第511节" 无独立正文)必须保留,
                # 否则灵魂永生式的"章标题下直接是节标题"会被误杀。
                all_text = "".join(p["text"] for p in body_paras).strip()
                if len(sections) == 1 and len(all_text) < 20 and heading_text:
                    continue

                # Determine the label
                if heading_text:
                    # Try to match heading text to an NCX label
                    label = _match_ncx_label(heading_text, reader._toc)
                    if not label:
                        label = heading_text
                else:
                    # No heading — use default label with optional section suffix
                    if sec_idx == 0 and len(sections) == 1:
                        label = default_label
                    else:
                        label = f"{default_label} (part {sec_idx + 1})"

                chapters_raw.append((label, body_paras, spine_idx, sec_idx == 0))

        # ── Build chapter + chunk rows (same logic as import_book) ───
        total_chars = 0
        chapter_rows: list[BookChapterRow] = []
        chunk_rows: list[BookChunkRow] = []

        for ch_idx, (label, paragraphs, spine_idx, is_first) in enumerate(chapters_raw):
            paras_text = [p["text"] for p in paragraphs]
            chapter_text = "\n\n".join(paras_text)
            groups = _chunk_paragraph_groups(paras_text)
            # 对齐: groups 是 paras_text 的连续切片 → 原 paragraphs 切片即段落对象
            _p = 0
            chunk_rows_this: list[BookChunkRow] = []
            for ck_idx, group_texts in enumerate(groups):
                group_objs = paragraphs[_p:_p + len(group_texts)]
                _p += len(group_texts)
                ck_text = "\n\n".join(group_texts)
                chunk_rows_this.append(BookChunkRow(
                    id=_new_id(),
                    book_id=target_id,
                    chapter_index=ch_idx,
                    chunk_index=ck_idx,
                    content=ck_text,
                    char_count=len(ck_text),
                    paragraph_count=len(group_texts),
                    paragraph_offsets=json.dumps(
                        _paragraph_offsets(ck_text, group_texts), ensure_ascii=False),
                    paragraph_sigs=json.dumps(
                        [o["sig"] for o in group_objs], ensure_ascii=False),
                ))

            ch_id = _new_id()
            ch_char_count = len(chapter_text)
            total_chars += ch_char_count

            # chapter_type priority: L3 agent > L1 structural > L2 label.
            # The spine-level structural type applies to its first section
            # only; later sections of the same spine fall back to label
            # classification.
            spine_type = None
            if is_first:
                spine_type = agent_map.get(spine_idx) or structural_map.get(spine_idx)

            chapter_rows.append(BookChapterRow(
                id=ch_id,
                book_id=target_id,
                chapter_index=ch_idx,
                title=label if label else f"Chapter {ch_idx + 1}",
                content=chapter_text,
                char_count=ch_char_count,
                status="raw",
                chapter_type=_classify_chapter(
                    label, index=ch_idx, total=len(chapters_raw),
                    structural_type=spine_type,
                ),
            ))

            chunk_rows.extend(chunk_rows_this)

        # ── Build metadata with cache ─────────────────────────────────
        metadata = reader._metadata.copy()
        merged_types = dict(structural_map)
        merged_types.update(agent_map)
        cache_info = {
            "epub_import_method": method,
            "epub_import_selector": selector,
            "chapter_types_source": (
                "agent" if agent_map
                else "structural" if structural_map
                else "heuristic"
            ),
            # Non-body only — keeps the map bounded and meaningful
            "chapter_type_map": {
                str(si): t for si, t in merged_types.items() if t != "body"
            },
            "structural_signals": {
                "guide": reader.guide_type_map(),
                "landmarks": reader.landmark_type_map(),
            },
        }
        metadata_json_str = json.dumps(cache_info, ensure_ascii=False)

        # Get title/author from EPUB metadata, fall back to filename.
        # Agent-provided metadata overrides when non-empty (same semantics
        # as chapter_types: provided-and-valid wins, otherwise auto-fallback).
        title = (
            metadata_title.strip()
            if metadata_title and metadata_title.strip()
            else metadata.get("title", source.stem)
        )
        author = (
            metadata_author.strip()
            if metadata_author and metadata_author.strip()
            else metadata.get("creator", "Unknown")
        )

        book_row = BookRow(
            id=target_id,
            title=title,
            author=author,
            author_nationality=(metadata_nationality or "").strip(),
            format="epub",
            source_path=str(local_copy.resolve()),
            category_id=category_id,
            metadata_json=metadata_json_str,
            chapter_count=len(chapter_rows),
            total_chars=total_chars,
            status="raw",
            time_created=now,
            time_updated=now,
        )

        # insert_book uses INSERT OR REPLACE — safe for both new and reimport.
        # Old chapters/chunks were already deleted above for reimport case.
        db.insert_book(book_row)
        db.insert_chapters(chapter_rows)
        db.insert_chunks(chunk_rows)

        return book_row, True

    finally:
        if hasattr(reader, "close"):
            reader.close()


def _delete_book_content(db: BookDB, book_id: str) -> None:
    """Delete chapters and chunks for a book (but keep the book row).

    Used before a force-reimport to clear old split data.
    """
    conn = db.get_conn()
    conn.execute("PRAGMA foreign_keys = OFF;")
    conn.execute(f"DELETE FROM {TABLE_BOOK_CHUNKS} WHERE book_id = ?;", (book_id,))
    conn.execute(f"DELETE FROM {TABLE_BOOK_CHAPTERS} WHERE book_id = ?;", (book_id,))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.commit()


def _match_ncx_label(heading_text: str, toc: list[dict]) -> str:
    """Try to match a heading extracted from HTML to an NCX label.

    Uses fuzzy matching — returns the best NCX label if the match score
    is high enough, otherwise returns an empty string.
    """
    if not toc or not heading_text:
        return ""
    best_score = 0
    best_label = ""
    for entry in toc:
        ncx_label = entry.get("label", "")
        if not ncx_label:
            continue
        score = _title_match_score(heading_text, ncx_label)
        if score > best_score:
            best_score = score
            best_label = ncx_label
    # Only return if we have a strong match (exact or one-contains-other)
    if best_score >= 2:
        return best_label
    return ""
