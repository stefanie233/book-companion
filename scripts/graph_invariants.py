"""对存量图谱跑机械体检，把「哪里真的坏了」钉成数字。

**先测量再动手** —— 本脚本是 ``bookgraph/invariants.py`` 的第一个消费者。
初稿设计曾凭推断断言「97% 的节点定位不到章」，实测是 3984/3990 正常 ——
所以任何结论先过这里。

用法:
  python scripts/graph_invariants.py            # 全部有图谱的书
  python scripts/graph_invariants.py 窄门        # 按书名子串
  python scripts/graph_invariants.py --verbose  # 逐条打印发现
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph.invariants import check_graph              # noqa: E402
from config import load_config                            # noqa: E402
from storage.db import BookDB                             # noqa: E402
from storage.graph_db import GraphDB, resolve_graph_db_path  # noqa: E402


def pull(gdb: GraphDB, book_id: str) -> dict:
    """把一本书的图谱五份行查出来（体检器是纯函数，不碰库）。"""
    conn = gdb.get_conn()
    return {
        "nodes": conn.execute(
            "SELECT id, canonical_name, kind FROM nodes WHERE project_id=?",
            (book_id,)).fetchall(),
        "edges": conn.execute(
            "SELECT source_id, target_id, kind, chunk_id FROM edges WHERE project_id=?",
            (book_id,)).fetchall(),
        "observations": conn.execute(
            "SELECT node_id, field, value, evidence FROM graph_observations "
            "WHERE project_id=?", (book_id,)).fetchall(),
        "quotes": conn.execute(
            "SELECT character_name, quote_text, chunk_id, batch_id FROM graph_quotes "
            "WHERE project_id=?", (book_id,)).fetchall(),
        "chunks": conn.execute(
            "SELECT chunk_id FROM graph_chunks WHERE book_id=?", (book_id,)).fetchall(),
        "aliases": conn.execute(
            "SELECT node_id, alias_name FROM graph_aliases WHERE project_id=?",
            (book_id,)).fetchall(),
        "unresolved": conn.execute(
            "SELECT reference_name, reference_kind FROM graph_unresolved "
            "WHERE project_id=?", (book_id,)).fetchall(),
    }


def chunk_text_map(db: BookDB, gdb: GraphDB, book_id: str) -> dict[str, str]:
    """图谱块 id → 正文。按内容指纹优先、坐标兜底，与 ``relink_chunks`` 同一把尺子。

    取不到的块就不进映射 —— 逐字核对会明说「只核对了 N/M 条」，不假装通过。
    """
    by_hash: dict[str, str] = {}
    by_coord: dict[tuple[int, int], str] = {}
    for c in db.get_chunks(book_id):
        by_hash.setdefault(
            hashlib.sha256((c.content or "").encode("utf-8")).hexdigest()[:16],
            c.content or "")
        by_coord[(c.chapter_index, c.chunk_index)] = c.content or ""
    out: dict[str, str] = {}
    for r in gdb.get_conn().execute(
            "SELECT chunk_id, chapter_index, chunk_index, content_hash "
            "FROM graph_chunks WHERE book_id=?", (book_id,)):
        text = by_hash.get(r["content_hash"])
        if text is None:
            text = by_coord.get((r["chapter_index"], r["chunk_index"]))
        if text is not None:
            out[r["chunk_id"]] = text
    return out


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

    totals: dict[str, int] = {}
    books = 0
    q_total = q_ok = 0
    for bid, title in rows:
        graph_path = resolve_graph_db_path(cfg, db, bid)
        if not graph_path or not Path(graph_path).exists():
            continue
        gdb = GraphDB(Path(graph_path))
        try:
            g = pull(gdb, bid)
            if not g["chunks"]:
                continue
            rep = check_graph(
                g,
                chunk_text=chunk_text_map(db, gdb, bid),
                book_text="".join(c.content or "" for c in db.get_chunks(bid)),
            )
        finally:
            gdb.close()
        books += 1
        q_total += rep.counts["quotes"]
        q_ok += rep.counts.get("anchored_ok", 0)
        for f in rep.findings:
            totals[f.code] = totals.get(f.code, 0) + f.count
        c = rep.counts
        head = (f"{str(title)[:20]:22} 节点{c['nodes']:>5} 边{c['edges']:>5} "
                f"引文{c['quotes']:>4} 观察{c['observations']:>4} "
                f"| 问题 {len(rep.findings):>3}")
        print(("OK  " if rep.clean else "!!  ") + head)
        if verbose:
            for f in rep.findings:
                print(f"      {f.render()}")
            for s in rep.skipped:
                print(f"      (跳过) {s}")

    if q_total:
        print(f"\n引文 {q_total} 条：锚对 {q_ok}（{q_ok * 100 // q_total}%）、"
              f"锚错块 {totals.get('quote_misanchored', 0)}、"
              f"全书找不到 {totals.get('quote_unverifiable', 0)}")
    print(f"\n{books} 本有图谱的书。问题按**涉及条目数**：")
    for code, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {code:26} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
