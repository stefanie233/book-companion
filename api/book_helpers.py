"""Book lookup / page-navigation helpers shared by sync, books and annotations."""
from __future__ import annotations

import re
from pathlib import Path


def _get_stable_id(db, book_id: str) -> str | None:
    """Resolve stable_book_id for a book, or None if no import_log."""
    log = db.get_import_log_by_book(book_id)
    return log.stable_book_id if log else None


# ── Viewport passage（阅读页聊天注入）───────────────────────────────────

_WS_RE = re.compile(r"\s+")


def _norm_ws(s: str) -> str:
    return _WS_RE.sub(" ", s or "").strip()


def _vp_raw_block(text: str, ch_hint: int, ck_hint: int,
                  total: int | None, note: str = "") -> str:
    """对齐失败时的兜底：直接把视窗文字交给模型，并声明边界未知。"""
    pos = f"第 {ck_hint + 1}/{total} 块" if total else f"第 {ck_hint + 1} 块"
    note = note or (
        "无法把这段文字对齐到数据库块（书可能被重导或文字有改动），"
        f"块边界未知；记录中的阅读位置是{pos}。"
    )
    return (
        f'<current-passage source="viewport" chapter_index="{ch_hint}" '
        f'chunk_index="{ck_hint}">\n'
        "  视窗文字（读者屏幕上实际渲染的内容，按阅读顺序；首尾行已按视口裁切）:\n"
        f"  {text}\n"
        f"  注：{note}\n"
        "</current-passage>"
    )


def build_viewport_passage(db, book_id: str, viewport, *,
                           position: dict | None = None,
                           visible_chapters=None,
                           max_chars: int = 3000) -> str | None:
    """把前端「视口内实际渲染的文字」装成 <current-passage> 块。

    与旧实现（取 (chapter, chunk) 那一块的正文）的区别：视窗可能横跨多个
    chunk、甚至多章，单块根本盖不住。这里把可见章的 chunk 文本按顺序拼成
    一条规范化串，用视窗文字的开头做定位，再把 chunk 边界映射回视窗文字
    上，逐段标注「第几块 / 块内约百分之几起止」。

    对齐失败（重导、改字、跨章太远）不降级回单块——那就又回到「模型看到
    的比读者少」的老问题；改为原样送出视窗文字并注明边界未知。

    返回 None 表示没有可用的视窗文字，调用方退回旧的 chunk 注入。
    """
    text = _norm_ws(str((viewport or {}).get("text") or ""))
    if not text:
        return None
    text = text[:max_chars]
    pos = position or {}
    try:
        ch_hint = int(pos.get("chapter", 0) or 0)
    except (TypeError, ValueError):
        ch_hint = 0
    try:
        ck_hint = int(pos.get("chunk", 0) or 0)
    except (TypeError, ValueError):
        ck_hint = 0

    # 候选章：视口可见章 + 阅读进度章，按章序排。只保留真实存在的章。
    chapters: list[int] = []
    for x in (visible_chapters or []):
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v not in chapters and db.get_chapter(book_id, v):
            chapters.append(v)
    if db.get_chapter(book_id, ch_hint) and ch_hint not in chapters:
        chapters.append(ch_hint)
    chapters.sort()
    if not chapters:
        return _vp_raw_block(text, ch_hint, ck_hint, None)

    # 拼接各章 chunk 的规范化文本，记住每块的区间。视口通常只覆盖 1–3 章，
    # 不做整本的字符级映射。
    def _hay_for(chs: list[int]):
        parts: list[str] = []
        rows: list[dict] = []
        joined = 0
        for ch in chs:
            row_ch = db.get_chapter(book_id, ch)
            title = (row_ch.title or "") if row_ch else ""
            total = db.get_chunk_count(book_id, ch)
            for ck in range(total):
                if joined > 600_000:        # 病态巨章保护：对齐失败就走兜底
                    break
                ckrow = db.get_chunk(book_id, ch, ck)
                if not ckrow:
                    continue
                norm = _norm_ws(ckrow.content)
                if not norm:
                    continue
                st = joined
                rows.append({
                    "chapter": ch, "chunk": ck, "total": total,
                    "start": st, "end": st + len(norm),
                    "id": ckrow.id, "title": title,
                })
                parts.append(norm)
                joined = st + len(norm) + 1   # 与 " ".join 的间隙一致
        return " ".join(parts), rows

    def _locate(hay: str, rows: list[dict]) -> int:
        hint_pos = 0
        for c in rows:
            if c["chapter"] == ch_hint and c["chunk"] == ck_hint:
                hint_pos = c["start"]
                break
        # 先用最长前缀在 hint 附近找，再放宽窗口/缩短前缀。
        for take in (80, 48, 32, 24):
            needle = text[:take]
            if not needle:
                break
            found = hay.find(needle, max(0, hint_pos - 30000))
            if found < 0:
                found = hay.find(needle)
            if found >= 0:
                return found
        return -1

    hay, chunks = _hay_for(chapters)
    if not chunks:
        return _vp_raw_block(text, ch_hint, ck_hint, None)
    start = _locate(hay, chunks)
    # section→chapter 的结构映射不完全可信（部分书的 spine_index 与渲染器
    # 差位：实测《窄门》渲染 section 21 落的是一章 index 20 的正文）。首轮
    # 没命中就以候选章为种子向外扩两圈（先 ±1 再 ±2）——视窗文字本身才是
    # 权威坐标。只扩邻近章，不在 min..max 之间整段扫。
    if start < 0:
        for radius in (1, 2):
            seeds = sorted(set(chapters + [ch_hint]))
            wide: list[int] = []
            for base in seeds:
                for d in range(-radius, radius + 1):
                    c = base + d
                    if (c < 0 or c in chapters or c in wide
                            or not db.get_chapter(book_id, c)):
                        continue
                    wide.append(c)
            if not wide:
                continue
            chapters = sorted(chapters + wide)
            hay, chunks = _hay_for(chapters)
            start = _locate(hay, chunks) if chunks else -1
            if start >= 0:
                break
    if start < 0:
        return _vp_raw_block(text, ch_hint, ck_hint, None)

    end = min(len(hay), start + len(text))
    covered = [c for c in chunks if c["start"] < end and c["end"] > start]
    if not covered:
        return _vp_raw_block(text, ch_hint, ck_hint, None)

    first, last = covered[0], covered[-1]
    n = len(covered)

    def _pct(v: int, c: dict) -> int:
        span = max(1, c["end"] - c["start"])
        return max(0, min(100, round(v * 100 / span)))

    pct_start = _pct(max(start, first["start"]) - first["start"], first)
    pct_end = _pct(min(end, last["end"]) - last["start"], last)

    def _seg(c: dict) -> str:
        a = max(0, max(start, c["start"]) - start)
        b = max(0, min(end, c["end"]) - start)
        return text[a:min(b, len(text))].strip()

    def _label(c: dict) -> str:
        lab = f"第 {c['chunk'] + 1}/{c['total']} 块"
        if c["title"]:
            lab += f" · {c['title']}"
        return lab

    # 坐标取「视窗起点所在块」（而不是记录里的进度块）：视窗跨块时，
    # 读者真正在看的是这一块起头的内容，agent 用 library_read 顺着读也对。
    lines = [
        f'<current-passage source="viewport" chapter_index="{first["chapter"]}" '
        f'chunk_index="{first["chunk"]}" chunk_id="{first["id"]}">',
        "  视窗文字（读者屏幕上实际渲染的内容，按阅读顺序；首尾行已按视口裁切）:",
    ]
    if n == 1:
        lines.append(
            f"  视窗覆盖：{_label(first)}（块内约 {pct_start}%–{pct_end}%），"
            "完全落在这一块内")
        seg = _seg(first)
        if seg:
            lines.append(f"  {seg}")
    else:
        span_desc = (
            f"{_label(first)}（块内约 {pct_start}% 处起） → "
            f"{_label(last)}（块内约 {pct_end}% 处止），共跨 {n} 块"
        )
        if first["chapter"] != last["chapter"]:
            span_desc += f"，跨 {len({c['chapter'] for c in covered})} 章"
        lines.append(f"  视窗覆盖：{span_desc}")
        for c in covered:
            lab = _label(c)
            if c is first:
                lab += f"（约 {pct_start}% 处起）"
            elif c is last:
                lab += f"（到约 {pct_end}% 处止）"
            lines.append(f"  【{lab}】")
            seg = _seg(c)
            if seg:
                lines.append(f"  {seg}")
    lines.append("</current-passage>")
    return "\n".join(lines)






# ── Chunk navigation helpers ────────────────────────────────────────────


def _find_prev_chunk(db, book_id: str, chapter: int, chunk: int) -> dict | None:
    """Find the chunk before (chapter, chunk), crossing chapter boundaries."""
    if chunk > 0:
        # Same chapter, previous chunk
        if db.get_chunk(book_id, chapter, chunk - 1):
            return {"chapter": chapter, "chunk": chunk - 1}
        return None
    # First chunk of chapter — go to previous chapter's last chunk
    if chapter <= 0:
        return None
    prev_total = db.get_chunk_count(book_id, chapter - 1)
    if prev_total == 0:
        return None
    return {"chapter": chapter - 1, "chunk": prev_total - 1}


def _find_next_chunk(db, book_id: str, chapter: int, chunk: int) -> dict | None:
    """Find the chunk after (chapter, chunk), crossing chapter boundaries."""
    total = db.get_chunk_count(book_id, chapter)
    if chunk + 1 < total:
        # Same chapter, next chunk
        return {"chapter": chapter, "chunk": chunk + 1}
    # Last chunk of chapter — go to next chapter's first chunk
    next_total = db.get_chunk_count(book_id, chapter + 1)
    if next_total == 0:
        return None
    return {"chapter": chapter + 1, "chunk": 0}


# ── Helpers ────────────────────────────────────────────────────────────


def _find_book_by_path(db, book_path: str) -> str | None:
    """Match a file path to an imported book via MD5."""
    if not book_path:
        return None
    try:
        from services.library_service import _hash_file
        book_id = _hash_file(Path(book_path))
        book = db.get_book(book_id)
        return book.id if book else None
    except Exception:
        return None


def _find_book_by_text(db, page_text: str) -> str | None:
    """Fuzzy-find a book by searching page text across all books."""
    try:
        results = db.search_books(page_text[:80], limit=1)
        return results[0]["book_id"] if results else None
    except Exception:
        return None


def _adjacent_chapter(chapters, current_idx: int, delta: int):
    """Get the previous or next chapter by index."""
    for ch in chapters:
        if ch.chapter_index == current_idx + delta:
            return ch
    return None
