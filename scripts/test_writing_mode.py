"""写作子模式（细致 / 标准）——决策 161。

覆盖三件事：
1. 模式只抬证据门槛与预算，不换流水线；
2. `writing_set_mode` 未获作者授权时只返回请求，不生效；
3. 细致模式动笔前必须交 `research_brief`（形状校验，不设配额）。

零外网、零模型：使用临时 SQLite 与假 client。跑法：
``python scripts/test_writing_mode.py``（退出码 0 = 全绿）。
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

from services.writing_service import WritingService                    # noqa: E402
from storage.db import BookDB                                          # noqa: E402
from storage.models import BookChunkRow, BookRow                       # noqa: E402
from storage.writing_db import WritingDB                               # noqa: E402
from tools import ToolRegistry                                         # noqa: E402
from writegraph import budget as budget_mod                            # noqa: E402
from writegraph.chat_tools import build_writing_registry               # noqa: E402

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


class Client:
    config = SimpleNamespace(model="fake")

    def __init__(self, text: str = "窗外的雨落在铁皮棚上。") -> None:
        self.text = text
        self.calls = 0

    async def stream(self, system, messages, tools, cancel_event=None,
                     scenario="", max_tokens=None):
        self.calls += 1
        yield self.text, [], None


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        # WritingService 从 project_dir/prompts 读模板；tmp 里放一份副本，
        # 避免把测试的写作工作区写进真实 data/。
        shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
        books = BookDB(tmp_path / "data" / "books.db")
        books.initialize()
        books.insert_book(BookRow(id="b1", title="窄门"))
        body = "潮气顺着墙根爬进来，铁皮棚上响了一夜。" * 6
        books.insert_chunks([BookChunkRow(
            id="c1", book_id="b1", chapter_index=1, chunk_index=0,
            content=body, char_count=len(body))])

        config = SimpleNamespace(
            project_dir=str(tmp_path),
            writing_db_path=str(tmp_path / "data" / "writing.db"),
            books_db_path=str(tmp_path / "data" / "books.db"),
            skills_dir=str(ROOT / "skills"), model="fake",
            api_base="http://127.0.0.1:1", api_key="x",
            max_iterations=8, auto_compact_threshold=1.0,
            compact_max_tokens=1000, compact_preserve_recent=2,
            preserve_recent_messages=2, context_alert_ratio=0.9)
        db = WritingDB(config.writing_db_path)
        svc = WritingService(config, db=db, api_client=Client())
        work_id = svc.create_work("潮汐")
        chapter_id = db.add_chapter(work_id, 1, title="第一章")
        svc.set_contract(str(chapter_id), purpose="试模式")

        section("提示词：模式只加一段覆盖")
        std = svc.agent_prompt(work_id, 1, mode="standard")
        detail = svc.agent_prompt(work_id, 1, mode="detail")
        check("标准模式不带细致段", "本会话：细致模式" in std, False)
        check("细致模式带细致段", "本会话：细致模式" in detail, True)
        check("现场写明模式", "写作模式" in detail and "细致" in detail, True)

        section("预算：细致模式只加余量")
        std_prof = budget_mod.profile_for("standard")
        det_prof = budget_mod.profile_for("detail")
        check("总预算更大", det_prof["total"] > std_prof["total"], True)
        check("参考素材预算更大",
              det_prof["specs"]["external_reference"]["budget"]
              > std_prof["specs"]["external_reference"]["budget"], True)
        check("必需块语义不变",
              all(det_prof["specs"][k]["required"]
                  == std_prof["specs"][k]["required"]
                  for k in ("chapter_contract", "facts_current",
                            "prev_tail", "context_card")), True)

        section("切模式：没授权不生效")
        events: list[tuple[str, bool]] = []
        ctx = {"work_id": work_id, "chapter_index": 1,
               "writing_mode": "standard", "allow_auto_mode": False,
               "set_writing_mode": lambda v, r=False: events.append((v, r))}
        reg = build_writing_registry(ToolRegistry(), svc, ctx)
        out = json.loads(reg.execute("writing_set_mode",
                                     json.dumps({"mode": "detail"})))
        check("返回需要授权", out.get("needs_authorization"), True)
        check("模式没变", ctx["writing_mode"], "standard")
        check("也没写会话", events, [])

        out = json.loads(reg.execute("writing_set_mode", json.dumps({
            "mode": "detail", "authorized": True, "reason": "要查码头细节"})))
        check("授权后切到细致", out.get("mode"), "detail")
        check("写回会话", events[-1], ("detail", False))
        check("现场值已变", ctx["writing_mode"], "detail")

        out = json.loads(reg.execute("writing_set_mode", json.dumps({
            "mode": "detail", "remember": True})))
        check("记住授权也要先授权", out.get("ok"), False)

        out = json.loads(reg.execute("writing_set_mode", json.dumps({
            "mode": "detail", "authorized": True, "remember": True})))
        check("记住以后可自切", out.get("remembered"), True)
        check("上下文本轮也放行", ctx["allow_auto_mode"], True)
        check("回调收到 remember", events[-1], ("detail", True))

        section("单章覆盖")
        out = json.loads(reg.execute("writing_set_mode", json.dumps({
            "mode": "standard", "chapter_index": 1, "authorized": True})))
        check("覆盖只作用这一章", out.get("scope"), "chapter")
        check("章行落库", svc.chapter(work_id, 1)["writing_mode"], "standard")
        check("有效模式取覆盖",
              svc.effective_writing_mode(work_id, 1, "detail"), "standard")
        out = json.loads(reg.execute("writing_set_mode", json.dumps({
            "mode": "inherit", "chapter_index": 1, "authorized": True})))
        check("可以清掉覆盖", out.get("mode"), "inherit")
        check("清掉后回到会话模式",
              svc.effective_writing_mode(work_id, 1, "detail"), "detail")

        section("细致模式：没有取舍说明不动笔")
        out = json.loads(asyncio.run(reg.execute_async(
            "writing_draft_chapter", json.dumps({"chapter_index": 1}))))
        check("缺 brief 被拒", out.get("ok"), False)
        check("说清要什么", "research_brief" in str(out.get("error")), True)
        check("没有偷偷调用模型", svc.api_client.calls, 0)

        section("细致模式：一句话交代也能过")
        out = json.loads(asyncio.run(reg.execute_async(
            "writing_draft_chapter", json.dumps({
                "chapter_index": 1,
                "research_brief": {"gaps": ["本章室内对话，不需要外部素材"]}},
                ensure_ascii=False))))
        check("写成了", out.get("ok"), True)
        check("回报模式", out.get("mode"), "detail")
        brief = db.latest_chapter_brief(chapter_id)
        check("取舍说明入库", bool(brief), True)
        check("gap 保留", (brief or {}).get("payload", {}).get("gaps"),
              ["本章室内对话，不需要外部素材"])
        check("审稿看得到取舍",
              "素材取舍" in svc._review_context(work_id, 1), True)

        section("细致模式：refs 与 brief 一起走")
        out = json.loads(asyncio.run(reg.execute_async(
            "writing_draft_chapter", json.dumps({
                "chapter_index": 1,
                "refs": [{"book_id": "b1", "chapter_index": 1,
                          "chunk_index": 0, "note": "借墙根潮气"}],
                "research_brief": {
                    "questions": ["潮气怎么写"],
                    "searched": [{"source": "library",
                                  "query": "潮气 墙根", "result": "1 条"}]}},
                ensure_ascii=False))))
        check("带着 refs 写成", out.get("ok"), True)
        check("ref 计数回传", out.get("reference", {}).get("count"), 1)
        brief = db.latest_chapter_brief(chapter_id)
        check("查询记录入库",
              (brief or {}).get("payload", {}).get("searched", [{}])[0]
              .get("query"), "潮气 墙根")

        db.close()
        books.close()

    print("\n==== summary ====")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
