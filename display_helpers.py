"""CLI display formatting — table views, previews, resume reports.

Extracted from commands.py to keep display logic separate from dispatch.
"""

from __future__ import annotations

from datetime import datetime

from content import ConversationMessage
from services.session_info import SessionInfo


def format_sessions_numbered(sessions: list[SessionInfo]) -> str:
    """Render sessions as a numbered list with titles for interactive selection.

    Plan A style:
      1  百年孤独叙事结构分析                   沉浸伴读  3条  10:30
      2  村上春树的比喻风格探讨                 发散探索  8条  昨天
    """
    if not sessions:
        return "没有找到已保存的会话。"

    lines: list[str] = []
    for i, s in enumerate(sessions, 1):
        title = s.title or "(新会话)"
        mode_label = _mode_label(s.mode)
        updated = _format_time(s.time_updated)
        title_display = _truncate(title, 50)
        lines.append(f" {i:3d}  {title_display:<50s}  {mode_label:<8s}  {s.message_count}条  {updated}")

    lines.append("")
    lines.append(f"共 {len(sessions)} 条会话")
    lines.append("/sessions <编号> 加载  |  /sessions delete <编号> 删除  |  /sessions search <关键词> 搜索")
    return "\n".join(lines)

_deprecated_sessions_table = format_sessions_numbered  # backward compat alias


def format_resume_report(session,
                         info: SessionInfo | None = None) -> str:
    """Structured resume report + recent conversation history."""
    sid = info.id if info else session.session_id
    turns = sum(1 for m in session.messages if m.role == "assistant")
    model = info.model if info else session.model
    mode = info.mode if info else session.mode

    lines = [
        "Session resumed",
        f"  Title            {session.title or '(无标题)'}",
        f"  Session ID       {sid}",
        f"  Messages         {len(session.messages)}",
        f"  Turns            {turns}",
        f"  Model            {model or '?'}",
        f"  Mode             {mode}",
    ]

    # Recent conversation
    MAX_HISTORY = 10
    msgs = session.messages
    tail = msgs[-MAX_HISTORY:] if len(msgs) > MAX_HISTORY else msgs
    prefix = len(msgs) - len(tail)
    if tail:
        if prefix:
            lines.append("")
            lines.append(f"Recent conversation (last {len(tail)} of {len(msgs)} messages):")
        else:
            lines.append("")
            lines.append("Conversation:")
        lines.append("")
        for msg in tail:
            role_symbol = {"user": "", "assistant": "  ", "system": "  "}.get(msg.role, "  ")
            text = message_preview(msg)
            if text:
                lines.append(f"  {role_symbol}{msg.role}  {text}")

    return "\n".join(lines)


def message_preview(msg: ConversationMessage) -> str:
    """Compact one-line preview of a conversation message."""
    parts: list[str] = []
    for b in msg.blocks:
        if b.type == "text" and b.text:
            text = b.text.replace("\n", " ").strip()
            if len(text) > 200:
                text = text[:197] + "..."
            parts.append(text)
        elif b.type == "tool_use":
            parts.append(f"[tool: {b.tool_name}(...)]")
        elif b.type == "tool_result":
            out = (b.tool_output or "").replace("\n", " ").strip()
            if len(out) > 80:
                out = out[:77] + "..."
            err = " ! error" if b.is_error else ""
            parts.append(f"[result: {out}{err}]")
    return "  ".join(parts) if parts else "(空)"


def render_conversation_tail(session, max_messages: int = 12) -> str:
    """Render recent conversation messages faithfully for session resume.

    Shows the last N messages with role labels, newlines preserved,
    and visual separators between turns. Truncates individual messages
    at a generous limit (3000 chars) to avoid flooding the terminal.
    """
    msgs = session.messages
    tail = msgs[-max_messages:] if len(msgs) > max_messages else msgs
    prefix = len(msgs) - len(tail)
    if not tail:
        return "（无消息）"

    lines: list[str] = []
    subtitle = f"最近 {len(tail)} 条" if prefix else "全量"
    lines.append(f"─── {subtitle} 历史对话 ───")
    lines.append("")

    for msg in tail:
        if msg.role == "user":
            text = _first_text_block(msg)
            if text:
                lines.append(f"  书伴> {text}")
        elif msg.role == "assistant":
            text = _first_text_block(msg)
            if text:
                lines.append(f"  书伴  >")
                for p in text.split("\n"):
                    lines.append(f"    {p}")
        elif msg.role == "tool":
            for b in msg.blocks:
                if b.type == "tool_result" and b.tool_output:
                    preview = b.tool_output.replace("\n", " ").strip()[:120]
                    if preview:
                        lines.append(f"    [工具 {b.tool_name}: {preview}]")

    lines.append("")
    remaining = len(msgs) - len(tail)
    total = len(msgs)
    if remaining > 0:
        lines.append(f"─── 以上为最近 {len(tail)} 条（共 {total} 条，前 {remaining} 条已压缩）───")
    else:
        lines.append(f"─── 以上为全部 {total} 条消息 ───")
    return "\n".join(lines)


def _first_text_block(msg) -> str:
    """Return the first text block content, or empty string."""
    for b in msg.blocks:
        if b.type == "text" and b.text:
            if len(b.text) > 3000:
                return b.text[:3000] + "\n\n[...截断]"
            return b.text
    return ""


# ── Internal helpers ────────────────────────────────────────────────


_MODE_LABELS_MAP: dict[str, str] = {
    "immersive": "沉浸",
    "overview": "分析",
    "divergent": "发散",
}


def _mode_label(mode: str | None) -> str:
    return _MODE_LABELS_MAP.get(mode or "", mode or "?")


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def _format_time(ts_ms: int) -> str:
    """Format a millisecond timestamp into a compact relative string.

    Today → HH:MM, yesterday → "昨天", older → MM-DD.
    """
    if not ts_ms:
        return "?"
    dt = datetime.fromtimestamp(ts_ms / 1000)
    now = datetime.now()
    today = now.date()
    if dt.date() == today:
        return dt.strftime("%H:%M")
    diff = (today - dt.date()).days
    if diff == 1:
        return "昨天"
    if dt.year == now.year:
        return dt.strftime("%m-%d")
    return dt.strftime("%Y-%m-%d")
