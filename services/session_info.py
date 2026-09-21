"""Lightweight session info — no message content, for display/list."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionInfo:
    """Public summary of a session (no message body)."""
    id: str
    project_id: str = ""
    title: str | None = None
    model: str | None = None
    mode: str = "immersive"
    message_count: int = 0
    time_created: int = 0
    time_updated: int = 0
    parent_id: str | None = None
    agent: str | None = None
    character_name: str = ""
    fork_point_index: int | None = None
