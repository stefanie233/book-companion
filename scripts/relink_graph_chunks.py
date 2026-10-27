"""把图谱的锚点重连到**当前**的 book_chunks 上。

重导会重新生成 ``book_chunks.id``（`services/epub_import_service.py` 的
`_new_id()`），而图谱的边 / 引文 / 观察都指着旧 id。实测 9 本有图谱的书里
**8 本交集为 0** —— 97% 的节点因此定位不到章，阅读侧的剧透门（`max_chapter`
过滤）形同虚设，引文的「翻书复核」也断了。

匹配顺序：**内容指纹优先，坐标兜底**。拒绝而非猜测 —— 一个旧块对应多个候选
记 ambiguous、找不到记 unresolved，两者都**不写**，且都在报告里列出来。

默认 dry-run，--apply 才写库。

用法:
  python scripts/relink_graph_chunks.py                # 全部书，只看
  python scripts/relink_graph_chunks.py 窄门            # 按书名子串
  python scripts/relink_graph_chunks.py 窄门 --apply    # 真写
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 控制台默认 GBK，书名/中文标点可能触发 UnicodeEncodeError 把写库那步一起带走。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import load_config                      # noqa: E402
from storage.db import BookDB                       # noqa: E402
from storage.graph_db import GraphDB, resolve_graph_db_path  # noqa: E402


def pick(db: BookDB, args: list[str]) -> list[tuple[str, str]]:
    """书名子串 → [(book_id, title)]；--all 取全部。"""
    cur = db.get_conn()
    if "--all" in args or not args:
        return cur.execute("SELECT id, title FROM books ORDER BY title").fetchall()
    out: list[tuple[str, str]] = []
    for arg in args:
        rows = cur.execute(
            "SELECT id, title FROM books WHERE title LIKE ? ORDER BY title",
            (f"%{arg}%",)).fetchall()
        if not rows:
            print(f"!! 库里没有书名含 {arg!r} 的书")
        out.extend(rows)
    return out


def current_chunks(db: BookDB, book_id: str) -> list[dict]:
    """当前 book_chunks 的 (id, 坐标, 内容指纹) —— 与 register_chunk 同一把尺子。"""
    return [{
        "chunk_id": c.id,
        "chapter_index": c.chapter_index,
        "chunk_index": c.chunk_index,
        "content_hash": hashlib.sha256(
            (c.content or "").encode("utf-8")).hexdigest()[:16],
    } for c in db.get_chunks(book_id)]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv

    cfg = load_config()
    db = BookDB(Path(cfg.books_db_path))
    db.initialize()

    targets = pick(db, args)
    print(f"{'APPLY' if apply else 'DRY-RUN'}  {len(targets)} 本书\n")

    touched = rejected = 0
    for bid, title in targets:
        graph_path = resolve_graph_db_path(cfg, db, bid)
        if not graph_path or not Path(graph_path).exists():
            continue
        gdb = GraphDB(Path(graph_path))
        try:
            rep = gdb.relink_chunks(bid, current_chunks(db, bid), dry_run=not apply)
        finally:
            gdb.close()
        if not rep.total:
            continue
        touched += 1
        rejected += len(rep.ambiguous) + len(rep.unresolved)
        flag = "OK " if not (rep.ambiguous or rep.unresolved) else "!! "
        print(f"{flag}{str(title)[:24]:26} {rep.summary()}")
        for item in (rep.ambiguous + rep.unresolved)[:5]:
            print(f"     {item['reason']}: {item.get('chunk_id')} "
                  f"@ch{item.get('chapter_index')}.{item.get('chunk_index')}")

    print(f"\n有图谱的书: {touched}；拒绝重连 {rejected} 条")
    if not apply:
        print("(dry-run 未写库；确认无误后加 --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
