"""Sub-agent system — isolated reading agents that don't pollute the main context.

A sub-agent reuses ConversationRuntime with a restricted tool set (whitelist
per type), a fresh Session, and a minimal system prompt. The main agent
delegates book-reading work to sub-agents via the ``Agent`` tool.

v1: synchronous execution only. The sub-agent runs to completion and its
output is returned directly as the tool result.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace

from config import Config
from session import Session
from tools import ToolExposure, ToolRegistry
import usage_log

from .types import (
    CONTEXT_CREATING_TOOLS,
    SUBAGENT_DEFAULT_MAX_ITERATIONS,
    SUBAGENT_MAX_ITERATIONS,
    SUBAGENT_PROMOTE_DEFERRED,
    SUBAGENT_SYSTEM_PROMPT,
    SUBAGENT_TOOLSETS,
)


# ── Output model ─────────────────────────────────────────────────────────


@dataclass
class SubAgentOutput:
    """Result of a sub-agent execution.

    Field design anticipates future SQLite storage — each field maps 1:1 to a
    database column.  ``result_text`` is the primary consumable output (the
    LLM's final response after executing its tool calls).
    """

    agent_id: str
    agent_type: str
    description: str
    status: str  # "completed" | "failed"
    result_text: str
    prompt: str = ""
    book_path: str | None = None
    chapters: list[int] = field(default_factory=list)
    token_usage: dict = field(default_factory=dict)
    created_at: str = ""
    completed_at: str = ""
    error: str | None = None


# ── Stub memory store (sub-agents have no long-term memory) ──────────────


class _EmptyMemoryStore:
    """Stub that always returns empty — sub-agents don't inject memory."""

    def freeze_for_system_prompt(self, target: str = "") -> str:
        return ""


# ── Runner ───────────────────────────────────────────────────────────────


class SubAgentRunner:
    """Runs a sub-agent to completion synchronously (within the async tool path).

    Reuses ``ConversationRuntime`` but swaps in:
    * A fresh Session (isolated conversation history)
    * A filtered ToolRegistry (only the tools for this sub-agent type)
    * A minimal system prompt (no CLAUDE.md / persona)
    * No memory / skills / hooks
    * Reduced max_iterations

    Called from the Agent tool's ``execute_async`` handler, so it can ``await``
    the sub-agent's ``run_turn()`` async generator.
    """

    def __init__(
        self,
        api_client,  # ApiClient protocol
        tool_registry: ToolRegistry,
        config: Config,
        context_recorder: object | None = None,
        scenario: str = "sub_agent",
    ):
        self._api_client = api_client
        self._full_registry = tool_registry
        self._config = config
        # Model-binding route: chat-spawned agents inherit ``sub_agent``;
        # import analysis passes ``import.analysis`` so its whole chain
        # uses the import model.
        self._scenario = scenario
        # Optional flight recorder — when injected, every ReAct round of the
        # sub-agent lands in data/context_log.db and shows up in the
        # /flight-recorder/viewer.html session list.
        self._context_recorder = context_recorder
        self.last_output: SubAgentOutput | None = None

    def _build_sub_registry(self, resolved_type: str,
                            reading_ctx: dict | None = None) -> ToolRegistry:
        """子代理的工具表。**单独一个方法是为了能被测到** ——
        两处调用点各抄一遍的话，「上下文到底有没有传进去」就只能靠跑真链路验。

        过滤出来的是**全新** registry，``reading_ctx`` 默认是空的 —— 子代理里
        任何读上下文的工具（``author_lookup`` / ``graph_node``）都会以为
        「没打开书」，一路返回空。

        **只在显式传了才拷**：``book_classifier`` 跑的是**正在导入的那本书**，
        而主会话的 ``reading_ctx`` 指的是**用户当前翻开的书**，两者不是一本 ——
        无差别继承会让导入分析去操作错的书。
        """
        toolsets = SUBAGENT_TOOLSETS.get(resolved_type,
                                         SUBAGENT_TOOLSETS["reader"])
        reg = self._full_registry.get_registry_for_toolsets(
            toolsets=toolsets,
            exclude_tools=CONTEXT_CREATING_TOOLS,
            exclude_exposure=[ToolExposure.DIRECT_MODEL_ONLY, ToolExposure.HIDDEN],
        )
        # 主 agent 隐藏、子代理白名单内可见。**必须换一个 ToolSpec**：
        # get_registry_for_toolsets 登记的是同一个对象，就地改 exposure
        # 会把主 agent 的工具表一起改掉。
        for name, spec in list(reg._tools.items()):
            if (spec.exposure == ToolExposure.DEFERRED
                    and spec.toolset in SUBAGENT_PROMOTE_DEFERRED):
                reg.register(replace(spec, exposure=ToolExposure.DIRECT))
        if reading_ctx:
            reg.reading_ctx.update(reading_ctx)
        return reg

    async def run(
        self,
        *,
        agent_type: str,
        description: str,
        prompt: str,
        model: str | None = None,
        reading_ctx: dict | None = None,
    ) -> SubAgentOutput:
        """Execute a sub-agent and return its structured output.

        ``reading_ctx`` 显式传入时才拷进子代理的 registry（见下面的注释）——
        读上下文的工具靠它拿到「当前是哪本书、剧透门开到哪」。
        """
        from runtime import ConversationRuntime

        agent_id = f"agent-{int(time.time() * 1_000_000)}"
        created_at = _iso_now()

        # Resolve agent type with fallback
        resolved_type = agent_type if agent_type in SUBAGENT_TOOLSETS else "reader"
        max_iter = SUBAGENT_MAX_ITERATIONS.get(
            resolved_type, SUBAGENT_DEFAULT_MAX_ITERATIONS)

        # Build filtered tool registry — unified pipeline, same as main agent
        filtered_registry = self._build_sub_registry(resolved_type, reading_ctx)

        # Minimal system prompt — no CLAUDE.md, no persona, no memory
        #
        # 用 replace 而非 format：提示词里的 JSON 示例带花括号，format 会把它们
        # 当占位符吃掉，逼得模板满屏写 {{ }} —— 那种转义正是提示词最难改的地方。
        system_prompt = SUBAGENT_SYSTEM_PROMPT.get(
            resolved_type, SUBAGENT_SYSTEM_PROMPT["reader"]
        ).replace("{description}", description)

        # Build sub-runtime: same ConversationRuntime, tighter constraints
        sub_runtime = ConversationRuntime(
            session=Session(),
            api_client=self._api_client,
            tool_registry=filtered_registry,
            config=self._config,
            memory_store=_EmptyMemoryStore(),
            skill_manager=None,
            hook_pipeline=None,
            system_prompt=system_prompt,
            scenario=self._scenario,
        ).with_max_iterations(max_iter)
        sub_runtime.context_recorder = self._context_recorder

        # Usage attribution follows the route group: chat-spawned agents
        # are "sub_agent", import analysis is "import".
        source = (self._scenario or "sub_agent").split(".")[0]
        result_parts: list[str] = []
        try:
            with usage_log.source_scope(source):
                async for chunk in sub_runtime.run_turn(prompt):
                    result_parts.append(chunk)
        except Exception as exc:
            return SubAgentOutput(
                agent_id=agent_id,
                agent_type=resolved_type,
                description=description,
                prompt=prompt,
                status="failed",
                result_text="".join(result_parts) if result_parts else f"Sub-agent error: {exc}",
                created_at=created_at,
                completed_at=_iso_now(),
                error=str(exc),
            )

        final_text = "".join(result_parts)

        # Extract book_path + chapters from session messages (best-effort)
        book_path = _extract_book_path(sub_runtime.session)
        chapters = _extract_chapters(sub_runtime.session)

        return SubAgentOutput(
            agent_id=agent_id,
            agent_type=resolved_type,
            description=description,
            prompt=prompt,
            status="completed",
            result_text=final_text,
            book_path=book_path,
            chapters=chapters,
            token_usage=getattr(sub_runtime.session, "token_usage", {}),
            created_at=created_at,
            completed_at=_iso_now(),
        )


    async def run_stream(
        self,
        *,
        agent_type: str,
        description: str,
        prompt: str,
        model: str | None = None,
        reading_ctx: dict | None = None,
    ):
        """Execute a sub-agent and stream its output chunks in real-time.

        Yields display-formatted chunks for the user.  Populates
        ``self.last_output`` with the structured result after completion.
        """
        from runtime import ConversationRuntime

        agent_id = f"agent-{int(time.time() * 1_000_000)}"
        created_at = _iso_now()

        resolved_type = agent_type if agent_type in SUBAGENT_TOOLSETS else "reader"
        max_iter = SUBAGENT_MAX_ITERATIONS.get(
            resolved_type, SUBAGENT_DEFAULT_MAX_ITERATIONS)

        filtered_registry = self._build_sub_registry(resolved_type, reading_ctx)

        # 同上：replace 而非 format，保住提示词里的花括号。
        system_prompt = SUBAGENT_SYSTEM_PROMPT.get(
            resolved_type, SUBAGENT_SYSTEM_PROMPT["reader"]
        ).replace("{description}", description)

        sub_runtime = ConversationRuntime(
            session=Session(),
            api_client=self._api_client,
            tool_registry=filtered_registry,
            config=self._config,
            system_prompt=system_prompt,
            memory_store=_EmptyMemoryStore(),
            skill_manager=None,
            hook_pipeline=None,
            scenario=self._scenario,
        ).with_max_iterations(max_iter)
        sub_runtime.context_recorder = self._context_recorder

        source = (self._scenario or "sub_agent").split(".")[0]
        raw_parts: list[str] = []
        first_chunk = True
        try:
            with usage_log.source_scope(source):
                async for chunk in sub_runtime.run_turn(prompt):
                    # Only prepend [Sub] to the first chunk; pass the rest raw.
                    # Newlines within a chunk get [Sub] markers so each logical
                    # line is tagged, but we avoid token-level fragmentation.
                    if first_chunk:
                        display = f"[Sub] {chunk}"
                        first_chunk = False
                    else:
                        display = chunk
                    display = display.replace("\n", "\n[Sub] ")
                    yield display
                    raw_parts.append(chunk)
        except Exception as exc:
            raw_text = "".join(raw_parts)
            self.last_output = SubAgentOutput(
                agent_id=agent_id,
                agent_type=resolved_type,
                description=description,
                prompt=prompt,
                status="failed",
                result_text=raw_text if raw_text else f"Sub-agent error: {exc}",
                created_at=created_at,
                completed_at=_iso_now(),
                error=str(exc),
            )
            yield f"\n[Sub] Error: {exc}\n"
            return

        final_text = "".join(raw_parts)
        book_path = _extract_book_path(sub_runtime.session)
        chapters = _extract_chapters(sub_runtime.session)

        self.last_output = SubAgentOutput(
            agent_id=agent_id,
            agent_type=resolved_type,
            description=description,
            prompt=prompt,
            status="completed",
            result_text=final_text,
            book_path=book_path,
            chapters=chapters,
            token_usage=getattr(sub_runtime.session, "token_usage", {}),
            created_at=created_at,
            completed_at=_iso_now(),
        )


# ── Helpers ──────────────────────────────────────────────────────────────


def _iso_now() -> str:
    """ISO 8601 timestamp without microseconds (SQLite-friendly)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _extract_book_path(session: Session) -> str | None:
    """Walk session messages to find the first book path used in a tool call."""
    for msg in session.messages:
        for block in msg.blocks:
            if block.tool_name in ("read_book", "book_info"):
                if block.tool_input:
                    try:
                        args = json.loads(block.tool_input)
                        return args.get("path")
                    except (json.JSONDecodeError, TypeError):
                        pass
    return None


def _extract_chapters(session: Session) -> list[int]:
    """Walk session messages to collect chapter indices that were read."""
    chapters: set[int] = set()
    for msg in session.messages:
        for block in msg.blocks:
            if block.tool_name == "read_book":
                if block.tool_input:
                    try:
                        args = json.loads(block.tool_input)
                        idx = args.get("chapter_index")
                        if idx is not None:
                            chapters.add(idx)
                    except (json.JSONDecodeError, TypeError):
                        pass
    return sorted(chapters)


# ── Agent tool entry point ───────────────────────────────────────────────


async def execute_agent(
    *,
    description: str,
    prompt: str,
    subagent_type: str = "reader",
    model: str | None = None,
    api_client=None,
    tool_registry: ToolRegistry | None = None,
    config: Config | None = None,
    reading_ctx: dict | None = None,
) -> str:
    """Async entry point for the ``Agent`` tool.

    Called via ``ToolSpec.execute_async`` from the main agent's ReAct loop.
    """
    if not tool_registry or not config or not api_client:
        return json.dumps(
            {"error": "Agent tool requires api_client, tool_registry, and config"}
        )

    runner = SubAgentRunner(api_client, tool_registry, config)
    output = await runner.run(
        agent_type=subagent_type,
        description=description,
        prompt=prompt,
        model=model,
        reading_ctx=reading_ctx,
    )
    return json.dumps(asdict(output), ensure_ascii=False)


async def execute_agent_stream(
    *,
    description: str,
    prompt: str,
    subagent_type: str = "reader",
    model: str | None = None,
    api_client=None,
    tool_registry: ToolRegistry | None = None,
    config: Config | None = None,
    reading_ctx: dict | None = None,
):
    """Streaming entry point for the ``Agent`` tool.

    Called via ``ToolSpec.execute_stream`` from the main agent's ReAct loop.
    Yields display chunks and populates the runner's ``last_output``.
    """
    if not tool_registry or not config or not api_client:
        yield json.dumps(
            {"error": "Agent tool requires api_client, tool_registry, and config"}
        )
        return

    runner = SubAgentRunner(api_client, tool_registry, config)

    async for chunk in runner.run_stream(
        agent_type=subagent_type,
        description=description,
        prompt=prompt,
        model=model,
        reading_ctx=reading_ctx,
    ):
        yield chunk

    if runner.last_output is not None:
        yield "\n" + json.dumps(asdict(runner.last_output), ensure_ascii=False)
