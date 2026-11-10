"""写作 refs：agent 选素材、服务端读回逐字原文（决策 158）。

零外网、零模型：只碰临时目录与临时 SQLite。跑法：
``python scripts/test_writing_refs.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.writing_service import WritingError, WritingService   # noqa: E402
from storage.db import BookDB                                      # noqa: E402
from storage.models import BookChunkRow, BookRow                   # noqa: E402
from storage.writing_db import WritingDB                           # noqa: E402

FAILED = 0

PARAS = [
    "来信收到了。窗外的梧桐落了一地，叶子贴在石阶上。",
    "我不再爱你了；但愿你永远不知道，这封信写完之后我在窗边站了多久。",
    "潮气顺着墙根爬进来，铁皮棚上响了一夜。",
    "后来我把信烧了，灰落在鞋面上，没有风。",
]


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"ok   {label}")
    else:
        FAILED += 1
        print(f"FAIL {label}\n     期望 {want!r}\n     实际 {got!r}")


def section(name: str) -> None:
    print(f"\n-- {name}")


def make_books(path: Path) -> None:
    db = BookDB(path)
    db.initialize()
    db.insert_book(BookRow(id="b1", title="窄门"))
    db.insert_book(BookRow(id="b2", title="面纱"))
    first = "\r\n\r\n".join(PARAS) + "\r\n\r\n" + ("信纸的边角卷起来。" * 12)
    second = ("码头上搬盐的工钱是三个铜板，霍乱那年涨到五个，"
              "船主还要抽一成；扛麻袋的人把腰弯成一张弓。" * 3)
    late = ("信纸的边角卷起来。" * 30 +
            "有一天我问她是否想去旅行，她对我说什么也不想，"
            "只要知道有这些美丽的地方存在，知道有人能前往，就已足够。")
    db.insert_chunks([
        BookChunkRow(id="c1", book_id="b1", chapter_index=1, chunk_index=0,
                     content=first, char_count=len(first)),
        BookChunkRow(id="c2", book_id="b2", chapter_index=2, chunk_index=0,
                     content=second, char_count=len(second)),
        BookChunkRow(id="c3", book_id="b1", chapter_index=3, chunk_index=0,
                     content=late, char_count=len(late)),
    ])


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        make_books(tmp_path / "data" / "books.db")
        config = SimpleNamespace(
            project_dir=str(tmp_path),
            writing_db_path=str(tmp_path / "data" / "writing.db"),
            books_db_path=str(tmp_path / "data" / "books.db"),
            # 子代理会实例化 ConversationRuntime，这些字段在真配置里都有。
            skills_dir=str(ROOT / "skills"), model="fake",
            api_base="http://127.0.0.1:1", api_key="x",
            max_iterations=8, auto_compact_threshold=1.0,
            compact_max_tokens=1000, compact_preserve_recent=2,
            preserve_recent_messages=2, context_alert_ratio=0.9,
        )
        db = WritingDB(config.writing_db_path)
        svc = WritingService(config, db=db)
        work_id = svc.create_work("潮汐")
        chapter_id = svc.db.add_chapter(work_id, 1, title="第一章")
        svc.set_contract(str(chapter_id), purpose="试 refs")

        section("书名 → id")
        check("精确匹配", svc.resolve_book_ids(["窄门"]), ["b1"])
        check("子串匹配", svc.resolve_book_ids(["面"]), ["b2"])
        try:
            svc.resolve_book_ids(["不存在的书"])
            missing = ""
        except WritingError as exc:
            missing = str(exc)
        check("找不到直接报错", "找不到《不存在的书》" in missing, True)

        section("书块坐标")
        items, errors = svc.resolve_refs([{
            "book_id": "b1", "chapter_index": 1, "chunk_index": 0}])
        check("解析出一条", len(items), 1)
        check("没有错误", errors, [])
        check("带书章块出处", "《窄门》" in items[0]["label"]
              and "块 #0" in items[0]["label"], True)
        check("逐字原文", PARAS[0] in items[0]["text"], True)
        sliced, errors = svc.resolve_refs([{
            "book_id": "b1", "chapter_index": 1, "chunk_index": 0,
            "para_start": 2, "para_end": 3}])
        check("段落也切片", "窗外的梧桐" in sliced[0]["text"], False)
        check("切到了指定段", "潮气顺着墙根" in sliced[0]["text"], True)
        check("出处标段落", "第 2–3 段" in sliced[0]["label"], True)

        section("素材卡身份")
        card = svc.materials_intake(
            excerpt="雨落在铁皮棚上，像有人撒豆子，一直撒到后半夜才停。",
            title="雨声", category="场景/天气", source="author")
        by_id, _ = svc.resolve_refs([{"material_id": card["material_id"]}])
        check("按身份读到卡", "撒豆子" in by_id[0]["text"], True)
        check("出处来自抬头", "素材库" in by_id[0]["label"], True)
        by_path, _ = svc.resolve_refs([{"path": card["path"]}])
        check("按路径也读得到", by_path[0]["material_id"],
              card["material_id"])

        section("本书 素材/ 的卡")
        svc.docs_write(
            work_id, "素材/观察/潮气.md",
            "# 潮气\n\n> 分类：场景/天气\n\n## 原文\n\n"
            "潮气顺着墙根爬进来，铁皮棚上响了一夜。\n")
        work_card, work_err = svc.resolve_refs(
            [{"path": "素材/观察/潮气.md", "note": "借墙根的潮气"}],
            work_id=work_id)
        check("按作品路径读到", len(work_card), 1)
        check("没有报错", work_err, [])
        check("正文是逐字原文", "铁皮棚上响了一夜" in work_card[0]["text"],
              True)
        check("出处标明作品素材", "作品素材" in work_card[0]["label"], True)
        built_work = svc.assemble(work_id, 1, refs=[
            {"path": "素材/观察/潮气.md"}])
        check("本书素材也能进装配", "铁皮棚上响了一夜" in built_work["text"],
              True)
        _, wrong_work = svc.resolve_refs([{"path": "素材/观察/潮气.md"}])
        check("不带作品找不到它", bool(wrong_work), True)

        section("读不到就说")
        _, bad = svc.resolve_refs([{"book_id": "b1", "chapter_index": 9,
                                    "chunk_index": 9}])
        check("缺块报错", bool(bad), True)
        _, empty = svc.resolve_refs([{"note": "什么都没给"}])
        check("缺坐标报错", "每条 ref" in (empty[0] if empty else ""), True)
        try:
            svc.assemble(work_id, 1, refs=[{"book_id": "b1",
                                            "chapter_index": 99,
                                            "chunk_index": 0}])
            blocked = ""
        except WritingError as exc:
            blocked = str(exc)
        check("装配前拦住坏坐标", "读不到" in blocked, True)

        section("装配：refs 进 external_reference")
        built = svc.assemble(work_id, 1, refs=[{
            "book_id": "b1", "chapter_index": 1, "chunk_index": 0,
            "note": "借它写信的语气"}])
        text = built["text"]
        check("块里有参考素材", "参考素材" in text, True)
        check("逐字原文进了上下文",
              "我不再爱你了" in text and "信纸的边角卷起来" in text, True)
        check("出处进了上下文", "《窄门》" in text, True)
        check("note 一起进", "借它写信的语气" in text, True)
        check("诊断计数", (built["reference"]["mode"],
                          built["reference"]["count"]), ("refs", 1))
        check("没传 refs 就不注入",
              "参考素材" in svc.assemble(work_id, 1)["text"], False)

        section("参考书架不设数量帽")
        shelf = [{"stable_book_id": f"s{i}", "book_id": f"b{i}",
                  "label": f"书{i}"} for i in range(6)]
        out = svc.set_reference_books(work_id, shelf)
        check("六本就六本", len(out["reference_books"]), 6)
        check("不静默截断", out["reference_books"][-1]["label"], "书5")

        section("检索范围")
        scoped = asyncio.run(svc.research_material(
            "", "诀别信 梧桐", source="library", book="窄门"))
        hit_books = {c.get("book_id") for c in scoped.get("cards") or []}
        check("只在指定书里找", hit_books <= {"b1"}, True)
        check("回带范围", scoped.get("book_scope"), ["窄门"])
        other = asyncio.run(svc.research_material(
            "", "霍乱 码头", source="library", book="面纱"))
        check("另一本能找到",
              {c.get("book_id") for c in other.get("cards") or []}, {"b2"})
        empty = asyncio.run(svc.research_material(
            "", "霍乱 码头", source="library", book="窄门"))
        check("范围外不返回", empty.get("count"), 0)

        section("关键词命中窗口")
        late_hit = asyncio.run(svc.research_material(
            "", "旅行 美丽的地方 有人能前往", source="library", book="窄门"))
        late_cards = late_hit.get("cards") or []
        check("命中块后半段的原句",
              any("有一天我问她是否想去旅行" in str(c.get("excerpt") or "")
                  for c in late_cards), True)
        check("关键词卡带 chunk_id",
              [c.get("chunk_id") for c in late_cards], ["c3"])
        check("回传命中词",
              late_cards[0].get("matched_terms") if late_cards else [],
              ["旅行", "美丽", "前往"])

        section("限定书时 auto 不先翻素材库")
        svc.materials_intake(excerpt="霍乱那年的码头涨了工钱，船主要抽一成。",
                             title="霍乱工钱", category="社会/经济")
        auto = asyncio.run(svc.research_material(
            "", "霍乱 码头", source="auto", book="面纱"))
        check("走书库而不是素材库", auto.get("source"), "library")
        check("仍然找得到", auto.get("count") > 0, True)

        section("写作页的素材侦察子代理")
        from sub_agent import SubAgentRunner
        from tools import ToolRegistry
        from writegraph.chat_tools import (_writing_agent_result,
                                           build_writing_registry)

        ctx = {"work_id": work_id, "chapter_index": 1}
        reg = build_writing_registry(ToolRegistry(), svc, ctx)
        agent = reg.get("Agent")
        check("Agent 已注册", agent is not None, True)
        props = (agent.parameters if agent else {}).get("properties") or {}
        check("只能派写作研究员",
              props.get("subagent_type", {}).get("enum"),
              ["writing_researcher"])
        check("不回传子代理中间话", getattr(agent, "execute_stream", None),
              None)
        runner = SubAgentRunner(None, reg, config)
        sub = runner._build_sub_registry("writing_researcher")
        check("子代理只看得见检索", sorted(sub.get_tool_names()),
              ["writing_research"])

        payload = {"status": "completed", "result_text": json.dumps({
            "refs": [
                {"book_id": "b1", "chapter_index": 1, "chunk_index": 0,
                 "note": "信件的语气"},
                {"book_id": "b1", "chapter_index": 99, "chunk_index": 0},
                {"path": "素材/观察/潮气.md", "note": "墙根的潮气"},
            ],
            "notes": "两条可用"}, ensure_ascii=False)}
        shaped = _writing_agent_result(svc, ctx, payload)
        check("保留能读的坐标", len(shaped["refs"]), 2)
        check("坏坐标单独报", len(shaped.get("dropped") or []), 1)
        check("说明带回来", shaped["notes"], "两条可用")
        failed = _writing_agent_result(
            svc, ctx, {"status": "failed", "error": "boom",
                       "result_text": ""})
        check("失败原样报", (failed["ok"], failed["error"]), (False, "boom"))
        loose = _writing_agent_result(
            svc, ctx, {"status": "completed", "result_text": "没找到合适的。"})
        check("没按格式就标出来", loose.get("parse_error"), True)
        check("空结果不编坐标", loose["refs"], [])

        section("子代理真跑一趟（假模型，零外网）")
        class FakeClient:
            config = SimpleNamespace(model="fake")

            def __init__(self) -> None:
                self.calls = 0

            async def stream(self, system, messages, tools, cancel_event=None,
                             scenario="", max_tokens=None):
                self.calls += 1
                if self.calls == 1:
                    yield "", [{"id": "t1", "name": "writing_research",
                                "arguments": json.dumps(
                                    {"query": "梧桐", "source": "library"},
                                    ensure_ascii=False)}], None
                else:
                    yield json.dumps({
                        "refs": [{"book_id": "b1", "chapter_index": 1,
                                  "chunk_index": 0, "note": "借梧桐落叶"}],
                        "notes": "一条可用"}, ensure_ascii=False), [], None

        client = FakeClient()
        svc.api_client = client
        trip_reg = build_writing_registry(ToolRegistry(), svc, ctx)
        raw = asyncio.run(trip_reg.execute_async("Agent", json.dumps({
            "description": "找梧桐落叶的原文",
            "prompt": "找一段写梧桐落叶、石阶与潮气的原文。"},
            ensure_ascii=False)))
        trip = json.loads(raw)
        check("子代理跑完", trip.get("ok"), True)
        check("先检索再收尾", client.calls, 2)
        check("坐标从子代理回到主 agent",
              (trip["refs"][0]["book_id"],
               trip["refs"][0]["chunk_index"]), ("b1", 0))
        check("说明也回来", trip.get("notes"), "一条可用")
        svc.api_client = None

        db.close()

    print("\n==== summary ====")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
