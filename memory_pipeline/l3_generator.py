"""L3 persona generator: synthesize user profile from scenes + unconsumed L1 persona.

Single LLM call (like L1/L2). Reads changed scene blocks + unconsumed
high-priority L1 persona/instruction memories + existing persona.md, generates
the full persona.md document, writes a versioned snapshot to persona_docs
(git-like), atomically writes data/memory/persona.md, and marks consumed the
L1 memories it fed.

Persona / USER.md authority split (2026-08-13):
  persona.md = AI 观察投影（描述性真实，L3 管理）
  USER.md    = 用户自我陈述（期望的对待方式，builtin 管，L3 不写）
Conflict handling is deferred (plan 4.6 TBD).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_pipeline.prompts import L3_PERSONA_SYSTEM_PROMPT, format_l3_prompt

logger = logging.getLogger(__name__)

# Max persona content length (chars) — chat-mode default
DEFAULT_PERSONA_MAX_CHARS = 2000
# Only persona/instruction L1 memories above this priority feed L3 (life profile)
DEFAULT_L3_PRIORITY_FLOOR = 60
# P4: regenerate persona every N new L1 memories (triggerEveryN default)
DEFAULT_PERSONA_TRIGGER_EVERY_N = 50
# Soft context budget: how many unconsumed persona memories / scenes to feed
MAX_L3_PERSONA_MEMORIES = 30
MAX_L3_CHANGED_SCENES = 20


def check_persona_trigger(
    wingman_db: Any,
    scene_store: Any,
    *,
    trigger_every_n: int = DEFAULT_PERSONA_TRIGGER_EVERY_N,
) -> tuple[bool, str]:
    """Decide whether to run L3 persona generation. Returns (should_run, reason).

    L3 trigger set (adapted to the book-centric pipeline):
      P1  explicit    — agent set request_persona_update flag (persona tool)
      P2  cold start  — scenes / unconsumed persona memories exist but no
                        persona has ever been generated
      P4  threshold   — memories_since_last_persona >= trigger_every_n
    P2.5 (recovery) is covered by reconcile_persona_file: a missing persona.md
    is restored from the latest DB snapshot, no regeneration needed.

    Cost control stays in generate_l3 (skips when nothing changed).
    """
    # P1: explicit request — highest priority, always honored
    cp = wingman_db.get_persona_checkpoint()
    if cp["request_persona_update"]:
        reason = cp["persona_update_reason"].strip() or "explicit request"
        return True, f"P1 {reason}"

    latest = wingman_db.get_latest_persona_doc()

    # P2: cold start — data exists but never generated a persona
    if latest is None:
        has_active = bool(scene_store.list_active()[:1])
        has_persona_mem = bool(
            wingman_db.get_unconsumed_persona_memories(limit=1)
        )
        if has_active or has_persona_mem:
            return True, "P2 cold start"

    # P4: threshold on new memories since the last generation
    memories_since = cp["memories_since_last_persona"]
    if memories_since >= trigger_every_n:
        return True, f"P4 threshold ({memories_since} memories)"

    return False, ""


@dataclass
class L3Result:
    """Result of one L3 generation run."""

    generated: bool = False
    mode: str = "skipped"   # "first" | "incremental" | "skipped"
    version: int = 0
    reason: str = ""
    errors: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    """Atomic temp-file + rename write (readers never see half-written file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".persona_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def reconcile_persona_file(path: Path, wingman_db: Any) -> str:
    """Make persona.md consistent with persona_docs (git-like reconcile).

    Handles the two failure modes of an editable md file:
      1. 用户直接改了 persona.md（md5 与 DB 最新版不一致）→ 记一条
         source='user_overlay' 版本（追溯用户改动），改动保留。
      2. 用户删了 / 清空了 persona.md → 从 DB 最新快照重建。

    Returns the reconciled persona content (or "" if none exists yet).
    """
    path = Path(path)
    md_content = ""
    if path.exists():
        try:
            md_content = path.read_text(encoding="utf-8").strip()
        except OSError:
            md_content = ""

    latest = wingman_db.get_latest_persona_doc()

    if md_content:
        if latest and latest.content.strip() and _md5(md_content) != latest.content_md5:
            # User modified the file after the last L3 commit → record the edit.
            try:
                wingman_db.insert_persona_doc(md_content, source="user_overlay")
                logger.info("L3 reconcile: user edit on %s recorded as user_overlay", path)
            except Exception as e:
                logger.warning("L3 reconcile: failed to record user edit: %s", e)
        return md_content

    # Missing or empty file → restore from DB snapshot if we have one.
    if latest and latest.content.strip():
        try:
            _atomic_write(path, latest.content)
            logger.info("L3 reconcile: restored %s from DB snapshot v%d", path, latest.version)
        except OSError as e:
            logger.warning("L3 reconcile: restore failed: %s", e)
        return latest.content

    return ""


async def generate_l3(
    api_client: Any,
    wingman_db: Any,
    scene_store: Any,
    persona_md_path: str | Path,
    *,
    model: str | None = None,
    timeout: int = 120,
    max_chars: int = DEFAULT_PERSONA_MAX_CHARS,
    priority_floor: int = DEFAULT_L3_PRIORITY_FLOOR,
    trigger_info: str = "",
) -> L3Result:
    """Generate/update the persona from changed scenes + unconsumed L1 persona.

    Args:
        api_client: object with async complete(system_prompt, user_message, *, model, timeout)
        wingman_db: WingmanDatabase (persona_docs / pipeline_state / L1 queries)
        scene_store: SceneStore (list_active / read_scene_file / parse_scene_file)
        persona_md_path: data/memory/persona.md (working copy)
        model: optional model override
        timeout: LLM call timeout in seconds
        max_chars: persona length cap (chars)
        priority_floor: min priority for unconsumed persona/instruction L1
        trigger_info: P1/P2/P4 reason string (shown in prompt)

    Returns:
        L3Result with generated flag / mode / version.
    """
    result = L3Result()

    # 1. Reconcile persona.md with DB (user edit → user_overlay; missing → restore)
    existing_persona = reconcile_persona_file(persona_md_path, wingman_db)
    mode = "incremental" if existing_persona else "first"
    result.mode = mode

    # 2. Changed scenes since last persona (compare ISO updated vs last_persona_time)
    cp = wingman_db.get_persona_checkpoint()
    last_persona_time = cp["last_persona_time"]
    active = scene_store.list_active()[:MAX_L3_CHANGED_SCENES]

    changed: list[Any] = []
    for block in active:
        updated = getattr(block, "updated", "") or ""
        if not last_persona_time or updated > last_persona_time:
            changed.append(block)

    changed_scenes_content = ""
    if changed:
        parts: list[str] = []
        for i, block in enumerate(changed, 1):
            raw = scene_store.read_scene_file(block)
            if not raw:
                continue
            parts.append(f"### [{i}] {getattr(block, 'filename', '') or getattr(block, 'book', '')}\n\n```markdown\n{raw}\n```")
        changed_scenes_content = "\n\n".join(parts)

    # 3. Unconsumed high-priority persona/instruction L1 (life profile channel)
    persona_memories = wingman_db.get_unconsumed_persona_memories(
        limit=MAX_L3_PERSONA_MEMORIES, priority_floor=priority_floor,
    )
    persona_mem_text = "\n\n".join(
        f"- [id={m.id} | {m.memory_type} | p={m.priority}] {m.content}"
        for m in persona_memories
    )

    # 4. If nothing changed and persona exists → skip (cost control)
    if not changed_scenes_content and not persona_mem_text and existing_persona:
        logger.info("L3 skipped: no scene changes and no new persona memories")
        result.reason = "no changes"
        return result

    # 5. Build prompt + call LLM
    user_prompt = format_l3_prompt(
        mode=mode,
        total_processed=int(wingman_db.get_pipeline_state("total_processed", "0") or 0),
        scene_count=len(active),
        changed_scene_count=len(changed),
        changed_scenes_content=changed_scenes_content,
        unconsumed_persona_content=persona_mem_text,
        persona_memory_count=len(persona_memories),
        existing_persona=existing_persona,
        trigger_info=trigger_info,
    )

    try:
        raw = await api_client.complete(
            L3_PERSONA_SYSTEM_PROMPT,
            user_prompt,
            model=model,
            timeout=timeout,
            scenario="memory.l3",
        )
    except Exception as e:
        logger.warning("L3 generation LLM call failed: %s", e)
        result.errors.append(str(e))
        return result

    if not raw or not raw.strip():
        logger.warning("L3 generation returned empty response")
        result.errors.append("empty LLM response")
        return result

    persona_content = raw.strip()
    if len(persona_content) > max_chars:
        logger.warning(
            "L3 persona exceeds %d chars (%d) — keeping as-is (prompt cap should bound)",
            max_chars, len(persona_content),
        )

    # 6. Write DB version + atomically write persona.md (dual write)
    try:
        result.version = wingman_db.insert_persona_doc(persona_content, source="l3")
        path = Path(persona_md_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, persona_content + "\n")
    except Exception as e:
        logger.warning("L3 persona write failed: %s", e)
        result.errors.append(str(e))
        return result

    # 7. Consume the fed L1 memories (mark scene_consumed; rows stay searchable)
    if persona_memories:
        wingman_db.mark_scene_consumed([m.id for m in persona_memories])

    # 8. Advance checkpoint
    wingman_db.mark_persona_generated()

    result.generated = True
    result.reason = trigger_info or ("首次生成" if mode == "first" else "迭代更新")
    logger.info(
        "L3 pipeline complete: mode=%s, version=%d, %d scenes changed, %d persona memories consumed",
        mode, result.version, len(changed), len(persona_memories),
    )
    return result
