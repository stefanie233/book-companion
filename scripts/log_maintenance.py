"""日志生命周期维护：给 data/logs 与两类 DB 日志回收年龄（决策 147）。

默认**演练**，只打印将删除什么、能腾多少空间；加 ``--apply`` 才动文件。

覆盖三类：
  1. 文本轮转文件（``*.log.1``、``bookgraph-*.jsonl``）与过期的一次性产物
     （``reading_*.log`` / ``server_*.log`` 等）—— 只按 mtime 判，内容不作判断。
  2. 飞行记录仪 ``data/context_log.db`` 的 ``context_snapshots``：按 created_at
     删掉过期轮次，``--vacuum`` 时尝试把空间还给磁盘（服务在跑会拿不到排他锁，
     失败只报告，不报错退出）。
  3. 各 ``data/graphs/graph_*.db`` 的 ``graph_llm_logs``：全文提示词/响应，
     按 created_at 回收。

额外的 ``--purge-test-usage`` 清理 ``llm_usage_log`` 里 model 以 fake- 开头的
行 —— 那是测试跑进生产库的污染（用量分析里它们按真实调用计费口径统计）。

用法::

    python scripts/log_maintenance.py                    # 演练
    python scripts/log_maintenance.py --apply            # 执行
    python scripts/log_maintenance.py --apply --vacuum   # 回收后压缩 DB
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from log_lifecycle import log_dir, prune_rotated  # noqa: E402

#: 常驻日志：即便长期没写入也不当「一次性产物」删掉。
_ACTIVE_LOGS = {
    "api.log", "memory.log", "providers.log", "reader_sync.log",
    "bookgraph.jsonl",
}


def _mb(n: float) -> str:
    """人读的体积：小于 1MB 用 KB（这些小日志按 MB 打全是 0.0）。"""
    if n < 1024:
        return f"{n:.0f}B"
    if n < 1048576:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1048576:.1f}MB"


def _age_days(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 86400


def _text_logs(days: int, *, apply: bool) -> tuple[int, int]:
    """轮转文件 + 过期的一次性产物。返回 (个数, 字节)。"""
    root = log_dir()
    # 先演练一次拿到清单（prune_rotated 在 apply 时会直接删，拿不到体积）。
    targets = prune_rotated(days=days, log_path=root, dry_run=True)
    for p in root.rglob("*"):
        if not p.is_file() or p in targets:
            continue
        name = p.name
        if name in _ACTIVE_LOGS:
            continue                    # 常驻日志不按「产物」删
        if name.rsplit(".", 1)[-1].isdigit():
            continue                    # 轮转份已由上面接管
        try:
            if _age_days(p) >= days:
                targets.append(p)
        except OSError:
            continue
    live = [p for p in targets if p.exists()]
    freed = sum(p.stat().st_size for p in live)
    for p in live:
        size = p.stat().st_size
        print(f"  {'删除' if apply else '将删'} "
              f"{p.relative_to(ROOT)}  {_mb(size)}  {_age_days(p):.0f}天前")
    if apply:
        for p in live:
            try:
                p.unlink()
            except OSError as exc:
                print(f"    失败 {p.name}: {exc}")
    return len(live), freed


def _db_logs(path: Path, table: str, days: int, *, apply: bool,
             vacuum: bool, text_cols: tuple[str, ...]) -> tuple[int, int]:
    """按 created_at 删除 *table* 里超过 *days* 天的行。

    返回 (行数, 字节)。演练时字节是**估算**：把待删行的文本列字符数
    按 3 字节/字算上界（中文正文为主，SQLite 的 length() 数的是字符）。
    """
    if not path.exists():
        return 0, 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    before = path.stat().st_size
    conn = sqlite3.connect(str(path))
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "created_at" not in cols:
            print(f"  跳过 {path.name}: {table} 没有 created_at 列")
            return 0, 0
        usable = [c for c in text_cols if c in cols]
        size_expr = "+".join(f"length({c})" for c in usable) or "0"
        n, chars = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM({size_expr}), 0) FROM {table} "
            "WHERE created_at < ?", (cutoff,)
        ).fetchone()
        if not n:
            return 0, 0   # 0 行不刷屏：26 个图库大多没有过期数据
        print(f"  {'删除' if apply else '将删'} {path.name}:{table} "
              f"{n} 行 ≈{_mb(chars * 3)}（早于 {cutoff} UTC，保留 {days} 天）")
        if apply:
            conn.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
            conn.commit()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass        # 有别的进程在读，下次再收
            if vacuum:
                try:
                    conn.execute("VACUUM")
                except sqlite3.OperationalError as exc:
                    print(f"    VACUUM 跳过（{exc}；服务在跑时属正常）")
            after = path.stat().st_size
            return n, max(before - after, 0)
        return n, chars * 3
    finally:
        conn.close()


def _purge_test_usage(*, apply: bool) -> int:
    """删掉 llm_usage_log 里测试模型的行（fake-model 等）。

    测试进程曾经直接写生产库：``test_graph_e2e`` 走 orchestrator 时
    ``usage_log.record`` 落进 data/sessions.db，用量分析于是把 fake-model
    当真调用统计。源头已在测试里隔离（决策 147），这里清历史残留。
    """
    path = ROOT / "data" / "sessions.db"
    if not path.exists():
        return 0
    conn = sqlite3.connect(str(path))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM llm_usage_log WHERE model LIKE 'fake-%'"
        ).fetchone()[0]
        print(f"  {'删除' if apply else '将删'} llm_usage_log 中 fake-* 模型 "
              f"{n} 行（测试污染）")
        if apply and n:
            conn.execute("DELETE FROM llm_usage_log WHERE model LIKE 'fake-%'")
            conn.commit()
        return n
    finally:
        conn.close()


def _inventory() -> None:
    root = log_dir()
    files = sorted(
        (p for p in root.rglob("*") if p.is_file()),
        key=lambda p: p.stat().st_size, reverse=True)
    print("当前占用：")
    total = 0
    for p in files:
        size = p.stat().st_size
        total += size
        print(f"  {_mb(size):>9}  {p.relative_to(ROOT)}")
    print(f"  {_mb(total):>9}  data/logs 合计")
    ctx = ROOT / "data" / "context_log.db"
    graphs = list((ROOT / "data" / "graphs").glob("graph_*.db"))
    print(f"  {_mb(sum(g.stat().st_size for g in graphs)):>9}  graphs/*.db"
          f"（{len(graphs)} 个图库）")
    if ctx.exists():
        print(f"  {_mb(ctx.stat().st_size):>9}  data/context_log.db")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="真正删除（默认演练）")
    ap.add_argument("--days", type=int, default=30,
                    help="文本轮转与一次性产物的保留天数（默认 30）")
    ap.add_argument("--context-days", type=int, default=30,
                    help="飞行记录仪保留天数（默认 30）")
    ap.add_argument("--graph-days", type=int, default=90,
                    help="graph_llm_logs 保留天数（默认 90）")
    ap.add_argument("--vacuum", action="store_true",
                    help="删除后尝试 VACUUM（服务在跑时可能拿不到锁）")
    ap.add_argument("--purge-test-usage", action="store_true",
                    help="顺带清 llm_usage_log 里 fake-* 的测试行")
    ap.add_argument("--skip-db", action="store_true",
                    help="只清文本与用量，不动 context_log / graph_llm_logs")
    args = ap.parse_args()

    print(f"{'执行' if args.apply else '演练（加 --apply 才删除）'}\n")
    _inventory()

    print("\n── 文本日志 ──")
    n_files, freed = _text_logs(args.days, apply=args.apply)

    n_ctx = ctx_freed = graph_freed = graph_rows = 0
    if args.skip_db:
        print("\n── DB 日志 ──\n  已跳过（--skip-db）")
    else:
        print("\n── 飞行记录仪 ──")
        n_ctx, ctx_freed = _db_logs(
            ROOT / "data" / "context_log.db", "context_snapshots",
            args.context_days, apply=args.apply, vacuum=args.vacuum,
            text_cols=("system_prompt", "messages_json", "response_text",
                       "tool_results_json"))

        print("\n── 图谱 LLM 日志 ──")
        for db in sorted((ROOT / "data" / "graphs").glob("graph_*.db")):
            rows, size = _db_logs(
                db, "graph_llm_logs", args.graph_days, apply=args.apply,
                vacuum=False,
                text_cols=("prompt", "response", "reasoning_content"))
            graph_rows += rows
            graph_freed += size

    n_usage = 0
    if args.purge_test_usage:
        print("\n── 用量日志 ──")
        n_usage = _purge_test_usage(apply=args.apply)

    verb = "已删" if args.apply else "将删"
    print(f"\n完成：{verb} 文件 {n_files} 个、飞行记录仪 {n_ctx} 轮、"
          f"图谱日志 {graph_rows} 行"
          + (f"、测试用量 {n_usage} 行" if args.purge_test_usage else "")
          + f"；预计释放 {_mb(freed + ctx_freed + graph_freed)}")
    if args.apply and not args.vacuum and (n_ctx or graph_rows):
        print("提示：加 --vacuum 才能真正把 DB 腾出的空间还给磁盘。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

