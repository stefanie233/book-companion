"""阅读会话的离线测试：假模型，不打网络。

钉住四件事：
1. 系统提示逐字稳定、历史只追加（前缀缓存的前提）；
2. 工具轮成对落进上下文，未来章节在工具层被拒绝；
3. 写笔记时逐字核对引文，核对不过的丢掉；
4. 超阈值换窗 = 摘要块 + 最近 N 章原文，且摘要只覆盖被换掉的章。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flight_recorder.recorder import ContextRecorder           # noqa: E402
from reading import prompts                                    # noqa: E402
from reading.session import ReadingSession                     # noqa: E402
from reading import map as map_mod                             # noqa: E402
from reading import tagging                                    # noqa: E402
from reading.store import (ReadingStore, chapter_map,           # noqa: E402
                           highlights_for, render_highlights,
                           roleplay_kernel, scene_view)
from reading.tools import ReadingTools                         # noqa: E402
from storage.db import BookDB                                  # noqa: E402
from storage.models import (BookChapterRow, BookChunkRow,       # noqa: E402
                            BookRow)

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok   ", name)
    else:
        FAIL += 1
        print("FAIL ", name, detail)


class FakeCfg:
    api_key = "k"
    api_base = "http://fake"
    model = "fake-model"
    graph_max_tokens = 8_000


def make_books(tmp: Path, n: int = 5) -> BookDB:
    db = BookDB(tmp / "books.db")
    db.initialize()
    db.insert_book(BookRow(id="b1", title="测试书", format="txt",
                           source_path="x", chapter_count=n))
    chapters, chunks = [], []
    for i in range(n):
        text = f"第{i}章的信。他走进房间，说了一句只有他们懂的话。"
        chapters.append(BookChapterRow(
            id=f"c{i}", book_id="b1", chapter_index=i,
            title=f"第{i}章", content=text, char_count=len(text),
            chapter_type="body"))
        chunks.append(BookChunkRow(
            id=f"k{i}", book_id="b1", chapter_index=i, chunk_index=0,
            content=text, char_count=len(text)))
    db.insert_chapters(chapters)
    db.insert_chunks(chunks)
    return db


class FakeSession(ReadingSession):
    """按最后一条 user 消息里的章号产出笔记；可注入一次工具调用。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[list[dict]] = []
        self.n_calls = 0
        self.tool_once = True
        self.prompt_base = 400
        self.prompt_step = 0

    async def _call(self, messages, usage, *, tools=None):
        self.calls.append([dict(m) for m in messages])
        self.n_calls += 1
        last_user = next((m.get("content") or "" for m in reversed(messages)
                          if m.get("role") == "user"), "")
        m = re.search(r'<chapter index="(\d+)"', last_user)
        idx = int(m.group(1)) if m else 0
        has_tool = any(x.get("role") == "tool" for x in messages[-3:])
        prompt = self.prompt_base + self.n_calls * self.prompt_step
        if tools and self.tool_once and not has_tool:
            data = self._data(None, [("read_passage", {"chapter": idx})],
                              prompt)
        else:
            data = self._data(json.dumps(self._note(idx), ensure_ascii=False),
                              None, prompt)
        usage.add(data)
        self._last_prompt_tokens = prompt
        return data

    @staticmethod
    def _note(idx: int) -> dict:
        return {
            "scene": f"第{idx}章的场面",
            "characters": [{"name": "她", "wants": "确认", "fears": "失去",
                            "knows": f"第{idx}章的事实", "state": "紧张"}],
            "relations": [{"from": "她", "to": "他", "kind": "情人",
                          "note": f"第{idx}章的关系"}],
            "facts": [f"第{idx}章确立的事实"],
            "quotes": [{"text": f"第{idx}章的信", "why": "标题",
                        "kind": "literary", "tag": "意象", "who": ""},
                       {"text": "完全捏造的句子", "why": "不该保留"}],
            "threads": [f"第{idx}章的线索"],
            "reading": f"第{idx}章的作用",
            "compact": f"第{idx}章：他收到一封信。",
        }

    @staticmethod
    def _data(content, tool_calls, prompt) -> dict:
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = [{
                "id": "call-1", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            } for name, args in tool_calls]
        return {"choices": [{"message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt, "completion_tokens": 40,
                          "prompt_tokens_details": {
                              "cached_tokens": max(0, prompt - 100)},
                          "completion_tokens_details": {
                              "reasoning_tokens": 10}}}


async def test_one_chapter(tmp: Path) -> None:
    db = make_books(tmp)
    store = ReadingStore(tmp / "reading.db")
    s = FakeSession(FakeCfg(), db, store, "b1", window_tokens=100_000,
                    threshold_ratio=0.35, max_tool_rounds=2)
    res = await s.run(limit=1, min_chars=0)
    note = s.notes.get(0) or {}
    rows = store.notes_for("b1")
    check("笔记落库", bool(rows) and rows[0]["compact"] == "第0章：他收到一封信。")
    check("工具调用成对",
          any(m.get("role") == "tool" for m in s.messages)
          and any(m.get("tool_calls") for m in s.messages
                  if m.get("role") == "assistant"))
    check("工具被真正执行", s.tools.calls.get("read_passage") == 1,
          str(s.tools.calls))
    check("引文逐字校验通过", len(note.get("quotes") or []) == 1,
          repr(note.get("quotes")))
    check("捏造引文被丢弃", note.get("unverified_quotes") == 1,
          repr(note.get("unverified_quotes")))
    check("系统提示逐字稳定",
          all(c[0].get("content") == prompts.READING_SYSTEM for c in s.calls))
    check("第二通调用是前者的前缀",
          s.calls[1][:len(s.calls[0])] == s.calls[0])
    check("没有失败章", not res.get("failures"), repr(res.get("failures")))
    db.close()
    store.close()


async def test_compact_context(tmp: Path) -> None:
    db = make_books(tmp)
    store = ReadingStore(tmp / "reading.db")
    s = FakeSession(FakeCfg(), db, store, "b1", window_tokens=100_000,
                    max_tool_rounds=1, context_mode="compact")
    s.tool_once = False
    await s.run(limit=1, min_chars=0)
    last = s.messages[-1].get("content") or ""
    check("compact 模式：上下文只放 compact",
          last.startswith("第 0 章笔记：")
          and "第0章：他收到一封信。" in last, last[:120])
    check("compact 模式：完整笔记仍落库",
          store.notes_for("b1")[0]["compact"] == "第0章：他收到一封信。")
    db.close()
    store.close()


async def test_resume(tmp: Path) -> None:
    db = make_books(tmp)
    store = ReadingStore(tmp / "reading.db")
    first = FakeSession(FakeCfg(), db, store, "b1", window_tokens=100_000,
                        max_tool_rounds=1)
    first.tool_once = False
    await first.run(limit=2, min_chars=0)
    check("续跑前：两章已落库", len(store.notes_for("b1")) == 2)

    second = FakeSession(FakeCfg(), db, store, "b1", window_tokens=100_000,
                         max_tool_rounds=1, resume=True)
    second.tool_once = False
    await second.run(limit=5, min_chars=0)
    check("续跑：跳过已完成章",
          second.n_calls == 3, f"calls={second.n_calls}")
    check("续跑：已完成章进入上下文摘要",
          "<reading-summary>" in (second.messages[1].get("content") or ""))
    check("续跑：全部笔记都在", len(store.notes_for("b1")) == 5)
    check("续跑：笔记带提示词版本",
          store.notes_for(
              "b1", prompt_version=prompts.READING_PROMPT_VERSION
          )[0].get("prompt_version") == prompts.READING_PROMPT_VERSION)
    db.close()
    store.close()


def test_gate(tmp: Path) -> None:
    db = make_books(tmp)
    chapters = {int(c.chapter_index): c for c in db.get_chapters("b1")}
    tools = ReadingTools(db, "b1", chapters, current_chapter=1)
    out = json.loads(tools.execute("read_passage", {"chapter": 3}))
    check("未来章：read_passage 拒绝", "不在已读范围" in out.get("error", ""),
          repr(out))
    out = json.loads(tools.execute("verify_quote", {"text": "第3章的信"}))
    check("未来章：verify_quote 找不到", out.get("found") is False, repr(out))
    out = json.loads(tools.execute("search_book",
                                   {"query": "他走进房间", "limit": 12}))
    chs = {r["chapter"] for r in out.get("results") or []}
    check("未来章：search_book 过滤", bool(chs) and max(chs) <= 1, str(chs))
    out = json.loads(tools.execute("search_book", {"query": "汤森"}))
    check("空结果带防剧透提示",
          "不要用后文" in out.get("note", ""), repr(out))
    out = json.loads(tools.execute("coverage", {}))
    check("coverage 报当前读点", out.get("read_through") == 1, repr(out))
    db.close()


async def _no_map(*args, **kwargs) -> str:
    return ""


async def test_reset(tmp: Path) -> None:
    db = make_books(tmp, n=6)
    store = ReadingStore(tmp / "reading.db")
    s = FakeSession(FakeCfg(), db, store, "b1", window_tokens=100_000,
                    threshold_ratio=0.001, recent_chapters=2,
                    max_tool_rounds=1)
    s.tool_once = False
    s.prompt_step = 200
    s.map_synth = _no_map          # 合成失败也必须能换窗（机械兜底）
    await s.run(limit=6, min_chars=0)
    rows = store.get_conn().execute(
        "SELECT * FROM reading_resets ORDER BY seq").fetchall()
    check("触发换窗", s.resets >= 1 and bool(rows), f"resets={s.resets}")
    first = dict(rows[0]) if rows else {}
    check("摘要含被换掉的章", "第0章：他收到一封信。" in first.get("summary", ""),
          first.get("summary", "")[:120])
    check("摘要不含保留章", "第2章：他收到一封信。" not in first.get("summary", ""))
    check("换窗后 system 仍是第一条",
          s.messages[0].get("content") == prompts.READING_SYSTEM)
    check("换窗后摘要在最前",
          "<reading-summary>" in (s.messages[1].get("content") or ""))
    check("最近章原文仍在",
          any("第5章的信" in (m.get("content") or "") for m in s.messages))
    db.close()
    store.close()


def test_consumers(tmp: Path) -> None:
    db = make_books(tmp, n=3)
    store = ReadingStore(tmp / "reading.db")
    run = store.start_run("b1")
    for i in range(3):
        store.save_note(run, "b1", i, FakeSession._note(i),
                        chapter_title=f"第{i}章")
    notes = store.notes_for("b1", as_of=1)
    kernel = roleplay_kernel(notes, 1)
    check("内核只到 as_of", "第1章的事实" in kernel and "第2章的事实" not in kernel,
          kernel[:200])
    check("内核带角色状态", "## 她" in kernel and "知道：第1章的事实" in kernel)
    check("内核带关系", "## 关系" in kernel and "—(情人)→" in kernel,
          kernel[:300])
    scene = scene_view(notes, 1, window=0)
    check("场景视图只到本章", "第1章" in scene and "第2章" not in scene,
          scene[:200])
    check("场景视图带关系", "关系：" in scene and "第1章的关系" in scene,
          scene[:300])
    db.close()
    store.close()


def test_flight_recorder(tmp: Path) -> None:
    db = make_books(tmp)
    store = ReadingStore(tmp / "reading.db")
    rec = ContextRecorder(tmp / "context_log.db")
    s = FakeSession(FakeCfg(), db, store, "b1", window_tokens=100_000,
                    max_tool_rounds=1, recorder=rec)
    s._call_no = 1
    s.tools.current_chapter = 1
    data = {"choices": [{"message": {
        "content": "{\"compact\": \"x\"}",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read_passage",
                                     "arguments": "{}"}}],
        "reasoning_content": "想一想"}}]}
    s._record_call(
        [{"role": "system", "content": "S"},
         {"role": "user", "content": "U"}],
        data, "fake-model", time.monotonic() - 0.1, [{"x": 1}])
    rows = rec.list_rounds(f"reading:{s.run_id}")
    row = rec.get_round(rows[0]["id"]) if rows else {}
    check("记录 system_prompt", row.get("system_prompt") == "S", repr(row))
    check("记录 messages",
          json.loads(row.get("messages_json") or "[]")[0]["content"] == "U")
    check("记录思维链", "<think>" in (row.get("response_text") or ""))
    check("记录模型与轮次", row.get("model") == "fake-model"
          and row.get("react_round") == 1)
    rec.close()
    db.close()
    store.close()


def test_alias_merge(tmp: Path) -> None:
    store = ReadingStore(tmp / "reading.db")
    run = store.start_run("b1")
    first = {"scene": "", "characters": [
        {"name": "情人", "wants": "旧目标", "knows": "旧知识"}],
        "facts": [], "quotes": [], "threads": [], "reading": "",
        "compact": "第0章"}
    second = {"scene": "", "characters": [
        {"name": "汤森", "aliases": ["情人"], "wants": "新目标",
         "knows": "新知识"}],
        "facts": [], "quotes": [], "threads": [], "reading": "",
        "compact": "第1章"}
    store.save_note(run, "b1", 0, first)
    store.save_note(run, "b1", 1, second)
    kernel = roleplay_kernel(store.notes_for("b1", as_of=1), 1)
    check("别名合并成一个角色",
          "## 汤森" in kernel and "## 情人" not in kernel,
          kernel[:200])
    check("别名保留旧称呼", "别名：情人" in kernel, kernel[:200])
    check("新章状态覆盖旧章", "想要：新目标" in kernel, kernel[:200])
    store.close()


def test_map_projection(tmp: Path) -> None:
    store = ReadingStore(tmp / "reading.db")
    run = store.start_run("b1")
    for i in range(6):
        store.save_note(run, "b1", i, FakeSession._note(i),
                        chapter_title=f"第{i}章")
    notes = store.notes_for("b1")
    plain = chapter_map(notes, max_chars=10_000)
    check("地图逐章升序",
          plain.index("第 0 章") < plain.index("第 5 章")
          and "第5章：他收到一封信。" in plain, plain[:200])
    cut = chapter_map(notes, as_of=2, max_chars=10_000)
    check("地图按 as_of 截断",
          "第 2 章" in cut and "第 3 章" not in cut, cut[:200])
    store.save_map("b1", start_chapter=0, end_chapter=2,
                   text="前两章她一直在等信。")
    with_arc = chapter_map(notes, store.maps_for("b1"), as_of=5,
                           max_chars=10_000)
    check("卷图顶替逐章行",
          "截至第 2 章：前两章她一直在等信。" in with_arc
          and "第 1 章 第1章" not in with_arc, with_arc[:300])
    check("未成卷的章仍是逐章行",
          "第 3 章 第3章：第3章：他收到一封信。" in with_arc, with_arc[:300])
    tight = chapter_map(notes, max_chars=120)
    check("预算紧时留最近章",
          "第 5 章" in tight and "第 0 章" not in tight, tight)
    check("卷图优先于逐章行进预算",
          "截至第 2 章" in chapter_map(store.notes_for("b1"),
                                     store.maps_for("b1"), max_chars=260))
    store.close()


def test_map_consumers(tmp: Path) -> None:
    store = ReadingStore(tmp / "reading.db")
    run = store.start_run("b1")
    for i in range(3):
        store.save_note(run, "b1", i, FakeSession._note(i),
                        chapter_title=f"第{i}章")
    notes = store.notes_for("b1", as_of=1)
    mp = chapter_map(notes, as_of=1)
    kernel = roleplay_kernel(notes, 1, map_text=mp)
    check("内核带头部地图", kernel.startswith("# 扮演内核（截至第 1 章）")
          and "<reading-map" in kernel, kernel[:200])
    check("内核仍保留角色状态", "知道：第1章的事实" in kernel, kernel[:200])
    scene = scene_view(notes, 1, window=0, map_text=mp)
    check("伴读视图带前情地图",
          scene.startswith("<reading-map") and "关系：" in scene,
          scene[:200])
    store.close()


async def test_reset_uses_map(tmp: Path) -> None:
    db = make_books(tmp, n=6)
    store = ReadingStore(tmp / "reading.db")
    s = FakeSession(FakeCfg(), db, store, "b1", window_tokens=100_000,
                    threshold_ratio=0.001, recent_chapters=2,
                    max_tool_rounds=1)
    s.tool_once = False
    s.prompt_step = 200

    async def _fake_map(*args, **kwargs) -> str:
        return "前情地图：她一直在等信。"

    s.map_synth = _fake_map
    await s.run(limit=6, min_chars=0)
    first = s.messages[1].get("content") or ""
    check("换窗用合成地图", "<reading-map>" in first
          and "前情地图：她一直在等信。" in first, first[:200])
    maps = store.maps_for("b1", level="reset")
    check("换窗地图落库", bool(maps) and "等信" in maps[-1]["text"], str(maps))
    db.close()
    store.close()


async def test_build_arc_maps(tmp: Path) -> None:
    store = ReadingStore(tmp / "reading.db")
    run = store.start_run("b1")
    for i in range(10):
        store.save_note(run, "b1", i, FakeSession._note(i),
                        chapter_title=f"第{i}章")
    seen: list[str] = []

    async def _fake(cfg, prev, entries, **kwargs) -> str:
        seen.append(prev)
        return f"卷图 {entries[0][0]}–{entries[-1][0]}"

    orig = map_mod.synthesize
    map_mod.synthesize = _fake
    try:
        built = await map_mod.build_arc_maps(None, store, "b1", arc_size=4)
    finally:
        map_mod.synthesize = orig
    check("按卷切段（10 章 / 4 = 2 卷）", len(built) == 2, str(built))
    check("第二卷带上第一卷的图", seen[1] == "卷图 0–3", str(seen))
    merged = map_mod.map_for(store, "b1")
    check("消费入口合并卷图与剩余章",
          "截至第 7 章：卷图 4–7" in merged and "第 8 章" in merged,
          merged[:300])
    check("累积卷图只取最新一张",
          merged.count("卷图") == 1 and "卷图 0–3" not in merged, merged)
    again = await map_mod.build_arc_maps(None, store, "b1", arc_size=4)
    check("已成的卷不重做", not again, str(again))
    store.close()


def test_quote_tags_parse(tmp: Path) -> None:
    db = make_books(tmp)
    store = ReadingStore(tmp / "reading.db")
    s = FakeSession(FakeCfg(), db, store, "b1", window_tokens=100_000)
    s.tools.current_chapter = 0
    raw = json.dumps({
        "scene": "", "characters": [], "relations": [], "facts": [],
        "threads": [], "reading": "", "compact": "x",
        "quotes": [
            {"text": "第0章的信", "kind": "literary",
             "tag": "自由间接引语", "who": "她"},
            {"text": "他走进房间", "kind": "kernel",
             "tag": "模型自造的标签", "who": "她"},
            {"text": "第0章的信", "kind": "kernel", "tag": "自欺"},
            {"text": "书里没有这句话", "kind": "kernel", "tag": "自欺"},
        ]}, ensure_ascii=False)
    note = s._parse_note(raw) or {}
    quotes = note.get("quotes") or []
    check("重复引文只留一条", len(quotes) == 2, repr(quotes))
    check("literary 保留视角 who",
          quotes[0].get("kind") == "literary"
          and quotes[0].get("who") == "她", repr(quotes[0]))
    check("词表外 tag 记 other 但保留 kind",
          quotes[1].get("tag") == "other"
          and quotes[1].get("kind") == "kernel"
          and quotes[1].get("who") == "她", repr(quotes[1]))
    check("捏造引文仍被丢弃", note.get("unverified_quotes") == 1,
          repr(note.get("unverified_quotes")))
    check("笔记记分级版本",
          note.get("quote_tags_version") == prompts.TAG_VERSION)
    db.close()
    store.close()


def test_highlights_projection(tmp: Path) -> None:
    store = ReadingStore(tmp / "reading.db")
    run = store.start_run("b1")
    store.save_note(run, "b1", 0, {
        "compact": "第0章", "characters": [], "relations": [], "facts": [],
        "threads": [], "reading": "", "scene": "",
        "quotes": [
            {"text": "海在我四周翻腾", "kind": "literary", "tag": "比喻",
             "who": "", "why": "把情绪外化成海", "chapter": 0, "chunk": 0},
            {"text": "我一直在等你", "kind": "kernel", "tag": "掩饰",
             "who": "阿莉莎", "why": "嘴上等，行动在推", "chapter": 0,
             "chunk": 0}]}, chapter_title="第0章")
    store.save_note(run, "b1", 1, {
        "compact": "第1章", "characters": [], "relations": [], "facts": [],
        "threads": [], "reading": "", "scene": "",
        "quotes": [{"text": "你爱上了一个影子", "kind": "kernel",
                    "tag": "自欺", "who": "阿莉莎", "why": "否定对方所爱",
                    "chapter": 1, "chunk": 0}]}, chapter_title="第1章")
    notes = store.notes_for("b1")
    lit = highlights_for(notes, kind="literary")
    check("按 kind 检索", len(lit) == 1 and lit[0]["tag"] == "比喻",
          repr(lit))
    kern = highlights_for(notes, who="阿莉莎")
    check("按 who 检索", len(kern) == 2, repr(kern))
    asof = highlights_for(notes, as_of=0)
    check("高亮按 as_of 截断", len(asof) == 2, repr(asof))
    tagged = highlights_for(notes, tag="自欺")
    check("按 tag 检索", len(tagged) == 1, repr(tagged))
    check("渲染带锚点",
          "[0:0]" in render_highlights(lit), render_highlights(lit))
    store.close()


async def test_backfill_tags(tmp: Path) -> None:
    store = ReadingStore(tmp / "reading.db")
    run = store.start_run("b1")
    for i in range(3):
        store.save_note(run, "b1", i, {
            "compact": f"第{i}章", "characters": [{"name": "她"}],
            "relations": [], "facts": [], "threads": [], "reading": "",
            "scene": "", "quotes": [
                {"text": f"第{i}章的信", "why": "标题", "chapter": i,
                 "chunk": 0}]})

    async def _fake(cfg, entries, **kwargs):
        return {e[0]: {"kind": "kernel", "tag": "自欺", "who": "她"}
                for e in entries}

    done = await tagging.backfill_tags(None, store, "b1", tagger=_fake)
    notes = store.notes_for("b1")
    check("回填覆盖全部章", len(done) == 1 and len(notes) == 3, str(done))
    check("回填写入 kind/tag/who",
          all(q.get("kind") == "kernel" and q.get("tag") == "自欺"
              and q.get("who") == "她"
              for n in notes for q in n["note"]["quotes"]), repr(notes[0]))
    check("回填记版本",
          all(n["note"].get("quote_tags_version") == prompts.TAG_VERSION
              for n in notes))
    again = await tagging.backfill_tags(None, store, "b1", tagger=_fake)
    check("已打标不重做", not again, str(again))
    store.close()


async def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        await test_one_chapter(root / "a")
        (root / "a2").mkdir()
        await test_compact_context(root / "a2")
        (root / "r").mkdir()
        await test_resume(root / "r")
        (root / "b").mkdir()
        test_gate(root / "b")
        (root / "c").mkdir()
        await test_reset(root / "c")
        (root / "d").mkdir()
        test_consumers(root / "d")
        (root / "e").mkdir()
        test_alias_merge(root / "e")
        (root / "f").mkdir()
        test_flight_recorder(root / "f")
        (root / "g").mkdir()
        test_map_projection(root / "g")
        (root / "h").mkdir()
        test_map_consumers(root / "h")
        (root / "i").mkdir()
        await test_reset_uses_map(root / "i")
        (root / "j").mkdir()
        await test_build_arc_maps(root / "j")
        (root / "k").mkdir()
        test_quote_tags_parse(root / "k")
        (root / "l").mkdir()
        test_highlights_projection(root / "l")
        (root / "m").mkdir()
        await test_backfill_tags(root / "m")
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))



