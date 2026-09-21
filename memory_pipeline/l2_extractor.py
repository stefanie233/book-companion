"""L2 scene block extractor: aggregate unconsumed L1 memories into per-book scene blocks.

Single LLM call (L2-2: 先单次调用, 场景块 >几十个时再升子 Agent).
Reads unconsumed L1 memories from wingman DB, feeds them + existing scene
blocks to the LLM, and applies create/update decisions to SceneStore.

Adapted from TencentDB Agent Memory 2.0 (MemoryCore/src/core/scene/),
book-centric for Book Companion (L2-1: 按书组织).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from memory_pipeline.prompts import L2_SCENE_SYSTEM_PROMPT, format_l2_prompt
from memory_pipeline.scene_store import DEFAULT_SECTIONS, SceneBlock, SceneStore

logger = logging.getLogger(__name__)

# How many unconsumed L1 memories to feed per L2 run (batch cap)
DEFAULT_L2_BATCH = 30
# Maximum active scene blocks shown to the LLM (soft context budget)
MAX_EXISTING_BLOCKS = 20


@dataclass
class L2Result:
    """Result of one L2 extraction run."""

    processed_books: list[str] = field(default_factory=list)
    consumed_count: int = 0
    errors: list[str] = field(default_factory=list)


async def extract_l2(
    api_client: Any,
    wingman_db: Any,
    scene_store: SceneStore,
    *,
    model: str | None = None,
    timeout: int = 120,
    batch: int = DEFAULT_L2_BATCH,
) -> L2Result:
    """Aggregate unconsumed L1 memories into scene blocks (single LLM call).

    Args:
        api_client: object with async complete(system_prompt, user_message, *, model, timeout)
        wingman_db: WingmanDatabase (get_unconsumed_memories / mark_scene_consumed)
        scene_store: SceneStore for reading/writing scene blocks
        model: optional model override
        timeout: LLM call timeout in seconds
        batch: max unconsumed memories to process per run

    Returns:
        L2Result with processed books and consumed memory count.
    """
    result = L2Result()

    # 1. Read unconsumed L1 memories
    memories = wingman_db.get_unconsumed_memories(limit=batch)
    if not memories:
        return result

    # 2. Load existing scene blocks (active only, capped)
    existing_blocks: list[dict[str, Any]] = []
    active = scene_store.list_active()[:MAX_EXISTING_BLOCKS]
    for block in active:
        parsed = scene_store.parse_scene_file(scene_store.read_scene_file(block) or "")
        parsed["heat"] = block.heat
        existing_blocks.append(parsed)

    # 3. Build prompt + call LLM
    new_memories = [
        {
            "id": m.id,
            "content": m.content,
            "memory_type": m.memory_type,
            "priority": m.priority,
            "scene_name": m.scene_name,
        }
        for m in memories
    ]

    user_prompt = format_l2_prompt(existing_blocks, new_memories)
    try:
        raw = await api_client.complete(
            L2_SCENE_SYSTEM_PROMPT,
            user_prompt,
            model=model,
            timeout=timeout,
        )
    except Exception as e:
        logger.warning("L2 extraction LLM call failed: %s", e)
        result.errors.append(str(e))
        return result

    if not raw:
        logger.warning("L2 extraction returned empty response")
        return result

    # 4. Parse decisions
    ops = _parse_l2_result(raw)
    if not ops:
        return result

    # 5. Apply decisions
    consumed_ids: list[str] = []
    for op in ops:
        try:
            _apply_scene_op(scene_store, op)
            result.processed_books.append(op.get("book", ""))
            consumed_ids.extend(op.get("consumed_ids", []))
        except Exception as e:
            logger.warning("L2 apply scene op failed for %s: %s",
                           op.get("book", "?"), e)
            result.errors.append(str(e))

    # 6. Mark consumed
    if consumed_ids:
        wingman_db.mark_scene_consumed(consumed_ids)
        result.consumed_count = len(consumed_ids)

    logger.info(
        "L2 pipeline complete: %d books updated, %d memories consumed",
        len(result.processed_books),
        result.consumed_count,
    )
    return result


def _parse_l2_result(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM JSON response into scene ops.

    Each op: {book, action, summary, sections, consumed_ids}
    """
    cleaned = raw.strip()

    # Strip markdown code block wrappers
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    # Extract JSON array
    array_match = re.search(r"\[[\s\S]*\]", cleaned)
    if not array_match:
        logger.warning("L2 extraction: no JSON array found in response")
        return []

    try:
        parsed = json.loads(array_match.group())
    except json.JSONDecodeError as e:
        logger.warning("L2 extraction: JSON parse failed: %s", e)
        return []

    if not isinstance(parsed, list):
        logger.warning("L2 extraction: response is not an array")
        return []

    ops: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        book = str(item.get("book", "")).strip()
        if not book:
            continue

        action = str(item.get("action", "create"))
        if action not in ("create", "update"):
            action = "create"

        sections = item.get("sections")
        if not isinstance(sections, dict):
            sections = {}

        ops.append(
            {
                "book": book,
                "action": action,
                "summary": str(item.get("summary", ""))[:60],
                "sections": {
                    k: str(v).strip()
                    for k, v in sections.items()
                    if k in DEFAULT_SECTIONS and str(v).strip()
                },
                "consumed_ids": [
                    str(x) for x in item.get("consumed_ids", []) if str(x).strip()
                ],
            }
        )

    if ops:
        logger.info(
            "L2 extraction: %d scene ops (create=%d, update=%d)",
            len(ops),
            sum(1 for o in ops if o["action"] == "create"),
            sum(1 for o in ops if o["action"] == "update"),
        )
    return ops


def _apply_scene_op(scene_store: SceneStore, op: dict[str, Any]) -> None:
    """Apply one scene op (create or update) to the store."""
    book = op["book"]
    existing = scene_store.get(book)

    # Merge with existing sections (update preserves old content)
    merged_sections: dict[str, str] = {}
    if existing is not None:
        old = scene_store.parse_scene_file(scene_store.read_scene_file(existing) or "")
        merged_sections.update(old.get("sections", {}))
    merged_sections.update(op.get("sections", {}))

    block = SceneBlock(
        book=book,
        summary=op.get("summary", ""),
        sections=merged_sections,
    )
    scene_store.upsert(block, existing=existing)
