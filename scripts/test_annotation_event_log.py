"""笔记/收藏变更日志（annotation_events）的离线测试。

钉住四件事：
1. 三个 API 动作（新建/修改/删除）都在事件表留痕；
2. 修改只记真正变化的字段，重复保存同样的值不产生事件；
3. 删除事件保留最终快照（原文 + 笔记），批注本体已不在；
4. agent 工具 annotation_event_log 可按动作/类型/书/关键词/分页读取。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import web                                              # noqa: E402
from aiohttp.test_utils import TestClient, TestServer                # noqa: E402

from api.routes.annotations import (                                 # noqa: E402
    handle_create_annotation, handle_delete_annotation,
    handle_update_annotation,
)
from services.library_service import record_import_event             # noqa: E402
from storage.db import BookDB                                        # noqa: E402
from storage.models import BookChapterRow, BookChunkRow, BookRow     # noqa: E402
from tools.library import _register_library_tools                    # noqa: E402
from tools.registry import ToolRegistry                              # noqa: E402

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

        text = "这是一段被收藏的原文，用来测试变更日志。"
        db.insert_book(BookRow(
            id="b1", title="窄门", author="纪德",
            format="epub", source_path="窄门.epub", chapter_count=1,
        ))
        db.insert_chapters([BookChapterRow(
            id="ch0", book_id="b1", chapter_index=0, title="第一章",
            content=text, char_count=len(text), chapter_type="body",
        )])
        db.insert_chunks([BookChunkRow(
            id="ck0", book_id="b1", chapter_index=0, chunk_index=0,
            content=text, char_count=len(text),
        )])
        record_import_event(
            db, db.get_book("b1"), source_filename="窄门.epub",
            metadata={"title": "窄门", "author": "纪德"},
            chapters_raw=[("第一章", text)],
        )

        app = web.Application()
        app["db"] = db
        app.router.add_post("/api/annotations", handle_create_annotation)
        app.router.add_put("/api/annotations/{ann_id}", handle_update_annotation)
        app.router.add_delete("/api/annotations/{ann_id}", handle_delete_annotation)

        async def run(client: TestClient) -> None:
            # ── 1. create ────────────────────────────────────────────
            resp = await client.post("/api/annotations", json={
                "book_id": "b1", "chapter_index": 0, "chunk_index": 0,
                "selected_text": text, "type": "highlight", "style": "box",
            })
            check("create returns 201", resp.status == 201, str(resp.status))
            ann_id = (await resp.json())["id"]
            rows, total = db.list_annotation_events(event_type="create")
            check("create event written", total == 1 and len(rows) == 1,
                  f"total={total}")
            ev = rows[0]
            check("create event fields", ev.annotation_type == "highlight"
                  and ev.style == "box" and ev.selected_text == text,
                  str(ev))
            check("create event has book title", ev.book_title == "窄门",
                  ev.book_title)
            check("create event links annotation",
                  ev.annotation_id == ann_id and ev.stable_book_id.startswith("sb-"),
                  f"{ev.annotation_id} / {ev.stable_book_id}")

            # ── 2. update：只记真正变化的字段 ────────────────────────
            resp = await client.put(f"/api/annotations/{ann_id}", json={
                "note_text": "我的笔记正文", "style": "underline",
            })
            check("update returns 200", resp.status == 200, str(resp.status))
            ups, up_total = db.list_annotation_events(event_type="update")
            check("update event written", up_total == 1 and len(ups) == 1,
                  f"total={up_total}")
            check("update records changed fields",
                  '"note_text"' in ups[0].changed_fields
                  and '"style"' in ups[0].changed_fields,
                  ups[0].changed_fields)
            check("update snapshot is new state",
                  ups[0].note_text == "我的笔记正文" and ups[0].style == "underline",
                  str(ups[0]))

            resp = await client.put(f"/api/annotations/{ann_id}", json={
                "note_text": "我的笔记正文",
            })
            _, up_total2 = db.list_annotation_events(event_type="update")
            check("same-value update does not add an event", up_total2 == 1,
                  str(up_total2))

            # ── 3. delete：保留最终快照 ──────────────────────────────
            resp = await client.delete(f"/api/annotations/{ann_id}")
            check("delete returns 200", resp.status == 200, str(resp.status))
            check("annotation row gone", db.get_annotation(ann_id) is None)
            dels, del_total = db.list_annotation_events(event_type="delete")
            check("delete event written", del_total == 1 and len(dels) == 1,
                  f"total={del_total}")
            check("delete keeps text snapshot",
                  dels[0].selected_text == text
                  and dels[0].note_text == "我的笔记正文", str(dels[0]))
            check("delete keeps book title", dels[0].book_title == "窄门",
                  dels[0].book_title)

            # ── 4. 过滤 / 分页 / 排序 ────────────────────────────────
            _, total_kind = db.list_annotation_events(annotation_type="highlight")
            check("filter by annotation_type", total_kind == 3, str(total_kind))
            by_book, _ = db.list_annotation_events(book_id="b1")
            check("filter by book_id", len(by_book) == 3, str(len(by_book)))
            q_rows, q_total = db.list_annotation_events(q="笔记正文")
            check("filter by q", q_total == 2 and len(q_rows) == 2, str(q_total))
            page, page_total = db.list_annotation_events(limit=2)
            check("pagination keeps pre-page total",
                  page_total == 3 and len(page) == 2,
                  f"total={page_total} len={len(page)}")
            newest, _ = db.list_annotation_events(limit=1)
            check("newest first", newest[0].event_type == "delete",
                  newest[0].event_type)

            # ── 5. agent 工具可读 ────────────────────────────────────
            registry = ToolRegistry()
            _register_library_tools(registry, db)
            spec = registry.get("annotation_event_log")
            check("tool registered", spec is not None)
            check("tool is read-only",
                  spec.permission_mode.name == "READ_ONLY",
                  spec.permission_mode.name)
            out = spec.execute(limit=5)
            check("tool output has all actions",
                  "新建收藏" in out and "修改收藏" in out and "删除收藏" in out
                  and "窄门" in out, out)
            check("tool output keeps deleted note text",
                  "我的笔记正文" in out, out)
            out_empty = spec.execute(q="不可能匹配的词")
            check("tool empty state", "没有匹配的变更记录" in out_empty, out_empty)

        async def scenario() -> None:
            async with TestClient(TestServer(app)) as client:
                await run(client)

        asyncio.run(scenario())
        db.close()

    print(f"\n{'=' * 40}\nPASS {PASS}  FAIL {FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
