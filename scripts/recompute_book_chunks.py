"""独立通道：只重算某本书的 book_chunks，**完全不碰 book_chapters**。

章号不变，所以批注（坐标在章级）、阅读进度、图谱的章级引用都不受影响。
分块算法与导入完全同一个（`_build_chunks_for_chapter`），所以对未经改动的书
重算结果是恒等的 —— 全库实测 4447 块逐字节相同、0 换主键。

主键策略见 `services.library_service._plan_chunk_rebuild`：同坐标同内容复用旧
id（图谱不断链），内容真变了才换新 id（图谱当作待重跑的新块）。**换主键是有
代价的** —— 图谱库把 chunk_id 当主键，所以本脚本把 `换主键` 和 `消失坐标` 都
报出来，apply 前先看一眼。

默认 dry-run，--apply 才写库。

用法:
  python scripts/recompute_book_chunks.py                 # 全部书，只看
  python scripts/recompute_book_chunks.py 人类简史          # 按书名子串
  python scripts/recompute_book_chunks.py 人类简史 --apply  # 真写
  python scripts/recompute_book_chunks.py --all --apply
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 控制台默认 GBK，书名/中文标点可能触发 UnicodeEncodeError 把写库那步一起带走。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import load_config                      # noqa: E402
from services.library_service import recompute_book_chunks  # noqa: E402
from storage.db import BookDB                       # noqa: E402


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


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv

    cfg = load_config()
    db = BookDB(Path(cfg.books_db_path))
    db.initialize()          # 跑待应用迁移（如 v23 的 heading_tag / in_toc）

    targets = pick(db, args)
    print(f"{'APPLY' if apply else 'DRY-RUN'}  {len(targets)} 本书\n")

    dirty = 0
    for bid, title in targets:
        s = recompute_book_chunks(db, bid, dry_run=not apply)
        clean = s["new_pk"] == 0 and not s["disappeared"]
        dirty += not clean
        print(f"{'OK ' if clean else '!! '}{str(title)[:26]:28} "
              f"章{s['chapters']:>4}  块 {s['old_chunks']:>4} -> {s['new_chunks']:<4} "
              f"逐字节复用{s['identical_pk']:>5}  等价保留{s['equivalent_kept']:>3}  "
              f"换主键{s['new_pk']:>3}  消失{len(s['disappeared']):>3}"
              f"{'   零差异（幂等）' if clean else ''}")
        if s["disappeared"]:
            head = ", ".join(f"{c}.{k}" for c, k in s["disappeared"][:10])
            more = "" if len(s["disappeared"]) <= 10 else f" …另 {len(s['disappeared']) - 10} 个"
            print(f"     消失坐标: {head}{more}")

    print(f"\n有差异: {dirty} 本")
    if not apply:
        print("(dry-run 未写库；确认无误后加 --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
