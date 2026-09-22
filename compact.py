"""Structured context compaction — NO LLM required.

Statistics-based summarization with XML blocks; the summary text itself is
additionally trimmed with a per-character/line budget.

Key features:
- Walks the boundary back to avoid splitting tool_use/tool_result pairs
- Incremental merge on re-compaction (preserves prior summary)
- Summary compression budget (max_chars, max_lines, max_line_chars)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from content import ContentBlock, ConversationMessage
from session import Session, SessionCompaction

COMPACT_CONTINUATION_PREAMBLE = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier portion of the conversation.\n\n"
)
COMPACT_RECENT_MESSAGES_NOTE = "Recent messages are preserved verbatim."
COMPACT_DIRECT_RESUME_INSTRUCTION = (
    "Continue the conversation from where it left off without asking the user any further "
    "questions. Resume directly — do not acknowledge the summary, do not recap what was "
    "happening, and do not preface with continuation text."
)


@dataclass
class CompactionConfig:
    preserve_recent_messages: int = 4
    max_estimated_tokens: int = 10_000


@dataclass
class CompactionResult:
    summary: str
    formatted_summary: str
    compacted_session: Session
    removed_message_count: int
    removed_messages: list[ConversationMessage] = field(default_factory=list)


def should_compact(session: Session, config: CompactionConfig) -> bool:
    """Check if the session exceeds the compaction threshold."""
    prefix_len = _compacted_summary_prefix_len(session)
    compactable = session.messages[prefix_len:]
    if len(compactable) <= config.preserve_recent_messages:
        return False
    estimated = sum(msg.estimate_tokens() for msg in compactable)
    return estimated >= config.max_estimated_tokens


def compact_session(session: Session, config: CompactionConfig) -> CompactionResult:
    """Compact a session: summarize older messages, preserve recent tail."""
    if not should_compact(session, config):
        return CompactionResult(
            summary="",
            formatted_summary="",
            compacted_session=session,
            removed_message_count=0,
        )

    existing_summary = _extract_existing_summary(session)
    prefix_len = 1 if existing_summary else 0

    # Find the keep-from boundary, protecting tool_use/tool_result pairs
    raw_keep_from = max(prefix_len, len(session.messages) - config.preserve_recent_messages)
    keep_from = _adjust_boundary_for_tool_pairs(session.messages, raw_keep_from, prefix_len)

    removed = session.messages[prefix_len:keep_from]
    preserved = session.messages[keep_from:]

    summary = _merge_summaries(existing_summary, _summarize_messages(removed))
    formatted = _format_compact_summary(summary)

    continuation = _build_continuation_message(summary, bool(preserved))

    compacted_messages = [
        ConversationMessage(role="system", blocks=[ContentBlock.text_block(continuation)]),
        *preserved,
    ]

    new_session = Session(
        session_id=session.session_id,
        created_at_ms=session.created_at_ms,
        messages=compacted_messages,
        compaction=SessionCompaction(
            count=(session.compaction.count + 1) if session.compaction else 1,
            removed_message_count=len(removed),
            summary=summary,
        ),
        fork=session.fork,
        model=session.model,
        mode=session.mode,
        prompt_history=list(session.prompt_history),
    )
    if session._persistence_path:
        new_session._persistence_path = session._persistence_path

    return CompactionResult(
        summary=summary,
        formatted_summary=formatted,
        compacted_session=new_session,
        removed_message_count=len(removed),
        removed_messages=removed,
    )


def estimate_session_tokens(session: Session) -> int:
    """Rough character/4 token estimate for a session."""
    return sum(msg.estimate_tokens() for msg in session.messages)


# ── Internal helpers ────────────────────────────────────────────────────


def _compacted_summary_prefix_len(session: Session) -> int:
    """If the first message is a system compaction message, return 1."""
    if session.messages and session.messages[0].role == "system":
        first_text = session.messages[0].first_text()
        if first_text and "session is being continued" in first_text.lower():
            return 1
    return 0


def _adjust_boundary_for_tool_pairs(
    messages: list[ConversationMessage],
    raw_keep_from: int,
    prefix_len: int,
) -> int:
    """Walk the keep-from boundary back to avoid splitting tool_use/tool_result pairs.

    Truncation must never orphan a tool_result: if the first preserved
    message is a tool_result, back the boundary up so its paired assistant
    tool_use message is preserved too.
    """
    k = raw_keep_from
    while k > 0 and k > prefix_len:
        first_preserved = messages[k]
        starts_with_tool_result = (
            first_preserved.blocks
            and first_preserved.blocks[0].type == "tool_result"
        )
        if not starts_with_tool_result:
            break
        preceding = messages[k - 1]
        preceding_has_tool_use = any(
            b.type == "tool_use" for b in preceding.blocks
        )
        if preceding_has_tool_use:
            k -= 1
            break
        k -= 1
    return k


def _summarize_messages(messages: list[ConversationMessage]) -> str:
    """Generate a structured XML summary of removed messages."""
    user_msgs = sum(1 for m in messages if m.role == "user")
    assistant_msgs = sum(1 for m in messages if m.role == "assistant")
    tool_msgs = sum(1 for m in messages if m.role == "tool")

    # Collect tool names
    tool_names: list[str] = []
    for m in messages:
        for b in m.blocks:
            if b.tool_name and b.tool_name not in tool_names:
                tool_names.append(b.tool_name)

    lines = [
        "<summary>",
        "Conversation summary:",
        f"- Scope: {len(messages)} earlier messages compacted (user={user_msgs}, assistant={assistant_msgs}, tool={tool_msgs}).",
    ]

    if tool_names:
        lines.append(f"- Tools mentioned: {', '.join(tool_names)}.")

    # Recent user requests
    user_texts = [
        _truncate(m.first_text() or "", 160)
        for m in messages
        if m.role == "user" and m.first_text()
    ][-3:]
    if user_texts:
        lines.append("- Recent user requests:")
        for t in user_texts:
            lines.append(f"  - {t}")

    # Key timeline (one line per message)
    lines.append("- Key timeline:")
    for m in messages:
        role = m.role
        content = _summarize_blocks(m.blocks)
        lines.append(f"  - {role}: {_truncate(content, 200)}")

    lines.append("</summary>")
    return "\n".join(lines)


def _summarize_blocks(blocks: list[ContentBlock]) -> str:
    parts: list[str] = []
    for b in blocks:
        if b.type == "text" and b.text:
            parts.append(b.text)
        elif b.type == "tool_use":
            parts.append(f"tool_use {b.tool_name}(...)")
        elif b.type == "tool_result":
            status = "error" if b.is_error else ""
            output_preview = _truncate(b.tool_output or "", 80)
            parts.append(f"tool_result {b.tool_name}: {status} {output_preview}")
        elif b.type == "thinking":
            parts.append(f"thinking ({len(b.thinking or '')} chars)")
    return " | ".join(parts)


def _extract_existing_summary(session: Session) -> str | None:
    if not session.messages or session.messages[0].role != "system":
        return None
    text = session.messages[0].first_text()
    if text and "session is being continued" in text.lower():
        # Extract the summary portion between preamble and "Recent messages"
        if "Summary:\n" in text:
            return text.split("Summary:\n", 1)[1].split("Recent messages", 1)[0].strip()
        return text
    return None


def _merge_summaries(existing: str | None, new_summary: str) -> str:
    if not existing:
        return new_summary

    # Strip leading whitespace from existing lines to prevent progressive
    # indentation across repeated compactions.
    stripped = "\n".join(line.lstrip() for line in existing.split("\n"))

    lines = [
        "<summary>",
        "Conversation summary:",
        "- Previously compacted context:",
    ]
    for line in stripped.split("\n"):
        lines.append(f"  {line}")
    lines.append("- Newly compacted context:")
    for line in new_summary.replace("<summary>", "").replace("</summary>", "").strip().split("\n"):
        lines.append(f"  {line}")
    lines.append("</summary>")
    return "\n".join(lines)


def _format_compact_summary(summary: str) -> str:
    """Strip XML tags and format for display."""
    text = summary
    if "<summary>" in text:
        text = text.split("<summary>", 1)[1]
    if "</summary>" in text:
        text = text.split("</summary>", 1)[0]
    return text.strip()


# ── Summary Compression ─────────────────────────────────────────────────

@dataclass
class SummaryCompressionBudget:
    """Budget for compressing the summary text itself."""
    max_chars: int = 1200
    max_lines: int = 24
    max_line_chars: int = 160


def compress_summary(summary: str, budget: SummaryCompressionBudget | None = None) -> str:
    """Compress a structured summary to fit within a character/line budget.

    Uses priority-based line selection:
    1. Scope/statistics lines (always keep)
    2. Section headers (keep if possible)
    3. Timeline entries (trim individual lines)
    4. Other lines (drop if over budget)
    """
    if budget is None:
        budget = SummaryCompressionBudget()

    lines = summary.split("\n")
    if not lines:
        return summary

    # Categorize lines by priority
    scope_lines: list[str] = []
    header_lines: list[str] = []  # Lines starting with "- X:"
    timeline_lines: list[str] = []  # Lines starting with "  -"
    body_lines: list[str] = []  # Everything else

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- Scope:") or stripped.startswith("- Tools mentioned:"):
            scope_lines.append(line)
        elif stripped.startswith("- ") and ":" in stripped.split(":")[0]:
            header_lines.append(line)
        elif stripped.startswith("  - "):
            timeline_lines.append(line)
        else:
            body_lines.append(line)

    # Trim individual lines to max_line_chars
    scope_lines = [_truncate(l, budget.max_line_chars) for l in scope_lines]
    header_lines = [_truncate(l, budget.max_line_chars) for l in header_lines]
    timeline_lines = [_truncate(l, budget.max_line_chars) for l in timeline_lines]

    # Assemble in priority order, respecting max_chars and max_lines
    result_parts: list[str] = []
    char_count = 0
    line_count = 0

    for line in scope_lines:
        if line_count >= budget.max_lines:
            break
        if char_count + len(line) > budget.max_chars:
            result_parts.append("...")
            return "\n".join(result_parts)
        result_parts.append(line)
        char_count += len(line) + 1
        line_count += 1

    for line in header_lines:
        if line_count >= budget.max_lines:
            break
        if char_count + len(line) > budget.max_chars:
            result_parts.append("...")
            return "\n".join(result_parts)
        result_parts.append(line)
        char_count += len(line) + 1
        line_count += 1

    for line in timeline_lines:
        if line_count >= budget.max_lines:
            break
        if char_count + len(line) > budget.max_chars:
            result_parts.append("...")
            return "\n".join(result_parts)
        if char_count + len(line) <= budget.max_chars:
            result_parts.append(line)
            char_count += len(line) + 1
            line_count += 1

    return "\n".join(result_parts)


def compact_and_compress(session: Session, config: CompactionConfig | None = None,
                         summary_budget: SummaryCompressionBudget | None = None) -> CompactionResult:
    """Compact session and then compress the resulting summary."""
    result = compact_session(session, config or CompactionConfig())
    if result.removed_message_count > 0:
        result.summary = compress_summary(result.summary, summary_budget)
        result.formatted_summary = compress_summary(result.formatted_summary, summary_budget)
    return result


def _build_continuation_message(summary: str, has_recent: bool) -> str:
    formatted = _format_compact_summary(summary)
    msg = f"{COMPACT_CONTINUATION_PREAMBLE}Summary:\n{formatted}"
    if has_recent:
        msg += f"\n\n{COMPACT_RECENT_MESSAGES_NOTE}"
    msg += f"\n{COMPACT_DIRECT_RESUME_INSTRUCTION}"
    return msg


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."
