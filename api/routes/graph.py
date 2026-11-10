"""BookGraph + AuthorGraph HTTP endpoints (split out of api_server.py)."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web



# ── BookGraph endpoints ───────────────────────────────────────────────────


def _resolve_graph_path(config, db, book_id: str) -> Path | None:
    """Resolve the graph DB path for a book via stable_book_id.

    Returns None if the book has no import_log (not yet imported with stable identity).
    """
    log = db.get_import_log_by_book(book_id)
    if not log or not log.stable_book_id:
        return None
    return config.graph_db_path(log.stable_book_id)




# Graph evolution: track in-progress extractions to prevent concurrent SQLite writes
_graph_extracting: set[str] = set()


async def _bg_graph_extract(stable_bid, dialogue, api_client, config, db, book_id):
    """Background graph extraction from dialogue (non-blocking).

    Fires after the SSE stream completes so the user sees no latency.
    Skips if a previous extraction for the same book is still running.
    """
    import logging  # 独立导入：后台任务作用域无模块级 logging 名
    if stable_bid in _graph_extracting:
        return
    _graph_extracting.add(stable_bid)
    try:
        from bookgraph.incremental import extract_from_dialogue
        from storage.graph_db import GraphDB
        graph_path = _resolve_graph_path(config, db, book_id) if db and config else None
        if not graph_path or not graph_path.exists():
            return
        gdb = GraphDB(graph_path)
        try:
            count = await extract_from_dialogue(stable_bid, gdb, api_client, dialogue)
            if count:
                logging.getLogger("api").info(
                    "Graph evolution: %d observations from chat (book=%s)", count, stable_bid)
        finally:
            gdb.close()
    except Exception:
        logging.getLogger("api").debug("Background graph extraction failed", exc_info=True)
    finally:
        _graph_extracting.discard(stable_bid)


_enrich_in_progress: set[str] = set()


async def _enrich_book_metadata_async(api_client, db, book_id):
    """Background one-shot LLM metadata cleanup (non-blocking).

    Fires after a new book import so the user sees no latency.  Skips if
    the same book is already being enriched.  Any failure degrades to a
    debug log — the import flow and HTTP response are never disturbed.
    """
    import logging  # 独立导入：后台任务作用域无模块级 logging 名
    if not api_client or book_id in _enrich_in_progress:
        return
    _enrich_in_progress.add(book_id)
    try:
        from services.metadata_enrich import enrich_book_metadata
        ok = await enrich_book_metadata(api_client, db, book_id)
        if ok:
            logging.getLogger("api").info(
                "Metadata LLM enrichment done: %s", book_id)
    except Exception:
        logging.getLogger("api").debug("Metadata enrichment failed", exc_info=True)
    finally:
        _enrich_in_progress.discard(book_id)


def _get_graph_db(request: web.Request, book_id: str):
    """Open (or create) the GraphDB for this book_id. Caller closes it.

    Resolves stable_book_id via import_log so the graph DB survives
    book deletion and re-import (same persistence model as annotations).
    Falls back to book_id naming only while the book has no import_log;
    legacy files that carry real data are migrated to stable naming.
    """
    from storage.graph_db import GraphDB, resolve_graph_db_path
    config = request.app.get("config")
    db = request.app.get("db")
    if config and db:
        graph_path = resolve_graph_db_path(config, db, book_id)
    elif config:
        graph_path = config.graph_db_path(book_id)
    else:
        from runtime_paths import data_path
        graph_path = data_path("graphs", f"graph_{book_id}.db")
    return GraphDB(graph_path)


def _start_graph_extraction(app: web.Application, db, config,
                            book_id: str, batch_size: int) -> bool:
    """Create the SSE queue + background extraction task for a book.

    Returns False when extraction is already running for this book (409).
    Shared by ``handle_graph_init`` and ``handle_graph_retry_failed``.
    """
    # Check if extraction already running (store asyncio.Task so we can
    # verify liveness — a boolean flag can get stuck if finally doesn't run)
    running_key = f"graph_running_{book_id}"
    existing_task = app.get(running_key)
    if existing_task is not None and not existing_task.done():
        return False

    # Create a queue so the SSE endpoint can consume real-time events
    # without polling the DB.  Keep the DB writes for crash recovery.
    queue: asyncio.Queue = asyncio.Queue()
    queue_key = f"graph_queue_{book_id}"
    app[queue_key] = queue

    async def _run_extraction():
        """Background task — runs the full extraction loop."""
        import logging
        log = logging.getLogger("bookgraph")
        try:
            from storage.graph_db import GraphDB, resolve_graph_db_path
            from bookgraph import GraphOrchestrator

            graph_path = resolve_graph_db_path(config, db, book_id)
            graph_db = GraphDB(graph_path)
            orch = GraphOrchestrator(config, db, graph_db, graph_path)

            def _push_event(event: dict):
                """Push orchestrator progress event into the SSE queue."""
                try:
                    queue.put_nowait(event)
                except Exception:
                    pass

            await orch.run(book_id, batch_size=batch_size,
                           progress_cb=_push_event)
            graph_db.close()
            log.info("Graph extraction complete for %s", book_id)
        except Exception as exc:
            log = logging.getLogger("bookgraph")
            log.exception("Graph extraction failed for %s: %s", book_id, exc)
            # Push error event so SSE clients know
            try:
                queue.put_nowait({
                    "type": "error",
                    "book_id": book_id,
                    "message": str(exc),
                })
            except Exception:
                pass
        finally:
            app.pop(running_key, None)

    task = asyncio.create_task(_run_extraction())
    app[running_key] = task
    return True


async def handle_graph_init(request: web.Request) -> web.Response:
    """POST /api/graph/init/{book_id}  — start graph extraction.

    Query params: ?batch_size=5

    Returns immediately with a "started" response. Progress is tracked
    via graph_progress table, pollable through /api/graph/progress/<book_id>.
    Extraction runs in a background asyncio task.
    """
    book_id = request.match_info["book_id"]
    batch_size = int(request.query.get("batch_size", "5"))

    db = request.app["db"]
    config = request.app.get("config")

    if not config:
        return web.json_response({"error": "config not available"}, status=503)

    book = db.get_book(book_id)
    if book is None:
        return web.json_response({"error": f"book not found: {book_id}"}, status=404)

    if not _start_graph_extraction(request.app, db, config, book_id, batch_size):
        return web.json_response(
            {"error": "extraction already running", "book_id": book_id},
            status=409,
        )

    return web.json_response({
        "status": "started",
        "book_id": book_id,
        "book_title": book.title,
        "batch_size": batch_size,
        "progress_url": f"/api/graph/progress/{book_id}",
    })


async def handle_graph_retry_failed(request: web.Request) -> web.Response:
    """POST /api/graph/{book_id}/retry-failed  — reset failed chunks and re-run.

    Query params: ?batch_size=5

    Idempotent: chunks already done are skipped by the orchestrator;
    record_result is ON CONFLICT-deduplicated, so re-running a mixed batch
    cannot duplicate nodes/edges.
    """
    book_id = request.match_info["book_id"]
    batch_size = int(request.query.get("batch_size", "5"))

    db = request.app["db"]
    config = request.app.get("config")

    if not config:
        return web.json_response({"error": "config not available"}, status=503)

    book = db.get_book(book_id)
    if book is None:
        return web.json_response({"error": f"book not found: {book_id}"}, status=404)

    # Refuse while an extraction is already running — resetting failed chunks
    # mid-run could corrupt the in-flight task's view of the chunk set.
    running_key = f"graph_running_{book_id}"
    existing_task = request.app.get(running_key)
    if existing_task is not None and not existing_task.done():
        return web.json_response(
            {"error": "extraction already running", "book_id": book_id},
            status=409,
        )

    gdb = _get_graph_db(request, book_id)
    try:
        reset = gdb.reset_failed_chunks(book_id)
        if reset:
            gdb.record_event(book_id, "retry_started",
                             step_label=f"重试失败章节（{reset}）",
                             detail=json.dumps({"reset": reset}, ensure_ascii=False))
    finally:
        gdb.close()

    if reset == 0:
        return web.json_response({"status": "no_failed_chunks", "reset": 0})

    _start_graph_extraction(request.app, db, config, book_id, batch_size)
    return web.json_response({
        "status": "started",
        "book_id": book_id,
        "book_title": book.title,
        "batch_size": batch_size,
        "reset": reset,
        "progress_url": f"/api/graph/progress/{book_id}",
    })


async def handle_graph_progress(request: web.Request) -> web.StreamResponse:
    """GET /api/graph/progress/{book_id}  — SSE stream of progress events.

    Consumes real-time events from an asyncio.Queue (fed by the orchestrator's
    progress_cb).  Falls back to DB polling when the queue is empty (covers
    reconnection and late-connection scenarios).
    """
    book_id = request.match_info["book_id"]
    queue_key = f"graph_queue_{book_id}"

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await resp.prepare(request)

    import json as _json
    gdb = _get_graph_db(request, book_id)
    last_db_id = 0
    complete_sent = False
    # Run scoping: only the CURRENT run's rows are replayed via DB poll.
    # Without this, a stale "complete" left by a previous run fires a
    # premature 提取完成 on every new connection (the restart bug).
    current_run_id: str | None = None

    # Snapshot the queue reference — it may be replaced if extraction restarts
    queue: asyncio.Queue | None = request.app.get(queue_key)

    async def _write_sse(data: dict):
        await resp.write(
            f"data: {_json.dumps(data, ensure_ascii=False)}\n\n".encode()
        )

    async def _try_drain_queue() -> bool:
        """Pull one event from the queue (non-blocking).  Returns True if an
        event was consumed, False if the queue is empty."""
        nonlocal queue, current_run_id, last_db_id
        if queue is None:
            queue = request.app.get(queue_key)
        if queue is None:
            return False
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            return False
        # Refresh reference in case queue was replaced
        queue = request.app.get(queue_key, queue)
        # Anchor to the run the event belongs to (first event sets it; a new
        # run mid-connection resets the replay cursor to the run's start).
        ev_run_id = event.get("run_id") or ""
        if ev_run_id and ev_run_id != current_run_id:
            current_run_id = ev_run_id
            last_db_id = 0
        await _write_sse(event)
        if event.get("type") == "complete":
            nonlocal complete_sent
            complete_sent = True
        return True

    def _poll_db() -> list[dict]:
        """Return new rows from graph_progress for the CURRENT run."""
        nonlocal last_db_id
        if current_run_id is None:
            return []
        try:
            conn = gdb.get_conn()
            rows = conn.execute(
                "SELECT id, batch_id, step_index, step_label, status, detail, "
                "run_id, updated_at FROM graph_progress "
                "WHERE book_id=? AND run_id=? AND id > ? ORDER BY id",
                (book_id, current_run_id, last_db_id),
            ).fetchall()
            new_events = []
            for r in rows:
                new_events.append(dict(r))
                last_db_id = r["id"]
            return new_events
        except Exception:
            return []

    # ── Reconnection anchor ─────────────────────────────────────────────
    # A client that dropped mid-run reconnects to a live task with an empty
    # queue. Anchor to the in-progress run (has run_start, no complete yet)
    # so DB replay catches it up without replaying stale finished runs.
    def _anchor_on_connect() -> None:
        nonlocal current_run_id, last_db_id
        try:
            task = request.app.get(f"graph_running_{book_id}")
            if task is None or task.done():
                return
            latest = gdb.get_latest_run(book_id)
            if not latest:
                return
            run_id_, _ = latest
            has_complete = gdb.get_conn().execute(
                "SELECT 1 FROM graph_progress "
                "WHERE book_id=? AND run_id=? AND batch_id='complete' LIMIT 1",
                (book_id, run_id_),
            ).fetchone()
            if has_complete:
                return  # that run already finished — wait for the next via queue
            current_run_id = run_id_
            last_db_id = 0
        except Exception:
            pass

    _anchor_on_connect()

    try:
        while True:
            # ── 1. Drain the real-time queue first ─────────────────────
            queue_consumed = False
            while await _try_drain_queue():
                queue_consumed = True

            if complete_sent:
                break

            # ── 2. Fall back to DB poll (reconnection / late join) ─────
            # Reconstructs proper event types (batch_start, batch_done, etc.)
            # from graph_progress rows so late-joining SSE clients see the
            # same events as the real-time queue.
            PHASE_EVENT_TYPES = frozenset({
                "run_start", "context_set", "scout_done", "content_filtered",
                "resume", "complete",
            })
            db_events = _poll_db()
            for ev in db_events:
                detail = _json.loads(ev["detail"]) if ev["detail"] else {}
                # Snapshot current stats for every event
                try:
                    stats = gdb.get_stats(book_id)
                except Exception:
                    stats = {}

                batch_id = ev["batch_id"]

                if batch_id in PHASE_EVENT_TYPES:
                    # ── Phase event: batch_id IS the event type ──────────
                    event_data = {
                        "type": batch_id,
                        "book_id": book_id,
                        "run_id": ev.get("run_id", ""),
                        **detail,
                        "stats": stats,
                    }
                else:
                    # ── Batch event: map status → event type ─────────────
                    # Nest batch-specific detail under "detail" to match
                    # the real-time queue event shape.
                    status_to_type = {
                        "in_progress": "batch_start",
                        "done": "batch_done",
                        "failed": "batch_failed",
                        "skipped": "batch_skipped",
                    }
                    event_type = status_to_type.get(ev["status"], "batch_start")
                    event_data = {
                        "type": event_type,
                        "book_id": book_id,
                        "run_id": ev.get("run_id", ""),
                        "batch_id": batch_id,
                        "batch_index": ev["step_index"],
                        "step_label": ev["step_label"],
                        "stats": stats,
                    }
                    if detail:
                        event_data["detail"] = detail

                await _write_sse(event_data)

                if batch_id == "complete":
                    complete_sent = True
                elif ev["status"] == "done" and detail:
                    # Legacy synthetic-complete fallback for old-style rows
                    # that don't have an explicit "complete" phase event in DB.
                    if stats.get("chunks_done", 0) >= stats.get("chunks_total", 1):
                        # Only emit if no real complete event exists yet
                        conn = gdb.get_conn()
                        has_complete = conn.execute(
                            "SELECT 1 FROM graph_progress "
                            "WHERE book_id=? AND batch_id='complete' LIMIT 1",
                            (book_id,),
                        ).fetchone()
                        if not has_complete:
                            await _write_sse({
                                "type": "complete",
                                "book_id": book_id,
                                "stats": stats,
                            })
                            complete_sent = True

            if complete_sent:
                break

            # ── 3. If neither queue nor DB produced anything and extraction
            #       is no longer running, send a final complete and exit ──
            # Safe: _poll_db never replays rows of a DIFFERENT run, so this
            # synthetic complete can't be a stale event from an old run — it's
            # a fresh snapshot of the current state for an idle/finished book.
            graph_task = request.app.get(f"graph_running_{book_id}")
            if (graph_task is None or graph_task.done()) and not queue_consumed and not db_events:
                await asyncio.sleep(1)
                # One last drain + poll
                while await _try_drain_queue():
                    pass
                final_db = _poll_db()
                if not final_db:
                    final_stats = gdb.get_stats(book_id)
                    await _write_sse({
                        "type": "complete",
                        "book_id": book_id,
                        "stats": final_stats,
                    })
                    break

            # Avoid busy-loop when nothing is happening
            if not queue_consumed and not db_events:
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        import logging
        logging.getLogger("bookgraph").warning("SSE progress error: %s", exc)
    finally:
        gdb.close()

    return resp


async def handle_graph_stats(request: web.Request) -> web.Response:
    """GET /api/graph/stats/{book_id}  — current graph statistics."""
    book_id = request.match_info["book_id"]
    gdb = _get_graph_db(request, book_id)
    try:
        stats = gdb.get_stats(book_id)
        return web.json_response({"book_id": book_id, "stats": stats})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        gdb.close()


async def handle_graph_nodes(request: web.Request) -> web.Response:
    """GET /api/graph/nodes/{book_id}?kind=character&limit=100  — list nodes."""
    book_id = request.match_info["book_id"]
    kind = request.query.get("kind")
    limit = int(request.query.get("limit", "100"))
    gdb = _get_graph_db(request, book_id)
    try:
        nodes = gdb.get_nodes_by_kind(book_id, kind=kind, limit=limit)
        return web.json_response({"book_id": book_id, "nodes": nodes})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        gdb.close()


async def handle_graph_books(request: web.Request) -> web.Response:
    """GET /api/graph/books  — list books that have graph data."""
    config = request.app.get("config")
    db = request.app.get("db")
    if not config or not db:
        return web.json_response({"error": "not configured"}, status=503)

    from storage.graph_db import GraphDB
    books = []
    # Resolve via import_log: stable_book_id → book_id, so we know which
    # book each graph DB belongs to and can query it with the correct project_id.
    for f in sorted(config.graphs_dir.glob("graph_*.db")):
        stable_id = f.stem.replace("graph_", "", 1)
        # Find the latest book_id for this stable identity
        import_logs = db.get_import_log_by_stable(stable_id)
        if not import_logs:
            continue
        # Use the most recent import (last in list) as the active book
        latest_log = import_logs[-1]
        book_id = latest_log.book_id
        book = db.get_book(book_id)
        if book is None:
            continue
        graph_db = None
        try:
            graph_db = GraphDB(f)
            stats = graph_db.get_stats(book_id)
            # Include books whose batches all failed — they still need the
            # retry entry point in the management panel.
            if stats["chunks_done"] > 0 or stats.get("chunks_failed", 0) > 0:
                books.append({
                    "book_id": book_id,
                    "title": book.title,
                    "author": book.author or "",
                    "nodes": stats["nodes"],
                    "edges": stats["edges"],
                    "aliases": stats["aliases"],
                    "merge_candidates": stats["merge_candidates"],
                    "chunks_done": stats["chunks_done"],
                    "chunks_total": stats["chunks_total"],
                    "chunks_failed": stats.get("chunks_failed", 0),
                    "kinds": stats.get("kinds", {}),
                })
        except Exception:
            pass  # corrupt or empty graph.db — skip
        finally:
            if graph_db is not None:
                graph_db.close()
    return web.json_response({"books": books})


# ── LLM call logs (flight recorder 图谱 tab) ─────────────────────────────

def _find_llm_run_file(config, run_id: str):
    """Locate the graph.db path + book_id owning ``run_id``.

    Returns ``(graph_db_path, book_id)`` or None.  Scans every graph_*.db —
    the per-book design means a run lives in exactly one graph file.
    """
    from storage.graph_db import GraphDB
    for f in sorted(config.graphs_dir.glob("graph_*.db")):
        gdb = None
        try:
            gdb = GraphDB(f)
            for r in gdb.list_llm_runs():
                if r["run_id"] == run_id:
                    return f, r.get("book_id") or f.stem.replace("graph_", "", 1)
        except Exception:
            pass
        finally:
            if gdb is not None:
                gdb.close()
    return None


async def handle_graph_llm_runs(request: web.Request) -> web.Response:
    """GET /api/graph/llm-runs  — aggregate LLM call logs across all books."""
    config = request.app.get("config")
    db = request.app.get("db")
    if not config or not db:
        return web.json_response({"error": "not configured"}, status=503)

    from storage.graph_db import GraphDB
    runs = []
    for f in sorted(config.graphs_dir.glob("graph_*.db")):
        stable_id = f.stem.replace("graph_", "", 1)
        import_logs = db.get_import_log_by_stable(stable_id)
        if not import_logs:
            continue
        book = db.get_book(import_logs[-1].book_id)
        title = book.title if book else stable_id
        gdb = None
        try:
            gdb = GraphDB(f)
            for r in gdb.list_llm_runs():
                r["book_title"] = title
                r["stable_id"] = stable_id
                runs.append(r)
        except Exception:
            pass
        finally:
            if gdb is not None:
                gdb.close()
    runs.sort(key=lambda r: (r.get("last_at") or ""), reverse=True)
    return web.json_response({"runs": runs})


async def handle_graph_llm_run_calls(request: web.Request) -> web.Response:
    """GET /api/graph/llm-runs/{run_id}/calls  — list a run's LLM calls."""
    run_id = request.match_info["run_id"]
    config = request.app.get("config")
    if not config:
        return web.json_response({"error": "not configured"}, status=503)
    found = _find_llm_run_file(config, run_id)
    if not found:
        return web.json_response({"error": f"run not found: {run_id}"}, status=404)
    path, book_id = found

    from storage.graph_db import GraphDB
    gdb = GraphDB(path)
    try:
        calls = gdb.list_llm_calls(run_id, phase=request.query.get("phase"))
        return web.json_response({"run_id": run_id, "book_id": book_id, "calls": calls})
    finally:
        gdb.close()


async def handle_graph_llm_run_call(request: web.Request) -> web.Response:
    """GET /api/graph/llm-runs/{run_id}/calls/{call_id}  — full call."""
    run_id = request.match_info["run_id"]
    config = request.app.get("config")
    if not config:
        return web.json_response({"error": "not configured"}, status=503)
    try:
        call_id = int(request.match_info["call_id"])
    except ValueError:
        return web.json_response({"error": "invalid call_id"}, status=404)
    found = _find_llm_run_file(config, run_id)
    if not found:
        return web.json_response({"error": f"run not found: {run_id}"}, status=404)
    path, _ = found

    from storage.graph_db import GraphDB
    gdb = GraphDB(path)
    try:
        call = gdb.get_llm_call(call_id)
        if call is None or call.get("run_id") != run_id:
            return web.json_response({"error": "call not found"}, status=404)
        return web.json_response(call)
    finally:
        gdb.close()


async def handle_graph_llm_run_delete(request: web.Request) -> web.Response:
    """DELETE /api/graph/llm-runs/{run_id}  — delete one run's LLM logs."""
    run_id = request.match_info["run_id"]
    config = request.app.get("config")
    if not config:
        return web.json_response({"error": "not configured"}, status=503)
    found = _find_llm_run_file(config, run_id)
    if not found:
        return web.json_response({"error": f"run not found: {run_id}"}, status=404)
    path, _ = found

    from storage.graph_db import GraphDB
    gdb = GraphDB(path)
    try:
        deleted = gdb.delete_llm_run(run_id)
        return web.json_response({"deleted": deleted, "run_id": run_id})
    finally:
        gdb.close()


async def handle_graph_characters(request: web.Request) -> web.Response:
    """GET /api/graph/{book_id}/characters  — list character nodes."""
    book_id = request.match_info["book_id"]
    gdb = _get_graph_db(request, book_id)
    try:
        chars = gdb.get_nodes_by_kind(book_id, kind="character", limit=200)
        # Enrich with edge count for sorting
        for c in chars:
            c["edge_count"] = gdb.total_node_count(book_id)  # rough proxy
        return web.json_response({"book_id": book_id, "characters": chars})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        gdb.close()


async def handle_book_authors(request: web.Request) -> web.Response:
    """GET /api/authors/{book_id} — 这本书**可扮演的作者**。

    路径参数是 ``book_id``（与 ``/api/graph/{book_id}/characters`` 同一个
    位置约定）。作者层与图谱是两套 id 空间，所以**不合并成一个
    「可扮演者」接口** —— 那会让前端要分辨两种 id（设计稿 §八 选项 A）。

    只列 ``role ∈ (author, co-author)``：译者/编者也在库里，但「扮演作者」
    对他们不成立，等真有需求再放出来。每条带 ``corpus``/``records`` 计数，
    前端据此隐藏空壳 —— 没有语料的作者扮演起来是空的，不如不列。
    """
    book_id = request.match_info["book_id"]
    db = request.app.get("db")
    config = request.app.get("config")
    if not db:
        return web.json_response({"error": "no books db"}, status=500)
    try:
        from authorgraph.scope import book_identity
        from storage.authors_db import AuthorsDB, resolve_authors_db_path
        ident = book_identity(db, book_id)
        adb = AuthorsDB(resolve_authors_db_path(config))
        try:
            rows = adb.authors_of_book(ident["stable_book_id"])
            out = []
            for r in rows:
                if (r.get("role") or "author") not in ("author", "co-author"):
                    continue
                st = adb.stats(r["author_id"])
                out.append({
                    "author_id": r["author_id"],
                    "canonical_name": r.get("canonical_name") or "",
                    "nationality": r.get("nationality") or "",
                    "birth_year": r.get("birth_year"),
                    "death_year": r.get("death_year"),
                    "role": r.get("role") or "author",
                    "position": r.get("position") or "unknown",
                    "corpus": st.get("corpus", 0),
                    "records": st.get("records", 0),
                })
        finally:
            adb.close()
        # 有话可说的排前面；都没语料时保持 role 顺序（author 在 co-author 前）。
        out.sort(key=lambda a: (-(a["corpus"] + a["records"]), a["role"]))
        return web.json_response({
            "book_id": book_id,
            "stable_book_id": ident["stable_book_id"],
            "authors": out,
        })
    except Exception as exc:                             # noqa: BLE001
        logging.getLogger("api").warning("book authors failed: %s", exc)
        return web.json_response({"book_id": book_id, "authors": [],
                                  "error": str(exc)}, status=500)


async def handle_author_timeline(request: web.Request) -> web.Response:
    """GET /api/authors/{book_id}/{author_id}/timeline — 一位作者的时间轴。

    **一根轴，上下两半**：上半 ``work``（作品），下半 ``life``（生平）。
    任一半可以是空的 —— 张景只有作品、没有生平，下半就该是空的，
    不要为了「好看」补内容。

    ``max_chapter`` 给了就施**同一条**剧透门（``gate_state``，全层唯一一处），
    没给就是**显式的不设门**（作者页的「全集视角」）。两种情况都在响应里
    如实标出来 —— 「无门」和「门开着」必须能分辨。

    无年份的条目**单独一桶**（``undated``），不混进轴上：轴的位置是年份，
    没有年份就没有位置。有门时它们本来就出不去（fail-closed）。
    """
    book_id = request.match_info["book_id"]
    author_id = request.match_info["author_id"]
    db = request.app.get("db")
    config = request.app.get("config")
    if not db:
        return web.json_response({"error": "no books db"}, status=500)
    try:
        from authorgraph.gate import gate_state
        from authorgraph.refresh import room_of
        from authorgraph.scope import book_identity
        from storage.authors_db import AuthorsDB, resolve_authors_db_path
        ident = book_identity(db, book_id)
        raw = request.query.get("max_chapter")
        reader_chapter = int(raw) if raw not in (None, "") else None
        adb = AuthorsDB(resolve_authors_db_path(config))
        try:
            author = adb.get_author(author_id)
            if not author:
                return web.json_response({"error": "查无此作者"}, status=404)
            gstate = (gate_state(adb, author_id, ident["stable_book_id"],
                                 reader_chapter)
                      if reader_chapter is not None
                      else {"gate": None, "reason": "no_gate",
                            "dated": 0, "ahead": 0})
            gated = reader_chapter is not None and gstate["gate"] is None
            rows = [] if gated else adb.records_for(
                author_id, max_year=gstate["gate"], limit=1000)
            total = adb.stats(author_id).get("records", 0)
            # 这本书里的身份（role/position）在 author_books 上，不在 authors 上 ——
            # 作者表的 `birth_year` 是「这个人」，relation 里的才是「在这本书里是谁」。
            rel = next((x for x in adb.authors_of_book(ident["stable_book_id"])
                        if x.get("author_id") == author_id), {})
        finally:
            adb.close()
    except Exception as exc:                             # noqa: BLE001
        logging.getLogger("api").warning("author timeline failed: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)

    rooms: dict[str, list] = {"work": [], "life": []}
    undated = {"work": 0, "life": 0}
    undated_items: list[dict[str, Any]] = []
    for r in rows:
        room = room_of(r.get("kind") or "")
        if r.get("year") is None:
            undated[room] += 1
            # 「没有年份」不是「没有内容」：不给位置，但得让人看得见。
            # 上限 60 条，够翻一眼又不至于把面板撑爆。
            if len(undated_items) < 60:
                undated_items.append({
                    "statement": r.get("statement") or "",
                    "kind": r.get("kind") or "",
                    "room": room,
                    "source": r.get("source") or "",
                    "url": (r.get("evidence_excerpt") or "")
                           if r.get("source") == "web" else "",
                })
            continue
        rooms[room].append({
            "id": r["id"], "year": r["year"], "year_end": r.get("year_end"),
            "statement": r.get("statement") or "",
            "kind": r.get("kind") or "",
            "source": r.get("source") or "",
            "url": (r.get("evidence_excerpt") or "")
                   if r.get("source") == "web" else "",
        })
    years = [x["year"] for v in rooms.values() for x in v]
    out: dict[str, Any] = {
        "book_id": book_id,
        "author_id": author_id,
        # 报出来：`stable_book_id` 解析不出来时会**静默退回** book_id
        # （`book_identity` 的既定兜底），而那样 `gate_state` 一条都查不到，
        # 表现成 `no_dated` —— 与「序跋里真没年份」长得一模一样。
        "stable_book_id": ident["stable_book_id"],
        "name": author.get("canonical_name") or "",
        "gate": gstate["gate"],
        "gate_reason": gstate["reason"],
        "gate_dated": gstate["dated"],
        "gate_ahead": gstate["ahead"],
        "span": {"min": min(years) if years else None,
                 "max": max(years) if years else None},
        "work": rooms["work"],
        "life": rooms["life"],
        "undated": undated,
        "undated_items": undated_items,
        # 头部要的「这个人是谁」：作者表给国籍/生卒，relation 给书内身份。
        "author": {
            "nationality": author.get("nationality") or "",
            "birth_year": author.get("birth_year"),
            "death_year": author.get("death_year"),
            "role": rel.get("role") or "author",
            "position": rel.get("position") or "unknown",
        },
        "not_shown": max(0, total - len(rows)),
    }
    if (ident["stable_book_id"] == book_id and gstate["reason"] == "no_dated"
            and not rows):
        # 两种完全不同的原因，必须分开说：真没年份 vs 根本没对上这本书。
        out["note"] = ("书的 stable id 没解析出来（退回用了 book_id），"
                       "所以剧透门一条语料都查不到 —— 这**不是**"
                       "「序跋里没有年份」。")
    return web.json_response(out)


async def handle_graph_full(request: web.Request) -> web.Response:
    """GET /api/graph/{book_id}/full?kinds=character,event&limit=500  — full graph data."""
    book_id = request.match_info["book_id"]
    kinds_str = request.query.get("kinds")
    kinds = kinds_str.split(",") if kinds_str else None
    limit = int(request.query.get("limit", "500"))
    gdb = _get_graph_db(request, book_id)
    try:
        result = gdb.get_full_graph(book_id, kinds=kinds, limit=limit)
        # 读者笔记挂在节点上（见 docs/reader-notes-design.md）。
        # **图谱页不按阅读进度过滤** —— 用户有权翻阅 agent 的笔记本；
        # 披露门管的是 agent **主动提及**，不是用户主动查看。
        # node_id 为空的笔记（实测 26%，写作手法/段落反应）挂不上任何节点，
        # 只对 agent 自己可见 —— 这是设计，不是遗漏。
        by_node: dict[str, list[dict]] = {}
        for nt in gdb.get_notes(book_id):
            nid = nt.get("node_id")
            if nid:
                by_node.setdefault(nid, []).append({
                    "quote": nt.get("quote_text") or "",
                    "text": nt.get("text") or "",
                })
        for nd in result["nodes"]:
            nd["notes"] = by_node.get(nd.get("id"), [])
        return web.json_response({
            "book_id": book_id,
            "nodes": result["nodes"],
            "edges": result["edges"],
        })
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        gdb.close()


async def handle_graph_merge_candidates(request: web.Request) -> web.Response:
    """GET /api/graph/{book_id}/merge-candidates?status=pending"""
    book_id = request.match_info["book_id"]
    status = request.query.get("status") or None
    gdb = _get_graph_db(request, book_id)
    try:
        candidates = gdb.get_merge_candidates(book_id, status=status)
        return web.json_response({"book_id": book_id, "candidates": candidates,
                                   "count": len(candidates)})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        gdb.close()


async def handle_graph_merge(request: web.Request) -> web.Response:
    """POST /api/graph/{book_id}/merge  {keep_id, remove_id}"""
    book_id = request.match_info["book_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    keep_id = body.get("keep_id", "").strip()
    remove_id = body.get("remove_id", "").strip()
    if not keep_id or not remove_id:
        return web.json_response({"error": "keep_id and remove_id required"}, status=400)
    if keep_id == remove_id:
        return web.json_response({"error": "cannot merge a node into itself"}, status=400)
    gdb = _get_graph_db(request, book_id)
    try:
        result = gdb.merge_nodes(book_id, keep_id, remove_id)
        return web.json_response({"status": "merged", "book_id": book_id, **result})
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        gdb.close()


async def handle_graph_merge_reject(request: web.Request) -> web.Response:
    """POST /api/graph/{book_id}/merge-candidates/{candidate_id}/reject"""
    book_id = request.match_info["book_id"]
    candidate_id = request.match_info["candidate_id"]
    gdb = _get_graph_db(request, book_id)
    try:
        conn = gdb.get_conn()
        conn.execute(
            "UPDATE graph_merge_candidates SET status='rejected' WHERE id=? AND project_id=?",
            (candidate_id, book_id),
        )
        return web.json_response({"status": "rejected", "candidate_id": int(candidate_id)})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        gdb.close()


async def handle_graph_auto_merge(request: web.Request) -> web.Response:
    """POST /api/graph/{book_id}/auto-merge  {min_confidence?: 0.9}"""
    book_id = request.match_info["book_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    min_conf = float(body.get("min_confidence", 0.9))
    gdb = _get_graph_db(request, book_id)
    try:
        merged = gdb.auto_merge(book_id, min_confidence=min_conf)
        return web.json_response({"status": "done", "book_id": book_id, "merged": merged})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        gdb.close()


async def handle_graph_reset(request: web.Request) -> web.Response:
    """DELETE /api/graph/{book_id}  — delete graph.db for this book."""
    book_id = request.match_info["book_id"]
    config = request.app.get("config")
    if not config:
        return web.json_response({"error": "config not available"}, status=503)
    from pathlib import Path
    from storage.graph_db import resolve_graph_db_path
    db = request.app.get("db")
    # Reset must remove BOTH naming candidates — a legacy book_id-named
    # file left behind would otherwise be migrated back into existence on
    # the next graph read (reset should be a true reset).
    graph_path = resolve_graph_db_path(config, db, book_id) if db else config.graph_db_path(book_id)
    deleted = []
    for p in {graph_path, config.graph_db_path(book_id)}:
        if p and p.exists():
            try:
                p.unlink()
                deleted.append(p.name)
            except OSError:
                pass
    if deleted:
        return web.json_response({"status": "deleted", "book_id": book_id, "files": deleted})
    return web.json_response({"error": "graph not found"}, status=404)
