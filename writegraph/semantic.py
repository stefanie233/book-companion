"""书块语义召回（决策 148）：向量近邻 + 回 books.db 取原文。

与 ``reference.search_library`` 的分工：

- 关键词检索（FTS/LIKE）命中**字面**出现的词，适合行话、物象、制度名；
- 语义检索命中**意思相近**的段落，适合「我需要一种说不出口的愧疚感」
  这类没有关键词的意图；
- 两条路都只借写法与细节，结果都是候选卡，不改变素材分级。

索引与查询必须同一模型：向量空间不可跨模型比较。索引里没有当前绑定模型的
向量时，这里返回空结果并说明，不拿旧模型的向量凑数。
"""

from __future__ import annotations

import sqlite3
from array import array
from pathlib import Path
from typing import Any, Iterable, Sequence

from services import embedding_service as embed_mod
from storage.embedding_db import (
    EmbeddingDB,
    resolve_embedding_db_path,
)
from writegraph import reference

try:                                            # pragma: no cover
    import numpy as np
except Exception:                               # pragma: no cover
    np = None

#: 余弦阈值：低于它的段落语义上已经没关系，宁缺勿滥。
MIN_SCORE = 0.25


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip("，。；、 ") + "…"


def _decode(blob: bytes) -> array:
    vec = array("f")
    vec.frombytes(blob)
    return vec


def rank(rows: Sequence[dict[str, Any]], query: Sequence[float],
         *, limit: int, min_score: float = MIN_SCORE) -> list[tuple[str, float]]:
    """余弦排序（向量已归一化，点积即可）。返回 ``(chunk_id, score)``。"""
    dim = len(query)
    usable = [r for r in rows if int(r.get("dim") or 0) == dim]
    if not usable:
        return []
    if np is not None:
        mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32)
                         for r in usable])
        scores = mat @ np.asarray(query, dtype=np.float32)
        order = np.argsort(-scores)[:max(1, int(limit))]
        out = []
        for i in order:
            score = float(scores[i])
            if score >= min_score:
                out.append((str(usable[int(i)]["chunk_id"]), score))
        return out
    scored = []
    for row in usable:
        score = sum(x * y for x, y in zip(_decode(row["vec"]), query))
        if score >= min_score:
            scored.append((str(row["chunk_id"]), float(score)))
    scored.sort(key=lambda pair: -pair[1])
    return scored[:max(1, int(limit))]


def _chunks(books_db: Path, chunk_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not chunk_ids or not Path(books_db).exists():
        return {}
    marks = ",".join("?" * len(chunk_ids))
    conn = _ro(Path(books_db))
    try:
        rows = conn.execute(
            "SELECT ck.id, ck.book_id, b.title AS book_title,"
            " ck.chapter_index, ch.title AS chapter_title,"
            " ck.chunk_index, ck.content"
            " FROM book_chunks ck"
            " JOIN books b ON b.id = ck.book_id"
            " LEFT JOIN book_chapters ch"
            "   ON ch.book_id = ck.book_id"
            "  AND ch.chapter_index = ck.chapter_index"
            f" WHERE ck.id IN ({marks})",
            tuple(chunk_ids)).fetchall()
    finally:
        conn.close()
    return {str(row["id"]): dict(row) for row in rows}


def cards(books_db: Path, hits: Sequence[tuple[str, float]], *,
          limit: int = 6, per_book: int = 2,
          terms: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """把 ``(chunk_id, score)`` 变成与关键词检索同形的候选卡。

    ``terms`` 是查询里抽出的词；给了就取命中窗口，而不是只截块头——语义
    命中常常落在块后半段，只给开头会让模型误判「没有这段」。
    """
    found = _chunks(books_db, [cid for cid, _ in hits])
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for chunk_id, score in hits:
        row = found.get(chunk_id)
        if row is None:                 # 删书/重导留下的孤儿向量
            continue
        book_id = str(row.get("book_id") or "")
        if seen.get(book_id, 0) >= per_book:
            continue
        seen[book_id] = seen.get(book_id, 0) + 1
        content = str(row.get("content") or "")
        matched = [str(t) for t in (terms or []) if t and str(t) in content]
        out.append({
            "chunk_id": chunk_id,
            "book_id": book_id,
            "book_title": str(row.get("book_title") or ""),
            "chapter_index": int(row.get("chapter_index") or 0),
            "chapter_title": str(row.get("chapter_title") or ""),
            "chunk_index": int(row.get("chunk_index") or 0),
            "excerpt": (reference._match_window(content, terms, 240)
                        if terms else _clean(content, 200)),
            "matched_terms": matched,
            "score": round(float(score), 4),
            "kind": "语义近邻",
            "caution": "只借写法与细节；不搬专名、设定、情节",
        })
        if len(out) >= max(1, int(limit)):
            break
    return out


async def search(config: Any, query: str, *,
                 limit: int = 6, books_db: Path | None = None,
                 embedding_db: Path | None = None,
                 book_ids: Iterable[str] | None = None,
                 min_score: float = MIN_SCORE) -> dict[str, Any]:
    """语义检索：返回与 ``reference.search_library`` 同形的素材卡。"""
    query = str(query or "").strip()
    if not query:
        raise embed_mod.EmbeddingError("检索词不能为空")
    db_path = Path(embedding_db or resolve_embedding_db_path(config))
    books_path = Path(books_db) if books_db else None
    if not db_path.exists():
        # 没建索引时不该先去碰 embedding 端点：连绑定都可能还没配。
        return {
            "source": "library", "recall": "semantic", "model": "",
            "cards": [], "count": 0, "index_chunks": 0,
            "note": "还没有语义索引：先跑 python scripts/build_embedding_index.py"
                    "（可加 --book 先索引几本）；关键词检索仍可用。",
        }
    ep = embed_mod.resolve_endpoint(config)
    db = EmbeddingDB(db_path)
    rows = db.vectors(ep.model, book_ids)
    stats = db.stats(ep.model)
    if not rows:
        return {
            "source": "library", "recall": "semantic", "model": ep.model,
            "cards": [], "count": 0, "index_chunks": 0,
            "note": f"索引里没有 {ep.model} 的向量（绑定模型换了？）；"
                    "先重跑 scripts/build_embedding_index.py。",
        }
    query_vec = await embed_mod.embed_one(config, query, model=ep.model)
    # 多取一些候选：同一本书容易连续占满 top-k，按书封顶后才够凑满 limit。
    hits = rank(rows, query_vec, limit=max(24, limit * 6),
                min_score=min_score)
    terms = reference.query_terms(query, limit=8)
    found = (cards(books_path, hits, limit=limit, terms=terms)
             if books_path else [])
    return {
        "source": "library", "recall": "semantic", "model": ep.model,
        "cards": found, "count": len(found),
        "index_chunks": stats["chunks"], "index_books": stats["books"],
        "note": (f"语义近邻（余弦 ≥ {min_score}），索引覆盖 {stats['books']} 本 / "
                 f"{stats['chunks']} 块；只借写法与细节。"
                 if found else
                 "语义索引里没有足够接近的段落；换个说法，或补索引。"),
    }
