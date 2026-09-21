"""Hook pipeline — async middleware for lifecycle events.

Hook handlers are Python modules in this directory that export a
``register(pipeline)`` function. Each handler is an async callable
receiving a HookContext and returning a bool (False cancels the chain).

Usage::

    pipeline = HookPipeline()
    pipeline.discover(Path("hooks"))
    await pipeline.dispatch("pre_turn", HookContext(user_input="hello"))
"""

from __future__ import annotations

import importlib.util
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

# ── Data model ──────────────────────────────────────────────────────────


@dataclass
class HookContext:
    """Context passed to every hook handler.

    Fields are populated by the dispatcher based on which hook is firing.
    Not all fields are available on every hook — handlers should guard
    with getattr / hasattr.
    """

    # Conversation
    user_input: str = ""
    assistant_output: str = ""
    mode: str = ""

    # Session
    session_id: str = ""

    # Command dispatch
    command_name: str = ""
    command_args: str = ""
    command_response: str = ""

    # Tool execution
    tool_name: str = ""
    tool_input: str = ""
    tool_output: str = ""

    # Compaction
    compacted_messages: int = 0

    # Streaming
    stream_active: bool = False

    # Runtime reference (populated by ConversationRuntime before dispatch)
    runtime: Any | None = None

    # Arbitrary extension data
    extra: dict[str, Any] = field(default_factory=dict)


# Handler signature: async (HookContext) -> bool
HookHandler = Callable[[HookContext], Awaitable[bool]]


# ── Built-in hook names ─────────────────────────────────────────────────

HOOK_POINTS = (
    "pre_turn",
    "post_turn",
    "pre_command",
    "post_command",
    "pre_tool",
    "post_tool",
    "on_message",
    "on_stream_start",
    "on_stream_end",
    "on_compaction",
    "on_session_save",
    "on_session_load",
    "on_session_start",
    "on_mode_change",
)


# ── Pipeline ────────────────────────────────────────────────────────────


class HookPipeline:
    """Ordered chain of hook handlers per lifecycle point."""

    def __init__(self):
        self._chains: dict[str, list[HookHandler]] = {hp: [] for hp in HOOK_POINTS}
        self._loaded_modules: list[str] = []

    # ── Registration ─────────────────────────────────────────────────

    def register(self, hook_name: str, handler: HookHandler) -> None:
        """Register a single handler for a hook point."""
        if hook_name not in self._chains:
            raise ValueError(f"Unknown hook point: {hook_name}")
        self._chains[hook_name].append(handler)

    def register_module(self, module_path: Path) -> None:
        """Load a Python module and call its ``register(hook_pipeline)`` function."""
        name = f"_hook_{module_path.stem}"
        spec = importlib.util.spec_from_file_location(name, module_path)
        if spec is None or spec.loader is None:
            print(f"[hooks] Cannot load: {module_path}")
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "register"):
            mod.register(self)
            self._loaded_modules.append(module_path.stem)
        else:
            print(f"[hooks] Module {module_path.stem} has no register() function")

    def discover(self, hooks_dir: Path) -> None:
        """Discover and load all hook modules from a directory."""
        if not hooks_dir.exists():
            return
        for entry in sorted(hooks_dir.iterdir()):
            if entry.suffix == ".py" and not entry.name.startswith("_"):
                self.register_module(entry)

    # ── Dispatch ────────────────────────────────────────────────────

    async def dispatch(self, hook_name: str, ctx: HookContext) -> bool:
        """Fire all handlers for a hook point. Returns False if any handler cancelled."""
        if hook_name not in self._chains:
            return True
        for handler in self._chains[hook_name]:
            try:
                ok = await handler(ctx)
                if not ok:
                    return False
            except Exception:
                traceback.print_exc()
                # Continue — one broken hook shouldn't break the chain
        return True

    # ── Queries ─────────────────────────────────────────────────────

    def handler_count(self, hook_name: str | None = None) -> int:
        """Return the number of registered handlers."""
        if hook_name:
            return len(self._chains.get(hook_name, []))
        return sum(len(v) for v in self._chains.values())

    def loaded_modules(self) -> list[str]:
        """Return names of successfully loaded hook modules."""
        return list(self._loaded_modules)
