"""向量 + BM25 混合索引（SQLite 存向量，numpy 暴力余弦）。

6k 段原文 + 4k 观察/引文，规模小，不需要 ANN 或独立向量库。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np

from . import EMBED_MODEL, INDEX_DIR
from . import embed as embed_mod
from .bm25 import BM25, tokenize
from .units import iter_units

DB_PATH = Path(os.environ.get(
    "WB_INDEX_PATH", str(Path(INDEX_DIR) / "index.db")))
SCHEMA = """
CREATE TABLE IF NOT EXISTS units(
  uid TEXT PRIMARY KEY, book TEXT, kind TEXT, chapter INTEGER,
  ref TEXT, title TEXT, text TEXT, tokens TEXT);
CREATE TABLE IF NOT EXISTS vectors(
  uid TEXT PRIMARY KEY, model TEXT, dim INTEGER, vec BLOB);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS units_book ON units(book, kind);
"""


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def embed_text(text: str, limit: int = 1800) -> str:
    """嵌入只取头尾：开头定场景，结尾常有落点；中间细节对主题贡献小。"""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 400] + " … " + text[-350:]


def _write_units(conn: sqlite3.Connection,
                 units: Sequence[dict[str, Any]]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO units VALUES (?,?,?,?,?,?,?,?)",
        [(u["uid"], u["book"], u["kind"], u["chapter"], u["ref"],
          u["title"], u["text"], json.dumps(tokenize(u["text"]),
                                          ensure_ascii=False))
         for u in units])
    conn.commit()


def _existing(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT uid FROM vectors")}


def build(*, limit: int | None = None, force: bool = False,
          model: str = EMBED_MODEL, batch: int = 10,
          concurrency: int = 3,
          books: set[str] | None = None) -> dict[str, Any]:
    t0 = time.time()
    units = list(iter_units())
    if books:
        units = [u for u in units if u["book"] in books]
    if limit:
        units = units[:limit]
    if force and DB_PATH.exists():
        DB_PATH.unlink()
    conn = _connect()
    _write_units(conn, units)
    have = set() if force else _existing(conn)
    todo = [u for u in units if u["uid"] not in have]
    print(f"[index] units={len(units)} need_embed={len(todo)}", flush=True)
    if todo:
        texts = [embed_text(u["text"]) for u in todo]
        done = 0

        def progress(d: int, total: int) -> None:
            nonlocal done
            if d - done >= 100 or d == total:
                done = d
                print(f"[embed] {d}/{total}", flush=True)

        def save_batch(start: int, vectors: list[list[float]]) -> None:
            rows = []
            for unit, vec in zip(todo[start:start + len(vectors)], vectors):
                arr = np.asarray(vec, dtype=np.float32)
                rows.append((unit["uid"], model, int(arr.shape[0]),
                             arr.tobytes()))
            conn.executemany(
                "INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)", rows)
            conn.commit()

        try:
            asyncio.run(embed_mod.embed_texts(
                texts, model=model, batch=batch, concurrency=concurrency,
                progress=progress, on_batch=save_batch))
        except embed_mod.EmbedError as exc:
            conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
                ("model", model), ("built_at", time.strftime(
                    "%Y-%m-%d %H:%M:%S")), ("partial", "1"),
                ("error", str(exc)[:400])])
            conn.commit()
            conn.close()
            print(f"[embed] partial: {exc}", flush=True)
            return {"model": model, "partial": "1",
                    "error": str(exc)[:400]}
    meta = {"model": model, "provider": embed_mod.PROVIDER_ID,
            "units": str(len(units)),
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": f"{time.time() - t0:.1f}"}
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                     list(meta.items()))
    conn.commit()
    conn.close()
    return meta


class Index:
    """内存索引：units 与向量矩阵同序，BM25 用同一套 token。"""

    def __init__(self, units: Sequence[dict[str, Any]],
                 tokens: Sequence[Sequence[str]], mat: np.ndarray,
                 model: str):
        self.units = list(units)
        self.tokens = list(tokens)
        self.mat = mat
        self.model = model
        self.bm25 = BM25(self.tokens)
        self.by_uid = {u["uid"]: i for i, u in enumerate(self.units)}
        self._books: dict[str, set[int]] = {}

    def allowed(self, book: str | None) -> set[int] | None:
        if not book:
            return None
        if book not in self._books:
            self._books[book] = {i for i, u in enumerate(self.units)
                                 if u["book"] == book}
        return self._books[book]

    def vector(self, qvec: Sequence[float], k: int = 10,
               book: str | None = None) -> list[tuple[int, float]]:
        scores = self.mat @ np.asarray(qvec, dtype=np.float32)
        allowed = self.allowed(book)
        if allowed is None:
            order = np.argsort(-scores)[:k]
        else:
            ids = np.fromiter(allowed, dtype=np.int64)
            order = ids[np.argsort(-scores[ids])[:k]]
        return [(int(i), float(scores[i])) for i in order]

    def lexical(self, text: str, k: int = 10,
                book: str | None = None) -> list[tuple[int, float]]:
        hits = self.bm25.search(tokenize(text), k=k,
                                allowed=self.allowed(book))
        return [(int(i), float(s)) for i, s in hits]

    def hybrid(self, text: str, qvec: Sequence[float], k: int = 10,
               book: str | None = None, *, rrf: int = 60,
               pool: int = 50, bm_text: str | None = None,
               vec_weight: float = 3.0,
               lex_weight: float = 1.0) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for rank, (idx, _) in enumerate(self.vector(qvec, pool, book)):
            scores[idx] = scores.get(idx, 0.0) + vec_weight / (rrf + rank + 1)
        lexical = self.lexical(bm_text or text, pool, book)
        for rank, (idx, _) in enumerate(lexical):
            scores[idx] = scores.get(idx, 0.0) + lex_weight / (rrf + rank + 1)
        return sorted(scores.items(), key=lambda kv: -kv[1])[:k]


def load(path: Path = DB_PATH) -> Index:
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在，先跑 build")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    meta = dict(conn.execute("SELECT k, v FROM meta"))
    rows = conn.execute(
        "SELECT uid, book, kind, chapter, ref, title, text, tokens"
        " FROM units ORDER BY uid").fetchall()
    vecs = {row[0]: (row[1], row[2], row[3]) for row in conn.execute(
        "SELECT uid, model, dim, vec FROM vectors")}
    conn.close()
    units: list[dict[str, Any]] = []
    tokens: list[list[str]] = []
    matrix: list[np.ndarray] = []
    model = meta.get("model", EMBED_MODEL)
    for row in rows:
        found = vecs.get(row[0])
        if found is None:
            continue
        units.append({"uid": row[0], "book": row[1], "kind": row[2],
                      "chapter": row[3], "ref": row[4], "title": row[5],
                      "text": row[6]})
        tokens.append(json.loads(row[7]))
        matrix.append(np.frombuffer(found[2], dtype=np.float32))
    if not matrix:
        raise RuntimeError("索引里没有向量")
    return Index(units, tokens, np.vstack(matrix), model)


def encode_query(text: str, *, model: str = EMBED_MODEL) -> list[float]:
    vecs = asyncio.run(embed_mod.embed_texts([text], model=model))
    return vecs[0]
