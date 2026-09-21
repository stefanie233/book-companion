# -*- coding: utf-8 -*-
"""章节类型补标注工具 —— 覆盖层便利贴的命令行入口（第 3 步最小版）。

用法:
  python scripts/chapter_type_editor.py <书ID或书名子串>
      → 列出全部章节 + 当前生效类型 + 来源（原表 / 便利贴）
  python scripts/chapter_type_editor.py <书> <章序号> set <类型> [备注]
      → 贴便利贴：把某章定为指定类型（human 优先，重导不丢）
  python scripts/chapter_type_editor.py <书> <章序号> clear
      → 撕便利贴：回落到原表（导入/AI 猜的）类型

  <类型> 取值: cover copyright toc preface body postscript
               appendix bibliography index notes
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, Path(__file__).resolve().parent.parent.as_posix())

from storage.db import BookDB

VALID = {
    "cover", "copyright", "toc", "preface", "body", "postscript",
    "appendix", "bibliography", "index", "notes",
}
LABELS = {
    "cover": "封面", "copyright": "版权", "toc": "目录", "preface": "前言",
    "body": "正文", "postscript": "后记", "appendix": "附录",
    "bibliography": "参考", "index": "索引", "notes": "注释",
}


def _find_book(db: BookDB, sub: str) -> str:
    rows = db.get_conn().execute(
        "SELECT id, title FROM books WHERE title LIKE ? LIMIT 5",
        (f"%{sub}%",),
    ).fetchall()
    if not rows:
        sys.exit(f"!! 无书匹配 {sub!r}")
    if len(rows) > 1:
        print(f"多本书命中，取第一本：{rows[0][1]}")
    return rows[0][0]


def _list(db: BookDB, book_id: str) -> None:
    row = db.get_conn().execute(
        "SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
    title = row[0] if row else book_id
    print(f"\n=== {title} ===")
    over = {o.chapter_index: o for o in _all_overrides(db, book_id)}
    for ch in db.get_chapters(book_id):
        mark = "◉" if ch.chapter_index in over else "○"
        src = over[ch.chapter_index].source if ch.chapter_index in over else "原表(猜)"
        lab = LABELS.get(ch.chapter_type, ch.chapter_type)
        print(f"  {mark} [{ch.chapter_index:3}] {ch.title[:26]:<28} "
              f"{lab:<4} ({src})  {ch.char_count}字")


def _all_overrides(db: BookDB, book_id: str) -> list:
    cur = db.get_conn().execute(
        "SELECT chapter_index, note, source FROM book_chapter_overrides "
        "WHERE book_id=? ORDER BY chapter_index;", (book_id,))
    out = []
    for r in cur.fetchall():
        o = object()
        out.append(type("OV", (), {"chapter_index": r[0], "note": r[1],
                                   "source": r[2]})())
    return out


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    db = BookDB(Path("data/books.db"))
    db.initialize()

    book_id = _find_book(db, args[0])
    if len(args) == 1:
        _list(db, book_id)
        return 0

    # <书> <章号> set <类型> [备注] | <书> <章号> clear
    try:
        idx = int(args[1])
    except ValueError:
        print("!! 章序号须为数字")
        return 1

    if len(args) >= 4 and args[2].lower() == "set":
        ctype = args[3]
        if ctype not in VALID:
            print(f"!! 非法类型 {ctype!r}，可选: {sorted(VALID)}")
            return 1
        note = " ".join(args[4:])
        db.set_chapter_override(book_id, idx, ctype, note=note)
        ch = db.get_chapter(book_id, idx)
        print(f"◉ 更新第{idx}章 [{ch.title[:20]}] → {LABELS.get(ctype, ctype)}")
        if note:
            print(f"  备注: {note}")
    elif args[2].lower() == "clear":
        db.clear_chapter_override(book_id, idx)
        ch = db.get_chapter(book_id, idx)
        print(f"○ 清除第{idx}章便利贴 → 回落 [{ch.chapter_type}]")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())