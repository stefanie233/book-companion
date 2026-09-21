"""Sub-agent type definitions — toolset allowlists and system prompt templates.

Each sub-agent type declares which **toolsets** (groups) it can access,
rather than listing individual tool names.  The main agent and sub-agents
share the same ``ToolRegistry`` — the difference is which toolsets are
enabled for each.

To add a new sub-agent type, add entries to ``SUBAGENT_TOOLSETS`` and
``SUBAGENT_SYSTEM_PROMPT``.
"""

from __future__ import annotations

# ── Context-creating tools ────────────────────────────────────────────────

# Tools that launch independent execution contexts (sub-agents, cron jobs,
# forked sessions). These MUST NOT be available inside any sub-agent to
# prevent recursive context nesting.
CONTEXT_CREATING_TOOLS: set[str] = {
    "Agent",
    # Future: "fork_session", "cron_create", ...
}

# ── Toolset allowlists ────────────────────────────────────────────────────

# Map sub-agent type → list of toolset names.  The main pipeline
# (ToolRegistry.get_registry_for_toolsets) resolves these to actual tools,
# automatically excluding CONTEXT_CREATING_TOOLS.

# fmt: off
SUBAGENT_TOOLSETS: dict[str, list[str]] = {
    "reader": [
        "file_reading",
        "knowledge",
        "utility",
    ],
    "extractor": [
        "book_graph",
    ],
    # v2:
    # "analyst": [
    #     "file_reading", "library_query", "knowledge",
    # ],
    # "librarian": [
    #     "library_query", "library_edit",
    # ],
    # "general": None,  # None = all non-context-creating tools
}
# fmt: on

SUBAGENT_DEFAULT_TYPE = "reader"

# ── System prompts ───────────────────────────────────────────────────────

SUBAGENT_SYSTEM_PROMPT: dict[str, str] = {
    "reader": (
        "You are a focused sub-agent. Work autonomously — do NOT ask "
        "the user questions. Use only the tools provided to you. "
        "Return a complete, structured result.\n\n"
        "Task: {description}"
    ),
    # v2:
    # "analyst": (
    #     "You are an analysis sub-agent. Read book content and cross-reference "
    #     "with external sources to produce analytical notes.\n\n"
    #     ...
    # ),
}

# ── Default model overrides ──────────────────────────────────────────────

# Use cheaper/faster models for reading sub-agents.
# None = use the main agent's model.
SUBAGENT_DEFAULT_MODEL: dict[str, str | None] = {
    "reader": None,  # v1: same model, no override
    # "reader": "gpt-4o-mini",  # Future: cheaper model for reading
}
