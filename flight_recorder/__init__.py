"""Context flight recorder — packaged as an uninstallable unit.

Usage:
    from flight_recorder import create_recorder, install_context_log_routes

    recorder = create_recorder(config.project_dir)      # None on failure
    app["context_recorder"] = ...                        # or install_context_log_routes(app, recorder)

To uninstall: delete this folder + remove the thin integration points
listed in README.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flight_recorder.recorder import ContextRecorder
from flight_recorder.server import install_context_log_routes

__all__ = ["ContextRecorder", "create_recorder", "install_context_log_routes"]


def create_recorder(project_dir: str | Path) -> ContextRecorder | None:
    """Build the recorder backed by ``data/context_log.db`` (best-effort).

    Returns None if the recorder cannot be constructed — the recorder is a
    debug aid and must never break startup.
    """
    try:
        return ContextRecorder(Path(project_dir) / "data" / "context_log.db")
    except Exception:
        return None
