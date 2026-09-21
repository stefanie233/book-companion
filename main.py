#!/usr/bin/env python3
"""Book Companion — CLI entry point.

A reading companion / psychological agent with:
- Persona system (SOUL.md)
- DB-driven memory (memory_entries + refinement pipeline + MEMORY.md snapshot)
- Wingman subsystem (user profile, moment detection, self-reflection)
- Structured context compaction (no LLM needed)
- Slash commands for session control
- ReAct tool-use loop
- Async streaming output + cancel support

Adapted from claw-code and Hermes-Agent engineering patterns.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import warnings
from pathlib import Path

# Silence jieba's pkg_resources deprecation warning (third-party, out of our control)
warnings.filterwarnings("ignore", message="pkg_resources is deprecated", category=UserWarning)

logger = logging.getLogger(__name__)

from config import Config, load_config, save_settings
from commands import (
    SLASH_COMMANDS,
    SlashCommandSpec,
    build_dispatch_table,
    is_slash_command,
    parse_slash,
    render_help,
)
from memory_store import MemoryStore
from persona import load_persona, save_default_soul
from runtime import ConversationRuntime, OpenAIClient
from session import Session
from services.session_service import SessionService
from skills import SkillManager
from storage.db import BookDB
from storage.db_session import SessionDB
from hooks import HookContext, HookPipeline
from tools import create_builtin_registry


def _print_greeting(config: Config, mode_label: str = "沉浸伴读") -> None:
    """Print the startup greeting."""
    model = config.model
    print(f"""
╔══════════════════════════════════════════╗
║         书伴 Book Companion              ║
║         Your Reading & Growth Partner    ║
╚══════════════════════════════════════════╝

Model: {model}  |  Mode: {mode_label}
Type /help for available commands, /quit to exit.
""")


def _register_cleaning_hook(hook_pipeline: HookPipeline, books_db: Any) -> None:
    """Register a post_tool hook that auto-cleans chapters after import_book."""
    from hooks import HookContext
    from services.library_service import clean_book

    async def _on_post_tool(ctx: HookContext) -> bool:
        if ctx.tool_name != "import_book":
            return True
        if not ctx.tool_output or '"error"' in ctx.tool_output:
            return True  # import failed, skip

        # Parse book_id from the import summary line: "📁 EPUB → <book_id>"
        book_id = ""
        for line in ctx.tool_output.split("\n"):
            if "→" in line:
                book_id = line.rsplit("→", 1)[-1].strip().split()[0]
                break

        if not book_id:
            return True

        try:
            result = clean_book(books_db, book_id)
            removed = result.get("total_chars_before", 0) - result.get("total_chars_after", 0)
            print(
                f"  [cleaner] {result['cleaned_count']} chapters cleaned, "
                f"{removed:,} chars removed"
            )
        except Exception as exc:
            print(f"  [cleaner] cleaning failed for {book_id}: {exc}")

        return True

    hook_pipeline.register("post_tool", _on_post_tool)


def _wire_review_tool_context(tool_registry, session_db, wingman_db, api_client, config) -> None:
    """Wire database/api references into the review_sessions agent tool."""
    try:
        set_ctx = getattr(tool_registry, '_review_set_context', None)
        if set_ctx:
            set_ctx(session_db=session_db, wingman_db=wingman_db, api_client=api_client,
                    config=config, tool_registry=tool_registry)
    except Exception:
        pass


async def _dispatch_session_start(hook_pipeline, runtime, session_db, api_client, config) -> None:
    """Dispatch on_session_start hook with the full context."""
    from hooks import HookContext
    ctx = HookContext(
        session_id=runtime.session.session_id,
        extra={
            "session_db": session_db,
            "api_client": api_client,
            "config": config,
        },
    )
    try:
        await hook_pipeline.dispatch("on_session_start", ctx)
    except Exception:
        pass


def _setup_runtime(config: Config, memory_store: MemoryStore,
                   session: Session | None = None,
                   service: SessionService | None = None,
                   hook_pipeline: HookPipeline | None = None,
                   bookbank_db: Any = None,
                   session_db: Any = None,
                   memory_manager: Any = None,
                   api_client: Any = None,
                   wingman_db: Any = None,
                   context_recorder: Any = None) -> ConversationRuntime:
    """Create a ConversationRuntime with all dependencies wired up."""
    if api_client is None:
        api_client = OpenAIClient(config)

    tool_registry = create_builtin_registry(
        search_api_key=config.search_api_key,
        api_client=api_client,
        config=config,
        bookbank_db=bookbank_db,
        session_db=session_db,
        session_service=service,
        wingman_db=wingman_db,
    )

    config.skills_dir.mkdir(parents=True, exist_ok=True)
    skill_manager = SkillManager(config.skills_dir)
    skill_manager.set_tool_registry(tool_registry)

    if session is None:
        if service:
            session = service.create(model=config.model, mode="immersive")
        else:
            session = Session()
            session.model = config.model

    if service:
        session._service = service

    hook_pipeline = hook_pipeline or HookPipeline()
    skill_manager.set_hook_pipeline(hook_pipeline)

    # ── Register review skill hooks + tools at startup ──────────────────
    # Hooks (on_session_start, pre_turn) must be active from session start.
    # Tools (review_sessions) should be always available to the agent.
    skill_manager.discover()
    skill_manager.register_skill_hooks("review")
    skill_manager.register_skill_tools("review")

    # ── Register wingman memory tools as ToolSpec ─────────────────────
    # Sub-agents use a filtered ToolRegistry; memory tools must be
    # registered as ToolSpec (toolset="knowledge") to be visible.
    if memory_manager:
        from tools import ToolSpec, ToolExposure
        from permissions import PermissionMode

        def _make_memory_handler(tool_name: str, mm: Any):
            def handle(**kwargs):
                return mm.handle_tool_call(tool_name, kwargs)
            return handle

        # Memory tools are DIRECT (always visible to agent and sub-agents).
        # The review_sessions sub-agent needs all five to do its job.
        _DIRECT_MEMORY_TOOLS = {
            "memory_search", "memory_read",
            "memory_list_tags", "memory_list_by_tag",
            "memory_write",
        }
        for schema in memory_manager.get_all_tool_schemas():
            t_name = schema.get("name", "")
            if not t_name or not t_name.startswith("memory_"):
                continue
            is_direct = t_name in _DIRECT_MEMORY_TOOLS
            tool_registry.register(ToolSpec(
                name=t_name,
                toolset="knowledge",
                exposure=ToolExposure.DIRECT if is_direct else ToolExposure.DEFERRED,
                category="memory",
                description=schema.get("description", ""),
                parameters=schema.get("parameters", {}),
                execute=_make_memory_handler(t_name, memory_manager),
                permission_mode=PermissionMode.READ_ONLY,
            ))

    runtime = ConversationRuntime(
        session=session,
        api_client=api_client,
        tool_registry=tool_registry,
        config=config,
        memory_store=memory_store,
        skill_manager=skill_manager,
        hook_pipeline=hook_pipeline,
        memory_manager=memory_manager,
    )
    runtime.context_recorder = context_recorder
    return runtime


_TITLE_GEN_SYSTEM_PROMPT = (
    "你是一个会话标题生成器。根据用户的第一条消息，生成一个5-15字的精简标题。"
    "只输出标题本身，不要多余内容，不要引号，不要标点。"
)
_title_gen_in_progress: set[str] = set()


async def _generate_title_async(
    runtime: ConversationRuntime,
    session_service: SessionService,
) -> None:
    """Generate a session title from the first user message via AI."""
    sid = runtime.session.session_id
    if runtime.session.title is not None or sid in _title_gen_in_progress:
        return
    _title_gen_in_progress.add(sid)
    # Find first user text message
    first_text = ""
    for msg in runtime.session.messages:
        if msg.role == "user":
            for block in msg.blocks:
                if block.type == "text" and block.text:
                    first_text = block.text.replace("\n", " ").strip()
                    break
            if first_text:
                break
    if not first_text:
        return

    client = runtime.api_client
    try:
        title = await client.complete(_TITLE_GEN_SYSTEM_PROMPT, first_text)
        if title and len(title) > 200:
            title = title[:200]
        if title:
            runtime.session.title = title
            session_service.update_title(runtime.session.session_id, title)
    finally:
        _title_gen_in_progress.discard(sid)


async def _select_session_interactive(
    sessions: list,
    prompt_session,
) -> int | None:
    """Interactive session selector — completion-menu dropdown.

    Uses prompt_toolkit's Completion engine (same style as slash commands):
    sessions appear in a dropdown below the cursor, ↓/↑ to navigate,
    Enter to select + submit in one stroke.  Returns 0-based index or None.
    """
    n = len(sessions)
    if n == 0:
        return None

    try:
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.validation import Validator
    except ImportError:
        return None

    mode_labels = {"immersive": "沉浸", "overview": "分析", "divergent": "发散"}

    class SessionCompleter(Completer):
        def get_completions(self, document, complete_event):
            for i, s in enumerate(sessions):
                title = (s.title or "(新会话)").strip()
                mode = mode_labels.get(s.mode or "", "?")
                yield Completion(
                    str(i + 1),
                    start_position=-len(document.text),
                    display=f"{i + 1:2d}. {title:<45s} {mode:<4s} {s.message_count}条",
                    display_meta=f"{_time_label(s.time_updated)}  {s.model or '?'}",
                )

    bindings = KeyBindings()

    @bindings.add("enter")
    def _enter_submit(event):
        """Select and submit in one stroke when completion menu is open."""
        buf = event.current_buffer
        if buf.complete_state:
            buf.apply_completion(buf.complete_state.current_completion)
            buf.validate_and_handle()
        else:
            buf.validate_and_handle()

    @bindings.add("escape")
    def _escape_cancel(event):
        event.app.exit(result=None)

    def _time_label(ts: int) -> str:
        from datetime import datetime
        if not ts:
            return "?"
        dt = datetime.fromtimestamp(ts / 1000)
        now = datetime.now()
        if dt.date() == now.date():
            return dt.strftime("%H:%M")
        if (now.date() - dt.date()).days == 1:
            return "昨天"
        if dt.year == now.year:
            return dt.strftime("%m-%d")
        return dt.strftime("%Y-%m-%d")

    try:
        result = await prompt_session.prompt_async(
            message=[("class:prompt", "选择会话"), ("", " > ")],
            default=" ",  # Force completion menu to open immediately
            completer=SessionCompleter(),
            complete_while_typing=True,
            key_bindings=bindings,
            validator=Validator.from_callable(
                lambda text: text.strip().isdigit() and 1 <= int(text.strip()) <= n,
                error_message=f"输入 1-{n} 或 ↑↓ 选择",
            ),
            validate_while_typing=False,
        )
        stripped = result.strip()
        if not stripped or not stripped.isdigit():
            return None
        idx = int(stripped) - 1
        return idx if 0 <= idx < n else None
    except (KeyboardInterrupt, EOFError):
        return None


def main() -> None:
    """Main REPL entry point — delegates to async main."""
    asyncio.run(main_async())


async def main_async() -> None:
    """Async main REPL loop with streaming display, cancel support, and background tasks."""
    # Force UTF-8 output on Windows consoles that default to GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = load_config(Path.cwd())

    # Console provider bindings (data/config/providers.json) override .env —
    # apply BEFORE anything reads config fields, consumers hot-switch via the
    # shared mutable instance afterwards.
    from provider_store import ProviderStore
    provider_store = ProviderStore(config.providers_path)
    applied = provider_store.apply_to_config(config)
    if applied:
        print(f"  Console providers active: {', '.join(applied.values())}")

    # Memory subsystem file logging (memory_pipeline/wingman -> memory.log)
    from memory_logging import setup_memory_logging_from_config
    setup_memory_logging_from_config(config)

    config.memory_dir.mkdir(parents=True, exist_ok=True)
    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    save_default_soul(config)

    memory_store = MemoryStore(config)
    from memory_manager import MemoryManager
    from memory_store import BuiltinMemoryProvider
    memory_manager = MemoryManager()
    memory_manager.add_provider(BuiltinMemoryProvider(memory_store))

    # ── Wingman memory provider ─────────────────────────────────────────
    from wingman.service import WingmanMemoryProvider
    wingman_db_path = config.project_dir / "data" / "memory.db"
    wingman = WingmanMemoryProvider(wingman_db_path, config)
    memory_manager.add_provider(wingman)

    books_db = BookDB(config.books_db_path)
    session_db = SessionDB(config.sessions_db_path)
    books_db.initialize()
    session_db.initialize()
    # Hook pipeline
    hook_pipeline = HookPipeline()
    hooks_dir = config.project_dir / "hooks"
    hook_pipeline.discover(hooks_dir)
    if hook_pipeline.handler_count() > 0:
        print(f"Loaded {hook_pipeline.handler_count()} hook handlers "
              f"from {len(hook_pipeline.loaded_modules())} modules")

    # ── Post-import book cleaning hook ──────────────────────────────────
    _register_cleaning_hook(hook_pipeline, books_db)

    session_service = SessionService(session_db, config, hook_pipeline=hook_pipeline)

    # Create API client
    api_client = OpenAIClient(config)

    # ── Context flight recorder (debugging) — packaged in flight_recorder/ ──
    from flight_recorder import create_recorder
    context_recorder = create_recorder(config.project_dir)

    runtime = _setup_runtime(config, memory_store, service=session_service,
                             hook_pipeline=hook_pipeline,
                             bookbank_db=books_db, session_db=session_db,
                             memory_manager=memory_manager, api_client=api_client,
                             wingman_db=wingman._db,
                             context_recorder=context_recorder)

    # Initialize memory providers
    memory_manager.initialize_all(
        runtime.session.session_id,
        api_client=api_client,
        config=config,
    )

    app_context: dict = {
        "config": config,
        "runtime": runtime,
        "memory_store": memory_store,
        "session_service": session_service,
        "skill_manager": runtime.skill_manager,
        "session_db": session_db,
        "api_client": api_client,
        "new_session": False,
        "pending_load": None,
        "system_prompt_dirty": False,
        "prompt_session": None,  # Set after prompt_toolkit init
        "select_session_fn": _select_session_interactive,
    }

    # Wire review skill's tools context (after runtime has tool_registry)
    _wire_review_tool_context(runtime.tool_registry, session_db, wingman._db, api_client, config)

    dispatch = build_dispatch_table(app_context)
    cancel_event = asyncio.Event()

    # ── Background auto-save ──────────────────────────────────────────
    background_tasks: set[asyncio.Task] = set()

    async def auto_save_progress(interval: int = 300):
        while True:
            await asyncio.sleep(interval)
            try:
                # Only persist sessions with actual content
                if runtime.session.messages:
                    runtime.session.save()
            except Exception:
                pass

    save_task = asyncio.create_task(auto_save_progress())
    background_tasks.add(save_task)
    save_task.add_done_callback(background_tasks.discard)

    # ── Book Companion API server (background) ──────────────────────────
    from api_server import create_app as create_api_app
    from aiohttp import web

    api_app = create_api_app(books_db, runtime, session_service)
    api_app["api_client"] = api_client  # for /api/chat SSE endpoint
    api_app["config"] = config  # for BookGraph endpoints
    api_app["provider_store"] = provider_store  # for /api/console endpoints
    api_app["context_recorder"] = context_recorder  # for /api/context-log

    # ── Folder watcher for auto-import ─────────────────────────────────
    # Create & register BEFORE app setup (aiohttp disallows mutation after)
    try:
        from services.watcher_service import WatchService
        watcher = WatchService(books_db, Path("data/watch"))
        api_app["watcher"] = watcher
    except Exception as exc:
        print(f"  Watcher: not created ({exc})")

    # ── ASR 预热已改为后台任务，不再占用启动同步通道 ─────────────────
    # 原先此处 `await _warmup_asr()` 会阻塞整个启动：实测冷缓存下要 42.6s
    # （import 4.3s + 模型构造 2.6s + dummy 推理 35.7s，大头是冷盘 I/O）。
    # 期间 HTTP 尚未监听，start.bat 轮询 /api/health 一直拿不到响应，
    # 前端页面被整整拖延 40 多秒。
    #
    # 现在改由 api_site.start() 之后排后台任务（见下方），服务先可用。
    # 就绪前的 /api/asr 会在 _get_sense_voice 的双检锁上等待并复用，
    # 既不重复加载模型，也不会返回失败。

    # ── Tokenizer 预热：刻意留在 listen 之前（与 ASR 不同，勿照搬后台化）──
    # 实测 0.99s：import tokenizers 19ms + Tokenizer.from_file 0.93s + encode <1ms。
    # 那 0.93s 不是冷盘 I/O（顺序读 19.3MB 仅 13ms，1502MB/s），而是 Rust 解析
    # 全程持 GIL——lag 采样 max 945ms / P50 484ms，1s 内事件循环只被调度到 2 次，
    # 换 asyncio.to_thread 也照占不误。
    #
    # 所以后台化只是把这次冻结从"还没人连接的空窗"挪到"前端正在首屏拉数据"的
    # 时刻，净效果更差。留在这里 = 0.99s 纯冷启动代价、零请求竞争。
    # 且该 tokenizer 只服务 /api/context/usage（上下文计量环），不在首屏路径上。
    try:
        from context_meter import resolve_profile
        from tokenizer_service import count_tokens
        _prof = resolve_profile(getattr(config, "model", ""), config)
        if _prof["tokenizer_path"]:
            await asyncio.to_thread(
                count_tokens, "warmup", _prof["tokenizer_path"])
            print("  Tokenizer: 预热完成")
    except Exception:
        pass

    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", 8765, reuse_address=True)
    await api_site.start()
    print(f"  API server: http://127.0.0.1:8765")

    # ── ASR 后台预热（服务已在监听，此处不阻塞）──────────────────────
    # 排在 api_site.start() 之后：/api/health 立刻可应答，start.bat 随即
    # 打开前端；模型在后台线程慢慢加载，prefetch 失败也不影响服务可用性。
    from api_server import warm_asr_background
    _asr_warmup_task = asyncio.create_task(warm_asr_background())
    background_tasks.add(_asr_warmup_task)
    _asr_warmup_task.add_done_callback(background_tasks.discard)

    # Start watching after server is ready
    if api_app.get("watcher"):
        api_app["watcher"].start()
        print(f"  Watcher: {Path('data/watch').resolve()} (auto-import EPUB/MOBI)")

    # ── Prompt toolkit setup ──────────────────────────────────────────
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory

        def _scrub_surrogates(text: str) -> str:
            """Replace lone surrogate characters that break UTF-8 encoding."""
            return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

        class SafeFileHistory(FileHistory):
            """FileHistory that sanitises strings before persisting."""

            def store_string(self, string: str) -> None:
                super().store_string(_scrub_surrogates(string))

        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.styles import Style

        class SlashCompleter(Completer):
            """Auto-complete slash commands with descriptions."""

            def __init__(self, commands: list[SlashCommandSpec]):
                self._commands = commands

            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith("/"):
                    return
                prefix = text[1:]
                if " " in prefix:
                    return
                for cmd in self._commands:
                    if cmd.name.startswith(prefix):
                        yield Completion(
                            cmd.name,
                            start_position=-len(prefix),
                            display=cmd.name,
                            display_meta=cmd.summary,
                        )
                    for alias in cmd.aliases:
                        if alias.startswith(prefix):
                            yield Completion(
                                alias,
                                start_position=-len(prefix),
                                display=f"{alias} → /{cmd.name}",
                                display_meta=cmd.summary,
                            )

        # ── Tool-call display labels ──────────────────────────────────
        TOOL_LABELS: dict[str, str] = {
            "read_book": "正在翻开书页...",
            "library_read": "正在翻阅章节...",
            "book_info": "正在查看目录...",
            "library_list": "正在清点书库...",
            "library_search": "正在翻箱倒柜...",
            "book_search": "正在逐页查找...",
            "import_book": "正在搬运新书...",
            "memory_search": "正在回忆往事...",
            "memory_read": "正在翻阅笔记...",
            "memory_write": "正在记下心得...",
            "memory_list_tags": "正在整理标签...",
            "memory_list_by_tag": "正在按图索骥...",
            "review_sessions": "正在复盘过往...",
            "web_search": "正在打探消息...",
            "web_fetch": "正在查阅资料...",
        }

        def _tool_label(name: str) -> str:
            """Map a tool name to a quirky Chinese display label."""
            return TOOL_LABELS.get(name, f"正在{name}...")

        # ── Chunk display processor ───────────────────────────────────
        _think_state = {"inside": False, "hint_shown": False}

        def _process_chunk(raw: str) -> str:
            """Transform a raw stream chunk for display.

            - Collapses <think>...</think> into a dim hint
            - Adds paragraph break after closing </think>
            - Replaces tool-call markers with fun labels
            """
            out = raw

            # Tool-call markers: [🔧 tool_name...] → fun label
            m = __import__('re').match(r"\n\[🔧 ([a-z_]+)\.\.\.\]\n", out)
            if m:
                return f"\n  📦 {_tool_label(m.group(1))}\n"

            # Think tag handling
            if _think_state["inside"]:
                if "</think>" in out:
                    _think_state["inside"] = False
                    _think_state["hint_shown"] = False
                    idx = out.find("</think>")
                    suffix = out[idx + 8:]
                    return "\n" + suffix.lstrip() if suffix.strip() else ""
                # Suppress think content while inside
                return ""

            if "<think>" in out:
                _think_state["inside"] = True
                _think_state["hint_shown"] = True
                idx = out.find("<think>")
                prefix = out[:idx] if idx > 0 else ""
                after_tag = out[idx + 7:]
                # Check if </think> is also in this chunk (fast thinker)
                if "</think>" in after_tag:
                    _think_state["inside"] = False
                    _think_state["hint_shown"] = False
                    end_idx = after_tag.find("</think>")
                    suffix = after_tag[end_idx + 8:]
                    return (prefix + "\n  💭 思考完毕\n" + suffix.lstrip()) if suffix.strip() else (prefix + "\n  💭 思考完毕")
                return prefix + "\n  💭 思考中..."

            return out

        def _status_bar() -> str:
            mode_label = runtime.current_mode_label
            # Quick-ref slash commands
            shortcuts = "/help /model /mode /memory /sessions"
            return (
                f" {shortcuts}  |  模式: {mode_label}  |  "
                f"消息: {len(runtime.session.messages)}  |  "
                f"~{runtime.session.estimate_tokens()} tokens"
            )

        # Ctrl+C key binding: cancel during streaming, quit when idle
        bindings = KeyBindings()

        @bindings.add(Keys.ControlC)
        def _handle_ctrl_c(event):
            if runtime._streaming:
                cancel_event.set()
                event.app.invalidate()
            else:
                event.app.exit(result="__QUIT__")

        style = Style.from_dict({
            "prompt": "#00afff bold",
            "bottom-toolbar": "bg:#1a1a2e #888888",
            "completion-menu.completion": "bg:#1e1e3a #cccccc",
            "completion-menu.completion.current": "bg:#00afff #000000",
            "completion-menu.meta": "bg:#2a2a4a #aaaaaa",
        })

        history_path = config.project_dir / "data" / ".input_history"

        # One-shot cleanup: scrub any surrogate-tainted entries from
        # previous sessions so prompt_toolkit's UTF-8 write won't crash.
        if history_path.exists():
            try:
                raw = history_path.read_text(encoding="utf-8", errors="replace")
                cleaned = _scrub_surrogates(raw)
                if cleaned != raw:
                    history_path.write_text(cleaned, encoding="utf-8")
            except Exception:
                pass

        prompt_session = PromptSession(
            history=SafeFileHistory(str(history_path)),
            completer=SlashCompleter(SLASH_COMMANDS),
            key_bindings=bindings,
            bottom_toolbar=_status_bar,
            style=style,
            complete_while_typing=True,
        )
        app_context["prompt_session"] = prompt_session
        use_rich_input = True
    except ImportError:
        use_rich_input = False
    except Exception as exc:
        # NoConsoleScreenBufferError or other console init failures
        # fall back to plain input()
        use_rich_input = False

    _print_greeting(config, runtime.current_mode_label)

    # ── Main REPL loop ────────────────────────────────────────────────

    while True:
        try:
            if use_rich_input:
                user_input = await prompt_session.prompt_async([
                    ("class:prompt", "书伴 "),
                    ("", "> "),
                ])
                user_input = user_input.strip()
                if user_input == "__QUIT__":
                    print("\nGoodbye!")
                    break
            else:
                user_input = (await asyncio.to_thread(input, "书伴> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        # ── Slash command detection ───────────────────────────────────

        if is_slash_command(user_input):
            cmd_name, args = parse_slash(user_input)
            if not cmd_name:
                continue

            handler = dispatch.get(cmd_name)
            if handler is None:
                print(f"Unknown command: /{cmd_name}. Type /help for available commands.")
                continue

            # pre_command hook
            await hook_pipeline.dispatch("pre_command", HookContext(
                command_name=cmd_name, command_args=args,
                session_id=runtime.session.session_id, runtime=runtime,
            ))

            try:
                if asyncio.iscoroutinefunction(handler):
                    response, should_exit = await handler(args, app_context)
                else:
                    response, should_exit = handler(args, app_context)
            except Exception as exc:
                print(f"Command error: {exc}")
                continue

            # post_command hook
            await hook_pipeline.dispatch("post_command", HookContext(
                command_name=cmd_name, command_args=args,
                command_response=response or "",
                session_id=runtime.session.session_id, runtime=runtime,
            ))

            if response:
                print(f"\n{response}\n")

            if should_exit:
                break

            # Handle side effects
            if app_context.get("new_session"):
                runtime = _setup_runtime(config, memory_store, service=session_service,
                                                 bookbank_db=books_db, session_db=session_db,
                                                 memory_manager=memory_manager,
                                                 api_client=api_client, wingman_db=wingman._db,
                                                 context_recorder=context_recorder)
                app_context["runtime"] = runtime
                app_context["new_session"] = False
                app_context["system_prompt_dirty"] = False
                dispatch = build_dispatch_table(app_context)
                memory_store.load_from_disk()
                memory_manager.on_session_switch(runtime.session.session_id)
                memory_manager.initialize_all(
                    runtime.session.session_id,
                    api_client=api_client, config=config,
                )
                print("New session started.")

                # ── Dispatch on_session_start hook ──────────────────────
                _dispatch_session_start(hook_pipeline, runtime, session_db, api_client, config)

            if app_context.get("pending_load"):
                loaded_session: Session = app_context["pending_load"]
                runtime.session.save()
                runtime = _setup_runtime(config, memory_store, loaded_session, service=session_service,
                                         bookbank_db=books_db, session_db=session_db,
                                         memory_manager=memory_manager, api_client=api_client, wingman_db=wingman._db,
                                         context_recorder=context_recorder)
                app_context["runtime"] = runtime
                app_context["system_prompt_dirty"] = False
                dispatch = build_dispatch_table(app_context)
                memory_manager.on_session_switch(loaded_session.session_id)
                memory_manager.initialize_all(
                    loaded_session.session_id,
                    api_client=api_client, config=config,
                )
                app_context["pending_load"] = None

                # ── Dispatch on_session_start hook ──────────────────────
                _dispatch_session_start(hook_pipeline, runtime, session_db, api_client, config)

            if app_context.get("system_prompt_dirty"):
                runtime = _setup_runtime(config, memory_store, runtime.session, service=session_service,
                                         bookbank_db=books_db, session_db=session_db,
                                         memory_manager=memory_manager, api_client=api_client, wingman_db=wingman._db,
                                         context_recorder=context_recorder)
                app_context["runtime"] = runtime
                app_context["system_prompt_dirty"] = False
                dispatch = build_dispatch_table(app_context)

            continue

        # ── Normal conversation turn ──────────────────────────────────

        print()  # blank line before response
        cancel_event.clear()

        try:
            async for chunk in runtime.run_turn(user_input, cancel_event):
                print(_process_chunk(chunk), end="", flush=True)
        except Exception as exc:
            print(f"[Runtime error: {exc}]")
            continue

        # Post-turn feedback
        if runtime._compacted_this_turn > 0:
            print(f"\n[Auto-compacted {runtime._compacted_this_turn} messages]")
        else:
            print()

        # Async title generation after first turn
        if runtime.session.title is None:
            # First user message — persist the session now (it was created
            # in-memory only, not yet in DB, to avoid empty-session clutter).
            runtime.session.save()
            task = asyncio.create_task(
                _generate_title_async(runtime, session_service),
            )
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

    # ── Cleanup ───────────────────────────────────────────────────────
    save_task.cancel()
    try:
        await save_task
    except asyncio.CancelledError:
        pass
    # Only persist sessions with actual content
    if runtime.session.messages:
        runtime.session.save()

    # ── End-of-session refinement via sub-agent ────────────────────────
    # DISABLED — the sub-agent review on /quit is too aggressive and
    # produces low-quality memories when the conversation is short or
    # technical. Manual review via review_sessions tool is the reliable
    # primary path. Keep code for future re-evaluation.
    if False and runtime.session.messages:
        try:
            from sub_agent import SubAgentRunner

            # Format session messages as simple text
            session_lines: list[str] = []
            for m in runtime.session.messages:
                if m.role == "user":
                    for b in m.blocks:
                        if b.type == "text" and b.text:
                            session_lines.append(f"读者：{b.text.strip()}")
                elif m.role == "assistant":
                    for b in m.blocks:
                        if b.type == "text" and b.text:
                            session_lines.append(f"书伴：{b.text.strip()}")
            session_text = "\n\n".join(session_lines)

            if session_text.strip():
                runner = SubAgentRunner(api_client, tool_registry, config)
                await asyncio.wait_for(
                    runner.run(
                        agent_type="reader",
                        description=f"Review session {runtime.session.session_id[:12]}",
                        prompt=(
                            "回顾这段对话，将值得记住的事情提炼为 1-3 条综合性记忆。"
                            "合并相关发现，不要逐条罗列——一条记忆 = 一个完整主题的洞察。\n\n"
                            "步骤：\n"
                            "1. 归纳核心主题（不超过 3 个）\n"
                            "2. 用 memory_search 检查重复\n"
                            "3. 用 memory_write 写入（content 要包含足够上下文，"
                            "keywords 用空格分隔的关键词用于搜索）\n"
                            "4. 为每条记忆添加合适的 tags\n\n"
                            f"对话内容：\n{session_text}"
                        ),
                    ),
                    timeout=30,
                )
                # Verify memories were produced → done or TBD
                linked = wingman._db.get_memories_by_session(
                    runtime.session.session_id, limit=1,
                )
                if linked:
                    session_db.mark_reviewed(runtime.session.session_id)
                else:
                    session_db.mark_tbd(runtime.session.session_id)
        except asyncio.TimeoutError:
            logger.debug("End-of-session review timed out")
        except Exception:
            logger.debug("End-of-session review failed", exc_info=True)

    # Graph evolution: fallback extraction at session end
    if runtime and getattr(runtime, 'current_book_id', ''):
        try:
            from bookgraph.incremental import messages_to_dialogue_text, extract_from_dialogue
            from storage.graph_db import GraphDB
            dialogue = messages_to_dialogue_text(runtime.session.messages)
            if dialogue.strip():
                _gpath = Path(getattr(runtime, 'current_graph_path', '') or '')
                if _gpath and _gpath.exists():
                    _gdb = GraphDB(_gpath)
                    try:
                        _cnt = await extract_from_dialogue(
                            runtime.current_book_id, _gdb, runtime.api_client, dialogue,
                        )
                        if _cnt:
                            logger.info("Graph evolution: %d observations from session-end fallback", _cnt)
                    finally:
                        _gdb.close()
        except Exception:
            logger.debug("Session-end graph extraction failed", exc_info=True)

    memory_manager.on_session_end([])

    # Shut down watcher
    if api_app and api_app.get("watcher"):
        api_app["watcher"].stop()

    # Shut down API server
    await api_runner.cleanup()

    # Shut down memory providers (closes wingman DB internally)
    memory_manager.shutdown_all()
    session_db.close()
    books_db.close()


if __name__ == "__main__":
    main()
