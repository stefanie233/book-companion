"""Tool registry.

String-in/string-out tool contract. Each tool has a JSON Schema for LLM function calling.
Built-in tools span 7 toolsets: file_reading, library_query, library_edit, knowledge, utility, file_ops, agent.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncGenerator, Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from enum import Enum
from pathlib import Path
from permissions import PermissionMode


# ── ToolExposure — controls how tools are surfaced to the LLM ────────────


class ToolExposure(Enum):
    """Four-tier tool exposure model.

    DIRECT            — always visible, included in sub-agents
    DEFERRED          — hidden, discoverable via tool_search, in sub-agents
    DIRECT_MODEL_ONLY — visible to main LLM, excluded from sub-agents
    HIDDEN            — never told to LLM, code-internal dispatch only
    """
    DIRECT = "direct"
    DEFERRED = "deferred"
    DIRECT_MODEL_ONLY = "direct_model_only"
    HIDDEN = "hidden"


# ── Toolset labels for render_usage_guide ────────────────────────────────

TOOLSET_LABELS: dict[str, str] = {
    "file_reading": "直接文件阅读 — 读取 EPUB/MOBI/TXT 原始文件的内容与元数据",
    "library_query": "书籍库查询 — 浏览、搜索、阅读已导入书籍，管理版本历史",
    "library_edit":  "书籍库管理 — 导入书籍、拆分章节、编辑元数据、删除与回滚",
    "knowledge":     "记忆与知识 — 搜索/写入个人记忆、网络搜索、查看历史会话",
    "utility":       "实用工具 — 时间、天气、搜索更多工具",
    "agent":         "代理工具 — 创建子代理处理并行任务",
    "file_ops":      "文件操作 — 直接读写文件系统（仅限必要时使用）",
}

TOOLSET_ORDER: list[str] = [
    "file_reading", "library_query", "knowledge",
    "utility", "library_edit", "file_ops", "agent",
]


@dataclass
class ToolSpec:
    """Immutable tool definition with JSON Schema and execution handler."""
    name: str
    description: str
    parameters: dict  # JSON Schema for function calling
    execute: Callable[..., str]  # Actual implementation (sync fallback)
    permission_mode: PermissionMode = PermissionMode.WORKSPACE_WRITE
    execute_async: Callable[..., Awaitable[str]] | None = None  # Async version for network tools
    execute_stream: Callable[..., AsyncGenerator[str, None]] | None = None  # Streaming generator
    toolset: str = ""  # Group tag for toolset-based filtering (sub-agents, mode switching)
    exposure: ToolExposure = ToolExposure.DIRECT  # Controls LLM visibility (default DIRECT for backward compat)
    category: str = ""  # Semantic category for tool_search discovery (session, memory, book, etc.)


class ToolRegistry:
    """Registry of available tools. String-in/string-out contract."""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}
        self._workspace_root: Path = Path.cwd()
        # Per-request mutable context for role-play tools (asyncio — single-threaded)
        # Per-request mutable context for reading/graph tools (asyncio).
        # Populated for ALL modes (book_id / graph_db_path / max_chapter / db),
        # not just role-play - so graph_search / graph_node work in companion
        # reading modes too. Role-play adds character_name on top.
        self.reading_ctx: dict[str, Any] = {}

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @workspace_root.setter
    def workspace_root(self, value: Path) -> None:
        self._workspace_root = value

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def unregister(self, name: str) -> bool:
        """Remove a tool from the registry. Returns True if it existed."""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def execute(self, name: str, input_json: str) -> str:
        """Execute a tool by name with JSON-encoded arguments (sync)."""
        spec = self._tools.get(name)
        if not spec:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            args = json.loads(input_json) if input_json.strip() else {}
        except json.JSONDecodeError:
            return json.dumps({"error": f"Invalid JSON input for tool '{name}'"})
        try:
            return spec.execute(**args)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def execute_async(self, name: str, input_json: str) -> str:
        """Execute a tool (async path). Prefers execute_async, falls back to execute."""
        spec = self._tools.get(name)
        if not spec:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            args = json.loads(input_json) if input_json.strip() else {}
        except json.JSONDecodeError:
            return json.dumps({"error": f"Invalid JSON input for tool '{name}'"})
        try:
            if spec.execute_async:
                return await spec.execute_async(**args)
            return spec.execute(**args)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def get_schemas(self) -> list[dict]:
        """Return OpenAI-compatible function definitions for visible tools only."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self.get_visible_specs()
        ]

    async def execute_async(self, name: str, input_json: str) -> str:
        """Execute a tool asynchronously — prefer async version, fallback to sync."""
        spec = self._tools.get(name)
        if not spec:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            args = json.loads(input_json) if input_json.strip() else {}
        except json.JSONDecodeError:
            return json.dumps({"error": f"Invalid JSON input for tool '{name}'"})
        try:
            if spec.execute_async is not None:
                return await spec.execute_async(**args)
            return spec.execute(**args)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def get_permission_requirements(self) -> dict[str, PermissionMode]:
        """Return mapping of tool_name → required permission mode."""
        return {name: spec.permission_mode for name, spec in self._tools.items()}

    def get_tool_names(self) -> list[str]:
        """Return names of all registered tools."""
        return list(self._tools.keys())

    def get_tool_names_by_toolset(self, toolsets: list[str]) -> list[str]:
        """Return names of tools belonging to any of the given toolsets."""
        return [
            name for name, spec in self._tools.items()
            if spec.toolset in toolsets
        ]

    def get_registry_for_toolsets(
        self,
        toolsets: list[str] | None = None,
        exclude_tools: set[str] | None = None,
        exclude_exposure: list[ToolExposure] | None = None,
    ) -> "ToolRegistry":
        """Return a new ToolRegistry filtered by toolset, exclusions, and exposure.

        Single unified pipeline for both main agent and sub-agents:
        - ``toolsets=None`` → all tools (subject to ``exclude_tools``)
        - ``toolsets=[...]`` → only tools in those groups
        - ``exclude_tools`` → always excluded regardless of toolset
        - ``exclude_exposure`` → exclude tools with these exposure levels
          (e.g. ``[ToolExposure.DIRECT_MODEL_ONLY, ToolExposure.HIDDEN]`` for sub-agents)
        """
        exclude = exclude_tools or set()
        exclude_exp = exclude_exposure or []
        new_registry = ToolRegistry()
        new_registry._workspace_root = self._workspace_root
        for name, spec in self._tools.items():
            if name in exclude:
                continue
            if toolsets is not None and spec.toolset not in toolsets:
                continue
            if exclude_exp and spec.exposure in exclude_exp:
                continue
            new_registry.register(spec)
        return new_registry

    def get_visible_specs(self) -> list[ToolSpec]:
        """Return tools the model sees directly (DIRECT + DIRECT_MODEL_ONLY)."""
        return [t for t in self._tools.values()
                if t.exposure in (ToolExposure.DIRECT, ToolExposure.DIRECT_MODEL_ONLY)]

    def get_deferred_specs(self) -> list[ToolSpec]:
        """Return tools discoverable only via tool_search (DEFERRED)."""
        return [t for t in self._tools.values()
                if t.exposure == ToolExposure.DEFERRED]

    def get_dispatch_specs(self) -> list[ToolSpec]:
        """Return all executable tools (excludes HIDDEN from dispatch view)."""
        return [t for t in self._tools.values()
                if t.exposure != ToolExposure.HIDDEN]

    def render_usage_guide(self) -> str:
        """Render an LLM-friendly tool category directory (Layer 1 + Layer 2 only).

        Groups visible tools by category, showing one-line summaries.
        Full parameter schemas are in the API tools parameter — the LLM
        only needs the category + name + brief to decide which tool to call.
        """
        specs_by_toolset: dict[str, list[ToolSpec]] = {}
        for spec in self.get_visible_specs():
            specs_by_toolset.setdefault(spec.toolset, []).append(spec)

        parts: list[str] = ["# Available Tools", ""]

        def _brief(desc: str) -> str:
            """Extract first sentence — handles both CN (。) and EN (. ) delimiters."""
            # Try Chinese period first
            pos = desc.find("。")
            if pos > 0:
                return desc[:pos].strip()
            # Try English period + space (sentence boundary)
            pos = desc.find(". ")
            if pos > 10:  # minimum length for a real sentence
                return desc[:pos + 1].strip()
            # Fallback: first line or full text
            return desc.split("\n")[0].strip()

        # Render in defined order
        for ts in TOOLSET_ORDER:
            tools = specs_by_toolset.get(ts, [])
            if not tools:
                continue
            label = TOOLSET_LABELS.get(ts, ts)
            parts.append(f"## {label}")
            for tool in tools:
                parts.append(f"- `{tool.name}`: {_brief(tool.description)}")
            parts.append("")

        # Handle any visible toolsets not in TOOLSET_ORDER
        seen = set(TOOLSET_ORDER)
        for ts in sorted(specs_by_toolset.keys()):
            if ts in seen:
                continue
            tools = specs_by_toolset[ts]
            label = TOOLSET_LABELS.get(ts, ts)
            parts.append(f"## {label}")
            for tool in tools:
                parts.append(f"- `{tool.name}`: {_brief(tool.description)}")
            parts.append("")

        return "\n".join(parts)
