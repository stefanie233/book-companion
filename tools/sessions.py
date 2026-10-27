"""Session-history tools: search / list / read past conversations."""
from __future__ import annotations


import json
from typing import Any
from permissions import PermissionMode
from tools.registry import ToolExposure, ToolRegistry, ToolSpec


def _tool_search_sessions(
    query: str, limit: int = 10, include_archived: bool = False, db: Any = None,
) -> str:
    """Search session history by content keyword.

    Searches across all past conversation messages for the given query.
    Returns matching sessions with metadata and content snippets.
    By default excludes archived sessions.
    """
    if db is None:
        return json.dumps({"error": "Database not available"})

    try:
        results = db.search_session_content(
            query, limit=limit, include_archived=include_archived,
        )
    except Exception as e:
        return json.dumps({"error": f"Search failed: {e}"})

    if not results:
        return json.dumps({"message": f"No sessions found matching '{query}'", "sessions": []})

    import datetime
    output = []
    for r in results:
        ts = datetime.datetime.fromtimestamp(r["time_updated"] / 1000).strftime("%Y-%m-%d %H:%M")
        entry = {
            "session_id": r["session_id"],
            "title": r["title"],
            "mode": r["mode"],
            "updated": ts,
            "snippets": r.get("snippets", []),
        }
        output.append(entry)

    return json.dumps({"sessions": output}, ensure_ascii=False)


def _tool_list_sessions(
    limit: int = 20, include_archived: bool = False, db: Any = None,
) -> str:
    """List recent sessions with metadata.

    Returns session IDs, titles, modes, message counts and update times.
    Does NOT load full message content — use read_session for that.
    By default excludes archived sessions.
    """
    if db is None:
        return json.dumps({"error": "Database not available"})

    try:
        rows = db.list_sessions(limit=limit, include_archived=include_archived)
    except Exception as e:
        return json.dumps({"error": f"List failed: {e}"})

    if not rows:
        return json.dumps({"message": "No sessions found", "sessions": []})

    import datetime
    output = []
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r.time_updated / 1000).strftime("%Y-%m-%d %H:%M")
        output.append({
            "session_id": r.id,
            "title": r.title or "无标题",
            "mode": r.mode,
            "message_count": r.message_count,
            "updated": ts,
        })

    return json.dumps({"sessions": output}, ensure_ascii=False)


def _tool_read_session(session_id: str, db: Any = None, session_service: Any = None) -> str:
    """Load a full session's conversation history by session ID.

    Returns all messages in the session with their content.
    Use list_sessions or search_sessions to find session IDs first.
    """
    if session_service is None:
        return json.dumps({"error": "Session service not available"})

    try:
        session = session_service.load_messages(session_id)
        if session is None:
            return json.dumps({"error": f"Session '{session_id}' not found"})

        messages = []
        for msg in session.messages:
            parts = []
            for block in msg.blocks:
                if block.type == "text":
                    parts.append({"type": "text", "text": block.text or ""})
                elif block.type == "tool_use":
                    parts.append({
                        "type": "tool_use",
                        "tool_name": block.tool_name or "",
                        "tool_input": block.tool_input or {},
                    })
                elif block.type == "tool_result":
                    parts.append({
                        "type": "tool_result",
                        "tool_output": (block.tool_output or "")[:500],
                    })
            messages.append({
                "role": msg.role,
                "parts": parts,
            })

        return json.dumps({
            "session_id": session.session_id,
            "title": session.title or "无标题",
            "mode": session.mode,
            "message_count": len(messages),
            "messages": messages,
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"Read session failed: {e}"})


def _register_session_tools(registry: ToolRegistry, db, session_service) -> None:
    # ── Session history tools ────────────────────────────────────────
    if db is not None:
        registry.register(ToolSpec(
            name="search_sessions",
            category="session",
            toolset="knowledge",
            exposure=ToolExposure.DEFERRED,
            description=(
                "Search past conversation history by content keyword. "
                "Returns matching sessions with metadata and message snippets. "
                "Use this to find previous discussions about a book, topic, or character. "
                "Excludes archived sessions by default — use include_archived=true to search them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword(s) to find in past conversations",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum sessions to return (default: 10, suggested: 3-5)",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived sessions (default: false)",
                    },
                },
                "required": ["query"],
            },
            execute=lambda query, limit=10, include_archived=False: _tool_search_sessions(query, limit, include_archived, db=db),
            permission_mode=PermissionMode.READ_ONLY,
        ))

        registry.register(ToolSpec(
            name="list_sessions",
            category="session",
            toolset="knowledge",
            exposure=ToolExposure.DEFERRED,
            description=(
                "List recent session history. Returns session IDs, titles, modes, "
                "message counts, and last-update timestamps. "
                "Use this to browse past conversations. Follow with read_session "
                "to load the full content of a specific session. "
                "Excludes archived sessions by default."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many recent sessions to list (default: 20)",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived sessions (default: false)",
                    },
                },
            },
            execute=lambda limit=20, include_archived=False: _tool_list_sessions(limit, include_archived, db=db),
            permission_mode=PermissionMode.READ_ONLY,
        ))

    if session_service is not None:
        registry.register(ToolSpec(
            name="read_session",
            category="session",
            toolset="knowledge",
            exposure=ToolExposure.DEFERRED,
            description=(
                "Load the full conversation history of a past session by its ID. "
                "Returns all messages with their content. "
                "Use list_sessions or search_sessions to find the session ID first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID (from list_sessions or search_sessions)",
                    },
                },
                "required": ["session_id"],
            },
            execute=lambda session_id: _tool_read_session(session_id, db=db, session_service=session_service),
            permission_mode=PermissionMode.READ_ONLY,
        ))
