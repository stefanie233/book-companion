"""``/api/chat`` 的模式隔离测试（零 LLM，aiohttp TestServer）。

盯两件事：**写作页不是阅读页**，以及**会话不跨模式复用**。

写作页复用聊天面板，旧前端（以及标签页切走留下的状态）会把阅读页的
``book_id`` / ``position`` / ``selection`` / ``reading_chapter`` 一起发过来。
后端如果在 ``mode='writing'`` 时照单全收，user message 里就会带上
``<reading-context>`` / ``<current-passage>``，模型随即把自己当成阅读伴侣
——实测 23:01 写作页发「测试」，回复成了「你正在读《卡拉马佐夫兄弟》开篇」。

反向也要守住：阅读模式的 ``<reading-context>`` / ``<current-passage>``
不能因为这次收紧而消失。

会话同理：带着阅读会话的 ``session_id`` 发写作请求，后端必须另起一条
会话——否则写作回合会写进阅读会话，历史也会被反向带进写作上下文。

跑法：``python scripts/test_chat_mode_context.py``（退出码 0 = 全绿）。
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

from aiohttp.test_utils import TestClient, TestServer                 # noqa: E402

import api_server                                                     # noqa: E402
from config import Config                                             # noqa: E402
from content import ContentBlock, ConversationMessage                  # noqa: E402
from services.session_service import SessionService                    # noqa: E402
from storage.db_session import SessionDB                               # noqa: E402
from storage.writing_db import WritingDB                               # noqa: E402
from tools import ToolRegistry                                         # noqa: E402

FAILED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"ok   {label}")
    else:
        print(f"FAIL {label}\n     {detail}")
        FAILED += 1


class NoBookDB:
    """写作模式不该碰书库。碰了就是上下文又漏了。"""

    def __getattr__(self, name):
        raise AssertionError(f"writing mode touched books db: {name}")


class QuietBookDB:
    """只够 ``resolve_graph_db_path`` 用；写作工具本身不查书库。"""

    def get_import_log_by_book(self, book_id):
        return None


class FakeBookDB:
    """阅读模式的最小书库，只够装配 reading-context / current-passage。"""

    def list_books(self):
        return [SimpleNamespace(id="bid-test", title="测试书", author="某人")]

    def get_book(self, book_id):
        return SimpleNamespace(title="测试书", author="某人")

    def get_chapter(self, book_id, index):
        return SimpleNamespace(title="第一章", chapter_type="body")

    def get_chunk_count(self, book_id, index):
        return 3

    def get_chunk(self, book_id, chapter, chunk):
        return SimpleNamespace(
            content=f"正文片段第{chunk + 1}块。", id=f"ck-{chunk + 1}")

    def get_import_log_by_book(self, book_id):
        return None

    def get_annotations_by_stable_book(self, book_id, limit=200):
        return []


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def stream(self, system_prompt, messages, tools, _x):
        self.calls.append({
            "system_prompt": system_prompt,
            "messages": messages,
            "tools": tools,
        })
        yield ("收到。", [], None)


async def _post(app, payload: dict) -> tuple[dict, str]:
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat", json=payload)
        body = await resp.text()
        assert resp.status == 200, body[:500]
        return dict(resp.headers), body


def _last_user(call: dict) -> str:
    users = [m for m in call["messages"] if m.get("role") == "user"]
    assert len(users) == 1, call["messages"]
    return users[0]["content"]


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bc_mode_ctx_"))
    (tmp / "data").mkdir(parents=True, exist_ok=True)

    class TestConfig(Config):
        @property
        def writing_db_path(self) -> Path:
            return tmp / "data" / "writing.db"

    cfg = TestConfig(project_dir=ROOT)

    # ── 写作模式：阅读现场一概不注入 ────────────────────────────────
    # 先建一本作品：空项目会话的 system prompt 不能出现它的书名（目录只
    # 报数量，清单要模型自己调 writing_status）。
    from storage.writing_db import WritingDB
    wdb = WritingDB(cfg.writing_db_path)
    wdb.initialize()
    wdb.create_work("某城来信")
    fake = FakeClient()
    app = api_server.create_app(NoBookDB(), config=cfg)
    app["api_client"] = fake
    await _post(app, {
        "message": "测试",
        "mode": "writing",
        # 旧前端可能带着这些；后端必须在 writing 模式里全部丢弃。
        "book_id": "bid-should-be-ignored",
        "reading_chapter": 0,
        "max_chapter": 0,
        "position": {"chapter": 0, "chunk": 0},
        "selection": "某些书中原文",
        "selection_source": "book",
        "writing_work_id": "",
        "writing_chapter_index": 0,
        "stream": "v1",
    })
    check("写作模式：只调用了一次 LLM", len(fake.calls) == 1, str(fake.calls))
    call = fake.calls[0]
    content = _last_user(call)
    check("写作模式：user message 只有用户原话 + 输入时间",
          content.startswith("测试\n\n（输入时间："), repr(content))
    for tag in ("<reading-context>", "<current-passage",
                "<persona-observation>", "<relevant-memories>",
                "<user-selection", "<home-context>"):
        check(f"写作模式：不注入 {tag}", tag not in content, content[:300])
    check("写作模式：system prompt 是写作人格",
          "执笔者" in call["system_prompt"], call["system_prompt"][:200])
    check("写作模式：system prompt 带写作现场",
          "当前写作现场" in call["system_prompt"], call["system_prompt"][:200])
    check("空项目：不注入已有作品书名",
          "某城来信" not in call["system_prompt"],
          call["system_prompt"][-400:])
    check("空项目：给 writing_status 指路",
          "writing_status" in call["system_prompt"], call["system_prompt"][-400:])

    # ── 写作页的引用：正文圈选要带，阅读页残留的书中选区别带 ──────────
    # 上面那条「selection_source=book」验的就是残留要丢；这里验另一种方向：
    # 作者在正文编辑器里圈出一段（source=writing），或引用上一轮回复
    # （source=chat），这两种是写作现场的一部分，必须带进 user message。
    quote_fake = FakeClient()
    app_quote = api_server.create_app(NoBookDB(), config=cfg)
    app_quote["api_client"] = quote_fake
    await _post(app_quote, {
        "message": "这段改冷一点",
        "mode": "writing",
        "selection": "他把烟按熄在铁皮上。",
        "selection_source": "writing",
        "writing_work_id": "",
        "writing_chapter_index": 0,
        "stream": "v1",
    })
    quoted = _last_user(quote_fake.calls[0])
    check("写作引用：注入 user-selection", "<user-selection" in quoted,
          quoted[:300])
    check("写作引用：source 标成写作正文",
          'source="写作正文"' in quoted, quoted[:300])
    check("写作引用：原文逐字带进来", "他把烟按熄在铁皮上。" in quoted,
          quoted[:300])
    check("写作引用：不是阅读上下文", "<reading-context>" not in quoted,
          quoted[:300])

    chat_fake = FakeClient()
    app_chatq = api_server.create_app(NoBookDB(), config=cfg)
    app_chatq["api_client"] = chat_fake
    await _post(app_chatq, {
        "message": "这句什么意思",
        "mode": "writing",
        "selection": "先不写正文，等你确认",
        "selection_source": "chat",
        "writing_work_id": "",
        "writing_chapter_index": 0,
        "stream": "v1",
    })
    chat_quoted = _last_user(chat_fake.calls[0])
    check("写作引用：引用回复也保留",
          'source="对话引用"' in chat_quoted, chat_quoted[:300])

    # ── 写作：机械验收把「说了没做」逼回去 ──────────────────────────
    # 2026-10-29 的会话里，模型说完「建出来并写成正文」就停了，正文 0 字。
    # 终止判据不能只有「这一轮没调工具」。
    wdb = WritingDB(cfg.writing_db_path)
    work_id = wdb.create_work("验收测试", genre="悬疑")
    wdb.add_chapter(work_id, 1, title="第一章")

    class PromiseClient:
        """第一轮只说不做，第二轮才老实汇报。"""

        def __init__(self) -> None:
            self.calls: list[list[dict]] = []

        async def stream(self, system_prompt, messages, tools, _x):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                yield ("我把第一章建出来并写成正文。", [], None)
            else:
                yield ("还没写，先确认设定。", [], None)

    promise = PromiseClient()
    app4 = api_server.create_app(NoBookDB(), config=cfg)
    app4["api_client"] = promise
    await _post(app4, {
        "message": "写第一章",
        "mode": "writing",
        "writing_work_id": work_id,
        "writing_chapter_index": 1,
        "stream": "v1",
    })
    check("机械验收：承诺未兑现时补一轮",
          len(promise.calls) == 2, str(len(promise.calls)))
    if len(promise.calls) == 2:
        note = promise.calls[1][-1]
        check("机械验收：回灌的是验收提示",
              note.get("role") == "user"
              and "机械验收" in (note.get("content") or ""),
              repr(note)[:200])
        check("机械验收：指向没做的那一步",
              "writing_draft_chapter" in (note.get("content") or ""),
              repr(note)[:200])

    # ── 撞轮次上限：要显式说，不要伪装成正常收尾 ────────────────────
    class LoopClient:
        """每轮都调一次工具，永远不自称做完。"""

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, system_prompt, messages, tools, _x):
            self.calls += 1
            yield ("再查一次。",
                   [{"id": f"call-{self.calls}", "name": "writing_status",
                     "arguments": "{}"}], None)

    loop = LoopClient()
    app5 = api_server.create_app(QuietBookDB(), config=cfg)
    app5["api_client"] = loop
    # 没有 runtime 就没有工具注册表，写作工具会全是 Tool not found。
    app5["runtime"] = SimpleNamespace(tool_registry=ToolRegistry())
    _headers, body5 = await _post(app5, {
        "message": "一直查",
        "mode": "writing",
        "stream": "v2",
        "writing_work_id": work_id,
        "writing_chapter_index": 1,
    })
    check("撞上限：跑满 40 轮", loop.calls == 40, str(loop.calls))
    check("撞上限：发 round_limit 事件",
          '"type": "round_limit"' in body5, body5[-300:])

    # ── 现场过期：建了章之后，下一轮的 system prompt 必须重拍 ──────
    fresh_work = wdb.create_work("刷新测试")

    class RefreshClient:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def stream(self, system_prompt, messages, tools, _x):
            self.prompts.append(system_prompt)
            if len(self.prompts) == 1:
                yield ("建一章。",
                       [{"id": "c1", "name": "writing_create_chapter",
                         "arguments": json.dumps(
                             {"title": "新章", "purpose": "推进"})}], None)
            else:
                yield ("建好了。", [], None)

    refresh = RefreshClient()
    app6 = api_server.create_app(QuietBookDB(), config=cfg)
    app6["api_client"] = refresh
    app6["runtime"] = SimpleNamespace(tool_registry=ToolRegistry())
    await _post(app6, {
        "message": "建一章",
        "mode": "writing",
        "stream": "v2",
        "writing_work_id": fresh_work,
    })
    check("现场刷新：跑了两轮", len(refresh.prompts) == 2,
          str(len(refresh.prompts)))
    if len(refresh.prompts) == 2:
        check("现场刷新：首轮还没有章节",
              "还没有章节" in refresh.prompts[0], refresh.prompts[0][-300:])
        check("现场刷新：次轮看到新章",
              "新章" in refresh.prompts[1], refresh.prompts[1][-300:])

    # ── 会话不跨模式：带着阅读会话 id 发写作请求，必须新建会话 ──────
    sdb = SessionDB(tmp / "data" / "sessions.db")
    sdb.initialize()
    ssvc = SessionService(sdb, cfg)
    reading_id = "sess-reading-test"
    ssvc.append_message(
        reading_id,
        ConversationMessage(
            role="user",
            blocks=[ContentBlock(type="text", text="读到哪里了")],
        ),
        mode="immersive", title="阅读会话")

    fake3 = FakeClient()
    app3 = api_server.create_app(NoBookDB(), config=cfg,
                                 session_service=ssvc)
    app3["api_client"] = fake3
    headers, _ = await _post(app3, {
        "message": "测试",
        "mode": "writing",
        "session_id": reading_id,
        "book_id": "bid-should-be-ignored",
        # 旧前端还会把客户端历史快照一起发来 —— 同样属于阅读线，必须丢掉。
        "history": [{"role": "user", "content": "读到哪里了"}],
        "stream": "v1",
    })
    new_sid = headers.get("X-Session-Id") or ""
    check("跨模式不复用：服务端另起会话",
          bool(new_sid) and new_sid != reading_id, repr(new_sid))
    check("跨模式不复用：原会话没有新消息",
          ssvc.get(reading_id).message_count == 1,
          str(ssvc.get(reading_id).message_count))
    call3 = fake3.calls[0]
    leaked = [m.get("content") for m in call3["messages"]
              if "读到哪里了" in (m.get("content") or "")]
    check("跨模式不复用：不继承另一条线的历史", not leaked, repr(leaked))
    check("跨模式不复用：新会话 mode=writing",
          new_sid and ssvc.get(new_sid).mode == "writing",
          str(ssvc.get(new_sid).mode if new_sid else "(no session)"))

    # ── 阅读模式：原有注入不能丢 ────────────────────────────────────
    fake2 = FakeClient()
    app2 = api_server.create_app(FakeBookDB(), config=cfg)
    app2["api_client"] = fake2
    await _post(app2, {
        "message": "这段话什么意思",
        "mode": "immersive",
        "book_id": "bid-test",
        "position": {"chapter": 0, "chunk": 0},
        "reading_chapter": 0,
        "stream": "v1",
    })
    content2 = _last_user(fake2.calls[0])
    check("阅读模式：仍有 <reading-context>",
          "<reading-context>" in content2, content2[:300])
    check("阅读模式：仍有 <current-passage>",
          "<current-passage " in content2, content2[:300])

    # ── 视窗文字优先：跨块时逐段标注块边界 ──────────────────────────
    # 视窗横跨第 2、3 块，单块正文盖不住；注入必须以 viewport 为准并标出
    # 每一段属于哪一块（而不是退回 position 指定的那一块）。
    fake_vp = FakeClient()
    app_vp = api_server.create_app(FakeBookDB(), config=cfg)
    app_vp["api_client"] = fake_vp
    await _post(app_vp, {
        "message": "这一段在讲什么",
        "mode": "immersive",
        "book_id": "bid-test",
        "position": {"chapter": 0, "chunk": 1},
        "reading_chapter": 0,
        "reading_viewport": {
            "text": "正文片段第2块。 正文片段第3块。",
            "sections": [{"sectionIndex": 0, "visiblePx": 700,
                          "text": "正文片段第2块。 正文片段第3块。"}],
        },
        "stream": "v1",
    })
    vp_content = _last_user(fake_vp.calls[0])
    check("视窗注入：来源是 viewport",
          'source="viewport"' in vp_content, vp_content[:800])
    check("视窗注入：标注跨 2 块",
          "共跨 2 块" in vp_content, vp_content[:800])
    check("视窗注入：逐块标出块号",
          "第 2/3 块" in vp_content and "第 3/3 块" in vp_content,
          vp_content[:800])
    check("视窗注入：不再只给 position 那一块",
          "正文片段第1块" not in vp_content, vp_content[:800])

    # ── 主页 / 阅读页：页面身份决定提示词与会话线 ──────────────────
    # 主页请求即便带着阅读页残留的 book_id / 选区 / 进度，也必须按主页
    # 处理：注入 home 语境，不能注入书的上下文。
    fake_home = FakeClient()
    app_home = api_server.create_app(FakeBookDB(), config=cfg)
    app_home["api_client"] = fake_home
    await _post(app_home, {
        "message": "今天读什么好",
        "mode": "immersive",
        "page": "home",
        "book_id": "bid-test",
        "position": {"chapter": 2, "chunk": 0},
        "reading_chapter": 2,
        "selection": "书中原文",
        "selection_source": "book",
        "stream": "v1",
    })
    home_content = _last_user(fake_home.calls[0])
    check("主页：有 <home-context>",
          "<home-context>" in home_content, home_content[:400])
    check("主页：不带 <reading-context>",
          "<reading-context>" not in home_content, home_content[:400])
    check("主页：不带 <current-passage",
          "<current-passage" not in home_content, home_content[:400])
    check("主页：不带选区", "书中原文" not in home_content, home_content[:400])

    # 会话线隔离：阅读会话不能被主页请求续写；同一页的请求必须续写。
    rdb = SessionDB(tmp / "data" / "sessions.db")
    rdb.initialize()
    rsvc = SessionService(rdb, cfg)
    reader_id = "sess-reader-test"
    rsvc.append_message(
        reader_id,
        ConversationMessage(
            role="user",
            blocks=[ContentBlock(type="text", text="读到哪里了")]),
        mode="immersive", title="阅读会话", project_id="bid-test")

    fake_x = FakeClient()
    app_x = api_server.create_app(FakeBookDB(), config=cfg,
                                  session_service=rsvc)
    app_x["api_client"] = fake_x
    # 主页请求带着阅读会话 id → 另起会话，且不吃阅读线历史。
    headers_x, _ = await _post(app_x, {
        "message": "主页问一句", "mode": "immersive", "page": "home",
        "session_id": reader_id,
        "history": [{"role": "user", "content": "读到哪里了"}],
        "stream": "v1",
    })
    home_sid = headers_x.get("X-Session-Id") or ""
    check("主页不复用阅读会话",
          bool(home_sid) and home_sid != reader_id, repr(home_sid))
    check("主页会话 project_id 为空",
          bool(home_sid) and not rsvc.get(home_sid).project_id,
          repr(rsvc.get(home_sid).project_id if home_sid else None))
    check("主页不继承阅读会话历史",
          not [m for m in fake_x.calls[0]["messages"]
               if "读到哪里了" in (m.get("content") or "")],
          repr(fake_x.calls[0]["messages"]))

    # 阅读页请求续写同一会话。
    before = rsvc.get(reader_id).message_count
    headers_r, _ = await _post(app_x, {
        "message": "继续", "mode": "immersive", "page": "reader",
        "book_id": "bid-test", "reading_chapter": 0,
        "session_id": reader_id, "stream": "v1",
    })
    after = rsvc.get(reader_id).message_count
    check("阅读页续写自己的会话",
          headers_r.get("X-Session-Id") == reader_id and after > before,
          f"{reader_id} -> {headers_r.get('X-Session-Id')!r}, {before} -> {after}")

    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
