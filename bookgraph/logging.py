"""LLM call logging for BookGraph — DB (graph_llm_logs) + JSONL audit trail.

Each LLM call during extraction (Scout / ContextSetter / Explorer) is
persisted in two places:

1. **graph.db → graph_llm_logs** — full prompt + response, queryable by the
   flight recorder's 图谱 tab for monitoring / debugging.
2. **data/logs/bookgraph.jsonl** — a lightweight append-only audit projection:
   sha256 hashes + truncated previews (not the full text) so the file stays
   small while remaining replayable/referenceable.

Logging is best-effort: any failure is swallowed so it can never break the
extraction pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import usage_log

log = logging.getLogger("bookgraph")

# JSONL rotation threshold — rename to bookgraph-{ts}.jsonl when exceeded
_JSONL_ROTATE_BYTES = 50 * 1024 * 1024

_PROMPT_PREVIEW_CHARS = 2000
_RESPONSE_PREVIEW_CHARS = 1000


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _preview(text: str, n: int) -> str:
    if not text:
        return ""
    return text[:n]


def _rotate_if_needed(path: Path) -> None:
    """Rename an oversized JSONL to bookgraph-{ts}.jsonl before appending."""
    try:
        if path.exists() and path.stat().st_size > _JSONL_ROTATE_BYTES:
            ts = time.strftime("%Y%m%d-%H%M%S")
            path.rename(path.with_name(f"bookgraph-{ts}.jsonl"))
    except OSError:
        pass  # rotation is best-effort


def _append_jsonl(path: Path, obj: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("bookgraph JSONL append failed", exc_info=True)


def log_llm_call(
    graph_db,
    jsonl_path: Path,
    *,
    book_id: str,
    book_title: str,
    run_id: str,
    phase: str,
    batch_id: str = "",
    batch_index: int = 0,
    chunk_ids: list[str] | None = None,
    total_chunks: int = 0,
    model: str = "",
    attempt: int = 1,
    max_attempts: int = 1,
    latency_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    tokens_cached: int | None = None,
    reasoning_tokens: int | None = None,
    status: str = "ok",
    error_detail: str | None = None,
    prompt: str = "",
    response: str = "",
    reasoning_content: str = "",
    parsed: dict | None = None,
    node_index: dict | None = None,
    scout_focus: str = "",
) -> None:
    """Record one LLM call: full text to graph_llm_logs, hash+preview to JSONL.

    ``parsed`` is a flat summary dict of extraction counts, e.g.
    {"new_nodes": 3, "new_edges": 2, "new_aliases": 1, "new_quotes": 0,
     "merge_candidates": 1} — computed by the caller from the LLM result.
    ``node_index`` is {"compact": int, "detailed": int} sizes fed to Explorer.
    """
    # ── 1. DB (full text, for the flight recorder 图谱 tab) ──────────────
    try:
        graph_db.record_llm_call(
            book_id=book_id, run_id=run_id, phase=phase,
            batch_id=batch_id, batch_index=batch_index,
            chunk_ids=chunk_ids, total_chunks=total_chunks,
            model=model, attempt=attempt, max_attempts=max_attempts,
            latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out,
            tokens_cached=tokens_cached, reasoning_tokens=reasoning_tokens,
            status=status, error_detail=error_detail,
            prompt=prompt, response=response,
            reasoning_content=reasoning_content,
        )
    except Exception:
        log.warning("graph_llm_logs insert failed", exc_info=True)

    # One second-resolution timestamp shared by the JSONL row and the
    # usage-log dedup key, so backfill from JSONL can never double-count
    # a call that was also recorded live.
    ts_str = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    # ── 2. JSONL audit projection (hash + preview) ────────────────────────
    if jsonl_path is not None:
        record: dict[str, Any] = {
            "ts": ts_str,
            "run_id": run_id,
            "book_id": book_id,
            "book_title": book_title,
            "phase": phase,
            "batch_id": batch_id,
            "batch_index": batch_index,
            "chunk_ids": chunk_ids or [],
            "total_chunks": total_chunks,
            "model": model,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "latency_ms": latency_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_cached": tokens_cached,
            "reasoning_tokens": reasoning_tokens,
            "prompt_hash": _sha256(prompt),
            "prompt_chars": len(prompt),
            "prompt_preview": _preview(prompt, _PROMPT_PREVIEW_CHARS),
            "status": status,
            "error_detail": error_detail,
            "response_hash": _sha256(response) if response else "",
            "response_chars": len(response),
            "response_preview": _preview(response, _RESPONSE_PREVIEW_CHARS),
        }
        if parsed:
            record["parsed"] = parsed
        if node_index:
            record["node_index"] = node_index
        if scout_focus:
            record["scout_focus"] = scout_focus
        _append_jsonl(jsonl_path, record)

    # ── 3. Console usage log (drives the 用量分析 heatmap) ────────────────
    # dedup_key matches scripts/backfill_usage.py exactly → idempotent.
    usage_log.record(
        source="bookgraph",
        model=model or "",
        tokens_in=tokens_in or 0,
        tokens_out=tokens_out or 0,
        status=status,
        ref_id=book_id,
        dedup_key=f"bg:{ts_str}:{run_id}:{batch_id}:{batch_index}:{attempt}:{phase}",
    )
