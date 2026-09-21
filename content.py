"""Content block types for agent messages.

Adapted from claw-code crates/runtime/src/session.rs ContentBlock enum pattern.
Strongly-typed blocks make compact/serialize/deserialize safe and explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ── Budget constants (Claude Code pattern) ───────────────────────────────

MAX_RESULT_SIZE_CHARS = 50_000          # Single tool result cap
MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000  # Per-turn total cap


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0

    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ContentBlock:
    """A single content block within a message.

    Uses a flat dataclass with optional fields rather than a union/enum
    to keep JSON serialization simple and match the JSONL format.
    """
    type: Literal["text", "thinking", "tool_use", "tool_result", "voice_input", "voice_output"]
    text: str | None = None
    thinking: str | None = None
    signature: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    is_error: bool = False
    replacement_state: Literal["fresh", "frozen", "must_reapply"] = "fresh"

    @classmethod
    def text_block(cls, text: str) -> ContentBlock:
        return cls(type="text", text=text)

    @classmethod
    def thinking_block(cls, thinking: str, signature: str | None = None) -> ContentBlock:
        return cls(type="thinking", thinking=thinking, signature=signature)

    @classmethod
    def tool_use_block(cls, tool_id: str, name: str, input_json: str) -> ContentBlock:
        return cls(type="tool_use", tool_use_id=tool_id, tool_name=name, tool_input=input_json)

    @classmethod
    def tool_result_block(cls, tool_id: str, name: str, output: str, is_error: bool = False,
                          replacement_state: Literal["fresh", "frozen", "must_reapply"] = "fresh") -> ContentBlock:
        return cls(type="tool_result", tool_use_id=tool_id, tool_name=name, tool_output=output,
                   is_error=is_error, replacement_state=replacement_state)

    def estimate_tokens(self) -> int:
        """Rough character/4 approximation for token counting."""
        chars = 0
        for val in [self.text, self.thinking, self.tool_input, self.tool_output]:
            if val:
                chars += len(val)
        if self.tool_name:
            chars += len(self.tool_name)
        return max(1, chars // 4)


@dataclass
class ConversationMessage:
    """One conversation message with optional token-usage metadata."""
    role: Literal["system", "user", "assistant", "tool"]
    blocks: list[ContentBlock] = field(default_factory=list)
    usage: TokenUsage | None = None
    id: str | None = None  # DB message id (set when loaded from DB; used for fork-from-message)
    time_created: int = 0  # DB message timestamp (ms); populated when loading from DB

    @classmethod
    def user_text(cls, text: str) -> ConversationMessage:
        return cls(role="user", blocks=[ContentBlock.text_block(text)])

    @classmethod
    def assistant_text(cls, text: str) -> ConversationMessage:
        return cls(role="assistant", blocks=[ContentBlock.text_block(text)])

    @classmethod
    def system_text(cls, text: str) -> ConversationMessage:
        return cls(role="system", blocks=[ContentBlock.text_block(text)])

    @classmethod
    def tool_result(cls, tool_use_id: str, tool_name: str, output: str, is_error: bool = False) -> ConversationMessage:
        return cls(role="tool", blocks=[ContentBlock.tool_result_block(tool_use_id, tool_name, output, is_error)])

    def estimate_tokens(self) -> int:
        return sum(block.estimate_tokens() for block in self.blocks)

    def first_text(self) -> str | None:
        for block in self.blocks:
            if block.type == "text" and block.text:
                return block.text
        return None

    def has_tool_uses(self) -> bool:
        return any(b.type == "tool_use" for b in self.blocks)

    def tool_uses(self) -> list[tuple[str, str, str]]:
        """Returns list of (tool_use_id, tool_name, tool_input)."""
        return [(b.tool_use_id or "", b.tool_name or "", b.tool_input or "")
                for b in self.blocks if b.type == "tool_use"]
