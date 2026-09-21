"""Orchestrator — loop_extract: drives Scout + Explorer + Recorder + progress events.

This is the main entry point for book graph extraction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import aiohttp

from bookgraph.context_setter import run_context_setter
from bookgraph.explorer import build_explorer_prompt, run_explorer
from bookgraph.logging import log_llm_call
from bookgraph.recorder import record_result, RecordStats
from bookgraph.head_scan import run_head_scan
from bookgraph.scout import run_scout
from config import Config
from storage.db import BookDB
from storage.graph_db import GraphDB

log = logging.getLogger("bookgraph")


@dataclass
class ExtractionResult:
    """Final result of a full extraction run."""
    book_id: str
    book_title: str
    total_chunks: int
    chunks_done: int
    chunks_failed: int
    final_stats: dict
    book_context: dict | None = None
    batch_stats: list[dict] = field(default_factory=list)
    scout_plan: dict | None = None


# ── Progress callback ─────────────────────────────────────────────────────

ProgressCallback = Callable[[dict], None]
"""Called with a progress event dict on each batch step.

Event shape:
    {
        "type": "batch_start" | "batch_done" | "batch_failed" | "context_set"
              | "scout_done" | "complete",
        "book_id": str,
        "batch_id": str,
        "batch_index": int,
        "total_batches": int,
        "done_chunks": int,
        "total_chunks": int,
        "step_label": str | None,
        "detail": dict | None,
    }
"""

# Scout auto-activation threshold: skip scout for books with fewer chunks
SCOUT_MIN_CHUNKS = 50

# How many Explorer LLM calls may run concurrently. Only the LLM calls are
# concurrent — all graph_db writes happen serially in the reaping loop below,
# which keeps the single SQLite connection free of interleaving.
BATCH_CONCURRENCY = 3


async def _run_batch_explorer(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    cfg: Config,
    graph_db: GraphDB,
    book_id: str,
    book_title: str,
    book_author: str,
    run_id: str,
    batch_id: str,
    batch_index: int,
    chunk_dicts: list[dict],
    total: int,
    system_prompt: str,
    jsonl_path: Path,
    extraction_focus: str = "",
) -> tuple[int, dict | None, str | None]:
    """Run one Explorer batch inside the concurrency semaphore.

    Returns ``(batch_index, result, error_type)``. The semaphore is held
    while building the node index AND making the LLM call, so later batches
    still see nodes committed by earlier ones (approximate sliding window —
    a batch only starts once an earlier one has handed its slot back).

    Every LLM call is logged via ``log_llm_call`` (graph_llm_logs + JSONL).
    """
    async with sem:
        done_count = graph_db.done_count(book_id)

        def _log(fields: dict) -> None:
            log_llm_call(
                graph_db, jsonl_path,
                book_id=book_id, book_title=book_title, run_id=run_id,
                phase="explorer", batch_id=batch_id, batch_index=batch_index,
                chunk_ids=[d["chunk_id"] for d in chunk_dicts],
                total_chunks=total, model=cfg.model,
                scout_focus=extraction_focus,
                **fields,
            )

        result, error_type = await run_explorer(
            session, cfg, graph_db, book_id, book_title, book_author,
            chunk_dicts, total, done_count, system_prompt,
            log_cb=_log,
        )
        return batch_index, result, error_type


# ── Main orchestrator ─────────────────────────────────────────────────────

class GraphOrchestrator:
    """Drives the Scout + Explorer + Recorder loop for one book.

    Usage:
        orch = GraphOrchestrator(config, books_db, graph_db)
        result = await orch.run(book_id, batch_size=5, progress_cb=callback)
    """

    def __init__(
        self,
        config: Config,
        books_db: BookDB,
        graph_db: GraphDB,
        graph_db_path=None,
    ):
        self.config = config
        self.books_db = books_db
        self.graph_db = graph_db
        self.graph_db_path = graph_db_path
        self._jsonl_path = Path(config.project_dir) / "data" / "logs" / "bookgraph.jsonl"

    async def run(
        self,
        book_id: str,
        batch_size: int = 5,
        progress_cb: ProgressCallback | None = None,
        use_scout: bool = True,
    ) -> ExtractionResult:
        """Run full extraction loop for a book.

        Args:
            book_id: Book identifier.
            batch_size: Default batch size for mechanical fallback (ignored
                        when scout produces a batch plan).
            progress_cb: Optional callback for SSE progress events.
            use_scout: Whether to run the scout phase (Phase 0) before
                       extraction.  Auto-disabled for books with < 50 chunks.

        Resumable: skips chunks already marked done.
        """
        # ── 1. Load book metadata + chunks ────────────────────────────────
        book = self.books_db.get_book(book_id)
        if book is None:
            raise ValueError(f"Book not found: {book_id}")

        all_chunks = self.books_db.get_chunks(book_id)
        if not all_chunks:
            raise ValueError(f"Book has no chunks: {book_id}")

        # Load chapter titles
        chapters = self.books_db.get_chapters(book_id)
        chapter_titles: dict[int, str] = {
            ch.chapter_index: (ch.title or f"第{ch.chapter_index + 1}章")
            for ch in chapters
        }

        total = len(all_chunks)

        # ── 1b. Run identity ─────────────────────────────────────────────
        # A run_id scopes every progress row + LLM call log for this run.
        # The SSE endpoint uses the run_start row as its replay anchor, so
        # this must be the FIRST progress row written each run.
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.graph_db.record_event(book_id, "run_start",
            step_label=f"提取开始 · {run_id}",
            detail=json.dumps({"run_id": run_id, "total_chunks": total},
                              ensure_ascii=False),
            run_id=run_id)
        self._emit(progress_cb, {
            "type": "run_start",
            "book_id": book_id,
            "run_id": run_id,
            "total_chunks": total,
        })

        # ── 2. Register chunks in graph_db (idempotent) ───────────────────
        for ch in all_chunks:
            content_hash = hashlib.sha256(
                (ch.content or "").encode("utf-8")
            ).hexdigest()[:16]
            self.graph_db.register_chunk(
                ch.id, book_id, ch.chapter_index, ch.chunk_index, content_hash
            )

        # ── 3. Init chapter nodes ─────────────────────────────────────────
        chapter_list = [
            (idx, chapter_titles.get(idx, f"第{idx + 1}章"))
            for idx in sorted(chapter_titles.keys())
        ]
        chapter_node_ids = self.graph_db.init_chapter_nodes(book_id, chapter_list)

        already_done = self.graph_db.done_count(book_id)
        if already_done > 0:
            log.info("Resume: %d/%d chunks already done", already_done, total)
            self._emit(progress_cb, {
                "type": "resume",
                "book_id": book_id,
                "run_id": run_id,
                "done_chunks": already_done,
                "total_chunks": total,
            })
            self.graph_db.record_event(book_id, "resume",
                step_label=f"续接: {already_done}/{total} chunks 已完成",
                detail=json.dumps({"done_chunks": already_done, "total_chunks": total},
                                  ensure_ascii=False),
                run_id=run_id)

        # ── 4. ContextSetter (cached) ─────────────────────────────────────
        book_context = None
        cached_ctx = self.graph_db.get_meta("book_context")
        if cached_ctx:
            try:
                book_context = json.loads(cached_ctx)
                log.info("ContextSetter: cache hit")
            except json.JSONDecodeError:
                cached_ctx = None

        if not cached_ctx:
            async with aiohttp.ClientSession() as session:
                def _log_ctx(fields: dict) -> None:
                    log_llm_call(
                        self.graph_db, self._jsonl_path,
                        book_id=book_id, book_title=book.title, run_id=run_id,
                        phase="context", total_chunks=total,
                        model=self.config.model,
                        **fields,
                    )
                book_context = await run_context_setter(
                    session, self.config.api_key, self.config.api_base,
                    self.config.model, book.title, book.author or "",
                    log_cb=_log_ctx,
                )
            if book_context is not None:
                self.graph_db.set_meta(
                    "book_context", json.dumps(book_context, ensure_ascii=False)
                )

        if book_context:
            self._emit(progress_cb, {
                "type": "context_set",
                "book_id": book_id,
                "run_id": run_id,
                "genre": book_context.get("genre"),
                "suggested_kinds": book_context.get("suggested_kinds", []),
            })
            self.graph_db.record_event(book_id, "context_set",
                step_label=f"世界观边界 · {book_context.get('genre', '未知')}",
                detail=json.dumps({
                    "genre": book_context.get("genre"),
                    "suggested_kinds": book_context.get("suggested_kinds", []),
                }, ensure_ascii=False),
                run_id=run_id)

        # ── 4b. Head scan: 书头版权/序边界（v0, region 落 graph meta）──────
        # 定稿:mapping 文档 §五-b。滑动窗口 + 连续两块 preface 终止;产物
        # 目前落 meta + 事件日志,消费(Scout/explorer 输入裁剪)留后续。
        head_scan = None
        cached_head = self.graph_db.get_meta("head_scan")
        if cached_head:
            try:
                head_scan = json.loads(cached_head)
                log.info("HeadScan: cache hit (verdict=%s, scanned=%d)",
                         head_scan.get("verdict"), head_scan.get("scanned", 0))
            except json.JSONDecodeError:
                cached_head = None

        if not head_scan:
            head_chunks = sorted(
                all_chunks, key=lambda c: (c.chapter_index, c.chunk_index)
            )
            async with aiohttp.ClientSession() as session:
                def _log_head(fields: dict) -> None:
                    log_llm_call(
                        self.graph_db, self._jsonl_path,
                        book_id=book_id, book_title=book.title, run_id=run_id,
                        phase="head_scan",
                        chunk_ids=[c.id for c in head_chunks
                                   if c.chapter_index <= 3],
                        total_chunks=len(head_chunks),
                        model=self.config.model,
                        **fields,
                    )
                head_scan = await run_head_scan(
                    session, self.config,
                    book.title, book.author or "",
                    head_chunks, chapter_titles,
                    log_cb=_log_head,
                )
            if head_scan and head_scan.get("verdict") != "fail":
                self.graph_db.set_meta(
                    "head_scan", json.dumps(head_scan, ensure_ascii=False)
                )

        if head_scan:
            hs = head_scan
            self._emit(progress_cb, {
                "type": "head_scan",
                "book_id": book_id,
                "run_id": run_id,
                "verdict": hs.get("verdict"),
                "scanned": hs.get("scanned", 0),
                "calls": hs.get("calls", 0),
                "preface_start": hs.get("preface_start"),
                "identity": len(hs.get("identity") or []),
                "promo": len(hs.get("promo_chunks") or []),
            })
            self.graph_db.record_event(book_id, "head_scan_done",
                step_label=f"头部扫描 · {hs.get('verdict')} · "
                           f"{hs.get('scanned', 0)} 块",
                detail=json.dumps({
                    "verdict": hs.get("verdict"),
                    "preface_start": hs.get("preface_start"),
                    "reason": hs.get("reason", ""),
                }, ensure_ascii=False),
                run_id=run_id)

        explorer_prompt = build_explorer_prompt(book_context)

        # ── 5. Phase 0: Scout (optional, cached) ──────────────────────────
        scout_plan: dict | None = None
        excluded_ids: set[str] = set()
        # Build a lookup early — needed by content filtering below
        chunk_by_id: dict[str, Any] = {ch.id: ch for ch in all_chunks}
        should_scout = use_scout and total >= SCOUT_MIN_CHUNKS

        if should_scout:
            # Check cache
            cached_scout = self.graph_db.get_meta("scout_plan")
            if cached_scout:
                try:
                    scout_plan = json.loads(cached_scout)
                    log.info("Scout: cache hit (%d batches)",
                             len(scout_plan.get("batch_plan", [])))
                except json.JSONDecodeError:
                    cached_scout = None

            if not cached_scout:
                chunk_dicts = [{
                    "chunk_id": ch.id,
                    "chapter_index": ch.chapter_index,
                    "chapter_title": chapter_titles.get(
                        ch.chapter_index, f"第{ch.chapter_index + 1}章"
                    ),
                    "chunk_index": ch.chunk_index,
                    "content": ch.content or "",
                } for ch in all_chunks]

                async with aiohttp.ClientSession() as session:
                    def _log_scout(fields: dict) -> None:
                        log_llm_call(
                            self.graph_db, self._jsonl_path,
                            book_id=book_id, book_title=book.title, run_id=run_id,
                            phase="scout",
                            chunk_ids=[d["chunk_id"] for d in chunk_dicts],
                            total_chunks=total,
                            model=self.config.model,
                            **fields,
                        )
                    scout_plan = await run_scout(
                        session, self.config,
                        book.title, book.author or "",
                        chunk_dicts, total,
                        log_cb=_log_scout,
                    )

                if scout_plan and scout_plan.get("batch_plan"):
                    self.graph_db.set_meta(
                        "scout_plan", json.dumps(scout_plan, ensure_ascii=False)
                    )
                    log.info("Scout: plan cached (%d batches)",
                             len(scout_plan["batch_plan"]))

            if scout_plan and scout_plan.get("batch_plan"):
                # ── Content filtering: mark excluded chunks ────────────────
                excluded_ids = set(scout_plan.get("excluded_chunk_ids") or [])
                content_warnings = scout_plan.get("content_warnings") or []
                if excluded_ids:
                    for cid in excluded_ids:
                        ch = chunk_by_id.get(cid)
                        if ch:
                            # Find the reason from content_warnings
                            reason = ""
                            for cw in content_warnings:
                                if cid in (cw.get("chunk_ids") or []):
                                    reason = cw.get("reason", "")
                                    break
                            self.graph_db.mark_chunk_excluded(cid, reason)
                    excluded_count = len(excluded_ids)
                    log.info("Scout: excluded %d chunks (%d content warnings) — "
                             "%s", excluded_count, len(content_warnings),
                             "foreign lang" if any("外语" in cw.get("reason", "") or
                                 "语" in cw.get("reason", "")
                                 for cw in content_warnings) else "non-narrative")
                    self._emit(progress_cb, {
                        "type": "content_filtered",
                        "book_id": book_id,
                        "run_id": run_id,
                        "excluded_count": excluded_count,
                        "total_chunks": total,
                        "content_warnings": [
                            {"chunk_count": len(cw.get("chunk_ids") or []),
                             "reason": cw.get("reason", ""),
                             "suggestion": cw.get("suggestion", "skip")}
                            for cw in content_warnings
                        ],
                    })
                    self.graph_db.record_event(book_id, "content_filtered",
                        step_label=f"过滤 {excluded_count} chunks",
                        detail=json.dumps({
                            "excluded_count": excluded_count,
                            "total_chunks": total,
                            "content_warnings": [
                                {"chunk_count": len(cw.get("chunk_ids") or []),
                                 "reason": cw.get("reason", ""),
                                 "suggestion": cw.get("suggestion", "skip")}
                                for cw in content_warnings
                            ],
                        }, ensure_ascii=False),
                        run_id=run_id)

                self._emit(progress_cb, {
                    "type": "scout_done",
                    "book_id": book_id,
                    "run_id": run_id,
                    "total_chunks": total,
                    "scout_batches": len(scout_plan["batch_plan"]),
                    "narrative_units": len(scout_plan.get("narrative_units", [])),
                    "excluded_chunks": len(excluded_ids) if excluded_ids else 0,
                })
                self.graph_db.record_event(book_id, "scout_done",
                    step_label=f"Scout: {len(scout_plan['batch_plan'])} 批次"
                              f" · {len(scout_plan.get('narrative_units', []))} 叙事单元"
                              + (f" · 排除 {len(excluded_ids)}" if excluded_ids else ""),
                    detail=json.dumps({
                        "total_chunks": total,
                        "scout_batches": len(scout_plan["batch_plan"]),
                        "narrative_units": len(scout_plan.get("narrative_units", [])),
                        "excluded_chunks": len(excluded_ids) if excluded_ids else 0,
                    }, ensure_ascii=False),
                    run_id=run_id)
            else:
                # Scout produced nothing useful — fall back to mechanical
                log.info("Scout: no usable plan, falling back to mechanical batching")
                scout_plan = None

        # ── 6. Build batch list ───────────────────────────────────────────
        if scout_plan and scout_plan.get("batch_plan"):
            # Scout-driven batches
            batch_plan = scout_plan["batch_plan"]
            batch_list: list[list[Any]] = []
            seen_ids: set[str] = set()
            # Pre-seed seen_ids with excluded so they aren't treated as leftover
            seen_ids.update(excluded_ids)
            for bp in batch_plan:
                batch_chunks: list[Any] = []
                for cid in bp.get("chunk_ids", []):
                    if cid not in seen_ids and cid not in excluded_ids:
                        ch = chunk_by_id.get(cid)
                        if ch:
                            batch_chunks.append(ch)
                            seen_ids.add(cid)
                if batch_chunks:
                    batch_list.append(batch_chunks)
            # Append any leftover chunks not in scout plan (but not excluded)
            leftover = [ch for ch in all_chunks
                        if ch.id not in seen_ids and ch.id not in excluded_ids]
            if leftover:
                log.info("Scout missed %d chunks — appending as final batch", len(leftover))
                batch_list.append(leftover)
            log.info("Scout plan: %d batches (vs %d mechanical)%s",
                     len(batch_list), (total + batch_size - 1) // batch_size,
                     f" — {len(excluded_ids)} excluded" if excluded_ids else "")
        else:
            # Mechanical fallback
            batch_list = [
                all_chunks[i:i + batch_size]
                for i in range(0, total, batch_size)
            ]

        total_batches = len(batch_list)

        # ── 7. Loop: batch → Explorer → Recorder ──────────────────────────
        # Preparation (serial) launches every Explorer call under a concurrency
        # semaphore; reaping (also serial on the event loop) performs all DB
        # writes so SQLite's single connection is never interleaved.
        batch_stats: list[dict] = []

        async with aiohttp.ClientSession() as session:
            sem = asyncio.Semaphore(BATCH_CONCURRENCY)
            pending: dict[int, asyncio.Task] = {}
            batch_by_index: dict[int, list[Any]] = {}
            batch_ids: dict[int, str] = {}
            batch_labels: dict[int, str] = {}

            for batch_index, batch_chunks_ in enumerate(batch_list, 1):
                batch_id = f"batch-{uuid.uuid4().hex[:8]}"

                # Build step label from scout plan or first chunk
                if scout_plan and scout_plan.get("batch_plan"):
                    bp = scout_plan["batch_plan"][batch_index - 1] if batch_index - 1 < len(scout_plan["batch_plan"]) else {}
                    step_label = bp.get("label") or (
                        f"第 {batch_chunks_[0].chapter_index + 1} 章「"
                        f"{chapter_titles.get(batch_chunks_[0].chapter_index, '')}」"
                    )
                else:
                    step_label = (
                        f"第 {batch_chunks_[0].chapter_index + 1} 章「"
                        f"{chapter_titles.get(batch_chunks_[0].chapter_index, '')}」"
                    )

                # Skip batch if all its chunks are already done
                if all(self.graph_db.is_chunk_done(ch.id) for ch in batch_chunks_):
                    log.info("Batch %d/%d: SKIP (all done)", batch_index, total_batches)
                    self._emit(progress_cb, {
                        "type": "batch_skipped",
                        "book_id": book_id,
                        "run_id": run_id,
                        "batch_id": batch_id,
                        "batch_index": batch_index,
                        "total_batches": total_batches,
                        "done_chunks": self.graph_db.done_count(book_id),
                        "total_chunks": total,
                    })
                    self.graph_db.record_progress(
                        book_id, batch_id, batch_index, step_label, "skipped",
                        detail=json.dumps({
                            "done_chunks": self.graph_db.done_count(book_id),
                            "total_chunks": total,
                            "total_batches": total_batches,
                        }),
                        run_id=run_id,
                    )
                    continue

                self._emit(progress_cb, {
                    "type": "batch_start",
                    "book_id": book_id,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "batch_index": batch_index,
                    "total_batches": total_batches,
                    "done_chunks": self.graph_db.done_count(book_id),
                    "total_chunks": total,
                    "step_label": step_label,
                })
                self.graph_db.record_progress(
                    book_id, batch_id, batch_index, step_label, "in_progress",
                    detail=json.dumps({"total_batches": total_batches}),
                    run_id=run_id,
                )

                # Build chunk dicts for Explorer
                chunk_dicts = [{
                    "chunk_id": ch.id,
                    "chapter_index": ch.chapter_index,
                    "chapter_title": chapter_titles.get(
                        ch.chapter_index, f"第{ch.chapter_index + 1}章"
                    ),
                    "chunk_index": ch.chunk_index,
                    "content": ch.content or "",
                } for ch in batch_chunks_]

                # Augment explorer prompt with scout extraction focus
                batch_explorer_prompt = explorer_prompt
                extraction_focus = ""
                if scout_plan and scout_plan.get("batch_plan"):
                    bp = scout_plan["batch_plan"][batch_index - 1] if batch_index - 1 < len(scout_plan["batch_plan"]) else {}
                    extraction_focus = bp.get("extraction_focus", "")
                    if extraction_focus:
                        batch_explorer_prompt = explorer_prompt + f"\n\n**本批提取重点**：{extraction_focus}"

                batch_ids[batch_index] = batch_id
                batch_labels[batch_index] = step_label
                batch_by_index[batch_index] = batch_chunks_
                pending[batch_index] = asyncio.create_task(
                    _run_batch_explorer(
                        sem, session, self.config, self.graph_db,
                        book_id, book.title, book.author or "",
                        run_id, batch_id, batch_index, chunk_dicts, total,
                        batch_explorer_prompt, self._jsonl_path, extraction_focus,
                    )
                )

            # ── Reap (serial DB writes — completions may be out of order) ──
            # asyncio.wait returns the ORIGINAL task objects (as_completed
            # wraps them, so we can't recover the batch_index from it).
            pending_by_task = {t: i for i, t in pending.items()}
            while pending:
                done, _ = await asyncio.wait(
                    pending.values(), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    batch_index = pending_by_task[task]
                    try:
                        _, result, error_type = task.result()
                    except Exception as exc:
                        # One unexpected per-batch failure shouldn't cancel the
                        # remaining pending batches — mark it failed, move on.
                        log.error("Batch %d/%d: UNEXPECTED ERROR: %s",
                                  batch_index, total_batches, exc)
                        result, error_type = None, "exhausted_retries"
                    batch_chunks_ = batch_by_index[batch_index]
                    batch_id = batch_ids[batch_index]
                    step_label = batch_labels[batch_index]

                    if result is None:
                        log.error("Batch %d/%d: FAILED (%s)", batch_index,
                                  total_batches, error_type)
                        for ch in batch_chunks_:
                            self.graph_db.mark_chunk_failed(ch.id, error_reason=error_type)
                        self.graph_db.record_progress(
                            book_id, batch_id, batch_index, step_label, "failed",
                            detail=json.dumps({"error_reason": error_type}),
                            run_id=run_id,
                        )
                        self._emit(progress_cb, {
                            "type": "batch_failed",
                            "book_id": book_id,
                            "run_id": run_id,
                            "batch_id": batch_id,
                            "batch_index": batch_index,
                            "total_batches": total_batches,
                            "done_chunks": self.graph_db.done_count(book_id),
                            "total_chunks": total,
                            "step_label": step_label,
                            "detail": {"error_reason": error_type},
                        })
                        batch_stats.append({
                            "batch_index": batch_index,
                            "status": "failed",
                            "error_reason": error_type,
                        })
                    else:
                        # Record result
                        chunk_ids = [ch.id for ch in batch_chunks_]
                        stats = record_result(
                            self.graph_db, book_id, chapter_node_ids, chunk_ids, result
                        )
                        self.graph_db.record_progress(
                            book_id, batch_id, batch_index, step_label, "done",
                            detail=json.dumps({
                                "new_nodes": stats.new_nodes,
                                "new_edges": stats.new_edges,
                                "new_aliases": stats.new_aliases,
                                "new_quotes": stats.new_quotes,
                                "merge_candidates": stats.merge_candidates,
                            }),
                            run_id=run_id,
                        )

                        done_now = self.graph_db.done_count(book_id)
                        log.info(
                            "Batch %d/%d: nodes=%d obs=%d edges=%d aliases=%d quotes=%d [%d/%d done]",
                            batch_index, total_batches,
                            stats.new_nodes, stats.new_observations,
                            stats.new_edges, stats.new_aliases,
                            stats.new_quotes, done_now, total,
                        )
                        self._emit(progress_cb, {
                            "type": "batch_done",
                            "book_id": book_id,
                            "run_id": run_id,
                            "batch_id": batch_id,
                            "batch_index": batch_index,
                            "total_batches": total_batches,
                            "done_chunks": done_now,
                            "total_chunks": total,
                            "step_label": step_label,
                            "detail": {
                                "new_nodes": stats.new_nodes,
                                "new_observations": stats.new_observations,
                                "new_edges": stats.new_edges,
                                "new_aliases": stats.new_aliases,
                                "new_quotes": stats.new_quotes,
                                "merge_candidates": stats.merge_candidates,
                            },
                        })
                        batch_stats.append({
                            "batch_index": batch_index,
                            "status": "done",
                            "nodes": stats.new_nodes,
                            "edges": stats.new_edges,
                            "aliases": stats.new_aliases,
                            "quotes": stats.new_quotes,
                            "merges": stats.merge_candidates,
                        })

                    del pending_by_task[task]
                    del pending[batch_index]

        # ── 8. Auto-merge high-confidence entity candidates ─────────────────
        try:
            merged = self.graph_db.auto_merge(book_id, min_confidence=0.9)
            if merged:
                log.info("[GraphOrchestrator] Auto-merged %d entity pairs for %s", merged, book_id)
        except Exception as exc:
            log.warning("[GraphOrchestrator] Auto-merge failed (non-fatal): %s", exc)

        # ── 9. Final stats ────────────────────────────────────────────────
        final_stats = self.graph_db.get_stats(book_id)
        self._emit(progress_cb, {
            "type": "complete",
            "book_id": book_id,
            "run_id": run_id,
            "done_chunks": final_stats["chunks_done"],
            "total_chunks": total,
            "final_stats": final_stats,
        })
        self.graph_db.record_event(book_id, "complete",
            step_label="提取完成",
            detail=json.dumps({
                "run_id": run_id,
                "done_chunks": final_stats["chunks_done"],
                "total_chunks": total,
                "final_stats": final_stats,
            }, ensure_ascii=False),
            run_id=run_id)

        return ExtractionResult(
            book_id=book_id,
            book_title=book.title,
            total_chunks=total,
            chunks_done=final_stats["chunks_done"],
            chunks_failed=final_stats["chunks_failed"],
            final_stats=final_stats,
            book_context=book_context,
            batch_stats=batch_stats,
            scout_plan=scout_plan,
        )

    def _emit(self, cb: ProgressCallback | None, event: dict):
        """Safely call progress callback."""
        if cb:
            try:
                cb(event)
            except Exception as exc:
                log.warning("Progress callback error: %s", exc)
