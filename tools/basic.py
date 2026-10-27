"""Basic tools: file ops, clock, weather, web search (moved from tools.py)."""
from __future__ import annotations


import json
import os
import time
from pathlib import Path
from permissions import PermissionMode
from tools.registry import ToolExposure, ToolRegistry, ToolSpec


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


def _register_basic(registry: ToolRegistry) -> None:
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


def _register_web_search(registry: ToolRegistry, search_api_key: str) -> None:
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


def _register_weather(registry: ToolRegistry) -> None:
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


def _register_tool_search(registry: ToolRegistry) -> None:
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
