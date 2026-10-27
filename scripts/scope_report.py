"""叙事域体检 —— 量「跨篇连线」到底有多少条。**只读，不改库。**

为什么需要它：P2b 的验收标准是「我与地坛 跨篇连线数为 0」，但那是**重跑之后**
的事。存量图谱里没有「域」这个概念，所以先得有个能对任何一本书算出这个数的
尺子，否则验收只能靠感觉。

三件事必须说清楚，否则数字会骗人：

1. **跨篇数只在「真的划了域」的书上有意义。** 长篇里人物本来就跨章，跨篇边是
   要的。所以单域的书这里只报「若判为集类会怎么划」，不报跨篇数。
2. **节点定位要经 `graph_chunks`**，不能拿 `nodes.first_appearance_chunk_id` 去
   join `books.db` 的块 —— 重导换过 `book_chunks.id`，图谱里的旧 id 在现库里
   根本不存在。这与 `GraphDB._chapter_of_chunk` 同一口径。
3. **定位率低就不下结论。** 锚点接不上的时候报出来的 0 是假的 0。

跑法：
  python scripts/scope_report.py              # 全部书
  python scripts/scope_report.py 我与地坛      # 按书名子串
  python scripts/scope_report.py --verbose    # 连跨篇的边明细一起列
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph.scope import COLLECTION, plan_scopes, top_level_units  # noqa: E402
from bookgraph.selection import select_chunks                          # noqa: E402
from config import Config                                              # noqa: E402
from storage.db import BookDB                                          # noqa: E402
from storage.graph_db import (                                         # noqa: E402
    TABLE_GRAPH_CHUNKS, TABLE_NODES, GraphDB, resolve_graph_db_path,
)

ROOT = Path(__file__).parent.parent
ANCHOR_FLOOR = 0.9


def load_plan(books_db: BookDB, book_id: str, verdict: str):
    """按提取时的口径重算域划分。*verdict* 是当时的语义裁决。"""
    chapters = books_db.get_chapters(book_id)
    chunks = books_db.get_chunks(book_id)
    if not chapters or not chunks:
        return None
    types = {c.chapter_index: c.chapter_type for c in chapters}
    sel = select_chunks(chunks, types)
    kept = [c for c in chunks if c.id in set(sel.keep)]
    return plan_scopes(chapters, kept, narrative_structure=verdict)


def shape(plan) -> str:
    return " | ".join(f"{s.label} {len(s.chunk_ids)}块" for s in plan.scopes)


def measure(book_id: str, gdb_path: Path, plan, verbose: bool) -> dict:
    """域内/跨域的边数。只在 plan.is_split 时有意义。"""
    chapter_scope = {ci: s.scope_id for s in plan.scopes for ci in s.chapter_indices}
    gdb = GraphDB(Path(gdb_path))
    conn = gdb.get_conn()
    node_scope: dict[str, str] = {}
    for r in conn.execute(
            f"SELECT n.id, gc.chapter_index ci FROM {TABLE_NODES} n "
            f"LEFT JOIN {TABLE_GRAPH_CHUNKS} gc "
            "  ON gc.chunk_id = n.first_appearance_chunk_id "
            " AND gc.book_id = n.project_id "
            "WHERE n.project_id=?", (book_id,)):
        node_scope[r["id"]] = chapter_scope.get(r["ci"], "")

    cross: list[tuple[str, str, str]] = []
    total = 0
    for r in conn.execute(
            "SELECT e.source_id s, e.target_id t, "
            "  (SELECT canonical_name FROM nodes WHERE id=e.source_id) sn, "
            "  (SELECT canonical_name FROM nodes WHERE id=e.target_id) tn "
            "FROM edges e WHERE e.project_id=?", (book_id,)):
        total += 1
        a, b = node_scope.get(r["s"], ""), node_scope.get(r["t"], "")
        if a and b and a != b:
            cross.append((r["sn"] or "?", r["tn"] or "?", f"{a}→{b}"))
    gdb.close()
    n = len(node_scope)
    anchored = sum(1 for v in node_scope.values() if v)
    return {"edges": total, "cross": cross, "nodes": n,
            "anchored": anchored, "rate": anchored / n if n else 0.0}


def verdict_of(gdb_path: Path) -> str:
    """图库缓存里的语义裁决 —— 那是提取当时真正生效的那一份。"""
    try:
        g = GraphDB(Path(gdb_path))
        raw = g.get_meta("book_context")
        g.close()
        return (json.loads(raw) or {}).get("narrative_structure", "") if raw else ""
    except Exception:
        return ""


def main() -> int:
    needle = next((a for a in sys.argv[1:] if not a.startswith("-")), "")
    verbose = "--verbose" in sys.argv
    cfg = Config()
    books_db = BookDB(ROOT / "data" / "books.db")
    rows = books_db.get_conn().execute(
        "SELECT id, title FROM books ORDER BY title").fetchall()
    shown = 0
    for b in rows:
        title = b["title"] or ""
        if needle and needle not in title:
            continue
        gdb_path = resolve_graph_db_path(cfg, books_db, b["id"])
        has_graph = bool(gdb_path) and Path(gdb_path).exists()
        plan = load_plan(books_db, b["id"],
                         verdict_of(gdb_path) if has_graph else "")
        if plan is None:
            continue
        shown += 1
        units = len(top_level_units(books_db.get_chapters(b["id"])))
        print(f"\n### {title[:34]}  ({b['id']})")
        print(f"    顶层单元 {units} · 域裁决 "
              f"{'** 划域 **' if plan.is_split else '单域'}"
              f"（{plan.reason or '未判定'}）")
        if plan.conflicts:
            print(f"    冲突：{plan.conflicts[0]}")

        if plan.is_split:
            print(f"    域：{shape(plan)}")
            if not has_graph:
                print("    无图谱（还没提取过）—— 跨篇数待提取后再量")
                continue
            m = measure(b["id"], gdb_path, plan, verbose)
            tail = "" if m["rate"] >= ANCHOR_FLOOR else \
                "  ← 不可信：锚点没接上（先跑 scripts/relink_graph_chunks.py）"
            print(f"    边 {m['edges']} 条 · **跨篇 {len(m['cross'])} 条**"
                  f"（{len(m['cross']) / m['edges']:.0%}）{tail}" if m["edges"]
                  else "    无边")
            print(f"    节点 {m['nodes']} 个 · 能定位到篇的 {m['rate']:.0%}")
            if verbose:
                for sn, tn, ab in m["cross"][:25]:
                    print(f"      {ab}  {sn} — {tn}")
        else:
            whatif = load_plan(books_db, b["id"], COLLECTION)
            if whatif is not None and whatif.is_split:
                print(f"    ↳ 若判为集类会划成：{shape(whatif)}")
            else:
                print(f"    ↳ 即使判为集类也不划域："
                      f"{whatif.reason if whatif else '—'}")
    books_db.close()
    if not shown:
        print("没有可报告的书（书名没匹配上？）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
