"""对比「按已完成的归属分类裁剪提取输入」会带来什么变化 —— 只看，不改。

第一版**只报告不裁剪**：先看清每本书会少提哪些块、哪些地方两份判断打架，
确认差异合理，再把裁剪接进 orchestrator。

用法:
  python scripts/selection_report.py            # 全部有图谱的书
  python scripts/selection_report.py 窄门
  python scripts/selection_report.py --verbose  # 逐条列冲突
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph.selection import select_chunks          # noqa: E402
from config import load_config                         # noqa: E402
from storage.db import BookDB                          # noqa: E402
from storage.graph_db import GraphDB, resolve_graph_db_path  # noqa: E402


def _meta(gdb: GraphDB, key: str) -> dict:
    raw = gdb.get_meta(key)
    if not raw:
        return {}
    try:
        return json.loads(raw) or {}
    except json.JSONDecodeError:
        return {}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verbose = "--verbose" in sys.argv

    cfg = load_config()
    db = BookDB(Path(cfg.books_db_path))
    db.initialize()
    cur = db.get_conn()

    if args:
        rows = []
        for a in args:
            rows += cur.execute("SELECT id, title FROM books WHERE title LIKE ?",
                                (f"%{a}%",)).fetchall()
    else:
        rows = cur.execute("SELECT id, title FROM books ORDER BY title").fetchall()

    print(f"{'书':24} {'总块':>5} {'保留':>5} {'体例裁':>6} {'头部裁':>6} "
          f"{'Scout裁':>7} {'冲突':>5}")
    seen = 0
    for bid, title in rows:
        graph_path = resolve_graph_db_path(cfg, db, bid)
        if not graph_path or not Path(graph_path).exists():
            continue
        gdb = GraphDB(Path(graph_path))
        try:
            chunks = db.get_chunks(bid)
            if not chunks:
                continue
            head = _meta(gdb, "head_scan")
            scout = _meta(gdb, "scout_plan")
        finally:
            gdb.close()
        types = {c.chapter_index: c.chapter_type for c in db.get_chapters(bid)}
        sel = select_chunks(
            chunks, types, head.get("boundaries") or {},
            set(scout.get("excluded_chunk_ids") or []))
        seen += 1
        c = sel.counts
        print(f"{str(title)[:22]:24} {sel.total:>5} {len(sel.keep):>5} "
              f"{c.get('by_type', 0):>6} {c.get('by_region', 0):>6} "
              f"{c.get('by_scout', 0):>7} {len(sel.conflicts):>5}")
        if verbose:
            for cf in sel.conflicts[:8]:
                print(f"      ! {cf.get('chunk_id', '')[:10]} "
                      f"ch{cf.get('chapter_index')}.{cf.get('chunk_index')} — {cf['who']}")

    print(f"\n{seen} 本有图谱且有块的书。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
