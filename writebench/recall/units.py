"""从 books.db 与 graph_*.db 取出可检索单元。

三类单元：原文块（chunk）、图谱观察（observation）、逐字引文（quote）。
图谱里的观察和引文比整块原文更短、更贴「可转写的细节」，所以单独成单元。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
BOOKS_DB = ROOT / "data" / "books.db"
GRAPH_DIR = ROOT / "data" / "graphs"
CASE_DIR = ROOT / "writebench" / "cases"

#: 提取器写的旁注不是人物素材。
META_MARKS = ("这一说法", "转述", "存疑")
MIN_CHARS = 40


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def case_books() -> dict[str, dict[str, str]]:
    """用例里显式给出的 stable_book_id → book_id 映射。"""
    out: dict[str, dict[str, str]] = {}
    for path in sorted(CASE_DIR.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        for book in (case.get("references") or {}).get("books") or []:
            sid = str(book.get("stable_book_id") or "")
            bid = str(book.get("book_id") or "")
            if sid and bid:
                out[sid] = {"stable_book_id": sid, "book_id": bid,
                            "label": str(book.get("label") or "")}
    return out


def book_map(graph_dir: Path = GRAPH_DIR) -> dict[str, str]:
    """book_id → stable_book_id：graph_chunks 里有 book_id，文件名里有 stable。"""
    mapping: dict[str, str] = {}
    for sid, info in case_books().items():
        mapping[info["book_id"]] = sid
    for path in sorted(graph_dir.glob("graph_*.db")):
        sid = path.stem[len("graph_"):]
        try:
            conn = _ro(path)
            rows = conn.execute(
                "SELECT DISTINCT book_id FROM graph_chunks").fetchall()
            conn.close()
        except sqlite3.Error:
            continue
        for row in rows:
            if row["book_id"]:
                mapping.setdefault(str(row["book_id"]), sid)
    return mapping


def _unit(book: str, kind: str, ref: str, chapter: int,
          text: str, title: str = "") -> dict[str, Any]:
    return {"uid": f"{kind}:{book}:{ref}", "book": book, "kind": kind,
            "chapter": int(chapter or 0), "ref": str(ref),
            "text": text[:4000], "title": title}


def _dedup_key(kind: str, book: str, text: str) -> str:
    return f"{kind}:{book}:{hashlib.sha1(text.encode()).hexdigest()[:16]}"


def iter_units(graph_dir: Path = GRAPH_DIR,
               books_db: Path = BOOKS_DB) -> Iterator[dict[str, Any]]:
    """按书、按类型逐条产出。同一本书里文字完全相同的单元只留一条。"""
    mapping = book_map(graph_dir)
    titles: dict[str, str] = {}
    seen: set[str] = set()
    conn = _ro(books_db)
    try:
        for row in conn.execute("SELECT id, title FROM books"):
            titles[str(row["id"])] = str(row["title"] or "")
        for row in conn.execute(
                "SELECT id, book_id, chapter_index, content FROM book_chunks"
                " WHERE char_count>=?", (MIN_CHARS,)):
            text = _norm(row["content"])
            if len(text) < MIN_CHARS:
                continue
            book = mapping.get(str(row["book_id"]), str(row["book_id"]))
            key = _dedup_key("chunk", book, text)
            if key in seen:
                continue
            seen.add(key)
            yield _unit(book, "chunk", row["id"], row["chapter_index"], text,
                        titles.get(str(row["book_id"]), ""))
    finally:
        conn.close()

    for path in sorted(graph_dir.glob("graph_*.db")):
        book = path.stem[len("graph_"):]
        try:
            conn = _ro(path)
        except sqlite3.Error:
            continue
        try:
            for row in conn.execute(
                    "SELECT id, value, evidence_text, anchor_chapter"
                    " FROM graph_observations"):
                value = _norm(row["value"])
                if len(value) < 20 or any(m in value for m in META_MARKS):
                    continue
                text = _norm(f"{value} {row['evidence_text'] or ''}")
                key = _dedup_key("observation", book, text)
                if key in seen:
                    continue
                seen.add(key)
                yield _unit(book, "observation", row["id"],
                            row["anchor_chapter"], text)
            for row in conn.execute(
                    "SELECT q.id, q.quote_text, q.scene_context,"
                    " c.chapter_index FROM graph_quotes q"
                    " LEFT JOIN graph_chunks c ON c.chunk_id=q.chunk_id"):
                quote = _norm(row["quote_text"])
                if len(quote) < 12:
                    continue
                text = _norm(f"{row['scene_context'] or ''} {quote}")
                key = _dedup_key("quote", book, text)
                if key in seen:
                    continue
                seen.add(key)
                yield _unit(book, "quote", row["id"],
                            row["chapter_index"], text)
        finally:
            conn.close()
