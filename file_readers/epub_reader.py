r"""EPUB/MOBI reader — originally ported from a PowerShell reader script.

Reads EPUB files directly (ZIP + XML parsing, zero extra deps).
Converts MOBI via Calibre (ebook-convert) or the Python ``mobi`` library.
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from file_readers.epub_semantics import map_epub3_type, resolve_spine_semantic
from file_readers.html_skeleton import _clean_inline_tags

# ── Helpers ────────────────────────────────────────────────────────────────


def _decode(text: bytes) -> str:
    """Best-effort UTF-8 decode with replacement."""
    return text.decode("utf-8", errors="replace")


def _clean_html(raw: str) -> str:
    """Strip HTML tags, decode entities, collapse blank lines.

    Handles MOBI-specific markup (mbp:pagebreak) and block-level elements
    (p, blockquote) by inserting newlines before stripping inline tags.
    """
    text = raw
    # MOBI page break markers
    text = re.sub(r"<mbp:pagebreak\s*/?>", "\n\n", text, flags=re.IGNORECASE)
    # Block-level → newlines (before stripping other tags)
    text = re.sub(r"</?p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?blockquote[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Remove inline elements: superscripts, images, links, font styling
    text = re.sub(r"<sup[^>]*>.*?</sup>", "", text, flags=re.DOTALL)
    text = re.sub(r"<img[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<a[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</a>", "", text)
    text = re.sub(r"</?font[^>]*>", "", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    # Collapse blank lines
    text = re.sub(r"\n[ \t]*\n+", "\n\n", text)
    return text.strip()




def _is_heading_p(attrs: str, inner: str) -> bool:
    """Detect whether a no-class <p> should be treated as a heading.

    MOBI/Kindle books use inline styles instead of CSS classes.
    Chapter titles are typically centered, with large/bold font.
    The font size may be on the ``<p>`` itself or on a child ``<font>`` tag.
    """
    # Alignment: check both <p> attrs and inline style
    has_center = (
        bool(re.search(r'align\s*=\s*["\']?center', attrs, re.IGNORECASE))
        or "text-align:center" in attrs
    )
    # Large font: may be on <p> attrs or on a child <font size="N"> tag
    # Chapter headings in MOBI files commonly use size="4" or higher
    has_large_font = bool(
        re.search(r'size\s*=\s*["\']?[4-7]', attrs)
        or re.search(r'<font[^>]*size\s*=\s*["\']?[4-7]', inner, re.IGNORECASE)
    )
    has_bold = "<b>" in inner.lower() or "<b " in inner.lower()
    inner_clean = re.sub(r"<[^>]+>", "", inner).strip()
    # Only treat as heading if the text is short, non-empty, and not purely numeric
    is_short = 1 <= len(inner_clean) <= 120
    not_pure_digits = not inner_clean.isdigit()
    return has_center and has_large_font and has_bold and is_short and not_pure_digits


def _format_html(html_content: str, as_paragraphs: bool = False) -> str | list[str]:
    """Format HTML content into readable text, preserving paragraph structure.

    Two-pass matching:

    1. **Class-based <p>** (EPUB): recognises common Chinese-ebook CSS classes:
       *Title* / title → chapter/section headings
       zs / postil / footnote → annotations (italic blockquote style)
       right-content / center-content → aligned text
       default → normal paragraph

    2. **No-class <p>** (MOBI / Kindle): uses inline-style heuristics:
       centered + large font + bold → heading (``# text``)
       default → normal paragraph

    When *as_paragraphs* is True, returns list of paragraph strings instead
    of joined text.
    """
    # Split on MOBI page-break markers to avoid cross-page paragraph merging
    blocks = re.split(r"<mbp:pagebreak\s*/?>", html_content, flags=re.IGNORECASE)

    paragraphs: list[str] = []
    seen: set[str] = set()

    for block in blocks:
        # ── Pass 1: class-based <p> (EPUB / Calibre-generated) ──────
        for m in re.finditer(
            r'<p[^>]*class="([^"]*)"[^>]*>(.*?)</p>',
            block,
            re.DOTALL,
        ):
            css_class = m.group(1).strip()
            inner = m.group(2)
            text = _clean_inline_tags(inner)
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)

            if "Title" in css_class or "title" in css_class:
                paragraphs.append(f"# {text}")
            elif css_class in ("zs", "postil", "footnote"):
                paragraphs.append(f"> _{text}_")
            elif css_class in ("right-content", "center-content"):
                paragraphs.append(f"  {text}")
            else:
                paragraphs.append(text)

        # ── Pass 2: no-class <p> (MOBI / Kindle inline styles) ─────
        for m in re.finditer(
            r"<p(?![^>]*class=)([^>]*)>(.*?)</p>",
            block,
            re.DOTALL,
        ):
            attrs = m.group(1)
            inner = m.group(2)
            text = _clean_inline_tags(inner)
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)

            if _is_heading_p(attrs, inner):
                paragraphs.append(f"# {text}")
            else:
                paragraphs.append(text)

    if not paragraphs:
        # Fallback: brute-force strip HTML, split on blank lines
        text = _clean_html(html_content)
        if text:
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    if as_paragraphs:
        return paragraphs
    return "\n\n".join(paragraphs).strip()


_STYLE_ATTR_RE = re.compile(r'style="([^"]*)"', re.IGNORECASE)

# NCX 单遍扫描：开/闭标签用来数嵌套深度，navLabel 给标题，content 给落点。
_NCX_NODE_RE = re.compile(
    r"<navPoint\b[^>]*>|</navPoint\s*>"
    r"|<navLabel\b[^>]*>\s*<text>(.*?)</text>"
    r'|<content\s+src="([^"]*)"',
    re.DOTALL | re.IGNORECASE,
)


def _inline_style(attrs: str) -> str:
    """Extract the inline ``style="..."`` payload from a <p> attribute string."""
    m = _STYLE_ATTR_RE.search(attrs)
    return m.group(1)[:80] if m else ""


def _prefix_class(css_class: str, text: str) -> str:
    """Mirror _format_html's class-based prefixing, verbatim."""
    if "Title" in css_class or "title" in css_class:
        return f"# {text}"
    if css_class in ("zs", "postil", "footnote"):
        return f"> _{text}_"
    if css_class in ("right-content", "center-content"):
        return f"  {text}"
    return text


def _format_html_styled(html_content: str) -> list[dict]:
    """Format HTML into paragraphs, each carrying its block style signature.

    Same two-pass matching as :func:`_format_html` (class-based <p> then
    no-class <p>), but returns paragraph objects ``[{"text", "sig"}]`` so the
    block's style (CSS class / inline style) survives clean-texting.

    ``sig`` is a coarse style signature for visual-family merging:
      - class-based:  ``"p|calibre19"``
      - inline-based: ``"p|inline|text-align:center;…"``

    ``text`` stays identical to what ``_format_html`` would produce for the
    same input, so a call-site can swap between the two without changing
    stored chapter content.
    """
    blocks = re.split(r"<mbp:pagebreak\s*/?>", html_content, flags=re.IGNORECASE)
    out: list[dict] = []
    seen: set[str] = set()

    for block in blocks:
        # ── Pass 1: class-based <p> (EPUB / Calibre-generated) ──────
        for m in re.finditer(
            r'<p[^>]*class="([^"]*)"[^>]*>(.*?)</p>', block, re.DOTALL,
        ):
            css_class = m.group(1).strip()
            inner = m.group(2)
            text = _clean_inline_tags(inner)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append({
                "text": _prefix_class(css_class, text),
                "sig": f"p|{css_class}",
            })

        # ── Pass 2: no-class <p> (MOBI / Kindle inline styles) ─────
        for m in re.finditer(
            r"<p(?![^>]*class=)([^>]*)>(.*?)</p>", block, re.DOTALL,
        ):
            attrs = m.group(1)
            inner = m.group(2)
            text = _clean_inline_tags(inner)
            if not text or text in seen:
                continue
            seen.add(text)
            style = _inline_style(attrs)
            if _is_heading_p(attrs, inner):
                text = f"# {text}"
            out.append({
                "text": text,
                "sig": f"p|inline|{style}",
            })

    if not out:
        # Fallback — mirror _format_html's brute-force strip
        text = _clean_html(html_content)
        if text:
            out = [
                {"text": p.strip(), "sig": "p||"}
                for p in text.split("\n\n") if p.strip()
            ]
    return out


def _looks_like_html(text: str) -> bool:
    """Quick heuristic: does this text contain HTML markup?"""
    return bool(re.search(r"<(p|div|html|body|span|h[1-6])\b", text[:2000], re.IGNORECASE))


# ── EPUB Reader ────────────────────────────────────────────────────────────


class EpubReader:
    """Read EPUB files — metadata, table of contents, and chapter text."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._zip: zipfile.ZipFile | None = None
        self._opf_dir: str = ""
        self._item_map: dict[str, str] = {}       # item id → relative href
        self._spine_ids: list[str] = []            # ordered idrefs (content only)
        # 与 _spine_ids 平行：每一项在 **OPF spine 原文**里的位置。导入时会
        # 跳过封面/目录等非正文 item，_spine_ids 的下标因此被压缩过；而
        # epub.js 渲染器用的是未压缩的 OPF 下标。落库的 chapter.spine_index
        # 必须是后者，否则 sync 的「结构定章」会整体错位（实测《窄门》差 1：
        # OPF spine[0]=titlepage 被跳过，之后每一项都前移一格）。
        self._spine_opf_index: list[int] = []
        self._media_types: dict[str, str] = {}      # item id → media-type
        self._metadata: dict[str, str] = {}
        self._toc: list[dict] = []                 # [{"label": ..., "src": ...}, ...]
        self._toc_to_spine: dict[int, int] = {}    # TOC index → spine index
        self._spine_to_toc: dict[int, int] = {}    # spine index → TOC index (reverse)
        # 过滤前的完整 NCX 目录 —— _filter_front_matter_toc 会把「扉页/版权/目录」
        # 这类条目从展示目录里摘掉，但它们的标签是 _classify_chapter 的关键词
        # 依据（"扉页"→cover、"版权信息"→copyright），必须留一份给分类兜底。
        self._toc_all: list[dict] = []
        self._spine_to_label_all: dict[int, str] = {}  # spine idx → 未过滤 NCX 标签
        # spine idx → 该书目录里的层级（0 = 最外层）。一个文件可能挂多个 NCX
        # 节点（人类简史 spine[6] 同时挂着「第一章」和它的四个小节），取**最浅**
        # 的那个 —— 那才是这个文件在目录树里的身份。
        self._spine_to_depth: dict[int, int] = {}
        self._cover_image_id: str | None = None    # manifest item id of cover image
        # ── Structural semantics (front/back matter identification) ──
        self._guide_refs: list[tuple[str, str]] = []      # (guide type, href) — EPUB2
        self._landmarks: list[tuple[str, str]] = []       # (epub:type, href) — EPUB3
        self._href_to_spine: dict[str, int] = {}          # href (no fragment) → spine idx
        self._guide_by_spine: dict[int, str] = {}         # spine idx → guide type
        self._landmark_by_spine: dict[int, str] = {}      # spine idx → landmark type
        self._spine_semantics: dict[int, str | None] = {} # spine idx → content epub:type (lazy)
        self._loaded = False

    # ── Public API ─────────────────────────────────────────────────────

    def get_info(self) -> str:
        """Return formatted metadata + table of contents."""
        self._ensure_loaded()
        lines: list[str] = []
        title = self._metadata.get("title", self.path.stem)
        author = self._metadata.get("creator", "Unknown")
        lines.append(f"📖 {title}")
        lines.append(f"✍️  {author}")
        lines.append("")
        lines.append(f"📄 {len(self._toc) if self._toc else len(self._spine_ids)} chapters")
        if self._toc:
            lines.append("")
            lines.append("## Table of Contents")
            for i, ch in enumerate(self._toc, 1):
                label = ch.get("label", f"Chapter {i}")
                lines.append(f"  {i:3d}. {label}")
        elif self._spine_ids:
            lines.append("")
            lines.append("## Chapters (no TOC found)")
            for i, _ in enumerate(self._spine_ids, 1):
                lines.append(f"  {i:3d}. Chapter {i}")
        return "\n".join(lines)

    def extract_cover(self) -> tuple[bytes, str] | None:
        """Extract cover image from EPUB zip.

        Returns (image_bytes, mime_type) or None if no cover found.
        MIME type is e.g. "image/jpeg", "image/png".
        """
        self._ensure_loaded()
        if not self._cover_image_id:
            return None
        href = self._item_map.get(self._cover_image_id)
        if not href:
            return None
        try:
            image_bytes = self._zip.read(href)
        except KeyError:
            return None
        mime_type = self._media_types.get(self._cover_image_id, "image/jpeg")
        return image_bytes, mime_type

    def read_chapter(self, index: int = 0, max_chars: int = 0) -> str:
        """Read a chapter by TOC index (0-based).  Falls back to raw spine
        index when no TOC mapping is available.

        Returns formatted text.  When *max_chars* > 0 the output is
        truncated at that boundary (last complete paragraph).
        """
        self._ensure_loaded()

        # Resolve TOC index → spine index when possible
        if self._toc and index in self._toc_to_spine:
            spine_index = self._toc_to_spine[index]
            toc_label = self._toc[index].get("label", "")
        elif self._toc and index < len(self._toc):
            # TOC exists but no href mapping — still use label, fallback spine
            spine_index = index
            toc_label = self._toc[index].get("label", "")
        else:
            spine_index = index
            toc_label = ""

        if not self._spine_ids:
            return "(empty book — no spine entries)"

        if spine_index < 0 or spine_index >= len(self._spine_ids):
            available = len(self._spine_ids)
            return f"(chapter index out of range — use 0–{available - 1})"

        item_id = self._spine_ids[spine_index]
        href = self._item_map.get(item_id)
        if not href:
            return f"(chapter {index}: no href mapping for id '{item_id}')"

        # Strip anchor fragment
        href = href.split("#")[0]
        try:
            raw_html = _decode(self._zip.read(href))
        except KeyError:
            return f"(chapter {index}: file '{href}' not found in archive)"

        text = self._format_html(raw_html)

        if toc_label:
            text = f"## {toc_label}\n\n{text}"

        if max_chars > 0 and len(text) > max_chars:
            # Cut at the last paragraph boundary within the limit
            cutoff = text[:max_chars]
            last_break = cutoff.rfind("\n\n")
            if last_break > max_chars // 2:
                text = cutoff[: last_break + 1] + "\n… (truncated)"
            else:
                text = cutoff + "\n… (truncated)"

        return text

    def read_range(self, start: int = 0, count: int = 1, max_chars: int = 8000) -> str:
        """Read a range of chapters as one text block."""
        parts: list[str] = []
        remaining = max_chars
        limit = len(self._toc) if self._toc else len(self._spine_ids)
        for i in range(start, min(start + count, limit)):
            chunk = self.read_chapter(i)
            remaining -= len(chunk)
            parts.append(chunk)
            if remaining <= 0:
                parts.append("\n… (range truncated by character limit)")
                break
        return "\n\n".join(parts)

    def search(self, query: str, max_results: int = 10) -> str:
        """Search book content and return matching excerpts."""
        self._ensure_loaded()
        query_lower = query.lower()
        results: list[tuple[int, str]] = []

        for idx, item_id in enumerate(self._spine_ids):
            href = self._item_map.get(item_id, "").split("#")[0]
            if not href:
                continue
            try:
                raw_html = _decode(self._zip.read(href))
            except KeyError:
                continue
            text = _clean_html(raw_html)
            positions = self._find_all_positions(text, query_lower)
            for pos in positions[:3]:  # max 3 excerpts per chapter
                excerpt = self._extract_excerpt(text, pos, query_lower)
                results.append((idx, excerpt))
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

        if not results:
            return f"(no matches for '{query}')"

        lines = [f"Search results for '{query}'", ""]
        for idx, excerpt in results[:max_results]:
            if self._toc and idx in self._spine_to_toc:
                label = self._toc[self._spine_to_toc[idx]].get("label", f"Chapter {idx + 1}")
            elif self._toc:
                label = f"Spine item {idx + 1}"
            else:
                label = f"Ch.{idx + 1}"
            lines.append(f"[{label}]")
            lines.append(f"  …{excerpt}…")
            lines.append("")
        return "\n".join(lines)

    # ── Internal ───────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._zip = zipfile.ZipFile(str(self.path), "r")
        self._parse_opf()
        self._parse_ncx()
        self._filter_front_matter_toc()
        self._build_toc_spine_map()
        self._build_semantic_indexes()
        self._loaded = True

    def _filter_front_matter_toc(self) -> None:
        """Remove TOC entries that are clearly front matter (扉页, copyright, etc.).

        Conservative: only removes unambiguous non-content pages.
        Does NOT remove prefaces, introductions, or forewords (real content).

        Filtering affects the DISPLAY toc only.  A full copy is kept in
        ``_toc_all`` because these labels are exactly what
        ``_classify_chapter`` keys on ("扉页"→cover, "版权信息"→copyright);
        discarding them would strand the page on a meaningless filename.
        """
        self._toc_all = list(self._toc)
        _FM_LABELS_RE = re.compile(
            r"^(扉页|封面|版权(?:页|信息|声明)?|书名页?|图书信息"
            r"|出版信息|目录|目次"
            r"|Title\s*Page|Copyright|Colophon|Half[\s-]Title"
            r"|Front\s*Cover|Back\s*Cover"
            r")\s*$",
            re.IGNORECASE,
        )
        filtered: list[dict] = []
        for entry in self._toc:
            label = entry.get("label", "").strip()
            if _FM_LABELS_RE.match(label):
                continue
            filtered.append(entry)
        self._toc = filtered

    def _build_toc_spine_map(self) -> None:
        """Map each TOC entry to its corresponding spine index via href matching."""
        if not self._spine_ids:
            return

        # 第一个声明该 href 的 spine 胜出（与逐条 break 的原语义一致）。
        href_to_spine: dict[str, int] = {}
        for si, item_id in enumerate(self._spine_ids):
            href = self._item_map.get(item_id, "").split("#")[0]
            if href:
                href_to_spine.setdefault(href, si)

        # 未过滤目录 → spine → label：被 _filter_front_matter_toc 摘掉的条目
        # （扉页/版权/目录…）在这里仍有标签，供 get_chapter_label 兜底。
        for entry in self._toc_all:
            si = href_to_spine.get(entry.get("src", "").split("#")[0])
            if si is None:
                continue
            self._spine_to_label_all.setdefault(si, entry.get("label", ""))
            d = entry.get("depth")
            if d is not None and (si not in self._spine_to_depth
                                  or d < self._spine_to_depth[si]):
                self._spine_to_depth[si] = d

        for toc_idx, toc_entry in enumerate(self._toc):
            si = href_to_spine.get(toc_entry.get("src", "").split("#")[0])
            if si is not None:
                self._toc_to_spine[toc_idx] = si
                self._spine_to_toc[si] = toc_idx

    def _build_semantic_indexes(self) -> None:
        """Map guide / landmarks hrefs to spine indices (same href matching
        convention as ``_build_toc_spine_map``).  Spine items already filtered
        by the ``_NON_CONTENT_*`` blacklist simply won't match — no extra
        handling needed."""
        self._href_to_spine.clear()
        for si, item_id in enumerate(self._spine_ids):
            href = self._item_map.get(item_id, "").split("#")[0]
            if href:
                self._href_to_spine[href] = si
        self._guide_by_spine = {
            self._href_to_spine[h]: t
            for t, h in self._guide_refs if h in self._href_to_spine
        }
        self._landmark_by_spine = {
            self._href_to_spine[h]: t
            for t, h in self._landmarks if h in self._href_to_spine
        }

    # ── Structural semantics accessors ──────────────────────────────────

    def guide_type_for_spine(self, spine_idx: int) -> str | None:
        """EPUB2 guide type for a spine item, if declared."""
        return self._guide_by_spine.get(spine_idx)

    def landmark_type_for_spine(self, spine_idx: int) -> str | None:
        """EPUB3 landmarks type for a spine item, if declared."""
        return self._landmark_by_spine.get(spine_idx)

    def semantic_type_for_spine(self, spine_idx: int) -> tuple[str, str] | None:
        """Merged structural type for a spine item.

        Returns ``(chapter_type, source)`` where source is one of
        ``epub_type`` / ``guide`` / ``landmark``, or None when no signal
        resolves to a known type.  Priority: content epub:type > guide >
        landmarks.
        """
        return resolve_spine_semantic(
            self.scan_spine_epub_type(spine_idx),
            self.guide_type_for_spine(spine_idx),
            self.landmark_type_for_spine(spine_idx),
        )

    def guide_type_map(self) -> dict[str, str]:
        """All guide types as {str(spine_idx): type} (non-empty only)."""
        return {str(si): t for si, t in self._guide_by_spine.items()}

    def landmark_type_map(self) -> dict[str, str]:
        """All landmark types as {str(spine_idx): type} (non-empty only)."""
        return {str(si): t for si, t in self._landmark_by_spine.items()}

    def epub_type_map(self) -> dict[str, str]:
        """Content-level epub:type already scanned (lazy).  Returns
        {str(spine_idx): type} for non-empty entries."""
        return {str(si): t for si, t in self._spine_semantics.items() if t}

    def scan_spine_epub_type(self, spine_idx: int, html: str | None = None) -> str | None:
        """Lazily scan one spine item's content-level ``epub:type``.

        Looks at the ``<body epub:type>`` declaration and the first
        ``<section|article|aside|nav epub:type>``.  When the body declares a
        container type (frontmatter/backmatter) and a more specific inner
        type exists, the inner one wins — but only if that inner type is
        itself mappable to a chapter_type.  An inner ``epub:type`` outside
        the vocabulary (e.g. a ``landmarks`` nav) is not "more specific",
        so the outer container type is kept rather than dropping the page's
        structural signal entirely.  Results are cached in
        ``_spine_semantics``; pass *html* to reuse content already read
        (avoids a second archive read during import).
        """
        if spine_idx in self._spine_semantics:
            return self._spine_semantics[spine_idx]
        if html is None:
            try:
                html = self._read_spine_html(spine_idx)
            except Exception:
                self._spine_semantics[spine_idx] = None
                return None

        def _norm(raw: str) -> str:
            v = raw.strip().split()[0].lower()
            return v[4:] if v.startswith("doc-") else v

        # <body epub:type> first; container types defer to an inner section
        m_body = re.search(
            r'<body\b[^>]*\bepub:type\s*=\s*["\']([^"\']+)["\']', html,
            re.IGNORECASE,
        )
        if m_body:
            body_t = _norm(m_body.group(1))
            if body_t in ("frontmatter", "backmatter"):
                m_inner = re.search(
                    r'<(?:section|article|aside|nav)\b[^>]*\bepub:type\s*=\s*'
                    r'["\']([^"\']+)["\']',
                    html, re.IGNORECASE,
                )
                inner_t = _norm(m_inner.group(1)) if m_inner else None
                # 内层只有「更具体」时才该赢。若它映射不到枚举（如 landmarks
                # 这类不在词表里的容器 nav），退回外层容器类型 —— 否则整页
                # 丢掉结构信号，最终只能退化成按文件名猜。
                self._spine_semantics[spine_idx] = (
                    inner_t if inner_t and map_epub3_type(inner_t) else body_t
                )
            else:
                self._spine_semantics[spine_idx] = body_t
            return self._spine_semantics[spine_idx]

        m_inner = re.search(
            r'<(?:section|article|aside|nav)\b[^>]*\bepub:type\s*=\s*'
            r'["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        )
        self._spine_semantics[spine_idx] = _norm(m_inner.group(1)) if m_inner else None
        return self._spine_semantics[spine_idx]

    def _parse_opf(self) -> None:
        """Parse container.xml → OPF → manifest + spine."""
        # -- container.xml --
        try:
            container = _decode(self._zip.read("META-INF/container.xml"))
        except KeyError:
            raise ValueError("Not a valid EPUB: missing META-INF/container.xml")

        m = re.search(r'<rootfile[^>]*full-path="([^"]+)"', container)
        if not m:
            raise ValueError("Cannot find OPF path in container.xml")
        opf_path = m.group(1)
        opf_dir = str(Path(opf_path).parent).replace("\\", "/")
        if opf_dir == ".":
            opf_dir = ""
        self._opf_dir = (opf_dir + "/") if opf_dir else ""

        # -- OPF --
        try:
            opf_xml = _decode(self._zip.read(opf_path))
        except KeyError:
            raise ValueError(f"OPF file not found: {opf_path}")

        # Namespace-agnostic: handle both prefixed and unprefixed variants.
        # Extract id/href independently so attribute order doesn't matter.
        for m in re.finditer(r'<item\s([^>]*?)/?>', opf_xml):
            attrs = m.group(1)
            id_m = re.search(r'id="([^"]*)"', attrs)
            href_m = re.search(r'href="([^"]*)"', attrs)
            if id_m and href_m:
                raw_href = self._opf_dir + href_m.group(1)
                self._item_map[id_m.group(1)] = os.path.normpath(raw_href).replace("\\", "/")

        # Also track media types so we can identify cover images
        self._media_types.clear()
        for m in re.finditer(r'<item\s([^>]*?)/?>', opf_xml):
            attrs = m.group(1)
            id_m = re.search(r'id="([^"]*)"', attrs)
            mt_m = re.search(r'media-type="([^"]*)"', attrs)
            if id_m and mt_m:
                self._media_types[id_m.group(1)] = mt_m.group(1)

        # Spine order — skip non-linear and common non-content entries
        _NON_CONTENT_IDS = {
            "cov001", "toc001", "cover", "titlepage",
            "copyright", "colophon", "halftitle", "frontmatter",
        }
        _NON_CONTENT_RE = re.compile(
            r"^(cov|toc|cover|titlepage|title\d*|copy|front|fm\d*|halftitle|colophon)\d*$",
            re.IGNORECASE,
        )
        _NON_CONTENT_HREF_RE = re.compile(
            r"(/(?:cover|title|copy|front|halftitle|colophon)[^/]*\.(?:x?html?|xml))",
            re.IGNORECASE,
        )
        # 注意 enumerate 计的是 OPF 里 **所有** itemref 的位置，包括下面被
        # 跳过的封面/目录项——epub.js 的 sectionIndex 正是这个原始序号
        # （parseSpine 对每个 itemref 编号，linear="no" 也不剔除）。
        for opf_pos, m in enumerate(re.finditer(r'<itemref\s([^>]*?)/?>', opf_xml)):
            attrs = m.group(1)
            idref_m = re.search(r'idref="([^"]*)"', attrs)
            if not idref_m:
                continue
            idref = idref_m.group(1)
            # Standard EPUB linear="no" means skip
            linear_m = re.search(r'linear="([^"]*)"', attrs)
            if linear_m and linear_m.group(1).lower() == "no":
                continue
            # Heuristic: skip known cover/toc/placeholder IDs
            if idref.lower() in _NON_CONTENT_IDS:
                continue
            if _NON_CONTENT_RE.match(idref):
                continue
            # Check HREF for front-matter path patterns
            href = self._item_map.get(idref, "")
            if _NON_CONTENT_HREF_RE.search(href):
                continue
            self._spine_ids.append(idref)
            self._spine_opf_index.append(opf_pos)

        # Metadata: title, creator
        m_title = re.search(
            r'<dc:title[^>]*>(.*?)</dc:title>', opf_xml, re.DOTALL
        )
        if m_title:
            self._metadata["title"] = _clean_html(m_title.group(1))

        m_creator = re.search(
            r'<dc:creator[^>]*>(.*?)</dc:creator>', opf_xml, re.DOTALL
        )
        if m_creator:
            self._metadata["creator"] = _clean_html(m_creator.group(1))

        m_publisher = re.search(
            r'<dc:publisher[^>]*>(.*?)</dc:publisher>', opf_xml, re.DOTALL,
        )
        if m_publisher:
            self._metadata["publisher"] = _clean_html(m_publisher.group(1))

        m_date = re.search(
            r'<dc:date[^>]*>(.*?)</dc:date>', opf_xml, re.DOTALL,
        )
        if m_date:
            self._metadata["date"] = _clean_html(m_date.group(1))

        m_identifier = re.search(
            r'<dc:identifier[^>]*>(.*?)</dc:identifier>', opf_xml, re.DOTALL,
        )
        if m_identifier:
            self._metadata["identifier"] = _clean_html(m_identifier.group(1))

        # ── Cover image detection ─────────────────────────────────────
        # Strategy 1: EPUB 2 <meta name="cover" content="cover-image-id"/>
        m_cover_meta = re.search(
            r'<meta\s[^>]*name="cover"\s[^>]*content="([^"]*)"',
            opf_xml, re.IGNORECASE,
        )
        if not m_cover_meta:
            m_cover_meta = re.search(
                r'<meta\s[^>]*content="([^"]*)"\s[^>]*name="cover"',
                opf_xml, re.IGNORECASE,
            )
        if m_cover_meta:
            cover_id = m_cover_meta.group(1)
            if cover_id in self._item_map:
                mt = self._media_types.get(cover_id, "")
                if mt.startswith("image/"):
                    self._cover_image_id = cover_id

        # Strategy 2: EPUB 3 <item properties="cover-image"/>
        if not self._cover_image_id:
            m_cover_item = re.search(
                r'<item\s[^>]*properties="cover-image"[^>]*/?>',
                opf_xml, re.IGNORECASE,
            )
            if m_cover_item:
                id_m = re.search(r'id="([^"]*)"', m_cover_item.group(0))
                if id_m and id_m.group(1) in self._item_map:
                    self._cover_image_id = id_m.group(1)

        # Strategy 3: fallback — first item with "cover" in id and image/ media-type
        if not self._cover_image_id:
            for item_id, mt in self._media_types.items():
                if mt.startswith("image/") and "cover" in item_id.lower():
                    if item_id in self._item_map:
                        self._cover_image_id = item_id
                        break

        # ── EPUB2 <guide> — explicit front/back matter references ─────
        # <reference type="preface" href="..."/> — type/href attribute
        # order is not guaranteed, so extract each independently.
        m_guide = re.search(
            r"<guide[^>]*>(.*?)</guide>", opf_xml, re.DOTALL | re.IGNORECASE
        )
        if m_guide:
            for m in re.finditer(
                r"<reference\b([^>]*?)/?>", m_guide.group(1), re.IGNORECASE
            ):
                attrs = m.group(1)
                type_m = re.search(r'type\s*=\s*"([^"]*)"', attrs)
                href_m = re.search(r'href\s*=\s*"([^"]*)"', attrs)
                if type_m and href_m:
                    href = os.path.normpath(
                        self._opf_dir + href_m.group(1)
                    ).replace("\\", "/").split("#")[0]
                    self._guide_refs.append((type_m.group(1).strip().lower(), href))

    def _parse_ncx(self) -> None:
        """Parse EPUB 2 NCX or EPUB 3 nav document for chapter labels."""
        if not self._zip:
            return

        # EPUB 2: toc.ncx
        # The NCX is typically listed in the OPF <spine toc="ncx"> or in <manifest>
        ncx_href = None
        for item_id, href in self._item_map.items():
            if "toc" in item_id.lower() and "ncx" in href.lower():
                ncx_href = href
                break
        # Fallback: look in OPF by searching for the ncx media-type
        if ncx_href is None:
            for item_id, href in self._item_map.items():
                if href.lower().endswith(".ncx"):
                    ncx_href = href
                    break

        if ncx_href:
            try:
                ncx_xml = _decode(self._zip.read(ncx_href))
                self._parse_ncx_content(ncx_xml)
                return
            except KeyError:
                pass

        # EPUB 3: nav.xhtml
        nav_href = None
        for item_id, href in self._item_map.items():
            if "nav" in href.lower() and href.lower().endswith((".xhtml", ".html")):
                nav_href = href
                break
        if nav_href:
            try:
                nav_html = _decode(self._zip.read(nav_href))
                self._parse_nav_html(nav_html)
                return
            except KeyError:
                pass

        # Last resort: map spine items to filenames
        for item_id in self._spine_ids:
            href = self._item_map.get(item_id, "")
            stem = Path(href).stem
            label = stem.replace("_", " ").replace("-", " ").title()
            self._toc.append({"label": label, "src": href, "depth": 0})

    def _parse_ncx_content(self, ncx_xml: str) -> None:
        """Parse EPUB 2 NCX markup — keeping the ``navPoint`` nesting depth.

        ``depth`` 是这本书**自己的目录层级**（部分 > 章 > 小节）。旧实现用一条
        ``<navPoint…><navLabel>`` 正则逐条抓，嵌套关系全丢 —— 人类简史的三层
        目录被拍成 123 条平表，章表因此无法表达从属关系，也看不出哪个节点是
        「章」哪个是「小节」。``depth`` 从 0 起（最外层 navPoint 为 0）。
        """
        stack: list[dict] = []
        for m in _NCX_NODE_RE.finditer(ncx_xml):
            tok = m.group(0)
            if tok.startswith("</"):
                if stack:
                    stack.pop()
                continue
            if tok.lower().startswith("<navpoint"):   # 开标签（navLabel 不匹配）
                node = {"label": "", "src": "", "depth": len(stack)}
                stack.append(node)
                self._toc.append(node)
                continue
            if not stack:
                continue
            if m.group(1) is not None:           # <navLabel><text>
                stack[-1]["label"] = html_mod.unescape(
                    re.sub(r"<[^>]+>", "", m.group(1)).strip()
                )
            elif m.group(2) is not None:         # <content src="…">
                stack[-1]["src"] = self._resolve_ncx_src(m.group(2))
        self._toc = [e for e in self._toc if e.get("label")]

    def _resolve_ncx_src(self, src: str) -> str:
        """NCX src 相对 OPF 目录；_item_map 里的路径相对 zip 根，补上前缀。"""
        if src and self._opf_dir and not src.startswith(self._opf_dir):
            return self._opf_dir + src
        return src

    def _parse_nav_html(self, nav_html: str) -> None:
        """Parse EPUB 3 nav document — ``<ol>`` 的嵌套即目录层级。

        与 NCX 版同样保留 ``depth``（0 = 最外层），两种格式的消费方一致。
        """
        toc_section = re.search(
            r'<nav[^>]*toc[^>]*>(.*?)</nav>', nav_html, re.DOTALL
        )
        if not toc_section:
            return
        depth = 0
        for m in re.finditer(
            r"<ol\b[^>]*>|</ol\s*>|<a[^>]*href=\"([^\"]*)\"[^>]*>(.*?)</a>",
            toc_section.group(1),
            re.DOTALL | re.IGNORECASE,
        ):
            tok = m.group(0)
            if tok.lower().startswith("<ol"):
                depth += 1
                continue
            if tok.startswith("</"):
                depth = max(0, depth - 1)
                continue
            src, label = m.group(1), html_mod.unescape(
                re.sub(r"<[^>]+>", "", m.group(2)).strip()
            )
            if label and src:
                if not src.startswith(self._opf_dir) and not src.startswith("/"):
                    src = self._opf_dir + src
                self._toc.append(
                    {"label": label, "src": src, "depth": max(0, depth - 1)}
                )

        # <nav epub:type="landmarks"> — explicit front/back matter links.
        # Searched INSIDE the landmarks section only, so a nav with
        # epub:type="landmarks toc" is not consumed by the toc branch above.
        landmarks = re.search(
            r"<nav[^>]*landmarks[^>]*>(.*?)</nav>", nav_html,
            re.DOTALL | re.IGNORECASE,
        )
        if landmarks:
            for m in re.finditer(
                r"<a\b([^>]*)>(.*?)</a>", landmarks.group(1),
                re.DOTALL | re.IGNORECASE,
            ):
                attrs = m.group(1)
                type_m = re.search(r'epub:type\s*=\s*["\']([^"\']+)["\']', attrs)
                href_m = re.search(r'href\s*=\s*["\']([^"\']+)["\']', attrs)
                if type_m and href_m:
                    src = href_m.group(1)
                    if not src.startswith(self._opf_dir) and not src.startswith("/"):
                        src = self._opf_dir + src
                    src = src.split("#")[0]
                    self._landmarks.append(
                        (type_m.group(1).strip().split()[0].lower(), src)
                    )

    def _format_html(self, html_content: str) -> str:
        """Format HTML content into readable text.

        Delegates to the module-level _format_html() function.
        """
        return _format_html(html_content, as_paragraphs=False)

    def read_paragraphs(self, index: int = 0) -> list[str]:
        """Read a chapter and return its paragraphs as a list.

        Uses the EPUB spine/TOC to locate the chapter, then parses
        HTML <p> tags into individual paragraph strings.
        """
        self._ensure_loaded()

        # Resolve TOC index → spine index (same logic as read_chapter)
        if self._toc and index in self._toc_to_spine:
            spine_index = self._toc_to_spine[index]
        elif self._toc and index < len(self._toc):
            spine_index = index
        else:
            spine_index = index

        return self._read_spine_item(spine_index, index)

    def read_paragraphs_by_spine(self, spine_index: int) -> list[str]:
        """Read a chapter directly by spine index, bypassing TOC mapping.

        Use this when iterating over all spine items rather than TOC entries.
        """
        self._ensure_loaded()
        return self._read_spine_item(spine_index, spine_index)

    def get_spine_html(self, spine_index: int) -> str:
        """Return the raw HTML for a spine item, without any formatting.

        Use this for structural analysis (skeleton extraction, heading
        detection) where the raw HTML markup is needed.  For reading text,
        use :meth:`read_paragraphs_by_spine` or :meth:`read_chapter` instead.
        """
        self._ensure_loaded()
        return self._read_spine_html(spine_index)

    def get_chapter_depth(self, spine_index: int) -> int:
        """该书目录里这个 spine 的层级；没有 NCX 信息时返回 -1。

        0 = 最外层（人类简史里是「第一部分 认知革命」这一档），1 = 章，
        2 = 小节。用于把平铺的章表还原成书自己的目录树。
        """
        self._ensure_loaded()
        return self._spine_to_depth.get(spine_index, -1)

    def opf_spine_index(self, spine_index: int) -> int:
        """内容 spine 下标 → OPF spine 下标（= epub.js 的 sectionIndex）。

        阅读器内部的 spine 下标用于 TOC / 深度 / 读原文，都是「跳掉封面
        目录后」的压缩坐标；落库给前端用的 `chapter.spine_index` 必须换成
        OPF 原始坐标，否则 sync 的结构定章整体错位。越界时原样返回。
        """
        self._ensure_loaded()
        if 0 <= spine_index < len(self._spine_opf_index):
            return self._spine_opf_index[spine_index]
        return spine_index

    def get_chapter_label(self, spine_index: int) -> str:
        """Get the best label for a spine item.

        Returns the NCX label if this spine index has a TOC entry.  Entries
        dropped from the display toc as front matter (扉页/版权/目录…) still
        yield their label here — that label is the classification keyword,
        while a filename like "part0001" carries no meaning at all.
        Otherwise generates a label from the filename.
        """
        self._ensure_loaded()
        if spine_index in self._spine_to_toc:
            toc_idx = self._spine_to_toc[spine_index]
            if toc_idx < len(self._toc):
                return self._toc[toc_idx].get("label", "")

        # 前置页兜底：展示目录里已摘掉，但标签对分类仍然有效。
        fm_label = self._spine_to_label_all.get(spine_index)
        if fm_label:
            return fm_label

        # Fallback: generate from filename
        if spine_index < len(self._spine_ids):
            item_id = self._spine_ids[spine_index]
            href = self._item_map.get(item_id, "")
            stem = Path(href).stem
            # Try to prettify the filename
            label = stem.replace("_", " ").replace("-", " ")
            return label if label else f"Chapter {spine_index + 1}"

        return f"Chapter {spine_index + 1}"

    def _read_spine_item(self, spine_index: int, display_index: int) -> list[str]:
        """Read a spine item by its spine index."""
        if not self._spine_ids:
            return [f"(empty book — no spine entries)"]

        if spine_index < 0 or spine_index >= len(self._spine_ids):
            available = len(self._spine_ids)
            return [f"(chapter index out of range — use 0–{available - 1})"]

        item_id = self._spine_ids[spine_index]
        href = self._item_map.get(item_id)
        if not href:
            return [f"(chapter {display_index}: no href mapping for id '{item_id}')"]

        href = href.split("#")[0]
        try:
            raw_html = _decode(self._zip.read(href))
        except KeyError:
            return [f"(chapter {display_index}: file '{href}' not found in archive)"]

        return _format_html(raw_html, as_paragraphs=True)

    def _read_spine_html(self, spine_index: int) -> str:
        """Internal: read raw HTML for a spine item, no formatting."""
        if spine_index < 0 or spine_index >= len(self._spine_ids):
            available = len(self._spine_ids)
            raise IndexError(
                f"Spine index {spine_index} out of range (0–{available - 1})"
            )
        item_id = self._spine_ids[spine_index]
        href = self._item_map.get(item_id, "").split("#")[0]
        if not href:
            raise KeyError(f"No href mapping for spine item '{item_id}'")
        try:
            return _decode(self._zip.read(href))
        except KeyError:
            raise FileNotFoundError(
                f"File '{href}' not found in EPUB archive"
            )

    @staticmethod
    def _find_all_positions(text: str, query_lower: str) -> list[int]:
        """Return all start positions of *query_lower* in *text*."""
        positions: list[int] = []
        start = 0
        while True:
            pos = text.lower().find(query_lower, start)
            if pos == -1:
                break
            positions.append(pos)
            start = pos + len(query_lower)
        return positions

    @staticmethod
    def _extract_excerpt(text: str, pos: int, query_lower: str) -> str:
        """Extract a ~120-char window around *pos*."""
        start = max(0, pos - 60)
        end = min(len(text), pos + len(query_lower) + 60)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return f"{prefix}{text[start:end].strip()}{suffix}"

    def close(self) -> None:
        if self._zip:
            self._zip.close()
            self._zip = None

    def __enter__(self):
        self._ensure_loaded()
        return self

    def __exit__(self, *args):
        self.close()


# ── MOBI Structured Extraction Helpers ─────────────────────────────────────


def _parse_opf_metadata(opf_path: Path) -> dict[str, str]:
    """Parse Dublin Core metadata from a MOBI-extracted OPF file.

    Returns a dict with keys: title, creator, publisher, language.
    Missing fields are omitted (caller should provide defaults).
    """
    ns = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "opf": "http://www.idpf.org/2007/opf",
    }
    try:
        tree = ET.parse(opf_path)
        root = tree.getroot()
    except Exception:
        return {}

    result: dict[str, str] = {}
    for key, tag in [
        ("title", ".//dc:title"),
        ("creator", ".//dc:creator"),
        ("publisher", ".//dc:publisher"),
        ("language", ".//dc:language"),
    ]:
        el = root.find(tag, ns)
        if el is not None and el.text:
            result[key] = el.text.strip()
    return result


def _parse_ncx_anchors(ncx_path: Path) -> list[tuple[str, int]]:
    """Parse a MOBI NCX file for chapter titles and filepos anchors.

    Returns list of (title, filepos_int) in reading order.
    Entries without a valid filepos anchor are skipped.
    """
    ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
    try:
        tree = ET.parse(ncx_path)
        root = tree.getroot()
    except Exception:
        return []

    entries: list[tuple[str, int]] = []
    for np in root.findall(".//ncx:navPoint", ns):
        label = np.find(".//ncx:navLabel/ncx:text", ns)
        content = np.find(".//ncx:content", ns)
        if label is None or content is None:
            continue
        title = (label.text or "").strip()
        if not title:
            continue
        src = content.get("src", "")
        fp_match = re.search(r"filepos(\d+)", src)
        if fp_match:
            entries.append((title, int(fp_match.group(1))))
    return entries


def _split_html_by_anchors(
    html_content: str,
    ncx_entries: list[tuple[str, int]],
) -> list[tuple[str, list[str]]]:
    """Split MOBI HTML into chapters at NCX filepos anchor positions.

    Each NCX entry becomes one chapter.  Content between consecutive
    anchors is extracted and formatted into paragraphs via
    :func:`_format_html`.

    Returns list of (chapter_title, [paragraphs]).
    """
    if not ncx_entries:
        # No NCX — return whole book as one chapter
        paras = _format_html(html_content, as_paragraphs=True)
        if isinstance(paras, str):
            paras = [paras]
        return [("", paras)]

    # Find all filepos anchor positions in the HTML
    filepos_positions: dict[int, int] = {}
    for m in re.finditer(r'<a\s+id="filepos(\d+)"', html_content):
        filepos_positions[int(m.group(1))] = m.start()

    chapters: list[tuple[str, list[str]]] = []

    for i, (title, fp_start) in enumerate(ncx_entries):
        if fp_start not in filepos_positions:
            continue

        start_pos = filepos_positions[fp_start]

        # Find the next NCX anchor that exists in HTML
        end_pos = len(html_content)
        for j in range(i + 1, len(ncx_entries)):
            _, fp_end = ncx_entries[j]
            if fp_end in filepos_positions:
                end_pos = filepos_positions[fp_end]
                break

        section_html = html_content[start_pos:end_pos]
        paras = _format_html(section_html, as_paragraphs=True)
        if isinstance(paras, str):
            paras = [paras] if paras else []

        if paras:
            chapters.append((title, paras))

    if not chapters:
        # Fallback: return whole book
        paras = _format_html(html_content, as_paragraphs=True)
        if isinstance(paras, str):
            paras = [paras]
        return [("", paras)]

    return chapters


# ── MOBI Structured Extraction Result ─────────────────────────────────────


@dataclass
class MobiStructuredBook:
    """Result of extracting and parsing a MOBI file via NCX + OPF.

    All fields are populated from the MOBI's internal XML metadata,
    not from external databases or filename heuristics.
    """
    title: str
    author: str = "Unknown"
    publisher: str = ""
    language: str = "zh"
    chapters: list[tuple[str, list[str]]] = field(default_factory=list)
    # each chapter: (title, [paragraphs])


# ── MOBI Reader ────────────────────────────────────────────────────────────


class MobiReader:
    """Convert and read MOBI files via Calibre or Python mobi library."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def get_info(self) -> str:
        """Return basic info by attempting conversion and reading first bits."""
        txt, tmpdir = self._convert()
        if not txt:
            return "(cannot convert MOBI — install Calibre or pip install mobi)"
        with open(txt, encoding="utf-8", errors="replace") as f:
            head = f.read(2000)
        self._cleanup(tmpdir)
        title = self.path.stem
        return (
            f"📖 {title}\n"
            f"(MOBI format — preview of first ~2000 chars)\n\n"
            f"{_clean_html(head)[:2000]}"
        )

    def read_chapter(self, index: int = 0, max_chars: int = 0) -> str:
        """Convert MOBI and read as plain text (MOBI has no chapter structure)."""
        txt, tmpdir = self._convert()
        if not txt:
            return "(cannot convert MOBI — install Calibre or pip install mobi)"

        with open(txt, encoding="utf-8", errors="replace") as f:
            content = f.read()

        self._cleanup(tmpdir)

        text = _format_html(content) if _looks_like_html(content) else _clean_html(content)
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + "\n… (truncated)"
        return text

    def read_paragraphs(self, index: int = 0) -> list[str]:
        """Convert MOBI and return paragraphs as a list.

        Uses the ``mobi`` library's NCX index to locate chapter boundaries
        when available, then extracts ``<p>`` tags into individual paragraphs.
        """
        txt, tmpdir = self._convert()
        if not txt:
            return ["(cannot convert MOBI — install Calibre or pip install mobi)"]

        with open(txt, encoding="utf-8", errors="replace") as f:
            content = f.read()

        self._cleanup(tmpdir)

        if _looks_like_html(content):
            return _format_html(content, as_paragraphs=True)
        else:
            # Plain text fallback: split on blank lines
            text = _clean_html(content)
            return [p.strip() for p in text.split("\n\n") if p.strip()]

    def extract_structured(self) -> MobiStructuredBook:
        """Extract MOBI, parse NCX + OPF, split HTML by filepos anchors.

        This is the primary entry point for MOBI import.  It does a single
        extraction, parses all available structural metadata (NCX chapter
        titles + filepos anchors, OPF Dublin Core metadata), splits the
        book into properly-titled chapters, and extracts paragraphs from
        each chapter section using :func:`_format_html`.

        Returns a :class:`MobiStructuredBook` with all fields populated.
        """
        # ── 1. Extract MOBI ──────────────────────────────────────────
        tmpdir = Path(tempfile.mkdtemp(prefix="mobi_reader_"))
        txt_path = tmpdir / "output.txt"

        try:
            import mobi
        except ImportError:
            return MobiStructuredBook(
                title=self.path.stem,
                author="Unknown",
                chapters=[("", ["(cannot convert MOBI — pip install mobi)"])],
            )

        try:
            mobi_dir, html_path = mobi.extract(str(self.path))
        except Exception as exc:
            print(f"[mobi] extraction failed: {exc}", file=__import__("sys").stderr)
            self._cleanup(tmpdir)
            return MobiStructuredBook(
                title=self.path.stem,
                author="Unknown",
                chapters=[("", [f"(MOBI extraction failed: {exc})"])],
            )

        try:
            # ── 2. Parse OPF metadata ────────────────────────────────
            opf_path = Path(mobi_dir) / "mobi7" / "content.opf"
            opf_meta = _parse_opf_metadata(opf_path) if opf_path.exists() else {}
            title = opf_meta.get("title", self.path.stem)
            author = opf_meta.get("creator", "Unknown")
            publisher = opf_meta.get("publisher", "")
            language = opf_meta.get("language", "zh")

            # ── 3. Parse NCX for chapter titles + filepos anchors ────
            ncx_path = Path(mobi_dir) / "mobi7" / "toc.ncx"
            ncx_entries = _parse_ncx_anchors(ncx_path) if ncx_path.exists() else []

            # ── 4. Read HTML content ─────────────────────────────────
            html_content = Path(html_path).read_text(encoding="utf-8")

            # ── 5. Split HTML by NCX filepos anchors ─────────────────
            chapters = _split_html_by_anchors(html_content, ncx_entries)

            return MobiStructuredBook(
                title=title,
                author=author,
                publisher=publisher,
                language=language,
                chapters=chapters,
            )

        finally:
            # Cleanup both temp directories
            md = Path(mobi_dir) if isinstance(mobi_dir, str) else mobi_dir
            if md and md.exists():
                shutil.rmtree(md, ignore_errors=True)
            self._cleanup(tmpdir)

    def _convert(self) -> Tuple[Optional[Path], Optional[Path]]:
        """Convert MOBI → plain text. Returns (txt_path, temp_dir)."""
        tmpdir = Path(tempfile.mkdtemp(prefix="mobi_reader_"))
        txt_path = tmpdir / "output.txt"

        # Method 1: Python mobi library (direct import — no subprocess)
        try:
            import mobi

            mobi_dir, html_path = mobi.extract(str(self.path))
            try:
                content = Path(html_path).read_text(encoding="utf-8")
                txt_path.write_text(content, encoding="utf-8")
                return txt_path, tmpdir
            finally:
                # Cleanup mobi-created temp directory
                md = Path(mobi_dir) if isinstance(mobi_dir, str) else mobi_dir
                if md and md.exists():
                    shutil.rmtree(md, ignore_errors=True)
        except ImportError:
            pass
        except Exception as exc:
            print(f"[mobi] direct extraction failed: {exc}", file=__import__("sys").stderr)

        # Method 2: Calibre ebook-convert
        calibre = _find_exe("ebook-convert")
        if calibre:
            result = subprocess.run(
                [str(calibre), str(self.path), str(txt_path)],
                capture_output=True, timeout=60,
            )
            if result.returncode == 0 and txt_path.exists():
                return txt_path, tmpdir

        # Nothing worked
        if tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)
        return None, None

    @staticmethod
    def _cleanup(tmpdir: Optional[Path]) -> None:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Shared helpers ─────────────────────────────────────────────────────────


def _find_exe(name: str) -> Optional[Path]:
    """Locate an executable in PATH."""
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / name
        if candidate.exists():
            return candidate
        candidate_win = Path(p) / f"{name}.exe"
        if candidate_win.exists():
            return candidate_win
        candidate_bat = Path(p) / f"{name}.bat"
        if candidate_bat.exists():
            return candidate_bat
    return None


# ── Unified factory ────────────────────────────────────────────────────────


def open_book(path: str | Path) -> EpubReader | MobiReader:
    """Open an EPUB or MOBI file and return the appropriate reader."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Book not found: {path}")
    ext = path.suffix.lower()
    if ext == ".epub":
        return EpubReader(path)
    elif ext == ".mobi":
        return MobiReader(path)
    else:
        raise ValueError(f"Unsupported format: {ext} (expected .epub or .mobi)")
