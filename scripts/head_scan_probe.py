"""HeadScan 真实验证探针。

用法:
  python scripts/head_scan_probe.py [book_title_substr]

从 books.db 取书名含子串的一本书的头部 chunks,在真实 LLM 上跑 run_head_scan,
打印头部结构对照表 + 判定结果,供评审提示词/边界判定是否正确。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from storage.db import BookDB
from bookgraph.head_scan import run_head_scan


def find_book(db: BookDB, sub: str):
    cur = db.get_conn().cursor()
    cur.execute("SELECT id, title FROM books WHERE title LIKE ? LIMIT 5",
                (f"%{sub}%",))
    return cur.fetchall()


def main() -> int:
    cfg = load_config()
    if not cfg.api_key:
        print("!! 未检测到 api_key(.env BOOK_COMPANION_API_KEY / OPENAI_API_KEY)")
        return 2
    print(f"端点: {cfg.api_base}  模型: {cfg.model}")

    sub = sys.argv[1] if len(sys.argv) > 1 else "身体从未忘记"
    db = BookDB(Path("data/books.db"))
    books = find_book(db, sub)
    if not books:
        print(f"!! 无书匹配 {sub!r}")
        return 1
    book_id, title = books[0][0], books[0][1]
    print(f"\n书: {title} ({book_id})")

    chapters = db.get_chapters(book_id)
    titles = {c.chapter_index: c.title for c in chapters}
    chunks = sorted(db.get_chunks(book_id),
                    key=lambda c: (c.chapter_index, c.chunk_index))
    head = chunks[:24]

    print("\n── 头部结构对照表(LLM 判定前) ──")
    for ch in head:
        ctitle = titles.get(ch.chapter_index, f"第{ch.chapter_index + 1}章")
        print(f"  {ch.chapter_index}.{ch.chunk_index} [{ctitle[:16]}] "
              f"len={ch.char_count}  {ch.content[:42]!r}")

    async def _run() -> dict:
        async with aiohttp.ClientSession() as session:
            return await run_head_scan(
                session, cfg, title, "", head, titles, log_cb=None)

    result = asyncio.run(_run())
    print("\n── HeadScan 判定结果 ──")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())