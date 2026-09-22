"""Memory pipeline scheduler: warm-up + idle timeout + shutdown flush.

Manages L0 message buffering and triggers L1 extraction at the right time.

Trigger paths:
  A. Conversation threshold (warm-up: 1 -> 2 -> 4 -> 8 -> N)
  B. Idle timeout (default 120s)
  C. Shutdown / session end flush
  D. Pre-compress (before context compression)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from memory_pipeline.l1_extractor import extract_l1, ExtractedMemory
from memory_pipeline.l1_dedup import dedup_l1
from memory_pipeline.l2_extractor import extract_l2
from memory_pipeline.scene_store import SceneStore
import usage_log
from memory_pipeline.l3_generator import (
    DEFAULT_L3_PRIORITY_FLOOR,
    DEFAULT_PERSONA_MAX_CHARS,
    DEFAULT_PERSONA_TRIGGER_EVERY_N,
    check_persona_trigger,
    generate_l3,
)

logger = logging.getLogger(__name__)

# L2 debounce: run L2 aggregation this many seconds after an L1 success
DEFAULT_L2_DEBOUNCE = 90
# L3 debounce: run persona generation this many seconds after an L2 success
DEFAULT_L3_DEBOUNCE = 5
# Archive cold books at most once per L2 run when overdue books exist
DEFAULT_COLD_CHECK_INTERVAL = 3600  # seconds between cold-tier checks


class MemoryPipelineScheduler:
    """Schedules L1 extraction based on conversation count and idle timeout.

    Lifecycle:
      - notify_conversation(): called each turn, buffers messages + checks threshold
      - flush(): called on shutdown/session_end, processes remaining buffer
      - on_pre_compress(): called before compact, triggers extraction

    Warm-up mode:
      New sessions start with threshold=1 (first conversation triggers L1 immediately).
      Threshold doubles after each successful L1 run: 1 -> 2 -> 4 -> 8 -> N (steady state).
    """

    def __init__(
        self,
        api_client: Any,
        wingman_db: Any,
        *,
        scenes_dir: str | None = None,
        every_n: int = 5,
        idle_timeout: int = 120,
        model: str | None = None,
        enable_warmup: bool = True,
        l2_debounce: int = DEFAULT_L2_DEBOUNCE,
        cold_days: int = 30,
        persona_path: str | None = None,
        persona_trigger_every_n: int = DEFAULT_PERSONA_TRIGGER_EVERY_N,
        persona_max_chars: int = DEFAULT_PERSONA_MAX_CHARS,
        persona_priority_floor: int = DEFAULT_L3_PRIORITY_FLOOR,
        l3_debounce: int = DEFAULT_L3_DEBOUNCE,
    ) -> None:
        self._api_client = api_client
        self._wingman_db = wingman_db
        self._every_n = every_n
        self._idle_timeout = idle_timeout
        self._model = model
        self._enable_warmup = enable_warmup
        self._l2_debounce = l2_debounce

        # L3 persona generation (enabled when persona_path provided)
        self._persona_path = Path(persona_path) if persona_path else None
        self._persona_trigger_every_n = persona_trigger_every_n
        self._persona_max_chars = persona_max_chars
        self._persona_priority_floor = persona_priority_floor
        self._l3_debounce = l3_debounce

        # L2 scene blocks (created lazily if scenes_dir provided)
        self._scene_store: SceneStore | None = None
        if scenes_dir:
            self._scene_store = SceneStore(
                Path(scenes_dir), cold_days=cold_days,
            )
            self._scene_store.initialize()

        # State
        self._buffer: list[dict[str, Any]] = []
        self._conversation_count = 0
        self._warmup_threshold = 1 if enable_warmup else every_n
        self._previous_scene_name = "无"
        self._idle_timer: asyncio.TimerHandle | None = None
        self._l1_running = False  # prevent overlapping L1 runs
        self._l2_running = False  # prevent overlapping L2 runs
        self._l2_timer: asyncio.TimerHandle | None = None
        self._l3_running = False  # prevent overlapping L3 runs
        self._l3_timer: asyncio.TimerHandle | None = None
        self._last_cold_check = 0.0  # monotonic seconds
        self._session_id: str = ""

    def set_session_id(self, session_id: str) -> None:
        """Update session_id for tagging extracted memories."""
        self._session_id = session_id

    def notify_conversation(self, user_msg: str, assistant_msg: str) -> None:
        """Buffer a completed conversation turn and check if L1 should trigger.

        Called by WingmanMemoryProvider.sync_turn() after each turn.
        """
        # Buffer messages with IDs for extraction
        msg_id = uuid.uuid4().hex[:8]
        self._buffer.append(
            {
                "id": f"msg_{msg_id}_u",
                "role": "user",
                "content": user_msg,
                "timestamp": _now_iso(),
            }
        )
        self._buffer.append(
            {
                "id": f"msg_{msg_id}_a",
                "role": "assistant",
                "content": assistant_msg,
                "timestamp": _now_iso(),
            }
        )
        self._conversation_count += 1

        # Reset idle timer
        self._reset_idle_timer()

        # Check threshold
        if self._conversation_count >= self._warmup_threshold:
            logger.info(
                "L1 trigger: conversation_count=%d >= threshold=%d",
                self._conversation_count,
                self._warmup_threshold,
            )
            # Schedule async extraction (non-blocking)
            asyncio.ensure_future(self._trigger_l1())

    async def _trigger_l1(self) -> None:
        """Run L1 extraction + dedup + write. Non-blocking, fault-tolerant."""
        usage_log.set_source("memory")  # standalone task: no reset needed
        if self._l1_running:
            logger.debug("L1 already running, skipping this trigger")
            return

        if not self._buffer:
            return

        self._l1_running = True
        buffer_snapshot = list(self._buffer)

        try:
            # 1. Extract
            result = await extract_l1(
                self._api_client,
                buffer_snapshot,
                previous_scene_name=self._previous_scene_name,
                model=self._model,
            )

            if not result or not result.memories:
                logger.info("L1 extraction: no memories extracted from %d messages", len(buffer_snapshot))
                if result:
                    self._previous_scene_name = result.last_scene_name
                self._buffer.clear()
                self._advance_warmup()
                return

            # 2. Assign record_ids for dedup tracking
            new_memories: list[dict[str, Any]] = []
            for mem in result.memories:
                rid = f"new_{uuid.uuid4().hex[:8]}"
                new_memories.append(
                    {
                        "record_id": rid,
                        "content": mem.content,
                        "type": mem.type,
                        "priority": mem.priority,
                        "scene_name": mem.scene_name,
                        "source_message_ids": mem.source_message_ids,
                        "metadata": mem.metadata,
                        "summary": "",
                        "keywords": "",
                        "source_session_id": self._session_id,
                    }
                )

            # 3. Dedup
            decisions = await dedup_l1(
                self._api_client,
                self._wingman_db,
                new_memories,
                model=self._model,
            )

            # 4. Apply decisions to DB
            written = self._wingman_db.apply_dedup_decisions(decisions, new_memories)
            logger.info(
                "L1 pipeline complete: %d memories extracted, %d written to DB",
                len(new_memories),
                written,
            )

            # 4b. Count new memories toward the L3 (P4) threshold
            if written:
                self._wingman_db.increment_memories_since_persona(written)

            # 5. Update state
            self._previous_scene_name = result.last_scene_name
            self._buffer.clear()
            self._advance_warmup()

            # 6. Schedule L2 aggregation (debounced, non-blocking)
            self._schedule_l2()

        except Exception as e:
            logger.warning("L1 pipeline failed (non-fatal): %s", e)
            # Don't clear buffer on failure - will retry on next trigger
        finally:
            self._l1_running = False

    def _advance_warmup(self) -> None:
        """Double the warmup threshold until it reaches steady state."""
        if self._enable_warmup and self._warmup_threshold < self._every_n:
            self._warmup_threshold = min(self._warmup_threshold * 2, self._every_n)
            logger.debug("Warmup threshold advanced to %d", self._warmup_threshold)

    # ── L2 scene aggregation scheduling ───────────────────────────────

    def _schedule_l2(self) -> None:
        """Schedule L2 aggregation after a short debounce (if enabled).

        Debounced so back-to-back L1 runs don't each trigger an L2 call.
        Runs at most one pending L2; overlapping runs are skipped.
        """
        if self._scene_store is None:
            return
        if self._l2_running:
            logger.debug("L2 already running, skipping schedule")
            return
        if self._l2_timer is not None:
            logger.debug("L2 already scheduled, refreshing debounce")
            self._l2_timer.cancel()

        loop = asyncio.get_event_loop()
        self._l2_timer = loop.call_later(
            self._l2_debounce,
            lambda: asyncio.ensure_future(self._trigger_l2()),
        )
        logger.debug("L2 scheduled in %ds", self._l2_debounce)

    def _cancel_l2(self) -> None:
        if self._l2_timer is not None:
            self._l2_timer.cancel()
            self._l2_timer = None

    async def _trigger_l2(self) -> None:
        """Run L2 scene aggregation + periodic cold archive. Non-blocking."""
        usage_log.set_source("memory")  # standalone task: no reset needed
        self._l2_timer = None
        if self._scene_store is None or self._l2_running:
            return
        self._l2_running = True
        try:
            await extract_l2(
                self._api_client,
                self._wingman_db,
                self._scene_store,
                model=self._model,
            )
            self._check_cold_archive()
            # L2 成功后检查 L3 persona 触发条件（debounced）
            self._schedule_l3()
        except Exception as e:
            logger.warning("L2 pipeline failed (non-fatal): %s", e)
        finally:
            self._l2_running = False

    # ── L3 persona generation scheduling ─────────────────────────────

    def _schedule_l3(self) -> None:
        """Schedule L3 persona generation after L2 (short debounce).

        The trigger check itself is cheap; the LLM call is what we debounce
        so back-to-back L1/L2 runs don't stack persona generations.
        """
        if self._persona_path is None or self._scene_store is None:
            return
        if self._l3_running:
            logger.debug("L3 already running, skipping schedule")
            return
        if self._l3_timer is not None:
            logger.debug("L3 already scheduled, refreshing debounce")
            self._l3_timer.cancel()

        loop = asyncio.get_event_loop()
        self._l3_timer = loop.call_later(
            self._l3_debounce,
            lambda: asyncio.ensure_future(self._trigger_l3()),
        )
        logger.debug("L3 persona check scheduled in %ds", self._l3_debounce)

    def _cancel_l3(self) -> None:
        if self._l3_timer is not None:
            self._l3_timer.cancel()
            self._l3_timer = None

    async def _trigger_l3(self) -> None:
        """Check persona trigger conditions and run L3 generation if fired."""
        usage_log.set_source("memory")  # standalone task: no reset needed
        self._l3_timer = None
        if self._persona_path is None or self._scene_store is None:
            return
        if self._l3_running:
            logger.debug("L3 already running, skipping this trigger")
            return

        should_run, reason = check_persona_trigger(
            self._wingman_db,
            self._scene_store,
            trigger_every_n=self._persona_trigger_every_n,
        )
        if not should_run:
            logger.debug("L3 persona check: no trigger (%s)", reason or "no data")
            return

        self._l3_running = True
        try:
            result = await generate_l3(
                self._api_client,
                self._wingman_db,
                self._scene_store,
                self._persona_path,
                model=self._model,
                max_chars=self._persona_max_chars,
                priority_floor=self._persona_priority_floor,
                trigger_info=reason,
            )
            if result.generated:
                logger.info(
                    "L3 persona generated: mode=%s version=%d (%s)",
                    result.mode, result.version, reason,
                )
            elif result.errors:
                logger.warning("L3 persona generation failed: %s", result.errors)
            else:
                logger.info("L3 persona skipped: %s", result.reason)
        except Exception as e:
            logger.warning("L3 persona generation failed (non-fatal): %s", e)
        finally:
            self._l3_running = False

    def _check_cold_archive(self) -> None:
        """Archive books untouched for cold_days, at most once per interval."""
        if self._scene_store is None:
            return
        now = time.monotonic()
        if now - self._last_cold_check < DEFAULT_COLD_CHECK_INTERVAL:
            return
        self._last_cold_check = now
        self._scene_store.archive_cold()

    def _reset_idle_timer(self) -> None:
        """Reset the idle timeout timer."""
        if self._idle_timer:
            self._idle_timer.cancel()

        loop = asyncio.get_event_loop()
        self._idle_timer = loop.call_later(
            self._idle_timeout,
            lambda: asyncio.ensure_future(self._on_idle_timeout()),
        )

    async def _on_idle_timeout(self) -> None:
        """Called when the session goes idle for idle_timeout seconds."""
        if self._buffer:
            logger.info("L1 trigger: idle timeout (%ds) with %d buffered messages", self._idle_timeout, len(self._buffer))
            await self._trigger_l1()

    def flush(self) -> None:
        """Force-process remaining buffer + pending L2. Called on shutdown/session_end."""
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

        if self._buffer:
            logger.info("L1 trigger: flush with %d buffered messages", len(self._buffer))
            asyncio.ensure_future(self._trigger_l1())

        # Run L2 immediately if scheduled (don't wait for debounce)
        if self._l2_timer is not None:
            self._l2_timer.cancel()
            self._l2_timer = None
            if not self._l2_running and self._scene_store is not None:
                logger.info("L2 trigger: flush (scheduled pending)")
                asyncio.ensure_future(self._trigger_l2())

        # Run L3 persona check immediately if scheduled
        if self._l3_timer is not None:
            self._l3_timer.cancel()
            self._l3_timer = None
            if not self._l3_running:
                logger.info("L3 trigger: flush (scheduled pending)")
                asyncio.ensure_future(self._trigger_l3())

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Trigger L1 before context compression discards old messages.

        Returns empty string (no text to inject into compression prompt).
        The value is in the side-effect of extracting memories before they're lost.
        """
        if self._buffer:
            logger.info("L1 trigger: pre-compress with %d buffered messages", len(self._buffer))
            asyncio.ensure_future(self._trigger_l1())
        return ""


def _now_iso() -> str:
    """Current time in ISO 8601 format."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
