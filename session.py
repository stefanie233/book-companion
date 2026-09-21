"""Session data model — adapted from claw-code crates/runtime/src/session.rs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from content import ContentBlock, ConversationMessage, TokenUsage

SESSION_VERSION = 1
_last_timestamp_ms = 0
_session_counter = 0


def _monotonic_ms() -> int:
    """Monotonic millisecond timestamp — prevents clock-drift ID collisions."""
    global _last_timestamp_ms, _session_counter
    wall = int(time.time() * 1000)
    candidate = max(wall, _last_timestamp_ms + 1)
    _last_timestamp_ms = candidate
    _session_counter += 1
    return candidate


def _generate_session_id() -> str:
    ts = _monotonic_ms()
    global _session_counter
    return f"session-{ts}-{_session_counter:04d}"


@dataclass
class SessionCompaction:
    count: int
    removed_message_count: int
    summary: str


@dataclass
class SessionFork:
    parent_session_id: str
    branch_name: str | None = None


@dataclass
class SessionPromptEntry:
    timestamp_ms: int
    text: str


@dataclass
class Session:
    """Persisted conversational state — delegates persistence to SessionService."""

    session_id: str = field(default_factory=_generate_session_id)
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    messages: list[ConversationMessage] = field(default_factory=list)
    compaction: SessionCompaction | None = None
    fork: SessionFork | None = None
    model: str | None = None
    mode: str = "immersive"
    title: str | None = None
    prompt_history: list[SessionPromptEntry] = field(default_factory=list)
    _service: object | None = None  # SessionService (avoid circular import)
    _last_persisted_count: int = 0  # Tracks how many messages are in the DB

    def touch(self) -> None:
        self.updated_at_ms = _monotonic_ms()

    def push_message(self, message: ConversationMessage) -> None:
        self.touch()
        self.messages.append(message)
        if self._service:
            self._service.append_message(self.session_id, message)
            self._last_persisted_count = len(self.messages)

    def push_user_text(self, text: str) -> None:
        self.prompt_history.append(SessionPromptEntry(
            timestamp_ms=_monotonic_ms(),
            text=text,
        ))
        self.push_message(ConversationMessage.user_text(text))

    def record_compaction(self, summary: str, removed_count: int) -> None:
        self.touch()
        count = (self.compaction.count + 1) if self.compaction else 1
        self.compaction = SessionCompaction(
            count=count, removed_message_count=removed_count, summary=summary,
        )

    def estimate_tokens(self) -> int:
        return sum(msg.estimate_tokens() for msg in self.messages)

    def save(self) -> None:
        """Delegate persistence to SessionService when wired."""
        if self._service:
            self._service.save(self)


# ── Public helpers for SQLite persistence layer ──────────────────────────


def _block_to_dict(block: ContentBlock) -> dict:
    d: dict = {"type": block.type}
    if block.replacement_state != "fresh":
        d["replacement_state"] = block.replacement_state
    if block.type == "text":
        d["text"] = block.text or ""
    elif block.type == "thinking":
        d["thinking"] = block.thinking or ""
        if block.signature:
            d["signature"] = block.signature
    elif block.type == "tool_use":
        d["tool_use_id"] = block.tool_use_id or ""
        d["tool_name"] = block.tool_name or ""
        d["tool_input"] = block.tool_input or ""
    elif block.type == "tool_result":
        d["tool_use_id"] = block.tool_use_id or ""
        d["tool_name"] = block.tool_name or ""
        d["tool_output"] = block.tool_output or ""
        d["is_error"] = block.is_error
    elif block.type in ("voice_input", "voice_output"):
        d["text"] = block.text or ""  # voice metadata stored as JSON in text field
    return d


def _dict_to_block(d: dict) -> ContentBlock:
    """Deserialize a part-data dict back into a ContentBlock.

    voice_input/voice_output store metadata as JSON in the text field —
    handled via the generic text=d.get("text").
    """
    btype = d.get("type", "text")
    return ContentBlock(
        type=btype,
        text=d.get("text"),
        thinking=d.get("thinking"),
        signature=d.get("signature"),
        tool_use_id=d.get("tool_use_id"),
        tool_name=d.get("tool_name"),
        tool_input=d.get("tool_input"),
        tool_output=d.get("tool_output"),
        is_error=d.get("is_error", False),
        replacement_state=d.get("replacement_state", "fresh"),
    )
