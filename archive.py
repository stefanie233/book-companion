"""Archive pipeline — extract cross-session knowledge from conversations.

Phase 1: Extract knowledge from current session → append to MEMORY.md / USER.md
Phase 2: Consolidate and reorganize full memory files when thresholds exceeded

Design: :doc:`留白-记忆机制设计方案` §4
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import usage_log
from llm_json import loads_tolerant

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

CONFIDENCE_THRESHOLD = 0.6
PHASE2_ENTRY_THRESHOLD = 50
PHASE2_CHAR_THRESHOLD = 10_000

_PROMPTS_DIR = Path(__file__).parent / "prompts" / "archive"
_PHASE1_TPL = (
    _PROMPTS_DIR / "phase1.md"
).read_text(encoding="utf-8") if (_PROMPTS_DIR / "phase1.md").exists() else ""
_PHASE2_TPL = (
    _PROMPTS_DIR / "phase2.md"
).read_text(encoding="utf-8") if (_PROMPTS_DIR / "phase2.md").exists() else ""

# Category → target routing
CATEGORY_ROUTES: dict[str, str] = {
    "A": "memory",
    "B": "memory",
    "C": "memory",
    "D": "user",
}

# Category → Chinese label prefix for human-readable entries
CATEGORY_LABELS: dict[str, str] = {
    "A": "[偏好] ",
    "B": "[进度] ",
    "C": "[观点] ",
    "D": "[约定] ",
}

_EXTRACT_SYSTEM_PROMPT = "你是一个精确的知识提取助手。只输出要求的 JSON 格式，不要额外内容。"


# ── Helpers ────────────────────────────────────────────────────────────────


def build_conversation_text(messages: list[dict]) -> str:
    """Build a clean conversation transcript from session messages.

    Filters out system noise: empty messages, pure-JSON tool outputs, etc.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "").strip()
        if not content:
            continue
        # Skip pure JSON blocks (tool outputs without natural language)
        stripped = content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                json.loads(stripped)
                continue
            except json.JSONDecodeError:
                pass
        label = {"user": "用户", "assistant": "留白", "tool": "工具"}.get(role, role)
        lines.append(f"{label}: {content}")
    return "\n\n".join(lines)


def _parse_json_response(response: str) -> list[dict]:
    """Parse LLM JSON response with fallback for common formatting issues.

    阶梯与裸引号修复统一在 ``llm_json.loads_tolerant``。
    """
    data = loads_tolerant(response, expect=list)
    if data is None:
        logger.warning("Phase 1: could not parse JSON from response: %s", response[:200])
        return []
    return data


# ── Phase 1: Session extraction ────────────────────────────────────────────


async def extract_phase1(
    messages: list[dict],
    api_client: Any,
    memory_store: Any,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    model: str | None = None,
) -> dict:
    """Extract knowledge from a completed conversation and append to memory files.

    Args:
        messages: List of ``{"role": "...", "content": "..."}`` dicts.
        api_client: ``OpenAIClient`` instance for the LLM extraction call.
        memory_store: ``MemoryStore`` instance for writing entries.
        confidence_threshold: Minimum confidence to keep an entry (default 0.6).
        model: Optional model override (defaults to config.model).

    Returns:
        Summary dict: ``{extracted, added, skipped_duplicate, skipped_low_confidence}``.
    """
    if not _PHASE1_TPL:
        return {"extracted": 0, "added": 0, "skipped_duplicate": 0,
                "skipped_low_confidence": 0, "error": "phase1.md template not found"}

    conv_text = build_conversation_text(messages)
    if not conv_text.strip():
        return {"extracted": 0, "added": 0, "skipped_duplicate": 0,
                "skipped_low_confidence": 0}

    # ── Call LLM ────────────────────────────────────────────────────────
    prompt = _PHASE1_TPL.format(conversation_text=conv_text)

    try:
        with usage_log.source_scope("archive"):
            response = await api_client.complete(
                system_prompt=_EXTRACT_SYSTEM_PROMPT,
                user_message=prompt,
                model=model,
                scenario="memory.archive",
            )
    except Exception as exc:
        logger.error("Phase 1 LLM call failed: %s", exc)
        return {"extracted": 0, "added": 0, "skipped_duplicate": 0,
                "skipped_low_confidence": 0, "error": str(exc)}

    if not response:
        return {"extracted": 0, "added": 0, "skipped_duplicate": 0,
                "skipped_low_confidence": 0}

    # ── Parse & filter ──────────────────────────────────────────────────
    entries = _parse_json_response(response)
    if not entries:
        return {"extracted": 0, "added": 0, "skipped_duplicate": 0,
                "skipped_low_confidence": 0}

    stats = {
        "extracted": len(entries),
        "added": 0,
        "skipped_duplicate": 0,
        "skipped_low_confidence": 0,
    }

    for entry in entries:
        category = entry.get("category", "").upper().strip()
        content = entry.get("content", "").strip()
        confidence = entry.get("confidence", 0.5)

        if confidence < confidence_threshold:
            stats["skipped_low_confidence"] += 1
            continue

        target = CATEGORY_ROUTES.get(category, "memory")
        label = CATEGORY_LABELS.get(category, "")
        formatted = label + content

        result = memory_store.add(target, formatted)
        if result.get("success"):
            stats["added"] += 1
        else:
            stats["skipped_duplicate"] += 1

    if stats["added"] > 0:
        logger.info(
            "Phase 1: %d extracted → %d added, %d duplicates, %d low-confidence",
            stats["extracted"], stats["added"],
            stats["skipped_duplicate"], stats["skipped_low_confidence"],
        )

    return stats


# ── Phase 2: Global consolidation ──────────────────────────────────────────


def _count_entries(memory_store: Any) -> dict:
    """Count entries and characters across both memory targets."""
    mem_count = len(memory_store.memory_entries)
    user_count = len(memory_store.user_entries)
    mem_chars = sum(len(e) for e in memory_store.memory_entries)
    user_chars = sum(len(e) for e in memory_store.user_entries)
    return {
        "memory_entries": mem_count,
        "user_entries": user_count,
        "memory_chars": mem_chars + (3 * max(0, mem_count - 1)),  # § separators
        "user_chars": user_chars + (3 * max(0, user_count - 1)),
    }


def _diff_entries(before: list[str], after: list[str]) -> dict:
    """Compute added/removed entries between two lists."""
    before_set = set(before)
    after_set = set(after)
    return {
        "added": [e for e in after if e not in before_set],
        "removed": [e for e in before if e not in after_set],
    }


def needs_consolidation(memory_store: Any) -> bool:
    """Check whether Phase 2 consolidation should be triggered."""
    counts = _count_entries(memory_store)
    total_entries = counts["memory_entries"] + counts["user_entries"]
    total_chars = counts["memory_chars"] + counts["user_chars"]
    return (
        total_entries > PHASE2_ENTRY_THRESHOLD
        or total_chars > PHASE2_CHAR_THRESHOLD
    )


async def consolidate_phase2(
    api_client: Any,
    memory_store: Any,
    *,
    model: str | None = None,
) -> dict:
    """Consolidate MEMORY.md and USER.md — dedup, reclassify, remove stale entries.

    Reads the full content of both files, calls the LLM to produce cleaned
    versions, and overwrites the files with the result.

    Returns a summary dict with before/after stats.
    """
    before_counts = _count_entries(memory_store)
    old_memory = list(memory_store.memory_entries)
    old_user = list(memory_store.user_entries)

    # Build input for LLM
    memory_text = "\n§\n".join(old_memory) if old_memory else "(empty)"
    user_text = "\n§\n".join(old_user) if old_user else "(empty)"

    prompt = _PHASE2_TPL.format(memory_content=memory_text, user_content=user_text)

    try:
        with usage_log.source_scope("archive"):
            response = await api_client.complete(
                system_prompt="你是一个精确的文本整理助手。只输出要求的 JSON 格式，不要额外内容。",
                user_message=prompt,
                model=model,
                scenario="memory.archive",
            )
    except Exception as exc:
        logger.error("Phase 2 LLM call failed: %s", exc)
        return {"before": before_counts, "after": before_counts, "error": str(exc)}

    if not response:
        return {"before": before_counts, "after": before_counts, "error": "Empty LLM response"}

    # Parse the cleaned output
    parsed = _parse_phase2_response(response)
    if not parsed:
        return {"before": before_counts, "after": before_counts, "error": "Could not parse Phase 2 output"}

    new_memory = parsed.get("memory", [])
    new_user = parsed.get("user", [])

    # Compute diffs before writing
    mem_diff = _diff_entries(old_memory, new_memory)
    user_diff = _diff_entries(old_user, new_user)

    # Write cleaned entries back
    memory_store.memory_entries = list(new_memory)
    memory_store.user_entries = list(new_user)
    memory_store.save_to_disk("memory")
    memory_store.save_to_disk("user")
    memory_store._freeze()

    after_counts = _count_entries(memory_store)
    logger.info(
        "Phase 2: memory %d→%d entries, user %d→%d entries",
        before_counts["memory_entries"], after_counts["memory_entries"],
        before_counts["user_entries"], after_counts["user_entries"],
    )

    return {
        "before": before_counts,
        "after": after_counts,
        "diff": {
            "memory": mem_diff,
            "user": user_diff,
        },
    }


def _parse_phase2_response(response: str) -> dict | None:
    """Parse Phase 2 JSON output: {"memory": [...], "user": [...]}.

    阶梯与裸引号修复统一在 ``llm_json.loads_tolerant``。

    键校验对**所有**解析路径一律生效（旧代码只有直解分支校验，两个 fallback
    分支接受任意 dict）。这不只是整洁问题：调用方拿到结果后会
    ``memory_store.memory_entries = list(parsed.get("memory", []))`` 直接覆盖
    并落盘 —— 一个围栏包裹的 ``{"foo": 1}`` 会让旧代码**清空全部记忆**。
    """
    data = loads_tolerant(response, expect=dict)
    if isinstance(data, dict) and ("memory" in data or "user" in data):
        return data
    logger.warning("Phase 2: could not parse JSON from response: %s", response[:200])
    return None


# ── Combined entry point ───────────────────────────────────────────────────


async def run_archive(
    scope: str,
    messages: list[dict],
    api_client: Any,
    memory_store: Any,
    *,
    model: str | None = None,
) -> str:
    """Run archive pipeline and return a JSON-encoded summary.

    Args:
        scope: ``"session"`` (Phase 1 only) or ``"full"`` (Phase 1 + Phase 2).
        messages: Current session messages for Phase 1 extraction.
        api_client: ``OpenAIClient`` instance.
        memory_store: ``MemoryStore`` instance.
        model: Optional model override.

    Returns:
        JSON string with archive results.
    """
    if scope not in ("session", "full"):
        return json.dumps(
            {"success": False, "error": f"Unknown scope '{scope}'. Use 'session' or 'full'."},
            ensure_ascii=False,
        )

    result: dict = {"success": True, "scope": scope}

    # Phase 1 — always run
    phase1 = await extract_phase1(messages, api_client, memory_store, model=model)
    result["phase1"] = phase1

    # Phase 2 — only for "full" scope
    if scope == "full":
        phase2 = await consolidate_phase2(api_client, memory_store, model=model)
        result["phase2"] = {"before": phase2.get("before"), "after": phase2.get("after")}
        if "error" in phase2:
            result["phase2"]["error"] = phase2["error"]

    return json.dumps(result, ensure_ascii=False)
