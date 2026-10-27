"""Built-in tool registry — the composition root.

Each toolset lives in its own module; this file only decides which ones
are registered and in what order.

Registration order is deliberately the same as the original single-file
layout: it determines the order of the ``tools`` array sent to the model,
and a stable order keeps the prompt prefix cache effective.
"""

from __future__ import annotations

import os
from typing import Any

from tools.agent import _register_agent_tool
from tools.author import _register_author_tools
from tools.basic import (
    _register_basic,
    _register_tool_search,
    _register_weather,
    _register_web_search,
)
from tools.books import _register_book_tools
from tools.graph import _register_graph_tools
from tools.import_probe import (
    _register_book_survey_tools,
    _register_epub_probe_tools,
)
from tools.library import _register_library_tools
from tools.registry import ToolRegistry
from tools.sessions import _register_session_tools
from tools.voice import _register_voice_tools


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

    _register_basic(registry)
    _register_web_search(registry, search_api_key)

    # ── Book reader tools ─────────────────────────────────────────────
    _register_book_tools(registry)

    # ── Library management tools ──────────────────────────────────────
    if bookbank_db is not None:
        _register_library_tools(registry, bookbank_db, api_client)
        # 导入前的探查工具（book_classifier 子代理用），只读、不碰库
        _register_epub_probe_tools(registry, bookbank_db)
        # 版式勘测仪器（book_classifier / book_verifier 用）—— 把「书」本身
        # 交给 agent，而不是把统计报表交给它。同样只读、不碰库。
        _register_book_survey_tools(registry, bookbank_db)

    _register_weather(registry)

    # ── Session history tools ─────────────────────────────────────────
    _register_session_tools(registry, bookbank_db, session_service)

    # ── TTS + voice tools ─────────────────────────────────────────────
    _register_voice_tools(registry, config, wingman_db)

    # ── Agent tool (sub-agent spawning) ───────────────────────────────
    _register_agent_tool(registry, api_client, config)

    # ── BookGraph / AuthorGraph ───────────────────────────────────────
    _register_graph_tools(registry)
    _register_tool_search(registry)
    _register_author_tools(registry, api_client, config)

    return registry
