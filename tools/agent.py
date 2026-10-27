"""Sub-agent spawning tool."""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any
from permissions import PermissionMode
from tools.registry import ToolRegistry, ToolSpec, ToolExposure


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
