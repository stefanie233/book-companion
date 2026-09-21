"""Permission system — adapted from claw-code crates/runtime/src/permissions.rs.

Simple mode-based authorization: a tool runs if the active mode is >= the
tool's required mode.  No interactive prompting — this is a local CLI agent.
Defaults to workspace-write.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class PermissionMode(IntEnum):
    """Ordered permission modes (higher = more permissive)."""
    READ_ONLY = 0
    WORKSPACE_WRITE = 1
    DANGER_FULL_ACCESS = 2
    ALLOW = 3

    def as_str(self) -> str:
        return {
            PermissionMode.READ_ONLY: "read-only",
            PermissionMode.WORKSPACE_WRITE: "workspace-write",
            PermissionMode.DANGER_FULL_ACCESS: "danger-full-access",
            PermissionMode.ALLOW: "allow",
        }[self]

    @classmethod
    def from_str(cls, s: str) -> PermissionMode:
        mapping = {
            "read-only": cls.READ_ONLY,
            "workspace-write": cls.WORKSPACE_WRITE,
            "danger-full-access": cls.DANGER_FULL_ACCESS,
            "allow": cls.ALLOW,
        }
        return mapping.get(s, cls.WORKSPACE_WRITE)


class PermissionOutcome:
    """Result of a permission check."""
    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

    def __bool__(self) -> bool:
        return self.allowed

    @classmethod
    def allow(cls) -> PermissionOutcome:
        return cls(True)

    @classmethod
    def deny(cls, reason: str = "") -> PermissionOutcome:
        return cls(False, reason)


class PermissionPolicy:
    """Evaluates permission for tool invocations.

    Authorization order:
    1. Check deny rules → hard block
    2. Check allow rules → auto-approve
    3. Check active_mode >= tool's required_mode
    """

    def __init__(self, active_mode: PermissionMode = PermissionMode.WORKSPACE_WRITE):
        self.active_mode = active_mode
        self._tool_requirements: dict[str, PermissionMode] = {}
        self._deny_rules: list[str] = []
        self._allow_rules: list[str] = []

    def with_tool_requirement(self, tool_name: str, mode: PermissionMode) -> PermissionPolicy:
        self._tool_requirements[tool_name] = mode
        return self

    def with_rules(self, allow: list[str] | None = None,
                   deny: list[str] | None = None) -> PermissionPolicy:
        if allow:
            self._allow_rules = allow
        if deny:
            self._deny_rules = deny
        return self

    def authorize(self, tool_name: str, tool_input: str = "") -> PermissionOutcome:
        # 1. Check deny rules
        if self._matches_rule(tool_name, self._deny_rules):
            return PermissionOutcome.deny(f"Tool '{tool_name}' is denied by policy")

        # 2. Check allow rules
        if self._matches_rule(tool_name, self._allow_rules):
            return PermissionOutcome.allow()

        # 3. Allow mode bypasses everything
        if self.active_mode >= PermissionMode.ALLOW:
            return PermissionOutcome.allow()

        # 4. Check if tool has a required mode
        required = self._tool_requirements.get(tool_name, PermissionMode.WORKSPACE_WRITE)
        if self.active_mode >= required:
            return PermissionOutcome.allow()

        return PermissionOutcome.deny(
            f"Tool '{tool_name}' requires {required.as_str()}, current mode is {self.active_mode.as_str()}"
        )

    def set_mode(self, mode: PermissionMode) -> None:
        self.active_mode = mode

    @staticmethod
    def _matches_rule(tool_name: str, rules: list[str]) -> bool:
        for rule in rules:
            if rule == tool_name:
                return True
            if rule.endswith(":*") and tool_name.startswith(rule[:-2]):
                return True
        return False
