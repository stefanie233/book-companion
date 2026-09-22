"""Context flight recorder — REST API handlers + aiohttp route installation.

Packaged separately so the recorder can be uninstalled by deleting this
folder and removing the thin integration points (see README.md).
"""

from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web

from flight_recorder.recorder import ContextRecorder

# Directory holding the self-contained viewer page (viewer.html)
_VIEWER_DIR = Path(__file__).resolve().parent


# ── REST handlers ─────────────────────────────────────────────────────


async def handle_context_log_list(request: web.Request) -> web.Response:
    """List sessions that have recorded rounds."""
    recorder = request.app.get("context_recorder")
    if not recorder:
        return web.json_response({"error": "context recorder not enabled"}, status=503)
    sessions = recorder.list_sessions()
    # Merge sessions from the external recorder (when one is wired in)
    hermes_rec = request.app.get("hermes_recorder")
    if hermes_rec:
        for s in hermes_rec.list_sessions():
            s["source"] = "hermes"
            sessions.append(s)
        sessions.sort(key=lambda x: x.get("last_at", ""), reverse=True)
    return web.json_response({"sessions": sessions})


async def handle_context_log_session(request: web.Request) -> web.Response:
    """List rounds for a session (metadata only)."""
    recorder = request.app.get("context_recorder")
    if not recorder:
        return web.json_response({"error": "context recorder not enabled"}, status=503)
    session_id = request.match_info["session_id"]
    rounds = recorder.list_rounds(session_id)
    if not rounds:
        hermes_rec = request.app.get("hermes_recorder")
        if hermes_rec:
            rounds = hermes_rec.list_rounds(session_id)
    return web.json_response({"rounds": rounds})


async def handle_context_log_round(request: web.Request) -> web.Response:
    """Get full content of a single round."""
    recorder = request.app.get("context_recorder")
    if not recorder:
        return web.json_response({"error": "context recorder not enabled"}, status=503)
    session_id = request.match_info["session_id"]
    round_id = int(request.match_info["round_id"])
    # Verify session_id matches to avoid cross-database id collision
    round_data = recorder.get_round(round_id)
    if round_data and round_data.get("session_id") == session_id:
        return web.json_response(round_data)
    # Fallback to the external recorder
    hermes_rec = request.app.get("hermes_recorder")
    if hermes_rec:
        round_data = hermes_rec.get_round(round_id)
        if round_data and round_data.get("session_id") == session_id:
            return web.json_response(round_data)
    return web.json_response({"error": "round not found"}, status=404)


async def handle_context_log_clear(request: web.Request) -> web.Response:
    """Delete records.  Optional ``session_id`` in path scopes the deletion."""
    recorder = request.app.get("context_recorder")
    if not recorder:
        return web.json_response({"error": "context recorder not enabled"}, status=503)
    session_id = request.match_info.get("session_id")
    deleted = recorder.clear(session_id)
    return web.json_response({"deleted": deleted})


# ── Viewer page ───────────────────────────────────────────────────────


async def _handle_viewer(request: web.Request) -> web.Response:
    """Serve the flight recorder viewer (self-contained html)."""
    return web.FileResponse(_VIEWER_DIR / "viewer.html")


async def _handle_context_log_legacy(request: web.Request) -> web.Response:
    """Redirect the old /context-log.html URL to the packaged viewer."""
    raise web.HTTPFound("/flight-recorder/viewer.html")


# ── Installation ──────────────────────────────────────────────────────


def install_context_log_routes(app: web.Application, context_recorder) -> None:
    """Wire the flight recorder into an aiohttp app.

    1. Expose the recorder on the app (the SSE chat handler reads it).
    2. Auto-detect an external session recorder DB (~/.hermes/context_log.db)
       and show its sessions alongside Book Companion ones.
    3. Register the /api/context-log REST routes + serve the viewer page.
    """
    app["context_recorder"] = context_recorder

    # Auto-detect the external recorder's database (~/.hermes/context_log.db)
    # 可用 BOOK_COMPANION_HERMES_DB 覆盖（换位置或指向不存在路径即禁用它）。
    env_db = os.environ.get("BOOK_COMPANION_HERMES_DB", "").strip()
    hermes_db = (Path(env_db).expanduser() if env_db
                 else Path.home() / ".hermes" / "context_log.db")
    if hermes_db.exists():
        try:
            app["hermes_recorder"] = ContextRecorder(hermes_db)
        except Exception:
            app["hermes_recorder"] = None
    else:
        app["hermes_recorder"] = None

    # REST API
    app.router.add_get("/api/context-log", handle_context_log_list)
    app.router.add_get("/api/context-log/{session_id}", handle_context_log_session)
    app.router.add_get("/api/context-log/{session_id}/{round_id}", handle_context_log_round)
    app.router.add_delete("/api/context-log", handle_context_log_clear)
    app.router.add_delete("/api/context-log/{session_id}", handle_context_log_clear)

    # Viewer page + legacy URL redirect
    app.router.add_get("/flight-recorder/viewer.html", _handle_viewer)
    app.router.add_get("/context-log.html", _handle_context_log_legacy)
