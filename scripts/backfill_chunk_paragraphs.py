"""One-off: backfill paragraph identity (v17) for pre-v17 chunks.

v17 给 book_chunks 加了 paragraph_count + paragraph_offsets，旧 chunk 这两列
落地为默认 0/'[]'。本脚本从已有 chunk.content 低损派生段落边界回填——
**不改 chunk 划分**，只补段落快照（upsert 按行）。

幂等：只处理 paragraph_count == 0 的行；中途中断可续跑。
默认 dry-run（只统计），--apply 才写库。

用法:
  python scripts/backfill_chunk_paragraphs.py            # 统计待回填
  python scripts/backfill_chunk_paragraphs.py --apply    # 回填
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.db import BookDB
from services.library_service import _extract_paragraphs, _paragraph_offsets


def _derive(conn, book_id: str):
    """为某书的所有空列 chunk 派生段落快照。返回 (filled, skipped)."""
    rows = conn.execute(
        "SELECT id, content FROM book_chunks "
        "WHERE book_id=? AND paragraph_count = 0",
        (book_id,),
    ).fetchall()
    updates = []
    skipped = 0
    for r in rows:
        cid, content = r["id"], r["content"]
        paras = _extract_paragraphs(content)
        if not paras:
            skipped += 1
            continue
        offsets = _paragraph_offsets(content, paras)
        if any(s < 0 for s, _ in offsets):
            skipped += 1  # 段落文本在 content 中定位失败（内容异常）——不硬填
            continue
        updates.append((len(paras), json.dumps(offsets, ensure_ascii=False), cid))
    return updates, skipped


def main() -> int:
    apply = "--apply" in sys.argv
    db = BookDB(Path("data/books.db"))
    conn = db.get_conn()

    books = conn.execute(
        "SELECT id, title FROM books "
        "WHERE id IN (SELECT DISTINCT book_id FROM book_chunks)"
    ).fetchall()
    if not books:
        print("库里没有 chunk，无需回填。")
        return 0

    total_filled = total_skipped = 0
    total_paras = 0
    per_book = []
    for b in books:
        updates, skipped = _derive(conn, b["id"])
        total_filled += len(updates)
        total_skipped += skipped
        total_paras += sum(u[0] for u in updates)
        per_book.append((b["title"], b["id"], len(updates), skipped))
        if apply and updates:
            conn.executemany(
                "UPDATE book_chunks SET paragraph_count=?, paragraph_offsets=? "
                "WHERE id=?;",
                updates,
            )
    conn.commit()

    print(f"{'[dry-run] ' if not apply else '[apply]   '}待回填 chunk: {total_filled} 行, "
          f"跳过(空/定位失败): {total_skipped}, 落段落总数: {total_paras}")
    for title, bid, n, skip in per_book[:20]:
        if n or skip:
            print(f"  {title[:22]:<24} {bid[:12]} 填 {n} 跳过 {skip}")
    if not apply:
        print("\n(dry-run 未写库; 加 --apply 执行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())