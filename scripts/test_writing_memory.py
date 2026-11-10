"""写作记忆维护（两段式）的测试（零 LLM）。

盯四件事：

1. Phase 1 抽取落日志；no-op 不产生待合并记录。
2. Phase 2 只写 作品记忆.md 与 设定/，闸门拒绝 计划稿/素材/超长/空内容。
3. 合并后待办清零，state 留下文件 hash 清单；作者改动可被 diff 看见。
4. 工具 writing_memory 能 status / auto，且不改写 计划稿/素材。

跑法：``python scripts/test_writing_memory.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.writing_service import WritingService            # noqa: E402
from storage.writing_db import WritingDB                       # noqa: E402
from tools import ToolRegistry                                 # noqa: E402
from writegraph import docs as docs_mod                        # noqa: E402
from writegraph import memory_guard as guard_mod               # noqa: E402
from writegraph.chat_tools import build_writing_registry       # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"ok   {label}")
    else:
        FAILED += 1
        print(f"FAIL {label}\n     期望 {want!r}\n     实际 {got!r}")


def has(label: str, needle: str, text: str) -> None:
    check(label, needle in text, True)


EXTRACT = {
    "no_op": False,
    "rollout_summary": "人物A在地点A下等船。",
    "preferences": [{"signal": "雨不要写满，留白",
                     "evidence": "作者：这段别写太满，留白。"}],
    "settings": [{"subject": "人物A", "kind": "人物",
                  "text": "说话短，紧张时摸表链。",
                  "evidence": "作者原话"}],
    "plans": [], "materials": [],
}

CONSOLIDATE = {
    "no_op": False,
    "bible": ("# 《雨夜》作品记忆\n\n## 这本书是什么\n\n雨夜的港口。\n\n"
              "## 写作约定\n\n- 雨不要写满，留白。\n\n## 忌讳\n"),
    "writes": [{"path": "设定/人物/人物A.md",
                "content": "# 人物A\n\n说话短，紧张时摸表链。\n"}],
    "deletes": [],
    "notes": "记下人物A的说话方式与留白约定",
}


SETTLE = {
    "fact_ops": [], "promise_ops": [], "entity_ops": [],
    "chapter_summary": "人物A在地点A等船，船到了。",
    "key_events": ["船到"],
}


class FakeClient:
    config = SimpleNamespace(model="fake-memory")

    def __init__(self, extract=None, consolidate=None, settle=None):
        self.calls: list[str] = []
        self.messages: list[list[dict]] = []
        self.extract = extract or EXTRACT
        self.consolidate = consolidate or CONSOLIDATE
        self.settle = settle or SETTLE

    async def stream(self, system, messages, tools, cancel_event=None,
                     scenario="", max_tokens=None):
        self.calls.append(scenario)
        if scenario == "writing.memory.extract":
            self.messages.append(messages)
        data = {
            "writing.memory.extract": self.extract,
            "writing.settle": self.settle,
        }.get(scenario, self.consolidate)
        yield json.dumps(data, ensure_ascii=False), [], None


def make_service(tmp: Path, client=None) -> WritingService:
    shutil.copytree(ROOT / "prompts", tmp / "prompts")
    cfg = SimpleNamespace(project_dir=tmp)
    return WritingService(cfg, db=WritingDB(tmp / "data" / "writing.db"),
                          api_client=client or FakeClient())


def test_guard(tmp: Path) -> None:
    root = tmp
    docs_mod.ensure_workspace(root, "闸门")
    bad = guard_mod.validate_ops(
        {"bible": "x" * 3001,
         "writes": [{"path": "计划稿/x.md", "content": "x"},
                    {"path": "设定/a.md", "content": "   "}],
         "deletes": ["素材/x.md"]}, root)
    check("闸门：整批拒绝", bad.ok, False)
    check("闸门：四类错误都报", len(bad.errors) >= 4, True)
    ok = guard_mod.validate_ops(
        {"bible": "# 记忆\n",
         "writes": [{"path": "设定/a.md", "content": "# A\n"}],
         "deletes": []}, root)
    check("闸门：合法 ops 通过", ok.ok, True)
    before = guard_mod.manifest(root)
    docs_mod.write_file(root, "设定/b.md", "# B\n")
    diff = guard_mod.diff_manifest(before, guard_mod.manifest(root))
    check("闸门：作者新增可见", diff["added"], ["设定/b.md"])
    content_diff = guard_mod.diff_snapshot(
        {"设定/a.md": "旧内容\n"}, {"设定/a.md": "新内容\n"})
    check("闸门：内容级 diff", ("+新内容" in content_diff
                                and "-旧内容" in content_diff), True)
    from writegraph import memory_write
    prompt = memory_write.build_consolidate_input(
        bible="# x\n", settings="", pending=[], changes=diff,
        diff=content_diff, audit={"counts": {"exact_duplicates": 0}})
    check("闸门：diff 进合并输入", "# 改动 diff" in prompt, True)
    check("闸门：audit 进合并输入", "机械审计" in prompt, True)
    fixed = guard_mod.ensure_bible_schema("# 记忆\n")
    check("schema：补齐三节", all(
        f"## {name}" in fixed for name in guard_mod.BIBLE_SECTIONS), True)
    check("schema：不丢原文", "# 记忆" in fixed, True)


def test_pipeline(tmp: Path) -> None:
    client = FakeClient()
    svc = make_service(tmp, client)
    wid = svc.create_work("雨夜")
    ch = svc.create_chapter(wid, title="等船")
    svc.db.save_draft(wid, ch["id"], "人物A在地点A下等船，雨声很响。")
    svc.set_contract(ch["id"], purpose="等船", must_hit_now=["船到"])

    out = asyncio.run(svc.maintain_memory(
        wid, 1, transcript="作者：这段别写太满，留白。", threshold=1))
    check("管线：整体成功", out.get("ok"), True)
    check("管线：两段都调了", client.calls,
          ["writing.memory.extract", "writing.memory.consolidate"])
    check("管线：待办清零", svc.memory_status(wid)["pending"], 0)
    root = svc.docs_root(wid)
    has("管线：作品记忆写入约定", "留白", docs_mod.read_bible(root))
    has("管线：设定写入纹理", "摸表链",
        docs_mod.read_file(root, "设定/人物/人物A.md")["content"])
    check("管线：不写计划稿", not any((root / "计划稿").iterdir()), True)
    check("管线：不写素材", not any((root / "素材").iterdir()), True)

    thr_client = FakeClient()
    thr = make_service(tmp.parent / (tmp.name + "-threshold"), thr_client)
    thr_wid = thr.create_work("雨夜阈值")
    thr_ch = thr.create_chapter(thr_wid, title="一")
    thr.db.save_draft(thr_wid, thr_ch["id"], "人物A在地点A等船。")
    thr.set_contract(thr_ch["id"], purpose="等船", must_hit_now=["船到"])
    tout = asyncio.run(thr.maintain_memory(
        thr_wid, 1, transcript="作者：留白。", threshold=3))
    check("阈值：先只抽取", thr_client.calls,
          ["writing.memory.extract"])
    check("阈值：合并挂起", tout["consolidate"]["status"], "pending")
    check("阈值：pending 留着", thr.memory_status(thr_wid)["pending"], 1)
    thr.db.close()

    noop = FakeClient(extract={"no_op": True, "rollout_summary": "",
                               "preferences": [], "settings": [],
                               "plans": [], "materials": []})
    svc2 = make_service(tmp.parent / (tmp.name + "-noop"), noop)
    wid2 = svc2.create_work("无事")
    svc2.create_chapter(wid2, title="一")
    out = asyncio.run(svc2.memory_extract(wid2, 1))
    check("抽取：no-op 标 skipped", out["status"], "skipped")
    check("抽取：no-op 不产生待办",
          svc2.memory_status(wid2)["pending"], 0)
    check("合并：没有待办直接返回",
          asyncio.run(svc2.memory_consolidate(wid2))["status"],
          "nothing_to_merge")

    bad = FakeClient(consolidate={
        "no_op": False, "bible": "",
        "writes": [{"path": "计划稿/x.md", "content": "x"}],
        "deletes": [], "notes": ""})
    svc3 = make_service(tmp.parent / (tmp.name + "-bad"), bad)
    wid3 = svc3.create_work("坏 ops")
    svc3.create_chapter(wid3, title="一")
    asyncio.run(svc3.memory_extract(wid3, 1))
    out = asyncio.run(svc3.memory_consolidate(wid3))
    check("合并：越界被闸门拒绝", (out.get("ok"), out.get("status")),
          (False, "guard_failed"))
    check("合并：拒绝后待办仍在",
          svc3.memory_status(wid3)["pending"], 1)

    reg = build_writing_registry(ToolRegistry(), svc, {"work_id": wid})
    out = json.loads(asyncio.run(reg.execute_async(
        "writing_memory", json.dumps({"action": "status"}))))
    check("工具：status", out.get("pending"), 0)
    out = json.loads(asyncio.run(reg.execute_async(
        "writing_memory", json.dumps({"action": "consolidate"}))))
    check("工具：consolidate 无待办",
          out.get("status"), "nothing_to_merge")


def test_dirty_tracking(tmp: Path) -> None:
    """结算标脏 → 下一轮提醒 → 抽取自动带结算/兑现结果 → 清脏。"""
    client = FakeClient()
    svc = make_service(tmp, client)
    wid = svc.create_work("雨夜")
    ch = svc.create_chapter(wid, title="等船")
    svc.db.save_draft(wid, ch["id"], "人物A在地点A下等船，船到了。")
    svc.set_contract(ch["id"], purpose="等船", must_hit_now=["船到"])

    out = asyncio.run(svc.settle(
        wid, 1, outcomes=[{"item": "船到", "verdict": "hit",
                           "evidence": "船到了"}]))
    check("结算：成功", out.get("ok"), True)
    status = svc.memory_status(wid)
    check("结算：本章标脏", status["dirty_chapters"], [1])
    check("提醒：下一轮能看到", "第1章" in svc.memory_reminder(wid), True)

    asyncio.run(svc.memory_extract(wid, 1))
    check("抽取：结算结果自动进输入",
          any("## 结算结果" in str(m) and "## 合同兑现申报" in str(m)
              for m in client.messages), True)
    check("抽取：清脏", svc.memory_status(wid)["dirty_chapters"], [])
    check("提醒：抽取后消失", svc.memory_reminder(wid), "")


def test_note_and_audit(tmp: Path) -> None:
    shutil.copytree(ROOT / "prompts", tmp / "prompts")
    db = WritingDB(tmp / "data" / "writing.db")
    svc = WritingService(SimpleNamespace(project_dir=tmp), db=db)
    wid = svc.create_work("雨夜")
    svc.create_chapter(wid, title="一")

    svc.docs_write(wid, "设定/人物/人物A.md", "# 人物A\n\n- 说话要短。\n")
    svc.docs_write(wid, "设定/人物/人物A副本.md",
                   "# 人物A\n\n- 说话要短。\n")
    svc.docs_write(wid, "设定/地点/人物A.md",
                   "# 人物A\n\n- 说话要短。\n")
    svc.docs_write(wid, "作品记忆.md",
                   "# 《雨夜》作品记忆\n\n## 写作约定\n\n- 留白\n- 留白\n")

    note = svc.memory_note(
        wid, target="bible", section="写作约定",
        text="- 雨不要写满。", evidence="作者：别写太满")
    check("note：写入待合并", (note["ok"], note["duplicate"]), (True, False))
    again = svc.memory_note(
        wid, target="bible", section="写作约定",
        text="- 雨不要写满。", evidence="作者：别写太满")
    check("note：同内容同出处幂等",
          (again["duplicate"], again["note_id"] == note["note_id"]),
          (True, True))
    check("note：只留一条 pending", svc.memory_status(wid)["pending"], 1)

    audit = svc.memory_audit(wid)
    check("audit：逐字重复", audit["counts"]["exact_duplicates"], 1)
    check("audit：同主题同名", audit["counts"]["same_subject"], 1)
    check("audit：作品记忆重复 bullet",
          audit["counts"]["bible_duplicates"], 1)
    check("audit：pending 无重复", audit["pending_duplicates"], [])

    svc.docs_write(wid, "设定/地点/地点A.md", "# 地点A\n\n- 铁皮。\n")
    svc.docs_write(wid, "设定/地点/地点A.md",
                   "- 铁皮。\n- 雨声很响。\n", append=True, dedupe=True)
    content = svc.docs_read(wid, "设定/地点/地点A.md")["content"]
    check("append：重复行跳过", content.count("- 铁皮。"), 1)
    check("append：新行留下", "- 雨声很响。" in content, True)
    try:
        svc.docs_write(wid, "设定/地点/地点A2.md", content,
                       check_duplicate=True)
        duplicate_err = ""
    except Exception as exc:                     # noqa: BLE001
        duplicate_err = str(exc)
    check("write：同内容拒绝", "内容与已有设定重复" in duplicate_err, True)

    reg = build_writing_registry(ToolRegistry(), svc, {"work_id": wid})
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "write", "path": "作品记忆.md", "content": "x"})))
    check("工具：作品记忆直写被拒", out.get("ok"), False)
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "append", "path": "设定/地点/地点A.md",
        "content": "- 铁皮。\n"})))
    check("工具：append 判重", out.get("skipped_duplicates"), 1)
    out = json.loads(asyncio.run(reg.execute_async(
        "writing_memory", json.dumps({"action": "audit"}))))
    check("工具：audit", (out.get("ok"), out.get("counts", {}).get(
        "exact_duplicates")), (True, 1))
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "write", "path": "设定/人物/阿莉莎.md",
        "content": "# 阿莉莎\n\n- 蓝眼睛。\n"})))
    check("工具：设定直写成功", out.get("ok"), True)
    check("工具：直写记 pending", svc.memory_status(wid)["pending"], 2)
    db.close()


OPS = {
    "no_op": False,
    "bible_ops": [{"op": "append_section", "section": "写作约定",
                   "content": "- 雨不要写满。"}],
    "writes": [],
    "appends": [{"path": "设定/人物/人物A.md", "section": "说话方式",
                 "content": "- 紧张时摸表链。"}],
    "patches": [{"path": "设定/地点/地点A.md", "old": "地点A是木头的。",
                 "new": "地点A是铁皮的。"}],
    "merges": [{"from": "设定/人物/人物A（旧）.md",
                "into": "设定/人物/人物A.md"}],
    "deletes": [],
    "notes": "追加说话方式、修正地点A、合并旧卡",
}


def test_ops_pipeline(tmp: Path) -> None:
    client = FakeClient(consolidate=OPS)
    svc = make_service(tmp, client)
    wid = svc.create_work("雨夜")
    svc.create_chapter(wid, title="一")
    svc.docs_write(wid, "设定/人物/人物A.md",
                   "# 人物A\n\n## 说话方式\n\n- 说话要短。\n")
    svc.docs_write(wid, "设定/人物/人物A（旧）.md",
                   "# 人物A\n\n- 说话要短。\n- 左眉有疤。\n")
    svc.docs_write(wid, "设定/地点/地点A.md", "# 地点A\n\n地点A是木头的。\n")
    svc.memory_note(wid, target="bible", section="写作约定",
                    text="- 雨不要写满。", evidence="作者原话")

    out = asyncio.run(svc.memory_consolidate(wid))
    check("ops：合并成功", out.get("status"), "consolidated")
    bible = docs_mod.read_bible(svc.docs_root(wid), max_chars=4000)
    check("ops：作品记忆追加小节", "雨不要写满" in bible, True)
    setting = docs_mod.read_file(
        svc.docs_root(wid), "设定/人物/人物A.md")["content"]
    check("ops：设定追加小节", "摸表链" in setting, True)
    check("ops：追加没覆盖旧行", "说话要短" in setting, True)
    check("ops：patch 改了原文", "地点A是铁皮的" in docs_mod.read_file(
        svc.docs_root(wid), "设定/地点/地点A.md")["content"], True)
    check("ops：merge 删了旧卡", (svc.docs_root(wid) / "设定/人物/人物A（旧）.md").exists(), False)
    merged = docs_mod.read_file(
        svc.docs_root(wid), "设定/人物/人物A.md")["content"]
    check("ops：merge 保留独有内容", "左眉有疤" in merged, True)
    check("ops：待办清零", svc.memory_status(wid)["pending"], 0)
    state = svc.db.get_memory_state(wid)
    check("ops：留下 baseline", bool(json.loads(state.get("baseline") or "{}")), True)
    check("delta：合并后无变化", svc.memory_delta(wid), "")
    svc.docs_write(wid, "设定/地点/地点A.md",
                   "# 地点A\n\n铁皮，雨声很响。\n")
    delta = svc.memory_delta(wid)
    check("delta：改了能看到",
          ("修改" in delta and "设定/地点/地点A.md" in delta), True)
    db = svc.db
    db.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        test_guard(tmp / "guard")
        test_pipeline(tmp / "main")
        test_dirty_tracking(tmp / "dirty")
        test_note_and_audit(tmp / "note")
        test_ops_pipeline(tmp / "ops")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n全绿" if not FAILED else f"\n{FAILED} 项失败")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())

