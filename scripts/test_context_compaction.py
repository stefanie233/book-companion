"""压缩机制的单测：事件式 checkpoint、摘要复用、工具对不切开、落库。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import context_compaction as cc                       # noqa: E402
import chat_context                                    # noqa: E402
from storage.db_session import SessionDB               # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok   ", name)
    else:
        FAIL += 1
        print("FAIL ", name, detail)


def turn(i: int, size: int = 300) -> list[dict]:
    return [
        {"role": "user", "content": f"问题{i} " + "问" * size},
        {"role": "assistant", "content": f"回答{i} " + "答" * size},
    ]


async def test_event_and_reuse() -> None:
    raw: list[dict] = []
    for i in range(1, 9):
        raw += turn(i)
    store: dict[str, dict] = {}
    calls: list[int] = []

    async def fake_summarize(messages, config):
        calls.append(len(messages))
        return f"摘要{len(calls)}"

    def load(sid):
        return store.get(sid)

    def save(sid, ck):
        store[sid] = {
            "seq": ck.seq, "raw_count": ck.raw_count, "summary": ck.summary,
            "dropped_count": ck.dropped_count,
            "tokens_before": ck.tokens_before,
            "tokens_after": ck.tokens_after, "trigger": ck.trigger,
        }

    out1 = await cc.compact_history(
        raw, 1200, session_id="s1", load_checkpoint=load,
        save_checkpoint=save, summarize=fake_summarize)
    check("首次压缩生成摘要", out1.generated and len(calls) == 1, str(calls))
    check("摘要插在最前", cc.is_summary_message(out1.messages[0]))
    check("checkpoint 记录覆盖条数",
          out1.checkpoint is not None and out1.checkpoint.raw_count > 0,
          repr(out1.checkpoint))

    # 同一份历史 + 同一 checkpoint：不得再次调用摘要，消息逐字一致
    out2 = await cc.compact_history(
        raw, 1200, session_id="s1", load_checkpoint=load,
        save_checkpoint=save, summarize=fake_summarize)
    check("复用：不再调用摘要", len(calls) == 1, str(calls))
    check("复用：消息逐字一致", out2.messages == out1.messages)
    check("复用：generated=False", not out2.generated)

    # 追加足够多的新回合，再压缩：新摘要必须包含旧摘要
    raw2 = raw + turn(9) + turn(10) + turn(11) + turn(12)
    seen: list[list[dict]] = []

    async def fake2(messages, config):
        seen.append(messages)
        return "摘要2"

    out3 = await cc.compact_history(
        raw2, 1200, session_id="s1", load_checkpoint=load,
        save_checkpoint=save, summarize=fake2)
    check("再压缩：seq 递增",
          out3.checkpoint is not None and out3.checkpoint.seq == 2,
          repr(out3.checkpoint))
    check("再压缩：输入含旧摘要",
          bool(seen) and any(cc.is_summary_message(m) for m in seen[0]),
          str(seen[0][:1]))
    check("再压缩：raw_count 前进",
          out3.checkpoint is not None
          and out3.checkpoint.raw_count > out1.checkpoint.raw_count)

    # 会话 fork / 截断后 raw_count 越界：checkpoint 必须作废重压，
    # 否则 new_count <= prior_count 会误判成“没有新消息”。
    store["s1"]["raw_count"] = 999
    hit: list[int] = []

    async def fake3(messages, config):
        hit.append(len(messages))
        return "重压摘要"

    out4 = await cc.compact_history(
        raw, 1200, session_id="s1", load_checkpoint=load,
        save_checkpoint=save, summarize=fake3)
    check("失效 checkpoint 被丢弃并重压",
          len(hit) == 1 and out4.generated
          and out4.checkpoint is not None and out4.checkpoint.seq == 1,
          f"hits={hit} ck={out4.checkpoint}")


async def test_tool_pairs() -> None:
    raw = [
        {"role": "user", "content": "旧问题" + "旧" * 800},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果" + "果" * 800},
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "新回答"},
    ]

    async def fake(messages, config):
        return "S"

    out = await cc.compact_history(raw, 900, summarize=fake)
    kept_ids = [m.get("tool_call_id") for m in out.messages
                if m.get("role") == "tool"]
    calls = [tc.get("id") for m in out.messages
             for tc in (m.get("tool_calls") or [])]
    check("工具对不切开：call 与 output 同进同出",
          (not kept_ids and not calls) or set(kept_ids) == set(calls),
          f"calls={calls} outputs={kept_ids}")


def test_persistence(tmp: Path) -> None:
    db = SessionDB(tmp / "data" / "sessions.db")
    db.initialize()
    db.insert_compaction("s1", seq=1, raw_count=8, summary="S1",
                         dropped_count=4, tokens_before=900, tokens_after=300)
    row = db.latest_compaction("s1")
    check("落库：取回 checkpoint",
          row is not None and row["summary"] == "S1"
          and row["raw_count"] == 8, repr(row))
    db.insert_compaction("s1", seq=2, raw_count=12, summary="S2")
    check("落库：取最新 seq", db.latest_compaction("s1")["seq"] == 2)
    db.close()


async def main() -> int:
    await test_event_and_reuse()
    await test_tool_pairs()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        test_persistence(Path(td))
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
