"""Abstract base class for pluggable memory providers.

Memory providers give the agent persistent recall across sessions.
Adapted from Hermes-Agent's ``agent/memory_provider.py``.

Architecture:
  - Built-in provider (MEMORY.md / USER.md) is always active
  - At most ONE external provider (wingman, honcho, etc.) at a time
  - MemoryManager orchestrates all providers

Lifecycle (called by MemoryManager):
  initialize(session_id, **kwargs) — connect, create resources, warm up
  system_prompt_block()            — static text for the system prompt
  prefetch(query)                  — recall context before each turn
  queue_prefetch(query)            — background recall for the next turn
  sync_turn(user, asst)            — persist after each turn
  get_tool_schemas()               — tool schemas to expose to the model
  handle_tool_call(name, args)     — dispatch a tool call
  shutdown()                       — clean exit

Optional hooks (override to opt in):
  on_turn_start(turn, msg, **kw)   — per-turn tick with runtime context
  on_session_end(messages)         — end-of-session extraction
  on_session_switch(new_id, ...)   — mid-process session_id rotation
  on_pre_compress(messages) -> str — extract before context compression
  on_memory_write(action, target, content, metadata=None) — mirror built-in writes
  on_delegation(task, result, ...) — parent-side observation of subagent work
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'builtin', 'wingman', 'honcho')."""

    # -- Core lifecycle -------------------------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is configured and ready.

        Called during init to decide whether to activate the provider.
        Should not make network calls — just check config and installed deps.
        """

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session. Called once at agent startup.

        kwargs may include:
          - config: The project Config instance
          - session: The Session instance
          - db: Database instance for storage queries
        """

    def system_prompt_block(self) -> str:
        """Return text to include in the system prompt.

        Called during system prompt assembly. Return empty string to skip.
        This is for STATIC provider info. Prefetched recall context is
        injected separately via prefetch().
        """
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Called before each API call. Return formatted text to inject as
        context, or empty string if nothing relevant.
        """
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. The result will be consumed
        by prefetch() on the next turn. Default is no-op.
        """

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        """Persist a completed turn to the backend. Non-blocking."""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas this provider exposes.

        Each schema follows the OpenAI function calling format.
        Return empty list if this provider has no tools (context-only).
        """

    def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **kwargs
    ) -> str:
        """Handle a tool call for one of this provider's tools.

        Must return a JSON string (the tool result).
        Only called for tool names returned by get_tool_schemas().
        """
        raise NotImplementedError(
            f"Provider {self.name} does not handle tool {tool_name}"
        )

    def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""

    # -- Optional hooks -------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn with the user message.

        Use for turn-counting, scope management, periodic maintenance.
        """

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Called when a session ends (explicit exit or timeout).

        NOT called after every turn — only at actual session boundaries.
        """

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        """Called when the agent switches session_id mid-process."""

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Called before context compression discards old messages.

        Return text to include in the compression summary prompt.
        """
        return ""

    def on_delegation(
        self, task: str, result: str, *, child_session_id: str = "", **kwargs
    ) -> None:
        """Called on the PARENT agent when a subagent completes."""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Called when the built-in memory tool writes an entry.

        Use to mirror built-in memory writes to your backend.
        """
