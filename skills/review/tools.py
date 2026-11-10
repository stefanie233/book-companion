"""Review tools — expose review_sessions to the agent.

Registers the `review_sessions` tool for manual triggering of session
refinement via sub-agents (memory_write tool).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from permissions import PermissionMode

logger = logging.getLogger(__name__)


def register_tools(registry) -> None:
    """Register review_sessions tool into the agent's ToolRegistry."""

    # ── Capture references from registry (set by caller) ────────────────
    _main_db: Any = None
    _wingman_db: Any = None
    _api_client: Any = None
    _config: Any = None
    _tool_registry: Any = None  # The full ToolRegistry (for sub-agent filtering)

    def _set_context(**kwargs) -> None:
        nonlocal _main_db, _wingman_db, _api_client, _config, _tool_registry
        _main_db = kwargs.get("session_db", _main_db)
        _wingman_db = kwargs.get("wingman_db", _wingman_db)
        _api_client = kwargs.get("api_client", _api_client)
        _config = kwargs.get("config", _config)
        _tool_registry = kwargs.get("tool_registry", _tool_registry)

    def _format_session_for_review(messages: list) -> str:
        """Format session messages as human-readable text for sub-agent review.

        Messages come from DB with 'role' and 'parts' (list of {type, data}).
        Each part's 'data' is a JSON string containing the actual content.
        """
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            parts = msg.get("parts", [])
            if not parts:
                continue

            if role == "user":
                for p in parts:
                    if p.get("type") == "text":
                        try:
                            d = json.loads(p.get("data", "{}"))
                            text = d.get("text", "")
                            if text and text.strip():
                                lines.append(f"读者：{text.strip()}")
                        except (json.JSONDecodeError, TypeError):
                            pass
            elif role == "assistant":
                for p in parts:
                    ptype = p.get("type", "")
                    try:
                        d = json.loads(p.get("data", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if ptype == "text":
                        text = d.get("text", "")
                        if text and text.strip():
                            lines.append(f"留白：{text.strip()}")
        return "\n\n".join(lines)

    async def _review_sessions_async(
        session_ids: str = "",
        mode: str = "",
    ) -> str:
        """Review sessions via sub-agents → memory_write (async — primary path)."""
        if not _main_db or not _wingman_db or not _api_client:
            return json.dumps({
                "error": "Review service not available (missing DB or API client)."
            })

        # Resolve target sessions
        if session_ids:
            sid_list = [s.strip() for s in session_ids.split(",") if s.strip()]
        else:
            all_sessions = _main_db.list_sessions(limit=100)
            sid_list = [s.id for s in all_sessions]

        if not sid_list:
            return json.dumps({"result": "No sessions to review."}, ensure_ascii=False)

        # Limit to 5 sessions per call
        sid_list = sid_list[:5]

        import asyncio
        from sub_agent import SubAgentRunner

        async def _review_one(sid: str) -> dict:
            try:
                msgs = _main_db.load_messages(sid)
                if not msgs:
                    return {"session_id": sid, "status": "skipped", "reason": "no messages"}

                session_text = _format_session_for_review(msgs)
                if not session_text.strip():
                    return {"session_id": sid, "status": "skipped", "reason": "empty"}

                runner = SubAgentRunner(_api_client, _tool_registry or registry, _config)
                output = await asyncio.wait_for(
                    runner.run(
                        agent_type="reader",
                        description=f"Review session {sid[:12]}",
                        prompt=(
                            "回顾以下对话，将值得长期记住的信息提炼为 1-3 条综合性记忆。\n\n"
                            "重要：请合并相关发现，不要逐条罗列。一条记忆应该是一个完整主题的洞察"
                            "（如「用户对某类书的偏好及原因」），而非单个孤立事实。\n\n"
                            "步骤：\n"
                            "1. 通读对话，归纳出 1-3 个核心主题\n"
                            "2. 用 memory_search 检查是否已有类似记忆，避免重复\n"
                            "3. 用 memory_write 写入，使用结构化格式。\n"
                            "   - keyword 包含 2-5 个核心主题词（精准词+归类词都放）\n"
                            "   - tags 负责归类（如 '村上春树', '科幻', '阅读偏好'）\n\n"
                            f"对话内容：\n{session_text}"
                        ),
                    ),
                    timeout=60,
                )
                # Verify memories were produced before marking done.
                # No memories → TBD (time_reviewed=0), won't block re-review
                # but won't keep appearing in automatic scans either.
                if output.status == "completed":
                    try:
                        linked = _wingman_db.get_memories_by_session(sid, limit=1)
                        if linked:
                            _main_db.mark_reviewed(sid)
                        else:
                            _main_db.mark_tbd(sid)
                    except Exception:
                        pass
                return {
                    "session_id": sid,
                    "status": output.status,
                    "result": output.result_text[:500] if output.result_text else "",
                }
            except asyncio.TimeoutError:
                return {"session_id": sid, "status": "timeout"}
            except Exception as e:
                return {"session_id": sid, "status": "failed", "error": str(e)}

        try:
            results = await asyncio.gather(*(_review_one(sid) for sid in sid_list))
        except Exception as e:
            return json.dumps({"error": f"Sub-agent review failed: {e}"}, ensure_ascii=False)

        return json.dumps({
            "reviewed": len(results),
            "sessions": results,
        }, ensure_ascii=False)

    def _review_sessions(
        session_ids: str = "",
        mode: str = "",
    ) -> str:
        """Sync fallback — delegates to async version via asyncio.run()."""
        import asyncio
        return asyncio.run(_review_sessions_async(session_ids, mode))

    # ── Register ────────────────────────────────────────────────────────
    from tools import ToolSpec

    registry.register(ToolSpec(
        name="review_sessions",
        toolset="knowledge",
        description=(
            "Batch-analyze past sessions via sub-agents to extract durable "
            "memories into memory_entries. This is HEAVYWEIGHT — it spawns "
            "sub-agents per session. "
            "NOT for reading session content — use search_sessions + "
            "read_session for that. "
            "Use this when the user asks to 'review', 'recap', or 'reflect' "
            "on past conversations. "
            "By default reviews ALL unreviewed sessions; pass session_ids "
            "to target specific ones."
        ),
        parameters={
            "type": "object",
            "properties": {
                "session_ids": {
                    "type": "string",
                    "description": (
                        "Comma-separated session IDs to review. "
                        "If empty, reviews all unrefined sessions."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["immersive", "overview", "divergent"],
                    "description": "Only review sessions of this mode.",
                },
            },
        },
        execute=_review_sessions,
        execute_async=_review_sessions_async,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # Store set_context so main.py can wire it up
    registry._review_set_context = _set_context
