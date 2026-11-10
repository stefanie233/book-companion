"""待归类整理（决策 167）的测试。

只碰临时目录与临时 SQLite，零外网。跑法：
``python scripts/test_materials_cook.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

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
#: 测试用的书块原文，待归类卡与已加工卡都必须逐字保住它。
EXCERPT = ("我写信告诉你，我不再爱你了；但愿你永远不知道，\r\n"
           "这封信写完之后我在窗边站了多久。")


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"ok   {label}")
    else:
        FAILED += 1
        print(f"FAIL {label}\n     期望 {want!r}\n     实际 {got!r}")


def section(name: str) -> None:
    print(f"\n-- {name}")


def raw_block(text: str) -> str:
    """取出文件里 ``## 原文`` 那一段的逐字字节（去掉末尾换行）。"""
    at = text.index("## 原文")
    block = text[at:]
    cut = block.find("\n## 说明")
    if cut >= 0:
        block = block[:cut]
    return block.rstrip()


def make_books_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE book_chunks (id TEXT PRIMARY KEY, book_id TEXT,"
                 " chapter_index INTEGER, chunk_index INTEGER, content TEXT,"
                 " char_count INTEGER)")
    conn.execute("CREATE TABLE book_chapters (book_id TEXT,"
                 " chapter_index INTEGER, title TEXT)")
    conn.execute("INSERT INTO books VALUES ('b1','窄门')")
    conn.execute("INSERT INTO book_chapters VALUES ('b1',12,'诀别')")
    conn.execute(
        "INSERT INTO book_chunks VALUES ('c1','b1',12,3,?,120)", (EXCERPT,))
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

        section("收录待归类卡")
        raw = svc.materials_intake_raw(
            excerpt=EXCERPT, book_title="窄门", chapter_index=12,
            chunk_index=3, cfi="epubcfi(/6/4[ch12]!/4/2/1:0)",
            instruction="这里那封诀别信")
        raw_path = str(raw.get("path"))
        raw_mid = str(raw.get("material_id"))
        check("落在待归类", raw_path.startswith("待归类/"), True)
        check("文件名是书名+时间戳",
              bool(mat._RAW_NAME_RE.match(raw_path.rsplit("/", 1)[-1])), True)
        raw_card = svc.materials_read(raw_path)
        check("抬头写待归类", mat.RAW_STATE in raw_card["content"], True)
        check("CFI 只做定位线索",
              "定位：epubcfi(" in raw_card["content"], True)
        check("出处带书/章/块",
              "书《窄门》第 12 章 · 块 #3" in raw_card["source_line"], True)
        check("原话存进指令", raw_card["instruction"], "这里那封诀别信")
        parsed = mat.parse_card(raw_card["content"], raw_path)
        check("解析为待归类卡", parsed["raw"], True)
        check("状态行认得", parsed["status"], "待归类")
        check("定位行认得",
              parsed["location"], "epubcfi(/6/4[ch12]!/4/2/1:0)")

        section("待归类清单")
        pending = svc.materials_pending()
        check("列出一张", pending["count"], 1)
        check("总数一致", pending["total"], 1)
        item = pending["items"][0]
        check("带路径", item["path"], raw_path)
        check("带出处", item["source"], "书《窄门》第 12 章 · 块 #3")
        check("带原文预览", "我不再爱你" in item["preview"], True)
        check("没有重复已加工卡", item["duplicate_of"], [])
        listing = svc.materials_list()
        check("页面拿到待加工数", listing["pending_count"], 1)

        section("加工成素材卡")
        root = svc.materials_root()
        old_text = (root / raw_path).read_text(encoding="utf-8")
        cooked = svc.materials_cook(
            raw_path, title="诀别信", category="情感/爱情/离别",
            tags="书信,诀别,克制",
            description="以信仰为由拒绝爱人的一封信；几乎没有直接抒情。")
        check("加工成功", cooked["ok"], True)
        new_path = str(cooked["path"])
        check("迁出待归类", new_path.startswith("情感/爱情/离别/"), True)
        check("身份不变", cooked["material_id"], raw_mid)
        check("旧待归类文件没了", (root / raw_path).exists(), False)
        check("新文件名带身份", raw_mid in new_path, True)
        new_text = (root / new_path).read_text(encoding="utf-8")
        check("原文逐字节不动", raw_block(old_text), raw_block(new_text))
        card = svc.materials_read(new_path)
        check("不再是待归类卡", card["raw"], False)
        check("写了加工时间", bool(card["processed_at"]), True)
        check("状态行已撤", mat.RAW_STATE in new_text, False)
        check("分类改了", card["category"], "情感/爱情/离别")
        check("标签改了", "书信" in card["tags"] and "克制" in card["tags"], True)
        check("说明只写是什么", "适用于" in card["description"], False)
        check("说明只有一节", new_text.count("## 说明"), 1)
        check("定位随卡走", card["location"], parsed["location"])
        check("指令随卡走", card["instruction"], parsed["instruction"])
        check("采集时间不变", card["collected_at"], parsed["collected_at"])

        section("加工后清单")
        check("待归类清空", svc.materials_pending()["total"], 0)
        check("页面待加工数归零", svc.materials_list()["pending_count"], 0)
        audit = svc.materials_audit()
        check("审计不误报原文改过", audit["identity_mismatch_count"], 0)

        section("同文同源再收一次")
        dup = svc.materials_intake_raw(
            excerpt=EXCERPT, book_title="窄门", chapter_index=12,
            chunk_index=3, cfi="epubcfi(/6/4[ch12]!/4/2/1:0)")
        # 待归类卡与已加工卡不同：同一段文字再收一次仍是新文件，不返回旧卡。
        check("待归类卡不去重", dup.get("exists"), False)
        check("第二次照常落盘", (root / str(dup["path"])).is_file(), True)
        dup_pending = svc.materials_pending()
        check("清单标出已有已加工卡",
              dup_pending["items"][0]["duplicate_of"], [new_path])
        again = svc.materials_cook(
            str(dup["path"]), title="诀别信", category="情感/爱情/离别")
        check("不覆盖已有已加工卡", again["exists"], True)
        check("指回原已加工卡", again["existing"], new_path)
        check("待归类卡原地不动", (root / str(dup["path"])).exists(), True)

        section("旧版待归类卡（无状态行）")
        legacy_body = "旧书里的一句话，没有人记得。"
        legacy_src = "书《旧书》第 2 章 · 块 #1"
        old_id = mat.excerpt_id(legacy_body, legacy_src + " · epubcfi(/6/4)")
        legacy_rel = "待归类/《旧书》-20260101-010101.md"
        mat.write_text(root, legacy_rel, mat.card_markdown(
            material_id=old_id, title="《旧书》-20260101-010101",
            excerpt=legacy_body, source_line=legacy_src,
            collected_at="2026-01-01 01:01"))
        legacy = mat.parse_card(
            (root / legacy_rel).read_text(encoding="utf-8"), legacy_rel)
        check("靠文件名认出旧卡", legacy["raw"], True)
        check("旧状态行仍认出待归类",
              mat.is_raw_card(
                  {"status": mat.LEGACY_RAW_STATE.split("：", 1)[-1]}), True)
        check("旧身份含 CFI 与现算不同",
              legacy["material_id"] != mat._card_identity(legacy), True)
        audit_legacy = svc.materials_audit()
        check("审计不误报旧卡", audit_legacy["identity_mismatch_count"], 0)
        legacy_cooked = svc.materials_cook(
            legacy_rel, title="旧书的一句", category="写法")
        legacy_new = str(legacy_cooked["path"])
        legacy_mid = str(legacy_cooked["material_id"])
        check("旧卡能加工", legacy_cooked["ok"], True)
        check("迁到分类", legacy_new.startswith("写法/"), True)
        check("身份换成规范值", legacy_mid != old_id, True)
        legacy_text = (root / legacy_new).read_text(encoding="utf-8")
        check("抬头换掉旧 id", f"素材 {old_id}" in legacy_text, False)
        check("抬头写上新 id", f"素材 {legacy_mid}" in legacy_text, True)
        check("按旧身份已找不到", mat.find_by_id(root, old_id), "")
        check("按新身份找得到", mat.find_by_id(root, legacy_mid), legacy_new)

        section("手写说明的待归类卡")
        manual = svc.materials_intake_raw(
            excerpt="第二段话，来自另一本书。", book_title="旧书")
        manual_rel = str(manual["path"])
        base_text = (root / manual_rel).read_text(encoding="utf-8")
        mat.write_text(root, manual_rel,
                       base_text + "\n## 说明\n\n作者手写的说明。\n")
        kept = svc.materials_cook(manual_rel, title="第二段", category="写法")
        kept_text = (root / str(kept["path"])).read_text(encoding="utf-8")
        check("没给新说明就保留手写", "作者手写的说明。" in kept_text, True)
        check("说明仍只有一节", kept_text.count("## 说明"), 1)
        manual2 = svc.materials_intake_raw(
            excerpt="第三段话，来自另一本书。", book_title="旧书")
        manual2_rel = str(manual2["path"])
        mat.write_text(root, manual2_rel,
                       (root / manual2_rel).read_text(encoding="utf-8")
                       + "\n## 说明\n\n旧的说明。\n")
        replaced = svc.materials_cook(
            manual2_rel, title="第三段", description="新写的说明。")
        check("不选分类就留在待归类",
              str(replaced["path"]).startswith("待归类/"), True)
        replaced_text = (root / str(replaced["path"])).read_text(
            encoding="utf-8")
        check("新说明覆盖旧的", "新写的说明。" in replaced_text
              and "旧的说明。" not in replaced_text, True)
        check("覆盖后仍只有一节", replaced_text.count("## 说明"), 1)

        section("坏动作")
        try:
            svc.materials_cook(new_path, title="再加工")
            recook = ""
        except Exception as exc:                     # noqa: BLE001
            recook = str(exc)
        check("已加工卡不能再加工", "已经加工过了" in recook, True)
        try:
            svc.materials_cook("待归类/不存在.md")
            missing = ""
        except Exception as exc:                     # noqa: BLE001
            missing = str(exc)
        check("不存在报错", "文件不存在" in missing, True)
        bad_rel = "待归类/《坏书》-20260202-020202.md"
        mat.write_text(root, bad_rel, "# 坏书\n\n" + mat.RAW_STATE + "\n")
        try:
            svc.materials_cook(bad_rel)
            no_body = ""
        except Exception as exc:                     # noqa: BLE001
            no_body = str(exc)
        check("没原文不能加工", "卡里没有原文" in no_body, True)
        (root / bad_rel).unlink()

        section("工具链路")
        reg = build_writing_registry(ToolRegistry(), svc, {})
        pending_tool = json.loads(reg.execute("writing_materials",
                                              '{"action": "pending"}'))
        check("工具能列待归类卡", pending_tool["ok"], True)
        check("工具数得清", pending_tool["total"], 1)
        check("工具标出重复", pending_tool["items"][0]["duplicate_of"],
              [new_path])
        fresh = svc.materials_intake_raw(
            excerpt="窗外的梧桐落了一地。", book_title="窄门",
            chapter_index=12, chunk_index=4)
        cook_tool = json.loads(reg.execute("writing_materials", json.dumps({
            "action": "cook", "path": fresh["path"], "title": "梧桐",
            "category": "场景/天气", "tags": "秋,树",
            "description": "院子里落了一地的梧桐叶。"})))
        check("工具能加工", cook_tool["ok"], True)
        check("工具迁出待归类",
              str(cook_tool["path"]).startswith("场景/天气/"), True)
        check("工具保留身份", cook_tool["material_id"],
              fresh["material_id"])
        pending_after = json.loads(reg.execute("writing_materials",
                                               '{"action": "pending"}'))
        check("只剩重复那张", pending_after["total"], 1)
        listing_tool = json.loads(reg.execute("writing_materials",
                                              '{"action": "list"}'))
        check("工具回报待加工数", listing_tool["pending_count"], 1)

        audit_final = svc.materials_audit()
        check("审计仍无坏卡", audit_final["broken_count"], 0)
        check("审计仍无身份错位", audit_final["identity_mismatch_count"], 0)

        section("清单排序")
        alt = tmp_path / "altmat"
        early = mat.intake_raw(alt, excerpt="先收的。", book_title="乙",
                               when=datetime(2026, 1, 1, 3, 4, 5))
        late = mat.intake_raw(alt, excerpt="后收的。", book_title="甲",
                              when=datetime(2026, 1, 2, 3, 4, 5))
        order = [i["path"] for i in mat.pending_cards(alt)["items"]]
        check("跨书名也按采集时间", order, [early["path"], late["path"]])

        db.close()

    print("\n==== summary ====")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
