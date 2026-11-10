"""写作轮次验收 + 异常文案的测试（零 LLM）。

盯三件事：

1. 未捕获异常压成工具结果时**永远非空**——2026-10-29 17:51 的事故里，
   三次超时后工具只回了 ``{"error": ""}``，模型既不知道原因也没法补救；
2. 验收器只认机械事实：说了写却没字、工具没跑完、写了正文没结算；
3. 催过的不再催，避免死循环。

跑法：``python scripts/test_writing_guard.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.writing_service import WritingService            # noqa: E402
from storage.writing_db import WritingDB                       # noqa: E402
from tools import ToolRegistry, ToolSpec, describe_exception   # noqa: E402
from writegraph.chat_tools import (                             # noqa: E402
    WritingGuard,
    _infra_failure,
    _promises_write,
    _settle_fixable,
    build_writing_registry,
)

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def _setup():
    db = WritingDB(os.path.join(tempfile.mkdtemp(prefix="wguard_"), "w.db"))
    svc = WritingService(SimpleNamespace(project_dir=ROOT), db=db)
    work_id = svc.create_work("验收测试", genre="悬疑")
    svc.create_chapter(work_id, title="第一章")
    ctx = {"work_id": work_id, "chapter_index": 1}
    return db, svc, ctx


# ── 异常文案 ──────────────────────────────────────────────────────────────

def test_describe_exception() -> None:
    for exc in (TimeoutError(), ValueError(), KeyError(),
                RuntimeError(""), Exception()):
        text = describe_exception(exc)
        check(f"异常文案非空：{type(exc).__name__}", bool(text.strip()), True)
    check("超时带可执行建议",
          "重试" in describe_exception(TimeoutError()), True)
    check("业务消息原样保留",
          describe_exception(RuntimeError("API 500: boom")),
          "RuntimeError: API 500: boom")

    # 工具层兜住异常后，回灌给模型的结果不能是空错误。
    reg = ToolRegistry()

    def boom(**_kw):
        raise TimeoutError()

    reg.register(ToolSpec(name="boom", description="", parameters={},
                          execute=boom, toolset="writing"))
    payload = json.loads(reg.execute("boom", "{}"))
    check("工具结果有错误文本", bool(str(payload.get("error") or "").strip()),
          True)


# ── 分类 ──────────────────────────────────────────────────────────────────

def test_classification() -> None:
    check("空错误算基础设施失败",
          _infra_failure(json.dumps({"error": ""})), "（空错误）")
    check("超时文案算基础设施失败",
          bool(_infra_failure(json.dumps({"error": "TimeoutError: x"}))), True)
    check("业务拒绝不算基础设施失败",
          _infra_failure(json.dumps({"ok": False, "error": "结算被拒：证据对不上"})),
          "")
    check("成功不是失败",
          _infra_failure(json.dumps({"ok": True, "word_count": 12})), "")

    check("承诺写正文",
          _promises_write("我顺手把第一章建出来并写成正文。"), True)
    check("明确的否定不算承诺",
          _promises_write("先不写正文，等你确认。"), False)
    check("如实说没写不算承诺",
          _promises_write("还没写正文，先确认设定。"), False)
    check("完成态不算承诺",
          _promises_write("正文已经写好了，下一步可以结算。"), False)


# ── 验收器 ────────────────────────────────────────────────────────────────

def test_promise_without_draft() -> None:
    _db, svc, ctx = _setup()
    guard = WritingGuard(svc, ctx)
    issues = guard.check("我把第一章建出来并写成正文。")
    check("说了要写：报未完成", len(issues), 1)
    check("说了要写：指向调用工具",
          "writing_draft_chapter" in issues[0], True)

    # 本章已有正文时不再算未完成。
    db = svc.db
    chapter = db.get_chapter(ctx["work_id"], 1)
    db.save_draft(ctx["work_id"], chapter["id"], "正文" * 50, kind="save")
    guard2 = WritingGuard(svc, ctx)
    check("本章已有正文：不再报", guard2.check("我这就写正文。"), [])


def test_infra_failure_nudge() -> None:
    _db, svc, ctx = _setup()
    guard = WritingGuard(svc, ctx)
    guard.begin_round()
    guard.observe("writing_draft_chapter", json.dumps({"error": ""}))
    issues = guard.check("")
    check("工具没跑完：报一次", len(issues), 1)
    check("工具没跑完：带工具名", "writing_draft_chapter" in issues[0], True)

    # 业务拒绝（ok=false）是模型看得懂的失败，不催。
    guard.begin_round()
    guard.observe("writing_settle_chapter",
                  json.dumps({"ok": False, "error": "结算被拒：x"}))
    check("业务拒绝：不催", guard.check(""), [])


def test_settle_nudge_once() -> None:
    db, svc, ctx = _setup()
    chapter = db.get_chapter(ctx["work_id"], 1)
    db.save_draft(ctx["work_id"], chapter["id"], "正文" * 80, kind="save")

    guard = WritingGuard(svc, ctx)
    guard.begin_round()
    guard.observe("writing_draft_chapter",
                  json.dumps({"ok": True, "chapter_index": 1}))
    first = guard.check("写完了，我先汇报。")
    check("写了正文没结算：报一次", len(first), 1)
    check("写了正文没结算：指向结算工具",
          "writing_settle_chapter" in first[0], True)
    check("同一章不重复催", guard.check("写完了，我先汇报。"), [])

    # 结算过（哪怕被拒）就不再催——模型已经知道结果。
    guard2 = WritingGuard(svc, ctx)
    guard2.begin_round()
    guard2.observe("writing_draft_chapter",
                   json.dumps({"ok": True, "chapter_index": 1}))
    guard2.observe("writing_settle_chapter",
                   json.dumps({"ok": False, "error": "结算被拒：x"}))
    check("结算尝试过：不再催", guard2.check(""), [])


class _FakeStream:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.frames.append(data)


def test_tool_heartbeat() -> None:
    """长工具执行期间要发注释帧，否则连接会被判死。"""
    from api.routes import chat as chat_mod

    registry = ToolRegistry()

    async def slow(**_kw):
        await asyncio.sleep(0.05)
        return json.dumps({"ok": True, "value": 7})

    registry.register(ToolSpec(name="slow", description="", parameters={},
                               execute=lambda **_kw: "{}",
                               execute_async=slow, toolset="writing"))

    stream = _FakeStream()
    old = chat_mod._TOOL_HEARTBEAT_SECONDS
    chat_mod._TOOL_HEARTBEAT_SECONDS = 0.01
    try:
        result = asyncio.run(
            chat_mod._await_tool_with_heartbeat(stream, registry, "slow", "{}"))
    finally:
        chat_mod._TOOL_HEARTBEAT_SECONDS = old

    check("心跳：工具结果原样返回", json.loads(result).get("value"), 7)
    check("心跳：长工具期间发了帧", bool(stream.frames), True)
    check("心跳：不是 data: 行（两版前端都会跳过）",
          all(not f.startswith(b"data:") for f in stream.frames), True)


def test_settle_retry_nudge() -> None:
    """引用/格式类结算拒绝要催一次；证据类不催。"""
    _db, svc, ctx = _setup()
    guard = WritingGuard(svc, ctx)
    guard.begin_round()
    guard.observe("writing_settle_chapter", json.dumps({
        "ok": False,
        "error": "结算被拒：fact_ops[0] subject_ref 不在作品图中：tmp-char-1"
                 "；可用的 subject_ref：人物A",
    }))
    issues = guard.check("")
    check("可修的结算拒绝：催一次", len(issues), 1)
    check("可修的结算拒绝：指向结算工具",
          "writing_settle_chapter" in issues[0], True)
    check("可修的结算拒绝：不重复催", guard.check(""), [])

    # 证据类拒绝重试会诱使模型编证据，不催。
    guard2 = WritingGuard(svc, ctx)
    guard2.begin_round()
    guard2.observe("writing_settle_chapter", json.dumps({
        "ok": False,
        "error": "结算被拒：fact_ops[0] evidence_text 未在本章正文中逐字出现",
    }))
    check("证据类拒绝：不催", guard2.check(""), [])

    check("分类：引用问题可修",
          _settle_fixable("fact_ops[0] subject_ref 不在作品图中：x"), True)
    check("分类：证据问题不可修",
          _settle_fixable("evidence_text 未在本章正文中逐字出现"), False)


def test_draft_progress() -> None:
    """写正文时要把 stage/delta 报给调用方（前端状态行），正文本身不上屏。"""
    _db, svc, ctx = _setup()
    chapter = svc.chapter(ctx["work_id"], 1)
    svc.set_contract(chapter["id"], purpose="推进")

    class Client:
        config = SimpleNamespace(model="fake")

        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario="", max_tokens=None):
            yield "第一段。", [], None
            yield "第二段。", [], None

    svc.api_client = Client()
    seen: list[dict] = []

    async def progress(event: dict) -> None:
        seen.append(event)

    reg = build_writing_registry(ToolRegistry(), svc, ctx, progress=progress)
    out = json.loads(asyncio.run(reg.execute_async(
        "writing_draft_chapter", json.dumps({"chapter_index": 1}))))
    check("进度：工具成功", out["ok"], True)
    stages = [e["data"].get("stage") for e in seen if e["event"] == "stage"]
    check("进度：报了装配", "assembled" in stages, True)
    check("进度：报了写作", "writing" in stages, True)
    check("进度：增量也报了",
          any(e["event"] == "delta" for e in seen), True)


def test_sse_progress_frames() -> None:
    """SSE 进度帧只带阶段与字数——正文一个字都不许进聊天窗。"""
    from api.routes import chat as chat_mod

    class Resp:
        def __init__(self) -> None:
            self.frames: list[bytes] = []

        async def write(self, data: bytes) -> None:
            self.frames.append(data)

    holder: dict = {"resp": Resp()}
    report = chat_mod._make_tool_progress(holder)

    async def feed() -> None:
        await report({"event": "stage", "data": {"stage": "assembled"}})
        await report({"event": "stage", "data": {"stage": "writing"}})
        for _ in range(5):
            await report({"event": "delta", "data": {"text": "正文"}})
        await report({"event": "stage", "data": {"stage": "revising"}})

    asyncio.run(feed())
    frames = [f.decode("utf-8") for f in holder["resp"].frames]
    check("进度帧：发了", len(frames) >= 3, True)
    check("进度帧：正文不上屏",
          all("正文" not in f for f in frames), True)
    check("进度帧：是 tool_progress",
          all('"type": "tool_progress"' in f for f in frames), True)
    check("进度帧：带累计字数",
          any('"chars": 10' in f for f in frames), True)


def main() -> None:
    test_describe_exception()
    test_classification()
    test_promise_without_draft()
    test_infra_failure_nudge()
    test_settle_nudge_once()
    test_settle_retry_nudge()
    test_tool_heartbeat()
    test_draft_progress()
    test_sse_progress_frames()
    print("\n全绿" if not FAILED else f"\n{FAILED} 项失败")
    sys.exit(0 if not FAILED else 1)


if __name__ == "__main__":
    main()
