"""File-based memory store — adapted from Hermes tools/memory_tool.py MemoryStore.

Maintains two files: MEMORY.md (agent knowledge) and USER.md (user profile).
Entries are delimited by "§" and frozen at session start for stable system prompts.

Key improvements over v1 (2026-06-02):
  - replace: substring-matching entry replacement (Hermes parity)
  - remove: substring-matching instead of index-based (Hermes parity)
  - File locking: msvcrt (Windows) / fcntl (Unix) cross-platform
  - Atomic writes: tempfile.mkstemp + os.fsync + os.replace
  - Content scanning: injection/exfiltration pattern detection
  - Budget policy: reject over-limit instead of silent auto-trim
  - Load-time freeze: snapshot never updated mid-session (prefix-cache stable)
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config

# ── Memory block template (loaded once from prompts/) ────────────────────

_MEMORY_BLOCK_TPL_PATH = Path(__file__).parent / "prompts" / "memory" / "memory_block.md"
_MEMORY_BLOCK_TPL = (
    _MEMORY_BLOCK_TPL_PATH.read_text(encoding="utf-8")
    if _MEMORY_BLOCK_TPL_PATH.exists()
    else "{separator}\n{title} [{pct}% — {char_count:,}/{char_limit:,} chars]\n{separator}\n\n{content}"
)

# fcntl is Unix-only; on Windows use msvcrt for file locking
msvcrt = None
fcntl = None
try:
    import fcntl
except ImportError:
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

ENTRY_DELIMITER = "\n§\n"

# Soft upper bound — log a warning when memory exceeds this (50k chars ≈ 150–200 entries).
# No hard rejection; Phase 2 consolidation is the primary size-control mechanism.
SOFT_WARN_CHARS = 50_000


# ---------------------------------------------------------------------------
# Content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
# ---------------------------------------------------------------------------

_MEMORY_THREAT_PATTERNS: list[tuple[str, str]] = [
    # Prompt injection
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    # Exfiltration via curl/wget with secrets
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_wget"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets"),
    # Persistence via shell rc
    (r'authorized_keys', "ssh_backdoor"),
    (r'\$HOME/\.ssh|\~/\.ssh', "ssh_access"),
]

# Invisible unicode chars commonly used in injection attacks
_INVISIBLE_CHARS: set[str] = {
    '​', '‌', '‍', '⁠', '﻿',
    '‪', '‫', '‬', '‭', '‮',
}


def _scan_memory_content(content: str) -> str | None:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    for char in _INVISIBLE_CHARS:
        if char in content:
            return (
                f"Blocked: content contains invisible unicode character "
                f"U+{ord(char):04X} (possible injection)."
            )

    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return (
                f"Blocked: content matches threat pattern '{pid}'. "
                f"Memory entries are injected into the system prompt and "
                f"must not contain injection or exfiltration payloads."
            )

    return None


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """Bounded curated memory with file persistence.

    Maintains two parallel states:
      - _frozen: snapshot captured at load_from_disk() time, used for system
        prompt injection. Never mutated mid-session — keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls,
        persisted to disk immediately. Tool responses reflect this live state.
    """

    def __init__(self, config: Config):
        self._config = config
        self._memory_dir = config.memory_dir
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._memory_path = self._memory_dir / "MEMORY.md"
        self._user_path = self._memory_dir / "USER.md"
        self._persona_path = self._memory_dir / "persona.md"  # L3 AI 观察投影
        self._memory_char_limit: int = config.memory_char_limit
        self._user_char_limit: int = config.user_memory_char_limit

        # Live lists
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []

        # Frozen snapshot — captured once at load, never mutated mid-session
        self._frozen: dict[str, str] = {"memory": "", "user": "", "persona": ""}

        self.load_from_disk()

    # ── Public API ──────────────────────────────────────────────────────────

    def load_from_disk(self) -> None:
        """Load entries from disk, deduplicate, and freeze for system prompt."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_entries = self._read_entries(self._memory_path)
        self.user_entries = self._read_entries(self._user_path)

        # Deduplicate while preserving order (keeps first occurrence)
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        self._freeze()

    def freeze_for_system_prompt(self, target: str) -> str:
        """Return the memory snapshot frozen at session start.

        This snapshot stays stable throughout the session — mid-session
        writes to MEMORY.md / USER.md do not update it. Those writes are
        discoverable via search_memory() (which searches live entries).
        The archive pipeline consolidates everything before the next session.
        """
        return self._frozen.get(target, "")

    def search(self, query: str) -> list[tuple[str, str, int]]:
        """Search all entries. Returns list of (target, text, 1-based index)."""
        results: list[tuple[str, str, int]] = []
        q = query.lower()
        for i, entry in enumerate(self.memory_entries, 1):
            if q in entry.lower():
                results.append(("memory", entry, i))
        for i, entry in enumerate(self.user_entries, 1):
            if q in entry.lower():
                results.append(("user", entry, i))
        return results

    # ── Mutation API (returns Dict like Hermes memory_tool) ─────────────────

    def add(self, target: str, content: str) -> dict[str, Any]:
        """Append a new entry. Returns structured result dict.

        Uses file locking + disk reload to safely handle concurrent sessions.
        No hard character limit — archive Phase 2 consolidation is the primary
        size-control mechanism. Logs a warning if the soft cap is exceeded.
        """
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)

            # Reject exact duplicates
            if content in entries:
                return self._success_response(
                    target, "Entry already exists (no duplicate added)."
                )

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

            # Soft warning if exceeding the advisory cap
            current_chars = self._char_count(target)
            if current_chars > SOFT_WARN_CHARS:
                logger.warning(
                    "Memory '%s' at %s chars (soft cap: %s). "
                    "Consider running archive Phase 2 to consolidate.",
                    target, f"{current_chars:,}", f"{SOFT_WARN_CHARS:,}",
                )

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> dict[str, Any]:
        """Find entry containing old_text substring, replace with new_content.

        If multiple entries match and they are different, the call fails with
        a list of previews so the caller can be more specific.
        No hard character limit — archive Phase 2 is the primary size control.
        """
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {
                "success": False,
                "error": "new_content cannot be empty. Use 'remove' to delete entries.",
            }

        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), operate on first
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [
                        e[:80] + ("..." if len(e) > 80 else "")
                        for _, e in matches
                    ]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }

            idx = matches[0][0]

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

            # Soft warning if exceeding the advisory cap
            current_chars = self._char_count(target)
            if current_chars > SOFT_WARN_CHARS:
                logger.warning(
                    "Memory '%s' at %s chars (soft cap: %s). "
                    "Consider running archive Phase 2 to consolidate.",
                    target, f"{current_chars:,}", f"{SOFT_WARN_CHARS:,}",
                )

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> dict[str, Any]:
        """Remove the entry containing old_text substring.

        Accepts substring matching (Hermes-style). For index-based removal,
        use the old_text as the exact entry content.
        """
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [
                        e[:80] + ("..." if len(e) > 80 else "")
                        for _, e in matches
                    ]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    # ── Internal helpers ────────────────────────────────────────────────────

    def _freeze(self) -> None:
        """Capture frozen snapshot of current entries for system prompt."""
        self._frozen["memory"] = self._render_block(
            "MEMORY (your personal reference notes about the world)",
            self.memory_entries,
            self._memory_char_limit,
        )
        self._frozen["user"] = self._render_block(
            "USER PROFILE (what you know about the user)",
            self.user_entries,
            self._user_char_limit,
        )
        self._frozen["persona"] = self._render_persona_block()

    def _render_persona_block(self) -> str:
        """Render persona.md (L3 AI observation projection) for system prompt.

        Injected between MEMORY and USER blocks — it is the agent's own
        observation baseline, distinct from the user's self-statement.
        Content is LLM-generated from conversation, so it is threat-scanned
        before injection (it lands in the system prompt).
        """
        try:
            content = self._persona_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not content:
            return ""
        if _scan_memory_content(content):
            logger.warning("Persona block blocked: content failed threat scan")
            return ""
        separator = "═" * 46
        return (
            f"{separator}\n"
            f"### 用户画像（AI 观察投影）\n"
            f"{separator}\n\n"
            f"以下内容由系统基于长期真实交流自动总结（L3 画像管线），"
            f"反映系统对用户的观察；不等同于用户自我陈述。\n\n"
            f"{content}\n"
        )

    def _render_block(
        self, title: str, entries: list[str], char_limit: int
    ) -> str:
        """Render a system prompt block using the prompts/memory/memory_block.md template."""
        if not entries:
            return ""
        content = ENTRY_DELIMITER.join(entries)
        char_count = len(content)
        pct = min(100, int(char_count / char_limit * 100)) if char_limit > 0 else 0
        separator = "═" * 46
        # When unlimited (char_limit=0), show only the character count
        if char_limit > 0:
            usage_label = f"{pct}% — {char_count:,}/{char_limit:,} chars"
        else:
            usage_label = f"{char_count:,} chars"
        return _MEMORY_BLOCK_TPL.format(
            separator=separator,
            title=title,
            pct=pct,
            char_count=char_count,
            char_limit=char_limit,
            usage_label=usage_label,
            content=content,
        )

    def _success_response(
        self, target: str, message: str | None = None
    ) -> dict[str, Any]:
        """Build a structured success response (Hermes-compatible format)."""
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)

        if limit > 0:
            pct = min(100, int((current / limit) * 100))
            usage = f"{pct}% — {current:,}/{limit:,} chars"
        else:
            usage = f"{current:,} chars"

        resp: dict[str, Any] = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": usage,
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def save_to_disk(self, target: str) -> None:
        """Persist entries to the appropriate file. Called after every mutation."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    # ── Target routing ──────────────────────────────────────────────────────

    def _path_for(self, target: str) -> Path:
        if target == "user":
            return self._user_path
        return self._memory_path

    def _entries_for(self, target: str) -> list[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self._user_char_limit
        return self._memory_char_limit

    def _reload_target(self, target: str) -> None:
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        This handles concurrent writes from other sessions.
        """
        fresh = self._read_entries(self._path_for(target))
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)

    # ── File I/O ────────────────────────────────────────────────────────────

    @staticmethod
    def _read_entries(path: Path) -> list[str]:
        """Read a memory file and split into entries.

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        # Use ENTRY_DELIMITER for consistency with _write_file.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: list[str]) -> None:
        """Write entries using atomic temp-file + rename.

        Atomic rename guarantees readers always see a complete file — either
        the old one or the new one, never a half-written file.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace(). Cross-platform: fcntl on Unix,
        msvcrt on Windows.
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            # No locking available — yield without protection
            yield
            return

        if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
            lock_path.write_text(" ", encoding="utf-8")

        fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()


# ---------------------------------------------------------------------------
# Built-in MemoryProvider wrapper
# ---------------------------------------------------------------------------

class BuiltinMemoryProvider:
    """Wraps MemoryStore as a MemoryProvider for the MemoryManager pipeline.

    This is the always-active built-in provider that reads from MEMORY.md
    and USER.md. It has no tools of its own (the ``memory`` / ``search_memory``
    tools are registered separately in the ToolRegistry).

    The wrapper delegates system prompt assembly and mutation bridging
    to the underlying MemoryStore while satisfying the MemoryProvider
    interface so MemoryManager can treat all providers uniformly.
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return True  # Built-in memory is always available

    def initialize(self, session_id: str, **kwargs) -> None:
        """Re-load from disk on session init (picks up changes from other sessions)."""
        self._store.load_from_disk()

    def system_prompt_block(self) -> str:
        """Return the frozen memory + persona + user blocks combined.

        Injection order (plan 4.6): MEMORY.md → persona.md（AI 观察基线）
        → USER.md（用户自述 / 期望的对待方式）.
        """
        mem_block = self._store.freeze_for_system_prompt("memory")
        persona_block = self._store.freeze_for_system_prompt("persona")
        user_block = self._store.freeze_for_system_prompt("user")
        parts = [b for b in (mem_block, persona_block, user_block) if b]
        return "\n\n".join(parts) if parts else ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Built-in has no semantic recall — return empty."""
        return ""

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        """Built-in memory is manually written, no auto-sync."""
        pass

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Built-in memory tools are registered in the main ToolRegistry."""
        return []

    def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **kwargs
    ) -> str:
        raise NotImplementedError("Built-in memory has no provider tools")

    def shutdown(self) -> None:
        pass
