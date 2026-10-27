"""Chat endpoints: the streaming conversation turn (SSE) + cancellation.

The turn pipeline lives in ``_handle_chat_impl``; everything it needs is
imported from the sibling route modules so this file has no back-edge to
api_server.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from aiohttp import web

import chat_context
from api.prompt_builders import (
    _build_author_card, _build_book_note, _build_reading_prompt,
)
from api.routes.graph import _bg_graph_extract, _resolve_graph_path
from api.routes.writing import _writing_service
from api.sse import _ThinkParser, _sse_write_json
from api.text_helpers import _as_int, _resolve_selection_position, _type_label

_APP_ROOT = Path(__file__).resolve().parents[2]


def _session_surface(mode: str, project_id: str) -> str:
    """主页 / 阅读页 / 写作页 —— 会话属于哪条线。

    与前端 ``sessions.js sessionSurface()`` 同一套判据：写作按 mode，扮演
    按首页（哪怕带着一本书），其余按有没有书：有书是阅读页，没书是主页。
    """
    if mode == "writing":
        return "writing"
    if mode == "roleplay":
        return "home"
    return "reader" if project_id else "home"


def _request_surface(mode: str, book_id: str) -> str:
    """这次请求发生在哪条会话线上（用实际会落库的 mode/book_id 判断）。"""
    if mode == "writing":
        return "writing"
    if mode == "roleplay":
        return "home"
    return "reader" if book_id else "home"


# ── User-initiated interrupt ───────────────────────────────────────────
# 中断注记既落盘也回灌上下文：下一次请求由 _history_from_session 把它拼在
# 被截断的 assistant 消息前面，模型才知道自己上一轮没说完。
_INTERRUPT_NOTICE_PATH = _APP_ROOT / "prompts" / "interruption.md"
_INTERRUPT_NOTICE_FALLBACK = (
    "【系统注记】上一轮回复在生成过程中被用户主动中断，因此上面的回复"
    "不完整、可能停在半句。这是用户的明确意图，不要假设你已经说完了，"
    "等用户下一句指示再继续。"
)
_INTERRUPT_NOTICE = (
    _INTERRUPT_NOTICE_PATH.read_text(encoding="utf-8").strip()
    if _INTERRUPT_NOTICE_PATH.exists()
    else _INTERRUPT_NOTICE_FALLBACK
) or _INTERRUPT_NOTICE_FALLBACK


class _TurnCancelRegistry:
    """turn_id → asyncio.Event, so one tab's stop never kills another turn.

    取消请求可能比 /api/chat 先到（按钮在响应头返回前就能按）：没有活跃
    回合时先把 turn_id 记在 ``_early`` 里，register() 发现它就直接置位。
    两边都做 TTL 清理 —— 异常路径漏掉 release() 也不会永久占内存。
    """

    _ACTIVE_TTL = 600.0   # 10 分钟：一轮对话不可能跑这么久
    _EARLY_TTL = 60.0     # 先到的取消最多等 1 分钟

    def __init__(self) -> None:
        self._active: dict[str, tuple[asyncio.Event, float]] = {}
        self._early: dict[str, float] = {}

    def _prune(self, now: float) -> None:
        for key, (_event, ts) in list(self._active.items()):
            if now - ts > self._ACTIVE_TTL:
                self._active.pop(key, None)
        for key, ts in list(self._early.items()):
            if now - ts > self._EARLY_TTL:
                self._early.pop(key, None)

    def register(self, turn_id: str) -> asyncio.Event:
        now = time.monotonic()
        self._prune(now)
        event = asyncio.Event()
        if self._early.pop(turn_id, None) is not None:
            event.set()
        self._active[turn_id] = (event, now)
        return event

    def cancel(self, turn_id: str) -> bool:
        """Set the turn's event. Returns whether the turn was already active."""
        now = time.monotonic()
        self._prune(now)
        current = self._active.get(turn_id)
        if current is not None:
            current[0].set()
            return True
        self._early[turn_id] = now
        return False

    def release(self, turn_id: str) -> None:
        self._active.pop(turn_id, None)


async def handle_chat_cancel(request: web.Request) -> web.Response:
    """Stop an in-flight /api/chat turn.

    The client sends the same ``turn_id`` it put on the chat request; the
    streaming loop checks that turn's event between chunks / tools / rounds.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    turn_id = str(body.get("turn_id") or "").strip() if isinstance(body, dict) else ""
    if not turn_id or len(turn_id) > 128:
        return web.json_response({"error": "invalid turn_id"}, status=400)
    registry = request.app.get("turn_cancels")
    active = bool(registry and registry.cancel(turn_id))
    logging.getLogger("api").info(
        "chat cancel turn=%s active=%s", turn_id[:12], active)
    return web.json_response({"ok": True, "active": active})


async def handle_chat(request: web.Request) -> web.StreamResponse:
    """Handle a chat message with optional reading context + selection.

    POST body: {message, mode, page, book_id, position: {chapter, chunk},
                selection, session_id, ...}

    ``page`` ∈ home / reader / writing：三个界面各有自己的提示词与会话线，
    与 session_id 所属的线不一致时另起会话（见 ``_request_surface``）。

    Returns an SSE stream of the agent's response tokens.
    """
    db = request.app["db"]
    runtime = request.app.get("runtime")
    api_client = request.app.get("api_client")

    if not api_client:
        return web.json_response(
            {"error": "API client not available"}, status=503
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    message = body.get("message", "").strip()
    if not message:
        return web.json_response({"error": "empty message"}, status=400)

    book_id = body.get("book_id", "")
    # Reset book context; will be set to stable_bid below if a book is open
    if runtime:
        runtime.current_book_id = ""
    position = body.get("position", {})
    selection = body.get("selection", "").strip()
    selection_source = body.get("selection_source", "book")  # "book" | "chat"
    history = body.get("history", [])  # recent conversation messages
    mode = body.get("mode", "immersive")  # immersive | overview | divergent | roleplay
    # 页面身份：主页 / 阅读页 / 写作页各有自己的提示词与会话线。
    # 旧客户端不发 page，按 mode/book_id 推断（阅读模式带书 = 阅读页）。
    page = (body.get("page") or "").strip().lower()
    if page not in ("home", "reader", "writing"):
        page = "reader" if book_id else "home"
    if mode == "writing":
        page = "writing"
    elif mode == "roleplay":
        page = "home"  # 扮演是首页功能，即便带着角色所属的书
    character_name = body.get("character_name", "")  # role-play: character to embody
    # 扮演的是**谁**：图谱里的实体（默认，也是这一列出现前的唯一行为）
    # 还是作者层台账里的人。**这一项不判就会静默出错** —— 《我与地坛》里
    # 「史铁生」是叙述者节点「我」的别名，按角色解析会命中那个节点，
    # 于是「扮演作者」变成扮演书里的「我」，一句错话都不报。
    role_kind = (body.get("role_kind") or "character").strip() or "character"
    role_ref = (body.get("role_ref") or "").strip()  # author_id（稳定主键）
    if role_kind not in ("character", "author"):
        # 不认识的种类**大声拒绝**，不退回 character —— 退回就等于
        # 把上面那个静默错误重新装回去（与 stream 版本同一处理）。
        return web.json_response(
            {"error": f"unsupported role_kind: {role_kind!r}"}, status=400
        )
    max_chapter = body.get("max_chapter")  # role-play: None=global, int=spoiler-free
    # 阅读进度（章号）。**与 max_chapter 分开**：max_chapter 是角色扮演的
    # 剧透门，会一并过滤图谱观察；reading_chapter 只喂 reader-notes 通道，
    # 不给阅读模式引入别的行为变化。缺了它笔记一律按「远」处理（不给正文）。
    reading_chapter = body.get("reading_chapter")

    # 写作页只关心作品/章；主页只带 home 语境。两者都跟「当前打开的书」
    # 无关，旧前端（或切页前留下的状态）可能还带着阅读页的 book_id / 选区 /
    # 进度：在这里一次性清掉，后面的上下文注入、记忆召回、会话 project_id
    # 都不会被别的页面污染。扮演是例外 —— 角色扮演本来就要书与剧透门。
    if mode == "writing" or (page == "home" and mode != "roleplay"):
        book_id = ""
        position = {}
        selection = ""
        selection_source = "book"
        max_chapter = None
        reading_chapter = None

    # 选区是「已见」的第一手证据：用户能选中就说明看见了。它同时用于
    # (a) 锚定注入的上下文，(b) 抬高本轮的剧透门。旧实现只有阅读进度，
    # 拟合错一章时门会把用户已经看到的内容挡在外面（实测 22:43：屏幕在
    # 二十四章，门卡在二十三章，模型侦察两轮才回答）。
    selection_chapter, selection_chunk = (None, None)
    if selection:
        selection_chapter, selection_chunk = _resolve_selection_position(
            db, book_id, body, position)

    # 剧透上限 —— **全流程唯一一处计算**。图谱工具、角色内核、图谱观察召回
    # 都读同一个值，不各自推断（各自推断就会不一致，今天已经栽过一次：
    # get_node_detail 按度数排、search_all 没排序，同一个问题两个答案）。
    #   roleplay + 沉浸 -> 前端显式给的门（reader 当前章）
    #   roleplay + 全局 -> None（用户显式选了不设防）
    #   其余模式        -> max(阅读进度, 视口可见章, 选区章)
    # 最后一条是补一个**洞**：max_chapter 只在扮演模式有值，所以阅读模式下
    # 图谱工具的门是 None —— 也就是**根本没门**，而工具描述一直写着
    # 「默认只含读者当前进度及之前的内容（防剧透）」。
    if mode == "roleplay":
        spoiler_gate = max_chapter
    else:
        _gates: list[int] = []
        _rc = _as_int(reading_chapter)
        if _rc is not None:
            _gates.append(_rc)
        # 视口内出现过的章：用户已经看见，门不该更低
        for _v in (body.get("visible_chapters") or []):
            _iv = _as_int(_v)
            if _iv is not None:
                _gates.append(_iv)
        if selection_chapter is not None:
            _gates.append(selection_chapter)
        spoiler_gate = max(_gates) if _gates else None
    logging.getLogger("api").info(
        "chat gate=%s read_ch=%s sel_ch=%s visible=%s mode=%s",
        spoiler_gate, reading_chapter, selection_chapter,
        body.get("visible_chapters"), mode)
    session_id = body.get("session_id", "")  # optional: persist to this session
    # 前端生成的回合标识：/api/chat/cancel 用同一个 id 找到这一轮的事件。
    # 缺了它（旧客户端）就退化成「断开连接」式取消，按钮仍能停 UI。
    turn_id = str(body.get("turn_id") or "").strip()
    if len(turn_id) > 128:
        turn_id = ""
    stream_version = body.get("stream", "v1")  # "v1" | "v2" — v2 = JSON-structured events
    if stream_version not in ("v1", "v2"):
        # An unrecognised value used to fall through to the v1 comparisons
        # (``== "v2"`` is False) and silently emit plain-text frames, which
        # strips reasoning and breaks the think row. Reject loudly instead.
        return web.json_response(
            {"error": f"unsupported stream version: {stream_version!r}"}, status=400
        )
    voice = body.get("voice")  # optional voice metadata for voice_input part

    session_service = request.app.get("session_service")

    # 会话不跨页复用：主页 / 阅读页 / 写作页各有一条会话线。
    # 前端已经分开管理，但旧标签页、旧缓存或直接调 API 仍可能带着另一条
    # 线的 session_id；直接续写会让三边的历史、提示词互相污染。线不一致
    # 就当作新会话（会话本身按需创建，不会留下空壳）。
    if session_service and session_id:
        try:
            _sinfo = session_service.get(session_id)
        except Exception:
            _sinfo = None
        if _sinfo is not None:
            _req_surface = _request_surface(mode, book_id)
            _sess_surface = _session_surface(
                _sinfo.mode, _sinfo.project_id or "")
            if _sess_surface != _req_surface:
                logging.getLogger("api").info(
                    "session %s surface=%s (mode=%r, book=%r) != request "
                    "surface=%s (page=%r) — starting new session",
                    session_id, _sess_surface, _sinfo.mode,
                    _sinfo.project_id, _req_surface, page)
                session_id = ""
                # 客户端快照也属于另一条会话线，不能当历史喂进去。
                history = []

    # 写作的新会话没有历史可继承；DB 里有会话时历史由 session_id 回读。
    if mode == "writing" and not session_id:
        history = []

    # ── Wrap main logic to surface tracebacks to browser ─────────
    import traceback as _traceback
    import logging as _logging
    _cancels = request.app.get("turn_cancels")
    cancel_event = _cancels.register(turn_id) if (_cancels and turn_id) else None
    try:
        return await _handle_chat_impl(
            request, db, runtime, api_client, book_id, position,
            selection, selection_source, history, mode,
            character_name, max_chapter, session_id,
            session_service, message, stream_version, voice,
            role_kind=role_kind, role_ref=role_ref,
            spoiler_gate=spoiler_gate, reading_chapter=reading_chapter,
            selection_chapter=selection_chapter,
            selection_chunk=selection_chunk,
            writing_work_id=(body.get("writing_work_id") or "").strip(),
            writing_chapter_index=_as_int(
                body.get("writing_chapter_index")) or 0,
            cancel_event=cancel_event,
        )
    except Exception:
        _logging.getLogger("api").error(
            "handle_chat crashed:\n%s", _traceback.format_exc()
        )
        return web.json_response(
            {"error": f"Internal error: {_traceback.format_exc()}"},
            status=500,
        )
    finally:
        if _cancels and turn_id:
            _cancels.release(turn_id)


def _history_from_session(session_service, session_id: str,
                          limit: int = 6) -> list[dict]:
    """Fallback: rebuild a plain-text history from the persisted session.

    The primary path is ``chat_context.load_session_history`` (structured,
    with tool pairs and a token budget). This plain-text version only runs
    when that rebuild throws — e.g. a legacy/malformed part row. It still
    takes the last N text-bearing messages and slices at 1500 chars, because
    a plain-text fallback cannot safely reconstruct call/output pairs.

    The browser also posts a ``history`` snapshot, but that snapshot is a
    view of whichever branch the UI happens to be rendering. With a session
    id the DB is the single source of truth; the client snapshot stays as
    the fallback for sessions that do not exist yet.
    """
    try:
        # Read-only context rebuild, not a session "open" — don't dispatch
        # on_session_load for every chat turn.
        sess = session_service.load_messages(session_id, dispatch_hooks=False)
    except Exception:
        return []
    if not sess:
        return []
    out: list[dict] = []
    for msg in sess.messages:
        if msg.role not in ("user", "assistant"):
            continue
        # Concatenate every text block (matches what loadSession renders —
        # an assistant message can carry text before *and* after a tool call;
        # first_text() would silently drop the tail).
        text = "".join(
            b.text for b in msg.blocks if b.type == "text" and b.text
        ).strip()
        # 中断注记拼回上下文，且放在最前面：这条历史稍后会被 [:1500]
        # 截断，注记不能因为被截断的正文太长而消失。
        _notes = [b.text for b in msg.blocks
                  if b.type == "interrupt" and b.text]
        if _notes:
            text = "\n\n".join(_notes + ([text] if text else [])).strip()
        if not text:
            continue
        out.append({
            "role": "user" if msg.role == "user" else "agent",
            "content": text[:1500],
        })
    return out[-limit:]


def _strip_think(text: str) -> str:
    """回灌给模型的 assistant 正文去掉 ``<think>``。

    ``<think>`` 是 SSE 解析器为前端展示生成的，不是模型协议的一部分。
    原样回灌等于让模型重读自己的推理草稿——飞行记录仪里约占输入的 8%。
    """
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cut = text.find("<think>")
    return (text[:cut] if cut >= 0 else text).strip()


def _blocks_from_text(text: str) -> list:
    """正文 → text/thinking 块，与流式展示的 ``<think>`` 解析同一规则。"""
    from content import ContentBlock as _CB
    blocks: list = []
    if not text:
        return blocks
    _last = 0
    _pat = re.compile(r"<think>(.*?)</think>", re.DOTALL)
    for m in _pat.finditer(text):
        if m.start() > _last:
            blocks.append(_CB(type="text", text=text[_last:m.start()]))
        thinking = m.group(1).strip()
        if thinking:
            blocks.append(_CB(type="thinking", thinking=thinking))
        _last = m.end()
    tail = text[_last:]
    if tail:
        open_at = tail.find("<think>")
        if open_at >= 0:
            # 中断可能切在推理中间：未闭合的 <think> 之后都算推理。
            if open_at > 0:
                blocks.append(_CB(type="text", text=tail[:open_at]))
            thinking = tail[open_at + 7:].strip()
            if thinking:
                blocks.append(_CB(type="thinking", thinking=thinking))
        else:
            blocks.append(_CB(type="text", text=tail))
    return blocks


def _persist_round_assistant(session_service, session_id: str,
                             text: str, tool_calls: list[dict]) -> None:
    """落一条带 tool_use 的 assistant 消息 —— ReAct 的一轮。"""
    if not (session_service and session_id):
        return
    from content import ConversationMessage as _CM, ContentBlock as _CB
    blocks = _blocks_from_text(text or "")
    for tc in tool_calls or []:
        blocks.append(_CB.tool_use_block(
            tc.get("id") or "", tc.get("name") or "",
            tc.get("arguments") or "{}"))
    if not blocks:
        return
    session_service.append_message(session_id, _CM(role="assistant", blocks=blocks))


def _persist_tool_message(session_service, session_id: str, tc: dict,
                          result: str, is_error: bool = False) -> None:
    """落一条 tool 结果；与 assistant 的 tool_use 成对存在。"""
    if not (session_service and session_id):
        return
    from content import ConversationMessage as _CM, ContentBlock as _CB
    session_service.append_message(session_id, _CM(
        role="tool",
        blocks=[_CB.tool_result_block(
            tc.get("id") or "", tc.get("name") or "",
            result or "", is_error=is_error)],
    ))


async def _handle_chat_impl(
    request, db, runtime, api_client, book_id, position,
    selection, selection_source, history, mode,
    character_name, max_chapter, session_id,
    session_service, message, stream_version="v1", voice=None,
    role_kind="character", role_ref="",
    # 剧透门由 handle_chat **算一次**（全流程唯一一处），这里只负责用。
    # 这俩曾经只在 handle_chat 的局部作用域里存在，impl 里直接引用 ——
    # NameError 被下面 recall 的 `except: pass` 吞掉一次，最后在 role_card
    # 处炸成 500：前端表现为「空气泡 + 没有 SSE」，日志里只有一行 traceback。
    spoiler_gate=None, reading_chapter=None,
    # 选区坐标（由 handle_chat 解析）：用于把注入的上下文锚到用户
    # 真正选中的那一块，而不是阅读进度拟合值。
    selection_chapter=None, selection_chunk=None,
    # 写作对话现场：前端写作页当前打开的作品/章。工具靠它解析默认目标。
    writing_work_id: str = "", writing_chapter_index: int = 0,
    # 用户主动打断：这个事件由 /api/chat/cancel 置位，流/工具/轮次之间检查。
    cancel_event: asyncio.Event | None = None,
) -> web.StreamResponse:
    import logging

    # ── Conversation history: the session wins over the client snapshot ──
    # Read BEFORE appending this turn's user message, otherwise the message
    # would appear twice (once in history, once as the current user input).
    #
    # 结构化重建（含 tool_use/tool_result）优先；失败才退回旧的纯文本切片。
    # 旧切片在工具轮之间会丢掉全部证据，下一轮模型只能重新摸路。
    raw_db_history: list[dict] = []
    if session_service and session_id:
        try:
            raw_db_history = chat_context.load_session_history(
                session_service, session_id)
        except Exception:
            logging.getLogger("api").warning(
                "structured history load failed; falling back to text",
                exc_info=True)
            raw_db_history = chat_context.client_history_to_api(
                _history_from_session(session_service, session_id))
        if raw_db_history:
            # 纯文本视图只留给图谱提取；模型走下面的结构化预算窗口。
            history = chat_context.to_legacy_history(raw_db_history)

    # ── Session persistence: create/append user message (EARLY) ─────────
    # Persist user message BEFORE context building so it survives any
    # downstream failure (500 error, LLM timeout, etc). This ensures the
    # user's input is always recorded, even when the agent never responds.
    persisted_session_id = ""
    # Returned to the browser so the live bubble can be forked from before a
    # page reload (fork-by-message-id). Without it, a just-sent message has no
    # DB id client-side and "重新回复" simply fails.
    user_message_id = ""
    title = message[:50].replace("\n", " ").strip()
    if session_service and session_id:
        try:
            from content import ConversationMessage as _CM, ContentBlock as _CB
            blocks = []
            if voice and isinstance(voice, dict):
                blocks.append(_CB(type="voice_input", text=json.dumps(voice, ensure_ascii=False)))
            blocks.append(_CB(type="text", text=message))
            user_msg = _CM(role="user", blocks=blocks)
            user_message_id = session_service.append_message(session_id, user_msg)
            info = session_service.get(session_id)
            if info and not info.title:
                session_service.update_title(session_id, title)
            if book_id and (not info or not info.project_id):
                session_service._db.update_session(
                    session_id, project_id=book_id,
                )
            if mode == "roleplay" and character_name:
                session_service._db.update_session(
                    session_id, character_name=character_name,
                    role_kind=role_kind, role_ref=role_ref,
                )
            persisted_session_id = session_id
        except Exception as exc:
            logging.getLogger("api").warning(f"Session user-msg persist failed: {exc}")
    elif session_service and not session_id:
        try:
            from session import Session as _SessionModel
            new_session = _SessionModel()
            new_session.mode = mode
            from content import ConversationMessage as _CM, ContentBlock as _CB
            blocks = []
            if voice and isinstance(voice, dict):
                blocks.append(_CB(type="voice_input", text=json.dumps(voice, ensure_ascii=False)))
            blocks.append(_CB(type="text", text=message))
            user_msg = _CM(role="user", blocks=blocks)
            user_message_id = session_service.append_message(
                new_session.session_id, user_msg,
                mode=mode, title=title,
                project_id=book_id or "",
                character_name=character_name if mode == "roleplay" else None,
                role_kind=role_kind, role_ref=role_ref,
            )
            persisted_session_id = new_session.session_id
        except Exception as exc:
            logging.getLogger("api").warning(f"Session create failed: {exc}")

    # ── Build context block ──────────────────────────────────────
    context_parts: list[str] = []

    # 写作模式不是阅读场景：书库、阅读位置、选区、笔记、画像、记忆召回
    # 全部不注入。写作需要的现场（作品/章/合同）由
    # ``WritingService.agent_prompt()`` 写进 system prompt——那里才是
    # 唯一真相源。混进阅读上下文，模型会直接把自己当成阅读伴侣：
    # 实测 23:01 写作页发「测试」，user message 里带着《卡拉马佐夫兄弟》的
    # reading-context / current-passage，回复成了「你正在读……开篇」。
    is_writing = mode == "writing"

    # Home page context — injected when no book is open, unloaded when a book opens
    if not book_id and not is_writing:
        try:
            all_books = db.list_books()
            if all_books:
                book_lines = "\n".join(
                    f"  - 《{b.title}》({'作者: ' + b.author if b.author else '未知作者'})"
                    for b in all_books[:20]
                )
                home_ctx = (
                    "<home-context>\n"
                    "  当前位置: 主页\n"
                    "  用户既可能想在此页闲聊，也可能需要导航帮助。\n"
                    "  在用户未表达阅读意愿前，默认用户只是想在此页聊天。\n"
                    f"  已导入书籍 ({len(all_books)} 本):\n{book_lines}\n"
                    "</home-context>"
                )
                context_parts.append(home_ctx)
        except Exception:
            pass

    # Reading position
    if book_id and not is_writing:
        book = db.get_book(book_id)
        if book:
            ch_idx = position.get("chapter", 0)
            ch = db.get_chapter(book_id, ch_idx)
            ck_idx = position.get("chunk", 0)
            total = db.get_chunk_count(book_id, ch_idx)

            ctx = (
                f"<reading-context>\n"
                f"  书名: 《{book.title}》\n"
                f"  作者: {book.author or '未知'}\n"
            )
            if ch:
                ctx += f"  章节: {ch.title} ({_type_label(ch.chapter_type)})\n"
            ctx += (
                f"  位置: 第 {ck_idx + 1}/{total} 块 "
                f"(chapter_index={ch_idx}, chunk_index={ck_idx})\n"
            )
            ctx += "</reading-context>"
            context_parts.append(ctx)

    # Selection (book or chat)
    if selection and not is_writing:
        if selection_source == "chat":
            context_parts.append(
                f"<user-selection source=\"书伴的回复\">\n"
                f"  用户引用书伴说过的话: \"{selection[:200]}"
                + ("…" if len(selection) > 200 else "") + "\"\n"
                f"  (用户对书伴的这段回复有疑问，请解释或展开)\n"
                f"</user-selection>"
            )
        else:
            context_parts.append(
                f"<user-selection source=\"书中原文\">\n"
                f"  用户选中: \"{selection[:200]}"
                + ("…" if len(selection) > 200 else "") + "\"\n"
                f"</user-selection>"
            )

    # ── Enriched context: current passage, annotations, memory ──────
    if book_id and not is_writing:
        ch_idx = position.get("chapter", 0)
        ck_idx = position.get("chunk", 0)

        # 2.3.1 Current passage (truncated to 2000 chars). Coordinates and
        # chunk_id are attributes so the agent can address the same block
        # with library_read / current_passage instead of guessing from prose.
        ck = db.get_chunk(book_id, ch_idx, ck_idx)
        if ck:
            passage = ck.content[:2000]
            if len(ck.content) > 2000:
                passage += "\n...（段落较长，已截断，可通过 library_read 查阅完整内容）"
            context_parts.append(
                f"<current-passage chapter_index=\"{ch_idx}\" "
                f"chunk_index=\"{ck_idx}\" chunk_id=\"{ck.id}\">\n"
                f"{passage}\n</current-passage>"
            )

        # 2.3.1b Selection context — the text the reader actually pointed
        # at. It can live in a different chunk than the reading position
        # (22:43: reader on 二十三章, selection "杨修" from 二十四章). The
        # old code injected only the current passage, so the model had to
        # search for text the user had already shown it.
        if (selection and selection_source != "chat"
                and selection_chapter is not None):
            sel_ck_idx = selection_chunk if selection_chunk is not None else 0
            if (selection_chapter, sel_ck_idx) != (ch_idx, ck_idx):
                sck = db.get_chunk(book_id, selection_chapter, sel_ck_idx)
                if sck:
                    sel_passage = sck.content[:1200]
                    if len(sck.content) > 1200:
                        sel_passage += "\n...（已截断）"
                    context_parts.append(
                        f"<selection-context chapter_index=\"{selection_chapter}\" "
                        f"chunk_index=\"{sel_ck_idx}\" chunk_id=\"{sck.id}\">\n"
                        f"  用户选中的文字来自这一块（不是当前阅读位置）。\n"
                        f"{sel_passage}\n</selection-context>"
                    )

        # 2.5.2 L0 article-index + 2.3.2 L1 user-annotations
        # stable_bid 必须用 import_log 的 stable id（BookRow 无此字段）；
        # 直接退回 book_id 会导致 annotations（stable_book_id=sb-*）查空
        stable_bid = book_id
        try:
            _slog = db.get_import_log_by_book(book_id)
            if _slog and _slog.stable_book_id:
                stable_bid = _slog.stable_book_id
        except Exception:
            pass
        # Stamp book context on runtime for graph evolution (compact-time extraction)
        # graph.db uses book_id as project_id (not stable_book_id)
        if runtime:
            runtime.current_book_id = book_id
            _cfg = request.app.get("config")
            _gp = _resolve_graph_path(_cfg, db, book_id) if _cfg and db else None
            if _gp:
                runtime.current_graph_path = str(_gp)
        anns = db.get_annotations_by_stable_book(stable_bid, limit=200)
        if anns:
            notes_n = sum(1 for a in anns if a.type in ("note", "both"))
            bm_n = sum(1 for a in anns if a.type in ("bookmark", "both"))
            latest = anns[-1]
            latest_snip = (latest.note_text or latest.selected_text or "")[:20]
            context_parts.append(
                f"<article-index>\n"
                f"  本书笔记: {notes_n}条 | 书签: {bm_n}个"
                f" | 最近: [{latest.id}] {latest_snip}\n"
                f"</article-index>"
            )
            # L1: current chapter annotations only
            chapter_anns = [a for a in anns if a.chapter_index == ch_idx]
            if chapter_anns:
                notes_ctx = "<user-annotations>\n"
                for a in chapter_anns:
                    tag = "笔记" if a.type in ("note", "both") else "书签"
                    snippet = (a.note_text or a.selected_text or "")[:200]
                    notes_ctx += (
                        f"  [{a.id}] {tag} | {snippet}\n"
                        f"    章节位置: ch.{a.chapter_index} ck.{a.chunk_index}\n"
                    )
                notes_ctx += "</user-annotations>"
                context_parts.append(notes_ctx)

    # 2.3.3 Memory snapshot (global memory + user profile)
    if not is_writing and runtime and hasattr(runtime, "memory_store"):
        mem_block = runtime.memory_store.freeze_for_system_prompt("memory")
        if mem_block:
            context_parts.append(
                f"<global-memory>\n{mem_block}\n</global-memory>"
            )
        persona_block = runtime.memory_store.freeze_for_system_prompt("persona")
        if persona_block:
            context_parts.append(
                f"<persona-observation>\n{persona_block}\n</persona-observation>"
            )
        user_block = runtime.memory_store.freeze_for_system_prompt("user")
        if user_block:
            context_parts.append(
                f"<user-profile>\n{user_block}\n</user-profile>"
            )

    # ── 统一记忆召回编排（腾讯编排表方案） ──────────────────────────
    # 声明式通道表见 recall_orchestrator.py：L1 原子记忆 / 用户收藏 / 用户笔记 /
    # 图谱观察（原内联 <graph-observations> 收编于此）。单通道失败不阻塞其他；
    # 图谱通道要求 current_book_id（非 roleplay；roleplay 用 format_role_kernel）。
    if runtime and message and mode not in ("roleplay", "writing"):
        try:
            from recall_orchestrator import fetch_dynamic_channels
            _cfg3 = request.app.get("config")
            _gp3 = _resolve_graph_path(_cfg3, db, book_id) if (_cfg3 and db and book_id) else None
            _recall_ctx = {
                "runtime": runtime,
                "books_db": db,
                "graph_db_path": str(_gp3) if _gp3 else None,
                "max_chapter": spoiler_gate,
                "reading_chapter": reading_chapter,
                "current_stable_book_id": locals().get("stable_bid"),
                "mode": mode,
            }
            for _pri, _tag, _content in fetch_dynamic_channels(message, ctx=_recall_ctx):
                context_parts.append(_content)
        except Exception:
            # 单通道失败不该拖垮整轮对话，但**绝不能静默** —— 这里原本是
            # `pass`，于是上面 ctx 里的 NameError 被吞了整整一版，
            # 召回通道静默死亡而没人发现。
            logging.getLogger("api").exception("recall channels failed")

    # Token budget: cap context_parts at 6000 chars, drop lowest priority first
    # Priority low → high: user-profile, global-memory, user-selection,
    #   reading-context, relevant-memories, user-notes, user-highlights,
    #   graph-observations, article-index, user-annotations, current-passage
    MAX_CTX_CHARS = 6000
    _drop_order = [
        "<user-profile>", "<global-memory>",
        "<user-selection", "<reading-context>",
        "<relevant-memories>", "<user-notes>", "<user-highlights>",
        "<graph-observations>", "<article-index>",
        "<user-annotations>", "<current-passage>", "<selection-context>",
    ]
    for _tag in _drop_order:
        if sum(len(p) for p in context_parts) <= MAX_CTX_CHARS:
            break
        for _i, _p in enumerate(context_parts):
            if _tag in _p:
                context_parts.pop(_i)
                break

    # ── Role-play mode: strip companion-reading context ──────────────
    # In role-play the agent IS the character — not a reading companion.
    # Reading position, annotations, memory, and home-context are removed.
    # Environment info is accessed via tools, not injected as context.
    if mode == "roleplay":
        context_parts.clear()
        # Reframe user-selection — "the reader showed you", not "the user
        # selected text for explanation"
        if selection:
            if selection_source == "chat":
                context_parts.append(
                    f"<user-selection source=\"对话引用\">\n"
                    f"  读者引用了你之前说过的话: \"{selection[:200]}"
                    + ("…" if len(selection) > 200 else "") + "\"\n"
                    f"</user-selection>"
                )
            else:
                context_parts.append(
                    f"<user-selection source=\"书中原文\">\n"
                    f"  读者向你展示了这段文字: \"{selection[:200]}"
                    + ("…" if len(selection) > 200 else "") + "\"\n"
                    f"</user-selection>"
                )

    # ── Build system prompt ──────────────────────────────────────
    # ── Role-play: build character kernel from graph (pure function, no LLM) ──
    # The kernel (identity + minimal extension + knowledge boundary) is
    # formatted directly from get_node_detail - no LLM pre-translation,
    # no frozen cache. Deep detail is fetched on-demand via
    # graph_context / graph_quotes / graph_trace during the conversation.
    role_card = ""
    config = request.app.get("config")
    if mode == "roleplay" and character_name and book_id:
        if role_kind == "author":
            # 作者**不在图谱里**（元层不进 nodes/edges），所以走另一个库。
            # 分叉的依据是 role_kind，不是「在图谱里找不到就试试作者层」——
            # 那种兜底会把「找不到」和「不是那种东西」混为一谈，且
            # 《我与地坛》的别名解析**找得到**，兜底永远轮不到。
            role_card = _build_author_card(
                config, db, book_id, role_ref, character_name, spoiler_gate)
        elif config:
            from pathlib import Path
            from storage.graph_db import GraphDB, resolve_graph_db_path
            from bookgraph.formatter import format_role_kernel

            graph_path = resolve_graph_db_path(config, db, book_id) if db else config.graph_db_path(book_id)
            if graph_path.exists():
                gdb = GraphDB(graph_path)
                try:
                    detail = gdb.get_node_detail(book_id, character_name,
                                                 max_chapter=spoiler_gate)
                    if not detail.get("error"):
                        # 章号给人话：内部 chapter_index 是 EPUB 脊序，
                        # 窄门正文第一章的坐标是 13（前言/目录占了前面）。
                        chapter_label = None
                        try:
                            for _ch in db.get_chapters(book_id):
                                if _ch.chapter_index == spoiler_gate:
                                    chapter_label = _ch.title
                                    break
                        except Exception:
                            chapter_label = None
                        role_card = format_role_kernel(
                            detail, max_chapter=spoiler_gate,
                            chapter_label=chapter_label)
                finally:
                    gdb.close()
            else:
                _log = logging.getLogger("api")
                _log.warning("role_card: no graph at %s", graph_path)

    # ── Populate per-request context for role-play tools ────────────
    # Populate reading context for ALL modes - graph_search / graph_node /
    # current_passage read from here. Role-play additionally uses
    # character_name (stamped on the session elsewhere).
    if runtime and hasattr(runtime, "tool_registry"):
        from storage.graph_db import resolve_graph_db_path
        r_ctx = runtime.tool_registry.reading_ctx
        r_ctx.clear()
        r_ctx.update({
            "book_id": book_id,
            "chapter_index": position.get("chapter", 0),
            "chunk_index": position.get("chunk", 0),
            # 扮演模式从不带 position（前端只在阅读页发）—— 不标出来，
            # current_passage 就会把 ch0 的目录当成「读者正在看的一段」。
            "has_position": bool(position),
            "reading_chapter": reading_chapter,
            "max_chapter": spoiler_gate,
            "graph_db_path": str(resolve_graph_db_path(config, db, book_id)) if config and db else "",
            "db": db,
            "config": config,
        })

    # ── 扮演：「你在哪本书里」是事实，不该让模型自己查 ────────────
    # 书名 / book_id / 读者上次读到的章，服务端全都有。不写进提示词，
    # 模型只能先 library_list 翻一遍书库来拿 id（实测每一回合多一轮）。
    book_note = ""
    if mode == "roleplay":
        book_note = _build_book_note(db, book_id, reading_chapter)

    # ── 写作模式：换一套提示词与工具，不借阅读人格 ────────────────
    # 写作的每一步都有明确输入输出，用的是同一套 ReAct 循环，
    # 但工具是 writing_*（请求级注册表，闭包持有本次请求的服务与现场）。
    active_registry = (runtime.tool_registry
                       if runtime and hasattr(runtime, "tool_registry")
                       else None)
    if mode == "writing":
        from writegraph.chat_tools import build_writing_registry
        _wsvc = _writing_service(request)
        if persisted_session_id:
            # 工具里发起的写作调用与这轮对话记在同一个会话下，
            # 飞行记录仪里才能一条线看到「对话 → 工具 → 正文生成」。
            _wsvc = _wsvc.with_scope(session_id=persisted_session_id)
        _wctx = {"work_id": writing_work_id,
                 "chapter_index": int(writing_chapter_index or 0)}
        if active_registry is not None:
            active_registry = build_writing_registry(
                active_registry, _wsvc, _wctx)
        system_prompt = _wsvc.agent_prompt(
            writing_work_id, int(writing_chapter_index or 0))
    else:
        system_prompt = _build_reading_prompt(runtime, mode, role_card,
                                              book_note=book_note)

    # ── Build messages with conversation history ─────────────────
    user_content = ""
    _injected_text = ""
    if context_parts:
        _injected_text = "\n\n".join(context_parts) + "\n\n"
        user_content += _injected_text
    user_content += message

    # ── Build messages: structured history + token budget ──────────
    # 不再用 history[-6:] + 每条 1500 字的硬切片。预算按窗口算，裁剪只
    # 发生在 user 回合边界，调用/结果永远成对；超出的老回合压成一段机械
    # 摘要（只进本次请求，不落盘，原始会话保持完整）。
    api_messages: list[dict] = []
    _history_source = (raw_db_history if raw_db_history
                       else chat_context.client_history_to_api(history))
    if _history_source:
        try:
            _tools_for_budget = (active_registry.get_schemas()
                                 if active_registry is not None else [])
        except Exception:
            _tools_for_budget = []
        _window = 0
        try:
            from context_meter import resolve_profile
            _window = int(resolve_profile(
                getattr(config, "model", ""), config).get("window") or 0)
        except Exception:
            _window = 0
        _reserve = (chat_context.approx_tokens(system_prompt)
                    + chat_context.approx_tokens(
                        json.dumps(_tools_for_budget, ensure_ascii=False)))
        _budget = chat_context.compute_history_budget(
            _window, reserve_tokens=_reserve)
        api_messages, _dropped = chat_context.build_prompt_history(
            _history_source, _budget)
        if _dropped:
            logging.getLogger("api").info(
                "chat history compacted: kept=%d dropped=%d budget=%d window=%d",
                len(api_messages), len(_dropped), _budget, _window)
    _hist_len = len(api_messages)  # history-only boundary for context meter
    api_messages.append({"role": "user", "content": user_content})


    # ── ReAct tool loop (max 5 rounds) ───────────────────────────
    # Each round: stream LLM → if tool_calls, execute them → feed results
    # back → loop.  Without this, tools are schema-only decoration.
    _recorder = request.app.get("context_recorder")
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "X-Session-Id, X-User-Message-Id",
            "X-Session-Id": persisted_session_id or "",
            "X-User-Message-Id": user_message_id or "",
        },
    )
    # 阅读/主页一轮 5 步足够；写作模式有建书→开岔路→合并→回滚这类
    # 多步工作流，5 步会在中途硬停。上限仍是有限值，模型不再调工具就退出。
    # 20 轮时实测自测清单的 B/C 阶段会正好撞顶：下一轮用户说「继续」，
    # 而跨轮历史只回灌纯文本、工具结果全丢，agent 只能从 status/log/读章
    # 重新摸路。40 轮让一个阶段能在一轮里收口，减少这种「撞顶 → 重摸」。
    MAX_REACT_ROUNDS = 40 if mode == "writing" else 5
    react_round = 0
    full_text = ""
    # 结构化落盘用：本轮最后一轮正文是否已经作为 tool 轮落过盘。
    # 工具轮会在执行工具前落 assistant+tool_use，最终轮在循环结束后落。
    final_round_text = ""
    final_round_persisted = False
    last_turn_usage = None  # final round's TokenUsage — context meter input

    # ── Agent reply persistence ─────────────────────────────────
    # Persisted the moment streaming ends — normally or interrupted —
    # so a dead SSE connection (browser gave up / machine slept) can
    # never take the reply's only way to disk down with it.
    _agent_persisted = False
    interrupted = False

    def _persist_agent_reply(text: str, was_interrupted: bool = False) -> None:
        nonlocal _agent_persisted
        # 中断时即使一个字都没吐出来也要落一条：下一轮的历史里必须有
        # 「上一轮被用户打断」这件事，否则模型会以为对话是连续的。
        if _agent_persisted or not (session_service and persisted_session_id):
            return
        if not text and not was_interrupted:
            return
        _agent_persisted = True
        try:
            from content import ConversationMessage as _CM, ContentBlock as _CB
            blocks = _blocks_from_text(text or "")
            if was_interrupted:
                # 注记和正文同属一条 assistant 消息：结构化历史重建把它
                # 拼回上下文；handle_session_load 用独立字段画「已中断」。
                blocks.append(_CB(type="interrupt", text=_INTERRUPT_NOTICE))
            agent_msg = _CM(
                role="assistant", blocks=blocks,
                # 中断轮没有可信的 usage —— 不要沿用上一轮的。
                usage=None if was_interrupted else last_turn_usage,
            )
            session_service.append_message(persisted_session_id, agent_msg)
        except Exception as exc:
            logging.getLogger("api").warning(f"Session agent-msg persist failed: {exc}")
    while react_round < MAX_REACT_ROUNDS:
        react_round += 1

        # Refresh tool schema each round (toolset visibility may change)
        tools_schema = (active_registry.get_schemas()
                        if active_registry is not None else None)

        # ── Save input for flight recorder (recorded after output) ──
        _rec_input = {
            "system_prompt": system_prompt,
            "messages": list(api_messages),
            "tools_count": len(tools_schema) if tools_schema else 0,
        } if _recorder else None

        if react_round == 1:
            try:
                await resp.prepare(request)
            except Exception as exc:
                import logging
                logging.getLogger("api").warning(f"SSE prepare failed: {exc}")
                return resp

        # 用户在上下文构建阶段就按了停止：仍然走完 prepare + 中断落盘，
        # 不直接 return —— 否则前端拿不到「已中断」的终态。
        if cancel_event is not None and cancel_event.is_set():
            interrupted = True
            break

        round_text = ""
        tool_calls_at_end: list[dict] = []
        think_parser = _ThinkParser() if stream_version == "v2" else None
        try:
            async for text_chunk, accum_tools, _round_usage in api_client.stream(
                system_prompt, api_messages, tools_schema, cancel_event,
            ):
                if text_chunk:
                    round_text += text_chunk
                    if stream_version == "v2":
                        for evt in think_parser.feed(text_chunk):
                            await _sse_write_json(resp, evt)
                    else:
                        await resp.write(f"data: {text_chunk}\n\n".encode("utf-8"))
                tool_calls_at_end = accum_tools
                if _round_usage:
                    last_turn_usage = _round_usage
        except Exception as exc:
            # 客户端断开会表现为写失败；如果同一轮已经收到取消请求，
            # 那是用户主动打断，不是故障 —— 注记要照常落盘。
            interrupted = bool(cancel_event and cancel_event.is_set())
            logging.getLogger("api").warning(
                "SSE stream error (interrupted=%s): %s", interrupted, exc)
            # break skips the full_text accumulation below and every
            # subsequent SSE write targets a connection that may already
            # be gone — persist what arrived before attempting any of it.
            full_text += round_text
            final_round_text = round_text
            _persist_agent_reply(round_text, interrupted)
            if not interrupted:
                try:
                    if stream_version == "v2":
                        await _sse_write_json(resp, {"type": "error", "message": f"回答中断: {exc}"})
                    else:
                        await resp.write(f"data: [回答中断: {exc}]\n\n".encode("utf-8"))
                except Exception:
                    pass
            break

        # Flush remaining think-parser buffer — the SSE connection may
        # already be dead; a failed write here must not kill the handler
        # before the reply is persisted.
        if think_parser:
            try:
                for evt in think_parser.flush():
                    await _sse_write_json(resp, evt)
            except Exception:
                pass

        full_text += round_text
        final_round_text = round_text
        final_round_persisted = False

        # 打断可能正好落在一轮流结束的瞬间：保留已收到的正文，但绝不
        # 再执行这一轮的 tool_calls（它们可能只拼了一半）。
        if cancel_event is not None and cancel_event.is_set():
            interrupted = True
            break

        # ── No tool calls → LLM is done ──────────────────────────────
        if not tool_calls_at_end:
            # ── Flight recorder: final round (no tools) ──
            if _recorder and _rec_input:
                _recorder.record_round(
                    session_id=persisted_session_id or "",
                    turn_index=1,
                    react_round=react_round,
                    mode=mode,
                    book_id=book_id,
                    response_text=round_text,
                    tool_calls=None,
                    tool_results=None,
                    **_rec_input,
                )
            break

        # ── Tool round: persist assistant + tool_use, then results ────
        # 工具轮必须结构化落盘。只在最后落一条纯文本，等于把 ReAct 的
        # 中间证据全部丢掉 —— 下一轮重建历史时模型手里只有「说过什么」，
        # 只能从 status/log/读章重新摸路。
        try:
            _persist_round_assistant(
                session_service, persisted_session_id,
                round_text, tool_calls_at_end)
            final_round_persisted = True
        except Exception as _exc:
            logging.getLogger("api").warning(
                "persist tool round failed: %s", _exc)

        _tool_results: list[dict] = []
        api_messages.append({
            "role": "assistant",
            # <think> 是我们的展示层标签，不回灌给模型重读。
            "content": _strip_think(round_text) or None,
            "tool_calls": [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls_at_end
            ],
        })
        _executed_ids: set[str] = set()
        for tc in tool_calls_at_end:
            # 有界等待：同步工具会占住事件循环，取消请求只能在工具返回后
            # 被处理 —— 所以每个工具前都查一次，剩下的不再执行。
            if cancel_event is not None and cancel_event.is_set():
                interrupted = True
                break
            tool_name = tc["name"]
            tool_input_str = tc.get("arguments", "{}")
            if stream_version == "v2":
                try:
                    await _sse_write_json(resp, {"type": "tool_start", "name": tool_name})
                except Exception:
                    pass
            else:
                try:
                    await resp.write(
                        f"data: [🔧 {tool_name}...]\n\n".encode("utf-8")
                    )
                except Exception:
                    pass
            try:
                if active_registry is not None:
                    result = await active_registry.execute_async(
                        tool_name, tool_input_str)
                else:
                    result = json.dumps({"error": f"Tool not found: {tool_name}"})
            except Exception as _exc:
                result = json.dumps({"error": f"Tool error: {_exc}"})
            # 回灌与落盘都用截断后的版本（头尾保留）；飞行记录仪仍存全文。
            # 读正文类工具放宽额度——它的输出是模型接下来要改的对象。
            _prompt_result = chat_context.truncate_tool_output(
                result, chat_context.tool_output_budget(tool_name))
            api_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": _prompt_result,
            })
            _executed_ids.add(tc["id"])
            try:
                _persist_tool_message(
                    session_service, persisted_session_id,
                    tc, _prompt_result)
            except Exception as _exc:
                logging.getLogger("api").warning(
                    "persist tool result failed: %s", _exc)
            if _recorder:
                _tool_results.append({
                    "name": tool_name,
                    "arguments": tool_input_str,
                    "result": result,
                })
            # 工具跑完时用户已经按了停止：结果已经落盘成对，但不再发
            # tool_end，也不再回灌下一轮 LLM。
            if cancel_event is not None and cancel_event.is_set():
                interrupted = True
                break
            if stream_version == "v2":
                # Truncate long tool results for the event
                result_preview = result[:200] + ("…" if len(result) > 200 else "")
                try:
                    await _sse_write_json(resp, {"type": "tool_end", "name": tool_name, "preview": result_preview})
                except Exception:
                    pass

        # 中断/异常导致没跑到的 call：补一条 aborted。
        # 落盘的 call 必须永远有 output —— 下一轮重建时才不会发出
        # OpenAI 协议拒绝的半对历史。
        if session_service and persisted_session_id:
            for tc in tool_calls_at_end:
                if tc["id"] in _executed_ids:
                    continue
                try:
                    _persist_tool_message(
                        session_service, persisted_session_id,
                        tc, "aborted", is_error=True)
                except Exception:
                    pass

        if interrupted:
            break

        # ── Flight recorder: round with tool calls ──────────────
        if _recorder and _rec_input:
            _recorder.record_round(
                session_id=persisted_session_id or "",
                turn_index=1,
                react_round=react_round,
                mode=mode,
                book_id=book_id,
                response_text=round_text,
                tool_calls=tool_calls_at_end,
                tool_results=_tool_results,
                **_rec_input,
            )

    # ── Interrupted: a distinct terminal signal before `done` ────
    # The client freezes its typewriter on this event and paints the
    # "已中断" marker; `done` still follows so session/usage bookkeeping runs.
    if interrupted:
        try:
            if stream_version == "v2":
                await _sse_write_json(resp, {"type": "interrupted"})
            else:
                await resp.write("data: [已中断]\n\n".encode("utf-8"))
        except Exception:
            pass

    # ── Context meter: turn-end snapshot (ring data source) ──────
    # Real totals from the final round's usage; blocks aligned proportionally.
    # An interrupted round has no trustworthy usage — leave the ring as is.
    usage_payload = None
    if runtime is not None and not interrupted:
        import asyncio as _asyncio
        import logging as _ctxlog
        try:
            from context_meter import format_bill, measure, resolve_profile
            _model = getattr(config, "model", "")
            _prof = resolve_profile(_model, config)
            _alert = getattr(config, "context_alert_ratio", 0.35)
            _snapshot = await _asyncio.to_thread(
                measure,
                system_prompt=system_prompt,
                tools_schema=tools_schema or [],
                history_messages=api_messages[:_hist_len],
                injected_context=_injected_text,
                user_input=message,
                usage=last_turn_usage,
                window=_prof["window"],
                model=_model,
                tokenizer_path=_prof["tokenizer_path"],
                session_id=persisted_session_id or "",
            )
            runtime.last_context_usage = _snapshot
            runtime.last_context_usage_session = persisted_session_id or ""
            _ctxlog.getLogger("context").debug(
                format_bill(_snapshot, alert_ratio=_alert))
            usage_payload = _snapshot.to_dict(alert_ratio=_alert)
        except Exception:
            _ctxlog.getLogger("api").warning(
                "context meter failed", exc_info=True)

    try:
        if stream_version == "v2":
            _done_evt: dict = {
                "type": "done",
                "session_id": persisted_session_id or "",
            }
            if usage_payload:
                _done_evt["usage"] = usage_payload
            if interrupted:
                # 兜底：客户端没读到最后那个 interrupted 帧时也能知道
                _done_evt["interrupted"] = True
            await _sse_write_json(resp, _done_evt)
        else:
            await resp.write(b"data: [DONE]\n\n")
    except Exception:
        pass

    # ── Persist agent reply to session ──────────────────────────
    # Interrupted streams persisted their partial text inside the
    # stream-except handler; the flag inside _persist_agent_reply
    # dedupes the two paths. The interrupt block is what lets the next
    # turn's history say "上一轮被用户打断了".
    _persist_agent_reply(
        "" if final_round_persisted else final_round_text,
        interrupted,
    )

    # ── Memory sync: feed completed turn to the memory pipeline ──────
    # Web chat reimplements the ReAct loop directly (api_client.stream),
    # bypassing runtime.run_turn() where the memory sync normally lives.
    # Call it here so the L0-L3 pipeline sees every completed turn.
    # 被打断的半句话不进长期记忆（L1 会把残缺回复当事实提炼）。
    if (not interrupted and runtime
            and getattr(runtime, "memory_manager", None) and full_text):
        try:
            # Strip think tags so L1 extracts from visible replies only
            _clean_reply = re.sub(
                r" thinking.*? response", "", full_text, flags=re.DOTALL
            ).strip()
            runtime.memory_manager.sync_all(
                message, _clean_reply,
                session_id=persisted_session_id or "",
            )
            runtime.memory_manager.queue_prefetch_all(
                message,
                session_id=persisted_session_id or "",
            )
        except Exception:
            pass

    # Graph evolution: background extraction from this dialogue turn
    # 同上：残缺回复不喂图谱，避免半句被记成角色观察。
    if (not interrupted and runtime
            and getattr(runtime, 'current_book_id', '') and full_text):
        _stable = runtime.current_book_id
        _dlg_parts = []
        for h in (history or [])[-3:]:
            _role = h.get("role", "")
            _content = h.get("content", "")
            if _role == "user" and _content:
                _dlg_parts.append(f"读者: {_content}")
            elif _role == "assistant" and _content:
                _dlg_parts.append(f"伴读助手: {_content}")
        _dlg_parts.append(f"读者: {message}")
        _dlg_parts.append(f"伴读助手: {full_text}")
        _dlg_text = "\n\n".join(_dlg_parts)
        import asyncio as _aio
        _aio.create_task(_bg_graph_extract(
            _stable, _dlg_text, api_client,
            request.app.get("config"), db, book_id,
        ))

    await resp.write_eof()
    return resp
