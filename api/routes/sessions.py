"""Session endpoints: list / load / fork / archive / search."""
from __future__ import annotations

import json

from aiohttp import web



async def handle_sessions_list(request: web.Request) -> web.Response:
    """List all chat sessions (metadata only).

    Query params:
        limit          — max sessions to return (default 100)
        search         — optional title keyword filter
        project_id     — filter by book
        character_name — filter by role-play character
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    limit = min(max(int(request.query.get("limit", "100")), 1), 500)
    search = request.query.get("search") or None
    project_id = request.query.get("project_id") or None
    character_name = request.query.get("character_name") or None
    root_only = request.query.get("root_only", "").lower() in ("1", "true", "yes")

    sessions = session_service.list(
        limit=limit, search=search, project_id=project_id,
        character_name=character_name, root_only=root_only,
    )

    # Build book-title lookup map for labeling sessions
    book_titles: dict[str, str] = {}
    try:
        db = request.app.get("db")
        if db:
            for b in db.list_books():
                book_titles[b.id] = b.title
    except Exception:
        pass

    # Batch query children counts (for tree expand indicators)
    children_counts = session_service.count_children_batch([s.id for s in sessions])

    items = []
    for s in sessions:
        pid = s.project_id or ""
        items.append({
            "id": s.id,
            "project_id": pid,
            "book_title": book_titles.get(pid, "") if pid else "",
            "title": s.title or "(无标题)",
            "mode": s.mode,
            "message_count": s.message_count,
            "time_created": s.time_created,
            "time_updated": s.time_updated,
            "character_name": s.character_name,
            "role_kind": s.role_kind or "character",
            "role_ref": s.role_ref or "",
            "children_count": children_counts.get(s.id, 0),
        })
    return web.json_response({"sessions": items, "count": len(items)})


async def handle_session_load(request: web.Request) -> web.Response:
    """Load a full session with all messages.

    Returns: {id, title, mode, messages: [{role, content}]}
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    info = session_service.get(session_id)
    if not info:
        return web.json_response({"error": "session not found"}, status=404)

    session = session_service.load_messages(session_id)
    if not session:
        return web.json_response({"error": "failed to load messages"}, status=500)

    messages: list[dict] = []
    # 一轮 ReAct 会落多条 assistant（每个工具轮一条）。流式展示时它们是
    # 同一个气泡；重载也必须合并成一个，否则气泡数、fork 索引都会和实时
    # 看到的不一致。
    pending: dict | None = None

    def _flush_pending() -> None:
        nonlocal pending
        if pending is None:
            return
        pending["thinking"] = pending["thinking"] or None
        pending["thinkings"] = pending["thinkings"] or None
        pending["parts"] = pending["parts"] or None
        messages.append(pending)
        pending = None

    for m in session.messages:
        # 工具调用/结果是给模型的历史证据，不是聊天记录：前端不渲染。
        # 它们的正文在 tool_output 里、content 为空，渲染出来是一个空气泡。
        if m.role == "tool":
            continue
        parts: list[dict] = []
        content = ""
        thinking = ""
        # One entry per thinking block. A ReAct turn persists one block per
        # LLM call, in call order; the frontend used to receive only the
        # concatenation and merged them into a single collapsed row, so a
        # reload looked like only the last call's reasoning survived.
        thinkings: list[str] = []
        for b in m.blocks:
            if b.type == "text" and b.text:
                content += b.text
            if b.type == "thinking" and b.thinking:
                thinking += b.thinking
                thinkings.append(b.thinking)
            if b.type in ("voice_input", "voice_output") and b.text:
                try:
                    parts.append({"type": b.type, "data": json.loads(b.text)})
                except json.JSONDecodeError:
                    pass  # malformed voice metadata, skip
            elif b.type == "tool_result" and b.text:
                # Check if it's a tts_speak result (has type:voice)
                try:
                    tr = json.loads(b.text)
                    if isinstance(tr, dict) and tr.get("type") == "voice":
                        parts.append({"type": "voice_output", "data": tr})
                except (json.JSONDecodeError, TypeError):
                    pass
        interrupted = any(b.type == "interrupt" for b in m.blocks)
        # 纯 tool_use 的 assistant 轮（没有正文/推理）也不进可见记录：
        # 它只是「我要调工具」的协议壳，正文在下一轮回复里。
        if (m.role == "assistant" and not content and not thinking
                and not parts and not interrupted):
            continue
        if m.role == "assistant":
            if pending is None:
                pending = {
                    "id": m.id,
                    "role": "assistant",
                    "content": "",
                    "interrupted": False,
                    "thinking": "",
                    "thinkings": [],
                    "parts": [],
                    "time_created": m.time_created or 0,
                }
            pending["content"] += content
            pending["thinking"] += thinking
            pending["thinkings"].extend(thinkings)
            pending["parts"].extend(parts)
            pending["interrupted"] = pending["interrupted"] or interrupted
            continue
        _flush_pending()
        messages.append({
            "id": m.id,
            "role": m.role,
            "content": content,
            # 中断注记不并进 content：正文保持原样，前端单独画一枚
            # 「已中断」标记，重载后和流式时看到的一致。
            "interrupted": interrupted,
            "thinking": thinking or None,  # omit if empty (for old sessions)
            # Per-call rows for the reload UI; old clients keep using
            # `thinking` (concatenated).
            "thinkings": thinkings if thinkings else None,
            "parts": parts if parts else None,  # omit if empty
            "time_created": m.time_created or 0,
        })
    _flush_pending()

    return web.json_response({
        "id": info.id,
        "project_id": info.project_id,
        "title": info.title or "(无标题)",
        "mode": info.mode,
        "character_name": info.character_name,
        "role_kind": info.role_kind or "character",
        "role_ref": info.role_ref or "",
        "message_count": info.message_count,
        "time_created": info.time_created,
        "time_updated": info.time_updated,
        "parent_id": info.parent_id,
        "fork_point_index": info.fork_point_index,
        "messages": messages,
    })


async def handle_session_delete(request: web.Request) -> web.Response:
    """Delete a session and all its messages."""
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    try:
        session_service.delete(session_id)
    except Exception as exc:
        return web.json_response({"error": f"delete failed: {exc}"}, status=500)
    return web.json_response({"deleted": session_id})


async def handle_session_set_title(request: web.Request) -> web.Response:
    """Manually set a session's title.

    POST body: {title: "..."}
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    title = (body.get("title") or "").strip()
    if not title:
        return web.json_response({"error": "title required"}, status=400)
    session_service.update_title(session_id, title[:200])
    return web.json_response({"id": session_id, "title": title[:200]})


async def handle_session_search(request: web.Request) -> web.Response:
    """Full-text search across session message content.

    Query params:
        q     — search query (required)
        limit — max results (default 10, max 50)
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    q = request.query.get("q", "").strip()
    if not q:
        return web.json_response({"error": "query required"}, status=400)
    limit = min(max(int(request.query.get("limit", "10")), 1), 50)

    results = session_service.search_content(q, limit=limit)
    return web.json_response({"results": results, "query": q})


async def handle_session_archive(request: web.Request) -> web.Response:
    """Archive a session (prune large tool outputs + mark archived)."""
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    try:
        session_service.archive(session_id)
    except Exception as exc:
        return web.json_response({"error": f"archive failed: {exc}"}, status=500)
    return web.json_response({"archived": session_id})


async def handle_session_fork(request: web.Request) -> web.Response:
    """Fork a session: deep-copy messages into a new session.

    The original session is preserved unchanged.
    POST body (optional):
        {branch_name, up_to_message_id, exclusive, fork_point_index}
    - exclusive=true: copy strictly before up_to_message_id (exclude it)
    - fork_point_index: stored on child for branch switcher rendering
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    branch_name = None
    up_to_message_id = None
    exclusive = False
    fork_point_index = None
    try:
        body = await request.json()
        branch_name = (body.get("branch_name") or "").strip() or None
        up_to_message_id = (body.get("up_to_message_id") or "").strip() or None
        exclusive = bool(body.get("exclusive", False))
        fpi = body.get("fork_point_index")
        if fpi is not None:
            fork_point_index = int(fpi)
    except Exception:
        pass  # body is optional

    child = session_service.fork(session_id, branch_name=branch_name,
                                 up_to_message_id=up_to_message_id,
                                 exclusive=exclusive,
                                 fork_point_index=fork_point_index)
    if not child:
        return web.json_response({"error": "session not found"}, status=404)

    return web.json_response({
        "id": child.session_id,
        "title": child.title or "(分叉)",
        "mode": child.mode,
        "message_count": len(child.messages),
        "time_created": child.created_at_ms,
        "parent_id": session_id,
    })


async def handle_session_branches(request: web.Request) -> web.Response:
    """Return branch info for the frontend switcher.

    Response: {current, parent, siblings, children}. ``siblings`` are the
    other children of ``parent`` — without them a fork point with 3+
    alternatives could only ever reach the parent and itself. Every entry
    carries ``fork_point_index`` (inferred for legacy NULL rows) and
    ``time_created`` for a stable "original first" ordering.
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    branches = session_service.get_branches(session_id)
    return web.json_response(branches)


async def handle_session_latest(request: web.Request) -> web.Response:
    """Resolve the fork family's most recently updated session.

    Used only by view-state restore: after a fork, reopening the app should
    land on the branch the user last interacted with, even if ``bc_view``
    still points at the parent (or at a sibling they peeked at).
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    latest_id = session_service.latest_in_family(session_id)
    info = session_service.get(latest_id)
    if not info:
        return web.json_response({"error": "session not found"}, status=404)
    return web.json_response({
        "id": info.id,
        "time_updated": info.time_updated,
    })


async def handle_session_children(request: web.Request) -> web.Response:
    """List direct child sessions (forks) of a session."""
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    children = session_service.list_children(session_id)

    # Build book-title lookup for labeling
    book_titles: dict[str, str] = {}
    try:
        db = request.app.get("db")
        if db:
            for b in db.list_books():
                book_titles[b.id] = b.title
    except Exception:
        pass

    # Batch query children counts (for nested tree expand indicators)
    children_counts = session_service.count_children_batch([s.id for s in children])

    items = []
    for s in children:
        pid = s.project_id or ""
        items.append({
            "id": s.id,
            "project_id": pid,
            "book_title": book_titles.get(pid, "") if pid else "",
            "title": s.title or "(无标题)",
            "mode": s.mode,
            "message_count": s.message_count,
            "time_created": s.time_created,
            "time_updated": s.time_updated,
            "character_name": s.character_name,
            "role_kind": s.role_kind or "character",
            "role_ref": s.role_ref or "",
            "children_count": children_counts.get(s.id, 0),
        })
    return web.json_response({"children": items, "count": len(items)})
