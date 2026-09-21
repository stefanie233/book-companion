"""Skill system — self-contained capability packages for the book companion agent.

Each skill is a directory under skills/ containing a SKILL.md file with
YAML frontmatter + Markdown prompt body. Skills are stackable layers
that inject domain expertise into the system prompt.

Progressive injection model:
  L0 — name + description always visible in static prompt section
  L2 — workflow guidance injected when trigger keywords/regex match
  L3 — full prompt body when skill is explicitly or auto-activated

Skills may optionally bundle tools (tools_module) and hooks (hooks_module)
that are registered on activation and unregistered on deactivation.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools import ToolRegistry
    from hooks import HookPipeline


@dataclass
class SkillSpec:
    """Immutable skill definition loaded from a SKILL.md file.

    All optional fields have defaults for backward compatibility with
    skills that only use name + description + prompt_body.
    """
    name: str
    description: str
    directory: Path
    prompt_body: str
    source_file: Path
    # L2: lightweight guidance injected on keyword/regex match
    workflow_guidance: str = ""
    # Auto-activation triggers
    trigger_keywords: list[str] = field(default_factory=list)
    trigger_regex: str = ""
    mode_constraints: list[str] = field(default_factory=list)
    auto_activate: bool = False
    # Execution: per-skill tool/hook modules (paths relative to skill directory)
    tools_module: str = ""
    hooks_module: str = ""


class SkillManager:
    """Manages skill discovery, loading, activation, and progressive prompt rendering.

    Wire-up:
        After construction, call set_tool_registry() and set_hook_pipeline()
        so that skill activation can register tools and hooks automatically.
    """

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)
        self._active: dict[str, SkillSpec] = {}
        # Injected dependencies (set via setters after construction)
        self._tool_registry: ToolRegistry | None = None
        self._hook_pipeline: HookPipeline | None = None
        # Lifecycle tracking
        self._skill_tools: dict[str, list[str]] = {}   # skill_name → tool names
        self._skill_hooks_loaded: set[str] = set()
        # Lazy cache for L0 rendering
        self._all_skills_cache: list[SkillSpec] | None = None

    # ── Dependency injection ───────────────────────────────────────────

    def set_tool_registry(self, registry: ToolRegistry) -> None:
        self._tool_registry = registry

    def set_hook_pipeline(self, pipeline: HookPipeline) -> None:
        self._hook_pipeline = pipeline

    # ── Discovery ──────────────────────────────────────────────────────

    def discover(self) -> list[str]:
        """Scan skills_dir for valid skill directories. Returns sorted skill names."""
        if not self.skills_dir.exists():
            return []
        names = []
        for entry in sorted(self.skills_dir.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").exists():
                names.append(entry.name)
        return names

    # ── Loading ────────────────────────────────────────────────────────

    def load(self, name: str) -> SkillSpec | None:
        """Parse a single SKILL.md into a SkillSpec. Returns None if not found."""
        skill_dir = self.skills_dir / name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None
        return _parse_skill_md(skill_file, skill_dir)

    def load_all(self) -> list[SkillSpec]:
        """Scan and parse all skill directories. Results are cached."""
        if self._all_skills_cache is not None:
            return self._all_skills_cache
        specs: list[SkillSpec] = []
        for name in self.discover():
            spec = self.load(name)
            if spec:
                specs.append(spec)
        self._all_skills_cache = specs
        return specs

    def _invalidate_cache(self) -> None:
        self._all_skills_cache = None

    # ── Activation ─────────────────────────────────────────────────────

    def activate(self, name: str) -> bool:
        """Activate a skill by name. Idempotent. Registers tools/hooks on first activation.

        Returns True on success.
        """
        if name in self._active:
            return True
        spec = self.load(name)
        if spec is None:
            return False
        self._active[name] = spec
        self.register_skill_tools(name)
        self.register_skill_hooks(name)
        return True

    def deactivate(self, name: str) -> bool:
        """Deactivate a skill by name. Unregisters its tools.

        Returns True if it was active.
        """
        if name in self._active:
            self.unregister_skill_tools(name)
            del self._active[name]
            return True
        return False

    def deactivate_all(self) -> None:
        """Deactivate all active skills."""
        for name in list(self._active.keys()):
            self.unregister_skill_tools(name)
        self._active.clear()

    def is_active(self, name: str) -> bool:
        return name in self._active

    def get_active_skills(self) -> list[SkillSpec]:
        return list(self._active.values())

    def get_active_names(self) -> list[str]:
        return list(self._active.keys())

    # ── Progressive prompt rendering ───────────────────────────────────

    def render_l0(self) -> str:
        """Render the always-visible skill list (name + description only).

        Placed in the static section of the system prompt so the LLM
        always knows what skills are available. Very low token cost.
        """
        specs = self.load_all()
        if not specs:
            return ""
        lines = ["## Available Skills", ""]
        for s in specs:
            trigger_hint = " (auto)" if s.auto_activate else ""
            lines.append(f"- **{s.name}**{trigger_hint}: {s.description}")
        return "\n".join(lines)

    def render_l2(self, user_input: str, current_mode: str) -> str:
        """Render workflow guidance for skills whose triggers match user_input.

        Scans ALL skills (not just active ones). A skill contributes its
        workflow_guidance when:
          1. It has non-empty workflow_guidance
          2. mode_constraints pass (if defined)
          3. trigger_keywords OR trigger_regex match user_input
        Returns empty string when nothing matches.
        """
        if not user_input:
            return ""
        specs = self.load_all()
        matched: list[SkillSpec] = []
        for s in specs:
            if not s.workflow_guidance:
                continue
            if not self._trigger_matches(s, user_input, current_mode):
                continue
            matched.append(s)
        if not matched:
            return ""
        parts: list[str] = ["## Contextual Guidance", ""]
        for s in matched:
            parts.append(f"### {s.name}")
            parts.append(s.workflow_guidance)
            parts.append("")
        return "\n".join(parts)

    def render_l3(self) -> str:
        """Render full prompt bodies for explicitly active skills.

        Same as the old render_for_system_prompt(). Returns empty string
        when no skills are active.
        """
        active = self.get_active_skills()
        if not active:
            return ""
        parts: list[str] = ["# Active Skills", ""]
        for spec in active:
            parts.append(f"## {spec.name} ({spec.description})")
            parts.append("")
            parts.append(spec.prompt_body)
            parts.append("")
        return "\n".join(parts)

    def render_for_system_prompt(self) -> str:
        """Alias for render_l3() — backward compatibility."""
        return self.render_l3()

    # ── Auto-activation ────────────────────────────────────────────────

    def evaluate_triggers(self, user_input: str, current_mode: str) -> list[str]:
        """Return names of skills that should auto-activate this turn.

        A skill qualifies when:
          1. auto_activate is True
          2. Not already active
          3. _trigger_matches() returns True
        """
        if not user_input:
            return []
        specs = self.load_all()
        triggered: list[str] = []
        for s in specs:
            if not s.auto_activate:
                continue
            if s.name in self._active:
                continue
            if self._trigger_matches(s, user_input, current_mode):
                triggered.append(s.name)
        return triggered

    def _trigger_matches(self, spec: SkillSpec, user_input: str, current_mode: str) -> bool:
        """Check whether a skill's triggers match the given input and mode."""
        # Mode constraints
        if spec.mode_constraints and current_mode not in spec.mode_constraints:
            return False
        # Keyword match (case-insensitive, any keyword)
        if spec.trigger_keywords:
            user_lower = user_input.lower()
            if not any(kw.lower() in user_lower for kw in spec.trigger_keywords):
                return False
        # Regex match
        if spec.trigger_regex:
            try:
                if not re.search(spec.trigger_regex, user_input):
                    return False
            except re.error:
                return False
        # At least one trigger mechanism must be configured
        if not spec.trigger_keywords and not spec.trigger_regex:
            return False
        return True

    # ── Tool lifecycle ─────────────────────────────────────────────────

    def register_skill_tools(self, skill_name: str) -> list[str]:
        """Import and register tools from a skill's tools_module. Tracks names for cleanup."""
        if self._tool_registry is None:
            return []
        spec = self._active.get(skill_name) or self.load(skill_name)
        if spec is None or not spec.tools_module:
            return []
        try:
            before = set(self._tool_registry.get_tool_names())
            mod = _import_module_from_path(
                f"skill_{skill_name}_tools",
                spec.directory / spec.tools_module,
            )
            if mod and hasattr(mod, "register_tools"):
                mod.register_tools(self._tool_registry)
            after = set(self._tool_registry.get_tool_names())
            self._skill_tools[skill_name] = list(after - before)
        except Exception:
            pass
        return self._skill_tools.get(skill_name, [])

    def unregister_skill_tools(self, skill_name: str) -> None:
        """Unregister all tools previously registered by this skill."""
        if self._tool_registry is None:
            return
        tool_names = self._skill_tools.pop(skill_name, [])
        for name in tool_names:
            self._tool_registry.unregister(name)

    # ── Hook lifecycle ─────────────────────────────────────────────────

    def register_skill_hooks(self, skill_name: str) -> None:
        """Import and register hooks from a skill's hooks_module. One-shot (no unregister)."""
        if self._hook_pipeline is None:
            return
        if skill_name in self._skill_hooks_loaded:
            return
        spec = self._active.get(skill_name) or self.load(skill_name)
        if spec is None or not spec.hooks_module:
            return
        try:
            mod = _import_module_from_path(
                f"skill_{skill_name}_hooks",
                spec.directory / spec.hooks_module,
            )
            if mod and hasattr(mod, "register_hooks"):
                mod.register_hooks(self._hook_pipeline)
            self._skill_hooks_loaded.add(skill_name)
        except Exception:
            pass


# ── Module loading helper ─────────────────────────────────────────────

def _import_module_from_path(module_name: str, file_path: Path) -> object | None:
    """Import a Python module from a file path. Returns the module or None."""
    try:
        resolved = file_path.resolve()
        spec = importlib.util.spec_from_file_location(module_name, str(resolved))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


# ── SKILL.md parsing ───────────────────────────────────────────────────

def _clean_yaml_value(value: str) -> str:
    """Strip surrounding quotes and whitespace from a simple YAML scalar."""
    value = value.strip()
    if len(value) >= 2:
        if (value.startswith("'") and value.endswith("'")) or \
           (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
    return value


def _parse_list_value(value: str) -> list[str]:
    """Split a comma-separated string into stripped, non-empty entries."""
    # Strip surrounding quotes first (simple YAML parsing)
    value = _clean_yaml_value(value)
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_skill_md(filepath: Path, directory: Path) -> SkillSpec | None:
    """Parse a SKILL.md file into a SkillSpec.

    Format:
        ---
        name: skill-name
        description: one-line description
        auto_activate: true
        trigger_keywords: "writing, editing, style"
        trigger_regex: "help me (write|edit|revise)"
        mode_constraints: "immersive, overview"
        tools_module: "tools.py"
        hooks_module: "hooks.py"
        ---
        # Title

        ## Workflow Guidance

        L2: lightweight guidance injected on keyword match ...

        ## Anything else

        L3: full prompt body (or the whole body if no L2 heading) ...
    """
    try:
        raw = filepath.read_text(encoding="utf-8").strip()
    except Exception:
        return None

    if not raw:
        return None

    name = directory.name
    description = ""
    prompt_body = raw
    workflow_guidance = ""
    trigger_keywords: list[str] = []
    trigger_regex = ""
    mode_constraints: list[str] = []
    auto_activate = False
    tools_module = ""
    hooks_module = ""

    # Parse YAML frontmatter
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1].strip()
            prompt_body = parts[2].strip()
            for line in frontmatter.splitlines():
                line = line.strip()
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = _clean_yaml_value(value)
                if key == "name" and value:
                    name = value
                elif key == "description" and value:
                    description = value
                elif key == "trigger_keywords" and value:
                    trigger_keywords = _parse_list_value(value)
                elif key == "trigger_regex" and value:
                    trigger_regex = value
                elif key == "mode_constraints" and value:
                    mode_constraints = _parse_list_value(value)
                elif key == "auto_activate" and value:
                    auto_activate = value.lower() == "true"
                elif key == "tools_module" and value:
                    tools_module = value
                elif key == "hooks_module" and value:
                    hooks_module = value

    # Split L2 (Workflow Guidance) from L3 (full body)
    l2_heading_pattern = re.compile(
        r"^##\s*(Workflow Guidance|L2)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = l2_heading_pattern.search(prompt_body)
    if m:
        l2_start = m.start()
        # Find the next ## heading after the L2 heading
        next_heading = re.search(r"^##\s+", prompt_body[m.end():], re.MULTILINE)
        if next_heading:
            l2_end = m.end() + next_heading.start()
            workflow_guidance = prompt_body[m.end():l2_end].strip()
            prompt_body = (prompt_body[:l2_start] + prompt_body[l2_end:]).strip()
        else:
            # L2 heading is the last significant section — take rest as L2
            workflow_guidance = prompt_body[m.end():].strip()
            prompt_body = prompt_body[:l2_start].strip()

    return SkillSpec(
        name=name,
        description=description,
        directory=directory,
        prompt_body=prompt_body,
        source_file=filepath,
        workflow_guidance=workflow_guidance,
        trigger_keywords=trigger_keywords,
        trigger_regex=trigger_regex,
        mode_constraints=mode_constraints,
        auto_activate=auto_activate,
        tools_module=tools_module,
        hooks_module=hooks_module,
    )
