"""全局素材库（决策 156）的测试。

零外网：只碰临时目录与临时 SQLite。跑法：
``python scripts/test_materials_library.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.writing_service import WritingService                  # noqa: E402
from storage.writing_db import WritingDB                            # noqa: E402
from tools.registry import ToolRegistry                             # noqa: E402
from writegraph import materials as mat                             # noqa: E402
from writegraph.chat_tools import build_writing_registry            # noqa: E402

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


def make_books_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute(
        "CREATE TABLE book_chunks (id TEXT PRIMARY KEY, book_id TEXT,"
        " chapter_index INTEGER, chunk_index INTEGER, content TEXT,"
        " char_count INTEGER)")
    conn.execute("CREATE TABLE book_chapters (book_id TEXT,"
                 " chapter_index INTEGER, title TEXT)")
    conn.execute("INSERT INTO books VALUES ('b1','窄门')")
    conn.execute("INSERT INTO book_chapters VALUES ('b1',12,'诀别')")
    conn.execute(
        "INSERT INTO book_chunks VALUES ('c1','b1',12,3,?,120)",
        ("我写信告诉你，我不再爱你了；但愿你永远不知道，"
         "这封信写完之后我在窗边站了多久。",))
    # 多段块：段落级索引拿它试（原文用 \r\n\r\n，与真实入库一致）。
    conn.execute(
        "INSERT INTO book_chunks VALUES ('c2','b1',12,4,?,400)",
        ("来信收到了。\r\n\r\n"
         "我不再爱你了；但愿你永远不知道，\r\n"
         "这封信写完之后我在窗边站了多久。\r\n\r\n"
         "窗外的梧桐落了一地。\r\n\r\n"
         "后来我把信烧了。",))
    conn.commit()
    conn.close()


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        books_db = tmp_path / "data" / "books.db"
        books_db.parent.mkdir(parents=True, exist_ok=True)
        make_books_db(books_db)
        config = SimpleNamespace(
            project_dir=str(tmp_path),
            writing_db_path=str(tmp_path / "data" / "writing.db"),
            books_db_path=str(books_db),
        )
        db = WritingDB(config.writing_db_path)
        svc = WritingService(config, db=db)

        section("根目录")
        root = svc.materials_root()
        check("库在 data/writing/materials", root.name, "materials")
        check("种下默认模块", (root / "情感").is_dir(), True)
        check("没有种进写作 git 的工作区", (root / "作品记忆.md").exists(), False)

        section("收录：作者点名（书 chunk）")
        row = svc.materials_intake(
            source="book", chunk_id="c1", title="诀别信",
            category="情感/爱情/离别", tags="书信,诀别,克制",
            description="以信仰为由拒绝爱人的一封信；几乎没有直接抒情。",
            origin="作者指令",
            instruction="把窄门里那段诀别信收进素材")
        mid = row.get("material_id")
        check("有身份", bool(mid), True)
        check("落进分类", row.get("path").startswith("情感/爱情/离别/"), True)
        check("文件名带哈希", mid in str(row.get("path")), True)
        card = svc.materials_read(str(row.get("path")))
        check("取到逐字原文", "我不再爱你了" in card["content"], True)
        check("带分类", card["category"], "情感/爱情/离别")
        check("带标签", "诀别" in card["tags"], True)
        check("说明不写适用情景", "适用于" in card["description"], False)
        check("存作者原话", card["instruction"], "把窄门里那段诀别信收进素材")

        section("段落级索引")
        check("CRLF 空行也切段",
              len(mat.split_paragraphs("甲\r\n\r\n乙\r\n\r\n\r\n丙")), 3)
        check("没有空行就是一段", len(mat.split_paragraphs("甲\n乙")), 1)
        whole = svc.materials_read_chunk(chunk_id="c2")
        check("块里 4 段", whole["count"], 4)
        check("段落带编号", [p["n"] for p in whole["paragraphs"]], [1, 2, 3, 4])
        check("提示怎么用", "para_start" in whole["hint"], True)
        span = svc.materials_intake(
            source="book", chunk_id="c2", para_start=2, para_end=3,
            title="只收信的一段", category="情感/爱情/离别")
        check("只收第 2–3 段", span.get("para_span"), "第 2–3 段")
        check("总段数回传", span.get("para_count"), 4)
        slice_card = svc.materials_read(str(span.get("path")))
        check("原文从库里切，不含首尾段",
              "来信收到了" in slice_card["excerpt"], False)
        check("拿到了中间两段", "窗外的梧桐" in slice_card["excerpt"], True)
        check("出处标了段落", "第 2–3 段" in slice_card["source_line"], True)
        check("不是整块身份", span.get("material_id") != mid, True)
        again_span = svc.materials_intake(
            source="book", chunk_id="c2", para_start=2, para_end=3,
            title="只收信的一段", category="情感/爱情/离别")
        check("同段重复收录去重", again_span.get("exists"), True)
        single = svc.materials_intake(
            source="book", chunk_id="c2", para_start=3,
            title="窗外的梧桐", category="场景/天气")
        check("只给起点收一段", single.get("para_span"), "第 3 段")
        try:
            svc.materials_intake(source="book", chunk_id="c2", para_start=5)
            bad_range = ""
        except Exception as exc:                     # noqa: BLE001
            bad_range = str(exc)
        check("越界报错带总段数", "只有 4 段" in bad_range, True)

        section("去重")
        again = svc.materials_intake(
            source="book", chunk_id="c1", title="诀别信",
            category="情感/爱情/离别")
        check("同摘要同出处不重复", again.get("exists"), True)
        check("返回原路径", again.get("path"), row.get("path"))

        section("检索")
        found = svc.materials_search("诀别")
        check("搜到卡", found.get("count"), 1)
        check("命中带身份", found["hits"][0].get("material_id"), mid)
        check("搜不到", svc.materials_search("火星殖民")["count"], 0)

        section("页面编辑")
        edited = svc.materials_write(
            str(row.get("path")),
            card["content"].replace("# 诀别信", "# 诀别信（改）"))
        check("页面可编辑正文", bool(edited.get("path")), True)
        check("标题改掉了",
              "诀别信（改）" in svc.materials_read(
                  str(row.get("path")))["content"], True)
        try:
            svc.materials_write(str(row.get("path")), "# 只剩标题\n")
            broken = False
        except Exception as exc:                     # noqa: BLE001
            broken = "抬头" in str(exc)
        check("编辑不能删抬头", broken, True)

        section("消费连线")
        work_id = svc.create_work("潮汐")
        chapter_id = svc.db.add_chapter(work_id, 1, title="第一章")
        promoted = svc.materials_promote(mid, work_id, note="改成码头背景")
        check("提升进作品素材", promoted.get("path").startswith("素材/"), True)
        work_file = svc.docs_root(work_id) / str(promoted.get("path"))
        check("作品里有副本", work_file.is_file(), True)
        check("副本带出处",
              "取自素材库：" in work_file.read_text(encoding="utf-8"), True)
        check("记了短名单", promoted.get("use", {}).get("kind"), "shortlist")
        merged = svc.materials_record_use(
            mid, work_id, kind="merge", chapter_index=1,
            target_path="设定/人物/她.md", note="把信仰改成家训")
        check("记了合并", merged.get("use", {}).get("kind"), "merge")
        stats = merged.get("stats") or {}
        check("统计：被一部作品用过", stats.get("works"), 1)
        check("统计：真正消费 1 次", stats.get("consumed"), 1)
        check("统计：事件 2 次", stats.get("events"), 2)
        uses = svc.materials_uses(material_id=mid)
        check("按素材查历史", len(uses.get("uses") or []), 2)
        check("按作品查历史",
              len(svc.materials_uses(work_id=work_id).get("uses") or []), 2)

        section("作品内检索（素材库 + 本书素材）")
        both = svc.search_materials("诀别", work_id=work_id)
        check("两处都有", both.get("count"), 1)  # 同一身份去重
        check("优先报库里那份", (both.get("cards") or [{}])[0].get("library"),
              "global")

        section("空项目联网落盘")
        saved = svc._save_web_cards("", "码头夜班", [{
            "title": "码头夜班", "url": "https://example.com/a",
            "excerpt": "搬一船盐给三个铜板。", "site_name": "地方志",
            "kind": "网页正文",
        }])
        check("落到全局库", str(saved.get("saved")).startswith("待归类/"), True)
        check("文件存在",
              (root / str(saved.get("saved"))).is_file(), True)

        section("空项目联网落盘（工具链路）")
        from services import web_research_service as web_mod

        async def fake_web(cfg, query, *, count=5, need_content=True):
            return {"provider": "deepseek", "source": "web", "query": query,
                    "cards": [{"title": "潮汐表", "url": "https://example.com/t",
                               "excerpt": "涨潮在子时，退潮在午时。",
                               "site_name": "海志", "kind": "网页正文"}],
                    "count": 1, "note": "外部来源。"}

        real_web = web_mod.search
        web_mod.search = fake_web
        try:
            web = asyncio.run(svc.research_material(
                "", "潮汐", source="web", enable_web=True, save=True))
        finally:
            web_mod.search = real_web
        check("联网空项目可落盘", str(web.get("saved")).startswith("待归类/"), True)
        check("标注落在素材库", web.get("saved_to"), "素材库")

        section("空项目检索")
        empty = asyncio.run(svc.research_material(
            "", "诀别", source="materials"))
        check("材料源", empty.get("source"), "materials")
        check("找得到", empty.get("count"), 1)
        auto = asyncio.run(svc.research_material("", "诀别", source="auto"))
        check("auto 先查素材库", auto.get("source"), "materials")

        section("回收站")
        deleted = svc.materials_delete(str(row.get("path")))
        check("删除进回收站", str(deleted.get("recycled")).startswith("_回收站/"),
              True)
        check("原文件不在", (root / str(row.get("path"))).exists(), False)
        check("历史仍在", len(svc.materials_uses(material_id=mid).get("uses") or []),
              2)
        bin_rows = svc.materials_recycled()
        check("回收站列出一项", len(bin_rows["items"]), 1)
        check("记得原始路径", bin_rows["items"][0]["original"], row.get("path"))
        check("回收站带原文预览",
              "我不再爱你了" in str(bin_rows["items"][0].get("preview") or ""),
              True)
        restored = svc.materials_restore(str(bin_rows["items"][0]["path"]))
        check("恢复到原分类", restored.get("restored"), row.get("path"))
        check("原文件回来了", (root / str(row.get("path"))).is_file(), True)
        check("回收站空了", len(svc.materials_recycled()["items"]), 0)
        # 清理只认显式动作：先扔一张，再用 days=0 清掉。
        again = svc.materials_delete(str(single.get("path")))
        purged = svc.materials_purge_recycled(days=0)
        check("清理过期条目", purged.get("count"), 1)
        check("清理后条目真的没了",
              (root / str(again.get("recycled"))).exists(), False)

        section("审计（只报告）")
        audit = svc.materials_audit()
        check("数得清在架卡片", audit.get("total") >= 4, True)
        check("没有坏卡", audit.get("broken_count"), 0)
        check("有未消费的卡", audit.get("unused") > 0, True)
        check("用过的不算未消费", audit.get("used") >= 1, True)
        check("回收站不再积压", audit["recycled"]["count"], 0)
        mat.write_text(root, "待归类/坏卡.md", "# 坏卡\n\n没有抬头。\n")
        audit = svc.materials_audit()
        check("抓得到坏抬头", audit.get("broken_count"), 1)
        (root / "待归类" / "坏卡.md").unlink()
        orig_text = svc.materials_read(str(row.get("path")))["content"]
        svc.materials_write(str(row.get("path")),
                            orig_text.replace("我不再爱你了", "我不再恨你了"))
        audit = svc.materials_audit()
        check("原文改过身份对不上", audit.get("identity_mismatch_count"), 1)
        svc.materials_write(str(row.get("path")), orig_text)
        check("改回去就对得上",
              svc.materials_audit().get("identity_mismatch_count"), 0)
        text = ("雨落在铁皮棚上，像有人撒豆子，一直撒到后半夜才停；"
                "我把窗户推开一条缝，潮气就顺着墙根爬进来。")
        svc.materials_intake(excerpt=text, title="雨一", category="场景/天气")
        svc.materials_intake(excerpt=text, title="雨二", source="web",
                             url="https://example.com/rain",
                             category="场景/天气")
        audit = svc.materials_audit()
        check("近重复只给候选", audit.get("near_duplicate_count") >= 1, True)
        check("审计不改文件",
              (root / "场景" / "天气").is_dir(), True)
        check("两张近重复都还在", len(list(
            (root / "场景" / "天气").glob("*.md"))), 2)

        short_text = "有天我问她是否想旅行，她说只要知道那些地方存在就够了。"
        long_text = ("“不，并不是这样。我是害怕……”" + short_text
                     + "”接着他说起夜晚起航的事。")
        short_card = mat.intake(
            root, excerpt=short_text, title="摘句卡", category="人物",
            tags="短卡标签", source_line="书《包含测试》")
        long_card = mat.intake(
            root, excerpt=long_text, title="整块卡", category="人物",
            tags="长卡标签", source_line="书《包含测试》第 14 章 · 块 #7")
        audit = svc.materials_audit()
        check("包含型重复被报告", audit.get("contained_count") >= 1, True)
        check("有候选不报 healthy", audit.get("healthy"), False)
        contained = audit.get("contained_duplicates") or [{}]
        check("指出保留长卡", "整块卡" in str(contained[0].get("keep") or ""),
              True)
        check("指出可回收短卡",
              "摘句卡" in str(contained[0].get("recycle") or ""), True)

        section("合并：许可门与包含型")
        short_path = str(short_card.get("path"))
        long_path = str(long_card.get("path"))
        unauth = svc.materials_merge(short_path, long_path)
        check("未授权只返回门",
              (unauth.get("needs_authorization"), unauth.get("from")),
              (True, short_path))
        check("未授权不动短卡", (root / short_path).is_file(), True)
        merged = svc.materials_merge(short_path, long_path, authorized=True)
        check("授权后合并成功", merged.get("merged"), True)
        check("短卡进了回收站", (root / short_path).exists(), False)
        long_after = svc.materials_read(long_path)
        check("长卡原文保留", short_text in long_after["excerpt"], True)
        check("长卡独有内容还在",
              "夜晚起航" in long_after["excerpt"], True)
        check("标签取并集",
              {"长卡标签", "短卡标签"}.issubset(set(long_after["tags"])), True)
        check("记下合并来源",
              any("合并自" in line for line in
                  long_after["content"].splitlines()), True)
        bin_row = next(row for row in svc.materials_recycled()["items"]
                       if row.get("original") == short_path)
        check("回收 manifest 指向长卡", bin_row.get("merged_into"), long_path)

        used_short = mat.intake(
            root, excerpt="另一段会被消费的短摘句，长度够判断包含关系。",
            title="已消费短卡", category="人物", source_line="书《消费测试》")
        used_long = mat.intake(
            root, excerpt="前文另一段会被消费的短摘句，长度够判断包含关系。后文",
            title="已消费长卡", category="人物",
            source_line="书《消费测试》第 2 章 · 块 #3")
        svc.materials_record_use(used_short["material_id"], work_id,
                                 kind="merge", note="测试消费门")
        try:
            svc.materials_merge(used_short["path"], used_long["path"],
                                authorized=True)
            used_err = ""
        except Exception as exc:                     # noqa: BLE001
            used_err = str(exc)
        check("被消费的短卡拒绝合并", "已被作品消费" in used_err, True)
        check("拒绝后两张都还在",
              ((root / used_short["path"]).is_file()
               and (root / used_long["path"]).is_file()), True)

        mat.intake_raw(root, excerpt="很久以前收进来的原始摘句。",
                       book_title="旧书", when=datetime(2025, 1, 1))
        audit = svc.materials_audit()
        check("报告待归类数量", audit.get("pending_count") >= 1, True)
        check("报告待归类超期", audit.get("pending_over_days") >= 1, True)
        check("待归类只报告不删", (root / "待归类").is_dir(), True)

        section("工具注册")
        reg = build_writing_registry(ToolRegistry(), svc,
                                     {"work_id": work_id, "chapter_index": 1})
        spec = reg.get("writing_materials")
        check("注册了 writing_materials", spec is not None, True)
        listing = json.loads(reg.execute("writing_materials",
                                         '{"action": "list"}'))
        check("工具能列目录", listing.get("ok"), True)
        gate_short = mat.intake(
            root, excerpt="工具许可门测试的短摘句，长度足以判断包含关系。",
            title="门短卡", category="人物", source_line="书《门测试》")
        gate_long = mat.intake(
            root, excerpt="前文工具许可门测试的短摘句，长度足以判断包含关系。后文",
            title="门长卡", category="人物",
            source_line="书《门测试》第 3 章 · 块 #2")
        gate = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "merge", "path": gate_short["path"],
            "target": gate_long["path"]})))
        check("工具：merge 未授权返回门",
              gate.get("needs_authorization"), True)
        check("工具：未授权不动文件",
              (root / str(gate_short["path"])).is_file(), True)
        gate_ok = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "merge", "path": gate_short["path"],
            "target": gate_long["path"], "authorized": True})))
        check("工具：授权后合并", gate_ok.get("merged"), True)
        intake = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "intake", "excerpt": "雨落在铁皮棚上，像有人撒豆子。",
            "title": "雨声", "category": "场景/天气",
            "tags": "雨,声音", "description": "铁皮棚上的雨声。",
            "source": "author"})))
        check("工具能收录", intake.get("ok"), True)
        check("工具收录有身份", bool(intake.get("material_id")), True)
        search = json.loads(reg.execute("writing_materials",
                                        '{"action": "search", "query": "雨声"}'))
        check("工具能检索", search.get("count"), 1)
        by_id = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "read", "material_id": intake.get("material_id")})))
        check("工具能按身份读卡",
              "雨落在铁皮棚上" in str(by_id.get("content") or ""), True)
        bad_chunk = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "read_chunk", "chunk_id": intake.get("material_id")})))
        check("素材 id 当块读时给提示",
              "素材卡 id" in str(bad_chunk.get("error") or ""), True)
        raw = svc.materials_intake_raw(
            excerpt="夜里下了雨，屋檐滴到天亮。", book_title="窄门")
        cooked = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "cook", "material_id": raw.get("material_id"),
            "title": "夜雨", "category": "场景/天气", "tags": "雨"})))
        check("工具能按身份加工待归类卡", cooked.get("ok"), True)
        check("加工后离开待归类",
              "待归类" not in str(cooked.get("path") or ""), True)
        peek = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "read_chunk", "chunk_id": "c2"})))
        check("工具能按段读块", peek.get("count"), 4)
        ranged = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "intake", "source": "book", "chunk_id": "c2",
            "para_start": 1, "para_end": 2, "title": "来信",
            "category": "情感"})))
        check("工具能按段收录", ranged.get("para_span"), "第 1–2 段")
        empty_reg = build_writing_registry(ToolRegistry(), svc, {})
        empty_intake = json.loads(empty_reg.execute("writing_materials", json.dumps({
            "action": "intake", "excerpt": "空项目也能收素材。",
            "title": "空项目", "category": "待归类"})))
        check("空项目也能收录", empty_intake.get("ok"), True)
        check("空项目来源是作者",
              "作者自述" in (root / str(empty_intake.get("path"))).read_text(
                  encoding="utf-8"), True)

        section("工具维护动作")
        renamed = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "rename", "path": intake.get("path"),
            "name": "雨声（改）"})))
        check("工具能改名",
              str(renamed.get("path") or "").endswith("雨声（改）.md"), True)
        moved = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "move", "path": renamed.get("path"),
            "target_dir": "场景/夜"})))
        check("工具能换分类",
              str(moved.get("path") or "").startswith("场景/夜/"), True)
        check("换分类后原文件还在",
              (root / str(moved.get("path"))).is_file(), True)
        check("换分类不动身份",
              mat.parse_card((root / str(moved.get("path"))).read_text(
                  encoding="utf-8"))["material_id"], intake.get("material_id"))
        audit_tool = json.loads(reg.execute("writing_materials",
                                            '{"action": "audit"}'))
        check("工具能盘点", "healthy" in audit_tool, True)
        json.loads(reg.execute("writing_materials", json.dumps({
            "action": "delete", "path": moved.get("path")})))
        recycled_tool = json.loads(reg.execute("writing_materials",
                                               '{"action": "recycled"}'))
        deleted_row = next(
            row for row in recycled_tool.get("items") or []
            if row.get("original") == moved.get("path"))
        check("工具能看回收站", bool(deleted_row), True)
        restored_tool = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "restore", "path": deleted_row["path"]})))
        check("工具能恢复", bool(restored_tool.get("restored")), True)
        check("恢复回原分类",
              (root / str(moved.get("path"))).is_file(), True)

        db.close()

    print("\n==== summary ====")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
