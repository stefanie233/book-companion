"""书块语义向量库 ``data/embeddings.db``（决策 148）。

单独一个文件，不进 books.db：

- books.db 的 schema 归导入链路，向量是写作侧的**派生物**，不该跟着书库迁移；
- 重导会复用 chunk 主键，``content_hash`` 不匹配的旧向量由下次建索引覆盖；
- 删书留下的孤儿向量不会让检索报错：返回卡前会回 books.db 取正文，取不到就跳过。

向量一律归一化后以 float32 小端字节存；查询向量的点积就是余弦。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from storage.db_base import BaseDatabase

EMBEDDING_DDL_V1: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS chunk_vectors (
        chunk_id      TEXT NOT NULL,
        model         TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        book_id       TEXT NOT NULL,
        chapter_index INTEGER NOT NULL DEFAULT 0,
        dim           INTEGER NOT NULL,
        vec           BLOB NOT NULL,
        updated_at    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chunk_id, model)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunk_vectors_book"
    " ON chunk_vectors(book_id, model)",
]


def content_hash(text: str) -> str:
    """块正文指纹；重导后正文变了就重嵌。"""
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()


def _default_user_root() -> Path:
    from runtime_paths import user_root
    return user_root()


def resolve_embedding_db_path(config: Any = None,
                              root: Path | None = None) -> Path:
    """向量库路径；与作者库同形状，全库一个文件。"""
    if root is not None:
        base = Path(root)
    else:
        project = getattr(config, "project_dir", "")
        base = Path(project) if project else _default_user_root()
    return base / "data" / "embeddings.db"


class EmbeddingDB(BaseDatabase):
    """书块向量。只存向量与索引所需的最小元数据，正文仍归 books.db。"""

    _MIGRATIONS: list[tuple[int, str, str | list[str]]] = [
        (1, "embedding v1: chunk vectors", EMBEDDING_DDL_V1),
    ]

    def __init__(self, db_path: Path | str):
        super().__init__(Path(db_path))
        self.initialize()

    # ── 写 ────────────────────────────────────────────────────────────────

    def upsert_many(self, model: str,
                    rows: Sequence[tuple[str, str, str, int, int, bytes]]
                    ) -> int:
        """rows = (chunk_id, content_hash, book_id, chapter_index, dim, vec)。"""
        if not rows:
            return 0
        now = int(time.time() * 1000)
        conn = self.get_conn()
        conn.executemany(
            "INSERT OR REPLACE INTO chunk_vectors"
            " (chunk_id, model, content_hash, book_id, chapter_index,"
            "  dim, vec, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            [(cid, model, chash, bid, int(chapter), int(dim), blob, now)
             for cid, chash, bid, chapter, dim, blob in rows])
        conn.commit()
        return len(rows)

    def delete_model(self, model: str) -> int:
        conn = self.get_conn()
        cur = conn.execute("DELETE FROM chunk_vectors WHERE model=?", (model,))
        conn.commit()
        return int(cur.rowcount or 0)

    # ── 读 ────────────────────────────────────────────────────────────────

    def fingerprints(self, model: str) -> dict[str, str]:
        """chunk_id → content_hash，用于增量建索引。"""
        conn = self.get_conn()
        return {str(row[0]): str(row[1]) for row in conn.execute(
            "SELECT chunk_id, content_hash FROM chunk_vectors WHERE model=?",
            (model,))}

    def vectors(self, model: str,
                book_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        conn = self.get_conn()
        sql = ("SELECT chunk_id, book_id, chapter_index, dim, vec"
               " FROM chunk_vectors WHERE model=?")
        args: list[Any] = [model]
        wanted = [str(b) for b in (book_ids or []) if str(b)]
        if wanted:
            sql += " AND book_id IN (" + ",".join("?" * len(wanted)) + ")"
            args.extend(wanted)
        return [
            {"chunk_id": str(row[0]), "book_id": str(row[1]),
             "chapter_index": int(row[2] or 0), "dim": int(row[3] or 0),
             "vec": row[4]}
            for row in conn.execute(sql, tuple(args))]

    def stats(self, model: str) -> dict[str, Any]:
        conn = self.get_conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM chunk_vectors WHERE model=?",
            (model,)).fetchone()[0]
        books = conn.execute(
            "SELECT COUNT(DISTINCT book_id) FROM chunk_vectors WHERE model=?",
            (model,)).fetchone()[0]
        return {"chunks": int(total or 0), "books": int(books or 0),
                "model": model}
