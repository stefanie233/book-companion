"""书籍在库状态日志（import_log）的离线测试。

钉住四件事：
1. 入库/删除两条动作都落进 import_log，删除不再因「没有活动行」而漏记；
2. 同一 book_id 重导复用稳定书号、import_count 累加，不会分叉出新身份；
3. 查询支持动作/书号/稳定书号/关键词过滤与分页，总数是分页前的；
4. agent 工具 library_event_log 可读，输出带动作与在库数量。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.library_service import delete_book, record_import_event   # noqa: E402
from storage.db import BookDB                                            # noqa: E402
from storage.models import BookRow                                      # noqa: E402
from tools.library import _register_library_tools                       # noqa: E402
from tools.registry import ToolRegistry                                  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok   ", name)
    else:
        FAIL += 1
        print("FAIL ", name, detail)


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = BookDB(Path(td) / "books.db")
        db.initialize()
        db.insert_book(BookRow(
            id="b1", title="窄门", author="纪德",
            format="epub", source_path="窄门.epub", chapter_count=1,
        ))
        db.insert_book(BookRow(
            id="b2", title="地下室手记", author="陀思妥耶夫斯基",
            format="epub", source_path="地下室手记.epub", chapter_count=1,
        ))

        # ── 1. 入库事件 ──────────────────────────────────────────────
        body = "第一章的正文从这里开始。" * 12
        ev1 = record_import_event(
            db, db.get_book("b1"),
            source_filename="窄门.epub",
            metadata={"title": "窄门", "author": "纪德", "publisher": "某社"},
            chapters_raw=[("第一章", body)],
        )
        check("import event written", ev1.event_type == "import"
              and ev1.in_library == 1, str(ev1))
        check("stable id generated", ev1.stable_book_id.startswith("sb-"),
              ev1.stable_book_id)
        check("first chapter captured", ev1.first_chapter == "第一章",
              repr(ev1.first_chapter))
        rows, total = db.list_import_log_events()
        check("log has 1 row", total == 1 and len(rows) == 1, f"{total}")

        # ── 2. 重导：复用稳定书号，计数累加 ──────────────────────────
        ev2 = record_import_event(
            db, db.get_book("b1"),
            source_filename="窄门.epub",
            metadata={"title": "窄门", "author": "纪德"},
            chapters_raw=[("第一章", body)],
        )
        check("reimport reuses stable id", ev2.stable_book_id == ev1.stable_book_id,
              f"{ev1.stable_book_id} -> {ev2.stable_book_id}")
        check("import_count increments", ev2.import_count == 2,
              str(ev2.import_count))

        # ── 3. 删除：有活动行 → 保留稳定书号与元数据 ─────────────────
        summary = delete_book(db, "b1")
        check("book gone after delete", db.get_book("b1") is None)
        delete_ev, _ = db.list_import_log_events(book_id="b1", event_type="delete")
        check("delete event written", len(delete_ev) == 1, str(delete_ev))
        check("delete keeps stable id",
              delete_ev[0].stable_book_id == ev1.stable_book_id,
              delete_ev[0].stable_book_id)
        check("delete keeps title/author", delete_ev[0].title == "窄门"
              and delete_ev[0].author == "纪德", str(delete_ev[0]))
        check("delete marks out-of-library", delete_ev[0].in_library == 0)
        check("delete summary intact", summary["title"] == "窄门")

        # ── 4. 删除：没有 import_log 的书也要记一笔 ──────────────────
        delete_book(db, "b2")
        b2_ev, _ = db.list_import_log_events(book_id="b2", event_type="delete")
        check("delete without import_log still logged", len(b2_ev) == 1,
              str(b2_ev))
        check("fallback stable id is empty", b2_ev[0].stable_book_id == "",
              repr(b2_ev[0].stable_book_id))

        # ── 5. 过滤 / 分页 / 排序 ─────────────────────────────────────
        _, imports = db.list_import_log_events(event_type="import")
        check("filter by event_type", imports == 2, str(imports))
        matches, _ = db.list_import_log_events(q="窄门")
        check("filter by title substring", len(matches) == 3, str(len(matches)))
        page, total_b1 = db.list_import_log_events(book_id="b1", limit=2)
        check("pagination keeps pre-page total", total_b1 == 3 and len(page) == 2,
              f"total={total_b1} len={len(page)}")
        page2, _ = db.list_import_log_events(book_id="b1", limit=2, offset=2)
        check("pagination offset", len(page2) == 1, str(len(page2)))
        newest, _ = db.list_import_log_events(limit=1)
        check("newest first", newest[0].book_id == "b2", str(newest[0]))
        active, _ = db.list_import_log_events(in_library=1)
        check("filter by in_library", len(active) == 2, str(len(active)))

        # ── 6. agent 工具可读 ───────────────────────────────────────
        registry = ToolRegistry()
        _register_library_tools(registry, db)
        spec = registry.get("library_event_log")
        check("tool registered", spec is not None)
        check("tool is read-only",
              spec.permission_mode.name == "READ_ONLY", spec.permission_mode.name)
        out = spec.execute(event_type="delete", limit=5)
        check("tool output has delete entries",
              "删除" in out and "窄门" in out and "地下室手记" in out, out)
        out_all = spec.execute(limit=5)
        check("tool header reports in-library count",
              "当前在库 0 本" in out_all, out_all)
        db.close()

    print(f"\n{'=' * 40}\nPASS {PASS}  FAIL {FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
