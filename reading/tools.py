"""阅读会话的只读工具。

**剧透门在工具层**：``search_book`` / ``read_passage`` / ``verify_quote``
只能看 ``chapter_index <= current_chapter``，未来的章一律拒绝。模型没法
靠「搜一下后面的情节」越界。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from storage.db import BookDB
from storage.models import BookChapterRow


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@dataclass
class ReadingTools:
    db: BookDB
    book_id: str
    chapters: dict[int, BookChapterRow]
    current_chapter: int = 0
    coverage_fn: Callable[[], dict[str, Any]] | None = None
    calls: dict[str, int] = field(default_factory=dict)

    # ── schema ────────────────────────────────────────────────────────────

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {
                "name": "search_book",
                "description": ("在**已读范围**内做全文检索，返回章/块与片段。"
                                "limit 建议 6，最多 12。"),
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "检索词，>=2 字"},
                    "limit": {"type": "integer",
                              "description": "返回条数，默认 6，最大 12"},
                }, "required": ["query"]}}},
            {"type": "function", "function": {
                "name": "read_passage",
                "description": ("读已读章节的原文。chapter 必填；chunk 可选，"
                                "不给则返回整章（过长时按块列出）。"),
                "parameters": {"type": "object", "properties": {
                    "chapter": {"type": "integer"},
                    "chunk": {"type": "integer"},
                }, "required": ["chapter"]}}},
            {"type": "function", "function": {
                "name": "verify_quote",
                "description": ("逐字核对一句引文是否在已读原文里。找不到就"
                                "不要引用；不要用改写后的句子来试探。"),
                "parameters": {"type": "object", "properties": {
                    "text": {"type": "string", "description": "待核对的原句"},
                }, "required": ["text"]}}},
            {"type": "function", "function": {
                "name": "coverage",
                "description": ("看当前已读进度与笔记覆盖（机械统计）。"
                                "不要每章都调；只有不确定读到哪时才用。"),
                "parameters": {"type": "object", "properties": {}}}},
        ]

    # ── dispatch ──────────────────────────────────────────────────────────

    def execute(self, name: str, args: dict[str, Any] | None = None) -> str:
        self.calls[name] = self.calls.get(name, 0) + 1
        args = args or {}
        try:
            if name == "search_book":
                return self._search(args)
            if name == "read_passage":
                return self._read(args)
            if name == "verify_quote":
                return self._verify(args)
            if name == "coverage":
                if self.coverage_fn:
                    return _j(self.coverage_fn())
                return _j({"read_through": self.current_chapter,
                           "noted": []})
            return _j({"error": f"未知工具 {name}"})
        except Exception as exc:                              # noqa: BLE001
            return _j({"error": f"{type(exc).__name__}: {exc}"})

    def _search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if len(query) < 2:
            return _j({"error": "query 至少 2 个字"})
        limit = max(1, min(int(args.get("limit") or 6), 12))
        rows = self.db.search_books(
            query, limit=limit * 3, book_id=self.book_id)
        out, seen = [], set()
        for r in rows or []:
            ch = int(r.get("chapter_index") or 0)
            ck = int(r.get("chunk_index") or 0)
            if ch > self.current_chapter or (ch, ck) in seen:
                continue
            seen.add((ch, ck))
            out.append({"chapter": ch, "chunk": ck,
                        "title": r.get("chapter_title") or "",
                        "snippet": r.get("snippet") or ""})
            if len(out) >= limit:
                break
        note = "" if out else (
            "已读范围内没有命中。不要用后文或全书知识补全；"
            "只写本章实际出现的称呼与事实。")
        return _j({"query": query, "read_through": self.current_chapter,
                   "results": out, "note": note})

    def _read(self, args: dict[str, Any]) -> str:
        ch = int(args.get("chapter") if args.get("chapter") is not None
                 else -1)
        if ch < 0 or ch > self.current_chapter:
            return _j({"error": f"第 {ch} 章不在已读范围"
                                f"（1–{self.current_chapter}）"})
        chapter = self.chapters.get(ch)
        title = chapter.title if chapter else ""
        rows = self.db.get_chunks(self.book_id, ch)
        if not rows:
            return _j({"error": f"第 {ch} 章没有正文"})
        ck = args.get("chunk")
        if ck is not None:
            ck = int(ck)
            hit = next((r for r in rows if r.chunk_index == ck), None)
            if hit is None:
                return _j({"error": f"第 {ch} 章没有第 {ck} 块",
                           "chunks": [r.chunk_index for r in rows]})
            return _j({"chapter": ch, "chunk": ck, "title": title,
                       "text": hit.content})
        full = "\n".join(r.content for r in rows)
        if len(full) <= 8_000:
            return _j({"chapter": ch, "title": title, "text": full})
        return _j({"chapter": ch, "title": title,
                   "note": "整章过长，按 chunk 读",
                   "chunks": [{"chunk": r.chunk_index,
                               "chars": len(r.content),
                               "preview": _norm(r.content)[:60]}
                              for r in rows]})

    def _verify(self, args: dict[str, Any]) -> str:
        needle = _norm(str(args.get("text") or ""))
        if len(needle) < 4:
            return _j({"error": "引文至少 4 个字；太短无法核对"})
        rows = self.db.get_chunks(self.book_id)
        hits: list[dict[str, Any]] = []
        for r in reversed(rows):
            if int(r.chapter_index) > self.current_chapter:
                continue
            body = _norm(r.content)
            pos = body.find(needle)
            if pos < 0:
                continue
            hits.append({
                "chapter": int(r.chapter_index),
                "chunk": int(r.chunk_index),
                "context": body[max(0, pos - 40):pos + len(needle) + 40],
            })
            if len(hits) >= 3:
                break
        return _j({"found": bool(hits), "quote": needle, "hits": hits,
                   "note": "" if hits else "逐字未找到；不要引用这句"})

    def verify_text(self, text: str) -> dict[str, Any]:
        """写笔记时的机械校验入口：不经过模型，不计数。"""
        return json.loads(self._verify({"text": text}))


