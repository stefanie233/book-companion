"""Book Companion HTTP API — content sync + book context + chunk reading.

Runs alongside the main REPL. Exposes endpoints for:
  - Test reader → GET  / (the test-reader HTML page)
  - Koodo Reader hook → POST /api/sync (page-turn position updates)
  - Agent reading context → GET  /api/books/<id>/context (structured reading map)
  - Chunk retrieval → GET  /api/books/<id>/chunk (by chapter + chunk index)

Start with::

    python api_server.py

Or import and call ``create_app()`` from main.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.web_middlewares import normalize_path_middleware

import usage_log

from services.library_service import match_page_text, get_book_context
from file_readers.epub_semantics import VALID_CHAPTER_TYPES

# Context flight recorder (debugging) — packaged as flight_recorder/
from flight_recorder.server import install_context_log_routes

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

    # ── Routes ──────────────────────────────────────────────────────

    app.router.add_get("/", handle_index)
    app.router.add_get("/avatars/{book_id}/{name}.png", handle_avatar_serve)
    app.router.add_get("/avatars/{book_id}/{name}.json", handle_avatar_json)
    app.router.add_get("/voice/{id}.wav", handle_voice_serve)
    app.router.add_post("/api/voice/upload", handle_voice_upload)
    app.router.add_static("/", Path(__file__).parent / "reader", show_index=False)
    app.router.add_post("/api/chat", handle_chat)
    app.router.add_post("/api/tts", handle_tts)
    app.router.add_post("/api/asr", handle_asr)
    app.router.add_post("/api/sync", handle_sync)
    app.router.add_get("/api/books", handle_list_books)
    app.router.add_get("/api/books/scan-folder", handle_scan_folder)
    app.router.add_post("/api/books/import", handle_import_book)
    app.router.add_post("/api/books/upload", handle_upload_book)
    app.router.add_get("/api/books/{book_id}/context", handle_context)
    app.router.add_get("/api/books/{book_id}/chapters", handle_list_chapter_types)
    app.router.add_put("/api/books/{book_id}/chapters/{index}/override", handle_set_override)
    app.router.add_delete("/api/books/{book_id}/chapters/{index}/override", handle_clear_override)
    app.router.add_get("/api/books/{book_id}/chunk", handle_chunk)
    app.router.add_get("/api/books/{book_id}/cover", handle_cover)
    app.router.add_get("/api/books/{book_id}/file", handle_book_file)
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

    # BookGraph — knowledge graph
    app.router.add_get("/api/graph/books", handle_graph_books)
    app.router.add_post("/api/graph/init/{book_id}", handle_graph_init)
    app.router.add_get("/api/graph/progress/{book_id}", handle_graph_progress)
    app.router.add_get("/api/graph/stats/{book_id}", handle_graph_stats)
    app.router.add_get("/api/graph/nodes/{book_id}", handle_graph_nodes)
    app.router.add_get("/api/graph/{book_id}/characters", handle_graph_characters)
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

    # ── Console (provider/slot management) ───────────────────────────
    app.router.add_get("/api/console/config", handle_console_config)
    app.router.add_post("/api/console/providers", handle_provider_save)
    app.router.add_delete("/api/console/providers/{provider_id}", handle_provider_delete)
    app.router.add_post("/api/console/providers/{provider_id}/test", handle_provider_test)
    app.router.add_post("/api/console/models/fetch", handle_models_fetch)
    app.router.add_put("/api/console/slots/{slot}", handle_slot_switch)

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


async def handle_chat(request: web.Request) -> web.StreamResponse:
    """Handle a chat message with optional reading context + selection.

    POST body: {message, book_id, position: {chapter, chunk}, selection}

    Returns an SSE stream of the agent's response tokens.
    """
    db = request.app["db"]
    runtime = request.app.get("runtime")
    api_client = request.app.get("api_client")

    if not api_client:
        return web.json_response(
            {"error": "API client not available"}, status=503
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    message = body.get("message", "").strip()
    if not message:
        return web.json_response({"error": "empty message"}, status=400)

    book_id = body.get("book_id", "")
    # Reset book context; will be set to stable_bid below if a book is open
    if runtime:
        runtime.current_book_id = ""
    position = body.get("position", {})
    selection = body.get("selection", "").strip()
    selection_source = body.get("selection_source", "book")  # "book" | "chat"
    history = body.get("history", [])  # recent conversation messages
    mode = body.get("mode", "immersive")  # immersive | overview | divergent | roleplay
    character_name = body.get("character_name", "")  # role-play: character to embody
    max_chapter = body.get("max_chapter")  # role-play: None=global, int=spoiler-free
    session_id = body.get("session_id", "")  # optional: persist to this session
    stream_version = body.get("stream", "v1")  # "v1" | "v2" — v2 = JSON-structured events
    if stream_version not in ("v1", "v2"):
        # An unrecognised value used to fall through to the v1 comparisons
        # (``== "v2"`` is False) and silently emit plain-text frames, which
        # strips reasoning and breaks the think row. Reject loudly instead.
        return web.json_response(
            {"error": f"unsupported stream version: {stream_version!r}"}, status=400
        )
    voice = body.get("voice")  # optional voice metadata for voice_input part

    session_service = request.app.get("session_service")

    # ── Wrap main logic to surface tracebacks to browser ─────────
    import traceback as _traceback
    import logging as _logging
    try:
        return await _handle_chat_impl(
            request, db, runtime, api_client, book_id, position,
            selection, selection_source, history, mode,
            character_name, max_chapter, session_id,
            session_service, message, stream_version, voice,
        )
    except Exception:
        _logging.getLogger("api").error(
            "handle_chat crashed:\n%s", _traceback.format_exc()
        )
        return web.json_response(
            {"error": f"Internal error: {_traceback.format_exc()}"},
            status=500,
        )


async def _handle_chat_impl(
    request, db, runtime, api_client, book_id, position,
    selection, selection_source, history, mode,
    character_name, max_chapter, session_id,
    session_service, message, stream_version="v1", voice=None,
) -> web.StreamResponse:
    import logging

    # ── Session persistence: create/append user message (EARLY) ─────────
    # Persist user message BEFORE context building so it survives any
    # downstream failure (500 error, LLM timeout, etc). This ensures the
    # user's input is always recorded, even when the agent never responds.
    persisted_session_id = ""
    title = message[:50].replace("\n", " ").strip()
    if session_service and session_id:
        try:
            from content import ConversationMessage as _CM, ContentBlock as _CB
            blocks = []
            if voice and isinstance(voice, dict):
                blocks.append(_CB(type="voice_input", text=json.dumps(voice, ensure_ascii=False)))
            blocks.append(_CB(type="text", text=message))
            user_msg = _CM(role="user", blocks=blocks)
            session_service.append_message(session_id, user_msg)
            info = session_service.get(session_id)
            if info and not info.title:
                session_service.update_title(session_id, title)
            if book_id and (not info or not info.project_id):
                session_service._db.update_session(
                    session_id, project_id=book_id,
                )
            if mode == "roleplay" and character_name:
                session_service._db.update_session(
                    session_id, character_name=character_name,
                )
            persisted_session_id = session_id
        except Exception as exc:
            logging.getLogger("api").warning(f"Session user-msg persist failed: {exc}")
    elif session_service and not session_id:
        try:
            from session import Session as _SessionModel
            new_session = _SessionModel()
            new_session.mode = mode
            from content import ConversationMessage as _CM, ContentBlock as _CB
            blocks = []
            if voice and isinstance(voice, dict):
                blocks.append(_CB(type="voice_input", text=json.dumps(voice, ensure_ascii=False)))
            blocks.append(_CB(type="text", text=message))
            user_msg = _CM(role="user", blocks=blocks)
            session_service.append_message(
                new_session.session_id, user_msg,
                mode=mode, title=title,
                project_id=book_id or "",
                character_name=character_name if mode == "roleplay" else None,
            )
            persisted_session_id = new_session.session_id
        except Exception as exc:
            logging.getLogger("api").warning(f"Session create failed: {exc}")

    # ── Build context block ──────────────────────────────────────
    context_parts: list[str] = []

    # Home page context — injected when no book is open, unloaded when a book opens
    if not book_id:
        try:
            all_books = db.list_books()
            if all_books:
                book_lines = "\n".join(
                    f"  - 《{b.title}》({'作者: ' + b.author if b.author else '未知作者'})"
                    for b in all_books[:20]
                )
                home_ctx = (
                    "<home-context>\n"
                    "  当前位置: 主页\n"
                    "  用户既可能想在此页闲聊，也可能需要导航帮助。\n"
                    "  在用户未表达阅读意愿前，默认用户只是想在此页聊天。\n"
                    f"  已导入书籍 ({len(all_books)} 本):\n{book_lines}\n"
                    "</home-context>"
                )
                context_parts.append(home_ctx)
        except Exception:
            pass

    # Reading position
    if book_id:
        book = db.get_book(book_id)
        if book:
            ch_idx = position.get("chapter", 0)
            ch = db.get_chapter(book_id, ch_idx)
            ck_idx = position.get("chunk", 0)
            total = db.get_chunk_count(book_id, ch_idx)

            ctx = (
                f"<reading-context>\n"
                f"  书名: 《{book.title}》\n"
                f"  作者: {book.author or '未知'}\n"
            )
            if ch:
                ctx += f"  章节: {ch.title} ({_type_label(ch.chapter_type)})\n"
            ctx += f"  位置: 第 {ck_idx + 1}/{total} 块\n"
            ctx += "</reading-context>"
            context_parts.append(ctx)

    # Selection (book or chat)
    if selection:
        if selection_source == "chat":
            context_parts.append(
                f"<user-selection source=\"书伴的回复\">\n"
                f"  用户引用书伴说过的话: \"{selection[:200]}"
                + ("…" if len(selection) > 200 else "") + "\"\n"
                f"  (用户对书伴的这段回复有疑问，请解释或展开)\n"
                f"</user-selection>"
            )
        else:
            context_parts.append(
                f"<user-selection source=\"书中原文\">\n"
                f"  用户选中: \"{selection[:200]}"
                + ("…" if len(selection) > 200 else "") + "\"\n"
                f"</user-selection>"
            )

    # ── Enriched context: current passage, annotations, memory ──────
    if book_id:
        ch_idx = position.get("chapter", 0)
        ck_idx = position.get("chunk", 0)

        # 2.3.1 Current passage (truncated to 2000 chars)
        ck = db.get_chunk(book_id, ch_idx, ck_idx)
        if ck:
            passage = ck.content[:2000]
            if len(ck.content) > 2000:
                passage += "\n...（段落较长，已截断，可通过 library_search 查阅完整内容）"
            context_parts.append(
                f"<current-passage>\n{passage}\n</current-passage>"
            )

        # 2.5.2 L0 article-index + 2.3.2 L1 user-annotations
        # stable_bid 必须用 import_log 的 stable id（BookRow 无此字段）；
        # 直接退回 book_id 会导致 annotations（stable_book_id=sb-*）查空
        stable_bid = book_id
        try:
            _slog = db.get_import_log_by_book(book_id)
            if _slog and _slog.stable_book_id:
                stable_bid = _slog.stable_book_id
        except Exception:
            pass
        # Stamp book context on runtime for graph evolution (compact-time extraction)
        # graph.db uses book_id as project_id (not stable_book_id)
        if runtime:
            runtime.current_book_id = book_id
            _cfg = request.app.get("config")
            _gp = _resolve_graph_path(_cfg, db, book_id) if _cfg and db else None
            if _gp:
                runtime.current_graph_path = str(_gp)
        anns = db.get_annotations_by_stable_book(stable_bid, limit=200)
        if anns:
            notes_n = sum(1 for a in anns if a.type in ("note", "both"))
            bm_n = sum(1 for a in anns if a.type in ("bookmark", "both"))
            latest = anns[-1]
            latest_snip = (latest.note_text or latest.selected_text or "")[:20]
            context_parts.append(
                f"<article-index>\n"
                f"  本书笔记: {notes_n}条 | 书签: {bm_n}个"
                f" | 最近: [{latest.id}] {latest_snip}\n"
                f"</article-index>"
            )
            # L1: current chapter annotations only
            chapter_anns = [a for a in anns if a.chapter_index == ch_idx]
            if chapter_anns:
                notes_ctx = "<user-annotations>\n"
                for a in chapter_anns:
                    tag = "笔记" if a.type in ("note", "both") else "书签"
                    snippet = (a.note_text or a.selected_text or "")[:200]
                    notes_ctx += (
                        f"  [{a.id}] {tag} | {snippet}\n"
                        f"    章节位置: ch.{a.chapter_index} ck.{a.chunk_index}\n"
                    )
                notes_ctx += "</user-annotations>"
                context_parts.append(notes_ctx)

    # 2.3.3 Memory snapshot (global memory + user profile)
    if runtime and hasattr(runtime, "memory_store"):
        mem_block = runtime.memory_store.freeze_for_system_prompt("memory")
        if mem_block:
            context_parts.append(
                f"<global-memory>\n{mem_block}\n</global-memory>"
            )
        persona_block = runtime.memory_store.freeze_for_system_prompt("persona")
        if persona_block:
            context_parts.append(
                f"<persona-observation>\n{persona_block}\n</persona-observation>"
            )
        user_block = runtime.memory_store.freeze_for_system_prompt("user")
        if user_block:
            context_parts.append(
                f"<user-profile>\n{user_block}\n</user-profile>"
            )

    # ── 统一记忆召回编排（腾讯编排表方案） ──────────────────────────
    # 声明式通道表见 recall_orchestrator.py：L1 原子记忆 / 用户收藏 / 用户笔记 /
    # 图谱观察（原内联 <graph-observations> 收编于此）。单通道失败不阻塞其他；
    # 图谱通道要求 current_book_id（非 roleplay；roleplay 用 format_role_kernel）。
    if runtime and message and mode != "roleplay":
        try:
            from recall_orchestrator import fetch_dynamic_channels
            _cfg3 = request.app.get("config")
            _gp3 = _resolve_graph_path(_cfg3, db, book_id) if (_cfg3 and db and book_id) else None
            _recall_ctx = {
                "runtime": runtime,
                "books_db": db,
                "graph_db_path": str(_gp3) if _gp3 else None,
                "max_chapter": max_chapter,
                "current_stable_book_id": locals().get("stable_bid"),
                "mode": mode,
            }
            for _pri, _tag, _content in fetch_dynamic_channels(message, ctx=_recall_ctx):
                context_parts.append(_content)
        except Exception:
            pass

    # Token budget: cap context_parts at 6000 chars, drop lowest priority first
    # Priority low → high: user-profile, global-memory, user-selection,
    #   reading-context, relevant-memories, user-notes, user-highlights,
    #   graph-observations, article-index, user-annotations, current-passage
    MAX_CTX_CHARS = 6000
    _drop_order = [
        "<user-profile>", "<global-memory>",
        "<user-selection", "<reading-context>",
        "<relevant-memories>", "<user-notes>", "<user-highlights>",
        "<graph-observations>", "<article-index>",
        "<user-annotations>", "<current-passage>",
    ]
    for _tag in _drop_order:
        if sum(len(p) for p in context_parts) <= MAX_CTX_CHARS:
            break
        for _i, _p in enumerate(context_parts):
            if _tag in _p:
                context_parts.pop(_i)
                break

    # ── Role-play mode: strip companion-reading context ──────────────
    # In role-play the agent IS the character — not a reading companion.
    # Reading position, annotations, memory, and home-context are removed.
    # Environment info is accessed via tools, not injected as context.
    if mode == "roleplay":
        context_parts.clear()
        # Reframe user-selection — "the reader showed you", not "the user
        # selected text for explanation"
        if selection:
            if selection_source == "chat":
                context_parts.append(
                    f"<user-selection source=\"对话引用\">\n"
                    f"  读者引用了你之前说过的话: \"{selection[:200]}"
                    + ("…" if len(selection) > 200 else "") + "\"\n"
                    f"</user-selection>"
                )
            else:
                context_parts.append(
                    f"<user-selection source=\"书中原文\">\n"
                    f"  读者向你展示了这段文字: \"{selection[:200]}"
                    + ("…" if len(selection) > 200 else "") + "\"\n"
                    f"</user-selection>"
                )

    # ── Build system prompt ──────────────────────────────────────
    # ── Role-play: build character kernel from graph (pure function, no LLM) ──
    # The kernel (identity + minimal extension + knowledge boundary) is
    # formatted directly from get_node_detail - no LLM pre-translation,
    # no frozen cache. Deep detail is fetched on-demand via
    # graph_context / graph_quotes / graph_trace during the conversation.
    role_card = ""
    config = request.app.get("config")
    if mode == "roleplay" and character_name and book_id:
        if config:
            from pathlib import Path
            from storage.graph_db import GraphDB, resolve_graph_db_path
            from bookgraph.formatter import format_role_kernel

            graph_path = resolve_graph_db_path(config, db, book_id) if db else config.graph_db_path(book_id)
            if graph_path.exists():
                gdb = GraphDB(graph_path)
                try:
                    detail = gdb.get_node_detail(book_id, character_name, max_chapter=max_chapter)
                    if not detail.get("error"):
                        role_card = format_role_kernel(detail, max_chapter=max_chapter)
                finally:
                    gdb.close()
            else:
                _log = logging.getLogger("api")
                _log.warning("role_card: no graph at %s", graph_path)

    # ── Populate per-request context for role-play tools ────────────
    # Populate reading context for ALL modes - graph_search / graph_node /
    # current_passage read from here. Role-play additionally uses
    # character_name (stamped on the session elsewhere).
    if runtime and hasattr(runtime, "tool_registry"):
        from storage.graph_db import resolve_graph_db_path
        r_ctx = runtime.tool_registry.reading_ctx
        r_ctx.clear()
        r_ctx.update({
            "book_id": book_id,
            "chapter_index": position.get("chapter", 0),
            "chunk_index": position.get("chunk", 0),
            "max_chapter": max_chapter,
            "graph_db_path": str(resolve_graph_db_path(config, db, book_id)) if config and db else "",
            "db": db,
            "config": config,
        })

    system_prompt = _build_reading_prompt(runtime, mode, role_card)

    # ── Build messages with conversation history ─────────────────
    user_content = ""
    _injected_text = ""
    if context_parts:
        _injected_text = "\n\n".join(context_parts) + "\n\n"
        user_content += _injected_text
    user_content += message

    api_messages: list[dict] = []
    # Prepend recent history for conversational continuity
    if history:
        for h in history[-6:]:  # last 3 turns (user+agent pairs)
            role = "assistant" if h.get("role") == "agent" else "user"
            api_messages.append({"role": role, "content": h.get("content", "")[:1500]})
    _hist_len = len(api_messages)  # history-only boundary for context meter
    api_messages.append({"role": "user", "content": user_content})


    # ── ReAct tool loop (max 5 rounds) ───────────────────────────
    # Each round: stream LLM → if tool_calls, execute them → feed results
    # back → loop.  Without this, tools are schema-only decoration.
    _recorder = request.app.get("context_recorder")
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "X-Session-Id",
            "X-Session-Id": persisted_session_id or "",
        },
    )
    MAX_REACT_ROUNDS = 5
    react_round = 0
    full_text = ""
    last_turn_usage = None  # final round's TokenUsage — context meter input

    # ── Agent reply persistence ─────────────────────────────────
    # Persisted the moment streaming ends — normally or interrupted —
    # so a dead SSE connection (browser gave up / machine slept) can
    # never take the reply's only way to disk down with it.
    _agent_persisted = False

    def _persist_agent_reply() -> None:
        nonlocal _agent_persisted
        if _agent_persisted or not (
            session_service and persisted_session_id and full_text
        ):
            return
        _agent_persisted = True
        try:
            from content import ConversationMessage as _CM, ContentBlock as _CB
            # Separate <think>...</think> from visible text so the
            # frontend can render expandable thinking rows on session
            # reload, matching the SSE v2 streaming experience.
            blocks: list = []
            _think_pat = re.compile(r"<think>(.*?)</think>", re.DOTALL)
            _last = 0
            for _m in _think_pat.finditer(full_text):
                if _m.start() > _last:
                    blocks.append(_CB(type="text", text=full_text[_last:_m.start()]))
                _think_content = _m.group(1).strip()
                if _think_content:
                    blocks.append(_CB(type="thinking", thinking=_think_content))
                _last = _m.end()
            if _last < len(full_text):
                blocks.append(_CB(type="text", text=full_text[_last:]))
            if not blocks:
                blocks.append(_CB(type="text", text=full_text))
            agent_msg = _CM(role="assistant", blocks=blocks, usage=last_turn_usage)
            session_service.append_message(persisted_session_id, agent_msg)
        except Exception as exc:
            logging.getLogger("api").warning(f"Session agent-msg persist failed: {exc}")
    while react_round < MAX_REACT_ROUNDS:
        react_round += 1

        # Refresh tool schema each round (toolset visibility may change)
        tools_schema = None
        if runtime and hasattr(runtime, "tool_registry"):
            tools_schema = runtime.tool_registry.get_schemas()

        # ── Save input for flight recorder (recorded after output) ──
        _rec_input = {
            "system_prompt": system_prompt,
            "messages": list(api_messages),
            "tools_count": len(tools_schema) if tools_schema else 0,
        } if _recorder else None

        if react_round == 1:
            try:
                await resp.prepare(request)
            except Exception as exc:
                import logging
                logging.getLogger("api").warning(f"SSE prepare failed: {exc}")
                return resp

        round_text = ""
        tool_calls_at_end: list[dict] = []
        think_parser = _ThinkParser() if stream_version == "v2" else None
        try:
            async for text_chunk, accum_tools, _round_usage in api_client.stream(
                system_prompt, api_messages, tools_schema, None,
            ):
                if text_chunk:
                    round_text += text_chunk
                    if stream_version == "v2":
                        for evt in think_parser.feed(text_chunk):
                            await _sse_write_json(resp, evt)
                    else:
                        await resp.write(f"data: {text_chunk}\n\n".encode("utf-8"))
                tool_calls_at_end = accum_tools
                if _round_usage:
                    last_turn_usage = _round_usage
        except Exception as exc:
            logging.getLogger("api").warning(f"SSE stream error: {exc}")
            # break skips the full_text accumulation below and every
            # subsequent SSE write targets a connection that may already
            # be gone — persist what arrived before attempting any of it.
            full_text += round_text
            _persist_agent_reply()
            try:
                if stream_version == "v2":
                    await _sse_write_json(resp, {"type": "error", "message": f"回答中断: {exc}"})
                else:
                    await resp.write(f"data: [回答中断: {exc}]\n\n".encode("utf-8"))
            except Exception:
                pass
            break

        # Flush remaining think-parser buffer — the SSE connection may
        # already be dead; a failed write here must not kill the handler
        # before the reply is persisted.
        if think_parser:
            try:
                for evt in think_parser.flush():
                    await _sse_write_json(resp, evt)
            except Exception:
                pass

        full_text += round_text

        # ── No tool calls → LLM is done ──────────────────────────────
        if not tool_calls_at_end:
            # ── Flight recorder: final round (no tools) ──
            if _recorder and _rec_input:
                _recorder.record_round(
                    session_id=persisted_session_id or "",
                    turn_index=1,
                    react_round=react_round,
                    mode=mode,
                    book_id=book_id,
                    response_text=round_text,
                    tool_calls=None,
                    tool_results=None,
                    **_rec_input,
                )
            break

        # ── Execute tools ────────────────────────────────────────────
        _tool_results: list[dict] = []
        api_messages.append({
            "role": "assistant",
            "content": round_text or None,
            "tool_calls": [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls_at_end
            ],
        })
        for tc in tool_calls_at_end:
            tool_name = tc["name"]
            tool_input_str = tc.get("arguments", "{}")
            if stream_version == "v2":
                try:
                    await _sse_write_json(resp, {"type": "tool_start", "name": tool_name})
                except Exception:
                    pass
            else:
                try:
                    await resp.write(
                        f"data: [🔧 {tool_name}...]\n\n".encode("utf-8")
                    )
                except Exception:
                    pass
            try:
                if runtime and hasattr(runtime, "tool_registry"):
                    result = await runtime.tool_registry.execute_async(tool_name, tool_input_str)
                else:
                    result = json.dumps({"error": f"Tool not found: {tool_name}"})
            except Exception as _exc:
                result = json.dumps({"error": f"Tool error: {_exc}"})
            api_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
            if _recorder:
                _tool_results.append({
                    "name": tool_name,
                    "arguments": tool_input_str,
                    "result": result,
                })
            if stream_version == "v2":
                # Truncate long tool results for the event
                result_preview = result[:200] + ("…" if len(result) > 200 else "")
                try:
                    await _sse_write_json(resp, {"type": "tool_end", "name": tool_name, "preview": result_preview})
                except Exception:
                    pass

        # ── Flight recorder: round with tool calls ──────────────
        if _recorder and _rec_input:
            _recorder.record_round(
                session_id=persisted_session_id or "",
                turn_index=1,
                react_round=react_round,
                mode=mode,
                book_id=book_id,
                response_text=round_text,
                tool_calls=tool_calls_at_end,
                tool_results=_tool_results,
                **_rec_input,
            )

    # ── Context meter: turn-end snapshot (ring data source) ──────
    # Real totals from the final round's usage; blocks aligned proportionally.
    usage_payload = None
    if runtime is not None:
        import asyncio as _asyncio
        import logging as _ctxlog
        try:
            from context_meter import format_bill, measure, resolve_profile
            _model = getattr(config, "model", "")
            _prof = resolve_profile(_model, config)
            _alert = getattr(config, "context_alert_ratio", 0.35)
            _snapshot = await _asyncio.to_thread(
                measure,
                system_prompt=system_prompt,
                tools_schema=tools_schema or [],
                history_messages=api_messages[:_hist_len],
                injected_context=_injected_text,
                user_input=message,
                usage=last_turn_usage,
                window=_prof["window"],
                model=_model,
                tokenizer_path=_prof["tokenizer_path"],
                session_id=persisted_session_id or "",
            )
            runtime.last_context_usage = _snapshot
            runtime.last_context_usage_session = persisted_session_id or ""
            _ctxlog.getLogger("context").debug(
                format_bill(_snapshot, alert_ratio=_alert))
            usage_payload = _snapshot.to_dict(alert_ratio=_alert)
        except Exception:
            _ctxlog.getLogger("api").warning(
                "context meter failed", exc_info=True)

    try:
        if stream_version == "v2":
            _done_evt: dict = {"type": "done", "session_id": persisted_session_id or ""}
            if usage_payload:
                _done_evt["usage"] = usage_payload
            await _sse_write_json(resp, _done_evt)
        else:
            await resp.write(b"data: [DONE]\n\n")
    except Exception:
        pass

    # ── Persist agent reply to session ──────────────────────────
    # Interrupted streams persisted their partial text inside the
    # stream-except handler; the flag inside _persist_agent_reply
    # dedupes the two paths.
    _persist_agent_reply()

    # ── Memory sync: feed completed turn to the memory pipeline ──────
    # Web chat reimplements the ReAct loop directly (api_client.stream),
    # bypassing runtime.run_turn() where the memory sync normally lives.
    # Call it here so the L0-L3 pipeline sees every completed turn.
    if runtime and getattr(runtime, "memory_manager", None) and full_text:
        try:
            # Strip think tags so L1 extracts from visible replies only
            _clean_reply = re.sub(
                r" thinking.*? response", "", full_text, flags=re.DOTALL
            ).strip()
            runtime.memory_manager.sync_all(
                message, _clean_reply,
                session_id=persisted_session_id or "",
            )
            runtime.memory_manager.queue_prefetch_all(
                message,
                session_id=persisted_session_id or "",
            )
        except Exception:
            pass

    # Graph evolution: background extraction from this dialogue turn
    if runtime and getattr(runtime, 'current_book_id', '') and full_text:
        _stable = runtime.current_book_id
        _dlg_parts = []
        for h in (history or [])[-3:]:
            _role = h.get("role", "")
            _content = h.get("content", "")
            if _role == "user" and _content:
                _dlg_parts.append(f"读者: {_content}")
            elif _role == "assistant" and _content:
                _dlg_parts.append(f"伴读助手: {_content}")
        _dlg_parts.append(f"读者: {message}")
        _dlg_parts.append(f"伴读助手: {full_text}")
        _dlg_text = "\n\n".join(_dlg_parts)
        import asyncio as _aio
        _aio.create_task(_bg_graph_extract(
            _stable, _dlg_text, api_client,
            request.app.get("config"), db, book_id,
        ))

    await resp.write_eof()
    return resp


# ── SSE v2 helpers ──────────────────────────────────────────────────────────


def _partial_tag_suffix(buf: str, tag: str) -> int:
    """Length of the longest suffix of *buf* that is a proper prefix of *tag*.

    ``<think>`` / ``</think>`` can be split across chunk boundaries, so we
    hold back at most ``len(tag) - 1`` trailing chars that might still grow
    into the real tag.  Everything before that is safe to stream out now.
    """
    for n in range(min(len(buf), len(tag) - 1), 0, -1):
        if buf.endswith(tag[:n]):
            return n
    return 0


class _ThinkParser:
    """State machine that splits LLM text chunks into structured SSE v2 events.

    runtime.py wraps DeepSeek reasoning in ``<think>...</think>`` tags.
    This parser consumes chunks and emits typed events:
      - ``{"type":"think_start"}``
      - ``{"type":"think","content":"..."}``
      - ``{"type":"think_end"}``
      - ``{"type":"text","content":"..."}``

    Streaming contract (enforced structurally, not by convention)
    ------------------------------------------------------------
    The whole point of this class is that reasoning must reach the browser
    *while the model is still thinking*.  A parser that waits for the
    closing ``</think>`` before emitting turns a 20-second reasoning phase
    into 20 seconds of silence followed by one giant blob — which is
    indistinguishable from "no streaming at all" to the reader.

    So the invariant here is: ``feed()`` emits every byte it can prove is
    not part of a tag, in the same call.  Only the trailing chars that
    could still grow into ``<think>``/``</think>`` are retained, and
    ``_pending`` is therefore bounded by ``len(tag) - 1`` (6 / 7 chars).
    There is no unbounded accumulation anywhere in this class, so the old
    "buffer the entire reasoning block" bug cannot be reintroduced by a
    future edit that forgets the contract.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self):
        self._state = "normal"       # "normal" | "in_think"
        # Only ever holds ≤ len(tag)-1 chars (a possible partial tag).
        self._pending = ""

    def feed(self, chunk: str) -> list[dict]:
        """Feed a text chunk; return SSE v2 events emitted *now*."""
        events: list[dict] = []
        data = self._pending + chunk
        self._pending = ""

        while data:
            if self._state == "normal":
                idx = data.find(self._OPEN)
                if idx == -1:
                    # No opening tag in flight: release everything except a
                    # possible partial "<think>" tail.  Holding back more
                    # than that would stall visible text for no reason.
                    hold = _partial_tag_suffix(data, self._OPEN)
                    emit = data[: len(data) - hold]
                    if emit:
                        events.append({"type": "text", "content": emit})
                    data = data[len(data) - hold:]
                    if data:                       # partial tag → carry over
                        self._pending = data
                    break
                # Text preceding the tag is visible output.
                if idx > 0:
                    events.append({"type": "text", "content": data[:idx]})
                data = data[idx + len(self._OPEN):]
                self._state = "in_think"
                events.append({"type": "think_start"})

            else:  # in_think
                idx = data.find(self._CLOSE)
                if idx == -1:
                    # Closing tag not here yet — stream the reasoning out
                    # immediately and keep only a possible partial tail.
                    hold = _partial_tag_suffix(data, self._CLOSE)
                    emit = data[: len(data) - hold]
                    if emit:
                        events.append({"type": "think", "content": emit})
                    data = data[len(data) - hold:]
                    if data:
                        self._pending = data
                    break
                if idx > 0:
                    events.append({"type": "think", "content": data[:idx]})
                data = data[idx + len(self._CLOSE):]
                self._state = "normal"
                events.append({"type": "think_end"})

        return events

    def flush(self) -> list[dict]:
        """Flush the held tail (called when the underlying stream ends)."""
        events: list[dict] = []
        if self._pending:
            if self._state == "in_think":
                # Unclosed think — surface the reasoning, then close the row
                # so the frontend label never sticks on "思考中…".
                events.append({"type": "think", "content": self._pending})
                events.append({"type": "think_end"})
            else:
                events.append({"type": "text", "content": self._pending})
            self._pending = ""
        self._state = "normal"
        return events

    @property
    def pending_chars(self) -> int:
        """Diagnostics: how many chars are held back awaiting a tag match."""
        return len(self._pending)


async def _sse_write_json(resp: web.StreamResponse, event: dict) -> None:
    """Write a JSON-structured SSE v2 event line."""
    await resp.write(
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
    )


_ROLEPLAY_DIRECTIVES = """## 扮演规则
1. 始终以第一人称回应--你就是这个角色。
2. 用角色的口吻和知识边界说话--不知道的事就说不知道。
3. 如果触及角色的痛苦记忆或敏感话题，可以表现出回避、沉默或转移话题。
4. 保持角色的一致性和深度，不要突然变成"AI 助手"。
5. 恪守知识边界：沉浸模式下，读者未提及的后续情节你尚不知晓，不要主动透露。"""


def _build_reading_prompt(runtime, mode: str = "immersive", role_card: str = "") -> str:
    """Build the reading-companion system prompt.

    Structure:
      [STATIC] SOUL persona (role-play: boundary-only) + mode overview
               + tool guide + platform hint (companion modes only)
      ─── DYNAMIC BOUNDARY ───
      [DYNAMIC] mode prompt  OR  role card

    In role-play mode (role_card non-empty):
      SOUL identity replaced by role card. Only SOUL's "## 边界" section
      is kept as a safety floor beneath the role card.
    """
    from persona import load_persona

    parts: list[str] = []
    is_role_play = bool(role_card)

    if is_role_play:
        # Role-play: boundary-only safety floor + reader card + role card
        soul_full = ""
        if runtime and hasattr(runtime, "config"):
            soul_full = load_persona(runtime.config)
        boundary = _extract_soul_boundary(soul_full)
        if boundary:
            parts.append(boundary)
        else:
            parts.append(
                "## 边界\n- 我是阅读伴侣，不是心理医生。如用户表现出需要帮助的信号，温和建议专业支持。\n"
                "- 我不碰用户的代码、文件、系统。"
            )

        # ── Reader card placeholder (dual-card model) ──────────────
        parts.append(_ROLEPLAY_DIRECTIVES)
        parts.append(role_card)

        parts.append(
            "# 读者卡\n"
            "读者画像待生成（UserGraph 尚未实现）。\n"
            "暂无读者偏好、阅读习惯、历史对话摘要等信息。"
        )
    else:
        # Normal mode: full SOUL
        if runtime and hasattr(runtime, "config"):
            parts.append(load_persona(runtime.config))
        else:
            parts.append(
                "你是「书伴」，一个阅读陪伴助手。你陪用户一起读书，像朋友一样聊书中的人和事。"
            )

    # Interaction-mode overview (companion modes only — role-play is a persona)
    if not is_role_play and _WEB_MODE_OVERVIEW:
        parts.append(_WEB_MODE_OVERVIEW)

    # Tool guide — role-play tools vs companion tools
    if is_role_play:
        parts.append(
            "## 工具使用\n"
            "- graph_context(question): 用自然语言提问查图谱（首选，一次返回入口+关系+引文）\n"
            "- graph_search(query): 搜索书中实体（人物/事件/地点/概念）\n"
            "- graph_node(name): 查实体详情（身份/关系/台词/出场）\n"
            "- graph_quotes(character): 回忆某个角色的经典台词\n"
            "- current_passage(): 获取读者当前正在看的段落原文"
        )
    else:
        parts.append(
            "## 工具使用\n"
            "- graph_context(question): 用自然语言提问查图谱（首选）\n"
            "- graph_search(query): 搜索书中实体（人物/事件/地点/概念）\n"
            "- graph_node(name): 查实体详情（身份/关系/台词/出场）\n"
            "- library_search(query): 全书库全文搜索，返回匹配段落。需要查书中其他位置时用"
            "\n\n"
            "## 记忆工具调用指南\n"
            "- memory_search(query): 搜索持久化记忆库（用户偏好/历史事件/规则），返回预览与关键词\n"
            "- memory_list_by_tag(tag): 按标签浏览记忆\n"
            "- annotation_search(query): 跨书搜索读者收藏的高亮与笔记\n"
            "⚠️ 调用限额：memory_search 与 annotation_search 每轮合计最多调用 3 次。"
            "首次无结果可换关键词重试，但总次数不超过 3 次；仍无结果说明信息不在记忆中，"
            "直接按已有信息回复，不要继续搜索。"
        )

    # Platform hint + deferred-tool discoverability (companion modes only;
    # role-play is a character, not the reading companion)
    if not is_role_play:
        try:
            from runtime import _PLATFORM_HINT
            if _PLATFORM_HINT:
                parts.append(_PLATFORM_HINT)
        except Exception:
            pass
        if runtime and hasattr(runtime, "tool_registry"):
            try:
                deferred_count = len(runtime.tool_registry.get_deferred_specs())
            except Exception:
                deferred_count = 0
            if deferred_count > 0:
                parts.append(
                    f"{deferred_count} 个扩展工具已隐藏以节省上下文。"
                    "若上方工具不满足需求，可调用 `tool_search` 用关键词发现它们。"
                )

    # Voice guides (role-play only - design spec + tag reference)
    if is_role_play:
        from voices import VOICE_DESIGN_GUIDE, VOICE_TAG_GUIDE
        parts.append(VOICE_DESIGN_GUIDE)
        parts.append(VOICE_TAG_GUIDE)

    # DYNAMIC BOUNDARY
    parts.append("\n─── 以上为稳定身份层，以下为当前会话模式 ───\n")

    if not is_role_play:
        # Role-play's kernel + directives are in the static region;
        # dynamic region carries only the mode prompt for non-role-play.
        try:
            from runtime import _get_mode_prompt
            mode_prompt = _get_mode_prompt(mode)
            if mode_prompt:
                parts.append(mode_prompt)
        except Exception:
            pass

    return "\n\n".join(parts)


# Web 端三模式总览（精简版，无 CLI 命令前缀）—— 与 runtime.MODE_L0_OVERVIEW 平行。
# 模块加载时读一次（对齐 runtime.py 的模块级常量模式）。
_WEB_MODE_OVERVIEW_PATH = Path(__file__).parent / "prompts" / "modes" / "l0_overview.web.md"
_WEB_MODE_OVERVIEW = (
    _WEB_MODE_OVERVIEW_PATH.read_text(encoding="utf-8").strip()
    if _WEB_MODE_OVERVIEW_PATH.exists()
    else ""
)


def _extract_soul_boundary(soul_text: str) -> str:
    """Extract just the '## 边界' section from SOUL.md text."""
    if not soul_text:
        return ""
    lines = soul_text.split("\n")
    in_boundary = False
    out: list[str] = []
    for line in lines:
        if line.strip().startswith("## 边界"):
            in_boundary = True
            out.append(line)
            continue
        if in_boundary:
            if line.strip().startswith("## ") and not line.strip().startswith("## 边界"):
                break
            out.append(line)
    return "\n".join(out).strip()# ── Session (chat history) handlers ────────────────────────────────


async def handle_sessions_list(request: web.Request) -> web.Response:
    """List all chat sessions (metadata only).

    Query params:
        limit          — max sessions to return (default 100)
        search         — optional title keyword filter
        project_id     — filter by book
        character_name — filter by role-play character
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    limit = min(max(int(request.query.get("limit", "100")), 1), 500)
    search = request.query.get("search") or None
    project_id = request.query.get("project_id") or None
    character_name = request.query.get("character_name") or None
    root_only = request.query.get("root_only", "").lower() in ("1", "true", "yes")

    sessions = session_service.list(
        limit=limit, search=search, project_id=project_id,
        character_name=character_name, root_only=root_only,
    )

    # Build book-title lookup map for labeling sessions
    book_titles: dict[str, str] = {}
    try:
        db = request.app.get("db")
        if db:
            for b in db.list_books():
                book_titles[b.id] = b.title
    except Exception:
        pass

    # Batch query children counts (for tree expand indicators)
    children_counts = session_service.count_children_batch([s.id for s in sessions])

    items = []
    for s in sessions:
        pid = s.project_id or ""
        items.append({
            "id": s.id,
            "project_id": pid,
            "book_title": book_titles.get(pid, "") if pid else "",
            "title": s.title or "(无标题)",
            "mode": s.mode,
            "message_count": s.message_count,
            "time_created": s.time_created,
            "time_updated": s.time_updated,
            "character_name": s.character_name,
            "children_count": children_counts.get(s.id, 0),
        })
    return web.json_response({"sessions": items, "count": len(items)})


async def handle_session_load(request: web.Request) -> web.Response:
    """Load a full session with all messages.

    Returns: {id, title, mode, messages: [{role, content}]}
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    info = session_service.get(session_id)
    if not info:
        return web.json_response({"error": "session not found"}, status=404)

    session = session_service.load_messages(session_id)
    if not session:
        return web.json_response({"error": "failed to load messages"}, status=500)

    messages = []
    for m in session.messages:
        parts = []
        content = ""
        thinking = ""
        for b in m.blocks:
            if b.type == "text" and b.text:
                content += b.text
            if b.type == "thinking" and b.thinking:
                thinking += b.thinking
            if b.type in ("voice_input", "voice_output") and b.text:
                try:
                    parts.append({"type": b.type, "data": json.loads(b.text)})
                except json.JSONDecodeError:
                    pass  # malformed voice metadata, skip
            elif b.type == "tool_result" and b.text:
                # Check if it's a tts_speak result (has type:voice)
                try:
                    tr = json.loads(b.text)
                    if isinstance(tr, dict) and tr.get("type") == "voice":
                        parts.append({"type": "voice_output", "data": tr})
                except (json.JSONDecodeError, TypeError):
                    pass
        messages.append({
            "id": m.id,
            "role": m.role,
            "content": content,
            "thinking": thinking or None,  # omit if empty (for old sessions)
            "parts": parts if parts else None,  # omit if empty
            "time_created": m.time_created or 0,
        })

    return web.json_response({
        "id": info.id,
        "project_id": info.project_id,
        "title": info.title or "(无标题)",
        "mode": info.mode,
        "character_name": info.character_name,
        "message_count": info.message_count,
        "time_created": info.time_created,
        "time_updated": info.time_updated,
        "parent_id": info.parent_id,
        "fork_point_index": info.fork_point_index,
        "messages": messages,
    })


async def handle_session_delete(request: web.Request) -> web.Response:
    """Delete a session and all its messages."""
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    try:
        session_service.delete(session_id)
    except Exception as exc:
        return web.json_response({"error": f"delete failed: {exc}"}, status=500)
    return web.json_response({"deleted": session_id})


async def handle_session_set_title(request: web.Request) -> web.Response:
    """Manually set a session's title.

    POST body: {title: "..."}
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    title = (body.get("title") or "").strip()
    if not title:
        return web.json_response({"error": "title required"}, status=400)
    session_service.update_title(session_id, title[:200])
    return web.json_response({"id": session_id, "title": title[:200]})


async def handle_session_search(request: web.Request) -> web.Response:
    """Full-text search across session message content.

    Query params:
        q     — search query (required)
        limit — max results (default 10, max 50)
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    q = request.query.get("q", "").strip()
    if not q:
        return web.json_response({"error": "query required"}, status=400)
    limit = min(max(int(request.query.get("limit", "10")), 1), 50)

    results = session_service.search_content(q, limit=limit)
    return web.json_response({"results": results, "query": q})


async def handle_session_archive(request: web.Request) -> web.Response:
    """Archive a session (prune large tool outputs + mark archived)."""
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    try:
        session_service.archive(session_id)
    except Exception as exc:
        return web.json_response({"error": f"archive failed: {exc}"}, status=500)
    return web.json_response({"archived": session_id})


async def handle_session_fork(request: web.Request) -> web.Response:
    """Fork a session: deep-copy messages into a new session.

    The original session is preserved unchanged.
    POST body (optional):
        {branch_name, up_to_message_id, exclusive, fork_point_index}
    - exclusive=true: copy strictly before up_to_message_id (exclude it)
    - fork_point_index: stored on child for branch switcher rendering
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    branch_name = None
    up_to_message_id = None
    exclusive = False
    fork_point_index = None
    try:
        body = await request.json()
        branch_name = (body.get("branch_name") or "").strip() or None
        up_to_message_id = (body.get("up_to_message_id") or "").strip() or None
        exclusive = bool(body.get("exclusive", False))
        fpi = body.get("fork_point_index")
        if fpi is not None:
            fork_point_index = int(fpi)
    except Exception:
        pass  # body is optional

    child = session_service.fork(session_id, branch_name=branch_name,
                                 up_to_message_id=up_to_message_id,
                                 exclusive=exclusive,
                                 fork_point_index=fork_point_index)
    if not child:
        return web.json_response({"error": "session not found"}, status=404)

    return web.json_response({
        "id": child.session_id,
        "title": child.title or "(分叉)",
        "mode": child.mode,
        "message_count": len(child.messages),
        "time_created": child.created_at_ms,
        "parent_id": session_id,
    })


async def handle_session_branches(request: web.Request) -> web.Response:
    """Return branch info for the frontend switcher.

    Response: {current, parent, children} where parent/children carry
    fork_point_index so the client knows where to render the switcher.
    """
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    branches = session_service.get_branches(session_id)
    return web.json_response(branches)


async def handle_session_children(request: web.Request) -> web.Response:
    """List direct child sessions (forks) of a session."""
    session_service = request.app.get("session_service")
    if not session_service:
        return web.json_response({"error": "session service not available"}, status=503)

    session_id = request.match_info["session_id"]
    children = session_service.list_children(session_id)

    # Build book-title lookup for labeling
    book_titles: dict[str, str] = {}
    try:
        db = request.app.get("db")
        if db:
            for b in db.list_books():
                book_titles[b.id] = b.title
    except Exception:
        pass

    # Batch query children counts (for nested tree expand indicators)
    children_counts = session_service.count_children_batch([s.id for s in children])

    items = []
    for s in children:
        pid = s.project_id or ""
        items.append({
            "id": s.id,
            "project_id": pid,
            "book_title": book_titles.get(pid, "") if pid else "",
            "title": s.title or "(无标题)",
            "mode": s.mode,
            "message_count": s.message_count,
            "time_created": s.time_created,
            "time_updated": s.time_updated,
            "character_name": s.character_name,
            "children_count": children_counts.get(s.id, 0),
        })
    return web.json_response({"children": items, "count": len(items)})


def _sync_trace(msg: str) -> None:
    """Append a reader-sync trace line (frontend bridge debugging)."""
    try:
        import time as _t
        from pathlib import Path as _P
        log_dir = _P("data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "reader_sync.log", "a", encoding="utf-8") as f:
            f.write(_t.strftime("%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


async def handle_sync(request: web.Request) -> web.Response:
    """Receive page-turn event from the reader frontend.

    POST body: {bookPath, chapterTitle, pageText, timestamp}

    Matches page text to a chunk in the database via fingerprint,
    optionally creates/updates a reading session.
    """
    db = request.app["db"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    page_text = body.get("pageText", "")
    chapter_title = body.get("chapterTitle", "")
    book_path = body.get("bookPath", "")
    book_id = body.get("bookId", "")
    # epub.js CFI of the viewport top — the render layer's authoritative
    # coordinate. Stored alongside (chapter, chunk) so reopening the book
    # can restore via a single deterministic display(cfi) instead of
    # replaying the fingerprint anchor chain.
    cfi = body.get("cfi", "") or ""

    if not page_text:
        return web.json_response({"matched": False, "reason": "empty page text"})

    _sync_trace(f"recv book={book_id or book_path} record={body.get('record', True)} "
                f"cfi={cfi[:48]!r} text={page_text[:30]!r}")

    # Find book: native reader sends bookId directly; Koodo-style hooks
    # send bookPath (hashed to book_id) or fall back to text search.
    if book_id:
        if not db.get_book(book_id):
            _sync_trace(f"miss: book {book_id} not in library")
            return web.json_response(
                {"matched": False, "reason": "book not found in library"}
            )
    else:
        # Find book by file path → MD5
        book_id = _find_book_by_path(db, book_path)
        if not book_id:
            # Try matching across all books
            book_id = _find_book_by_text(db, page_text)

        if not book_id:
            return web.json_response(
                {"matched": False, "reason": "book not found in library"}
            )

    # Fingerprint match. hint = reader's last known position: consecutive
    # scroll reports are always adjacent, so the matcher resolves repeated
    # short text to the nearest occurrence instead of the book's earliest
    # (which teleported progress backwards across chapters).
    hint = None
    _hint_ch = body.get("hintChapter")
    _hint_ck = body.get("hintChunk")
    if _hint_ch is not None:
        try:
            hint = (int(_hint_ch), int(_hint_ck) if _hint_ck is not None else None)
        except (TypeError, ValueError):
            hint = None
    result = match_page_text(db, book_id, chapter_title, page_text, hint=hint)
    if not result:
        _sync_trace(f"miss: no chunk match text={page_text[:40]!r}")
        return web.json_response(
            {"matched": False, "reason": "no chunk match", "book_id": book_id}
        )

    # Record position in reading session. The native reader reports on
    # every scroll — dedup identical positions so positions_json doesn't
    # bloat with consecutive duplicates.
    #
    # record=False: the reader's programmatic position restore only
    # refreshes the UI. Recording the restore landing spot (section top /
    # book-start fallback) would overwrite the real progress and loop
    # "reopen → back to the start".
    if body.get("record", True):
        session = db.get_active_session(book_id)
        if not session:
            session = db.start_reading_session(book_id)
        last = db.get_last_position(session.id)
        if last != (result["chapter_index"], result["chunk_index"]):
            db.record_position(
                session.id, result["chapter_index"], result["chunk_index"], cfi
            )
    else:
        session = db.get_active_session(book_id)   # 仅用于返回 session_id
    session_id = session.id if session else ""
    _sync_trace(f"hit ch={result['chapter_index']} ck={result['chunk_index']} "
                f"{'recorded' if session_id and body.get('record', True) else 'ui-only'}")

    # Get chapter type for breadcrumb
    ch = db.get_chapter(book_id, result["chapter_index"])
    chapter_type = ch.chapter_type if ch else "body"

    # Build breadcrumb
    all_chapters = db.get_chapters(book_id)
    prev_ch = _adjacent_chapter(all_chapters, result["chapter_index"], -1)
    next_ch = _adjacent_chapter(all_chapters, result["chapter_index"], +1)
    breadcrumb = f"{_type_label(chapter_type)} · "
    breadcrumb += f"前: {prev_ch.title if prev_ch else '无'} | "
    breadcrumb += f"后: {next_ch.title if next_ch else '无'}"

    return web.json_response({
        "matched": True,
        "position": {
            **result,
            "chapter_type": chapter_type,
            "breadcrumb": breadcrumb,
        },
        "session_id": session_id,
    })


async def handle_list_books(request: web.Request) -> web.Response:
    """List all imported books in the library."""
    db = request.app["db"]
    books = db.list_books()
    bookbank = db._path.parent / "bookbank"

    enriched: list[dict] = []
    for b in books:
        has_cover = any(
            (bookbank / f"{b.id}_cover.{ext}").exists()
            for ext in ("jpg", "jpeg", "png", "gif", "webp", "svg")
        )
        # Get latest reading position across all sessions for this book
        last_read_at = None
        last_chapter = None
        last_chunk = None
        active = db.get_active_session(b.id)
        if active and active.positions_json:
            import json as _json
            try:
                positions = _json.loads(active.positions_json)
                if positions:
                    last = positions[-1]
                    last_chapter = last.get("chapter")
                    last_chunk = last.get("chunk")
                    last_read_at = last.get("ts", active.started_at)
            except Exception:
                pass

        enriched.append({
            "id": b.id,
            "title": b.title,
            "author": b.author,
            "author_nationality": b.author_nationality,
            "format": b.format,
            "chapter_count": b.chapter_count,
            "total_chars": b.total_chars,
            "status": b.status,
            "stable_book_id": _get_stable_id(db, b.id),
            "has_cover": has_cover,
            "time_created": b.time_created,
            "time_updated": b.time_updated,
            "last_read_at": last_read_at,
            "last_chapter": last_chapter,
            "last_chunk": last_chunk,
        })

    return web.json_response({"books": enriched})


async def handle_context(request: web.Request) -> web.Response:
    """Return a structured reading map for a book."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    ctx = get_book_context(db, book_id)
    return web.json_response(ctx)


async def handle_chunk(request: web.Request) -> web.Response:
    """Return a specific chunk's content."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    try:
        chapter = int(request.query.get("chapter", 0))
        chunk = int(request.query.get("chunk", 0))
    except ValueError:
        return web.json_response({"error": "invalid chapter/chunk"}, status=400)

    ck = db.get_chunk(book_id, chapter, chunk)
    if not ck:
        return web.json_response(
            {"error": f"chunk not found: ch.{chapter} ck.{chunk}"}, status=404
        )

    ch = db.get_chapter(book_id, chapter)
    total = db.get_chunk_count(book_id, chapter)

    # Adjacent chunks — cross chapter boundaries when needed
    prev_anchor = _find_prev_chunk(db, book_id, chapter, chunk)
    next_anchor = _find_next_chunk(db, book_id, chapter, chunk)

    # Record reading position for persistence — opt-in via ?record=1.
    # The chunk view (real user page turns) passes it. Other callers —
    # notably the native reader fetching anchor text inside
    # jumpToAnchor(skipRecord:true) — must NOT write progress, or the
    # frontend's skip-record exemption is bypassed server-side. Deduped
    # like /api/sync so repeated opens don't bloat positions_json.
    if request.query.get("record") == "1":
        try:
            session = db.get_active_session(book_id)
            if not session:
                session = db.start_reading_session(book_id)
            last = db.get_last_position(session.id)
            if last != (chapter, chunk):
                db.record_position(session.id, chapter, chunk)
        except Exception:
            pass  # Best-effort; don't block chunk loading

    return web.json_response({
        "content": ck.content,
        "chapter_title": ch.title if ch else "",
        "chapter_type": ch.chapter_type if ch else "body",
        "chunk_index": chunk,
        "total_chunks": total,
        "prev_anchor": prev_anchor,
        "next_anchor": next_anchor,
        "char_count": len(ck.content) if ck else 0,
        "progress": {
            "chapter": chapter,
            "chunk": chunk,
            "total_chunks": total,
        },
        "prev": prev_anchor,
        "next": next_anchor,
    })


async def handle_cover(request: web.Request) -> web.Response:
    """Return cover image for a book, or a generated SVG placeholder."""
    from pathlib import Path as _Path

    db = request.app["db"]
    book_id = request.match_info["book_id"]

    book = db.get_book(book_id)
    if not book:
        return web.json_response({"error": "Book not found"}, status=404)

    # Look for extracted cover in bookbank
    bookbank = db._path.parent / "bookbank"
    cover_file = None
    for ext in ("jpg", "jpeg", "png", "gif", "webp", "svg"):
        candidate = bookbank / f"{book_id}_cover.{ext}"
        if candidate.exists():
            cover_file = candidate
            break

    if cover_file:
        content_type = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif",
            "webp": "image/webp", "svg": "image/svg+xml",
        }.get(cover_file.suffix.lstrip("."), "image/jpeg")
        return web.Response(
            body=cover_file.read_bytes(),
            content_type=content_type,
            # Covers are immutable per book_id — cache them so shelf
            # re-renders (sorting, row regrouping on resize) don't
            # re-download every image.
            headers={"Cache-Control": "private, max-age=86400"},
        )

    # Fallback: generate a placeholder SVG cover
    title = book.title or "Book"
    initials = title[:4]
    svg = _generate_placeholder_cover(initials)
    return web.Response(
        text=svg,
        content_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=300"},
    )


async def handle_book_file(request: web.Request) -> web.Response:
    """Serve the original EPUB file from bookbank for native rendering.

    Only EPUB books are served — MOBI/txt have no native render path and
    the frontend falls back to chunk rendering. The file is resolved from
    bookbank by book_id (the same copy the backend parsed at import),
    never from source_path.
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    book = db.get_book(book_id)
    if not book:
        return web.json_response({"error": "Book not found"}, status=404)
    if (book.format or "").lower() != "epub":
        return web.json_response({"error": "only epub supported"}, status=415)

    bookbank = db._path.parent / "bookbank"
    candidate = bookbank / f"{book_id}.epub"
    try:
        resolved = candidate.resolve()
    except OSError:
        return web.json_response({"error": "file missing"}, status=404)
    try:
        if not resolved.is_relative_to(bookbank.resolve()):
            return web.json_response({"error": "book not found"}, status=404)
    except ValueError:
        return web.json_response({"error": "book not found"}, status=404)
    if not resolved.is_file():
        return web.json_response({"error": "file missing"}, status=404)

    return web.FileResponse(
        path=resolved,
        headers={"Content-Type": "application/epub+zip"},
    )


def _generate_placeholder_cover(text: str) -> str:
    """Generate a warm-toned SVG placeholder cover with title text."""
    import html as _html
    escaped = _html.escape(text)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="240" height="320" viewBox="0 0 240 320">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#4a3828"/>
      <stop offset="50%" stop-color="#5c4430"/>
      <stop offset="100%" stop-color="#3d2e20"/>
    </linearGradient>
  </defs>
  <rect width="240" height="320" fill="url(#bg)" rx="4"/>
  <rect x="8" y="4" width="224" height="312" fill="none" stroke="#6b5d4f" stroke-width="1" rx="3"/>
  <rect x="14" y="10" width="212" height="300" fill="none" stroke="#5a4d40" stroke-width="0.5" rx="2"/>
  <text x="120" y="150" text-anchor="middle" fill="#9a8d7e" font-family="serif" font-size="28" font-weight="bold">{escaped}</text>
  <line x1="60" y1="165" x2="180" y2="165" stroke="#6b5d4f" stroke-width="0.5"/>
</svg>"""


async def handle_import_book(request: web.Request) -> web.Response:
    """Import a book from a server-local file path.

    POST body: {path: "/path/to/your/book.epub"}
    Returns the imported book metadata, or an error.
    """
    db = request.app["db"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    path = body.get("path", "").strip()
    if not path:
        return web.json_response({"error": "path is required"}, status=400)

    from services.library_service import import_book

    try:
        book, is_new = import_book(db, path)
    except FileNotFoundError:
        return web.json_response({"error": f"file not found: {path}"}, status=404)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    # Fire one-shot LLM metadata cleanup for newly imported books (async,
    # non-blocking).  Re-imports (is_new=False) and missing api_client skip.
    if is_new and request.app.get("api_client"):
        asyncio.create_task(_enrich_book_metadata_async(
            request.app["api_client"], db, book.id,
        ))

    return web.json_response({
        "imported": is_new,
        "book": {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "format": book.format,
            "chapter_count": book.chapter_count,
            "total_chars": book.total_chars,
            "status": book.status,
        },
    })


async def handle_scan_folder(request: web.Request) -> web.Response:
    """Scan a folder for EPUB/MOBI files not yet imported.

    GET /api/books/scan-folder?path=/some/folder
    Returns a list of unimported files with size and modification time.
    """
    from pathlib import Path

    db = request.app["db"]
    folder = request.query.get("path", "").strip()
    if not folder:
        # 未显式传 path → 用配置的默认卸货区（.env BOOK_COMPANION_SCAN_FOLDER /
        # settings.json scan_folder），也没有 → 400 让前端引导输入。
        cfg = request.app.get("config")
        folder = (getattr(cfg, "scan_folder", "") or "").strip() if cfg else ""
    if not folder:
        return web.json_response(
            {"error": "未配置扫描文件夹：请传 path，或在 .env 设置 "
                      "BOOK_COMPANION_SCAN_FOLDER"},
            status=400)

    folder_path = Path(folder)
    if not folder_path.is_dir():
        return web.json_response({"error": f"not a directory: {folder}"}, status=404)

    # Collect all EPUB/MOBI files
    epub_files = list(folder_path.glob("*.epub"))
    mobi_files = list(folder_path.glob("*.mobi"))
    all_files = epub_files + mobi_files

    # Get already-imported source paths
    imported_paths: set[str] = set()
    for book in db.list_books(limit=10000):
        if book.source_path:
            imported_paths.add(book.source_path.replace("\\", "/"))

    # Filter to unimported only
    unimported: list[dict] = []
    for fp in all_files:
        fp_str = str(fp).replace("\\", "/")
        if fp_str in imported_paths:
            continue
        stat = fp.stat()
        unimported.append({
            "path": str(fp),
            "name": fp.stem,
            "format": fp.suffix.lower().lstrip("."),
            "size_mb": round(stat.st_size / (1024 * 1024), 1),
            "modified_at": int(stat.st_mtime),
        })

    # Sort by name
    unimported.sort(key=lambda f: f["name"].lower())

    return web.json_response({
        "folder": str(folder_path),
        "total_files": len(all_files),
        "already_imported": len(all_files) - len(unimported),
        "unimported": unimported,
    })


async def handle_upload_book(request: web.Request) -> web.Response:
    """Upload a book file from the browser and import it.

    Accepts multipart/form-data with a single file field named "file".
    Supports .epub, .mobi, .txt files up to 100MB.

    Returns the imported book metadata (same format as /api/books/import).
    """
    db = request.app["db"]

    # Parse multipart — grab the first file field
    reader = await request.multipart()
    file_field = None
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            file_field = field
            break
        field = await reader.next()

    if file_field is None:
        return web.json_response(
            {"error": "未找到文件字段。请使用 file 字段上传文件。"},
            status=400,
        )

    filename = file_field.filename or "uploaded.book"
    ext = Path(filename).suffix.lower()
    if ext not in (".epub", ".mobi", ".txt"):
        return web.json_response(
            {"error": f"不支持的文件格式: {ext}。支持: .epub, .mobi, .txt"},
            status=400,
        )

    # Save uploaded file to a temp location in data/watch/
    watch_dir = Path(__file__).parent / "data" / "watch"
    watch_dir.mkdir(parents=True, exist_ok=True)

    import uuid
    temp_name = f"upload_{uuid.uuid4().hex[:8]}{ext}"
    temp_path = watch_dir / temp_name

    size = 0
    with open(temp_path, "wb") as f:
        while True:
            chunk = await file_field.read_chunk(65536)  # 64KB chunks
            if not chunk:
                break
            f.write(chunk)
            size += len(chunk)
            if size > 104_857_600:  # 100MB limit
                f.close()
                temp_path.unlink(missing_ok=True)
                return web.json_response(
                    {"error": "文件过大（上限 100MB）"}, status=413
                )

    if size == 0:
        temp_path.unlink(missing_ok=True)
        return web.json_response({"error": "上传的文件为空"}, status=400)

    from services.library_service import import_book

    try:
        book, is_new = import_book(db, str(temp_path))
    except FileNotFoundError:
        temp_path.unlink(missing_ok=True)
        return web.json_response({"error": "文件保存失败"}, status=500)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        return web.json_response({"error": str(exc)}, status=500)

    # Fire one-shot LLM metadata cleanup for newly imported books (async,
    # non-blocking).  Re-imports (is_new=False) and missing api_client skip.
    if is_new and request.app.get("api_client"):
        asyncio.create_task(_enrich_book_metadata_async(
            request.app["api_client"], db, book.id,
        ))

    # Clean up the temp upload file — import_book already copied to bookbank
    temp_path.unlink(missing_ok=True)

    return web.json_response({
        "imported": is_new,
        "book": {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "format": book.format,
            "chapter_count": book.chapter_count,
            "total_chars": book.total_chars,
            "status": book.status,
        },
    })


async def handle_delete_book(request: web.Request) -> web.Response:
    """Delete a book and all related data (chapters, chunks, reading sessions, files).

    Returns the deleted book's summary for confirmation.
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    from services.library_service import delete_book

    try:
        summary = delete_book(db, book_id)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)

    return web.json_response({"deleted": True, **summary})


async def handle_search_book(request: web.Request) -> web.Response:
    """Sentence-level full-text search within a book."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    query = request.query.get("q", "").strip()
    if not query or len(query) < 1:
        return web.json_response({"error": "q parameter is required"}, status=400)

    limit = min(int(request.query.get("limit", "30")), 50)
    from services.library_service import search_sentences

    results = search_sentences(db, book_id, query, limit=limit)
    return web.json_response({
        "query": query,
        "book_id": book_id,
        "count": len(results),
        "results": results,
    })


async def handle_search_overview(request: web.Request) -> web.Response:
    """Per-chapter hit-count overview for a search query within a book.

    Query params:
        q       — search string (required, min 1 char)
        current — current chapter index (optional, for proximity sorting)

    Returns a list of chapters that contain hits, each with a hit_count.
    When *current* is provided, chapters are sorted by proximity to it.
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"error": "q parameter is required"}, status=400)

    from services.library_service import search_chapter_overview

    chapters = search_chapter_overview(db, book_id, query)

    # Proximity sort: chapters closest to current reading position first
    current_str = request.query.get("current")
    if current_str:
        try:
            current_ch = int(current_str)
            chapters.sort(
                key=lambda c: (abs(c["chapter_index"] - current_ch),
                                c["chapter_index"])
            )
        except ValueError:
            pass

    total_hits = sum(c["hit_count"] for c in chapters)
    return web.json_response({
        "query": query,
        "book_id": book_id,
        "total_chapters": len(chapters),
        "total_hits": total_hits,
        "chapters": chapters,
    })


async def handle_search_chapter(request: web.Request) -> web.Response:
    """Sentence-level search, with pagination.

    Query params:
        q       — search string (required)
        chapter — chapter index (optional; when omitted, searches the
                  entire book in natural reading order)
        offset  — pagination offset (default 0)
        limit   — page size (default 20, max 50)
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"error": "q parameter is required"}, status=400)

    chapter_str = request.query.get("chapter")
    chapter_index: int | None = None
    if chapter_str is not None:
        try:
            chapter_index = int(chapter_str)
        except ValueError:
            return web.json_response({"error": "chapter must be an integer"}, status=400)

    offset = max(int(request.query.get("offset", "0")), 0)
    limit = min(max(int(request.query.get("limit", "20")), 1), 50)

    from services.library_service import search_chapter_sentences

    result = search_chapter_sentences(
        db, book_id, query, chapter_index, offset=offset, limit=limit,
    )
    return web.json_response({
        "query": query,
        "book_id": book_id,
        "chapter_index": chapter_index,
        **result,
    })


# ── Chapter type overrides（覆盖层便利贴 · 标注面板）───────────────────────


async def handle_list_chapter_types(request: web.Request) -> web.Response:
    """List all chapters with effective type + override marker for the tagging panel.

    Lightweight (no chunk content). Each entry carries:
      type          — effective type (override already applied by get_chapters)
      overridden    — whether a human sticky-note exists
      original_type — raw (AI-guessed/import) type → what clearing falls back to
      note / source — sticky-note metadata, '' when not overridden
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    book = db.get_book(book_id)
    if not book:
        return web.json_response({"error": "Book not found"}, status=404)

    chapters = db.get_chapters(book_id)
    source_types = db.get_chapter_source_types(book_id)
    overrides = db.list_chapter_overrides(book_id)

    out = []
    for ch in chapters:
        ov = overrides.get(ch.chapter_index)
        out.append({
            "chapter_index": ch.chapter_index,
            "title": ch.title or "",
            "char_count": ch.char_count or 0,
            "type": ch.chapter_type,                       # 已盖便利贴
            "overridden": ov is not None,
            "original_type": source_types.get(ch.chapter_index, "body"),
            "note": ov.note if ov else "",
            "source": ov.source if ov else "",
        })
    return web.json_response({
        "book": {"id": book.id, "title": book.title},
        "chapters": out,
    })


async def handle_set_override(request: web.Request) -> web.Response:
    """Stick a human override（贴便利贴/更新）on one chapter's type."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    try:
        index = int(request.match_info["index"])
    except ValueError:
        return web.json_response({"error": "chapter index must be an integer"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    ctype = (body.get("chapter_type") or "").strip()
    note = (body.get("note") or "").strip()
    if ctype not in VALID_CHAPTER_TYPES:
        return web.json_response(
            {"error": f"invalid chapter_type {ctype!r}; valid: {sorted(VALID_CHAPTER_TYPES)}"},
            status=400,
        )
    if not db.get_book(book_id):
        return web.json_response({"error": "Book not found"}, status=404)

    db.set_chapter_override(book_id, index, ctype, note=note)
    return web.json_response({
        "set": True,
        "book_id": book_id,
        "chapter_index": index,
        "chapter_type": ctype,
        "note": note,
        "label": _type_label(ctype),
    })


async def handle_clear_override(request: web.Request) -> web.Response:
    """Remove a human override（撕便利贴），回落原表类型."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    try:
        index = int(request.match_info["index"])
    except ValueError:
        return web.json_response({"error": "chapter index must be an integer"}, status=400)
    if not db.get_book(book_id):
        return web.json_response({"error": "Book not found"}, status=404)

    fallback = db.get_chapter_source_types(book_id).get(index, "body")
    db.clear_chapter_override(book_id, index)
    return web.json_response({
        "cleared": True,
        "book_id": book_id,
        "chapter_index": index,
        "original_type": fallback,
        "label": _type_label(fallback),
    })


# ── Annotations ──────────────────────────────────────────────────────────


async def handle_list_annotations(request: web.Request) -> web.Response:
    """List annotations with three query modes.

    ?stable_book_id=X            — all annotations for a book
    ?stable_book_id=X&chunk=Z    — annotations for a specific chunk
    ?type=note                   — all notes (cross-book)
    """
    db = request.app["db"]
    stable_book_id = request.query.get("stable_book_id", "")
    chunk_str = request.query.get("chunk")
    ann_type = request.query.get("type", "")

    if ann_type in ("note", "highlight"):
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        rows = db.get_annotations_by_type(ann_type, limit=limit, offset=offset)
        return web.json_response({
            "annotations": [_annotation_to_dict(r) for r in rows],
            "count": len(rows),
        })

    if not stable_book_id:
        return web.json_response({"error": "stable_book_id or type=note/highlight required"}, status=400)

    if chunk_str is not None:
        # Chunk-level: need book_id from import_log
        try:
            chunk_index = int(chunk_str)
        except ValueError:
            return web.json_response({"error": "chunk must be an integer"}, status=400)
        import_log = db.get_latest_import_for_stable(stable_book_id)
        if not import_log or not import_log.book_id:
            return web.json_response({"annotations": [], "count": 0})
        chapter_index = int(request.query.get("chapter", "0"))
        rows = db.get_annotations_by_chunk(import_log.book_id, chapter_index, chunk_index)
    else:
        rows = db.get_annotations_by_stable_book(stable_book_id)

    return web.json_response({
        "annotations": [_annotation_to_dict(r) for r in rows],
        "count": len(rows),
    })


async def handle_create_annotation(request: web.Request) -> web.Response:
    """Create a new annotation (highlight, note, or both)."""
    db = request.app["db"]
    from storage.models import AnnotationRow
    from storage.db import _now_ms, _new_id
    from services.library_service import compute_annotation_offsets

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    book_id = str(body.get("book_id", "")).strip()
    if not book_id:
        return web.json_response({"error": "book_id is required"}, status=400)

    # Resolve stable_book_id from import_log
    import_log = db.get_import_log_by_book(book_id)
    if not import_log:
        return web.json_response({"error": f"No import_log found for book_id={book_id}"}, status=404)
    stable_book_id = import_log.stable_book_id

    selected_text = str(body.get("selected_text", "")).strip()
    if not selected_text or len(selected_text) < 2:
        return web.json_response({"error": "selected_text is required (min 2 chars)"}, status=400)

    ann_type = str(body.get("type", "highlight"))
    # v16: type 二分——highlight=收藏（下划线），note=批注；bookmark 废弃
    if ann_type not in ("highlight", "note"):
        ann_type = "highlight"

    color = str(body.get("color", "#FFD54F"))
    note_text = str(body.get("note_text", "")) if body.get("note_text") else None
    # v16: native 标注层——收藏样式（首版仅 underline）+ CFI 精确定位
    style = str(body.get("style", "underline")) if ann_type == "highlight" else None
    cfi_range = str(body.get("cfi_range", "")) or None

    chapter_index = body.get("chapter_index")
    chunk_index = body.get("chunk_index")

    # Compute persistent coordinates
    context_before = str(body.get("context_before", ""))[:40] or ""
    context_after = str(body.get("context_after", ""))[:40] or ""
    offsets = compute_annotation_offsets(
        db, book_id, chapter_index, chunk_index,
        selected_text, context_before, context_after,
    )

    now = _now_ms()
    row = AnnotationRow(
        id=_new_id("ann"),
        stable_book_id=stable_book_id,
        book_id=book_id,
        chapter_index=chapter_index,
        chunk_index=chunk_index,
        selected_text=selected_text,
        context_before=context_before or None,
        context_after=context_after or None,
        # v14 persistent coordinates
        paragraph_index=offsets.get("paragraph_index"),
        offset_in_paragraph=offsets.get("offset_in_paragraph"),
        chapter_offset_start=offsets.get("chapter_offset_start"),
        chapter_offset_end=offsets.get("chapter_offset_end"),
        context_window=offsets.get("context_window"),
        source_session_id=None,  # populated when linked to a conversation session
        chunking_version=1,
        # core fields
        type=ann_type,
        note_text=note_text,
        note_format=str(body.get("note_format", "markdown")),
        color=color,
        style=style,
        cfi_range=cfi_range,
        match_status="linked",
        time_created=now,
        time_updated=now,
    )
    ann_id = db.insert_annotation(row)

    return web.json_response({"id": ann_id, "stable_book_id": stable_book_id}, status=201)


async def handle_update_annotation(request: web.Request) -> web.Response:
    """Update an annotation (color, note_text, type, style, cfi_range)."""
    db = request.app["db"]
    ann_id = request.match_info["ann_id"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    allowed = {"color", "note_text", "note_format", "type", "style", "cfi_range"}
    updates = {k: v for k, v in body.items() if k in allowed and v is not None}
    if not updates:
        return web.json_response({"error": "No valid fields to update"}, status=400)
    if "type" in updates and updates["type"] not in ("highlight", "note"):
        return web.json_response({"error": "type must be highlight or note"}, status=400)

    db.update_annotation(ann_id, **updates)
    return web.json_response({"updated": True, "id": ann_id})


async def handle_delete_annotation(request: web.Request) -> web.Response:
    """Delete an annotation permanently."""
    db = request.app["db"]
    ann_id = request.match_info["ann_id"]

    deleted = db.delete_annotation(ann_id)
    if not deleted:
        return web.json_response({"error": "Annotation not found"}, status=404)

    return web.json_response({"deleted": True, "id": ann_id})


def _get_stable_id(db, book_id: str) -> str | None:
    """Resolve stable_book_id for a book, or None if no import_log."""
    log = db.get_import_log_by_book(book_id)
    return log.stable_book_id if log else None


def _annotation_to_dict(r) -> dict:
    return {
        "id": r.id,
        "stable_book_id": r.stable_book_id,
        "book_id": r.book_id,
        "chapter_index": r.chapter_index,
        "chunk_index": r.chunk_index,
        "selected_text": r.selected_text,
        "context_before": r.context_before,
        "context_after": r.context_after,
        # v14 persistent coordinates
        "paragraph_index": r.paragraph_index,
        "offset_in_paragraph": r.offset_in_paragraph,
        "chapter_offset_start": r.chapter_offset_start,
        "chapter_offset_end": r.chapter_offset_end,
        "context_window": r.context_window,
        "source_session_id": r.source_session_id,
        "chunking_version": r.chunking_version,
        # v16 native annotation layer
        "style": r.style,
        "cfi_range": r.cfi_range,
        # core fields
        "type": r.type,
        "note_text": r.note_text,
        "note_format": r.note_format,
        "color": r.color,
        "match_status": r.match_status,
        "time_created": r.time_created,
        "time_updated": r.time_updated,
    }


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


def _code_fingerprint() -> dict:
    """Identify the code this *process* is actually running.

    A long-lived server can outlive the source on disk: ``api_server.py``
    gets rewritten while the old process keeps serving the old module
    object.  That failure mode is invisible over HTTP — the endpoints
    answer, routes exist, and bugs that were already fixed still appear
    alive.  Exposing the loaded file's mtime + size + digest lets the
    reader (and us) tell "code is broken" apart from "process is stale".
    """
    import hashlib
    from pathlib import Path as _Path
    p = _Path(__file__)
    try:
        raw = p.read_bytes()
        return {
            "api_server_mtime": datetime.fromtimestamp(
                p.stat().st_mtime).isoformat(timespec="seconds"),
            "api_server_bytes": len(raw),
            "api_server_sha256": hashlib.sha256(raw).hexdigest()[:16],
        }
    except Exception as exc:  # pragma: no cover - diagnostics only
        return {"api_server_mtime": f"unreadable: {exc}"}


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


# ── Chunk navigation helpers ────────────────────────────────────────────


def _find_prev_chunk(db, book_id: str, chapter: int, chunk: int) -> dict | None:
    """Find the chunk before (chapter, chunk), crossing chapter boundaries."""
    if chunk > 0:
        # Same chapter, previous chunk
        if db.get_chunk(book_id, chapter, chunk - 1):
            return {"chapter": chapter, "chunk": chunk - 1}
        return None
    # First chunk of chapter — go to previous chapter's last chunk
    if chapter <= 0:
        return None
    prev_total = db.get_chunk_count(book_id, chapter - 1)
    if prev_total == 0:
        return None
    return {"chapter": chapter - 1, "chunk": prev_total - 1}


def _find_next_chunk(db, book_id: str, chapter: int, chunk: int) -> dict | None:
    """Find the chunk after (chapter, chunk), crossing chapter boundaries."""
    total = db.get_chunk_count(book_id, chapter)
    if chunk + 1 < total:
        # Same chapter, next chunk
        return {"chapter": chapter, "chunk": chunk + 1}
    # Last chunk of chapter — go to next chapter's first chunk
    next_total = db.get_chunk_count(book_id, chapter + 1)
    if next_total == 0:
        return None
    return {"chapter": chapter + 1, "chunk": 0}


# ── Helpers ────────────────────────────────────────────────────────────


def _find_book_by_path(db, book_path: str) -> str | None:
    """Match a file path to an imported book via MD5."""
    if not book_path:
        return None
    try:
        from services.library_service import _hash_file
        book_id = _hash_file(Path(book_path))
        book = db.get_book(book_id)
        return book.id if book else None
    except Exception:
        return None


def _find_book_by_text(db, page_text: str) -> str | None:
    """Fuzzy-find a book by searching page text across all books."""
    try:
        results = db.search_books(page_text[:80], limit=1)
        return results[0]["book_id"] if results else None
    except Exception:
        return None


def _adjacent_chapter(chapters, current_idx: int, delta: int):
    """Get the previous or next chapter by index."""
    for ch in chapters:
        if ch.chapter_index == current_idx + delta:
            return ch
    return None


def _type_label(chapter_type: str) -> str:
    """Human-readable label for chapter type."""
    labels = {
        "cover": "封面",
        "copyright": "版权",
        "toc": "目录",
        "preface": "前言",
        "body": "正文",
        "postscript": "后记",
        "appendix": "附录",
        "bibliography": "参考",
        "index": "索引",
        "notes": "注释",
    }
    return labels.get(chapter_type, chapter_type)


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
        from pathlib import Path
        graph_path = Path(__file__).parent / "data" / "graphs" / f"graph_{book_id}.db"
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


async def handle_graph_full(request: web.Request) -> web.Response:
    """GET /api/graph/{book_id}/full?kinds=character,event&limit=500  — full graph data."""
    book_id = request.match_info["book_id"]
    kinds_str = request.query.get("kinds")
    kinds = kinds_str.split(",") if kinds_str else None
    limit = int(request.query.get("limit", "500"))
    gdb = _get_graph_db(request, book_id)
    try:
        result = gdb.get_full_graph(book_id, kinds=kinds, limit=limit)
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


# ── Avatar ───────────────────────────────────────────────────────────────


def _avatar_dir(book_id: str) -> Path:
    """Ensure and return the avatar directory for a book."""
    d = Path(__file__).parent / "roles" / "avatars" / book_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_avatar_name(name: str) -> str:
    """Sanitize a character name for use in a filename."""
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")[:80]


async def handle_avatar_upload(request: web.Request) -> web.Response:
    """POST /api/graph/{book_id}/characters/{name}/avatar

    Accepts multipart/form-data with a file field named ``file``.
    Saves to roles/avatars/{book_id}/{sanitized_name}.png.
    Max 2 MB.  Accepts PNG / JPEG / GIF / WebP.
    """
    book_id = request.match_info["book_id"]
    char_name = request.match_info["name"]

    reader = await request.multipart()
    file_field = None
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            file_field = field
            break
        field = await reader.next()

    if file_field is None:
        return web.json_response({"error": "未找到文件字段"}, status=400)

    ext = Path(file_field.filename or "avatar.png").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return web.json_response({"error": f"不支持的格式: {ext}"}, status=400)

    sanitized = _sanitize_avatar_name(char_name)
    dest = _avatar_dir(book_id) / f"{sanitized}{ext}"

    # Remove old avatar with any extension
    for old in _avatar_dir(book_id).glob(f"{sanitized}.*"):
        if old != dest:
            old.unlink(missing_ok=True)

    size = 0
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await file_field.read_chunk(65536)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
                if size > 2_097_152:  # 2 MB
                    raise ValueError("文件过大（上限 2MB）")
    except ValueError as exc:
        dest.unlink(missing_ok=True)
        return web.json_response({"error": str(exc)}, status=413)

    return web.json_response({
        "uploaded": True,
        "book_id": book_id,
        "character_name": char_name,
        "url": f"/avatars/{book_id}/{sanitized}{ext}",
    })


async def handle_avatar_serve(request: web.Request) -> web.Response:
    """GET /avatars/{book_id}/{name}.png — serve character avatar or 404.

    If the file does not exist the frontend falls back to a default
    coloured-circle avatar.
    """
    book_id = request.match_info["book_id"]
    name = request.match_info["name"]

    sanitized = _sanitize_avatar_name(Path(name).stem)
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        path = _avatar_dir(book_id) / f"{sanitized}{ext}"
        if path.exists():
            return web.FileResponse(path=path)

    return web.json_response({"error": "avatar not found"}, status=404)


async def handle_avatar_json(request: web.Request) -> web.Response:
    """GET /avatars/{book_id}/{name}.json — serve voice-config / metadata JSON."""
    book_id = request.match_info["book_id"]
    name = request.match_info["name"]

    sanitized = _sanitize_avatar_name(name)
    path = _avatar_dir(book_id) / f"{sanitized}.json"
    if path.exists():
        return web.FileResponse(path=path)
    return web.json_response({"error": "json file not found"}, status=404)


# ── Voice (TTS + ASR) ────────────────────────────────────────────────────

# Lazy-loaded SenseVoiceSmall singleton (loaded once, reused across requests).
#
# 线程安全说明：后台预热跑在线程里，/api/asr 也经 asyncio.to_thread 跑在线程里，
# 两者可能同时首次触达这里。裸 check-then-set 会让并发调用各加载一份
# （实测 4 并发 = 加载 4 次，内存翻倍且全局变量被后写覆盖）。改为双检锁后
# 只加载一次，后到者阻塞等待、加载完成后复用——这同时天然实现了
# "预热尚未就绪时，ASR 请求等待而非失败"的语义。
_sense_voice_model: Any = None
_sense_voice_lock = threading.Lock()
# idle → loading → ready | failed，便于排查与观测
_SENSE_VOICE_STATE: dict = {"state": "idle", "error": None, "loaded_at": None}


def _get_sense_voice():
    """Return the global SenseVoiceSmall model, loading on first call.

    线程安全（双检锁）：并发调用只会真正加载一次，其余调用等待后复用同一实例。
    始终通过 asyncio.to_thread 调用——模型构造会短暂抢占 GIL
    （实测单次事件循环抖动约 1.5s），绝不能直接跑在事件循环上。
    """
    global _sense_voice_model
    if _sense_voice_model is not None:      # 快路径：已就绪，零锁开销
        return _sense_voice_model

    with _sense_voice_lock:
        if _sense_voice_model is not None:  # 双检：可能已被别的线程加载完
            return _sense_voice_model

        model_dir = Path(__file__).parent / "data" / "models" / "iic" / "SenseVoiceSmall"
        if not model_dir.exists():
            _SENSE_VOICE_STATE.update(
                state="failed", error=f"model not found: {model_dir}")
            raise RuntimeError(
                f"SenseVoiceSmall model not found at {model_dir}. "
                "Download it: pip install modelscope && python scripts/download_asr_model.py"
            )

        _SENSE_VOICE_STATE["state"] = "loading"
        try:
            from funasr_onnx import SenseVoiceSmall
            model = SenseVoiceSmall(
                str(model_dir),
                batch_size=1,
                quantize=True,
                device_id=-1,  # CPU
            )
        except Exception as exc:
            _SENSE_VOICE_STATE.update(state="failed", error=str(exc))
            raise

        _sense_voice_model = model
        _SENSE_VOICE_STATE.update(
            state="ready", error=None,
            loaded_at=datetime.now().isoformat(timespec="seconds"),
        )
        return _sense_voice_model


async def warm_asr_background(grace_s: float = 2.5) -> None:
    """在后台预热 ASR 模型——不阻塞启动通道。

    调用方应把它排成后台任务（asyncio.create_task），而不是 await：
    HTTP 服务先开始监听，模型随后在后台线程加载。就绪之前到达的
    /api/asr 会在 _get_sense_voice 的锁上等待并复用结果，不会重复加载。

    grace_s: 先让出若干秒再开始，避开前端首屏加载期。模型构造阶段有
             一次约 1.5s 的 GIL 抢占（实测，仅此一次），错开可让抖动
             落在用户空闲窗口内。设为 0 表示立即开始。
    """
    if grace_s > 0:
        await asyncio.sleep(grace_s)

    if _sense_voice_model is not None:  # 已被首个 ASR 请求抢先加载
        return

    import tempfile
    import wave

    print("  ASR: SenseVoiceSmall 后台预热中（不阻塞启动）...")
    try:
        model = await asyncio.to_thread(_get_sense_voice)

        # dummy 推理：逼 ONNX 建好计算图与内存池。
        # 实测首次推理 6.2s、第二次仅 0.19s，预热可把这 6s 从用户首次录音中省掉。
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            dummy = tmp.name
        try:
            with wave.open(dummy, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 8000)  # 0.5s 静音
            await asyncio.to_thread(model, [dummy], language="auto", use_itn=True)
        finally:
            Path(dummy).unlink(missing_ok=True)

        print("  ASR: 后台预热完成，语音输入已就绪")
    except Exception as exc:
        # 不抛出：预热失败不应影响已启动的服务；首个 ASR 请求会自行重试加载
        _SENSE_VOICE_STATE.update(state="failed", error=str(exc))
        print(f"  ASR: 后台预热失败 ({exc})")



_VOICE_DIR = Path(__file__).parent / "data" / "voice"
_VOICE_TTS_DIR = _VOICE_DIR / "tts"
_VOICE_REC_DIR = _VOICE_DIR / "recordings"
_VOICE_REFS_DIR = _VOICE_DIR / "refs"


def _voice_hash(text: str, voice: str, speed: float) -> str:
    """Stable cache key for TTS (text + voice + speed)."""
    import hashlib
    payload = f"{text}|{voice}|{speed:.1f}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _voice_find(name: str) -> Path | None:
    """Find a voice file across subdirectories (tts/ or recordings/)."""
    for d in (_VOICE_TTS_DIR, _VOICE_REC_DIR, _VOICE_REFS_DIR):
        p = d / f"{name}.wav"
        if p.exists():
            return p
    # Legacy: also check the root voice dir
    p = _VOICE_DIR / f"{name}.wav"
    return p if p.exists() else None


async def handle_tts(request: web.Request) -> web.Response:
    """POST /api/tts  — text-to-speech via MiMo TTS.

    Body: ``{"text": "...", "voice": "茉莉", "speed": 1.0}``
    Returns: ``{"url": "/voice/tts_{hash}.wav", "duration_ms": 2340}``

    Caches by (text + voice + speed) hash so identical requests hit disk.
    """
    import base64
    import hashlib

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text is required"}, status=400)
    voice = body.get("voice", "茉莉")
    speed = float(body.get("speed", 1.0))

    # ── Cache check ──────────────────────────────────────────────────
    _VOICE_TTS_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = _voice_hash(text, voice, speed)
    cache_path = _VOICE_TTS_DIR / f"tts_{cache_key}.wav"
    if cache_path.exists():
        return web.json_response({
            "url": f"/voice/tts_{cache_key}.wav",
            "voice": voice,
            "cached": True,
        })

    # ── Call MiMo TTS ────────────────────────────────────────────────
    config = request.app.get("config")
    if not config:
        return web.json_response({"error": "config not available"}, status=503)

    import aiohttp
    is_clone = voice.startswith("clone_")
    tts_opts = {"speed": speed}
    if is_clone:
        tts_opts["voice_id"] = voice
    else:
        tts_opts["voice"] = voice
    tts_body = {
        "model": "mimo-v2.5-tts",
        "messages": [
            {"role": "user", "content": "请用自然的声音朗读以下内容"},
            {"role": "assistant", "content": text},
        ],
        "stream": False,
        "extra_body": {"tts_options": tts_opts},
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.api_base}/chat/completions",
                json=tts_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    return web.json_response(
                        {"error": f"TTS API error: {resp.status}", "detail": err[:200]},
                        status=502,
                    )
                data = json.loads(await resp.text())
    except Exception as exc:
        return web.json_response({"error": f"TTS request failed: {exc}"}, status=502)

    choices = data.get("choices", [])
    if not choices:
        return web.json_response({"error": "TTS returned no data"}, status=502)

    audio_b64 = choices[0].get("message", {}).get("audio", {}).get("data", "")
    if not audio_b64:
        return web.json_response({"error": "TTS returned no audio"}, status=502)

    # Strip optional data URL prefix
    if "," in audio_b64:
        audio_b64 = audio_b64.split(",", 1)[1]

    wav_bytes = base64.b64decode(audio_b64)
    cache_path.write_bytes(wav_bytes)

    return web.json_response({
        "url": f"/voice/tts_{cache_key}.wav",
        "voice": voice,
        "cached": False,
    })


async def _asr_via_api(config, wav_path: str, model: str) -> dict:
    """Cloud ASR via the bound console ASR slot. Two protocols:
      ``mimo-`` prefix  → chat/completions + input_audio (MiMo multimodal)
      anything else     → OpenAI whisper-compatible /audio/transcriptions

    Credentials follow the TTS same-key fallback: ``asr_api_key/base`` from
    the console binding, else the main LLM ``api_key/api_base``.

    Raises on upstream failure/timeout; the caller maps it to HTTP 502.
    Returns ``{"text", "emotion", "language"}`` — emotion is always empty
    (cloud ASR has no emotion granularity; the frontend handles that).
    """
    import base64

    import aiohttp

    key = config.asr_api_key or config.api_key
    base = (config.asr_api_base or config.api_base).rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    timeout = aiohttp.ClientTimeout(total=30)

    # ── MiMo multimodal channel (chat/completions + input_audio) ──────
    if model.startswith("mimo-"):
        with open(wav_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:audio/wav;base64,{audio_b64}",
                            },
                        }
                    ],
                }
            ],
            "stream": False,
            "extra_body": {"asr_options": {"language": "auto"}},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/chat/completions",
                json=body,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"ASR API error: {resp.status} {err[:200]}")
                data = json.loads(await resp.text())
        choices = data.get("choices", [])
        text = (choices[0].get("message", {}).get("content", "") or "").strip()
        if not text:
            raise RuntimeError("ASR API returned empty text")
        return {"text": text, "emotion": "", "language": "auto"}

    # ── Whisper-compatible channel (/audio/transcriptions, multipart) ──
    with open(wav_path, "rb") as f:
        audio_bytes = f.read()
    content_type = "audio/webm" if wav_path.endswith(".webm") else "audio/wav"
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=Path(wav_path).name,
                   content_type=content_type)
    form.add_field("model", model)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base}/audio/transcriptions",
            data=form,
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise RuntimeError(f"ASR API error: {resp.status} {err[:200]}")
            data = json.loads(await resp.text())
    text = (data.get("text") or "").strip()
    if not text:
        raise RuntimeError("ASR API returned empty text")
    return {"text": text, "emotion": "", "language": "auto"}


async def handle_asr(request: web.Request) -> web.Response:
    """POST /api/asr  — speech-to-text.

    Accepts ``multipart/form-data`` with a ``file`` field (WAV/WebM).
    Returns ``{"text": "转写结果", "emotion": "HAPPY", "language": "zh"}``.

    Channel: if the console ASR slot is bound (``config.asr_model`` non-empty),
    audio goes to the cloud ASR API; otherwise the local SenseVoiceSmall
    engine handles it. The emotion field is optional — it may be empty for
    non-speech audio.
    """
    import re
    import tempfile

    reader = await request.multipart()
    file_field = None
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            file_field = field
            break
        field = await reader.next()

    if file_field is None:
        return web.json_response({"error": "未找到音频文件"}, status=400)

    audio_bytes = b""
    while True:
        chunk = await file_field.read_chunk(65536)
        if not chunk:
            break
        audio_bytes += chunk

    if not audio_bytes:
        return web.json_response({"error": "音频文件为空"}, status=400)

    # Write to temp file (SenseVoiceSmall needs a file path)
    suffix = ".wav"
    if file_field.filename and file_field.filename.endswith(".webm"):
        suffix = ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    config = request.app.get("config")
    asr_model = (getattr(config, "asr_model", "") or "").strip() if config else ""

    if asr_model:
        # ── Cloud channel: console ASR slot bound ─────────────────────
        try:
            result = await _asr_via_api(config, tmp_path, asr_model)
        except Exception as exc:
            return web.json_response({"error": f"ASR failed: {exc}"}, status=502)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return web.json_response(result)

    try:
        # Local SenseVoice channel.
        # Load in thread too — first call imports funasr_onnx (numpy chain),
        # which must never run on the event loop (GIL/import-lock freeze).
        model = await asyncio.to_thread(_get_sense_voice)
        # Inference in thread (funasr_onnx is sync, avoid blocking event loop)
        result = await asyncio.to_thread(
            model, [tmp_path], language="auto", use_itn=True,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not result or not result[0]:
        return web.json_response({"error": "ASR failed"}, status=502)

    raw = str(result[0])
    # Parse structured output: <|lang|><|emotion|><|event|><|itn|>text
    m = re.match(r'<\|(.+?)\|><\|(.+?)\|><\|(.+?)\|><\|(.+?)\|>(.*)', raw)
    if m:
        lang = m.group(1)
        emotion = m.group(2) if m.group(2) not in ("EMO_UNKNOWN", "") else ""
        text = m.group(5).strip()
        return web.json_response({
            "text": text,
            "emotion": emotion,
            "language": lang,
        })
    else:
        # Fallback: return raw output as text
        return web.json_response({"text": raw.strip()})


async def handle_voice_upload(request: web.Request) -> web.Response:
    """POST /api/voice/upload — persist a user recording.

    Accepts ``multipart/form-data`` with a ``file`` field (WAV).
    Returns ``{"url": "/voice/rec_{id}.wav", "duration_ms": 5000}``.
    """
    import uuid
    import struct

    reader = await request.multipart()
    file_field = None
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            file_field = field
            break
        field = await reader.next()

    if file_field is None:
        return web.json_response({"error": "未找到音频文件"}, status=400)

    audio_bytes = b""
    while True:
        chunk = await file_field.read_chunk(65536)
        if not chunk:
            break
        audio_bytes += chunk

    if not audio_bytes or len(audio_bytes) < 44:
        return web.json_response({"error": "音频文件无效"}, status=400)

    # Parse WAV header for duration (16kHz, 16-bit, mono expected)
    duration_ms = 0
    try:
        riff, size, wave = struct.unpack_from("<4sI4s", audio_bytes, 0)
        if riff == b"RIFF" and wave == b"WAVE":
            # Read fmt chunk for sample rate and channels
            fmt_offset = 12
            while fmt_offset < min(size + 8, 256):
                chunk_id, chunk_size = struct.unpack_from("<4sI", audio_bytes, fmt_offset)
                if chunk_id == b"fmt ":
                    audio_fmt, channels, sample_rate = struct.unpack_from(
                        "<HHI", audio_bytes, fmt_offset + 8
                    )
                    byte_rate, block_align, bits = struct.unpack_from(
                        "<IHH", audio_bytes, fmt_offset + 16
                    )
                    data_size = size - (fmt_offset + 8 + chunk_size) + 8  # rough
                    if byte_rate > 0:
                        duration_ms = int((data_size / byte_rate) * 1000)
                    break
                fmt_offset += 8 + chunk_size
    except Exception:
        pass  # Use default duration_ms=0 if header parse fails

    file_id = f"rec_{uuid.uuid4().hex[:12]}"
    _VOICE_REC_DIR.mkdir(parents=True, exist_ok=True)
    dest = _VOICE_REC_DIR / f"{file_id}.wav"
    dest.write_bytes(audio_bytes)

    return web.json_response({
        "url": f"/voice/{file_id}.wav",
        "duration_ms": duration_ms,
        "size": len(audio_bytes),
    })


async def handle_voice_serve(request: web.Request) -> web.Response:
    """GET /voice/{id}.wav — serve cached voice audio."""
    file_id = request.match_info["id"]
    path = _voice_find(file_id)
    if not path:
        return web.json_response({"error": "voice file not found"}, status=404)
    return web.FileResponse(path=path)


# ── Console (provider/slot management) ─────────────────────────────────

from provider_store import (  # noqa: E402 (lightweight, no cycles)
    ProviderInUseError,
    SLOTS,
    guess_model_kind,
)


def _console_store(request: web.Request):
    """App's ProviderStore; lazily built in degraded runs that have a config."""
    store = request.app.get("provider_store")
    if store is not None:
        return store
    config = request.app.get("config")
    if config is None:
        raise web.HTTPServiceUnavailable(
            text="console unavailable: no config in this run mode")
    from provider_store import ProviderStore
    store = ProviderStore(config.providers_path)
    request.app["provider_store"] = store
    return store


def _console_apply(store, request: web.Request) -> dict:
    """Hot-apply console bindings to the shared Config; no-op without config."""
    config = request.app.get("config")
    return store.apply_to_config(config) if config is not None else {}


async def handle_console_config(request: web.Request) -> web.Response:
    """GET /api/console/config — providers (keys masked) + slots + effective resolution."""
    store = _console_store(request)
    return web.json_response(store.snapshot(request.app["config"]))


async def handle_provider_save(request: web.Request) -> web.Response:
    """POST /api/console/providers — create/edit. api_key empty on edit = keep old."""
    store = _console_store(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    try:
        entry = store.upsert_provider(body)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    # The edited provider may be bound to a slot — re-apply so key/base edits hot-switch
    applied = _console_apply(store, request)
    masked = dict(entry)
    masked["api_key"] = store.mask_key(store.reveal_key(entry["id"]))
    return web.json_response({"ok": True, "provider": masked, "applied": applied})


async def handle_provider_delete(request: web.Request) -> web.Response:
    """DELETE /api/console/providers/{id} — refused (409) while a slot binds it."""
    store = _console_store(request)
    try:
        store.delete_provider(request.match_info["provider_id"])
    except ProviderInUseError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    return web.json_response({"ok": True})


async def handle_slot_switch(request: web.Request) -> web.Response:
    """PUT /api/console/slots/{slot} — bind provider+model (provider_id="" unbinds).

    Hot-applies to the shared Config instance; the next request from any
    consumer (OpenAIClient/bookgraph/tts) picks it up. Streaming responses
    already in flight are unaffected.
    """
    store = _console_store(request)
    slot = request.match_info["slot"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    try:
        entry = store.set_slot(
            slot,
            provider_id=body["provider_id"] if "provider_id" in body else None,
            model=body.get("model"),
            params=body.get("params"),
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    applied = _console_apply(store, request)
    return web.json_response({"ok": True, "slot": entry, "applied": applied})


_FRIENDLY_HTTP_ERRORS = {
    401: "认证失败：密钥无效或过期",
    403: "禁止访问：密钥无此端点权限",
    404: "端点不存在：检查 base_url 是否需要以 /v1 结尾",
}


def _console_test_detail(kind: str, text: str) -> str:
    """One-line human detail from a successful probe response body."""
    try:
        data = json.loads(text)
    except Exception:
        return ""
    if kind == "embedding":
        items = data.get("data") or []
        dims = len(items[0].get("embedding", [])) if items else 0
        return f"embedding dims={dims}"
    if kind == "tts":
        return f"models={len(data.get('data') or [])}"
    return "chat ok"


async def handle_provider_test(request: web.Request) -> web.Response:
    """POST /api/console/providers/{id}/test — probe connectivity (10s timeout).

    llm: 1-token chat completion; embedding: real single embed; tts: GET /models
    (synthesis is heavy — auth + reachability is what a key test needs).
    Model precedence: request body > slot binding > provider's cached list.
    """
    store = _console_store(request)
    provider = store.get_provider(request.match_info["provider_id"])
    if provider is None:
        return web.json_response({"error": "provider not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Model-first: the probe targets one model; kind derives from it
    # (body > any slot bound to this provider > first cached model).
    slot_model = ""
    for s in SLOTS:
        slot = store.get_slot(s)
        if slot.get("provider_id") == provider["id"] and slot.get("model"):
            slot_model = slot["model"]
            break
    model = str(body.get("model") or slot_model
                or ((provider.get("models") or [{}])[0].get("id") or ""))
    kind = (str(body.get("kind") or "").strip()
            or store.model_kind(provider["id"], model)
            or guess_model_kind(model))

    base = provider["base_url"].rstrip("/")
    headers = {
        "Authorization": f"Bearer {store.reveal_key(provider['id'])}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=10)
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            if kind == "llm":
                payload = {"model": model,
                           "messages": [{"role": "user", "content": "ping"}],
                           "max_tokens": 1, "stream": False}
                async with sess.post(f"{base}/chat/completions", json=payload,
                                     headers=headers) as resp:
                    status, text = resp.status, await resp.text()
            elif kind == "embedding":
                payload = {"model": model, "input": ["ping"]}
                async with sess.post(f"{base}/embeddings", json=payload,
                                     headers=headers) as resp:
                    status, text = resp.status, await resp.text()
            else:  # tts
                async with sess.get(f"{base}/models", headers=headers) as resp:
                    status, text = resp.status, await resp.text()
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "timeout after 10s"})
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    latency_ms = round((time.monotonic() - t0) * 1000)
    if status in (200, 201):
        return web.json_response({"ok": True, "status": status,
                                  "latency_ms": latency_ms,
                                  "detail": _console_test_detail(kind, text)})
    friendly = _FRIENDLY_HTTP_ERRORS.get(status)
    out: dict = {"ok": False, "status": status, "latency_ms": latency_ms,
                 "error": friendly or f"HTTP {status}"}
    if not friendly:
        out["detail"] = text[:200]
    return web.json_response(out)


async def handle_models_fetch(request: web.Request) -> web.Response:
    """POST /api/console/models/fetch — pull GET {base}/models for the form.

    Body: {base_url, api_key?, provider_id?}. Empty key with a provider_id
    resolves the stored key (edit mode). Returns kind-guessed model rows;
    the console lets the user correct kinds before saving. No fool-proofing:
    vendor errors surface verbatim.
    """
    store = _console_store(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    base = str(body.get("base_url") or "").strip().rstrip("/")
    key = str(body.get("api_key") or "").strip()
    pid = str(body.get("provider_id") or "").strip()
    if not key and pid:
        key = store.reveal_key(pid)
    if not base or not key:
        return web.json_response(
            {"error": "base_url 与 api_key 均为空（编辑模式可只填 base_url）"},
            status=400)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(f"{base}/models", headers=headers) as resp:
                status, text = resp.status, await resp.text()
    except asyncio.TimeoutError:
        return web.json_response(
            {"ok": False, "error": "timeout after 10s"}, status=502)
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=502)
    if status not in (200, 201):
        return web.json_response(
            {"ok": False, "status": status,
             "error": _FRIENDLY_HTTP_ERRORS.get(status) or f"HTTP {status}"},
            status=502)
    try:
        data = json.loads(text)
        ids = [str(m.get("id") or "").strip()
               for m in (data.get("data") or []) if isinstance(m, dict)]
    except Exception:
        return web.json_response(
            {"ok": False, "error": "响应不是合法的 /models JSON"}, status=502)
    models = [{"id": mid, "kind": guess_model_kind(mid)} for mid in ids if mid]
    return web.json_response({"ok": True, "models": models})


# ── Context meter · 上下文水位回查 ───────────────────────────────────────


async def handle_context_usage(request: web.Request) -> web.Response:
    """GET /api/context/usage?session_id=&mode= — ring data on session load.

    Snapshot hit (runtime just finished THIS session) → exact snapshot.
    Otherwise rebuild: system/tools baseline from runtime + conversation
    from persisted messages (real tokenizer counts); totals real when the
    last assistant message carries usage, estimated otherwise.
    """
    runtime = request.app.get("runtime")
    config = request.app.get("config")
    session_service = request.app.get("session_service")
    sid = str(request.query.get("session_id", "")).strip()
    mode = str(request.query.get("mode", "immersive")).strip() or "immersive"
    alert_ratio = getattr(config, "context_alert_ratio", 0.35) if config else 0.35
    model = getattr(config, "model", "") if config else ""

    snap = getattr(runtime, "last_context_usage", None) if runtime else None
    if (snap is not None and sid
            and getattr(runtime, "last_context_usage_session", "") == sid):
        return web.json_response({"ok": True, **snap.to_dict(alert_ratio=alert_ratio)})

    import asyncio as _asyncio
    from content import TokenUsage as _TU
    from context_meter import measure, resolve_profile

    system_prompt = ""
    tools_schema: list[dict] = []
    if runtime is not None:
        try:
            system_prompt = _build_reading_prompt(runtime, mode, "")
            tools_schema = runtime.tool_registry.get_schemas()
        except Exception:
            pass

    history_messages: list[dict] = []
    real_usage = None
    if sid and session_service is not None:
        try:
            loaded = session_service.load_messages(sid)
        except Exception:
            loaded = None
        if loaded is not None:
            for _m in reversed(loaded.messages):
                if _m.usage and (_m.usage.input_tokens or _m.usage.output_tokens):
                    real_usage = _TU(
                        input_tokens=_m.usage.input_tokens,
                        output_tokens=_m.usage.output_tokens,
                    )
                    break
            conv = loaded.messages[-40:]  # cap: proportional counting only
            if runtime is not None:
                try:
                    history_messages = [runtime._msg_to_api_dict(m) for m in conv]
                except Exception:
                    history_messages = []
            if not history_messages:
                history_messages = [
                    {"role": m.role, "content": m.first_text() or ""}
                    for m in conv
                ]

    prof = resolve_profile(model, config)
    snapshot = await _asyncio.to_thread(
        measure,
        system_prompt=system_prompt,
        tools_schema=tools_schema,
        history_messages=history_messages,
        injected_context="",
        user_input="",
        usage=real_usage,
        window=prof["window"],
        model=model,
        tokenizer_path=prof["tokenizer_path"],
        session_id=sid,
    )
    return web.json_response({"ok": True, **snapshot.to_dict(alert_ratio=alert_ratio)})


# ── Console · 用量分析 handlers ──────────────────────────────────────────


def _usage_profiles(request: web.Request):
    """Shared ModelProfileStore for the usage handlers."""
    from model_profiles import get_profiles_store
    return get_profiles_store(request.app.get("config"))


async def handle_usage_profiles_get(request: web.Request) -> web.Response:
    """GET /api/console/usage/profiles — pricing + windows + tokenizer."""
    return web.json_response({"ok": True, "profiles": _usage_profiles(request).get_profiles()})


async def handle_usage_profiles_put(request: web.Request) -> web.Response:
    """PUT /api/console/usage/profiles — replace {models, tokenizer, vendors}."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    try:
        _usage_profiles(request).save_profiles(
            body.get("models") or {}, body.get("tokenizer") or {},
            body.get("vendors") or {})
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    # 路径可能刚被修好/文件刚下载完——清缓存让新配置立即生效，不必重启
    from tokenizer_service import clear_cache
    clear_cache()
    return web.json_response({"ok": True})


def _month_bounds(month: str) -> tuple[int, int] | None:
    """Local-timezone [start_ms, end_ms) for 'YYYY-MM'; None if invalid."""
    try:
        y, m = month.split("-")
        year, mon = int(y), int(m)
        if not 1 <= mon <= 12 or year < 2000:
            return None
    except (ValueError, AttributeError):
        return None
    start = datetime(year, mon, 1)
    end_year, end_mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    end = datetime(end_year, end_mon, 1)
    return (
        int(start.timestamp() * 1000),
        int(end.timestamp() * 1000),
    )


async def handle_usage_overview(request: web.Request) -> web.Response:
    """GET /api/console/usage/overview?month=YYYY-MM — 用量分析页一次取全:
    统计卡 totals + 每日×模型 days(堆叠柱)+ 按模型汇总 by_model(模型卡)。"""
    month = request.query.get("month") or datetime.now().strftime("%Y-%m")
    bounds = _month_bounds(month)
    if bounds is None:
        return web.json_response({"error": "month 需为 YYYY-MM"}, status=400)
    rows = usage_log.get_store().usage_daily_by_model(bounds[0], bounds[1])

    days: dict[str, list] = {}
    by_model: dict[str, dict] = {}
    for r in rows:
        days.setdefault(r["day"], []).append(r)
        m = by_model.setdefault(r["model"], {
            "model": r["model"], "calls": 0, "tokens_in": 0,
            "tokens_out": 0, "errors": 0})
        for k in ("calls", "tokens_in", "tokens_out", "errors"):
            m[k] += r[k]

    # totals 由 by_model 求和派生 → 与 days 恒等,统计卡与图表数字永远一致
    totals = {
        "calls": sum(m["calls"] for m in by_model.values()),
        "tokens_in": sum(m["tokens_in"] for m in by_model.values()),
        "tokens_out": sum(m["tokens_out"] for m in by_model.values()),
        "errors": sum(m["errors"] for m in by_model.values()),
    }
    return web.json_response({
        "ok": True,
        "month": month,
        "totals": totals,
        "days": [{"day": d, "models": days[d]} for d in sorted(days)],
        "by_model": sorted(by_model.values(),
                           key=lambda m: m["calls"], reverse=True),
    })


# ── Startup ─────────────────────────────────────────────────────────────


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
