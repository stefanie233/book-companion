"""Sub-agent spawning tool."""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Any
from permissions import PermissionMode
from tools.registry import ToolRegistry, ToolSpec, ToolExposure

_READER_DESCRIPTION = (
    "Launch a reading sub-agent to read and analyze book content "
    "without polluting the main conversation context. "
    "Use this when you need to read book chapters — the sub-agent "
    "works autonomously with restricted tools and returns "
    "structured reading notes. "
    "Available subagent_type(s): 'reader' (book reading)."
)


def _resolve_type(kwargs: dict, default_type: str,
                  allowed_types: set[str] | None) -> tuple[str, str]:
    """``(type, error)``：空值走默认；显式给了不认识的类型要报错，不能
    静默退回默认 —— 否则写作页传 'reader' 会得到一个空工具表的子代理。"""
    wanted = str(kwargs.get("subagent_type") or default_type).strip()
    if allowed_types is not None and wanted not in allowed_types:
        return wanted, (f"未知子代理类型 {wanted}；可用："
                        + " / ".join(sorted(allowed_types)))
    return wanted, ""


def _register_agent_tool(registry: ToolRegistry, api_client: Any, config: Any,
                         *,
                         default_type: str = "reader",
                         allowed_types: set[str] | None = None,
                         description: str | None = None,
                         streaming: bool = True,
                         result_transform: Callable[[dict], dict] | None = None
                         ) -> None:
    """Register the Agent tool for sub-agent spawning.

    The sync ``execute`` is a stub — the Agent tool must be called via the
    async execution path (``execute_async``) because it needs to ``await``
    the sub-agent's ``run_turn()``.

    写作页在**请求级副本**上再注册一次（各自的 tool_registry 闭包不同）：
    ``default_type`` / ``allowed_types`` 决定这页能派哪种子代理，
    ``streaming=False`` 关掉逐字回传（子代理的中间话不该进主上下文），
    ``result_transform`` 把子代理的最终文本压成主 agent 能直接用的结构。
    """
    from sub_agent import execute_agent

    allowed = set(allowed_types or ())
    types = allowed or {default_type}

    # Sync stub — tell the caller to use the async path
    def _agent_sync(**kwargs: Any) -> str:
        return json.dumps(
            {"error": "Agent tool must be called via async execution path"}
        )

    # Async handler — closure captures runtime dependencies
    async def _agent_async(**kwargs: Any) -> str:
        subagent_type, error = _resolve_type(kwargs, default_type, allowed or None)
        if error:
            return json.dumps({"error": error}, ensure_ascii=False)
        raw = await execute_agent(
            description=kwargs.get("description", ""),
            prompt=kwargs.get("prompt", ""),
            subagent_type=subagent_type,
            model=kwargs.get("model"),
            api_client=api_client,
            tool_registry=registry,
            config=config,
        )
        if result_transform is None:
            return raw
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw
        if not isinstance(payload, dict):
            return raw
        return json.dumps(result_transform(payload), ensure_ascii=False)

    # Streaming handler — yields sub-agent progress chunks to the user
    async def _agent_stream(**kwargs: Any) -> AsyncGenerator[str, None]:
        from sub_agent import execute_agent_stream

        subagent_type, error = _resolve_type(kwargs, default_type, allowed or None)
        if error:
            yield json.dumps({"error": error}, ensure_ascii=False)
            return
        async for chunk in execute_agent_stream(
            description=kwargs.get("description", ""),
            prompt=kwargs.get("prompt", ""),
            subagent_type=subagent_type,
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
            description=description or _READER_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "一句话说明子代理要做什么",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "子代理的详细指令：查什么、限定哪本书、重点是什么",
                    },
                    "subagent_type": {
                        "type": "string",
                        "description": ("子代理类型：" + " / ".join(sorted(types))),
                        **({"enum": sorted(types)} if allowed else {}),
                    },
                    "model": {
                        "type": "string",
                        "description": "子代理模型（可选）",
                    },
                },
                "required": ["description", "prompt"],
            },
            execute=_agent_sync,
            execute_async=_agent_async,
            execute_stream=_agent_stream if streaming else None,
            permission_mode=PermissionMode.READ_ONLY,
        )
    )
