"""素材库维护：盘点（只读）与回收站清理（默认演练）。

决策 157：维护动作显式触发，不在启动时自动跑。
``--audit`` 报告未消费、近重复、坏抬头、回收站积压；
``--purge-recycled`` 默认只打印会删什么，``--apply`` 才真的删。
回收站默认保留 90 天，在架卡片永远不动。

    python scripts/maintain_materials.py --audit
    python scripts/maintain_materials.py --audit --unused-days 180
    python scripts/maintain_materials.py --purge-recycled --days 90
    python scripts/maintain_materials.py --purge-recycled --days 90 --apply
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import load_config                                  # noqa: E402
from storage.writing_db import WritingDB                       # noqa: E402
from writegraph import materials as mat                        # noqa: E402


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def show_audit(root: Path, *, unused_days: int,
               use_counts: dict[str, int]) -> int:
    data = mat.audit(root, use_counts=use_counts, unused_days=unused_days)
    print(f"素材库：{root}")
    print(f"  在架 {data['total']} 张 · 用过 {data['used']} · "
          f"未消费 {data['unused']}（其中 {unused_days} 天未动 "
          f"{data['unused_over_days']}）")
    recycled = data["recycled"]
    print(f"  回收站 {recycled['count']} 项 / {_fmt_bytes(recycled['bytes'])}"
          f" · 超过 {recycled['retention_days']} 天 {recycled['overdue']} 项")
    if data.get("pending_count"):
        print(f"  待归类 {data['pending_count']} 张 · 超过 {unused_days} 天未加工 "
              f"{data.get('pending_over_days', 0)}")
    if data["broken_count"]:
        print(f"\n坏抬头 {data['broken_count']} 张：")
        for row in data["broken"]:
            print(f"  - {row['path']}（{row['why']}）")
    if data["duplicate_id_count"]:
        print(f"\n身份重复 {data['duplicate_id_count']} 组：")
        for row in data["duplicate_ids"]:
            print(f"  - {row['material_id']}: {', '.join(row['paths'])}")
    if data["identity_mismatch_count"]:
        print(f"\n原文被改过、身份对不上 {data['identity_mismatch_count']} 张"
              "（改回原文或另收一张）：")
        for row in data["identity_mismatch"]:
            print(f"  - {row['path']}（{row['material_id']}）")
    if data["near_duplicate_count"]:
        print(f"\n近重复候选 {data['near_duplicate_count']} 组"
              "（只报告，不自动合并）：")
        for row in data["near_duplicates"]:
            print(f"  - {row['why']} · {row['prefix'][:30]}")
            print(f"    {', '.join(row['paths'])}")
            if row.get("recycle"):
                print(f"    建议保留：{row['keep']}；可回收：{row['recycle']}")
    if data.get("pending_over_days"):
        print(f"\n待归类超过 {unused_days} 天未加工 {data['pending_over_days']} 张"
              "（只报告，不自动处理）：")
        for path in data.get("pending_over_sample") or []:
            print(f"  - {path}")
    samples = data.get("samples") or {}
    if samples.get("stale"):
        print(f"\n最久未动的几张（前 {len(samples['stale'])}）：")
        for path in samples["stale"]:
            print(f"  - {path}")
    bad = (data["broken_count"] + data["duplicate_id_count"]
           + data["identity_mismatch_count"])
    return 1 if bad else 0


def show_purge(root: Path, *, days: int, apply: bool) -> int:
    now_ms = time.time() * 1000
    items = [row for row in mat.list_recycled(root)
             if row.get("deleted_at")
             and now_ms - int(row["deleted_at"]) > days * 86400000]
    if not items:
        print(f"回收站里没有超过 {days} 天的条目。")
        return 0
    print(f"回收站超过 {days} 天的条目：{len(items)} 项")
    for row in items[:30]:
        print(f"  - {row['name']} · {row['original'] or row['path']}")
    if len(items) > 30:
        print(f"  … 另有 {len(items) - 30} 项")
    if not apply:
        print("\n演练模式：没有删除。确认后加 --apply。")
        return 0
    result = mat.purge_recycled(root, days=days)
    print(f"\n已清理 {result['count']} 项。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", action="store_true", help="盘点（只读）")
    parser.add_argument("--purge-recycled", action="store_true",
                        help="清理回收站里超过保留期的条目")
    parser.add_argument("--days", type=int, default=mat.RECYCLE_RETENTION_DAYS,
                        help=f"保留天数，默认 {mat.RECYCLE_RETENTION_DAYS}")
    parser.add_argument("--unused-days", type=int, default=90,
                        help="未消费多少天算久，默认 90")
    parser.add_argument("--apply", action="store_true",
                        help="真的删除（不加只演练）")
    args = parser.parse_args()
    if not (args.audit or args.purge_recycled):
        parser.print_help()
        return 2
    cfg = load_config(ROOT)
    root = mat.ensure_library(mat.library_root(cfg.project_dir))
    code = 0
    if args.audit:
        db = WritingDB(cfg.writing_db_path)
        try:
            counts = {mid: int(row.get("events") or 0)
                      for mid, row in db.material_use_counts().items()}
        finally:
            db.close()
        code = show_audit(root, unused_days=args.unused_days,
                          use_counts=counts)
    if args.purge_recycled:
        code = max(code, show_purge(root, days=args.days, apply=args.apply))
    return code


if __name__ == "__main__":
    sys.exit(main())
