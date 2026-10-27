"""声音采集的可见性闸 + 候选块 —— **正文可以看，但只产声音，不产事实**。

与 ``authorgraph/scope.py``（序跋采集）是两套闸，不是松紧不同：

| 链路 | 能看见 | 能产出 |
|---|---|---|
| 事实采集 | 序跋 | ``author_records`` |
| 声音采集 | 正文（按 position） | ``author_corpus`` + ``author_voice_traits`` |

``position`` 决定正文用不用得上（设计稿 04 §二）：
``narrator``/``avatar`` 用，``editor``/``recorder``/``absent``/``unknown``
不用 —— 后四种书里的正文**不是作者写的**，拿它提炼文风是把译者/记录对象
的风格记到作者头上。这不是保守，是**错误方向不可逆**。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from bookgraph.selection import select_chunks

#: 少于这个字数的块不采样 —— 判断不出句法节奏，只会给模型噪声。
MIN_SAMPLE_CHARS = 80

#: 单块送入模型的上限（设计稿 08 §四.3：每段 ≤500 字）。
MAX_WINDOW_CHARS = 500


@dataclass(frozen=True)
class Visibility:
    body: bool
    speaker: str          # 落 ``author_corpus.speaker``
    domain: str           # ``fiction`` / ``nonfiction`` / 其它
    reason: str


#: position → 能不能用正文。**判不准就不冒进**（unknown 不用）。
VISIBILITY: dict[str, Visibility] = {
    "narrator": Visibility(
        True, "author", "nonfiction",
        "作者本人就是叙述者（非虚构/回忆录），正文可作声音"),
    "avatar": Visibility(
        True, "via_character", "fiction",
        "作者借角色说话（自传体/第一人称小说）—— 口吻可用，经历不可用"),
    "editor": Visibility(
        False, "editor", "paratext",
        "作者是整理者/解说者，正文是别人的文本"),
    "recorder": Visibility(
        False, "author", "paratext",
        "作者记录别人的话，正文不是作者的声音"),
    "absent": Visibility(
        False, "", "public", "作者不在这本书里，正文不可用"),
    "unknown": Visibility(
        False, "", "unknown", "位置没判出来 —— 判不准就不采样正文"),
}


def visibility_for(position: str | None) -> Visibility:
    return VISIBILITY.get((position or "").strip().lower(),
                          VISIBILITY["unknown"])


_WS = re.compile(r"\s+")

#: 脚注/注释标记：``(1)``、``（12）``。**只认 1–3 位数字** ——
#: ``(1984)`` 是年份，不是注号。实测《房思琪》的注释块 571 字里 5 个注号，
#: 模型读完直接报「这不是叙事文本」；抽样阶段就该拦住，别浪费模型注意力。
_FOOTNOTE = re.compile(r"[（(]\s*\d{1,3}\s*[)）]")
#: 一块里注号达到这个数，判为「正文混注释」，整块不采。
MAX_FOOTNOTE_MARKS = 3

#: 目录页识别：开头就是「Contents / 目录」，或大量短行 + 多个章序号。
_TOC_HEAD = re.compile(r"^\s*(?:contents|目录|目\s*录)\s*$", re.I | re.M)
_CHAPTER_MARK = re.compile(
    r"第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章回节篇部卷讲集]")


def looks_like_toc(text: str) -> bool:
    """目录页 —— 正文块里夹着的目录（实测《当下的力量》第一章第一块）。

    只认两种强信号，不碰「短行都算」：诗也是短行，不能误杀。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    if any(_TOC_HEAD.match(ln) for ln in lines[:3]):
        return True
    if len(lines) >= 8:
        short = sum(1 for ln in lines if len(ln) <= 24)
        marks = sum(1 for ln in lines if _CHAPTER_MARK.search(ln))
        if short / len(lines) >= 0.6 and marks >= 3:
            return True
    return False


def norm_text(text: str) -> str:
    """逐字比较用的归一：只抹空白，**不动标点/全半角**（存储保持原文）。"""
    return _WS.sub("", text or "")


def _sketch(text: str, *, n: int = 5, k: int = 3) -> tuple[int, ...]:
    """5-gram bottom-k 指纹 —— 近似重复段落去重用。

    不做两两 Jaccard：一本书上千块，O(n²) 的集合运算比读一遍书还贵。
    取哈希最小的三个 shingle 作签名，**相同的段落必然同签名**，
    近似重复大概率同签名；漏掉的近似重复只是多喂一段，不会写错数据。
    """
    s = norm_text(text)
    if len(s) < n:
        return ()
    hs = sorted(int(hashlib.sha1(s[i:i + n].encode("utf-8")).hexdigest()[:8], 16)
                for i in range(len(s) - n + 1))
    return tuple(hs[:k])


@dataclass(frozen=True)
class ChunkView:
    chunk_id: str
    chapter_index: int
    chunk_index: int
    chapter_title: str
    chapter_type: str
    text: str
    band: str                 # front / middle / back
    paragraph_offsets: tuple[tuple[int, int], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"chunk_id": self.chunk_id,
                "chapter_index": self.chapter_index,
                "chunk_index": self.chunk_index,
                "chapter_title": self.chapter_title,
                "chapter_type": self.chapter_type,
                "text": self.text, "band": self.band}


def _paragraph_offsets(raw: str) -> tuple[tuple[int, int], ...]:
    """``paragraph_offsets`` 是库里的 JSON，坏了就当没有（逐字窗口仍可用）。"""
    if not raw:
        return ()
    try:
        data = json.loads(raw)
        out = []
        for item in data if isinstance(data, list) else []:
            if (isinstance(item, (list, tuple)) and len(item) == 2
                    and all(isinstance(x, int) for x in item) and item[0] >= 0):
                out.append((item[0], item[1]))
        return tuple(out)
    except Exception:                                   # noqa: BLE001
        return ()


def eligible_chunks(books_db: Any, book_id: str, *,
                    min_chars: int = MIN_SAMPLE_CHARS,
                    boundaries: dict[str, str] | None = None,
                    scout_excluded: set[str] | None = None
                    ) -> tuple[list[ChunkView], list[str]]:
    """可用于声音采样的正文块。返回 ``(chunks, warnings)``。

    三份「这块是什么」的判断一次用全（与 ``bookgraph/selection.py`` 同源）：

    - ``chapter_type``（章级体例）
    - ``head_scan.boundaries``（块级：copyright/promo/toc）
    - Scout 的 ``excluded_chunk_ids``

    再加声音层自己的筛子：过短、逐字/近似重复、正文混注释、块内目录页。
    序跋由 ``authorgraph/scope.py`` 单独采，不在这里重复。
    """
    warnings: list[str] = []
    chapters = {c.chapter_index: c for c in books_db.get_chapters(book_id)}
    rows = books_db.get_chunks(book_id)
    if not rows:
        return [], ["这本书没有正文块"]
    types = {ci: (getattr(c, "chapter_type", "") or "body")
             for ci, c in chapters.items()}
    sel = select_chunks(rows, chapter_types=types,
                        boundaries=boundaries or {},
                        scout_excluded=scout_excluded or set())
    if sel.drop or sel.conflicts:
        warnings.append("提取裁剪：" + sel.summary())
    by_id = {r.id: r for r in rows}
    kept: list[tuple[Any, Any]] = []
    for cid in sel.keep:
        r = by_id.get(cid)
        if r is None:
            continue
        ch = chapters.get(r.chapter_index)
        text = r.content or ""
        if len(norm_text(text)) < min_chars:
            continue
        kept.append((r, ch))

    total = len(kept)
    out: list[ChunkView] = []
    seen_hash: set[str] = set()
    seen_sketch: set[tuple[int, ...]] = set()
    dropped_dup = dropped_note = dropped_toc = 0
    for i, (r, ch) in enumerate(kept):
        text = r.content or ""
        if looks_like_toc(text):
            dropped_toc += 1
            continue
        if len(_FOOTNOTE.findall(text)) >= MAX_FOOTNOTE_MARKS:
            dropped_note += 1
            continue
        h = hashlib.sha1(norm_text(text).encode("utf-8")).hexdigest()
        sig = _sketch(text)
        if h in seen_hash or (sig and sig in seen_sketch):
            dropped_dup += 1
            continue
        seen_hash.add(h)
        if sig:
            seen_sketch.add(sig)
        share = (i + 0.5) / max(total, 1)
        band = "front" if share <= 0.2 else ("back" if share > 0.8 else "middle")
        out.append(ChunkView(
            chunk_id=r.id, chapter_index=r.chapter_index,
            chunk_index=r.chunk_index,
            chapter_title=(getattr(ch, "title", "") or "") if ch else "",
            chapter_type=(getattr(ch, "chapter_type", "") or "body") if ch else "body",
            text=text, band=band,
            paragraph_offsets=_paragraph_offsets(
                getattr(r, "paragraph_offsets", "") or "")))
    if dropped_dup:
        warnings.append(f"去掉重复段落 {dropped_dup} 块（逐字或近似重复）")
    if dropped_note:
        warnings.append(f"去掉正文混注释的块 {dropped_note} 块"
                        f"（注号 ≥{MAX_FOOTNOTE_MARKS}）")
    if dropped_toc:
        warnings.append(f"去掉块内目录页 {dropped_toc} 块")
    if not out:
        warnings.append("过滤后没有可采样的正文块")
    return out, warnings


def content_hash(chunks: list[ChunkView], stable_book_id: str = "") -> str:
    """本书参与声音的 chunk 的内容指纹 —— 重导/改正文后它一定变。

    **只算内容，不算采样参数**：采样参数变了不该让旧声音失效。
    """
    parts = [f"{stable_book_id}|{c.chunk_id}|"
             f"{hashlib.sha1(norm_text(c.text).encode('utf-8')).hexdigest()}"
             for c in chunks]
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()[:32]


__all__ = ["ChunkView", "MAX_WINDOW_CHARS", "MIN_SAMPLE_CHARS", "VISIBILITY",
           "Visibility", "content_hash", "eligible_chunks", "norm_text",
           "visibility_for"]
