"""L1 memory extractor: single LLM call for scene segmentation + memory extraction.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from memory_pipeline.prompts import L1_EXTRACTION_SYSTEM_PROMPT, format_extraction_prompt

logger = logging.getLogger(__name__)

# How many recent messages to extract from (vs. background context)
DEFAULT_NEW_MESSAGE_COUNT = 10
DEFAULT_BACKGROUND_COUNT = 5


@dataclass
class ExtractedMemory:
    """A single extracted memory before dedup."""

    content: str
    type: str = "episodic"  # persona | episodic | instruction
    priority: int = 50
    source_message_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Assigned after extraction for dedup tracking
    record_id: str = ""
    scene_name: str = ""


@dataclass
class ExtractionResult:
    """Result of L1 extraction: scenes + memories."""

    scenes: list[dict] = field(default_factory=list)
    memories: list[ExtractedMemory] = field(default_factory=list)
    last_scene_name: str = "无"


async def extract_l1(
    api_client: Any,
    messages: list[dict[str, Any]],
    *,
    previous_scene_name: str = "无",
    model: str | None = None,
    timeout: int = 120,
) -> ExtractionResult | None:
    """Extract L1 memories from buffered conversation messages.

    Single LLM call that does both scene segmentation and memory extraction.
    Messages are split into "new" (extraction source) and "background" (context only).

    Args:
        api_client: object with async complete(system_prompt, user_message, *, model, timeout) -> str|None
        messages: buffered conversation messages, each with id/role/content/timestamp
        previous_scene_name: last scene name from prior extraction (for continuity)
        model: optional model override
        timeout: LLM call timeout in seconds

    Returns:
        ExtractionResult with scenes and memories, or None on failure.
    """
    if not messages:
        return None

    # Split messages: last N are "new", preceding M are "background"
    n_new = min(len(messages), DEFAULT_NEW_MESSAGE_COUNT)
    n_bg = min(max(len(messages) - n_new, 0), DEFAULT_BACKGROUND_COUNT)

    new_messages = messages[-n_new:]
    background_messages = messages[-(n_new + n_bg):-n_new] if n_bg > 0 else []

    # Ensure messages have required fields
    for m in new_messages:
        if "id" not in m:
            m["id"] = f"msg_{id(m)}"

    user_prompt = format_extraction_prompt(
        new_messages=new_messages,
        background_messages=background_messages,
        previous_scene_name=previous_scene_name,
    )

    try:
        raw = await api_client.complete(
            L1_EXTRACTION_SYSTEM_PROMPT,
            user_prompt,
            model=model,
            timeout=timeout,
        )
    except Exception as e:
        logger.warning("L1 extraction LLM call failed: %s", e)
        return None

    if not raw:
        logger.warning("L1 extraction returned empty response")
        return None

    return _parse_extraction_result(raw, previous_scene_name)


def _parse_extraction_result(raw: str, previous_scene_name: str) -> ExtractionResult:
    """Parse the LLM JSON response into ExtractionResult."""
    cleaned = raw.strip()

    # Strip markdown code block wrappers
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    # Extract JSON array
    array_match = re.search(r"\[[\s\S]*\]", cleaned)
    if not array_match:
        logger.warning("L1 extraction: no JSON array found in response")
        return ExtractionResult(last_scene_name=previous_scene_name)

    try:
        parsed = json.loads(array_match.group())
    except json.JSONDecodeError as e:
        logger.warning("L1 extraction: JSON parse failed: %s", e)
        return ExtractionResult(last_scene_name=previous_scene_name)

    if not isinstance(parsed, list):
        logger.warning("L1 extraction: response is not an array")
        return ExtractionResult(last_scene_name=previous_scene_name)

    scenes: list[dict] = []
    memories: list[ExtractedMemory] = []
    last_scene_name = previous_scene_name

    for scene in parsed:
        if not isinstance(scene, dict):
            continue

        scene_name = scene.get("scene_name", previous_scene_name)
        last_scene_name = scene_name

        scene_entry = {
            "scene_name": scene_name,
            "message_ids": scene.get("message_ids", []),
            "memory_count": 0,
        }

        for mem in scene.get("memories", []):
            if not isinstance(mem, dict):
                continue
            content = mem.get("content", "").strip()
            if not content:
                continue

            mem_type = mem.get("type", "episodic")
            if mem_type not in ("persona", "episodic", "instruction"):
                mem_type = "episodic"

            priority = mem.get("priority", 50)
            try:
                priority = int(priority)
            except (ValueError, TypeError):
                priority = 50

            memories.append(
                ExtractedMemory(
                    content=content,
                    type=mem_type,
                    priority=priority,
                    source_message_ids=mem.get("source_message_ids", []),
                    metadata=mem.get("metadata", {}),
                    scene_name=scene_name,
                )
            )
            scene_entry["memory_count"] += 1

        scenes.append(scene_entry)

    logger.info(
        "L1 extraction: %d scenes, %d memories (last_scene=%s)",
        len(scenes),
        len(memories),
        last_scene_name,
    )

    return ExtractionResult(
        scenes=scenes,
        memories=memories,
        last_scene_name=last_scene_name,
    )
