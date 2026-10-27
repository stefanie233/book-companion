"""``/api/chat`` 上下文管理测试（零 LLM）。

盯四件事：
1. 工具调用/结果**结构化落盘**，而不是在落盘时被压成纯文本；
2. 下一轮重建历史时 call/output 成对出现，顺序正确；
3. 裁剪只发生在 user 回合边界，绝不切开一对 call/output；
4. 超出预算的老回合被压成摘要，原始会话不落盘、不丢消息。

跑法：``python scripts/test_chat_context.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aiohttp.test_utils import TestClient, TestServer                 # noqa: E402

import api_server                                                     # noqa: E402
import chat_context                                                   # noqa: E402
from config import Config                                             # noqa: E402
from content import ContentBlock, ConversationMessage                  # noqa: E402
from services.session_service import SessionService                    # noqa: E402
from storage.db_session import SessionDB                               # noqa: E402

FAILED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"ok   {label}")
    else:
        print(f"FAIL {label}\n     {detail}")
        FAILED += 1


class NoBookDB:
    def __getattr__(self, name):
        raise AssertionError(f"home mode touched books db: {name}")


class ToolThenTextClient:
    """第一轮调 get_time，第二轮给正文；记录每次收到的 messages。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def stream(self, system_prompt, messages, tools, _x):
        self.calls.append({
            "system_prompt": system_prompt,
            "messages": [dict(m) for m in messages],
            "tools": tools,
        })
        if len(self.calls) == 1:
            yield ("<think>先看看时间</think>",
                   [{"id": "call-1", "name": "get_time",
                     "arguments": "{}"}], None)
        else:
            yield ("现在可以回答了。", [], None)


class TextOnlyClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def stream(self, system_prompt, messages, tools, _x):
        self.calls.append({"messages": [dict(m) for m in messages]})
        yield ("收到。", [], None)


def _unit_checks() -> None:
    # ── token 估算：中文按 UTF-8 字节算，不按字符 ────────────────
    check("approx_tokens 中文按字节估算",
          chat_context.approx_tokens("中" * 100) == 75,
          str(chat_context.approx_tokens("中" * 100)))

    # ── 工具输出截断：头尾都保留 ────────────────────────────────
    text = "HEAD" + "x" * 20_000 + "TAIL"
    cut = chat_context.truncate_tool_output(text, max_tokens=100)
    check("工具输出截断：保留头部", cut.startswith("HEAD"), cut[:20])
    check("工具输出截断：保留尾部", cut.endswith("TAIL"), cut[-20:])
    check("工具输出截断：带省略说明", "中间省略" in cut, cut[:200])
    check("工具输出截断：短文本原样",
          chat_context.truncate_tool_output("short") == "short")
    check("工具输出预算：读正文章节放宽",
          chat_context.tool_output_budget("writing_read_chapter")
          > chat_context.tool_output_budget("get_time"),
          f"{chat_context.tool_output_budget('writing_read_chapter')} vs "
          f"{chat_context.tool_output_budget('get_time')}")

    # ── call/output 配平 ───────────────────────────────────────
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "type": "function",
             "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        {"role": "tool", "tool_call_id": "orphan", "content": "x"},
    ]
    fixed = chat_context.normalize_call_pairs(msgs)
    check("配平：缺失 output 补 aborted",
          fixed[1] == {"role": "tool", "tool_call_id": "c1", "content": "aborted"},
          repr(fixed))
    check("配平：output 按 call 顺序重排",
          fixed[2].get("tool_call_id") == "c2", repr(fixed))
    check("配平：孤儿 output 被丢弃",
          not any(m.get("tool_call_id") == "orphan" for m in fixed), repr(fixed))

    # ── 会话消息 → API messages ────────────────────────────────
    persisted = [
        ConversationMessage(
            role="user",
            blocks=[ContentBlock(type="text", text="第一问")],
        ),
        ConversationMessage(
            role="assistant",
            blocks=[
                ContentBlock(type="thinking", thinking="内部推理"),
                ContentBlock(type="text", text="我查一下"),
                ContentBlock.tool_use_block("c1", "get_time", "{}"),
            ],
        ),
        ConversationMessage(
            role="tool",
            blocks=[ContentBlock.tool_result_block("c1", "get_time", '{"now":1}')],
        ),
        ConversationMessage(
            role="assistant",
            blocks=[ContentBlock(type="text", text="第一答")],
        ),
        ConversationMessage(
            role="user",
            blocks=[
                ContentBlock(type="interrupt", text="上一轮被打断"),
                ContentBlock(type="text", text="第二问"),
            ],
        ),
    ]
    api = chat_context.session_messages_to_api(persisted)
    check("重建：条数正确", len(api) == 5, repr(api))
    check("重建：thinking 不回灌",
          all("内部推理" not in str(m.get("content")) for m in api), repr(api))
    check("重建：assistant 带 tool_calls",
          api[1]["role"] == "assistant"
          and api[1]["tool_calls"][0]["id"] == "c1", repr(api[1]))
    check("重建：tool 按 id 配对",
          api[2] == {"role": "tool", "tool_call_id": "c1",
                     "content": '{"now":1}'}, repr(api[2]))
    check("重建：中断注记拼在用户消息前",
          api[4]["content"].startswith("上一轮被打断"), repr(api[4]))

    # ── 预算裁剪：在回合边界切，不切开 call/output ─────────────
    long_msgs = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "old", "type": "function",
             "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "old", "content": "旧结果" + "旧" * 1000},
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "长" * 2000},
    ]
    kept, dropped = chat_context.build_prompt_history(long_msgs, 2000)
    check("裁剪：旧回合整段被裁", bool(dropped), repr(dropped))
    check("裁剪：旧 call 不会单独留在窗口里",
          not any(m.get("tool_call_id") == "old" for m in kept), repr(kept))
    check("裁剪：摘要插在最前",
          kept and kept[0]["role"] == "user"
          and kept[0]["content"].startswith("<conversation-summary>"),
          repr(kept[:2]))
    check("裁剪：新回合完整保留",
          any(m.get("content") == "长" * 2000 for m in kept), repr(kept))
    check("裁剪：总预算不超", chat_context.estimate_tokens(kept) <= 2000,
          str(chat_context.estimate_tokens(kept)))

    # ── 最后一回合超预算也必须整回合保留 ───────────────────────
    huge = [
        {"role": "user", "content": "短"},
        {"role": "user", "content": "长" * 4000},
    ]
    kept2, _ = chat_context.build_prompt_history(huge, 500)
    check("裁剪：最后一回合超预算也不切碎",
          kept2[-1].get("content") == "长" * 4000, repr(kept2[-1])[:80])


async def _e2e_checks(tmp: Path) -> None:
    class TestConfig(Config):
        @property
        def writing_db_path(self) -> Path:
            return tmp / "data" / "writing.db"

    cfg = TestConfig(project_dir=ROOT)
    sdb = SessionDB(tmp / "data" / "sessions.db")
    sdb.initialize()
    ssvc = SessionService(sdb, cfg)

    fake = ToolThenTextClient()
    app = api_server.create_app(NoBookDB(), config=cfg, session_service=ssvc)
    app["api_client"] = fake

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat", json={
            "message": "现在几点", "mode": "immersive", "page": "home",
            "stream": "v1",
        })
        body = await resp.text()
        check("E2E：首轮返回 200", resp.status == 200, body[:300])
        sid = resp.headers.get("X-Session-Id") or ""
        check("E2E：拿到 session id", bool(sid), repr(sid))

        # 动作：两轮 LLM（工具轮 + 正文轮）
        check("E2E：两轮 LLM", len(fake.calls) == 2, str(len(fake.calls)))

        # 第二轮请求里必须已经带着工具对（结构化历史）
        msgs = fake.calls[1]["messages"]
        asst = [m for m in msgs
                if m.get("role") == "assistant" and m.get("tool_calls")]
        tools = [m for m in msgs if m.get("role") == "tool"]
        check("E2E：模型看到 assistant tool_calls", len(asst) == 1, repr(msgs))
        check("E2E：模型看到配对 tool 结果",
              len(tools) == 1 and tools[0].get("tool_call_id") == "call-1",
              repr(tools))
        check("E2E：<think> 不回灌",
              all("<think>" not in str(m.get("content")) for m in msgs),
              repr(asst))

        # 落盘：user / assistant(tool_use) / tool / assistant(正文)
        sess = ssvc.load_messages(sid)
        roles = [m.role for m in sess.messages]
        check("E2E：落盘四条消息",
              roles == ["user", "assistant", "tool", "assistant"], repr(roles))
        if len(sess.messages) >= 3:
            check("E2E：assistant 落 tool_use",
                  any(b.type == "tool_use" and b.tool_use_id == "call-1"
                      for b in sess.messages[1].blocks),
                  repr([b.type for b in sess.messages[1].blocks]))
            check("E2E：tool 落 tool_result",
                  sess.messages[2].blocks[0].type == "tool_result"
                  and sess.messages[2].blocks[0].tool_use_id == "call-1",
                  repr(sess.messages[2].blocks))
            check("E2E：thinking 也落盘（给重载 UI）",
                  any(b.type == "thinking" for b in sess.messages[1].blocks),
                  repr([b.type for b in sess.messages[1].blocks]))

        # 会话加载：工具消息不出现；只有 tool_use 的 assistant 轮也不
        # 产出正文气泡（有 thinking 时保留 thinking 行，供重载 UI 展示）。
        lr = await client.get(f"/api/sessions/{sid}")
        data = await lr.json()
        load_roles = [m["role"] for m in data.get("messages", [])]
        check("E2E：会话加载过滤 tool", "tool" not in load_roles,
              repr(load_roles))
        check("E2E：同一轮多条 assistant 合并成一个气泡",
              load_roles == ["user", "assistant"]
              and data["messages"][1].get("content") == "现在可以回答了。"
              and "先看看时间" in (data["messages"][1].get("thinking") or ""),
              repr([(m["role"], m.get("content"), m.get("thinking"))
                    for m in data["messages"]]))
        check("E2E：加载出的正文正确",
              data.get("messages", [{}])[-1].get("content") == "现在可以回答了。",
              repr(data.get("messages")))

    # ── 第二轮对话：新 app（同一 session db）验证历史继承 ──────────
    fake2 = TextOnlyClient()
    app2 = api_server.create_app(NoBookDB(), config=cfg, session_service=ssvc)
    app2["api_client"] = fake2
    async with TestClient(TestServer(app2)) as client2:
        resp2 = await client2.post("/api/chat", json={
            "message": "继续", "mode": "immersive", "page": "home",
            "session_id": sid, "stream": "v1",
        })
        check("E2E：第二轮返回 200", resp2.status == 200,
              (await resp2.text())[:300])
        hist = fake2.calls[0]["messages"]
        check("E2E：第二轮继承 assistant tool_calls",
              any(m.get("role") == "assistant" and m.get("tool_calls")
                  for m in hist), repr(hist))
        check("E2E：第二轮继承配对 tool 结果",
              any(m.get("role") == "tool"
                  and m.get("tool_call_id") == "call-1" for m in hist),
              repr(hist))


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bc_chat_ctx_"))
    (tmp / "data").mkdir(parents=True, exist_ok=True)

    _unit_checks()
    await _e2e_checks(tmp)

    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
