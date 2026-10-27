"""Book Companion HTTP layer — composition root + facade.

This file owns three things and nothing else:

* the aiohttp app: middlewares, dependency injection, and the **ordered**
  route table (``create_app``). Route registration order is a correctness
  contract — e.g. ``/api/sessions/search`` must precede
  ``/api/sessions/{session_id}``. Handlers may move; these ``add_*`` lines
  must not be reordered.
* process-level endpoints: ``/api/health`` and its code fingerprint.
* the launcher (``run_server``).

Handlers live in ``api/routes/<domain>.py``; shared helpers in
``api/sse.py`` / ``api/prompt_builders.py`` / ``api/text_helpers.py`` /
``api/book_helpers.py``. They are re-exported below so existing
``from api_server import X`` call sites keep working.

Start with::

    python api_server.py

Or import and call ``create_app()`` from main.py.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path

from aiohttp import web

# Context flight recorder (debugging) — packaged as flight_recorder/
from flight_recorder.server import install_context_log_routes


# ── Extracted handlers re-exported for existing import sites ──
from api.routes.avatar import (
    handle_avatar_json,
    handle_avatar_serve,
    handle_avatar_upload,
)
from api.routes.voice import (
    handle_asr,
    handle_tts,
    handle_voice_serve,
    handle_voice_upload,
    warm_asr_background,
)
from api.routes.authors import (
    handle_author_dossier,
    handle_author_sources,
    handle_author_voice,
    handle_authors_list,
)
from api.routes.graph import (
    handle_author_timeline,
    handle_book_authors,
    handle_graph_auto_merge,
    handle_graph_books,
    handle_graph_characters,
    handle_graph_full,
    handle_graph_init,
    handle_graph_llm_run_call,
    handle_graph_llm_run_calls,
    handle_graph_llm_run_delete,
    handle_graph_llm_runs,
    handle_graph_merge,
    handle_graph_merge_candidates,
    handle_graph_merge_reject,
    handle_graph_nodes,
    handle_graph_progress,
    handle_graph_reset,
    handle_graph_retry_failed,
    handle_graph_stats,
)
from api.routes.chat import (_TurnCancelRegistry, handle_chat, handle_chat_cancel)
from api.routes.sessions import (handle_session_archive, handle_session_branches, handle_session_children, handle_session_delete, handle_session_fork, handle_session_latest, handle_session_load, handle_session_search, handle_session_set_title, handle_sessions_list)
from api.routes.sync import (handle_sync)
from api.routes.books import (handle_book_file, handle_chunk, handle_context, handle_cover, handle_delete_book, handle_import_book, handle_list_books, handle_scan_folder, handle_search_book, handle_search_chapter, handle_search_overview, handle_set_book_order, handle_update_book, handle_upload_book)
from api.routes.annotations import (handle_clear_override, handle_create_annotation, handle_delete_annotation, handle_list_annotations, handle_list_chapter_types, handle_set_override, handle_update_annotation)
from api.routes.console import (
    handle_console_config,
    handle_context_usage,
    handle_models_fetch,
    handle_provider_delete,
    handle_provider_save,
    handle_provider_test,
    handle_route_switch,
    handle_slot_switch,
    handle_usage_overview,
    handle_usage_profiles_get,
    handle_usage_profiles_put,
)
from api.routes.writing import (
    handle_writing_apply_revision,
    handle_writing_chapter,
    handle_writing_content,
    handle_writing_context,
    handle_writing_contract,
    handle_writing_create_chapter,
    handle_writing_create_work,
    handle_writing_delete_work,
    handle_writing_feedback,
    handle_writing_feedback_status,
    handle_writing_get_review,
    handle_writing_map,
    handle_writing_propose_revision,
    handle_writing_reject_revision,
    handle_writing_rename_chapter,
    handle_writing_rename_work,
    handle_writing_review,
    handle_writing_references_get,
    handle_writing_references_set,
    handle_writing_revisions,
    handle_writing_saved_content,
    handle_writing_set_tension,
    handle_writing_set_work_pin,
    handle_writing_settle,
    handle_writing_style_check,
    handle_writing_style_get,
    handle_writing_style_set,
    handle_writing_tension,
    handle_writing_vcs_amend,
    handle_writing_vcs_branches,
    handle_writing_vcs_checkout,
    handle_writing_vcs_cherry_pick,
    handle_writing_vcs_commit,
    handle_writing_vcs_create_branch,
    handle_writing_vcs_create_tag,
    handle_writing_vcs_delete_branch,
    handle_writing_vcs_delete_tag,
    handle_writing_vcs_diff,
    handle_writing_vcs_discard,
    handle_writing_vcs_log,
    handle_writing_vcs_merge,
    handle_writing_vcs_merge_base,
    handle_writing_vcs_reflog,
    handle_writing_vcs_refs,
    handle_writing_vcs_rename_branch,
    handle_writing_vcs_restore,
    handle_writing_vcs_revert,
    handle_writing_vcs_stash_drop,
    handle_writing_vcs_stash_list,
    handle_writing_vcs_stash_pop,
    handle_writing_vcs_stash_push,
    handle_writing_vcs_status,
    handle_writing_vcs_switch,
    handle_writing_vcs_tags,
    handle_writing_work,
    handle_writing_works,
    handle_writing_write,
)

# Wall-clock start of *this process*, surfaced by /api/health. A server that
# has been up longer than the newest source edit is running stale code.
_PROCESS_STARTED = datetime.now()


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Allow cross-origin requests from file:// and localhost."""
    if request.method == "OPTIONS":
        response = web.Response(status=204)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@web.middleware
async def nocache_middleware(request: web.Request, handler) -> web.Response:
    """Disable browser caching for all responses (dev-friendly)."""
    response = await handler(request)
    response.headers.setdefault(
        "Cache-Control", "no-cache, no-store, must-revalidate"
    )
    return response


def _setup_api_file_logging() -> None:
    """Attach a rotating file handler (+ stderr echo) to the "api" logger.

    Idempotent. Without this, SSE/stream failure warnings live only in the
    console window — the 2026-09-18 reply-loss incident left no trace
    because every warning went to stdout while the server ran headless.
    """
    log_path = Path.cwd() / "data" / "logs" / "api.log"
    lg = logging.getLogger("api")
    if any(
        isinstance(h, RotatingFileHandler)
        and str(h.baseFilename) == str(log_path)
        for h in lg.handlers
    ):
        return
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_h = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    lg.setLevel(logging.INFO)
    lg.addHandler(file_h)
    lg.addHandler(stream_h)


def create_app(db, runtime=None, session_service=None, config=None,
               context_recorder=None) -> web.Application:
    """Build the aiohttp Application with all routes.

    Args:
        db: BookDB for book content.
        runtime: ConversationRuntime (optional, for memory/persona).
        session_service: SessionService (optional, for chat persistence).
        config: Config (optional, for BookGraph endpoints).
        context_recorder: ContextRecorder (optional, for debugging).
    """
    app = web.Application(middlewares=[cors_middleware, nocache_middleware])
    _setup_api_file_logging()

    # Inject dependencies
    app["db"] = db
    app["runtime"] = runtime
    app["session_service"] = session_service
    app["api_client"] = None  # Set by main.py after creating the app
    app["config"] = config
    # User-initiated stop: turn_id -> asyncio.Event (see handle_chat_cancel).
    app["turn_cancels"] = _TurnCancelRegistry()

    # ── Routes ──────────────────────────────────────────────────────

    app.router.add_get("/", handle_index)
    app.router.add_get("/avatars/{book_id}/{name}.png", handle_avatar_serve)
    app.router.add_get("/avatars/{book_id}/{name}.json", handle_avatar_json)
    app.router.add_get("/voice/{id}.wav", handle_voice_serve)
    app.router.add_post("/api/voice/upload", handle_voice_upload)
    app.router.add_static("/", Path(__file__).parent / "reader", show_index=False)
    app.router.add_post("/api/chat", handle_chat)
    app.router.add_post("/api/chat/cancel", handle_chat_cancel)
    app.router.add_post("/api/tts", handle_tts)
    app.router.add_post("/api/asr", handle_asr)
    app.router.add_post("/api/sync", handle_sync)
    app.router.add_get("/api/books", handle_list_books)
    app.router.add_get("/api/books/scan-folder", handle_scan_folder)
    app.router.add_put("/api/books/order", handle_set_book_order)
    app.router.add_post("/api/books/import", handle_import_book)
    app.router.add_post("/api/books/upload", handle_upload_book)
    app.router.add_get("/api/books/{book_id}/context", handle_context)
    app.router.add_get("/api/books/{book_id}/chapters", handle_list_chapter_types)
    app.router.add_put("/api/books/{book_id}/chapters/{index}/override", handle_set_override)
    app.router.add_delete("/api/books/{book_id}/chapters/{index}/override", handle_clear_override)
    app.router.add_get("/api/books/{book_id}/chunk", handle_chunk)
    app.router.add_get("/api/books/{book_id}/cover", handle_cover)
    app.router.add_get("/api/books/{book_id}/file", handle_book_file)
    app.router.add_put("/api/books/{book_id}", handle_update_book)
    app.router.add_delete("/api/books/{book_id}", handle_delete_book)
    app.router.add_get("/api/watch/status", handle_watch_status)
    app.router.add_post("/api/watch/scan", handle_watch_scan)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/books/{book_id}/search", handle_search_book)
    app.router.add_get("/api/books/{book_id}/search/overview", handle_search_overview)
    app.router.add_get("/api/books/{book_id}/search/chapter", handle_search_chapter)
    app.router.add_get("/api/annotations", handle_list_annotations)
    app.router.add_post("/api/annotations", handle_create_annotation)
    app.router.add_put("/api/annotations/{ann_id}", handle_update_annotation)
    app.router.add_delete("/api/annotations/{ann_id}", handle_delete_annotation)

    # WriteGraph — 写作工作台
    app.router.add_get("/api/writing/works", handle_writing_works)
    app.router.add_post("/api/writing/works", handle_writing_create_work)
    app.router.add_get("/api/writing/works/{work_id}", handle_writing_work)
    app.router.add_put("/api/writing/works/{work_id}",
                       handle_writing_rename_work)
    app.router.add_delete("/api/writing/works/{work_id}",
                          handle_writing_delete_work)
    app.router.add_put("/api/writing/works/{work_id}/pin",
                       handle_writing_set_work_pin)
    app.router.add_get("/api/writing/works/{work_id}/map",
                       handle_writing_map)
    app.router.add_get("/api/writing/works/{work_id}/tension",
                       handle_writing_tension)
    app.router.add_put("/api/writing/works/{work_id}/tension",
                       handle_writing_set_tension)
    app.router.add_get("/api/writing/works/{work_id}/style",
                       handle_writing_style_get)
    app.router.add_put("/api/writing/works/{work_id}/style",
                       handle_writing_style_set)
    app.router.add_get("/api/writing/works/{work_id}/references",
                       handle_writing_references_get)
    app.router.add_put("/api/writing/works/{work_id}/references",
                       handle_writing_references_set)
    app.router.add_post("/api/writing/works/{work_id}/chapters",
                        handle_writing_create_chapter)
    app.router.add_get("/api/writing/works/{work_id}/chapters/{index}",
                       handle_writing_chapter)
    app.router.add_put(
        "/api/writing/works/{work_id}/chapters/{index}/title",
        handle_writing_rename_chapter)
    app.router.add_get(
        "/api/writing/works/{work_id}/chapters/{index}/context",
        handle_writing_context)
    app.router.add_put(
        "/api/writing/works/{work_id}/chapters/{index}/contract",
        handle_writing_contract)
    app.router.add_put(
        "/api/writing/works/{work_id}/chapters/{index}/content",
        handle_writing_content)
    app.router.add_get(
        "/api/writing/works/{work_id}/chapters/{index}/saved",
        handle_writing_saved_content)
    app.router.add_post(
        "/api/writing/works/{work_id}/chapters/{index}/write",
        handle_writing_write)
    app.router.add_post(
        "/api/writing/works/{work_id}/chapters/{index}/settle",
        handle_writing_settle)
    app.router.add_post(
        "/api/writing/works/{work_id}/chapters/{index}/review",
        handle_writing_review)
    app.router.add_get(
        "/api/writing/works/{work_id}/chapters/{index}/review",
        handle_writing_get_review)
    app.router.add_get(
        "/api/writing/works/{work_id}/chapters/{index}/revisions",
        handle_writing_revisions)
    app.router.add_get(
        "/api/writing/works/{work_id}/chapters/{index}/style-check",
        handle_writing_style_check)
    app.router.add_post(
        "/api/writing/works/{work_id}/chapters/{index}/findings/{finding_id}/revision",
        handle_writing_propose_revision)
    app.router.add_post(
        "/api/writing/works/{work_id}/chapters/{index}/revisions/{revision_id}/apply",
        handle_writing_apply_revision)
    app.router.add_post(
        "/api/writing/works/{work_id}/chapters/{index}/revisions/{revision_id}/reject",
        handle_writing_reject_revision)
    # 写作 git（项目级提交 / 分支 / 回滚）
    app.router.add_get("/api/writing/works/{work_id}/vcs/status",
                       handle_writing_vcs_status)
    app.router.add_get("/api/writing/works/{work_id}/vcs/log",
                       handle_writing_vcs_log)
    app.router.add_get("/api/writing/works/{work_id}/vcs/diff",
                       handle_writing_vcs_diff)
    app.router.add_post("/api/writing/works/{work_id}/vcs/commit",
                        handle_writing_vcs_commit)
    app.router.add_get("/api/writing/works/{work_id}/vcs/branches",
                       handle_writing_vcs_branches)
    app.router.add_post("/api/writing/works/{work_id}/vcs/branches",
                        handle_writing_vcs_create_branch)
    app.router.add_post("/api/writing/works/{work_id}/vcs/checkout",
                        handle_writing_vcs_checkout)
    app.router.add_post("/api/writing/works/{work_id}/vcs/restore",
                        handle_writing_vcs_restore)
    app.router.add_get("/api/writing/works/{work_id}/vcs/refs",
                       handle_writing_vcs_refs)
    app.router.add_get("/api/writing/works/{work_id}/vcs/reflog",
                       handle_writing_vcs_reflog)
    app.router.add_get("/api/writing/works/{work_id}/vcs/merge-base",
                       handle_writing_vcs_merge_base)
    app.router.add_post("/api/writing/works/{work_id}/vcs/switch",
                        handle_writing_vcs_switch)
    app.router.add_post("/api/writing/works/{work_id}/vcs/branches/rename",
                        handle_writing_vcs_rename_branch)
    app.router.add_post("/api/writing/works/{work_id}/vcs/branches/delete",
                        handle_writing_vcs_delete_branch)
    app.router.add_get("/api/writing/works/{work_id}/vcs/tags",
                       handle_writing_vcs_tags)
    app.router.add_post("/api/writing/works/{work_id}/vcs/tags",
                        handle_writing_vcs_create_tag)
    app.router.add_post("/api/writing/works/{work_id}/vcs/tags/delete",
                        handle_writing_vcs_delete_tag)
    app.router.add_get("/api/writing/works/{work_id}/vcs/stash",
                       handle_writing_vcs_stash_list)
    app.router.add_post("/api/writing/works/{work_id}/vcs/stash",
                        handle_writing_vcs_stash_push)
    app.router.add_post("/api/writing/works/{work_id}/vcs/stash/pop",
                        handle_writing_vcs_stash_pop)
    app.router.add_post("/api/writing/works/{work_id}/vcs/stash/drop",
                        handle_writing_vcs_stash_drop)
    app.router.add_post("/api/writing/works/{work_id}/vcs/merge",
                        handle_writing_vcs_merge)
    app.router.add_post("/api/writing/works/{work_id}/vcs/revert",
                        handle_writing_vcs_revert)
    app.router.add_post("/api/writing/works/{work_id}/vcs/cherry-pick",
                        handle_writing_vcs_cherry_pick)
    app.router.add_post("/api/writing/works/{work_id}/vcs/amend",
                        handle_writing_vcs_amend)
    app.router.add_post("/api/writing/works/{work_id}/vcs/discard",
                        handle_writing_vcs_discard)
    app.router.add_get("/api/writing/feedback", handle_writing_feedback)
    app.router.add_post("/api/writing/feedback/{feedback_id}/status",
                        handle_writing_feedback_status)

    # BookGraph — knowledge graph
    app.router.add_get("/api/graph/books", handle_graph_books)
    app.router.add_post("/api/graph/init/{book_id}", handle_graph_init)
    app.router.add_get("/api/graph/progress/{book_id}", handle_graph_progress)
    app.router.add_get("/api/graph/stats/{book_id}", handle_graph_stats)
    app.router.add_get("/api/graph/nodes/{book_id}", handle_graph_nodes)
    app.router.add_get("/api/graph/{book_id}/characters", handle_graph_characters)
    # 作者档案（设计稿 05）—— 新接口，旧 timeline 保持不变。
    app.router.add_get("/api/authors", handle_authors_list)
    app.router.add_get("/api/authors/{author_id}/dossier",
                       handle_author_dossier)
    app.router.add_get("/api/authors/{author_id}/voice", handle_author_voice)
    app.router.add_get("/api/authors/{author_id}/sources",
                       handle_author_sources)
    app.router.add_get("/api/authors/{book_id}", handle_book_authors)
    app.router.add_get("/api/authors/{book_id}/{author_id}/timeline",
                       handle_author_timeline)
    app.router.add_post("/api/graph/{book_id}/characters/{name}/avatar", handle_avatar_upload)
    app.router.add_get("/api/graph/{book_id}/full", handle_graph_full)
    app.router.add_get("/api/graph/{book_id}/merge-candidates", handle_graph_merge_candidates)
    app.router.add_post("/api/graph/{book_id}/merge", handle_graph_merge)
    app.router.add_post("/api/graph/{book_id}/auto-merge", handle_graph_auto_merge)
    app.router.add_post("/api/graph/{book_id}/merge-candidates/{candidate_id}/reject", handle_graph_merge_reject)
    app.router.add_post("/api/graph/{book_id}/retry-failed", handle_graph_retry_failed)
    app.router.add_delete("/api/graph/{book_id}", handle_graph_reset)
    app.router.add_get("/api/graph/llm-runs", handle_graph_llm_runs)
    app.router.add_get("/api/graph/llm-runs/{run_id}/calls", handle_graph_llm_run_calls)
    app.router.add_get("/api/graph/llm-runs/{run_id}/calls/{call_id}", handle_graph_llm_run_call)
    app.router.add_delete("/api/graph/llm-runs/{run_id}", handle_graph_llm_run_delete)

    # Sessions (chat history)
    app.router.add_get("/api/sessions/list", handle_sessions_list)
    app.router.add_get("/api/sessions/search", handle_session_search)
    app.router.add_get("/api/sessions/{session_id}", handle_session_load)
    app.router.add_delete("/api/sessions/{session_id}", handle_session_delete)
    app.router.add_post("/api/sessions/{session_id}/title", handle_session_set_title)
    app.router.add_post("/api/sessions/{session_id}/archive", handle_session_archive)
    app.router.add_post("/api/sessions/{session_id}/fork", handle_session_fork)
    app.router.add_get("/api/sessions/{session_id}/children", handle_session_children)
    app.router.add_get("/api/sessions/{session_id}/branches", handle_session_branches)
    app.router.add_get("/api/sessions/{session_id}/latest", handle_session_latest)

    # ── Console (provider/slot management) ───────────────────────────
    app.router.add_get("/api/console/config", handle_console_config)
    app.router.add_post("/api/console/providers", handle_provider_save)
    app.router.add_delete("/api/console/providers/{provider_id}", handle_provider_delete)
    app.router.add_post("/api/console/providers/{provider_id}/test", handle_provider_test)
    app.router.add_post("/api/console/models/fetch", handle_models_fetch)
    app.router.add_put("/api/console/slots/{slot}", handle_slot_switch)
    app.router.add_put("/api/console/routes/{scenario}", handle_route_switch)

    # ── Console · 用量分析 (usage analytics) ────────────────────────────
    app.router.add_get("/api/console/usage/profiles", handle_usage_profiles_get)
    app.router.add_put("/api/console/usage/profiles", handle_usage_profiles_put)
    app.router.add_get("/api/console/usage/overview", handle_usage_overview)
    # Context meter — ring data for the reader chat page
    app.router.add_get("/api/context/usage", handle_context_usage)

    # ── Context flight recorder (debugging) — packaged in flight_recorder/ ──
    install_context_log_routes(app, context_recorder)

    return app


# ── Handlers ──────────────────────────────────────────────────────────


async def handle_index(request: web.Request) -> web.Response:
    """Serve the reader HTML page."""
    html_path = Path(__file__).parent / "reader" / "index.html"
    if not html_path.exists():
        return web.Response(text="reader/index.html not found", status=404)
    return web.FileResponse(path=html_path)


async def handle_watch_status(request: web.Request) -> web.Response:
    """Return watcher status: folder, alive, recent import events."""
    watcher = request.app.get("watcher")
    if not watcher:
        return web.json_response({"watching": False, "reason": "watcher not started"})
    return web.json_response(watcher.get_status())


async def handle_watch_scan(request: web.Request) -> web.Response:
    """Manually trigger a scan of the watch folder (bypasses watchdog events)."""
    watcher = request.app.get("watcher")
    if not watcher:
        return web.json_response({"error": "watcher not started"}, status=503)
    count = watcher._scan_existing()
    return web.json_response({"scanned": count, **watcher.get_status()})


def _http_layer_files() -> list:
    """Every source file that defines the HTTP surface, in stable order."""
    root = Path(__file__).resolve().parent
    files = [root / "api_server.py"]
    api_dir = root / "api"
    if api_dir.is_dir():
        files += sorted(
            p for p in api_dir.rglob("*.py") if "__pycache__" not in p.parts
        )
    return files


def _code_fingerprint() -> dict:
    """Identify the code this *process* is actually running.

    A long-lived server can outlive the source on disk: the HTTP layer
    gets rewritten while the old process keeps serving the old module
    objects.  That failure mode is invisible over HTTP — the endpoints
    answer, routes exist, and bugs that were already fixed still appear
    alive.  Exposing the layer's mtime + size + digest lets the reader
    (and us) tell "code is broken" apart from "process is stale".

    Hashing only ``api_server.py`` would be blind since the routes moved
    to ``api/``: editing ``api/routes/graph.py`` changes no byte of this
    file, so health would report fresh code while the process still runs
    the old one.  The digest covers this file plus every module under
    ``api/`` (path + bytes, sorted).
    """
    import hashlib
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    latest, total, count = 0.0, 0, 0
    for p in _http_layer_files():
        try:
            raw = p.read_bytes()
            mtime = p.stat().st_mtime
        except OSError:  # pragma: no cover - diagnostics only
            continue
        digest.update(p.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        latest = max(latest, mtime)
        total += len(raw)
        count += 1
    if not count:  # pragma: no cover
        return {"api_server_mtime": "unreadable"}
    return {
        "api_server_mtime": datetime.fromtimestamp(latest).isoformat(
            timespec="seconds"),
        "api_server_bytes": total,
        "api_server_sha256": digest.hexdigest()[:16],
        "api_source_files": count,
    }


_ENV_PLACEHOLDER_KEYS = {"sk-your-key-here"}


def _llm_ready(config) -> bool:
    """主 LLM 是否已配置：key 非空且不是 .env.example 的占位值。

    config.api_key 由启动时 apply_to_config 与控制台热应用实时维护，
    这里读它即是权威状态；前端 onboarding 靠 /api/health 的这个字段触发。
    """
    key = (getattr(config, "api_key", "") or "").strip()
    return bool(key) and key not in _ENV_PLACEHOLDER_KEYS


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint.

    Includes the code fingerprint so a stale long-running process is
    detectable by comparing ``api_server_sha256`` against the file on disk.
    ``llm_ready`` 驱动前端首次运行引导（onboarding modal）。
    """
    config = request.app.get("config")
    return web.json_response({
        "status": "ok",
        "pid": os.getpid(),
        "started": _PROCESS_STARTED.isoformat(timespec="seconds"),
        "llm_ready": bool(config) and _llm_ready(config),
        **_code_fingerprint(),
    })


def run_server(db, host: str = "127.0.0.1", port: int = 8766,
               session_service=None, context_recorder=None):
    """Blocking convenience - run the API server."""
    app = create_app(db, session_service=session_service,
                     context_recorder=context_recorder)
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    import sys
    from pathlib import Path as P
    sys.path.insert(0, str(P(__file__).parent))
    from storage.db import BookDB

    # Use the book-domain database (data/books.db)
    data_dir = P(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = data_dir / "books.db"
    db = BookDB(db_path)
    db.initialize()

    # Initialize session service for chat persistence (best-effort)
    session_service = None
    try:
        from config import load_config
        from storage.db_session import SessionDB
        from services.session_service import SessionService
        config = load_config(P(__file__).parent)
        session_db = SessionDB(config.sessions_db_path)
        session_db.initialize()
        session_service = SessionService(session_db, config)
    except Exception as exc:
        print(f"Warning: session service unavailable ({exc}). Chat history will not persist.")
        session_service = None

    print(f"Book Companion API starting on http://127.0.0.1:8766")
    print(f"Endpoints:")
    print(f"  POST /api/sync              — page-turn position update")
    print(f"  GET  /api/books/<id>/context — book reading map")
    print(f"  GET  /api/books/<id>/chunk  — chunk content")
    print(f"  GET  /api/health            — health check")
    print(f"  GET  /api/sessions/list     — list chat sessions")
    print(f"  GET  /api/sessions/<id>     — load session messages")
    # Context flight recorder (best-effort, packaged in flight_recorder/)
    try:
        from flight_recorder import create_recorder
        context_recorder = create_recorder(P(__file__).parent)
        if context_recorder is None:
            print('  Context recorder: unavailable')
    except Exception as exc:
        context_recorder = None
        print(f'  Context recorder: unavailable ({exc})')

    run_server(db, session_service=session_service, context_recorder=context_recorder)
