"""建立写作语义召回索引（决策 148）。

把 ``book_chunks`` 的正文用**当前绑定的 embedding 模型**嵌入，向量落
``data/embeddings.db``。索引是增量的：正文 hash 没变的块跳过；重导改了正文
的块会重嵌；``--force`` 只重嵌本次选中的块，不动别的书。

跑法::

    python scripts/build_embedding_index.py                  # 全库
    python scripts/build_embedding_index.py --dry-run        # 只看要嵌多少
    python scripts/build_embedding_index.py --book 窄门      # 先索引一本
    python scripts/build_embedding_index.py --limit 50       # 试跑

中途限流失败不会丢已完成的批次：每批完成即落库，重跑会从断点继续。
"""
from __future__ import annotations

import argparse
import asyncio
import math
import sqlite3
import sys
import time
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config                                          # noqa: E402
from services import embedding_service as embed_mod                # noqa: E402
from storage.embedding_db import (                                 # noqa: E402
    EmbeddingDB,
    content_hash,
    resolve_embedding_db_path,
)


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_books(conn: sqlite3.Connection, names: list[str]) -> set[str]:
    rows = conn.execute("SELECT id, title FROM books").fetchall()
    if not names:
        return {str(r["id"]) for r in rows}
    picked: set[str] = set()
    for name in names:
        key = name.strip()
        exact = [str(r["id"]) for r in rows if str(r["title"]) == key]
        hit = exact or [str(r["id"]) for r in rows if key in str(r["title"])]
        if not hit:
            raise SystemExit(f"书库里找不到《{key}》")
        picked.update(hit)
    return picked


def _chunks(conn: sqlite3.Connection, book_ids: set[str], *,
            min_chars: int, limit: int | None) -> list[dict]:
    rows = conn.execute(
        "SELECT id, book_id, chapter_index, content"
        " FROM book_chunks WHERE char_count>=?"
        " ORDER BY book_id, chapter_index, chunk_index", (min_chars,)
    ).fetchall()
    out = [{"chunk_id": str(r["id"]), "book_id": str(r["book_id"]),
            "chapter_index": int(r["chapter_index"] or 0),
            "content": str(r["content"] or "")}
           for r in rows if str(r["book_id"]) in book_ids]
    return out[:limit] if limit else out


def main() -> int:
    parser = argparse.ArgumentParser(description="建立写作语义召回索引")
    parser.add_argument("--book", action="append", default=[],
                        help="书名（可重复，支持子串）；不传=全库")
    parser.add_argument("--limit", type=int, default=0,
                        help="本次最多处理多少块（试跑用）")
    parser.add_argument("--model", default="", help="覆盖绑定模型")
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--min-chars", type=int, default=120)
    parser.add_argument("--force", action="store_true",
                        help="已索引的块也重嵌（只影响本次选中范围）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不调接口")
    parser.add_argument("--project-dir", default=str(ROOT))
    args = parser.parse_args()

    project = Path(args.project_dir).resolve()
    config = Config(project_dir=project)
    books_db = getattr(config, "books_db_path",
                       project / "data" / "books.db")
    emb_db = resolve_embedding_db_path(config)
    ep = embed_mod.resolve_endpoint(config)
    model = args.model.strip() or ep.model
    print(f"[embed] endpoint={ep.provider_name or ep.base_url} "
          f"model={model} source={ep.source}")

    conn = _ro(Path(books_db))
    try:
        book_ids = _resolve_books(conn, args.book)
        chunks = _chunks(conn, book_ids, min_chars=args.min_chars,
                         limit=args.limit or None)
    finally:
        conn.close()

    db = EmbeddingDB(emb_db)
    have = {} if args.force else db.fingerprints(model)
    todo = [c for c in chunks
            if have.get(c["chunk_id"]) != content_hash(c["content"])]
    requests = math.ceil(len(todo) / max(1, min(args.batch, 10)))
    print(f"[embed] books={len(book_ids)} chunks={len(chunks)} "
          f"need={len(todo)} requests≈{requests}")
    if args.dry_run or not todo:
        if not todo and chunks:
            print("[embed] 已经是最新")
        print(f"[embed] index={db.stats(model)}")
        db.close()
        return 0

    done = 0
    t0 = time.time()

    def progress(d: int, total: int) -> None:
        nonlocal done
        if d - done >= 100 or d == total:
            done = d
            print(f"[embed] {d}/{total} ({time.time() - t0:.0f}s)",
                  flush=True)

    def save_batch(start: int, vectors: list[list[float]]) -> None:
        rows = []
        for item, vec in zip(todo[start:start + len(vectors)], vectors):
            blob = array("f", vec).tobytes()
            rows.append((item["chunk_id"], content_hash(item["content"]),
                         item["book_id"], item["chapter_index"],
                         len(vec), blob))
        db.upsert_many(model, rows)

    try:
        asyncio.run(embed_mod.embed_texts(
            config, [c["content"] for c in todo], model=model,
            batch=args.batch, concurrency=args.concurrency,
            progress=progress, on_batch=save_batch))
    except embed_mod.EmbeddingError as exc:
        print(f"[embed] 失败（已完成批次已落库）：{exc}", flush=True)
        print(f"[embed] index={db.stats(model)}")
        db.close()
        return 2
    print(f"[embed] 完成，用时 {time.time() - t0:.0f}s，"
          f"index={db.stats(model)}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
