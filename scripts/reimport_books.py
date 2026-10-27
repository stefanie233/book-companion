"""就地重导：补上结构列（spine_index / level / parent_index / heading_tag / in_toc）。

用法:
  python scripts/reimport_books.py 人类简史 大棋局      # 按书名子串
  python scripts/reimport_books.py --aligned            # 只重导已对齐过的书（推荐）
  python scripts/reimport_books.py --all                # 全部（**含未对齐的，危险**）
  python scripts/reimport_books.py 人类简史 --dry        # 只看会变成什么

重导走的是和首次导入完全相同的 ``import_epub_with_split``，方法/selector 从
``books.metadata_json`` 里读回来 —— 切分逻辑没动，所以 chapter_index 与
chunk_index 都不变，批注（按 chunk 定位）和 agent 覆盖层（按 chapter_index）
不会错位。

**优先用 ``--aligned``**：它只挑 spine_index 齐全的书，也就是「重导过一次且
成功」的那些，再跑一次按构造就是安全的。``--all`` 会连没对齐过的书一起重导，
而它们 ``metadata_json`` 里往往没有 ``epub_import_method``，脚本会退回默认的
``h_tags`` 全切 —— 当下的力量（15 章）与非暴力沟通（21 章）就是这么被炸成
94 / 101 章的。名字撞车的书（两个「反杜林论」条目，旧的那个 EPUB 是坏的）
按书名子串也会挑错，``--aligned`` 顺带避开。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 控制台默认 GBK，书名与层级分布里的中文会触发 UnicodeEncodeError。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import load_config
from services.epub_import_service import import_epub_with_split
from storage.db import BookDB


def pick(db: BookDB, pat: str) -> list[tuple[str, str]]:
    cur = db.get_conn().execute(
        "SELECT id, title FROM books WHERE title LIKE ? ORDER BY title",
        (f"%{pat}%",))
    return [(r[0], r[1]) for r in cur.fetchall()]


def main() -> int:
    cfg = load_config()
    db = BookDB(cfg.books_db_path)
    db.initialize()
    bb = cfg.books_db_path.parent / "bookbank"
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv

    if "--all" in sys.argv:
        cur = db.get_conn().execute("SELECT id, title FROM books ORDER BY title")
        targets = [(r[0], r[1]) for r in cur.fetchall()]
    elif "--aligned" in sys.argv:
        cur = db.get_conn().execute(
            "SELECT DISTINCT b.id, b.title FROM books b "
            "JOIN book_chapters ch ON ch.book_id = b.id "
            "WHERE ch.spine_index >= 0 ORDER BY b.title")
        targets = [(r[0], r[1]) for r in cur.fetchall()]
    else:
        targets = [t for a in args for t in pick(db, a)]
    if not targets:
        print("!! 没匹配到书")
        return 2

    for bid, title in targets:
        epub = bb / f"{bid}.epub"
        if not epub.exists():
            print(f"--  {str(title)[:22]:24} 缺 epub，跳过")
            continue
        meta = json.loads(db.get_book(bid).metadata_json or "{}")
        method = meta.get("epub_import_method") or "h_tags"
        selector = meta.get("epub_import_selector") or ""
        before = db.get_chapters(bid)
        if dry:
            print(f"--  {str(title)[:22]:24} {len(before):>4} 章  "
                  f"方法={method} selector={selector!r}")
            continue
        try:
            import_epub_with_split(str(epub), method, selector, db, book_id=bid)
        except Exception as exc:
            print(f"!!  {str(title)[:22]:24} 重导失败: {type(exc).__name__}: {exc}")
            continue
        after = db.get_chapters(bid)
        lv = {}
        for c in after:
            lv[c.level] = lv.get(c.level, 0) + 1
        rooted = sum(1 for c in after if c.parent_index >= 0)
        print(f"OK  {str(title)[:22]:24} {len(before):>4} -> {len(after):>4} 章  "
              f"层级分布={dict(sorted(lv.items()))}  有父的={rooted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
