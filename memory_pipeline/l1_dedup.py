"""L1 memory dedup: two-phase conflict detection.

Phase 1: Candidate recall (no LLM, DB search by similarity)
Phase 2: Batch LLM judgment (single call, store/skip/update/merge)
"""

from __future__ import annotations

import logging
from typing import Any

from llm_json import loads_tolerant
from memory_pipeline.prompts import L1_DEDUP_SYSTEM_PROMPT, format_dedup_prompt

logger = logging.getLogger(__name__)

DEFAULT_CONFLICT_RECALL_TOP_K = 5
VALID_TYPES = {"persona", "episodic", "instruction"}
VALID_ACTIONS = {"store", "skip", "update", "merge"}


async def dedup_l1(
    api_client: Any,
    wingman_db: Any,
    new_memories: list[dict[str, Any]],
    *,
    model: str | None = None,
    timeout: int = 120,
    conflict_recall_top_k: int = DEFAULT_CONFLICT_RECALL_TOP_K,
) -> list[dict[str, Any]]:
    """Two-phase dedup: candidate recall + batch LLM judgment.

    Args:
        api_client: object with async complete() method
        wingman_db: WingmanDatabase with search_memories_by_similarity()
        new_memories: list of {record_id, content, type, priority, scene_name, ...}
        model: optional model override
        timeout: LLM call timeout
        conflict_recall_top_k: max candidates per new memory

    Returns:
        list of DedupDecision dicts: {record_id, action, target_ids,
        merged_content, merged_type, merged_priority, merged_timestamps}
    """
    if not new_memories:
        return []

    store_all = lambda: [
        {"record_id": m["record_id"], "action": "store", "target_ids": []}
        for m in new_memories
    ]

    # ── Phase 1: Candidate recall (no LLM) ──────────────────────────
    matches: list[dict] = []
    any_candidates = False

    for mem in new_memories:
        candidates = wingman_db.search_memories_by_similarity(
            mem["content"], limit=conflict_recall_top_k
        )
        # Convert MemoryEntryRow to plain dicts
        cand_dicts = [
            {
                "id": c.id,
                "content": c.content,
                "memory_type": c.memory_type,
                "priority": c.priority,
                "scene_name": c.scene_name,
            }
            for c in candidates
        ]
        if cand_dicts:
            any_candidates = True
        matches.append({"newMemory": mem, "candidates": cand_dicts})

    if not any_candidates:
        logger.debug("L1 dedup: no similar records found, all will be stored")
        return store_all()

    # ── Phase 2: Batch LLM judgment ─────────────────────────────────
    user_prompt = format_dedup_prompt(matches)

    try:
        raw = await api_client.complete(
            L1_DEDUP_SYSTEM_PROMPT,
            user_prompt,
            model=model,
            timeout=timeout,
            scenario="memory.l1_dedup",
        )
    except Exception as e:
        logger.warning("L1 dedup LLM call failed, defaulting all to store: %s", e)
        return store_all()

    if not raw:
        logger.warning("L1 dedup returned empty response, defaulting to store")
        return store_all()

    decisions = _parse_dedup_result(raw, new_memories)
    return decisions


def _parse_dedup_result(
    raw: str,
    new_memories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Parse the LLM JSON response into dedup decisions.

    阶梯与裸引号修复统一在 ``llm_json.loads_tolerant``。失败退回
    ``_fallback_store_all``（全部当新记忆入库）—— 不丢数据，但重复记忆会累积。
    """
    parsed = loads_tolerant(raw, expect=list)
    if parsed is None:
        logger.warning("L1 dedup: no parseable JSON array in response")
        return _fallback_store_all(new_memories)

    decisions: list[dict[str, Any]] = []

    for item in parsed:
        if not isinstance(item, dict):
            continue

        record_id = str(item.get("record_id", ""))
        if not record_id:
            continue

        action = str(item.get("action", "store"))
        if action not in VALID_ACTIONS:
            logger.warning("L1 dedup: invalid action '%s' for %s, defaulting to store", action, record_id)
            action = "store"

        merged_type = item.get("merged_type")
        if merged_type and merged_type not in VALID_TYPES:
            merged_type = None

        decisions.append(
            {
                "record_id": record_id,
                "action": action,
                "target_ids": [str(t) for t in item.get("target_ids", [])],
                "merged_content": item.get("merged_content"),
                "merged_type": merged_type,
                "merged_priority": (
                    int(item["merged_priority"])
                    if isinstance(item.get("merged_priority"), (int, float))
                    else None
                ),
                "merged_timestamps": item.get("merged_timestamps"),
            }
        )

    # Ensure all memories have a decision (fill missing with "store")
    decided_ids = {d["record_id"] for d in decisions}
    for mem in new_memories:
        if mem["record_id"] not in decided_ids:
            decisions.append(
                {"record_id": mem["record_id"], "action": "store", "target_ids": []}
            )

    store_count = sum(1 for d in decisions if d["action"] == "store")
    skip_count = sum(1 for d in decisions if d["action"] == "skip")
    update_count = sum(1 for d in decisions if d["action"] == "update")
    merge_count = sum(1 for d in decisions if d["action"] == "merge")

    logger.info(
        "L1 dedup: %d decisions (store=%d, skip=%d, update=%d, merge=%d)",
        len(decisions), store_count, skip_count, update_count, merge_count,
    )

    return decisions


def _fallback_store_all(
    new_memories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fallback: store all memories when parsing fails."""
    return [
        {"record_id": m["record_id"], "action": "store", "target_ids": []}
        for m in new_memories
    ]
