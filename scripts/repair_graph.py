"""存量图谱修复 —— 体检 + 锚点重连 + 同名多 kind 合并 + kind 归一。**默认 dry-run。**

四件事，按**风险从低到高**排：

1. **锚点重连**（``--relink``）：把边/引文/观察/节点首现指回当前 ``book_chunks``。
   纯机械，内容指纹优先、坐标兜底，拒绝而非猜测。风险最低。
2. **同名多 kind 合并**（``--merge``）：同名即同实体 —— 保留证据最多的那个
   节点（边度数 + 观察数 + 引文数），其余并进去。**只并同名，不并相似名。**
3. **kind 归一**（``--kinds``）：把旧库里的 ``person`` / ``character (archetypal)``
   等按 ``normalize_kind`` 改回核心词表。**必须在 ``--merge`` 之后跑** ——
   同名不同 kind 的两个节点归一后会撞 ``UNIQUE(project_id, canonical_name,
   kind)``（实测《道德经》18 处）。检测到碰撞就跳过，不硬改。
4. **孤儿**：**只报告，不删**。一个节点没有边也没有观察，可能是提取漏了关系，
   也可能是它真就不该存在 —— 这是判断，不是机械操作，交给人或 agent 决定。

跑法：
  python scripts/repair_graph.py                  # 只体检 + 出修复计划
  python scripts/repair_graph.py --relink --apply
  python scripts/repair_graph.py --merge --apply
  python scripts/repair_graph.py --all --apply    # 三件都做（merge 在 kinds 前）
  python scripts/repair_graph.py 我与地坛         # 按书名子串
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph.invariants import check_graph, normalize_kind     # noqa: E402
from config import Config                                       # noqa: E402
from storage.db import BookDB                                   # noqa: E402
from storage.graph_db import GraphDB, resolve_graph_db_path     # noqa: E402

ROOT = Path(__file__).parent.parent


def pull(gdb: GraphDB, book_id: str) -> dict:
    conn = gdb.get_conn()
    q = lambda sql: conn.execute(sql, (book_id,)).fetchall()   # noqa: E731
    return {
        "nodes": q("SELECT * FROM nodes WHERE project_id=?"),
        "edges": q("SELECT * FROM edges WHERE project_id=?"),
        "observations": q("SELECT * FROM graph_observations WHERE project_id=?"),
        "quotes": q("SELECT * FROM graph_quotes WHERE project_id=?"),
        "chunks": q("SELECT * FROM graph_chunks WHERE book_id=?"),
        "aliases": q("SELECT * FROM graph_aliases WHERE project_id=?"),
        "unresolved": q("SELECT * FROM graph_unresolved WHERE project_id=?"),
    }


def evidence_score(g: dict, node_id: str) -> tuple:
    """谁更像「正主」：边度数 + 观察数 + 引文数，再按 id 定序（稳定）。"""
    deg = sum(1 for e in g["edges"]
              if node_id in (e["source_id"], e["target_id"]))
    obs = sum(1 for o in g["observations"] if o["node_id"] == node_id)
    return (deg + obs, deg, obs)


def merge_plan(g: dict) -> list[tuple[str, str, str]]:
    """``[(keep_id, remove_id, name)]`` —— 同名多 kind 的合并计划。"""
    by_name: dict[str, list] = {}
    for n in g["nodes"]:
        if n["kind"] == "chapter":
            continue
        by_name.setdefault(n["canonical_name"], []).append(n)
    plan: list[tuple[str, str, str]] = []
    for name, rows in by_name.items():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: (evidence_score(g, r["id"]), r["id"]),
                      reverse=True)
        keep = rows[0]
        for r in rows[1:]:
            plan.append((keep["id"], r["id"], name))
    return plan


def kind_plan(g: dict) -> tuple[dict[tuple[str, str], int], list[str]]:
    """``({(旧 kind, 新 kind): 条数}, [碰撞说明])`` —— kind 归一计划。

    **为什么要有这一步**：``normalize_kind`` 只在**写入**时生效，旧库的行原样
    留着提取当时的说法。于是 ``person``(332) 与 ``character`` 并存 —— 而
    ``kind='character'`` 是三处硬编码（``incremental._get_character_names``、
    ``get_node_index_data``、``get_node_index_two_tier``），用了 ``person``
    的那本书**扮演模式整个是死的**（实测《身体从未忘记》：1971 条边、484 条
    观察全在，0 个 character）。

    **碰撞必须先合并**：``nodes`` 上有 ``UNIQUE(project_id, canonical_name,
    kind)``，两个同名不同 kind 的节点归一后会撞。实测《道德经》18 处 ——
    孔子裂成 4 个（``philosopher`` / ``character (archetypal)`` /
    ``character (historical/archetypal)`` / ``character``）。所以这里
    **检测到碰撞就不动手**，让调用方先跑 ``--merge``。
    """
    changes: dict[tuple[str, str], int] = {}
    seen: dict[tuple[str, str], list[str]] = {}
    for n in g["nodes"]:
        raw = n["kind"]
        new, _ = normalize_kind(raw)
        if new != raw:
            changes[(raw, new)] = changes.get((raw, new), 0) + 1
        seen.setdefault((n["canonical_name"], new), []).append(raw)
    collisions = [
        f"{name} [{kind}] ← {sorted(set(raws))}"
        for (name, kind), raws in seen.items() if len(raws) > 1
    ]
    return changes, collisions


def relink(gdb: GraphDB, books_db: BookDB, book_id: str, apply: bool) -> str:
    chunks = books_db.get_chunks(book_id)
    current = [{
        "chunk_id": c.id, "chapter_index": c.chapter_index,
        "chunk_index": c.chunk_index,
        "content_hash": hashlib.sha256(
            (c.content or "").encode("utf-8")).hexdigest()[:16],
    } for c in chunks]
    rl = gdb.relink_chunks(book_id, current, dry_run=not apply)
    return rl.summary()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    needle = args[0] if args else ""
    apply = "--apply" in sys.argv
    do_relink = "--relink" in sys.argv or "--all" in sys.argv
    do_merge = "--merge" in sys.argv or "--all" in sys.argv
    do_kinds = "--kinds" in sys.argv or "--all" in sys.argv
    if not (do_relink or do_merge or do_kinds):
        do_relink = do_merge = do_kinds = True   # 不给开关 = 出完整修复计划

    cfg = Config()
    books_db = BookDB(ROOT / "data" / "books.db")
    rows = books_db.get_conn().execute(
        "SELECT id, title FROM books ORDER BY title").fetchall()
    touched = 0
    for b in rows:
        if needle and needle not in (b["title"] or ""):
            continue
        gdb_path = resolve_graph_db_path(cfg, books_db, b["id"])
        if not gdb_path or not Path(gdb_path).exists():
            continue
        gdb = GraphDB(Path(gdb_path))
        g = pull(gdb, b["id"])
        if not g["nodes"]:
            gdb.close()
            continue
        touched += 1
        print(f"\n### {b['title'][:34]}  ({b['id']})")

        rep = check_graph(g)
        print(f"    体检：{len(rep.findings)} 类问题 / "
              f"节点 {rep.counts.get('nodes', 0)} 边 {rep.counts.get('edges', 0)}")
        for code, n in sorted(rep.by_code().items(), key=lambda kv: -kv[1]):
            print(f"      {code:<26} {n}")

        if do_relink:
            print(f"    锚点：{relink(gdb, books_db, b['id'], apply)}")

        if do_merge:
            plan = merge_plan(g)
            if plan:
                print(f"    同名多 kind：{len(plan)} 组可并"
                      + ("（已并）" if apply else "（dry-run）"))
                for keep, remove, name in plan[:8]:
                    print(f"      {name} → 保留 {keep[:12]}，并掉 {remove[:12]}")
                if apply:
                    done = 0
                    for keep, remove, _ in plan:
                        try:
                            gdb.merge_nodes(b["id"], keep, remove)
                            done += 1
                        except Exception as exc:
                            print(f"      合并失败 {remove[:12]}: {exc}")
                    print(f"      实并 {done}/{len(plan)}")
                    # 合并动过 nodes 表 —— 后面 kind 归一必须看**新**快照，
                    # 否则会拿合并前的重名去报「碰撞」而整步跳过（踩过一次）。
                    g = pull(gdb, b["id"])
            else:
                print("    同名多 kind：无")

        if do_kinds:
            # 顺序要紧：**先合并后归一**。反过来的话，两个同名不同 kind 的节点
            # 会被 UPDATE 撞上 UNIQUE(project_id, canonical_name, kind)。
            changes, collisions = kind_plan(g)
            if not changes:
                print("    kind 归一：无（本库的 kind 已全在词表内）")
            elif collisions and do_merge and not apply:
                print(f"    kind 归一：{sum(changes.values())} 个节点待改 —— "
                      f"但先跑 --merge 清掉 {len(collisions)} 处同名碰撞")
            elif collisions:
                print(f"    kind 归一：**跳过** —— {len(collisions)} 处同名碰撞未清：")
                for c in collisions[:6]:
                    print(f"      {c}")
                if not do_merge:
                    print("      （加 --merge 一起跑，或先单独跑 --merge --apply）")
            else:
                print(f"    kind 归一：{sum(changes.values())} 个节点"
                      + ("（已改）" if apply else "（dry-run）"))
                for (src, dst), n in sorted(changes.items(), key=lambda kv: -kv[1]):
                    print(f"      {src:<38} -> {dst:<12} x{n}")
                if apply:
                    conn = gdb.get_conn()
                    for (src, dst) in changes:
                        conn.execute(
                            "UPDATE nodes SET kind=? WHERE project_id=? AND kind=?",
                            (dst, b["id"], src))
                    conn.commit()
                    print("      已写库")

        orphans = [f for f in rep.findings if f.code == "orphan_node"]
        if orphans:
            print(f"    孤儿：{orphans[0].count} 个 —— **只报告，不删**"
                  f"（该补关系还是该撤回，是判断不是机械操作）")
        gdb.close()

    if not touched:
        print("没有可修的书（书名没匹配上 / 没建过图谱）")
    elif not apply:
        print("\n(dry-run 未写库；确认无误后加 --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
