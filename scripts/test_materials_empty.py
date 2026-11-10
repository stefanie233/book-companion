"""清空素材库文件夹（逐张进回收站，保留文件夹与子分类）的测试。

只碰临时目录，零外网。跑法：``python scripts/test_materials_empty.py``。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.writing_service import WritingService                  # noqa: E402
from storage.writing_db import WritingDB                            # noqa: E402
from writegraph import materials as mat                             # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"ok   {label}")
    else:
        FAILED += 1
        print(f"FAIL {label}\n     期望 {want!r}\n     实际 {got!r}")


def section(name: str) -> None:
    print(f"\n-- {name}")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        config = SimpleNamespace(
            project_dir=str(tmp_path),
            writing_db_path=str(tmp_path / "data" / "writing.db"),
            books_db_path=str(tmp_path / "data" / "books.db"),
        )
        db = WritingDB(config.writing_db_path)
        svc = WritingService(config, db=db)
        root = svc.materials_root()

        section("准备")
        a = svc.materials_intake(
            excerpt="甲：雨落在铁皮棚上，一直落到后半夜。",
            title="甲", category="场景/天气")
        b = svc.materials_intake(
            excerpt="乙：梧桐叶落了一地，扫帚靠在门边。",
            title="乙", category="场景/天气/夜")
        c = svc.materials_intake(
            excerpt="丙：码头的风把盐味吹进衣领。",
            title="丙", category="场景/其他")
        check("主分类文件落了", (root / str(a["path"])).is_file(), True)
        check("子分类也有卡", (root / str(b["path"])).is_file(), True)
        check("兄弟分类不动", (root / str(c["path"])).is_file(), True)

        section("清空")
        res = svc.materials_empty("场景/天气")
        check("清了两张", res.get("count"), 2)
        check("主文件没了", (root / str(a["path"])).exists(), False)
        check("子分类文件没了", (root / str(b["path"])).exists(), False)
        check("兄弟分类还在", (root / str(c["path"])).is_file(), True)
        check("文件夹保留", (root / "场景" / "天气").is_dir(), True)
        check("子分类保留", (root / "场景" / "天气" / "夜").is_dir(), True)
        again = svc.materials_empty("场景/天气")
        check("再清一次是 0", again.get("count"), 0)

        section("回收站记录")
        items = svc.materials_recycled()["items"]
        check("两张都有记录", len(items), 2)
        check("记得原路径", sorted(i["original"] for i in items),
              sorted([a["path"], b["path"]]))
        check("记录是文件", sorted(i["kind"] for i in items), ["file", "file"])

        section("恢复")
        restored = [svc.materials_restore(i["path"]).get("restored")
                    for i in items]
        check("回到原路径", sorted(restored), sorted([a["path"], b["path"]]))
        check("主卡回来了", (root / str(a["path"])).is_file(), True)
        check("子分类卡回来了", (root / str(b["path"])).is_file(), True)
        check("回收站空了", len(svc.materials_recycled()["items"]), 0)

        section("坏路径")
        for bad, phrase in (("场景/不存在", "文件夹不存在"),
                            ("_回收站", "回收站是只读的"),
                            (str(a["path"]), "不是目录")):
            try:
                svc.materials_empty(bad)
                err = ""
            except Exception as exc:                 # noqa: BLE001
                err = str(exc)
            check(f"{bad} 被拒", phrase in err, True)

        section("不碰别的分类")
        check("丙还在", (root / str(c["path"])).is_file(), True)
        check("总卡数不变", len(mat.list_cards(root)), 3)

        db.close()

    print("\n==== summary ====")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
