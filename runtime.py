"""Conversation runtime — the core agent loop.

Generic over ApiClient and ToolExecutor traits via Python Protocols.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from compact import (
    CompactionConfig,
    SummaryCompressionBudget,
    compact_and_compress,
    compact_session,
    compress_summary,
    estimate_session_tokens,
)
from context_files import discover_instruction_files, render_instruction_files
from hooks import HookContext, HookPipeline
from persona import load_persona
from config import Config
from content import ContentBlock, ConversationMessage, TokenUsage
from memory_store import MemoryStore
from permissions import PermissionMode, PermissionOutcome, PermissionPolicy
from session import Session
from skills import SkillManager
from tools import ToolRegistry
import usage_log


# ── Traits (Python Protocols) ───────────────────────────────────────────


class ApiClient(Protocol):
    """Async streaming API contract — one method."""

    async def stream(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[tuple[str, list[dict], TokenUsage | None], None]:
        """Send request, yield (text_delta, accumulated_tool_calls, usage_or_none).

        Each yield provides:
        - text_delta: the current chunk's text fragment ("" if none)
        - accumulated_tool_calls: tool calls accumulated so far (may still be building)
        - usage: TokenUsage on the last chunk, None on all earlier chunks

        Checks cancel_event between chunks and stops if it is set.
        """
        ...


class ToolExecutor(Protocol):
    """Minimal tool execution contract — string-in/string-out."""

    def execute(self, tool_name: str, input_json: str) -> str:
        ...


# ── OpenAI Implementation ───────────────────────────────────────────────


def _sanitize_text(text: str) -> str:
    """Remove lone surrogate characters that break UTF-8 encoding.

    DeepSeek reasoning_content may contain lone surrogates (\\uD800-\\uDFFF)
    that are valid JSON but invalid UTF-8. ``errors="replace"`` on both
    encode and decode ensures we always produce a clean string.
    """
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _build_tool_calls(acc: dict[int, dict]) -> list[dict]:
    """Convert accumulated tool-call dict into a sorted list."""
    return [
        {"id": v["id"], "name": v["name"], "arguments": v["arguments"]}
        for _, v in sorted(acc.items())
    ]


class OpenAIClient:
    """OpenAI-compatible async streaming API client.

    Uses aiohttp + raw SSE parsing to avoid surrogate-character issues
    in the OpenAI SDK's UTF-8 handling.  Also provides an AsyncOpenAI
    fallback codepath for providers that return clean UTF-8.
    """

    def __init__(self, config: Config):
        self.config = config

    async def stream(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[tuple[str, list[dict], TokenUsage | None], None]:
        """Stream LLM response chunks asynchronously.

        Yields (text_delta, accumulated_tool_calls, usage_or_none) for each chunk.
        Retries up to 2 times on transient network errors.
        """
        import aiohttp

        api_messages = [{"role": "system", "content": system_prompt}] + messages

        body: dict = {
            "model": self.config.model,
            "messages": api_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        last_exc: Exception | None = None
        last_usage: TokenUsage | None = None
        for attempt in range(3):  # 1 initial + 2 retries
            if attempt > 0:
                await asyncio.sleep(attempt)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.config.api_base}/chat/completions",
                        json=body,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            raise RuntimeError(f"API {resp.status}: {error_text[:500]}")

                        text_parts: list[str] = []
                        tool_calls_acc: dict[int, dict] = {}
                        reasoning_open = False    # track <think> tag state
                        last_usage = None         # per-attempt: only a fully
                                                  # consumed stream yields usage

                        # Parse SSE stream line-by-line
                        async for raw_line in resp.content:
                            if cancel_event and cancel_event.is_set():
                                return

                            # Decode line, replacing invalid UTF-8
                            try:
                                line = raw_line.decode("utf-8", errors="replace")
                            except Exception:
                                continue

                            line = line.strip()
                            if not line or not line.startswith("data: "):
                                continue

                            data_str = line[6:]  # strip "data: " prefix
                            if data_str == "[DONE]":
                                continue

                            try:
                                data = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            # Parse usage BEFORE the choices guard: include_usage
                            # streams end with a choices-less chunk the old guard
                            # swallowed before the usage block below could run.
                            usage = None
                            u = data.get("usage")
                            if u:
                                usage = TokenUsage(
                                    input_tokens=u.get("prompt_tokens", 0),
                                    output_tokens=u.get("completion_tokens", 0),
                                    total_tokens=u.get("total_tokens", 0),
                                )
                                last_usage = usage

                            # Extract delta from OpenAI-compatible chunk
                            choices = data.get("choices", [])
                            if not choices:
                                if usage is not None:  # usage-only terminal chunk
                                    yield "", _build_tool_calls(tool_calls_acc), usage
                                continue
                            delta = choices[0].get("delta", {})

                            text_delta = ""
                            # Capture reasoning/thinking content (DeepSeek)
                            reasoning = delta.get("reasoning_content", "")
                            if reasoning and isinstance(reasoning, str):
                                chunk = _sanitize_text(reasoning)
                                if not reasoning_open:
                                    chunk = "<think>" + chunk
                                    reasoning_open = True
                                text_delta = chunk
                                text_parts.append(chunk)
                            content = delta.get("content", "")
                            if content and isinstance(content, str):
                                chunk = _sanitize_text(content)
                                if reasoning_open:
                                    chunk = "</think>" + chunk
                                    reasoning_open = False
                                text_delta = chunk if not text_delta else text_delta + chunk
                                text_parts.append(chunk)

                            # Accumulate tool calls
                            tool_calls_delta = delta.get("tool_calls", [])
                            if tool_calls_delta:
                                for tc in tool_calls_delta:
                                    idx = tc.get("index", 0)
                                    if idx not in tool_calls_acc:
                                        tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                                    if tc.get("id"):
                                        tool_calls_acc[idx]["id"] = tc["id"]
                                    func = tc.get("function", {})
                                    if func.get("name"):
                                        tool_calls_acc[idx]["name"] += func["name"]
                                    if func.get("arguments"):
                                        tool_calls_acc[idx]["arguments"] += func["arguments"]

                            # Usage was parsed above the choices guard.
                            yield text_delta, _build_tool_calls(tool_calls_acc), usage

                        # Close <think> tag if still open at stream end
                        if reasoning_open:
                            yield "</think>", _build_tool_calls(tool_calls_acc), None

                # Full stream consumed — record real usage exactly once.
                if last_usage is not None:
                    usage_log.record(
                        model=self.config.model,
                        tokens_in=last_usage.input_tokens,
                        tokens_out=last_usage.output_tokens,
                    )
                return  # success
            except Exception as exc:
                last_exc = exc
                if cancel_event and cancel_event.is_set():
                    return
                continue

        usage_log.record(model=self.config.model, status="error")  # all retries exhausted
        raise last_exc  # type: ignore[misc]

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        model: str | None = None,
        timeout: int = 30,
    ) -> str | None:
        """Non-streaming completion for lightweight tasks (title gen, etc.)."""
        import aiohttp

        body: dict = {
            "model": model or self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.config.api_base}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = json.loads(await resp.text())
                    u = data.get("usage") or {}
                    usage_log.record(
                        model=model or self.config.model,
                        tokens_in=u.get("prompt_tokens", 0),
                        tokens_out=u.get("completion_tokens", 0),
                    )
                    choices = data.get("choices", [])
                    if not choices:
                        return None
                    content = choices[0].get("message", {}).get("content", "")
                    return _sanitize_text(content.strip()) if content else None
        except Exception:
            return None

    async def transcribe(
        self,
        audio_base64: str,
        *,
        language: str = "auto",
    ) -> str | None:
        """ASR via MiMo chat completions (mimo-v2.5-asr). Non-streaming.

        Args:
            audio_base64: raw base64-encoded audio bytes (WAV/WebM).
            language: ISO 639-1 code or ``"auto"``.
        """
        import aiohttp

        body: dict = {
            "model": "mimo-v2.5-asr",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:audio/wav;base64,{audio_base64}",
                            },
                        }
                    ],
                }
            ],
            "stream": False,
            "extra_body": {"asr_options": {"language": language}},
        }

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.config.api_base}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = json.loads(await resp.text())
                    choices = data.get("choices", [])
                    if not choices:
                        return None
                    content = choices[0].get("message", {}).get("content", "")
                    return content.strip() if content else None
        except Exception:
            return None


# ── Turn Summary ────────────────────────────────────────────────────────


@dataclass
class TurnSummary:
    assistant_text: str = ""
    tool_results: list[ConversationMessage] = field(default_factory=list)
    iterations: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    auto_compacted: bool = False
    compacted_count: int = 0


# ── Conversation Runtime ────────────────────────────────────────────────


# ── Interaction Modes ──────────────────────────────────────────────────

MODE_LABELS: dict[str, str] = {
    "immersive": "沉浸伴读",
    "overview": "俯瞰分析",
    "divergent": "发散探索",
}
DEFAULT_MODE = "immersive"

# ── Prompt templates loaded from prompts/ directory ──────────────────────

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Mode prompts — lazy-loaded from prompts/modes/*.md with in-memory cache
_MODE_CACHE: dict[str, str] = {}

def _get_mode_prompt(mode: str) -> str:
    """Load mode prompt from prompts/modes/{mode}.md, cached after first read."""
    if mode not in _MODE_CACHE:
        path = _PROMPTS_DIR / "modes" / f"{mode}.md"
        if path.exists():
            _MODE_CACHE[mode] = path.read_text(encoding="utf-8").strip()
        else:
            _MODE_CACHE[mode] = f"# Current mode: {mode}"
    return _MODE_CACHE[mode]

# L0 mode overview — loaded once at module level
_L0_OVERVIEW_PATH = _PROMPTS_DIR / "modes" / "l0_overview.md"
MODE_L0_OVERVIEW = (
    _L0_OVERVIEW_PATH.read_text(encoding="utf-8").strip()
    if _L0_OVERVIEW_PATH.exists()
    else "# Available Interaction Modes"
)

# Platform hint — loaded once at module level
_PLATFORM_HINT_PATH = _PROMPTS_DIR / "platform_hint.md"
_PLATFORM_HINT = (
    _PLATFORM_HINT_PATH.read_text(encoding="utf-8").strip()
    if _PLATFORM_HINT_PATH.exists()
    else "You are running as a CLI-based reading companion."
)

# Environment section template — loaded once at module level
_ENV_TEMPLATE_PATH = _PROMPTS_DIR / "environment.md"
_ENV_TEMPLATE = (
    _ENV_TEMPLATE_PATH.read_text(encoding="utf-8")
    if _ENV_TEMPLATE_PATH.exists()
    else "# Environment\n- OS: {os} {release}\n- Shell: {shell}\n- Working directory: {cwd}\n- Python: {python_version}"
)


class ConversationRuntime:
    """Coordinates the model loop, tool execution, permissions, and session."""

    def __init__(
        self,
        session: Session,
        api_client: ApiClient,
        tool_registry: ToolRegistry,
        config: Config,
        memory_store: MemoryStore,
        skill_manager: SkillManager | None = None,
        hook_pipeline: HookPipeline | None = None,
        memory_manager: Any = None,
        db: Any = None,
        *,
        system_prompt: str | None = None,
    ):
        self.session = session
        self.api_client = api_client
        self.tool_registry = tool_registry
        self.config = config
        self.memory_store = memory_store
        self.memory_manager = memory_manager  # MemoryManager or None
        self.db = db  # storage.db.Database — for recall hook access
        self.current_book_id = ""  # set by api_server / commands when a book is opened
        self.current_graph_path = ""  # set by api_server alongside current_book_id
        self.context_recorder = None  # optional ContextRecorder for debugging
        # Context meter snapshot (written at turn end; read by /api/context/usage)
        self.last_context_usage = None  # ContextUsage or None
        self.last_context_usage_session = ""
        self.skill_manager = skill_manager or SkillManager(config.skills_dir)
        self.hook_pipeline = hook_pipeline or HookPipeline()
        # Skills are only injected when explicitly wired (not in sub-agents)
        self._skills_enabled = skill_manager is not None

        # Interaction mode — restore from session if available
        self._mode = session.mode if session.mode in MODE_LABELS else DEFAULT_MODE

        # Build system prompt once per runtime
        self.system_prompt = system_prompt if system_prompt is not None else self._build_system_prompt()

        # Permission policy
        self.permission_policy = PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
        for name, mode in tool_registry.get_permission_requirements().items():
            self.permission_policy.with_tool_requirement(name, mode)

        # Configurable limits
        self._max_iterations = config.max_iterations
        self._compact_config = CompactionConfig(
            preserve_recent_messages=config.compact_preserve_recent,
            max_estimated_tokens=config.compact_max_tokens,
        )
        self._auto_compact_threshold = config.auto_compact_threshold

        # State flags
        self._streaming = False  # True while run_turn() is active
        self._compacted_this_turn = 0  # Messages compacted in the last run_turn()

    @property
    def current_mode(self) -> str:
        return self._mode

    @property
    def current_mode_label(self) -> str:
        return MODE_LABELS.get(self._mode, self._mode)

    async def set_mode(self, mode: str) -> bool:
        """Switch interaction mode. Rebuilds system prompt. Returns True if valid."""
        if mode not in MODE_LABELS:
            return False
        old_mode = self._mode
        self._mode = mode
        self.session.mode = mode
        self.system_prompt = self._build_system_prompt()
        await self.hook_pipeline.dispatch("on_mode_change", HookContext(
            mode=mode, session_id=self.session.session_id,
            extra={"old_mode": old_mode}, runtime=self,
        ))
        return True

    def activate_skill(self, name: str) -> bool:
        """Activate a skill by name. Rebuilds system prompt. Returns True on success."""
        result = self.skill_manager.activate(name)
        if result:
            self.system_prompt = self._build_system_prompt()
        return result

    def deactivate_skill(self, name: str) -> bool:
        """Deactivate a skill by name. Rebuilds system prompt. Returns True if it was active."""
        result = self.skill_manager.deactivate(name)
        if result:
            self.system_prompt = self._build_system_prompt()
        return result

    def deactivate_all_skills(self) -> None:
        """Deactivate all skills. Rebuilds system prompt."""
        self.skill_manager.deactivate_all()
        self.system_prompt = self._build_system_prompt()

    def get_active_skill_names(self) -> list[str]:
        """Return names of currently active skills."""
        return self.skill_manager.get_active_names()

    # ── Builder methods ─────────────────────────────────────────────────

    def with_max_iterations(self, n: int) -> ConversationRuntime:
        self._max_iterations = n
        return self

    def with_compact_threshold(self, n: int) -> ConversationRuntime:
        self._auto_compact_threshold = n
        return self

    # ── Main turn loop ──────────────────────────────────────────────────

    async def run_turn(
        self,
        user_input: str,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[str, None]:
        """Execute one conversation turn, yielding text chunks in real-time.

        Yields str fragments for display. Handles the full ReAct loop internally:
        streaming → tool calls → next LLM round, until no more tool calls.
        """
        self._streaming = True
        self._compacted_this_turn = 0
        try:
            # pre_turn hook
            await self.hook_pipeline.dispatch("pre_turn", HookContext(
                user_input=user_input, session_id=self.session.session_id,
                mode=self._mode, runtime=self,
            ))

            # Auto-activate skills whose triggers match user input
            triggered = self.skill_manager.evaluate_triggers(user_input, self._mode)
            for name in triggered:
                self.skill_manager.activate(name)

            # Rebuild system prompt for this turn (enables L2 context injection)
            self.system_prompt = self._build_system_prompt(user_input=user_input)

            self.session.push_user_text(user_input)
            api_messages = self._build_api_messages()
            _base_len = len(api_messages)  # history boundary (current user msg is last)

            # ── Memory prefetch: recall context from external providers ────
            _prefetch_context = ""
            if self.memory_manager:
                try:
                    self.memory_manager.on_turn_start(
                        self.session._turn_count
                        if hasattr(self.session, '_turn_count') else 0,
                        user_input,
                    )
                    _prefetch_context = (
                        self.memory_manager.prefetch_all(user_input) or ""
                    )
                except Exception:
                    pass

            _injected_parts: list[str] = []
            if _prefetch_context:
                from memory_manager import build_memory_context_block
                _block = build_memory_context_block(_prefetch_context)
                if _block:
                    # Inject as a user-visible context message (not system prompt)
                    _injected_parts.append(_block)
                    api_messages.append({
                        "role": "user",
                        "content": _block,
                    })

            # ── Ephemeral context: transient info for this turn only ─────
            # Injected as a user message (not system prompt) so it doesn't
            # break prefix caching.  The <ephemeral-context> tag tells the
            # model this is per-turn metadata, not conversation history.
            _ephemeral_text = (
                "<ephemeral-context>\n"
                f"Current date: {time.strftime('%Y-%m-%d')}\n"
                f"Current time: {time.strftime('%H:%M:%S')}\n"
                "</ephemeral-context>"
            )
            _injected_parts.append(_ephemeral_text)
            api_messages.append({"role": "user", "content": _ephemeral_text})
            _post_inject = len(api_messages)  # context-meter boundary
            _injected_text = "\n\n".join(_injected_parts)

            iterations = 0

            while iterations < self._max_iterations:
                iterations += 1

                if cancel_event and cancel_event.is_set():
                    yield "\n[已取消]"
                    return

                tools_schema = self.tool_registry.get_schemas()

                # ── Save input for flight recorder (recorded after output) ──
                _rec_input = {
                    "system_prompt": self.system_prompt,
                    "messages": list(api_messages),
                    "tools_count": len(tools_schema) if tools_schema else 0,
                } if self.context_recorder else None

                # ── Stream LLM response ────────────────────────────────
                last_text = ""
                tool_calls_at_end: list[dict] = []
                last_usage = TokenUsage()

                try:
                    await self.hook_pipeline.dispatch("on_stream_start", HookContext(
                        session_id=self.session.session_id, mode=self._mode,
                        stream_active=True, runtime=self,
                    ))
                    async for text_chunk, accum_tools, usage in self.api_client.stream(
                        self.system_prompt, api_messages, tools_schema, cancel_event,
                    ):
                        if text_chunk:
                            last_text += text_chunk
                            yield text_chunk
                        tool_calls_at_end = accum_tools
                        if usage:
                            last_usage = usage
                except Exception as exc:
                    await self.hook_pipeline.dispatch("on_stream_end", HookContext(
                        session_id=self.session.session_id, mode=self._mode,
                        stream_active=False, extra={"error": str(exc)}, runtime=self,
                    ))
                    error_msg = f"[Error calling API: {exc}]"
                    self.session.push_message(ConversationMessage.assistant_text(error_msg))
                    yield error_msg
                    return

                await self.hook_pipeline.dispatch("on_stream_end", HookContext(
                    session_id=self.session.session_id, mode=self._mode,
                    stream_active=False, assistant_output=last_text, runtime=self,
                ))

                # ── Build and store assistant message ───────────────────
                assistant_blocks: list[ContentBlock] = []
                if last_text:
                    # Split <think>...</think> into thinking_block + text_block
                    remaining = last_text
                    while True:
                        t_start = remaining.find("<think>")
                        if t_start == -1:
                            if remaining:
                                assistant_blocks.append(ContentBlock.text_block(remaining))
                            break
                        # Text before <think>
                        if t_start > 0:
                            assistant_blocks.append(ContentBlock.text_block(remaining[:t_start]))
                        t_end = remaining.find("</think>", t_start + 7)
                        if t_end == -1:
                            # Unclosed <think> — treat rest as thinking
                            thinking = remaining[t_start + 7:]
                            if thinking:
                                assistant_blocks.append(ContentBlock.thinking_block(thinking))
                            remaining = ""
                            break
                        thinking = remaining[t_start + 7:t_end]
                        if thinking:
                            assistant_blocks.append(ContentBlock.thinking_block(thinking))
                        remaining = remaining[t_end + 8:]
                for tc in tool_calls_at_end:
                    assistant_blocks.append(ContentBlock.tool_use_block(
                        tc["id"], tc["name"], tc["arguments"],
                    ))

                assistant_msg = ConversationMessage(
                    role="assistant",
                    blocks=assistant_blocks,
                    usage=last_usage,
                )
                self.session.push_message(assistant_msg)
                api_messages.append(self._msg_to_api_dict(assistant_msg))

                # ── No tool calls → done ────────────────────────────────
                if not tool_calls_at_end:
                    if not last_text:
                        yield "(received empty response from API)"
                    # Flight recorder: final round (no tools)
                    if self.context_recorder and _rec_input:
                        self.context_recorder.record_round(
                            session_id=self.session.session_id,
                            turn_index=self.session._turn_count
                                if hasattr(self.session, '_turn_count') else 0,
                            react_round=iterations,
                            mode=self._mode,
                            book_id=self.current_book_id,
                            response_text=last_text,
                            tool_calls=None,
                            tool_results=None,
                            **_rec_input,
                        )
                    break

                # ── Execute tools ────────────────────────────────────────
                _tool_results: list[dict] = []
                for tc in tool_calls_at_end:
                    if cancel_event and cancel_event.is_set():
                        yield "\n[已取消]"
                        return

                    tool_id = tc["id"]
                    tool_name = tc["name"]
                    tool_input = tc["arguments"]

                    outcome = self.permission_policy.authorize(tool_name, tool_input)
                    if not outcome.allowed:
                        result_msg = ConversationMessage.tool_result(
                            tool_id, tool_name, f"[Denied: {outcome.reason}]", is_error=True,
                        )
                    else:
                        yield f"\n[🔧 {tool_name}...]\n"
                        try:
                            spec = self.tool_registry.get(tool_name)
                            if spec and spec.execute_stream is not None:
                                # Streaming execution — forward chunks to user,
                                # collect for LLM tool result
                                output_parts: list[str] = []
                                try:
                                    args = json.loads(tool_input) if tool_input.strip() else {}
                                except json.JSONDecodeError:
                                    result_msg = ConversationMessage.tool_result(
                                        tool_id, tool_name,
                                        json.dumps({"error": f"Invalid JSON for '{tool_name}'"}),
                                        is_error=True,
                                    )
                                    self.session.push_message(result_msg)
                                    api_messages.append(self._msg_to_api_dict(result_msg))
                                    continue
                                try:
                                    async for chunk in spec.execute_stream(**args):
                                        yield chunk
                                        output_parts.append(chunk)
                                    output = "".join(output_parts)
                                except Exception as exc:
                                    output = json.dumps({"error": str(exc)})
                                is_error = '"error"' in output[:50].lower()
                                result_msg = ConversationMessage.tool_result(
                                    tool_id, tool_name, output, is_error=is_error,
                                )
                            elif spec and spec.execute_async is not None:
                                output = await self.tool_registry.execute_async(tool_name, tool_input)
                                is_error = '"error"' in output[:50].lower()
                                result_msg = ConversationMessage.tool_result(
                                    tool_id, tool_name, output, is_error=is_error,
                                )
                            else:
                                output = self.tool_registry.execute(tool_name, tool_input)
                                is_error = '"error"' in output[:50].lower()
                                result_msg = ConversationMessage.tool_result(
                                    tool_id, tool_name, output, is_error=is_error,
                                )
                        except Exception as exc:
                            result_msg = ConversationMessage.tool_result(
                                tool_id, tool_name, str(exc), is_error=True,
                            )

                    self.session.push_message(result_msg)
                    api_messages.append(self._msg_to_api_dict(result_msg))

                    # Dispatch post_tool hook (e.g. auto-clean after import)
                    tool_output_text = (
                        result_msg.blocks[0].tool_output
                        if result_msg.blocks else ""
                    )
                    await self.hook_pipeline.dispatch("post_tool", HookContext(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        tool_output=tool_output_text or "",
                        session_id=self.session.session_id,
                        mode=self._mode,
                        runtime=self,
                    ))
                    if self.context_recorder:
                        _tool_results.append({
                            "name": tool_name,
                            "arguments": tool_input,
                            "result": tool_output_text or "",
                        })

                # ── Flight recorder: round with tool calls ──────────────
                if self.context_recorder and _rec_input:
                    self.context_recorder.record_round(
                        session_id=self.session.session_id,
                        turn_index=self.session._turn_count
                            if hasattr(self.session, '_turn_count') else 0,
                        react_round=iterations,
                        mode=self._mode,
                        book_id=self.current_book_id,
                        response_text=last_text,
                        tool_calls=tool_calls_at_end,
                        tool_results=_tool_results,
                        **_rec_input,
                    )

            # ── Context meter: turn-end snapshot (ring data source) ─────
            # Before auto-compact: this is the watermark the turn ended on.
            try:
                from context_meter import format_bill, measure, resolve_profile
                _model = self.config.model
                _prof = resolve_profile(_model, self.config)
                _snapshot = await asyncio.to_thread(
                    measure,
                    system_prompt=self.system_prompt,
                    tools_schema=tools_schema or [],
                    history_messages=(
                        api_messages[:max(_base_len - 1, 0)]
                        + api_messages[_post_inject:]
                    ),
                    injected_context=_injected_text,
                    user_input=user_input,
                    usage=last_usage if last_usage.input_tokens else None,
                    window=_prof["window"],
                    model=_model,
                    tokenizer_path=_prof["tokenizer_path"],
                    session_id=self.session.session_id,
                )
                self.last_context_usage = _snapshot
                self.last_context_usage_session = self.session.session_id
                logging.getLogger("context").debug(
                    format_bill(_snapshot, alert_ratio=self.config.context_alert_ratio))
            except Exception:
                logging.getLogger("context").debug(
                    "context meter failed", exc_info=True)

            # ── Auto-compact check ──────────────────────────────────────
            if self._auto_compact_threshold > 0:
                estimated = self.session.estimate_tokens()
                if estimated > self._auto_compact_threshold:
                    # Trigger memory extraction before discarding old messages
                    if self.memory_manager:
                        try:
                            self.memory_manager.on_pre_compress(
                                self.session.to_messages() if hasattr(self.session, 'to_messages')
                                else [{"role": m.role, "content": m.content}
                                      for m in self.session.messages]
                            )
                        except Exception:
                            pass
                    result = compact_and_compress(
                        self.session,
                        CompactionConfig(
                            preserve_recent_messages=self._compact_config.preserve_recent_messages,
                            max_estimated_tokens=0,
                        ),
                        SummaryCompressionBudget(max_chars=1200, max_lines=24, max_line_chars=160),
                    )
                    if result.removed_message_count > 0:
                        # Incremental graph extraction from discarded messages
                        if self.current_book_id and result.removed_messages:
                            await self._extract_graph_observations(result.removed_messages)
                        _service = self.session._service
                        self.session = result.compacted_session
                        self.session._service = _service
                        self._compacted_this_turn = result.removed_message_count
                        self.session.save()  # persist compaction immediately
                        self._run_health_probe()
                        await self.hook_pipeline.dispatch("on_compaction", HookContext(
                            session_id=self.session.session_id, mode=self._mode,
                            compacted_messages=result.removed_message_count, runtime=self,
                        ))

            # ── Memory sync: persist turn to external providers ──────────
            if self.memory_manager and last_text:
                try:
                    assistant_response = last_text
                    self.memory_manager.sync_all(
                        user_input, assistant_response,
                        session_id=self.session.session_id or "",
                    )
                    self.memory_manager.queue_prefetch_all(
                        user_input,
                        session_id=self.session.session_id or "",
                    )
                except Exception:
                    pass

        finally:
            self._streaming = False
            await self.hook_pipeline.dispatch("post_turn", HookContext(
                user_input=user_input, session_id=self.session.session_id,
                mode=self._mode, runtime=self,
            ))

    def manual_compact(self) -> CompactionResult:
        """Manually trigger compaction (via /compact command)."""
        result = compact_and_compress(
            self.session,
            self._compact_config,
            SummaryCompressionBudget(max_chars=1200, max_lines=24, max_line_chars=160),
        )
        if result.removed_message_count > 0:
            _service = self.session._service
            self.session = result.compacted_session
            self.session._service = _service
            self._run_health_probe()
        return result

    async def _extract_graph_observations(self, messages: list) -> None:
        """Extract observations from messages before they're lost to compaction.

        Incremental graph evolution - feeds dialogue into the bookgraph so
        character observations from the conversation are preserved.
        """
        if not self.current_book_id:
            return
        from pathlib import Path
        from bookgraph.incremental import messages_to_dialogue_text, extract_from_dialogue
        from storage.graph_db import GraphDB

        dialogue = messages_to_dialogue_text(messages)
        if not dialogue.strip():
            return

        graph_path = Path(self.current_graph_path) if self.current_graph_path else None
        if not graph_path or not graph_path.exists():
            return

        gdb = GraphDB(graph_path)
        try:
            count = await extract_from_dialogue(
                self.current_book_id, gdb, self.api_client, dialogue,
            )
            if count:
                logger.info("Graph evolution: %d observations from compacted messages", count)
        except Exception:
            logger.warning("Graph evolution extraction failed", exc_info=True)
        finally:
            gdb.close()

    def _run_health_probe(self) -> None:
        """Run a non-destructive tool probe to verify runtime is functional after compaction.

        If the tool executor is broken, the next turn will catch it with a clear error.
        """
        try:
            self.tool_registry.execute("list_directory", '{"path": "."}')
        except Exception:
            pass  # Health probe failure is non-fatal; next turn will surface the issue

    # ── System prompt builder ───────────────────────────────────────────

    # Class-level boundary marker between static and dynamic prompt layers
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"

    def _build_system_prompt(self, user_input: str = "") -> str:
        """Assemble the system prompt with static / semi-static / dynamic layers.

        Layer 1 — Static (built once, cached across turns):
          A. Identity (persona / SOUL.md, includes thinking principles)
          B. L0 skill list (name + description, always visible)
          C. L0 mode overview (one-liners for all three modes)
          D. Tool usage guide (rebuilds only when tools are registered)
          E. Wingman profile summary (rebuilds only on profile update)
          F. Platform hint (capabilities + tool routing + memory)
          → DYNAMIC BOUNDARY MARKER

        Layer 2 — Dynamic (rebuilt each turn):
          1. Active interaction mode (full prompt, only the current mode)
          2. L3 active skills (full prompt bodies)
          3. L2 keyword-matched skills (workflow snippets)
          4. OS / Shell / Python / working directory

        Layer 3 — Semi-static memory (rebuilt each turn, but stable within
        a session — may be split into a second cache tier later):
          5. Instruction files (CLAUDE.md upward traversal)
          6. Frozen memory blocks (MEMORY.md / persona.md / USER.md)

        Ephemeral (per-turn, injected as user message, not here):
          - Current date / time → <ephemeral-context>
          - ContextBrief 小纸条   → <memory-context>
        """
        static_parts: list[str] = []
        dynamic_parts: list[str] = []
        memory_parts: list[str] = []

        # ═══════════════════════════════════════════════════════════════════
        # LAYER 1 — STATIC (prefix-cache friendly)
        # ═══════════════════════════════════════════════════════════════════

        # A. Identity — personality, voice, judgment, boundaries (SOUL.md)
        static_parts.append(load_persona(self.config))

        # B. Available skills (L0: name + description, always visible)
        if self._skills_enabled:
            l0 = self.skill_manager.render_l0()
            if l0:
                static_parts.append(l0)

        # C. Mode overview (L0: one-liner for each mode, always visible)
        static_parts.append(MODE_L0_OVERVIEW)

        # D. Tool usage guide (auto-generated from registered tools;
        #    rebuilds only on tool registration, not every turn)
        static_parts.append(self.tool_registry.render_usage_guide())

        # Tell LLM about deferred tools that can be discovered via tool_search
        deferred_count = len(self.tool_registry.get_deferred_specs())
        if deferred_count > 0:
            static_parts.append(
                f"\n{deferred_count} additional tools are available but hidden to save context. "
                f"When the functionality you need is not found in the above list, "
                f"call `tool_search` with relevant keywords to discover them."
            )

        # E. Wingman profile summary (rebuilds on profile update)
        if self.memory_manager:
            try:
                ext_block = self.memory_manager.build_system_prompt()
                if ext_block:
                    static_parts.append(ext_block)
            except Exception:
                pass

        # F. Platform hint
        static_parts.append(_PLATFORM_HINT)

        # Dynamic boundary marker
        static_parts.append(self.SYSTEM_PROMPT_DYNAMIC_BOUNDARY)

        # ═══════════════════════════════════════════════════════════════════
        # LAYER 2 — DYNAMIC (rebuilt each turn)
        # ═══════════════════════════════════════════════════════════════════

        # 1. Interaction mode — full prompt for the active mode only
        mode_prompt = _get_mode_prompt(self._mode)
        if mode_prompt:
            dynamic_parts.append(mode_prompt)

        # 2. Active skills — L3 full prompt bodies
        l3_section = self.skill_manager.render_l3()
        if l3_section:
            dynamic_parts.append(l3_section)

        # 3. Contextual guidance — L2 workflow snippets on keyword match
        if self._skills_enabled and user_input:
            l2_section = self.skill_manager.render_l2(user_input, self._mode)
            if l2_section:
                dynamic_parts.append(l2_section)

        # 4. Environment (OS / Shell / Python / working directory)
        dynamic_parts.append(_build_environment_section())

        # ═══════════════════════════════════════════════════════════════════
        # LAYER 3 — SEMI-STATIC MEMORY (stable within a session)
        # ═══════════════════════════════════════════════════════════════════

        # 5. Instruction files (CLAUDE.md upward traversal)
        instruction_files = discover_instruction_files(self.config.project_dir)
        if instruction_files:
            memory_parts.append(render_instruction_files(instruction_files))

        # 6. Frozen memory snapshot (MEMORY.md / USER.md)
        mem_context = self.memory_store.freeze_for_system_prompt("memory")
        if mem_context:
            memory_parts.append(mem_context)

        return "\n\n".join(static_parts + dynamic_parts + memory_parts)

    def _build_api_messages(self) -> list[dict]:
        """Convert session messages to OpenAI-compatible API dicts.

        System prompt is passed separately to api_client.stream(), so it is
        never stored in session.messages.  System-role messages in the session
        are compaction continuation summaries — they must be included so the
        model can see the compressed history.
        """
        result: list[dict] = []
        for msg in self.session.messages:
            result.append(self._msg_to_api_dict(msg))
        return result

    def _msg_to_api_dict(self, msg: ConversationMessage) -> dict:
        """Convert a single ConversationMessage to OpenAI API format."""
        if msg.role == "tool":
            # Tool messages must be flat
            block = msg.blocks[0] if msg.blocks else ContentBlock.text_block("")
            return {
                "role": "tool",
                "tool_call_id": block.tool_use_id or "",
                "content": block.tool_output or "",
            }

        if msg.role == "assistant" and msg.has_tool_uses():
            tool_calls_api = []
            content = None
            for b in msg.blocks:
                if b.type == "text" and b.text:
                    content = b.text
                elif b.type == "tool_use":
                    tool_calls_api.append({
                        "id": b.tool_use_id,
                        "type": "function",
                        "function": {
                            "name": b.tool_name,
                            "arguments": b.tool_input,
                        },
                    })
            d: dict = {"role": "assistant", "content": content}
            if tool_calls_api:
                d["tool_calls"] = tool_calls_api
            return d

        # Simple text message
        text = msg.first_text() or ""
        return {"role": msg.role, "content": text}


def _build_environment_section() -> str:
    """Build the environment context section from the prompts/environment.md template."""
    import os
    import platform

    return _ENV_TEMPLATE.format(
        os=platform.system(),
        release=platform.release(),
        shell=os.environ.get("SHELL", os.environ.get("COMSPEC", "unknown")),
        cwd=Path.cwd(),
        python_version=platform.python_version(),
    )
