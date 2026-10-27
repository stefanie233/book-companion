"""独立进程驱动一次真实的正文提取。**不要从 api_server 进程里跑。**

为什么必须独立进程：``api_server`` 进程内跑 orchestrator 会冻住事件循环
（``getaddrinfo`` 走 executor，与 SSE 的事件循环互相等）。服务内的提取走的是
另一条路（``api_server`` 的 graph 端点）；这个脚本是**真实链路验证**用的
驱动壳，跑在它自己的进程里，不碰服务。

跑法：
  python scripts/run_graph_extraction.py 我与地坛
  python scripts/run_graph_extraction.py 我与地坛 --no-verify     # 关掉回炉环
  python scripts/run_graph_extraction.py 我与地坛 --batch-size 4
  python scripts/run_graph_extraction.py --list                   # 列出可跑的书

跑完会打印每一批的节点/边/引文数、被裁的块、划出的叙事域、以及**未经验证的
批次**（沉默不是成功 —— 没验干净必须看得见）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# line_buffering：重定向到文件时 Python 默认块缓冲，跑到一半去看是空的 ——
# 而「跑到哪了」正是这个脚本存在的意义。
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from bookgraph import GraphOrchestrator                        # noqa: E402
from config import Config                                      # noqa: E402
from storage.db import BookDB                                  # noqa: E402
from storage.graph_db import GraphDB, resolve_graph_db_path    # noqa: E402

ROOT = Path(__file__).parent.parent


def _pick(books_db: BookDB, needle: str):
    rows = books_db.get_conn().execute(
        "SELECT id, title FROM books ORDER BY title").fetchall()
    hits = [r for r in rows if needle in (r["title"] or "") or r["id"] == needle]
    if not hits:
        print(f"没有匹配「{needle}」的书")
        return None
    if len(hits) > 1:
        print(f"「{needle}」匹配到 {len(hits)} 本，请给更具体的名字：")
        for r in hits:
            print(f"  {r['title'][:40]}  ({r['id']})")
        return None
    return hits[0]


def _load_config() -> Config:
    """跟服务走同一条选路：控制台绑定优先，`.env` 兜底。

    光 `Config()` 拿不到用户在控制台配的连接（那些在
    ``data/config/providers.json`` 里），直接跑会因为没有 api_key 而全批失败。
    """
    cfg = Config()
    try:
        from provider_store import SLOT_MAIN_LLM, ProviderStore
        store = ProviderStore(ROOT / "data" / "config" / "providers.json")
        r = store.resolve(SLOT_MAIN_LLM, cfg)
        if r.key:
            cfg.api_key, cfg.api_base = r.key, r.base_url
            if r.model:
                cfg.model = r.model
        print(f"模型：{r.model or cfg.model}（来源 {r.source}"
              + (f" · {r.provider_name}" if r.provider_name else "") + "）")
    except Exception as exc:
        print(f"（控制台连接读取失败，退回 .env：{exc}）")
    if not cfg.api_key:
        print("**没有可用的 api_key** —— 先在控制台配一个连接，或设 .env。")
    return cfg


async def _run(book_id: str, title: str, batch_size: int, verify: bool,
               rounds: int = 3) -> int:
    cfg = _load_config()
    books_db = BookDB(ROOT / "data" / "books.db")
    gdb_path = resolve_graph_db_path(cfg, books_db, book_id)
    if not gdb_path:
        print("解析不出图谱库路径")
        return 2
    gdb = GraphDB(Path(gdb_path))
    print(f"书：{title}  ({book_id})")
    print(f"图谱：{gdb_path}")
    print(f"回炉环：{'开' if verify else '关'} · 批大小 {batch_size} · "
          f"回炉上限 {rounds} 轮\n")

    def _on_event(e: dict) -> None:
        t = e.get("type")
        if t == "batch_done":
            d = e.get("detail") or {}
            print(f"  [{e['batch_index']}/{e['total_batches']}] "
                  f"{e.get('step_label', '')[:28]}  节点 {d.get('new_nodes', 0)} "
                  f"边 {d.get('new_edges', 0)} 引文 {d.get('new_quotes', 0)}")
        elif t == "batch_unverified":
            v = e.get("verify") or {}
            print(f"  !! 第 {e['batch_index']} 批**未经验证**"
                  f"（{v.get('verdict')}，回炉 {v.get('rounds', 0)} 轮，"
                  f"残余 {v.get('residual', 0)} 条）")
        elif t == "scopes_planned":
            print(f"  叙事域：{e.get('scope_count')} 篇 "
                  f"{'、'.join((e.get('labels') or [])[:6])}")
        elif t == "content_filtered":
            print(f"  输入裁剪：裁掉 {e.get('excluded_count', 0)} 块")
        elif t == "relink":
            print(f"  锚点重连：{e.get('resolved')}/{e.get('total')} 可用")
        elif t in ("scout_done", "context_set", "head_scan"):
            print(f"  {t}")

    try:
        orch = GraphOrchestrator(cfg, books_db, gdb, gdb_path)
        res = await orch.run(book_id, batch_size=batch_size,
                             progress_cb=_on_event, verify=verify,
                             verify_rounds=rounds)
    finally:
        gdb.close()
        books_db.close()

    print(f"\n完成：{res.chunks_done}/{res.total_chunks} 块 · "
          f"失败 {res.chunks_failed}")
    print(f"图谱：{res.final_stats}")
    return 1 if res.chunks_failed else 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    books_db = BookDB(ROOT / "data" / "books.db")
    if "--list" in sys.argv or not args:
        for r in books_db.get_conn().execute(
                "SELECT id, title FROM books ORDER BY title"):
            print(f"  {r['title'][:44]:<46} {r['id']}")
        books_db.close()
        return 0
    hit = _pick(books_db, args[0])
    books_db.close()
    if hit is None:
        return 2
    bs = 5
    if "--batch-size" in sys.argv:
        bs = int(sys.argv[sys.argv.index("--batch-size") + 1])
    rounds = 3
    if "--verify-rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--verify-rounds") + 1])
    return asyncio.run(_run(hit["id"], hit["title"] or "", bs,
                            "--no-verify" not in sys.argv, rounds))


if __name__ == "__main__":
    raise SystemExit(main())
