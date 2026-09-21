"""Instruction file discovery -- adapted from claw-code prompt.rs discover_instruction_files().

Walks up from CWD to root, collecting .book-companion.md / CLAUDE.md files.
Content-hash dedup prevents redundant injection. Char budgets enforced.

Smart truncation: files can use ``<!-- LLM_CUTOFF -->`` to mark where
developer-only reference content begins. Content after the marker is
excluded from the LLM context but preserved in the source file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# ── Instruction files section template (loaded once from prompts/) ───────

_INSTR_FILES_TPL_PATH = Path(__file__).parent / "prompts" / "instruction_files.md"
_INSTR_FILES_TPL = (
    _INSTR_FILES_TPL_PATH.read_text(encoding="utf-8")
    if _INSTR_FILES_TPL_PATH.exists()
    else "# Project Instructions\n\n{file_blocks}"
)

# Budget limits (from claw-code prompt.rs)
MAX_TOTAL_CHARS = 12_000
MAX_FILE_CHARS = 4_000

# Marker: everything before this line goes to the LLM; reference tables
# after it are for human developers only.
_LLM_CUTOFF_MARKER = "<!-- LLM_CUTOFF -->"

# File candidates per directory (order matters -- first found wins per dir)
CANDIDATES = [
    ".book-companion.md",
    "BOOK-COMPANION.md",
    "CLAUDE.md",
    ".claude/CLAUDE.md",
    ".claude/instructions.md",
]


def _smart_truncate(content: str, max_chars: int = MAX_FILE_CHARS) -> str:
    """Truncate file content intelligently for LLM context budget.

    1. If ``<!-- LLM_CUTOFF -->`` marker is present, cut there.
    2. Otherwise, find the last ``## `` or ``# `` heading before *max_chars*
       and cut at that boundary so the LLM never sees a truncated heading.
    3. Fall back to hard truncation if no suitable heading is found.
    """
    # Explicit cutoff marker takes priority
    cutoff_pos = content.find(_LLM_CUTOFF_MARKER)
    if cutoff_pos >= 0:
        return content[:cutoff_pos].strip()

    if len(content) <= max_chars:
        return content

    # Find the last section heading before the limit
    best = -1
    for pattern in ("\n## ", "\n# "):
        pos = 0
        while True:
            pos = content.find(pattern, pos)
            if pos == -1 or pos >= max_chars:
                break
            best = max(best, pos)
            pos += 1

    if best > 0:
        return content[:best].strip() + "\n\n[Full reference truncated for context budget]"

    return content[:max_chars] + "\n\n[truncated]"


def discover_instruction_files(start_dir: Path | None = None) -> list[tuple[Path, str]]:
    """Walk from start_dir upward, collecting instruction files.

    Returns list of (path, content) ordered from root -> CWD (closest last).
    Content-hash dedup: same content across directories only kept once.
    """
    start = (start_dir or Path.cwd()).resolve()
    if not start.is_dir():
        start = start.parent

    # Collect directories from root to CWD
    dirs = []
    cursor: Path | None = start
    while cursor is not None:
        dirs.append(cursor)
        cursor = cursor.parent if cursor != cursor.parent else None

    # Reverse so root comes first, CWD last (closest has highest priority)
    dirs.reverse()

    # Collect files with content dedup
    seen_hashes: set[str] = set()
    files: list[tuple[Path, str]] = []
    total_chars = 0

    for d in dirs:
        for candidate in CANDIDATES:
            candidate_path = d / candidate
            if not candidate_path.is_file():
                continue
            try:
                content = candidate_path.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if not content:
                continue

            # Smart truncate per-file (marker-aware, heading-boundary aware)
            content = _smart_truncate(content, MAX_FILE_CHARS)
            if not content:
                continue  # Entire file was cut by LLM_CUTOFF marker

            # Content-hash dedup
            h = hashlib.sha256(content.encode()).hexdigest()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            # Budget check
            if total_chars + len(content) > MAX_TOTAL_CHARS:
                remaining = MAX_TOTAL_CHARS - total_chars
                if remaining > 200:
                    content = content[:remaining] + "\n\n[budget truncated]"
                else:
                    break

            total_chars += len(content)
            files.append((candidate_path, content))
            break  # Only first found candidate per directory

    return files


def render_instruction_files(files: list[tuple[Path, str]]) -> str:
    """Render discovered instruction files into a system prompt section."""
    if not files:
        return ""

    blocks = []
    for path, content in files:
        try:
            rel = path.relative_to(Path.cwd())
        except ValueError:
            rel = path
        blocks.append(f"## {rel}\n\n{content}")

    return _INSTR_FILES_TPL.format(file_blocks="\n\n".join(blocks)).strip()
