"""Slash command system.

Declarative SlashCommandSpec table + dict-based dispatch.
Detection: starts with '/' and first word has no additional '/' chars.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from display_helpers import format_resume_report, format_sessions_numbered, message_preview, render_conversation_tail


@dataclass
class SlashCommandSpec:
    """Static metadata for a slash command."""
    name: str
    aliases: list[str]
    summary: str
    argument_hint: str | None = None


# ── Command specs table (drives /help output and dispatch) ──────────────


SLASH_COMMANDS: list[SlashCommandSpec] = [
    SlashCommandSpec("help", [], "显示可用指令列表"),
    SlashCommandSpec("model", [], "查看或切换当前模型", "[模型名]"),
    SlashCommandSpec("immersive", ["immerse"], "切换到沉浸伴读模式（不剧透）"),
    SlashCommandSpec("overview", [], "切换到俯瞰分析模式（全局视角）"),
    SlashCommandSpec("divergent", ["diverge"], "切换到发散探索模式（联网搜索与外部知识）"),
    SlashCommandSpec("mode", [], "显示当前互动模式"),
    SlashCommandSpec("compact", [], "手动压缩会话历史"),
    SlashCommandSpec("clear", ["new"], "开始新会话", "[--confirm]"),
    SlashCommandSpec("memory", [], "查看、搜索、添加、替换或删除记忆", "[add|search|replace|remove]"),
    SlashCommandSpec("persona", [], "查看或重载代理人格"),
    SlashCommandSpec("cost", ["usage"], "显示当前会话 token 用量"),
    SlashCommandSpec("save", [], "强制保存当前会话"),
    SlashCommandSpec("sessions", ["session"], "列出、加载或管理会话",
                     "[list/<编号>/<ID>/latest/delete/search <关键词>]"),
    SlashCommandSpec("permissions", [], "查看或设置权限模式", "[mode]"),
    SlashCommandSpec("skill", [], "列出、激活或关闭技能", "[技能名] [on|off]"),
    SlashCommandSpec("quit", ["exit", "q"], "保存会话并退出"),
    SlashCommandSpec("fork", [], "从现有会话分叉出新分支", "[会话ID] [--name <分支名>]"),
    SlashCommandSpec("resume", [], "恢复最近的会话"),
    SlashCommandSpec("review", [], "审查历史会话，提取值得记录的偏好/洞察/修正/边界", "[会话ID]"),
    SlashCommandSpec("config", [], "查看或修改配置", "[<键> [<值>]]"),
    SlashCommandSpec("export", [], "导出会话为 Markdown", "[--format json|markdown|txt]"),
    SlashCommandSpec("graph_init", [], "为书籍构建知识图谱", "<book_id> [--batch-size N]"),
    SlashCommandSpec("graph_status", [], "查看图谱提取进度", "[book_id]"),
    SlashCommandSpec("graph_stats", [], "查看图谱统计", "[book_id]"),
    SlashCommandSpec("graph_reset", [], "重置图谱（删除重建）", "<book_id> [--confirm]"),
]


# ── Slash command detection ─────────────────────────────────────────────


def is_slash_command(text: str) -> bool:
    """Check if text looks like a slash command (a single '/cmd' token, not a path)."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    first_word = stripped.split()[0]
    # Paths like /Users/foo are not slash commands
    return first_word.count("/") == 1


def parse_slash(text: str) -> tuple[str, list[str]]:
    """Parse a slash command into (command_name, args)."""
    parts = text.strip()[1:].split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


# ── Display helpers ─────────────────────────────────────────────────────


def render_help() -> str:
    """Render the help text from SLASH_COMMANDS table."""
    lines = ["Available slash commands:", ""]
    width = max(len(cmd.name) + (len(", ".join(cmd.aliases)) + 2 if cmd.aliases else 0) + 1
                for cmd in SLASH_COMMANDS)
    for cmd in SLASH_COMMANDS:
        alias_str = f" ({', '.join(cmd.aliases)})" if cmd.aliases else ""
        hint = f" {cmd.argument_hint}" if cmd.argument_hint else ""
        lines.append(f"  /{cmd.name}{alias_str:<{width - len(cmd.name)}}  {cmd.summary}{hint}")
    return "\n".join(lines)


# ── Handler type ────────────────────────────────────────────────────────

# Handler: receives (args, context_dict) → returns (response_text, should_exit)
CommandHandler = Callable[[list[str], dict[str, Any]], tuple[str, bool]]


def build_dispatch_table(app_context: dict[str, Any]) -> dict[str, CommandHandler]:
    """Build the command dispatch table using the app context."""
    config = app_context["config"]

    def _cmd_help(args: list[str], ctx: dict) -> tuple[str, bool]:
        return render_help(), False

    def _cmd_model(args: list[str], ctx: dict) -> tuple[str, bool]:
        if args:
            config.model = args[0]
            from config import save_settings
            save_settings(config)
            return f"Model switched to: {args[0]}", False
        return f"Current model: {config.model}", False

    def _cmd_compact(args: list[str], ctx: dict) -> tuple[str, bool]:
        runtime = ctx.get("runtime")
        if not runtime:
            return "No active session to compact.", False
        result = runtime.manual_compact()
        if result.removed_message_count > 0:
            return (
                f"Compacted {result.removed_message_count} messages.\n"
                f"Summary:\n{result.formatted_summary}"
            ), False
        return "Session is below compaction threshold — nothing to compact.", False

    def _cmd_clear(args: list[str], ctx: dict) -> tuple[str, bool]:
        if "--confirm" not in args:
            return "Use '/clear --confirm' to start a fresh session (current session will be saved).", False
        runtime = ctx.get("runtime")
        if runtime:
            runtime.session.save()
        ctx["new_session"] = True
        return "Starting a fresh session...", False

    def _cmd_memory(args: list[str], ctx: dict) -> tuple[str, bool]:
        store = ctx.get("memory_store")
        if not store:
            return "Memory store not available.", False

        if args and args[0] == "search":
            query = " ".join(args[1:])
            if not query:
                return "Usage: /memory search <关键词>", False
            results = store.search(query)
            if not results:
                return f"(no results for '{query}')", False
            lines = [f"## Memory search: '{query}'", ""]
            for target, text, idx in results:
                lines.append(f"[{target} #{idx}] {text}")
            return "\n".join(lines), False

        if args and args[0] == "add":
            # /memory add [user|agent] <text> — explicit target, or auto-detect
            if len(args) >= 2 and args[1] in ("user", "agent", "memory"):
                target = {"user": "user", "agent": "memory", "memory": "memory"}[args[1]]
                text = " ".join(args[2:])
            else:
                text = " ".join(args[1:])
                # Heuristic: treat as user profile when about preferences/habits
                target = "user" if any(w in text for w in ["用户", "喜欢", "偏好", "习惯", "user"]) else "memory"
            if not text:
                return "Usage: /memory add [user|agent] <text>", False
            result = store.add(target, text)
            if result["success"]:
                return f"Added to {target}: {text}", False
            return result.get("error", "Failed to add entry."), False

        if args and args[0] == "replace":
            # /memory replace <old_text_substring> <new_text>
            # First arg after "replace" is old_text, rest is new_content
            if len(args) < 3:
                return "Usage: /memory replace <old_text_substring> <new_text>", False
            old_text = args[1]
            new_content = " ".join(args[2:])
            target = "memory"  # default to memory, user must specify if needed
            result = store.replace(target, old_text, new_content)
            if result["success"]:
                return f"Replaced in {target}.", False
            return result.get("error", "Failed to replace entry."), False

        if args and args[0] == "remove":
            # /memory remove <substring>
            if len(args) < 2:
                return "Usage: /memory remove <substring>", False
            old_text = " ".join(args[1:])
            result = store.remove("memory", old_text)
            if result["success"]:
                return f"Removed from memory.", False
            return result.get("error", "Failed to remove entry."), False

        lines = ["## Memories", ""]
        if store.memory_entries:
            lines.append("### Agent Knowledge (MEMORY.md)")
            for i, entry in enumerate(store.memory_entries, 1):
                lines.append(f"  {i}. {entry}")
        if store.user_entries:
            lines.append("")
            lines.append("### User Profile (USER.md)")
            for i, entry in enumerate(store.user_entries, 1):
                lines.append(f"  {i}. {entry}")
        if not store.memory_entries and not store.user_entries:
            lines.append("(no memories yet)")
        return "\n".join(lines), False

    def _cmd_persona(args: list[str], ctx: dict) -> tuple[str, bool]:
        from persona import load_persona
        if args and args[0] == "reload":
            ctx["system_prompt_dirty"] = True
            return "Persona reloaded from SOUL.md.", False
        return load_persona(config), False

    def _cmd_cost(args: list[str], ctx: dict) -> tuple[str, bool]:
        runtime = ctx.get("runtime")
        if not runtime:
            return "No active session.", False
        est_tokens = runtime.session.estimate_tokens()
        msg_count = len(runtime.session.messages)
        return (
            f"Session stats:\n"
            f"  Messages: {msg_count}\n"
            f"  Estimated tokens: ~{est_tokens}\n"
            f"  Compaction count: {runtime.session.compaction.count if runtime.session.compaction else 0}"
        ), False

    def _cmd_save(args: list[str], ctx: dict) -> tuple[str, bool]:
        runtime = ctx.get("runtime")
        if not runtime:
            return "No active session.", False
        runtime.session.save()
        return "Session saved.", False

    def _cmd_sessions(args: list[str], ctx: dict) -> tuple[str, bool]:
        """Unified sessions command: list / load / delete / search.

        Usage:
          /sessions              → 列出所有会话（编号列表）
          /sessions list         → 同上
          /sessions <编号>        → 加载指定编号的会话
          /sessions <会话ID>      → 按 ID 加载会话
          /sessions latest       → 加载最近的会话
          /sessions delete <编号> → 删除指定编号的会话
          /sessions search <关键词> → 搜索会话标题
        """
        service = ctx.get("session_service")
        if not service:
            return "Session service not available.", False
        service.ensure_migrated()

        # ── No args: list all ──────────────────────────────────────────
        if not args:
            sessions = service.list(limit=100)
            return format_sessions_numbered(sessions), False

        cmd = args[0]

        # ── /sessions list ─────────────────────────────────────────────
        if cmd == "list":
            sessions = service.list(limit=100)
            return format_sessions_numbered(sessions), False

        # ── /sessions delete <ref> ─────────────────────────────────────
        if cmd == "delete" and len(args) >= 2:
            ref = args[1]
            if ref.isdigit():
                idx = int(ref) - 1
                all_sessions = service.list(limit=100)
                if idx < 0 or idx >= len(all_sessions):
                    return f"编号超出范围（1-{len(all_sessions)}）。", False
                sid = all_sessions[idx].id
            else:
                sid = ref.strip("<>")
            service.delete(sid)
            return f"会话已删除：{sid}", False

        # ── /sessions search <keyword> ─────────────────────────────────
        if cmd == "search" and len(args) >= 2:
            keyword = " ".join(args[1:])
            sessions = service.list(search=keyword, limit=100)
            return format_sessions_numbered(sessions), False

        # ── /sessions <编号> — load by number ─────────────────────────
        if cmd.isdigit():
            idx = int(cmd) - 1
            all_sessions = service.list(limit=100)
            if idx < 0 or idx >= len(all_sessions):
                return f"编号超出范围（1-{len(all_sessions)}）。", False
            summary = all_sessions[idx]
            session = service.load_messages(summary.id)
            if session is None:
                return f"会话读取失败：{summary.id}", False
            ctx["pending_load"] = session
            return format_resume_report(session, summary), False

        # ── /sessions latest / last / recent ──────────────────────────
        if cmd.lower() in ("latest", "last", "recent"):
            sessions = service.list(limit=1)
            if not sessions:
                return "没有找到可恢复的会话。", False
            summary = sessions[0]
            session = service.load_messages(summary.id)
            if session is None:
                return f"会话读取失败：{summary.id}", False
            ctx["pending_load"] = session
            return format_resume_report(session, summary), False

        # ── /sessions <ID> — load by session ID ───────────────────────
        ref = cmd.strip("<>")
        info = service.get(ref)
        if not info:
            return f"找不到会话：{ref}。使用 /sessions 查看可用会话。", False
        session = service.load_messages(info.id)
        if session is None:
            return f"会话读取失败：{ref}", False
        ctx["pending_load"] = session
        return format_resume_report(session, info), False

    async def _cmd_sessions_async(args: list[str], ctx: dict) -> tuple[str, bool]:
        """Async handler — arrow-key session picker when no args."""
        service = ctx.get("session_service")
        if not service:
            return "Session service not available.", False
        service.ensure_migrated()

        # With args → delegate to sync handler
        if args:
            return _cmd_sessions(args, ctx)

        # No args → numbered list + completion-menu dropdown selector
        all_sessions = service.list(limit=55)
        sessions = [s for s in all_sessions if s.message_count > 0]
        if not sessions:
            return "暂无历史对话。", False

        select_fn = ctx.get("select_session_fn")
        prompt_session = ctx.get("prompt_session")

        # Fallback: plain input mode (no prompt_toolkit)
        if not prompt_session or not select_fn:
            return format_sessions_numbered(sessions), False

        # Print the list so it's always visible, even if completion menu fails
        print(format_sessions_numbered(sessions))
        print()

        idx = await select_fn(sessions, prompt_session)
        if idx is None or idx < 0 or idx >= len(sessions):
            return "", False  # Cancelled

        selected = sessions[idx]
        session = service.load_messages(selected.id)
        if not session:
            return f"会话读取失败：{selected.id}", False

        ctx["pending_load"] = session

        # Immersive display
        mode_labels = {"immersive": "沉浸伴读", "overview": "俯瞰分析", "divergent": "发散探索"}
        mode_label = mode_labels.get(selected.mode, selected.mode)
        lines = [
            "═══════════════════════════════════════",
            f"  已加载：{selected.title or '(无标题)'}",
            f"  ID：{selected.id}",
            f"  模型：{selected.model or '?'}  |  模式：{mode_label}",
            "═══════════════════════════════════════",
            "",
        ]
        lines.append(render_conversation_tail(session))
        return "\n".join(lines), False

    def _cmd_fork(args: list[str], ctx: dict) -> tuple[str, bool]:
        service = ctx.get("session_service")
        if not service:
            return "Session service not available.", False
        if not args:
            return "用法：/fork <会话ID> [--name <分支名>]", False

        session_id = args[0].strip("<>")
        branch_name = None
        if "--name" in args:
            idx = args.index("--name")
            if idx + 1 < len(args):
                branch_name = args[idx + 1]

        child = service.fork(session_id, branch_name=branch_name)
        if child is None:
            return f"找不到源会话：{session_id}", False

        info = service.get(child.session_id)
        report = format_resume_report(child, info) if info else f"已分叉：{child.session_id}"
        return f"从 {session_id} 分叉出新会话：{child.session_id}\n\n{report}", False

    def _cmd_permissions(args: list[str], ctx: dict) -> tuple[str, bool]:
        runtime = ctx.get("runtime")
        if not runtime:
            return "No active session.", False

        # /permissions list — show tool-level overrides
        if args and args[0] == "list":
            policy = runtime.permission_policy
            lines = [f"## Permission Mode: {policy.active_mode.as_str()}", ""]
            tool_reqs = runtime.tool_registry.get_permission_requirements()
            lines.append("### Tool Requirements")
            for name, mode in sorted(tool_reqs.items()):
                overridden = mode != policy.active_mode
                marker = " *" if overridden else ""
                lines.append(f"  {name}: {mode.as_str()}{marker}")
            return "\n".join(lines), False

        if args:
            new_mode = args[0].lower()
            from permissions import PermissionMode
            try:
                mode = PermissionMode.from_str(new_mode)
                runtime.permission_policy.set_mode(mode)
                return f"Permission mode set to: {mode.as_str()}", False
            except Exception:
                return f"Invalid mode. Options: read-only, workspace-write, danger-full-access, allow", False
        return f"Current permission mode: {runtime.permission_policy.active_mode.as_str()}", False

    def _cmd_quit(args: list[str], ctx: dict) -> tuple[str, bool]:
        runtime = ctx.get("runtime")
        if runtime:
            runtime.session.save()
        return "Goodbye!", True

    def _make_mode_handler(mode_name: str):
        """Factory: create a handler that switches to a specific interaction mode."""
        from runtime import MODE_LABELS
        label = MODE_LABELS.get(mode_name, mode_name)

        async def handler(args: list[str], ctx: dict) -> tuple[str, bool]:
            runtime = ctx.get("runtime")
            if not runtime:
                return "No active session.", False
            if not await runtime.set_mode(mode_name):
                return f"Unknown mode: {mode_name}", False
            return f"模式已切换为：{label}（{mode_name}）", False

        return handler

    def _cmd_mode(args: list[str], ctx: dict) -> tuple[str, bool]:
        runtime = ctx.get("runtime")
        if not runtime:
            return "No active session.", False
        from runtime import MODE_LABELS
        modes_list = "\n".join(
            f"  {'* ' if runtime.current_mode == k else '  '}/{k} — {v}"
            for k, v in MODE_LABELS.items()
        )
        return f"当前模式：{runtime.current_mode_label}\n\n可用模式：\n{modes_list}", False

    def _cmd_skill(args: list[str], ctx: dict) -> tuple[str, bool]:
        runtime = ctx.get("runtime")
        if not runtime:
            return "No active session.", False

        sm = runtime.skill_manager

        # /skill off — deactivate all
        if args and args[0] == "off":
            count = len(runtime.get_active_skill_names())
            runtime.deactivate_all_skills()
            return f"已关闭所有技能（共 {count} 个）。", False

        # /skill — list available skills
        if not args:
            specs = sm.load_all()
            if not specs:
                return "在 skills/ 目录下未发现任何技能。将 SKILL.md 放入 skills/<name>/ 目录即可。", False

            active_names = set(runtime.get_active_skill_names())
            lines = ["## 技能列表", ""]
            for s in specs:
                if s.name in active_names:
                    status = "●"
                elif s.auto_activate:
                    status = "◉"
                else:
                    status = "○"
                tags: list[str] = []
                if s.auto_activate:
                    kws = ", ".join(s.trigger_keywords[:3])
                    tags.append(f"auto: {kws}")
                if s.mode_constraints:
                    tags.append(f"mode: {','.join(s.mode_constraints)}")
                if s.tools_module:
                    tags.append("+tools")
                if s.hooks_module:
                    tags.append("+hooks")
                tag_str = f"  [{'; '.join(tags)}]" if tags else ""
                lines.append(f"  {status} {s.name} — {s.description}{tag_str}")
            lines.append("")
            lines.append("● 已激活  ◉ 可自动激活  ○ 未激活  |  使用 /skill <name> 激活，/skill <name> info 查看详情。")
            return "\n".join(lines), False

        # /skill <name> or /skill <name> off
        name = args[0]
        if len(args) >= 2 and args[1] == "off":
            if runtime.deactivate_skill(name):
                spec = sm.load(name)
                label = spec.description if spec else name
                return f"技能已关闭：{label}", False
            else:
                available = sm.discover()
                if name not in available:
                    return f"技能 '{name}' 不存在。使用 /skill 查看可用技能。", False
                return f"技能 '{name}' 未激活。", False

        # /skill <name> info — show skill details
        if len(args) >= 2 and args[1] == "info":
            spec = sm.load(args[0])
            if spec is None:
                return f"技能 '{args[0]}' 未找到。", False
            active = spec.name in runtime.get_active_skill_names()
            lines = [
                f"## {spec.name}",
                f"  Description: {spec.description}",
                f"  Active: {active}",
                f"  Auto-activate: {spec.auto_activate}",
            ]
            if spec.trigger_keywords:
                lines.append(f"  Trigger keywords: {', '.join(spec.trigger_keywords)}")
            if spec.trigger_regex:
                lines.append(f"  Trigger regex: {spec.trigger_regex}")
            if spec.mode_constraints:
                lines.append(f"  Mode constraints: {', '.join(spec.mode_constraints)}")
            if spec.workflow_guidance:
                lines.append(f"  L2 guidance: {len(spec.workflow_guidance)} chars")
            lines.append(f"  L3 body: {len(spec.prompt_body)} chars")
            if spec.tools_module:
                lines.append(f"  Tools module: {spec.tools_module}")
                if spec.name in sm._skill_tools:
                    lines.append(f"    Registered: {', '.join(sm._skill_tools[spec.name])}")
            if spec.hooks_module:
                lines.append(f"  Hooks module: {spec.hooks_module}  (loaded: {spec.name in sm._skill_hooks_loaded})")
            lines.append(f"  Directory: {spec.directory}")
            lines.append(f"  Source: {spec.source_file}")
            if spec.prompt_body:
                preview = spec.prompt_body[:2000]
                if len(spec.prompt_body) > 2000:
                    preview += "\n... (truncated)"
                lines.append("")
                lines.append("  ── L3 Body ──")
                lines.append("  ```")
                lines.append(preview)
                lines.append("  ```")
            return "\n".join(lines), False

        # /skill <name> — activate
        spec = sm.load(name)
        if spec is None:
            available = sm.discover()
            hint = f"可用技能：{', '.join(available)}" if available else "skills/ 目录下未发现技能。"
            return f"技能 '{name}' 未找到。{hint}", False

        if runtime.activate_skill(name):
            tool_info = ""
            if spec.tools_module and spec.name in sm._skill_tools:
                tool_info = f"（已注册 {len(sm._skill_tools[spec.name])} 个工具）"
            return f"技能已激活：{spec.description} {tool_info}", False
        else:
            return f"无法激活技能 '{name}'。", False

    # ── /resume ──────────────────────────────────────────────────────

    async def _cmd_resume(args: list[str], ctx: dict) -> tuple[str, bool]:
        """Load the most recent session."""
        service = ctx.get("session_service")
        if not service:
            return "Session service not available.", False
        sessions = service.list(limit=1)
        if not sessions:
            return "No sessions found.", False
        latest = sessions[0]
        session = service.load_messages(latest.id)
        if session is None:
            return f"Cannot load session {latest.id}.", False
        ctx["pending_load"] = session
        return format_resume_report(latest, session), False

    # ── /config ──────────────────────────────────────────────────────

    def _cmd_config(args: list[str], ctx: dict) -> tuple[str, bool]:
        """View or modify configuration."""
        cfg = ctx.get("config")
        if not cfg:
            return "Config not available.", False

        if not args:
            # Show current config
            import json
            settings_path = cfg.project_dir / "settings.json"
            if settings_path.exists():
                raw = settings_path.read_text(encoding="utf-8")
                return f"## settings.json\n\n```json\n{raw}\n```", False
            return "No settings.json found.", False

        # /config <key> — show value
        key = args[0]
        if len(args) == 1:
            val = getattr(cfg, key, None)
            if val is None and hasattr(cfg, "_raw_settings"):
                val = cfg._raw_settings.get(key)
            if val is None:
                return f"Key '{key}' not found.", False
            return f"{key} = {val}", False

        # /config <key> <value> — set value (simple string/int for now)
        value = " ".join(args[1:])
        import json
        settings_path = cfg.project_dir / "settings.json"
        current = {}
        if settings_path.exists():
            current = json.loads(settings_path.read_text(encoding="utf-8"))
        # Try type coercion
        if value.lower() in ("true", "false"):
            value = value.lower() == "true"
        else:
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass
        current[key] = value
        settings_path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        ctx["system_prompt_dirty"] = True
        return f"Set {key} = {value}. Restart required for some settings.", False

    # ── /export ──────────────────────────────────────────────────────

    def _cmd_export(args: list[str], ctx: dict) -> tuple[str, bool]:
        """Export current session."""
        runtime = ctx.get("runtime")
        if not runtime:
            return "No active session.", False

        fmt = "markdown"
        if len(args) >= 1 and args[0].startswith("--format"):
            if "=" in args[0]:
                fmt = args[0].split("=", 1)[1].lower()
            elif len(args) >= 2:
                fmt = args[1].lower()

        session = runtime.session
        out_path = config.sessions_dir / f"{session.session_id}_export.md"

        lines = [f"# {session.title or 'Untitled Session'}", ""]
        lines.append(f"- Model: {session.model}")
        lines.append(f"- Mode: {session.mode}")
        lines.append(f"- Created: {session.created_at_ms}")
        lines.append("")

        for msg in session.messages:
            role_label = {
                "user": "## 👤 读者",
                "assistant": f"## 📖 {config.agent_name or '留白'}",
            }.get(msg.role, f"## {msg.role}")
            lines.append(role_label)
            lines.append("")
            for block in msg.blocks:
                if block.type == "text" and block.text:
                    lines.append(block.text)
                    lines.append("")
                elif block.type == "tool_use":
                    lines.append(f"> 🔧 **{block.tool_name}**")
                    lines.append(f"> ```json")
                    for line in (block.tool_input or "{}").split("\n"):
                        lines.append(f"> {line}")
                    lines.append(f"> ```")
                    lines.append("")
                elif block.type == "tool_result":
                    preview = block.content[:500] if block.content else ""
                    lines.append(f"> 📋 Result ({len(block.content)} chars):")
                    lines.append("> ```")
                    for line in preview.split("\n")[:10]:
                        lines.append(f"> {line}")
                    lines.append("> ```")
                    lines.append("")
            lines.append("")

        out_path.write_text("\n".join(lines), encoding="utf-8")
        return f"Exported to: {out_path}", False

    def _cmd_review(args: list[str], ctx: dict) -> tuple[str, bool]:
        """Review sessions via sub-agents (to be reimplemented in Phase C)."""
        main_db = ctx.get("session_db")
        wingman_db = ctx.get("wingman_db")
        api_client = ctx.get("api_client")

        if not all([main_db, wingman_db, api_client]):
            return "Review service unavailable (missing DB or API client).", False

        if args:
            session_ids = [s.strip() for s in args[0].split(",") if s.strip()]
        else:
            all_sessions = main_db.list_sessions(limit=100)
            session_ids = [s.id for s in all_sessions]

        if not session_ids:
            return "No sessions to review.", False

        # Phase C: launch sub-agents per session here
        return (
            f"Review functionality is being refactored to use sub-agents. "
            f"{len(session_ids)} session(s) pending. Use `review_sessions` tool instead.",
            False,
        )

    def _cmd_graph_init(args: list[str], ctx: dict) -> tuple[str, bool]:
        """Build knowledge graph for a book. /graph_init <book_id> [--batch-size N]"""
        if not args:
            return "用法：/graph_init <book_id> [--batch-size N]", False

        book_id = args[0]
        batch_size = 5
        if "--batch-size" in args:
            idx = args.index("--batch-size")
            if idx + 1 < len(args):
                try:
                    batch_size = int(args[idx + 1])
                except ValueError:
                    return "Invalid batch size.", False

        books_db = ctx.get("books_db") or ctx.get("bookbank_db")
        config = ctx.get("config")
        if not books_db or not config:
            return "Books DB or config not available.", False

        from pathlib import Path
        from storage.graph_db import GraphDB, resolve_graph_db_path
        from bookgraph import GraphOrchestrator

        # Shared resolver: stable naming when a log exists (legacy
        # book_id-named data is migrated in place), book_id naming while
        # the book has no stable identity yet.
        graph_db_path = resolve_graph_db_path(config, books_db, book_id)
        graph_db = GraphDB(graph_db_path)

        orch = GraphOrchestrator(config, books_db, graph_db, graph_db_path)

        import asyncio
        import time

        def progress_cb(event: dict):
            etype = event.get("type", "")
            if etype == "context_set":
                print(f"  [context] genre={event.get('genre')} "
                      f"kinds={event.get('suggested_kinds', [])}")
            elif etype == "batch_start":
                print(f"  [batch {event.get('batch_index')}/{event.get('total_batches')}] "
                      f"{event.get('step_label', '')} ...")
            elif etype == "batch_done":
                d = event.get("detail", {})
                print(f"  [batch {event.get('batch_index')}/{event.get('total_batches')}] "
                      f"nodes={d.get('new_nodes', 0)} edges={d.get('new_edges', 0)} "
                      f"aliases={d.get('new_aliases', 0)} "
                      f"quotes={d.get('new_quotes', 0)} "
                      f"[{event.get('done_chunks')}/{event.get('total_chunks')}]")
            elif etype == "batch_failed":
                print(f"  [batch {event.get('batch_index')}] FAILED")
            elif etype == "batch_skipped":
                print(f"  [batch {event.get('batch_index')}] SKIP (already done)")
            elif etype == "complete":
                stats = event.get("final_stats", {})
                print(f"\n  === 完成 ===")
                print(f"  nodes={stats.get('nodes', 0)} "
                      f"edges={stats.get('edges', 0)} "
                      f"aliases={stats.get('aliases', 0)}")

        print(f"开始为书籍 {book_id} 构建知识图谱...")
        t0 = time.time()
        try:
            result = asyncio.run(orch.run(
                book_id, batch_size=batch_size, progress_cb=progress_cb
            ))
            elapsed = time.time() - t0
            return (
                f"图谱构建完成 [{elapsed:.1f}s]\n"
                f"  书名：《{result.book_title}》\n"
                f"  进度：{result.chunks_done}/{result.total_chunks} chunks "
                f"({result.chunks_failed} failed)\n"
                f"  节点：{result.final_stats.get('nodes', 0)}\n"
                f"  边：{result.final_stats.get('edges', 0)}\n"
                f"  别名：{result.final_stats.get('aliases', 0)}\n"
                f"  合并候选：{result.final_stats.get('merge_candidates', 0)}\n"
                f"  graph.db: {graph_db_path}\n\n"
                f"前端进度面板：http://localhost:8000/ → 选书 → 图谱分析",
                False,
            )
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return f"图谱构建失败：{exc}", False
        finally:
            graph_db.close()

    def _cmd_graph_status(args: list[str], ctx: dict) -> tuple[str, bool]:
        """Show graph extraction progress. /graph_status [book_id]"""
        config = ctx.get("config")
        books_db = ctx.get("books_db") or ctx.get("bookbank_db")
        if not config:
            return "Config not available.", False

        # Determine book_id
        if args:
            book_id = args[0]
        else:
            # Find any graph DB
            graph_files = list(config.graphs_dir.glob("graph_*.db"))
            if not graph_files:
                return "没有找到任何图谱。用 /graph_init <book_id> 开始构建。", False
            lines = ["已有图谱：", ""]
            for f in graph_files:
                bid = f.stem.replace("graph_", "")
                book = books_db.get_book(bid) if books_db else None
                title = book.title if book else bid
                from storage.graph_db import GraphDB
                gdb = GraphDB(f)
                stats = gdb.get_stats(bid)
                lines.append(
                    f"  《{title}》  {stats['chunks_done']}/{stats['chunks_total']} chunks  "
                    f"nodes={stats['nodes']} edges={stats['edges']}"
                )
                gdb.close()
            return "\n".join(lines), False

        from pathlib import Path
        from storage.graph_db import GraphDB, resolve_graph_db_path
        graph_db_path = resolve_graph_db_path(config, books_db, book_id)
        if not graph_db_path.exists():
            return f"未找到 {book_id} 的图谱。用 /graph_init {book_id} 开始构建。", False

        book = books_db.get_book(book_id) if books_db else None
        title = book.title if book else book_id
        gdb = GraphDB(graph_db_path)
        stats = gdb.get_stats(book_id)
        gdb.close()

        lines = [
            f"《{title}》 图谱状态",
            f"  chunks: {stats['chunks_done']}/{stats['chunks_total']} done "
            f"({stats['chunks_failed']} failed)",
            f"  nodes: {stats['nodes']}",
            f"  edges: {stats['edges']}",
            f"  aliases: {stats['aliases']}",
            f"  merge candidates: {stats['merge_candidates']}",
            "",
            "实体分类：",
        ]
        for kind, count in stats.get("kinds", {}).items():
            lines.append(f"  {kind}: {count}")
        return "\n".join(lines), False

    def _cmd_graph_stats(args: list[str], ctx: dict) -> tuple[str, bool]:
        """Alias for graph_status with more detail."""
        return _cmd_graph_status(args, ctx)

    def _cmd_graph_reset(args: list[str], ctx: dict) -> tuple[str, bool]:
        """Delete and rebuild graph. /graph_reset <book_id> --confirm"""
        if not args:
            return "用法：/graph_reset <book_id> --confirm", False
        if "--confirm" not in args:
            return "加 --confirm 确认删除（不可恢复）", False
        book_id = args[0]
        config = ctx.get("config")
        if not config:
            return "Config not available.", False
        from pathlib import Path
        from storage.graph_db import resolve_graph_db_path
        # Remove BOTH naming candidates — a legacy book_id-named file left
        # behind would otherwise be migrated back into existence on the
        # next graph read (reset should be a true reset).
        graph_db_path = resolve_graph_db_path(config, books_db, book_id)
        deleted = False
        for p in {graph_db_path, config.graph_db_path(book_id)}:
            if p and p.exists():
                p.unlink()
                deleted = True
        if not deleted:
            return f"未找到 {book_id} 的图谱。", False
        return f"已删除 {book_id} 的图谱。用 /graph_init {book_id} 重建。", False

    # Dispatch table
    return {
        "help": _cmd_help,
        "model": _cmd_model,
        "immersive": _make_mode_handler("immersive"),
        "immerse": _make_mode_handler("immersive"),
        "overview": _make_mode_handler("overview"),
        "divergent": _make_mode_handler("divergent"),
        "diverge": _make_mode_handler("divergent"),
        "mode": _cmd_mode,
        "skill": _cmd_skill,
        "compact": _cmd_compact,
        "clear": _cmd_clear,
        "new": _cmd_clear,
        "memory": _cmd_memory,
        "persona": _cmd_persona,
        "cost": _cmd_cost,
        "usage": _cmd_cost,
        "save": _cmd_save,
        "sessions": _cmd_sessions_async,
        "session": _cmd_sessions_async,
        "fork": _cmd_fork,
        "permissions": _cmd_permissions,
        "quit": _cmd_quit,
        "exit": _cmd_quit,
        "q": _cmd_quit,
        "resume": _cmd_resume,
        "config": _cmd_config,
        "export": _cmd_export,
        "review": _cmd_review,
        "graph_init": _cmd_graph_init,
        "graph_status": _cmd_graph_status,
        "graph_stats": _cmd_graph_stats,
        "graph_reset": _cmd_graph_reset,
    }
