"""Tool registry — adapted from claw-code crates/tools/src/lib.rs pattern.

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
    """Four-tier tool exposure model (based on OpenAI Codex pattern).

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
    """Registry of available tools. String-in/string-out contract (claw-code pattern)."""

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

# ── Built-in tool implementations ──────────────────────────────────────


def _tool_read_file(path: str) -> str:
    """Read contents of a text file."""
    p = Path(path)
    if not p.exists():
        return json.dumps({"error": f"File not found: {path}"})
    try:
        content = p.read_text(encoding="utf-8")
        if len(content) > 50000:
            content = content[:50000] + f"\n\n[truncated — {len(content) - 50000} more chars]"
        return content
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# File extensions that the agent must never create or modify.
# The agent is a reading companion, not a code editor.
_BLOCKED_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".bat", ".sh", ".ps1", ".cmd", ".vbs",
    ".js", ".ts", ".rb", ".go", ".rs", ".java", ".c", ".cpp",
})


def _tool_write_file(path: str, content: str) -> str:
    """Write content to a text file.

    Blocked: script/code files (the agent is a reading companion, not a developer).
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in _BLOCKED_EXTENSIONS:
        return json.dumps({
            "error": (
                f"Cannot write {suffix} files. "
                "This agent is a reading companion, not a code editor. "
                "If you need to search or analyse data, use the available tools "
                "(search_sessions, list_sessions, memory_search, etc.)."
            ),
        })
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} chars to {path}"
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _tool_get_time(format: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Get current date and time."""
    return time.strftime(format)


def _tool_list_directory(path: str = ".") -> str:
    """List files and directories at a given path."""
    try:
        p = Path(path)
        if not p.exists():
            return json.dumps({"error": f"Directory not found: {path}"})
        entries = []
        for entry in sorted(p.iterdir()):
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"  {entry.name}{suffix}")
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _format_weather_response(data: dict, city: str) -> str:
    """Parse wttr.in JSON response into a human-readable weather string."""
    try:
        current = data["current_condition"][0]
    except (KeyError, IndexError, TypeError):
        return json.dumps({"error": f"Could not parse weather data for {city}"})

    temp_c = current.get("temp_C", "?")
    feels_like = current.get("FeelsLikeC", "?")
    humidity = current.get("humidity", "?")
    desc = current.get("weatherDesc", [{}])[0].get("value", "?")
    wind_speed = current.get("windspeedKmph", "?")
    wind_dir = current.get("winddir16Point", "?")
    visibility = current.get("visibility", "?")

    nearest = data.get("nearest_area", [{}])[0]
    area_name = nearest.get("areaName", [{}])[0].get("value", city)
    country = nearest.get("country", [{}])[0].get("value", "")

    return "\n".join([
        f"Current weather for {area_name}, {country}:",
        f"  Condition: {desc}",
        f"  Temperature: {temp_c}°C (feels like {feels_like}°C)",
        f"  Humidity: {humidity}%",
        f"  Wind: {wind_speed} km/h {wind_dir}",
        f"  Visibility: {visibility} km",
    ])


def _tool_get_weather(city: str = "Beijing") -> str:
    """Get current weather for a city using wttr.in (free, no API key)."""
    try:
        import urllib.request
        import urllib.parse
    except ImportError:
        return json.dumps({"error": "urllib not available"})

    encoded = urllib.parse.quote(city)
    url = f"https://wttr.in/{encoded}?format=j1"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "book-companion/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        return json.dumps({"error": f"Weather request failed: {exc}"})

    # wttr.in occasionally returns plain text (rate limiting, service issues).
    # Try JSON first, fall back to returning the raw text as-is.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()

    return _format_weather_response(data, city)


async def _tool_get_weather_async(city: str = "Beijing") -> str:
    """Async weather lookup using aiohttp."""
    import aiohttp
    import urllib.parse

    encoded = urllib.parse.quote(city)
    url = f"https://wttr.in/{encoded}?format=j1"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                raw = await resp.text()
    except Exception as exc:
        return json.dumps({"error": f"Weather request failed: {exc}"})

    # wttr.in occasionally returns plain text (rate limiting, service issues).
    # Try JSON first, fall back to returning the raw text as-is.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()

    return _format_weather_response(data, city)


def _format_search_response(data: dict, max_results: int) -> str:
    """Parse Tavily API response into a human-readable search result string."""
    lines: list[str] = []
    if data.get("answer"):
        lines.append(f"[AI Answer] {data['answer']}")
        lines.append("")

    results = data.get("results", [])[:max_results]
    if results:
        lines.append(f"--- {len(results)} sources ---")
        lines.append("")
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            content = r.get("content", "")
            lines.append(f"[{i}] {title}")
            lines.append(f"    URL: {url}")
            if content:
                lines.append(f"    {content[:300]}")
            lines.append("")
    elif not data.get("answer"):
        lines.append("(no results found)")

    return "\n".join(lines)


def _tool_web_search(query: str, max_results: int = 5, deep_search: bool = False,
                     topic: str = "general") -> str:
    """Search the web using Tavily API (AI-optimized)."""
    try:
        import urllib.request
    except ImportError:
        return json.dumps({"error": "urllib not available"})

    search_api_key = os.environ.get("BOOK_COMPANION_SEARCH_API_KEY", "")
    if not search_api_key:
        return json.dumps({"error": "Search API key not configured. Set BOOK_COMPANION_SEARCH_API_KEY in .env"})

    body = json.dumps({
        "api_key": search_api_key,
        "query": query,
        "search_depth": "advanced" if deep_search else "basic",
        "topic": topic,
        "max_results": max(1, min(int(max_results), 20)),
        "include_answer": True,
        "include_raw_content": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=body,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return json.dumps({"error": f"Search request failed: {exc}"})

    return _format_search_response(data, max_results)


async def _tool_web_search_async(query: str, max_results: int = 5, deep_search: bool = False,
                                 topic: str = "general") -> str:
    """Async web search using aiohttp."""
    import aiohttp

    search_api_key = os.environ.get("BOOK_COMPANION_SEARCH_API_KEY", "")
    if not search_api_key:
        return json.dumps({"error": "Search API key not configured. Set BOOK_COMPANION_SEARCH_API_KEY in .env"})

    body = json.dumps({
        "api_key": search_api_key,
        "query": query,
        "search_depth": "advanced" if deep_search else "basic",
        "topic": topic,
        "max_results": max(1, min(int(max_results), 20)),
        "include_answer": True,
        "include_raw_content": False,
    })

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.tavily.com/search",
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
    except Exception as exc:
        return json.dumps({"error": f"Search request failed: {exc}"})

    return _format_search_response(data, max_results)


# ── Book reader tools ──────────────────────────────────────────────────────


def _tool_book_info(path: str) -> str:
    """Get book metadata and table of contents."""
    try:
        from file_readers import open_book
        reader = open_book(path)
        try:
            return reader.get_info()
        finally:
            if hasattr(reader, 'close'):
                reader.close()
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return json.dumps({"error": f"Failed to read book: {exc}"})


def _tool_read_book(path: str, chapter_index: int = 0, max_chars: int = 8000) -> str:
    """Read a chapter from a book."""
    try:
        from file_readers import open_book
        reader = open_book(path)
        try:
            return reader.read_chapter(index=chapter_index, max_chars=max_chars)
        finally:
            if hasattr(reader, 'close'):
                reader.close()
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return json.dumps({"error": f"Failed to read book: {exc}"})


def _tool_list_books(directory: str = "") -> str:
    """Scan a directory for EPUB/MOBI files."""
    scan_path = Path(directory) if directory else Path.cwd()
    if not scan_path.exists():
        return json.dumps({"error": f"Directory not found: {directory}"})

    epubs = sorted(scan_path.rglob("*.epub"))
    mobis = sorted(scan_path.rglob("*.mobi"))
    all_books = sorted(epubs + mobis, key=lambda p: p.name.lower())

    if not all_books:
        return f"(no EPUB/MOBI files found in {scan_path})"

    lines = [f"📚 Found {len(all_books)} books in {scan_path}", ""]
    for i, book in enumerate(all_books, 1):
        ext = book.suffix.lstrip(".").upper()
        size = book.stat().st_size
        size_str = f"{size / 1024:.0f} KB" if size < 1024**2 else f"{size / 1024**2:.1f} MB"
        lines.append(f"  {i:3d}. [{ext}] {book.name}  ({size_str})")
        # Show relative dir if nested
        rel = book.parent.relative_to(scan_path)
        if str(rel) != ".":
            lines.append(f"       📁 {rel}")
    return "\n".join(lines)


def _tool_book_search(path: str, query: str, max_results: int = 10) -> str:
    """Search inside a book for a keyword or phrase."""
    try:
        from file_readers import open_book
        reader = open_book(path)
        try:
            return reader.search(query, max_results=max_results)
        finally:
            if hasattr(reader, "close"):
                reader.close()
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return json.dumps({"error": f"Failed to search book: {exc}"})


def _register_book_tools(registry: ToolRegistry) -> None:
    """Register book-related tools."""
    registry.register(ToolSpec(
        name="book_info",
        category="book",
        toolset="file_reading",
        description=(
            "Get metadata and table of contents for an EPUB or MOBI file. "
            "Returns title, author, and full chapter list. "
            "Strongly prefer import_book + library_read for actual reading — "
            "library reads use pre-chunked ~800-char segments which are easier "
            "for the model to digest. Use this only to inspect unimported files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub or .mobi file",
                },
            },
            "required": ["path"],
        },
        execute=_tool_book_info,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="read_book",
        category="book",
        toolset="file_reading",
        description=(
            "Read content from a book file (EPUB/MOBI) chapter by chapter. "
            "Specify chapter_index (0-based) to read a specific chapter. "
            "Use max_chars to limit output length (default 8000, 0 = unlimited). "
            "Content beyond max_chars is truncated. "
            "NOTE: prefer library_read for books already imported — it reads "
            "pre-chunked ~800-char segments that are easier for the model to "
            "digest. Use this only for books not yet in the library."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub or .mobi file",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index to read (default: 0)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default: 8000, 0 = unlimited)",
                },
            },
            "required": ["path"],
        },
        execute=_tool_read_book,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="list_books",
        category="book",
        toolset="file_reading",
        description=(
            "Scan a directory for EPUB and MOBI book files. "
            "Returns a numbered list of books with file sizes. "
            "If no directory is specified, scans the current working directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory to scan (default: current directory)",
                },
            },
        },
        execute=_tool_list_books,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="book_search",
        category="book",
        toolset="file_reading",
        description=(
            "Full-text search inside an EPUB book. "
            "Search for keywords, character names, or phrases across all chapters. "
            "Returns excerpted passages with chapter context. "
            "Use this to find where a character or topic appears, "
            "or to locate a specific passage the reader is referencing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub file",
                },
                "query": {
                    "type": "string",
                    "description": "Search keyword or phrase",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 10)",
                },
            },
            "required": ["path", "query"],
        },
        execute=_tool_book_search,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    return registry


def _register_library_tools(registry: ToolRegistry, db: Any, api_client: Any = None) -> None:
    """Register library management tools (import, list, read, search).

    These tools operate on the SQLite book library rather than raw files.
    ``db`` is the Database instance for book CRUD.  ``api_client`` is only
    needed by the LLM-backed enrichment tool.
    """
    from services.library_service import import_book, clean_book, split_book_chapters, format_import_summary
    from services.library_service import search_library as svc_search
    from services.library_service import edit_book, edit_chapter, remove_chapter, manage_categories, delete_book
    from services.library_service import replace_in_chapter
    from services.library_service import list_versions, rollback_chapter
    from services.epub_import_service import extract_epub_skeleton, import_epub_with_split

    def _tool_import_book(path: str, category_id: str = "") -> str:
        """Import an EPUB/MOBI book into the library."""
        try:
            cat = category_id if category_id else None
            book, is_new = import_book(db, path, category_id=cat)
            prefix = "" if is_new else "(already imported — skipped)\n"
            return prefix + format_import_summary(book)
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {path}"})
        except Exception as exc:
            return json.dumps({"error": f"Import failed: {exc}"})

    def _tool_library_list(status: str = "") -> str:
        """List all imported books."""
        try:
            filt = status if status else None
            books = db.list_books(status=filt)
            if not books:
                return "(no books in library — use import_book to add one)"
            lines = [f"📚 Library ({len(books)} books)", ""]
            for b in books:
                status_icon = "✓" if b.status == "clean" else "○"
                # Include chunk count if available
                try:
                    chunk_count = db.get_chunk_count(b.id)
                except Exception:
                    chunk_count = 0
                chunk_info = f", {chunk_count:,} chunks" if chunk_count else ""
                lines.append(
                    f"  {status_icon} [{b.format.upper()}] {b.title}  "
                    f"({b.chapter_count} ch{chunk_info}, {b.total_chars:,} chars)"
                )
                lines.append(f"       id: {b.id}  status: {b.status}")
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"List failed: {exc}"})

    def _tool_library_read(book_id: str, chapter_index: int = 0,
                           chunk_index: int = 0, max_chars: int = 0) -> str:
        """Read a chunk from an imported book chapter stored in the database.

        Each chapter is divided into chunks of ~800 characters. Use
        chunk_index to advance through the chapter (0-based). Omit
        max_chars to read the entire chunk; the chunk is pre-sized.
        """
        try:
            # Try chunk-based read first
            chunk = db.get_chunk(book_id, chapter_index, chunk_index)
            if chunk is not None:
                text = chunk.content
                if max_chars > 0 and len(text) > max_chars:
                    text = text[:max_chars] + "\n… (truncated)"
                total = db.get_chunk_count(book_id, chapter_index)
                ch = db.get_chapter(book_id, chapter_index)
                title = ch.title if ch else f"Chapter {chapter_index + 1}"
                header = f"## {title}  [{chunk_index + 1}/{total}]\n\n"
                return header + text

            # Fallback: old books without chunks — read chapter.content
            ch = db.get_chapter(book_id, chapter_index)
            if ch is None:
                return f"(chapter {chapter_index} not found for book {book_id})"
            text = ch.content
            if max_chars > 0 and len(text) > max_chars:
                cutoff = text[:max_chars]
                last_break = cutoff.rfind("\n\n")
                if last_break > max_chars // 2:
                    text = cutoff[:last_break + 1] + "\n… (truncated)"
                else:
                    text = cutoff + "\n… (truncated)"
            header = f"## {ch.title}\n\n" if ch.title else ""
            return header + text
        except Exception as exc:
            return json.dumps({"error": f"Read failed: {exc}"})

    def _tool_library_search(query: str, max_results: int = 20) -> str:
        """Full-text search across all imported books."""
        try:
            results = svc_search(db, query, limit=max_results)
            if not results:
                return f"(no matches for '{query}')"
            lines = [f"Search results for '{query}' ({len(results)} matches)", ""]
            for r in results:
                lines.append(
                    f"[{r['book_title']} — {r['chapter_title'] or f'Ch.{r['chapter_index']+1}'}]"
                )
                lines.append(f"  …{r['snippet']}…")
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"Search failed: {exc}"})

    registry.register(ToolSpec(
        name="import_book",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Import an EPUB or MOBI book into the library database. "
            "Parses all chapters, cleans the content, and stores them for "
            "fast future access. Returns a summary with book_id for use "
            "with library_read and library_search. "
            "After import, the book-cleaner skill automatically runs to "
            "remove headers, page numbers, and other artifacts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub or .mobi file",
                },
                "category_id": {
                    "type": "string",
                    "description": "Optional category ID for organizing books",
                },
            },
            "required": ["path"],
        },
        execute=_tool_import_book,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── epub_import ──────────────────────────────────────────────────

    def _tool_epub_import(path: str, method: str = "", selector: str = "",
                           category_id: str = "", book_id: str = "",
                           chapter_types: str = "",
                           metadata_title: str = "", metadata_author: str = "",
                           metadata_nationality: str = "") -> str:
        """LLM-assisted EPUB import with smart chapter boundary detection.

        Two-step workflow built into the tool description — the agent
        reads the description and follows the recipe.
        """
        import json as _json

        # Step 1: skeleton extraction (no method provided)
        if not method:
            try:
                result = extract_epub_skeleton(path)
                if "error" in result:
                    return _json.dumps({"error": result["error"]})
                return _json.dumps({
                    "step": 1,
                    "title": result["title"],
                    "author": result["author"],
                    "filename": result["filename"],
                    "spine_count": result["spine_count"],
                    "toc": result["toc"],
                    "skeleton": result["skeleton"],
                    "source_spine_index": result["source_spine_index"],
                    "structural": result.get("structural", {}),
                    "candidates": result.get("candidates", []),
                    "instructions": (
                        "Analyse the HTML skeleton above. Identify the heading "
                        "pattern used for chapter/section titles. Then call "
                        "epub_import again with:\n"
                        "  method='h_tags' (if headings use <h1>-<h6> tags)\n"
                        "  method='font_size' + selector='N' (if headings use "
                        "<font size='N'><b> pattern)\n"
                        "  method='css_class' + selector='classname' (if headings "
                        "use CSS classes like 'chaptertitle')\n"
                        "For font_size, the selector is the font size number "
                        "(e.g. '5'). For css_class, the selector is the class "
                        "name value.\n\n"
                        "OPTIONAL CHAPTER CLASSIFICATION: examine the 'candidates' "
                        "list (front/back suspicious spine items with label + "
                        "~200-char preview). Decide each one's chapter_type from "
                        "the 10 allowed values: cover, copyright, toc, preface, "
                        "body, postscript, appendix, bibliography, index, notes. "
                        "On the Step 2 call, pass chapter_types as a JSON string "
                        "mapping spine_index to type, e.g. "
                        "chapter_types='{\"0\":\"preface\",\"4\":\"postscript\"}'. "
                        "Only include entries you are confident about — omit the "
                        "rest to fall back to automatic detection.\n\n"
                        "OPTIONAL METADATA CLEANUP: use the 'filename' above "
                        "together with 'title' and 'author'. Strip marketing "
                        "noise (e.g. '作家榜经典', '某某推荐', '完整版') from the "
                        "title and any 著/编/译 suffixes from the author name; "
                        "keep the book's real title verbatim otherwise. "
                        "Determine the author's nationality in standard Chinese "
                        "(e.g. '法国', '美国', '日本'). Only fill what you are "
                        "sure about — leave nationality empty otherwise, never "
                        "guess. On the Step 2 call pass metadata_title='…', "
                        "metadata_author='…', metadata_nationality='…' to "
                        "override the OPF values. Omit any field you are unsure "
                        "about to keep the OPF value."
                    ),
                }, ensure_ascii=False)
            except FileNotFoundError:
                return _json.dumps({"error": f"File not found: {path}"})
            except Exception as exc:
                return _json.dumps({"error": f"Skeleton extraction failed: {exc}"})

        # Step 2: import with splitting
        try:
            if method not in ("h_tags", "font_size", "css_class"):
                return _json.dumps({
                    "error": (
                        f"Unknown method: {method}. "
                        "Must be 'h_tags', 'font_size', or 'css_class'."
                    ),
                })
            if method in ("font_size", "css_class") and not selector:
                return _json.dumps({
                    "error": (
                        f"Method '{method}' requires a selector. "
                        "For font_size, pass the font size number (e.g. '5'). "
                        "For css_class, pass the class name (e.g. 'chaptertitle-c')."
                    ),
                })

            cat = category_id if category_id else None
            bid = book_id if book_id else None
            book, is_new = import_epub_with_split(
                path, method, selector, db,
                category_id=cat, book_id=bid, chapter_types=chapter_types,
                metadata_title=metadata_title, metadata_author=metadata_author,
                metadata_nationality=metadata_nationality,
            )
            prefix = "(re-imported — overwritten)\n" if bid else ""
            if not is_new and not bid:
                prefix = "(already imported — skipped)\n"
            return prefix + format_import_summary(book)
        except FileNotFoundError:
            return _json.dumps({"error": f"File not found: {path}"})
        except Exception as exc:
            return _json.dumps({"error": f"EPUB import failed: {exc}"})

    registry.register(ToolSpec(
        name="epub_import",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DIRECT,
        description=(
            "Import an EPUB book with intelligent chapter boundary detection. "
            "TWO-STEP WORKFLOW:\n"
            "  Step 1: call with path only → returns structural summary + HTML "
            "skeleton showing the book's heading structure (tags, CSS classes, "
            "font sizes). Analyse this skeleton to identify the heading pattern.\n"
            "  Step 2: call again with path + method + selector → mechanically "
            "splits every spine file at detected heading boundaries and imports "
            "to the library.\n"
            "  Step 2b (optional): pass chapter_types='{\"0\":\"preface\","
            "\"4\":\"postscript\"}' to classify front/back matter (see Step 1 "
            "'candidates'). Omit to let automatic detection (EPUB structure "
            "signals + label heuristics) decide.\n"
            "  Step 2c (optional): pass metadata_title/metadata_author/"
            "metadata_nationality to clean marketing noise from the OPF "
            "title/author (see Step 1 'filename').\n\n"
            "METHODS (choose one based on skeleton analysis):\n"
            "  'h_tags'   — split at <h1>-<h6> boundaries (no selector needed)\n"
            "  'font_size' — split at <font size='N'><b>…</b></font> headings "
            "(selector = font size number, e.g. '5')\n"
            "  'css_class' — split at elements with a specific CSS class "
            "(selector = class name, e.g. 'chaptertitle-c')\n\n"
            "CACHING: once imported, the method+selector are saved per book. "
            "Re-imports skip Step 1 and use the cached settings. "
            "Use book_id= to force-reimport with the same method.\n\n"
            "FALLBACK: the original import_book tool is still available for "
            "EPUBs that already have correct chapter splits in the NCX/spine."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub file.",
                },
                "method": {
                    "type": "string",
                    "enum": ["", "h_tags", "font_size", "css_class"],
                    "description": (
                        "Heading detection method. Omit for Step 1 (skeleton "
                        "extraction). Required for Step 2 (import)."
                    ),
                },
                "selector": {
                    "type": "string",
                    "description": (
                        "Method-specific selector: font size number for "
                        "'font_size' method, CSS class name for 'css_class' "
                        "method. Not needed for 'h_tags'."
                    ),
                },
                "category_id": {
                    "type": "string",
                    "description": "Optional category ID for organising books.",
                },
                "book_id": {
                    "type": "string",
                    "description": (
                        "Optional book_id for re-import (overwrites existing "
                        "chapters). Omit for first-time import."
                    ),
                },
                "chapter_types": {
                    "type": "string",
                    "description": (
                        "Optional JSON string mapping spine_index to "
                        "chapter_type, e.g. '{\"0\":\"preface\",\"4\":"
                        "\"postscript\"}'. Valid values: cover, copyright, toc, "
                        "preface, body, postscript, appendix, bibliography, "
                        "index, notes. See Step 1 'candidates' for the spine "
                        "indices to classify. Omit to fall back to automatic "
                        "detection."
                    ),
                },
                "metadata_title": {
                    "type": "string",
                    "description": (
                        "Optional cleaned book title from Step 1 metadata "
                        "cleanup; overrides the OPF title."
                    ),
                },
                "metadata_author": {
                    "type": "string",
                    "description": (
                        "Optional cleaned author name (no 著/编/译 suffixes); "
                        "overrides the OPF creator."
                    ),
                },
                "metadata_nationality": {
                    "type": "string",
                    "description": (
                        "Optional author nationality in standard Chinese "
                        "(e.g. '法国'); empty to skip."
                    ),
                },
            },
            "required": ["path"],
        },
        execute=_tool_epub_import,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── Book metadata enrichment (one-shot LLM cleanup) ─────────────────
    _enrich_in_flight: set[str] = set()

    def _fmt_change(before: str, after: str, limit: int = 60) -> str:
        """'旧 → 新' with truncation, or just the unchanged value."""
        if after and after != before:
            return f"{before[:limit]} → {after[:limit]}"
        return before[:limit]

    async def _tool_enrich_metadata(book_id: str = "", all_books: bool = False) -> str:
        """LLM metadata cleanup for one book or the whole library."""
        if not api_client:
            return json.dumps(
                {"error": "Metadata enrichment unavailable (no api_client)."},
                ensure_ascii=False,
            )
        from services.metadata_enrich import enrich_book_metadata

        if book_id:
            b = db.get_book(book_id)
            if not b:
                return json.dumps({"error": f"Book not found: {book_id}"}, ensure_ascii=False)
            targets = [b]
        elif all_books:
            targets = db.list_books(limit=10000)
            if not targets:
                return json.dumps({"result": "No books in the library."}, ensure_ascii=False)
        else:
            return json.dumps({
                "error": "Provide book_id to enrich one book, or set all_books=true for the whole library.",
            }, ensure_ascii=False)

        results: list[dict] = []
        for b in targets:
            if b.id in _enrich_in_flight:
                continue
            _enrich_in_flight.add(b.id)
            try:
                before = (b.title, b.author or "", b.author_nationality or "")
                ok = await enrich_book_metadata(api_client, db, b.id)
                after = db.get_book(b.id)
                aft = (after.title, after.author or "", after.author_nationality or "")
                changed = ok and aft != before
                results.append({
                    "book_id": b.id,
                    "title": _fmt_change(before[0], aft[0]),
                    "author": _fmt_change(before[1], aft[1]),
                    "nationality": aft[2],
                    "status": "changed" if changed else ("ok" if ok else "failed"),
                })
            finally:
                _enrich_in_flight.discard(b.id)

        return json.dumps({
            "processed": len(results),
            "results": results,
        }, ensure_ascii=False)

    registry.register(ToolSpec(
        name="enrich_book_metadata",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Clean book metadata via one LLM call per book: strip marketing "
            "noise from the title (e.g. '作家榜经典：局外人（…北岛推荐版本！…）' "
            "→ '局外人'), normalize the author name (drop 著/编/译 suffixes "
            "and nationality brackets like （美） or [美]), and fill in the "
            "author's nationality (standard Chinese, empty when unsure). "
            "Never renames a book, never guesses. Use when book titles or "
            "authors look messy on the shelf or reading page. One book at a "
            "time (book_id) or the whole library (all_books=true)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": (
                        "Book id to enrich (see library_list for ids). "
                        "Omit it and set all_books=true to enrich every book."
                    ),
                },
                "all_books": {
                    "type": "boolean",
                    "description": (
                        "Enrich the whole library (one LLM call per book). "
                        "Only when book_id is empty."
                    ),
                },
            },
        },
        execute=lambda book_id="", all_books=False: json.dumps(
            {"error": "enrich_book_metadata is async-only — call via the runtime."},
            ensure_ascii=False,
        ),
        execute_async=_tool_enrich_metadata,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    registry.register(ToolSpec(
        name="library_list",
        category="book",
        toolset="library_query",
        description=(
            "List all books imported into the library. "
            "Shows title, format, chapter count, chunk count, "
            "character count, and import status for each book. "
            "Chunks are the reading unit (~800 chars each); "
            "a book with 12 chapters and 256 chunks means the "
            "longest chapter has ~30 reading units."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: 'raw' or 'clean' (default: all)",
                },
            },
        },
        execute=_tool_library_list,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="library_read",
        category="book",
        toolset="library_query",
        description=(
            "Read a chunk of ~800 chars from an imported book chapter. "
            "Use book_id from library_list, and chapter_index (0-based) "
            "to pick a chapter. Each chapter is pre-split into chunks of "
            "~800 characters — use chunk_index (0-based) to advance "
            "through the chapter. The response header shows progress "
            "like [3/12] = chunk 3 of 12 in this chapter. "
            "Much faster than read_book since no file parsing is needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (returned by import_book or library_list)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index (default: 0)",
                },
                "chunk_index": {
                    "type": "integer",
                    "description": "0-based chunk index within the chapter (default: 0). Increment to read the next chunk.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Optional truncation limit (default: 0 = read entire chunk)",
                },
            },
            "required": ["book_id"],
        },
        execute=_tool_library_read,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="library_search",
        category="book",
        toolset="library_query",
        description=(
            "Full-text search across all imported books in the library. "
            "Searches within reading chunks (~800 chars) using FTS5 trigram "
            "index for fast keyword and phrase matching. "
            "Returns excerpted passages with book, chapter, and chunk location. "
            "Use this to find where a topic, character, or quote appears "
            "across your entire library. Suggested limit: 3-5 results."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keyword or phrase",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 20)",
                },
            },
            "required": ["query"],
        },
        execute=_tool_library_search,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_annotation_search(query: str, limit: int = 5,
                                annotation_type: str = "") -> str:
        """Search the reader's highlights & notes across ALL books (全库跨书).

        Recall channel 兜底: 注入的 <user-highlights>/<user-notes> 不足时
        主动跨书检索用户收藏与笔记。
        """
        try:
            from wingman.service import WingmanMemoryProvider
            terms = WingmanMemoryProvider._extract_keywords(query)
            t = (annotation_type or "").strip()
            types = {
                "highlight": ("highlight",),
                "note": ("note",),
            }.get(t, ("highlight", "note"))
            rows = db.search_annotations(
                terms, types=types, limit=min(int(limit), 20),
            )
            if not rows:
                return "(没有匹配的收藏/笔记)"
            lines = [f"📌 收藏/笔记命中 ({len(rows)} 条)"]
            for a in rows:
                try:
                    b = db.get_book(a.book_id) if a.book_id else None
                    title = b.title if b else (a.stable_book_id or "?")
                except Exception:
                    title = a.stable_book_id or "?"
                kind = "笔记" if a.type in ("note", "both") else "收藏"
                text = (a.note_text or a.selected_text or "").strip()
                if len(text) > 120:
                    text = text[:117] + "…"
                lines.append(f"- [{kind}] 《{title}》 ch.{a.chapter_index} ck.{a.chunk_index}")
                lines.append(f"    {text}")
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"annotation_search failed: {exc}"})

    registry.register(ToolSpec(
        name="annotation_search",
        category="book",
        toolset="library_query",
        exposure=ToolExposure.DIRECT,
        description=(
            "跨书搜索读者收藏的高亮与笔记（annotations 表，全库）。"
            "按关键词匹配选中文本/笔记正文，当前在读书的结果排前。"
            "当上下文注入的 <user-highlights>/<user-notes> 不够时，"
            "或读者提到某段之前收藏/批注过的文字时使用。"
            "Suggested limit: 3-5."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词——想找的收藏/笔记主题或原文片段",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限（默认 5）",
                },
                "annotation_type": {
                    "type": "string",
                    "enum": ["", "highlight", "note"],
                    "description": "限定类型：highlight=收藏 / note=笔记 / 空=两者",
                },
            },
            "required": ["query"],
        },
        execute=_tool_annotation_search,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_library_split(book_id: str,
                            dry_run: bool = True,
                            force: bool = False) -> str:
        """Auto-detect chapter boundaries and split a single-chapter book into chapters."""
        try:
            result = split_book_chapters(db, book_id, force=force, dry_run=dry_run)
            if "error" in result:
                return f"Split failed: {result['error']}"
            if result.get("skipped"):
                return f"Skipped: {result['reason']}"
            mode = "[DRY RUN] " if dry_run else ""
            lines = [
                f"{mode}Split '{result['title']}' into {result['new_chapter_count']} chapters:",
                "",
            ]
            for ch in result["chapters"]:
                lines.append(
                    f"  [{ch['index']}] {ch['title'][:60]} "
                    f"({ch['chars']:,} chars)"
                )
            if dry_run:
                lines.append("")
                lines.append("Review the plan, then run with dry_run=false to apply.")
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"Split failed: {exc}"})

    registry.register(ToolSpec(
        name="library_split_chapters",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Re-split a book's chapters using auto-detected boundaries. "
            "Newly imported books are already auto-chunked into ~800-char "
            "reading units; use this only when chapter boundaries are clearly "
            "wrong. Uses regex patterns (第X章, Chapter X, etc.) to find "
            "chapter headings. ALWAYS use dry_run=true first to preview."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID to split (from library_list or import_book)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview the split plan without applying (default: true)",
                },
                "force": {
                    "type": "boolean",
                    "description": "Force re-split even if book already has multiple chapters",
                },
            },
            "required": ["book_id"],
        },
        execute=_tool_library_split,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    # ── Library edit tools ──────────────────────────────────────────────

    def _tool_library_edit_book(book_id: str, title: str = "",
                                 author: str = "", category_id: str = "") -> str:
        """Edit a book's metadata."""
        try:
            fields = {}
            if title:
                fields["title"] = title
            if author:
                fields["author"] = author
            if category_id:
                fields["category_id"] = category_id
            if not fields:
                return "Error: at least one of title, author, or category_id must be provided"
            book = edit_book(db, book_id, **fields)
            return format_import_summary(book)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Edit book failed: {exc}"})

    registry.register(ToolSpec(
        name="library_edit_book",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Edit metadata for an imported book in the library. "
            "You can update the title, author, and/or category assignment. "
            "At least one of title, author, or category_id is required."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list or import_book)",
                },
                "title": {
                    "type": "string",
                    "description": "New title for the book",
                },
                "author": {
                    "type": "string",
                    "description": "New author name",
                },
                "category_id": {
                    "type": "string",
                    "description": "New category ID (from library_manage_categories list)",
                },
            },
            "required": ["book_id"],
        },
        execute=_tool_library_edit_book,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_edit_chapter(book_id: str, chapter_index: int,
                                    content: str = "", title: str = "") -> str:
        """Edit a chapter's content or title."""
        try:
            ct = content if content else None
            tl = title if title else None
            if ct is None and tl is None:
                return "Error: at least one of content or title must be provided"
            result = edit_chapter(db, book_id, chapter_index, content=ct, title=tl)
            lines = [
                f"✓ Chapter {result['chapter_index']} updated",
                f"  Title: '{result['before']['title']}' → '{result['after']['title']}'",
                f"  Chars: {result['before']['char_count']:,} → {result['after']['char_count']:,}",
                f"  Book total: {result['book_total_chars']:,} chars",
            ]
            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Edit chapter failed: {exc}"})

    registry.register(ToolSpec(
        name="library_edit_chapter",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Edit the content or title of a specific chapter in an imported book. "
            "At least one of content or title must be provided. "
            "The chapter_index is 0-based (first chapter = 0)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list or import_book)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index to edit",
                },
                "content": {
                    "type": "string",
                    "description": "New content for the chapter",
                },
                "title": {
                    "type": "string",
                    "description": "New title for the chapter",
                },
            },
            "required": ["book_id", "chapter_index"],
        },
        execute=_tool_library_edit_chapter,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_replace(book_id: str, chapter_index: int,
                               old_text: str, new_text: str = "",
                               mode: str = "literal",
                               dry_run: bool = True,
                               context: int = 50) -> str:
        """Find-and-replace text within a chapter — like WPS/Word replace."""
        try:
            if not old_text:
                return json.dumps({"error": "old_text cannot be empty"})
            result = replace_in_chapter(db, book_id, chapter_index,
                                        old_text, new_text,
                                        mode=mode, dry_run=dry_run,
                                        context=context)

            label = "[DRY RUN] " if dry_run else ""
            lines = [
                f"{label}Replace in '{result['chapter_title']}' "
                f"(Ch.{result['chapter_index'] + 1}): "
                f"{result['count']} match(es) found",
                f"  Mode: {result['mode']}",
                f"  Chars: {result['chars_before']:,} → {result['chars_after']:,} "
                f"({result['chars_delta']:+,})",
                "",
            ]

            if result["count"] == 0:
                lines.append("  (no matches found — check old_text and try again)")
            else:
                for i, p in enumerate(result["preview"], 1):
                    lines.append(f"  [{i}] {p['before']}")
                    lines.append(f"      {p['after']}")
                    lines.append("")
                if result.get("preview_capped"):
                    remaining = result["total_matches"] - len(result["preview"])
                    lines.append(f"  ... and {remaining} more match(es)")
                    lines.append("")

            if dry_run and result["count"] > 0:
                lines.append("Review the changes, then run with dry_run=false to apply.")
            elif not dry_run and result["count"] > 0:
                lines.append(f"Applied {result['count']} replacement(s).")

            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Replace failed: {exc}"})

    registry.register(ToolSpec(
        name="library_replace",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Find-and-replace text within a book chapter — like WPS/Word replace. "
            "Supports progressive preview: start with a small context to scan, "
            "then expand to verify surrounding text before applying.\n"
            "  • literal (default) — exact string match, safe and predictable\n"
            "  • regex — Python regex pattern for advanced find/replace\n"
            "ALWAYS use dry_run=true first to preview the changes, "
            "then confirm with the user before running with dry_run=false. "
            "Use context=200 (or higher) when you need to verify large surrounding "
            "text, or context=50 for a quick overview scan."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list or import_book)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index to search within",
                },
                "old_text": {
                    "type": "string",
                    "description": "Text to find (literal string, or regex pattern if mode='regex')",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text (default: empty string = delete matches)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["literal", "regex"],
                    "description": "Search mode: 'literal' for exact match (default), 'regex' for pattern matching",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview changes without applying (default: true). Set to false to actually apply.",
                },
                "context": {
                    "type": "integer",
                    "description": "Chars of surrounding context in preview (default 50). Use 200+ to see more surrounding text for verification, or 20-30 for a quick scan. Progressive: start small, then expand.",
                },
            },
            "required": ["book_id", "chapter_index", "old_text"],
        },
        execute=_tool_library_replace,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_delete_chapter(book_id: str, chapter_index: int) -> str:
        """Delete a chapter from a book."""
        try:
            result = remove_chapter(db, book_id, chapter_index)
            lines = [
                f"🗑 Deleted chapter {result['removed']['chapter_index']}: "
                f"'{result['removed']['title']}' ({result['removed']['char_count']:,} chars)",
                f"Book now has {result['book_after']['chapter_count']} chapters, "
                f"{result['book_after']['total_chars']:,} total chars",
            ]
            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Delete chapter failed: {exc}"})

    registry.register(ToolSpec(
        name="library_delete_chapter",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Delete a specific chapter from an imported book. "
            "This operation cannot be undone — confirm with the user first. "
            "The chapter_index is 0-based. Remaining chapters keep their indices "
            "(no reindexing, so there may be gaps)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list or import_book)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index to delete",
                },
            },
            "required": ["book_id", "chapter_index"],
        },
        execute=_tool_library_delete_chapter,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_delete_book(book_id: str, confirm: str = "") -> str:
        """Delete an entire book and all its chapters from the library."""
        try:
            # Safety: require explicit confirmation
            book = db.get_book(book_id)
            if book is None:
                return json.dumps({"error": f"Book not found: {book_id}"})
            expected = f"delete {book.title}"
            if confirm.strip().lower() != expected.lower():
                return json.dumps({
                    "error": "Confirmation required. This will permanently delete "
                             f"'{book.title}' ({book.chapter_count} chapters, "
                             f"{book.total_chars:,} chars). "
                             f"Pass confirm=\"{expected}\" to proceed.",
                    "needs_confirmation": True,
                })
            result = delete_book(db, book_id)
            lines = [
                f"Deleted '{result['title']}' by {result['author'] or 'Unknown'}",
                f"  Format: {result['format'].upper()}",
                f"  Chapters: {result['chapter_count']}",
                f"  Total chars: {result['total_chars']:,}",
            ]
            if result.get("local_copy_removed"):
                lines.append(f"  Local copy removed: {result['local_copy_removed']}")
            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Delete book failed: {exc}"})

    registry.register(ToolSpec(
        name="library_delete_book",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Delete an entire book and all its chapters from the library. "
            "This operation is IRREVERSIBLE — all chapter data will be lost. "
            "ALWAYS confirm with the user before calling this tool. "
            "Requires confirm=\"delete <book_title>\" as a safety check."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID to delete (from library_list)",
                },
                "confirm": {
                    "type": "string",
                    "description": "Must be 'delete <book title>' exactly — a safety check to prevent accidental deletion",
                },
            },
            "required": ["book_id", "confirm"],
        },
        execute=_tool_library_delete_book,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_manage_categories(action: str, category_id: str = "",
                                         name: str = "", sort_order: int = 0) -> str:
        """Manage book categories: create, update, delete, or list them."""
        try:
            kwargs = {}
            if category_id:
                kwargs["category_id"] = category_id
            if name:
                kwargs["name"] = name
            if sort_order:
                kwargs["sort_order"] = sort_order
            result = manage_categories(db, action, **kwargs)

            if action == "list":
                if result["count"] == 0:
                    return "(no categories)"
                lines = [f"📁 Categories ({result['count']})", ""]
                for cat in result["categories"]:
                    lines.append(f"  {cat['id']}  {cat['name']}  (order: {cat['sort_order']})")
                return "\n".join(lines)
            elif action == "create":
                cat = result["category"]
                return f"✓ Created category '{cat['name']}' (id: {cat['id']})"
            elif action == "update":
                return f"✓ Updated category {result['category_id']}: {', '.join(result['updated_fields'])}"
            elif action == "delete":
                return f"✓ Deleted category {result['category_id']}. {result.get('note', '')}"
            return json.dumps(result)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Category operation failed: {exc}"})

    registry.register(ToolSpec(
        name="library_manage_categories",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Manage book categories: create, rename, delete, or list them. "
            "Use this to organize books by genre, topic, or reading status.\n"
            "  action='create' — create a new category (name required)\n"
            "  action='update' — rename or reorder a category (category_id + name/sort_order)\n"
            "  action='delete' — delete a category (books under it become uncategorized)\n"
            "  action='list'   — list all existing categories"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "delete", "list"],
                    "description": "Category operation to perform",
                },
                "category_id": {
                    "type": "string",
                    "description": "Category ID (required for update and delete)",
                },
                "name": {
                    "type": "string",
                    "description": "Category name (required for create, optional for update)",
                },
                "sort_order": {
                    "type": "integer",
                    "description": "Sort order for display ordering (optional)",
                },
            },
            "required": ["action"],
        },
        execute=_tool_library_manage_categories,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    # ── Version control tools ──────────────────────────────────────────

    def _tool_library_list_versions(book_id: str, chapter_index: int = -1,
                                     limit: int = 50) -> str:
        """List version history for a chapter or all chapters."""
        try:
            ci = chapter_index if chapter_index >= 0 else None
            versions = list_versions(db, book_id, chapter_index=ci, limit=limit)
            if not versions:
                scope = f"Chapter {chapter_index}" if chapter_index >= 0 else "Book"
                return f"(no version history for {scope} {book_id})"

            lines = [f"Version History ({len(versions)} entries)", ""]
            for v in versions:
                delta = v["new_char_count"] - v["old_char_count"]
                delta_str = f"{delta:+,}"
                lines.append(
                    f"  {v['version_id']}  Ch.{v['chapter_index']}  "
                    f"{v['operation']:20s}  "
                    f"{v['old_char_count']:,} -> {v['new_char_count']:,} chars  "
                    f"({delta_str})"
                )
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"List versions failed: {exc}"})

    registry.register(ToolSpec(
        name="library_list_versions",
        category="book",
        toolset="library_query",
        exposure=ToolExposure.DEFERRED,
        description=(
            "List version history for a book's chapters. Shows every edit/replace/"
            "delete operation with before/after char counts. "
            "Use chapter_index to filter to a specific chapter, or omit to see all. "
            "Each version has a version_id for use with library_rollback."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "Filter to a specific chapter (0-based). Omit or use -1 to see all chapters.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum versions to return (default: 50)",
                },
            },
            "required": ["book_id"],
        },
        execute=_tool_library_list_versions,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_library_rollback(version_id: str, dry_run: bool = True) -> str:
        """Rollback a chapter to a previous version."""
        try:
            result = rollback_chapter(db, version_id, dry_run=dry_run)
            label = "[DRY RUN] " if dry_run else ""
            lines = [
                f"{label}Rollback to {version_id}",
                f"  Operation: {result['operation']}",
                f"  Chapter: {result['chapter_index']}",
                f"  Current chars: {result['current_chars']:,}",
                f"  Restored chars: {result['restored_chars']:,}",
                f"  Delta: {result['chars_delta']:+,}",
                f"  Snapshot hash: {result['snapshot_hash']}",
            ]
            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Rollback failed: {exc}"})

    registry.register(ToolSpec(
        name="library_rollback",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Rollback a chapter to a previous version from its snapshot. "
            "Use library_list_versions first to find the version_id to restore. "
            "ALWAYS use dry_run=true first to preview the rollback details, "
            "then confirm with the user before dry_run=false. "
            "Rollback itself creates a new version (so it's reversible)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "version_id": {
                    "type": "string",
                    "description": "Version ID to rollback to (from library_list_versions)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview the rollback without applying (default: true)",
                },
            },
            "required": ["version_id"],
        },
        execute=_tool_library_rollback,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    return registry


# ── TTS tool helpers ─────────────────────────────────────────────────────

# ── Voice helpers ────────────────────────────────────────────────────


def _read_audio_as_b64(path) -> str | None:
    """Read audio file and return data URI for voiceclone API."""
    from pathlib import Path
    import base64
    p = Path(path) if not isinstance(path, Path) else path
    if not p.exists():
        return None
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("utf-8")
    ext = p.suffix.lower()
    mime = "audio/mpeg" if ext in (".mp3", ".mpeg") else "audio/wav"
    return f"data:{mime};base64,{b64}"


def _resolve_voice(voice: str, book_id: str, wingman_db: Any,
                   style: str | None = None) -> dict:
    """Resolve voice parameter to {model, voice_value, user_msg, error}.

    voice forms:
      - preset name ("茉莉")          -> mimo-v2.5-tts
      - "clone:<name>"                 -> mimo-v2.5-tts-voiceclone (ref audio from CLONE_VOICES)
      - "design:<prompt>"              -> mimo-v2.5-tts-voicedesign
      - character name                 -> lookup voice_roles -> voiceclone (or error)
      - "clone_xxx" (legacy voice_id)  -> backward compat (preset path)
    """
    from pathlib import Path
    from voices import PRESET_VOICE_NAMES, CLONE_VOICE_MAP

    result: dict = {"model": "", "voice_value": None, "user_msg": None, "error": None}

    # 1. Preset voice
    if voice in PRESET_VOICE_NAMES:
        result["model"] = "mimo-v2.5-tts"
        result["voice_value"] = voice
        if style:
            result["user_msg"] = style
        return result

    # 2. clone:<name> - user DIY reference audio
    if voice.startswith("clone:"):
        clone_name = voice[6:]
        clone = CLONE_VOICE_MAP.get(clone_name)
        if not clone:
            result["error"] = f"未找到克隆音色 '{clone_name}'，请检查 voices.py 的 CLONE_VOICES"
            return result
        ref_path = Path(__file__).parent / clone["ref_audio"]
        b64 = _read_audio_as_b64(ref_path)
        if not b64:
            result["error"] = f"克隆参考音频文件不存在: {clone['ref_audio']}"
            return result
        result["model"] = "mimo-v2.5-tts-voiceclone"
        result["voice_value"] = b64
        if style:
            result["user_msg"] = style
        return result

    # 3. design:<prompt> - ad-hoc voicedesign
    if voice.startswith("design:"):
        prompt = voice[7:]
        result["model"] = "mimo-v2.5-tts-voicedesign"
        result["user_msg"] = (prompt + " " + style) if style else prompt
        return result

    # 4. Legacy clone_xxx voice_id - temporary backward compat
    if voice.startswith("clone_"):
        result["model"] = "mimo-v2.5-tts"
        result["voice_value"] = voice
        if style:
            result["user_msg"] = style
        return result

    # 5. Character name - lookup voice_roles
    if wingman_db and book_id:
        role = wingman_db.get_voice_role(book_id, voice)
        if role:
            ref_path = Path(__file__).parent / role["ref_audio_path"]
            b64 = _read_audio_as_b64(ref_path)
            if b64:
                result["model"] = "mimo-v2.5-tts-voiceclone"
                result["voice_value"] = b64
                if style:
                    result["user_msg"] = style
                return result
            result["error"] = f"角色 '{voice}' 的参考音频文件丢失，请重新铸造"
            return result
        result["error"] = f"角色 '{voice}' 尚无音色，请先调 voice_design 铸造"
        return result

    # No book_id context - fallback to preset default
    result["model"] = "mimo-v2.5-tts"
    result["voice_value"] = "茉莉"
    if style:
        result["user_msg"] = style
    return result


def _tool_tts_speak(text: str, voice: str = "茉莉", style: str = "",
                    *, config: Any = None, wingman_db: Any = None,
                    book_id: str = "") -> str:
    """Sync fallback - uses urllib to avoid asyncio.run() in running loop."""
    import base64
    import hashlib
    import urllib.request
    from pathlib import Path

    text = text.strip()[:500]
    if not text:
        return json.dumps({"error": "text is required"})

    resolved = _resolve_voice(voice, book_id, wingman_db, style or None)
    if resolved.get("error"):
        return json.dumps({"error": resolved["error"]}, ensure_ascii=False)

    # Console TTS slot model overrides the voice-kind mapping when set
    model = config.tts_model or resolved["model"]
    voice_value = resolved.get("voice_value")
    user_msg = resolved.get("user_msg")

    voice_dir = Path(__file__).parent / "data" / "voice" / "tts"
    voice_dir.mkdir(parents=True, exist_ok=True)

    cache_payload = f"{text}|{model}|{voice_value or ''}|{user_msg or ''}"
    cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()[:16]
    cache_path = voice_dir / f"tts_{cache_key}.wav"

    if cache_path.exists():
        return json.dumps({
            "type": "voice",
            "url": f"/voice/tts_{cache_key}.wav",
            "text": text[:100],
            "voice": voice,
        }, ensure_ascii=False)

    messages = []
    if user_msg:
        messages.append({"role": "user", "content": user_msg})
    messages.append({"role": "assistant", "content": text})

    audio: dict = {"format": "wav"}
    if voice_value:
        audio["voice"] = voice_value

    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "audio": audio,
    }).encode("utf-8")

    tts_key = config.tts_api_key or config.api_key
    req = urllib.request.Request(
        f"{config.tts_api_base or config.api_base}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {tts_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        return json.dumps({"error": f"TTS failed: {exc}"})

    choices = data.get("choices", [])
    if not choices:
        return json.dumps({"error": "TTS returned no data"})

    audio_b64 = choices[0].get("message", {}).get("audio", {}).get("data", "")
    if not audio_b64:
        return json.dumps({"error": "TTS returned no audio"})

    if "," in audio_b64:
        audio_b64 = audio_b64.split(",", 1)[1]

    cache_path.write_bytes(base64.b64decode(audio_b64))

    return json.dumps({
        "type": "voice",
        "url": f"/voice/tts_{cache_key}.wav",
        "text": text[:100],
        "voice": voice,
    }, ensure_ascii=False)


async def _tool_tts_speak_async(text: str, voice: str = "茉莉", style: str = "",
                                *, config: Any = None, wingman_db: Any = None,
                                book_id: str = "") -> str:
    """Call MiMo TTS and return the audio URL for the frontend to render."""
    import base64
    import hashlib
    import aiohttp
    from pathlib import Path

    text = text.strip()[:500]
    if not text:
        return json.dumps({"error": "text is required"})

    resolved = _resolve_voice(voice, book_id, wingman_db, style or None)
    if resolved.get("error"):
        return json.dumps({"error": resolved["error"]}, ensure_ascii=False)

    # Console TTS slot model overrides the voice-kind mapping when set
    model = config.tts_model or resolved["model"]
    voice_value = resolved.get("voice_value")
    user_msg = resolved.get("user_msg")

    voice_dir = Path(__file__).parent / "data" / "voice" / "tts"
    voice_dir.mkdir(parents=True, exist_ok=True)

    cache_payload = f"{text}|{model}|{voice_value or ''}|{user_msg or ''}"
    cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()[:16]
    cache_path = voice_dir / f"tts_{cache_key}.wav"

    if cache_path.exists():
        return json.dumps({
            "type": "voice",
            "url": f"/voice/tts_{cache_key}.wav",
            "text": text[:100],
            "voice": voice,
        }, ensure_ascii=False)

    messages = []
    if user_msg:
        messages.append({"role": "user", "content": user_msg})
    messages.append({"role": "assistant", "content": text})

    audio: dict = {"format": "wav"}
    if voice_value:
        audio["voice"] = voice_value

    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "audio": audio,
    }
    tts_key = config.tts_api_key or config.api_key
    headers = {
        "Authorization": f"Bearer {tts_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.tts_api_base or config.api_base}/chat/completions",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return json.dumps({"error": f"TTS API error: {resp.status}"})
                data = await resp.json()
    except Exception as exc:
        return json.dumps({"error": f"TTS failed: {exc}"})

    choices = data.get("choices", [])
    if not choices:
        return json.dumps({"error": "TTS returned no data"})

    audio_b64 = choices[0].get("message", {}).get("audio", {}).get("data", "")
    if not audio_b64:
        return json.dumps({"error": "TTS returned no audio"})

    if "," in audio_b64:
        audio_b64 = audio_b64.split(",", 1)[1]

    cache_path.write_bytes(base64.b64decode(audio_b64))

    return json.dumps({
        "type": "voice",
        "url": f"/voice/tts_{cache_key}.wav",
        "text": text[:100],
        "voice": voice,
    }, ensure_ascii=False)


# ── Voice design / lookup / list tools ───────────────────────────────


async def _tool_voice_design_async(book_id: str, character_name: str, prompt: str,
                                    *, config: Any = None, wingman_db: Any = None) -> str:
    """Cast a character voice: voicedesign generates ref audio, stored for voiceclone reuse."""
    import base64
    import aiohttp
    from pathlib import Path
    from voices import VOICE_CAST_SAMPLE

    if not book_id or not character_name or not prompt:
        return json.dumps({"error": "book_id, character_name, prompt 都是必填"})
    if not wingman_db:
        return json.dumps({"error": "音色数据库不可用"})

    ref_dir = Path(__file__).parent / "data" / "voice" / "refs"
    ref_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize character name for filename
    safe_name = "".join(c for c in character_name if c.isalnum() or c in "._-") or "char"
    ref_filename = f"refs_{book_id}_{safe_name}.wav"
    ref_path = ref_dir / ref_filename
    ref_rel = f"data/voice/refs/{ref_filename}"

    body = {
        "model": "mimo-v2.5-tts-voicedesign",
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": VOICE_CAST_SAMPLE},
        ],
        "stream": False,
        "audio": {"format": "wav"},
    }
    tts_key = config.tts_api_key or config.api_key
    headers = {
        "Authorization": f"Bearer {tts_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.tts_api_base or config.api_base}/chat/completions",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    return json.dumps({"error": f"voicedesign API error: {resp.status}"})
                data = await resp.json()
    except Exception as exc:
        return json.dumps({"error": f"voicedesign failed: {exc}"})

    choices = data.get("choices", [])
    if not choices:
        return json.dumps({"error": "voicedesign returned no data"})

    audio_b64 = choices[0].get("message", {}).get("audio", {}).get("data", "")
    if not audio_b64:
        return json.dumps({"error": "voicedesign returned no audio"})

    if "," in audio_b64:
        audio_b64 = audio_b64.split(",", 1)[1]

    ref_path.write_bytes(base64.b64decode(audio_b64))

    # Persist to voice_roles table
    wingman_db.upsert_voice_role(book_id, character_name, prompt, ref_rel)

    return json.dumps({
        "type": "voice_design",
        "url": f"/voice/{ref_filename}",
        "character": character_name,
        "prompt": prompt[:200],
        "ref_audio": ref_rel,
    }, ensure_ascii=False)


def _tool_voice_role_lookup(character_name: str = "", *, wingman_db: Any = None,
                            reading_ctx: dict | None = None) -> str:
    """Look up a character's cast voice (design prompt + ref audio)."""
    if not wingman_db:
        return json.dumps({"error": "音色数据库不可用"})
    book_id = ""
    if reading_ctx:
        book_id = reading_ctx.get("book_id", "")
    if not book_id or not character_name:
        return json.dumps({"error": "book_id 和 character_name 都是必填"})
    role = wingman_db.get_voice_role(book_id, character_name)
    if not role:
        return json.dumps({"not_found": True, "character": character_name,
                           "hint": "该角色尚无音色，请调 voice_design 铸造"}, ensure_ascii=False)
    return json.dumps({
        "character": character_name,
        "prompt": role["design_prompt"],
        "ref_audio": role["ref_audio_path"],
        "has_audio": True,
    }, ensure_ascii=False)


def _tool_voice_list() -> str:
    """List available voices (preset + clone). Voicedesign is dynamic, not listed."""
    from voices import PRESET_VOICES, CLONE_VOICES
    presets = [{"name": v["name"], "gender": v.get("gender", ""),
                "desc": v.get("desc", "")} for v in PRESET_VOICES]
    clones = [{"name": v["name"], "gender": v.get("gender", ""),
               "desc": v.get("desc", "")} for v in CLONE_VOICES]
    return json.dumps({
        "preset": presets,
        "clone": clones,
        "design_hint": "角色音色是动态铸造的，调 voice_role_lookup 查询特定角色",
    }, ensure_ascii=False)


def _register_agent_tool(registry: ToolRegistry, api_client: Any, config: Any) -> None:
    """Register the Agent tool for sub-agent spawning.

    The sync ``execute`` is a stub — the Agent tool must be called via the
    async execution path (``execute_async``) because it needs to ``await``
    the sub-agent's ``run_turn()``.
    """
    from sub_agent import execute_agent

    # Sync stub — tell the caller to use the async path
    def _agent_sync(**kwargs: Any) -> str:
        return json.dumps(
            {"error": "Agent tool must be called via async execution path"}
        )

    # Async handler — closure captures runtime dependencies
    async def _agent_async(**kwargs: Any) -> str:
        return await execute_agent(
            description=kwargs.get("description", ""),
            prompt=kwargs.get("prompt", ""),
            subagent_type=kwargs.get("subagent_type", "reader"),
            model=kwargs.get("model"),
            api_client=api_client,
            tool_registry=registry,
            config=config,
        )

    # Streaming handler — yields sub-agent progress chunks to the user
    async def _agent_stream(**kwargs: Any) -> AsyncGenerator[str, None]:
        from sub_agent import execute_agent_stream

        async for chunk in execute_agent_stream(
            description=kwargs.get("description", ""),
            prompt=kwargs.get("prompt", ""),
            subagent_type=kwargs.get("subagent_type", "reader"),
            model=kwargs.get("model"),
            api_client=api_client,
            tool_registry=registry,
            config=config,
        ):
            yield chunk

    registry.register(
        ToolSpec(
            name="Agent",
            category="agent",
            toolset="agent",
            exposure=ToolExposure.DIRECT_MODEL_ONLY,
            description=(
                "Launch a reading sub-agent to read and analyze book content "
                "without polluting the main conversation context. "
                "Use this when you need to read book chapters — the sub-agent "
                "works autonomously with restricted tools and returns "
                "structured reading notes. "
                "Available subagent_type(s): 'reader' (book reading)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Short description of what the sub-agent should do",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Detailed instructions for the sub-agent, including "
                        "which book/chapters to read and what to focus on",
                    },
                    "subagent_type": {
                        "type": "string",
                        "description": "Sub-agent type: 'reader' (default) for book reading",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override for the sub-agent",
                    },
                },
                "required": ["description", "prompt"],
            },
            execute=_agent_sync,
            execute_async=_agent_async,
            execute_stream=_agent_stream,
            permission_mode=PermissionMode.READ_ONLY,
        )
    )


# ═══════════════════════════════════════════════════════════════════════════
# Session history tools
# ═══════════════════════════════════════════════════════════════════════════


def _tool_search_sessions(
    query: str, limit: int = 10, include_archived: bool = False, db: Any = None,
) -> str:
    """Search session history by content keyword.

    Searches across all past conversation messages for the given query.
    Returns matching sessions with metadata and content snippets.
    By default excludes archived sessions.
    """
    if session_db is None:
        return json.dumps({"error": "Database not available"})

    try:
        results = session_db.search_session_content(
            query, limit=limit, include_archived=include_archived,
        )
    except Exception as e:
        return json.dumps({"error": f"Search failed: {e}"})

    if not results:
        return json.dumps({"message": f"No sessions found matching '{query}'", "sessions": []})

    import datetime
    output = []
    for r in results:
        ts = datetime.datetime.fromtimestamp(r["time_updated"] / 1000).strftime("%Y-%m-%d %H:%M")
        entry = {
            "session_id": r["session_id"],
            "title": r["title"],
            "mode": r["mode"],
            "updated": ts,
            "snippets": r.get("snippets", []),
        }
        output.append(entry)

    return json.dumps({"sessions": output}, ensure_ascii=False)


def _tool_list_sessions(
    limit: int = 20, include_archived: bool = False, db: Any = None,
) -> str:
    """List recent sessions with metadata.

    Returns session IDs, titles, modes, message counts and update times.
    Does NOT load full message content — use read_session for that.
    By default excludes archived sessions.
    """
    if session_db is None:
        return json.dumps({"error": "Database not available"})

    try:
        rows = session_db.list_sessions(limit=limit, include_archived=include_archived)
    except Exception as e:
        return json.dumps({"error": f"List failed: {e}"})

    if not rows:
        return json.dumps({"message": "No sessions found", "sessions": []})

    import datetime
    output = []
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r.time_updated / 1000).strftime("%Y-%m-%d %H:%M")
        output.append({
            "session_id": r.id,
            "title": r.title or "无标题",
            "mode": r.mode,
            "message_count": r.message_count,
            "updated": ts,
        })

    return json.dumps({"sessions": output}, ensure_ascii=False)


def _tool_read_session(session_id: str, db: Any = None, session_service: Any = None) -> str:
    """Load a full session's conversation history by session ID.

    Returns all messages in the session with their content.
    Use list_sessions or search_sessions to find session IDs first.
    """
    if session_service is None:
        return json.dumps({"error": "Session service not available"})

    try:
        session = session_service.load_messages(session_id)
        if session is None:
            return json.dumps({"error": f"Session '{session_id}' not found"})

        messages = []
        for msg in session.messages:
            parts = []
            for block in msg.blocks:
                if block.type == "text":
                    parts.append({"type": "text", "text": block.text or ""})
                elif block.type == "tool_use":
                    parts.append({
                        "type": "tool_use",
                        "tool_name": block.tool_name or "",
                        "tool_input": block.tool_input or {},
                    })
                elif block.type == "tool_result":
                    parts.append({
                        "type": "tool_result",
                        "tool_output": (block.tool_output or "")[:500],
                    })
            messages.append({
                "role": msg.role,
                "parts": parts,
            })

        return json.dumps({
            "session_id": session.session_id,
            "title": session.title or "无标题",
            "mode": session.mode,
            "message_count": len(messages),
            "messages": messages,
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"Read session failed: {e}"})


def create_builtin_registry(
        search_api_key: str = "",
        api_client: Any = None,
        config: Any = None,
        bookbank_db: Any = None,
        session_db: Any = None,
        session_service: Any = None,
        wingman_db: Any = None,
) -> ToolRegistry:
    """Create a ToolRegistry pre-populated with V1 built-in tools.

    ``api_client`` and ``config`` are required for the ``Agent`` tool
    (sub-agent spawning). They are optional for backwards compatibility
    but the Agent tool will return an error if they are missing.

    ``db`` is the Database instance for library tools (import_book,
    library_list, library_read, library_search). When omitted, library
    tools are not registered.
    archive tool. When omitted, the archive tool is not registered.
    """
    # Set search key in environment so tool functions can access it
    if search_api_key and not os.environ.get("BOOK_COMPANION_SEARCH_API_KEY"):
        os.environ["BOOK_COMPANION_SEARCH_API_KEY"] = search_api_key

    registry = ToolRegistry()

    registry.register(ToolSpec(
        name="read_file",
        category="file",
        toolset="file_ops",
        exposure=ToolExposure.DEFERRED,
        description="Read the contents of a text file. Use to read book excerpts, notes, or configuration.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read"},
            },
            "required": ["path"],
        },
        execute=_tool_read_file,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="write_file",
        category="file",
        toolset="file_ops",
        exposure=ToolExposure.HIDDEN,
        description="Write content to a file. Use to save notes, reading logs, or configuration.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to write to"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        execute=_tool_write_file,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    registry.register(ToolSpec(
        name="get_time",
        category="utility",
        toolset="utility",
        description="Get the current date and time. Use when you need to know what time it is.",
        parameters={
            "type": "object",
            "properties": {
                "format": {"type": "string", "description": "strftime format string (default: %Y-%m-%d %H:%M:%S)"},
            },
        },
        execute=_tool_get_time,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="list_directory",
        category="file",
        toolset="file_ops",
        exposure=ToolExposure.DEFERRED,
        description="List files and directories at a given path.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: current directory)"},
            },
        },
        execute=_tool_list_directory,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # Web search via Tavily API
    if search_api_key:
        registry.register(ToolSpec(
            name="web_search",
            category="search",
            toolset="knowledge",
            description=(
                "Search the web using Tavily (AI-optimized search engine). "
                "Returns an AI-generated answer followed by source links and snippets. "
                "Use for current events, factual lookups, author background, "
                "historical context, or any information beyond your knowledge cutoff."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"},
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (1-20, default 5)",
                    },
                    "deep_search": {
                        "type": "boolean",
                        "description": "Use advanced search for comprehensive research (slower but deeper)",
                    },
                    "topic": {
                        "type": "string",
                        "enum": ["general", "news"],
                        "description": "Search topic: 'general' for broad search, 'news' for current events",
                    },
                },
                "required": ["query"],
            },
            execute=_tool_web_search,
            execute_async=_tool_web_search_async,
            permission_mode=PermissionMode.READ_ONLY,
        ))

    # ── Book reader tools ─────────────────────────────────────────────────
    _register_book_tools(registry)

    # ── Library management tools ──────────────────────────────────────────
    if bookbank_db is not None:
        _register_library_tools(registry, bookbank_db, api_client)

    # Weather via wttr.in (free, no API key needed)
    registry.register(ToolSpec(
        name="get_weather",
        category="utility",
        toolset="utility",
        description=(
            "Get current weather conditions for a city. "
            "Returns temperature, humidity, wind, visibility, and general condition. "
            "Use when the user asks about weather, or when you want to naturally "
            "reference the current environment to enhance the reading atmosphere."
        ),
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name for weather lookup (e.g. 'Beijing', 'London', 'Tokyo')",
                },
            },
            "required": ["city"],
        },
        execute=_tool_get_weather,
        execute_async=_tool_get_weather_async,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── Session history tools ────────────────────────────────────────
    if bookbank_db is not None:
        registry.register(ToolSpec(
            name="search_sessions",
            category="session",
            toolset="knowledge",
            exposure=ToolExposure.DEFERRED,
            description=(
                "Search past conversation history by content keyword. "
                "Returns matching sessions with metadata and message snippets. "
                "Use this to find previous discussions about a book, topic, or character. "
                "Excludes archived sessions by default — use include_archived=true to search them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword(s) to find in past conversations",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum sessions to return (default: 10, suggested: 3-5)",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived sessions (default: false)",
                    },
                },
                "required": ["query"],
            },
            execute=lambda query, limit=10, include_archived=False: _tool_search_sessions(query, limit, include_archived, db=db),
            permission_mode=PermissionMode.READ_ONLY,
        ))

        registry.register(ToolSpec(
            name="list_sessions",
            category="session",
            toolset="knowledge",
            exposure=ToolExposure.DEFERRED,
            description=(
                "List recent session history. Returns session IDs, titles, modes, "
                "message counts, and last-update timestamps. "
                "Use this to browse past conversations. Follow with read_session "
                "to load the full content of a specific session. "
                "Excludes archived sessions by default."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many recent sessions to list (default: 20)",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived sessions (default: false)",
                    },
                },
            },
            execute=lambda limit=20, include_archived=False: _tool_list_sessions(limit, include_archived, db=db),
            permission_mode=PermissionMode.READ_ONLY,
        ))

    if session_service is not None:
        registry.register(ToolSpec(
            name="read_session",
            category="session",
            toolset="knowledge",
            exposure=ToolExposure.DEFERRED,
            description=(
                "Load the full conversation history of a past session by its ID. "
                "Returns all messages with their content. "
                "Use list_sessions or search_sessions to find the session ID first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID (from list_sessions or search_sessions)",
                    },
                },
                "required": ["session_id"],
            },
            execute=lambda session_id: _tool_read_session(session_id, db=db, session_service=session_service),
            permission_mode=PermissionMode.READ_ONLY,
        ))

    # ── TTS + voice tools ─────────────────────────────────────────────
    if config is not None:
        # Capture reading_ctx for voice resolution (book_id -> character lookup)
        _reading_ctx = registry.reading_ctx

        registry.register(ToolSpec(
            name="tts_speak",
            category="voice",
            toolset="knowledge",
            exposure=ToolExposure.DIRECT,
            description=(
                "将文本转换为语音并播放。用于角色扮演时主动'说话'。"
                "voice 可传：预置音色名(茉莉/苏打/白桦等)、角色名(查曾用音色)、"
                "'clone:名'(用户DIY音色)、'design:描述'(即兴设计音色)。"
                "text 中可内联语音标签控制情绪节奏，如 [叹气]罢了、[停顿]你信我。"
                "style 可选，传入导演指令控制整体语气情绪。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要朗读的文本（建议 ≤500 字以保证生成速度）",
                    },
                    "voice": {
                        "type": "string",
                        "description": "音色标识：预置名/角色名/clone:名/design:描述。默认茉莉",
                    },
                    "style": {
                        "type": "string",
                        "description": "可选风格指令，如'轻快活泼'/'低沉愤怒'。控制本次语气",
                    },
                },
                "required": ["text"],
            },
            execute=lambda text, voice="茉莉", style="": _tool_tts_speak(
                text, voice, style, config=config, wingman_db=wingman_db,
                book_id=_reading_ctx.get("book_id", "")),
            execute_async=lambda text, voice="茉莉", style="": _tool_tts_speak_async(
                text, voice, style, config=config, wingman_db=wingman_db,
                book_id=_reading_ctx.get("book_id", "")),
            permission_mode=PermissionMode.READ_ONLY,
        ))

        # Voice design - cast a character's voice (voicedesign -> ref audio -> voiceclone reuse)
        if wingman_db is not None:
            registry.register(ToolSpec(
                name="voice_design",
                category="voice",
                toolset="knowledge",
                exposure=ToolExposure.DIRECT,
                description=(
                    "为角色铸造音色。用 voicedesign 模型基于描述生成参考音频，"
                    "后续该角色配音走 voiceclone 保证音色稳定。"
                    "prompt 按《音色设计规范》编写：身份锚点/声音质感/语速节奏/情绪底色。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "character_name": {"type": "string", "description": "角色名"},
                        "prompt": {
                            "type": "string",
                            "description": "音色描述，按音色设计规范编写",
                        },
                    },
                    "required": ["character_name", "prompt"],
                },
                execute=lambda character_name, prompt: json.dumps(
                    {"error": "voice_design 需要异步执行，请使用 async 路径"}),
                execute_async=lambda character_name, prompt: _tool_voice_design_async(
                    _reading_ctx.get("book_id", ""), character_name, prompt,
                    config=config, wingman_db=wingman_db),
                permission_mode=PermissionMode.READ_ONLY,
            ))

            registry.register(ToolSpec(
                name="voice_role_lookup",
                category="voice",
                toolset="knowledge",
                exposure=ToolExposure.DIRECT,
                description="查询角色曾用音色（复用入口）。返回 design prompt 和参考音频路径。",
                parameters={
                    "type": "object",
                    "properties": {
                        "character_name": {"type": "string", "description": "角色名"},
                    },
                    "required": ["character_name"],
                },
                execute=lambda character_name="": _tool_voice_role_lookup(
                    character_name, wingman_db=wingman_db, reading_ctx=_reading_ctx),
                permission_mode=PermissionMode.READ_ONLY,
            ))

        registry.register(ToolSpec(
            name="voice_list",
            category="voice",
            toolset="knowledge",
            exposure=ToolExposure.DIRECT,
            description="列出可用音色（预置 + 克隆）。角色音色是动态铸造的，不在列表中。",
            parameters={"type": "object", "properties": {}},
            execute=lambda: _tool_voice_list(),
            permission_mode=PermissionMode.READ_ONLY,
        ))

    # ── Agent tool (sub-agent spawning) ──────────────────────────────
    _register_agent_tool(registry, api_client, config)

    # ── tool_search — BM25 discovery of deferred tools ─────────────────
    from tool_search import ToolSearchEngine, ToolSearchEntry

    def _handle_tool_search(query: str) -> str:
        import re

        # Parse category: prefix for semantic filtering
        # Syntax: "category: session" → all session tools
        #          "category: memory 搜索" → memory tools matching "搜索"
        cat_filter: str | None = None
        keyword_query = query.strip()
        m = re.match(r"category:\s*(\S+)", query)
        if m:
            cat_filter = m.group(1).lower()
            keyword_query = query[m.end():].strip()

        deferred = [
            ToolSearchEntry(
                name=s.name,
                description=s.description,
                toolset=s.toolset,
                category=getattr(s, "category", ""),
            )
            for s in registry.get_deferred_specs()
        ]

        # Apply category filter first (exact match)
        if cat_filter:
            deferred = [t for t in deferred if t.category.lower() == cat_filter]
            if not deferred:
                return json.dumps({
                    "message": f"No tools found in category '{cat_filter}'.",
                    "available_categories": sorted(set(
                        getattr(s, "category", "") for s in registry.get_deferred_specs()
                        if getattr(s, "category", "")
                    )),
                }, ensure_ascii=False)

        if not deferred:
            return json.dumps({"message": "No additional tools available."})

        engine = ToolSearchEngine(deferred)
        results = engine.search(keyword_query) if keyword_query else [
            {"name": t.name, "description": t.description,
             "toolset": t.toolset, "category": t.category, "score": 0}
            for t in deferred
        ]
        if not results:
            hint = f" in category '{cat_filter}'" if cat_filter else ""
            return json.dumps({"message": f"No tools found matching '{keyword_query}'{hint}."})

        lines = [f"Found {len(results)} matching tool(s):", ""]
        for r in results:
            brief = r["description"].split("。")[0].split("\n")[0].strip()
            cat_tag = f" [{r.get('category', '')}]" if r.get("category") else ""
            lines.append(f"  - `{r['name']}`{cat_tag}: {brief}")
        return "\n".join(lines)

    # ── BookGraph: node detail query (DIRECT, all kinds) ──────────────

    def _tool_graph_node(
        name: str,
        max_chapter: int | None = None,
    ) -> str:
        """Fetch a single entity's detail from the knowledge graph.

        Returns identity / relations / quotes (if character) / timeline
        as compact markdown via formatter.format_node. Works for any
        node kind (character / event / location / concept / …), not
        just characters.
        """
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_node

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        # Resolve effective max_chapter:
        #   -1            -> None (full-book view)
        #   None (omitted)-> reader's current progress from ctx
        #   int           -> custom ceiling
        if max_chapter == -1:
            effective_max = None
        elif max_chapter is None:
            effective_max = ctx.get("max_chapter")
        else:
            effective_max = max_chapter

        gdb = GraphDB(graph_path)
        try:
            detail = gdb.get_node_detail(book_id, name, max_chapter=effective_max)
        finally:
            gdb.close()

        if detail.get("error"):
            return json.dumps(detail, ensure_ascii=False)

        return format_node(detail, max_chapter=effective_max)

    registry.register(ToolSpec(
        name="graph_node",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DIRECT,
        description=(
            "查询书中某个实体（人物/事件/地点/概念等）的详情。"
            "返回身份、关系、台词（若是角色）、出场章节，格式为紧凑 Markdown。"
            "默认只含读者当前进度及之前的内容（防剧透）；传 max_chapter=-1 查全书视角。"
            "示例: graph_node(\"思琪\"), graph_node(\"生日宴\"), graph_node(\"思琪\", max_chapter=-1)"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "实体名（支持子串匹配），如「思琪」「李国华」「生日宴」",
                },
                "max_chapter": {
                    "type": "integer",
                    "description": "章节上限。不传 = 读者当前进度（默认防剧透）；传 -1 = 全书视角；传正整数 = 自定义上限。",
                },
            },
            "required": ["name"],
        },
        execute=_tool_graph_node,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # -- BookGraph + reading tools (DIRECT) -------------------------
    # These tools read per-request context from registry.reading_ctx,
    # populated by api_server for all modes (not just role-play).

    def _tool_graph_search(query: str) -> str:
        """Search the knowledge graph for matching entities."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_search

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")
        gdb = GraphDB(graph_path)
        try:
            results = gdb.search_all(
                book_id, query, max_chapter=max_chapter, limit=10,
            )
        finally:
            gdb.close()

        return format_search(results, query=query)

    registry.register(ToolSpec(
        name="graph_search",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DIRECT,
        description=(
            "搜索书中的实体（人物/事件/地点/概念等）。"
            "返回实体名称、类型、描述和首次出场章节，格式为紧凑 Markdown。"
            "当读者提到某个人或事、你想确认书中细节、或需要回忆角色关系时使用。"
            "找到实体后可用 graph_node 查详情。"
            "示例: graph_search(\"思琪\"), graph_search(\"生日宴\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词——人物名、事件名、地点或概念",
                },
            },
            "required": ["query"],
        },
        execute=_tool_graph_search,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── BookGraph: context query (PRIMARY, DIRECT) ─────────────────────
    # Composes search + node + baked trace. One call gives the LLM a
    # focused subgraph so it doesn't need to chain graph_node/trace.
    # Mirrors codegraph's context tool (always-loaded, bakes trace in).

    def _tool_graph_context(question: str) -> str:
        """Answer a natural-language question using the knowledge graph."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_context

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")

        # Extract candidate entity names from the question.
        # jieba splits Chinese; 2-grams catch names jieba breaks
        # (e.g. "思琪" -> "思"+"琪" -> 2-gram "思琪" recovers it).
        import jieba
        tokens = [t.strip() for t in jieba.cut(question) if t.strip()]
        candidates: set[str] = set()
        for t in tokens:
            if len(t) >= 2:
                candidates.add(t)
        for i in range(len(tokens) - 1):
            gram = (tokens[i] + tokens[i + 1]).strip()
            if len(gram) >= 2:
                candidates.add(gram)

        gdb = GraphDB(graph_path)
        try:
            # Search each candidate, collect entry points (cap 5)
            entries: list[dict] = []
            seen_names: set[str] = set()
            for cand in candidates:
                if len(entries) >= 5:
                    break
                results = gdb.search_all(book_id, cand, max_chapter=max_chapter, limit=2)
                for r in results:
                    nm = r["name"]
                    if nm in seen_names:
                        continue
                    detail = gdb.get_node_detail(book_id, nm, max_chapter=max_chapter)
                    if not detail.get("error"):
                        entries.append(detail)
                        seen_names.add(nm)
                    if len(entries) >= 5:
                        break

            if not entries:
                return f"未在图谱中找到与「{question}」相关的实体。"

            # Bake paths between entry pairs (cap 3 paths)
            paths: list[dict] = []
            for i in range(len(entries)):
                for j in range(i + 1, len(entries)):
                    if len(paths) >= 3:
                        break
                    p = gdb.find_path(
                        book_id,
                        entries[i]["canonical_name"],
                        entries[j]["canonical_name"],
                        max_chapter=max_chapter,
                    )
                    if p.get("hops"):
                        paths.append(p)

            # Collect quotes from entry characters (cap 6)
            quotes: list[dict] = []
            for e in entries:
                if e.get("kind") == "character":
                    for q in e.get("quotes", [])[:3]:
                        quotes.append({**q, "character": e["canonical_name"]})
                if len(quotes) >= 6:
                    break
        finally:
            gdb.close()

        return format_context(
            {"question": question, "entries": entries, "paths": paths, "quotes": quotes},
            max_chapter=max_chapter,
        )

    registry.register(ToolSpec(
        name="graph_context",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DIRECT,
        description=(
            "用自然语言提问查图谱：入口实体 + 关系 + 关系链 + 相关引文，一次返回。"
            "当读者问「X 和 Y 什么关系」「书里的 XX 主题」、或需要聚焦了解某块内容时使用。"
            "这是查图谱的首选工具，不必再分别调 graph_search/graph_node/graph_trace。"
            "示例: graph_context(\"思琪和伊纹的关系\"), graph_context(\"书中的暴力场景\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "自然语言问题，如「思琪和伊纹的关系」「亚历山大和第欧根尼的互动」",
                },
            },
            "required": ["question"],
        },
        execute=_tool_graph_context,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_current_passage() -> str:
        """Return the chunk of text the reader is currently on."""
        ctx = registry.reading_ctx
        db = ctx.get("db")
        book_id = ctx.get("book_id", "")
        if not db or not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        ch_idx = ctx.get("chapter_index", 0)
        ck_idx = ctx.get("chunk_index", 0)
        ck = db.get_chunk(book_id, ch_idx, ck_idx)
        if not ck:
            return json.dumps({"error": "无法获取当前段落"}, ensure_ascii=False)

        book = db.get_book(book_id)
        ch = db.get_chapter(book_id, ch_idx)
        total = db.get_chunk_count(book_id, ch_idx)
        passage = ck.content[:1500]
        if len(ck.content) > 1500:
            passage += "\n…（段落较长，已截断）"

        header = (
            f"《{book.title if book else book_id}》 | "
            f"{ch.title if ch else '第' + str(ch_idx + 1) + '章'} | "
            f"第 {ck_idx + 1}/{total} 块\n\n"
        )
        return header + passage

    registry.register(ToolSpec(
        name="current_passage",
        category="reading",
        toolset="file_reading",
        exposure=ToolExposure.DIRECT,
        description=(
            "获取读者当前正在阅读的段落原文。"
            "当你需要确认读者正在看什么内容、或者读者提到「这一段」时使用。"
            "返回当前章节标题、位置信息和段落文本（最长 1500 字符）。"
            "不需要参数。"
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        execute=_tool_current_passage,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── BookGraph: DEFERRED tools (discovered via tool_search) ─────────

    def _tool_graph_quotes(character: str) -> str:
        """Return a character's quotes from the knowledge graph."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_quotes

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")
        gdb = GraphDB(graph_path)
        try:
            quotes = gdb.get_quotes_by_character(
                book_id, character, max_chapter=max_chapter, limit=15,
            )
        finally:
            gdb.close()

        return format_quotes(quotes, character=character)

    registry.register(ToolSpec(
        name="graph_quotes",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DEFERRED,
        description=(
            "回忆某个角色的经典台词。返回台词文本、场景上下文和出处章节。"
            "当你想引用角色的话、或读者问「XX 说过什么」时使用。"
            "示例: graph_quotes(\"思琪\"), graph_quotes(\"李国华\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "character": {
                    "type": "string",
                    "description": "角色名字，如「思琪」「李国华」",
                },
            },
            "required": ["character"],
        },
        execute=_tool_graph_quotes,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_graph_trace(from_name: str, to_name: str) -> str:
        """Find the relation path between two entities."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_trace

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")
        gdb = GraphDB(graph_path)
        try:
            path = gdb.find_path(book_id, from_name, to_name, max_chapter=max_chapter)
        finally:
            gdb.close()

        return format_trace(path)

    registry.register(ToolSpec(
        name="graph_trace",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DEFERRED,
        description=(
            "查找两个实体之间的关系路径（最短路径，≤4 跳）。"
            "返回 A 到 B 经过哪些实体和关系。"
            "当你想理清「X 怎么卷入 Y」「X 和 Y 什么关系」时使用。"
            "示例: graph_trace(\"思琪\", \"伊纹\"), graph_trace(\"亚历山大\", \"第欧根尼\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_name": {
                    "type": "string",
                    "description": "起点实体名",
                },
                "to_name": {
                    "type": "string",
                    "description": "终点实体名",
                },
            },
            "required": ["from_name", "to_name"],
        },
        execute=_tool_graph_trace,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_graph_timeline(name: str) -> str:
        """Show a node's chapter appearance timeline."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_timeline

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")
        gdb = GraphDB(graph_path)
        try:
            detail = gdb.get_node_detail(book_id, name, max_chapter=max_chapter)
        finally:
            gdb.close()

        if detail.get("error"):
            return json.dumps(detail, ensure_ascii=False)

        return format_timeline(detail, max_chapter=max_chapter)

    registry.register(ToolSpec(
        name="graph_timeline",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DEFERRED,
        description=(
            "查看某个实体在哪些章节出场。返回已见出场和后续出场（防剧透）。"
            "当你想确认「这个角色/事件在第几章出现」时使用。"
            "示例: graph_timeline(\"思琪\"), graph_timeline(\"生日宴\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "实体名",
                },
            },
            "required": ["name"],
        },
        execute=_tool_graph_timeline,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="tool_search",
        toolset="utility",
        exposure=ToolExposure.DIRECT,
        category="search",
        description=(
            "Discover hidden tools not shown in the default list. "
            "PREFER 'category: <name>' first — it gives a full overview: "
            "'category: session' (session history), "
            "'category: memory' (memory search/write), "
            "'category: book' (book reading). "
            "Use keyword search only when you don't know which category. "
            "Suggested keywords: 'edit', 'import', 'delete', 'history' etc."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords, e.g. 'edit chapter', 'import book'. Use 'category: <name>' to filter by category (session, memory, book, search, file, utility, agent).",
                },
            },
            "required": ["query"],
        },
        execute=_handle_tool_search,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    return registry
