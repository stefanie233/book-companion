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
    #: 可扮演者的种类 —— ``character``（图谱实体）| ``author``（作者层）。
    #: 消费端**必须先看这一列**再解析名字：同一个人名在两个库里都可能有，
    #: 而名字解析不会报错，只会悄悄给你另一个人。
    role_kind: str = "character"
    #: 稳定主键。``author`` 时是 ``author_id``；``character`` 时空着
    #: （那边的键是 ``project_id`` + 正名）。
    role_ref: str = ""
