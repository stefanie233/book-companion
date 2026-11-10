"""Memory subsystem file logging setup.

The L0-L3 memory pipeline and wingman provider already emit structured logs
via the stdlib ``logging`` module — but until now nothing attached a file
handler.  With ``start.bat`` running the agent in a separate window, pipeline
activity (or its silent failure) was invisible after the fact.

Design notes (deliberately not path-dependent on the old per-day
``data/logs/2026/MM/DD/wingman.txt`` scheme):
  - Single rotating file ``data/logs/memory.log`` (5 MB x 3): pipeline logs
    are low-volume (a few lines per turn), so one file is easier to tail and
    grep than per-day/per-module directories.
  - Handler attaches to *parent* loggers only (memory_pipeline, wingman, ...);
    child modules inherit it through the stdlib logger hierarchy.
  - Idempotent: safe to call once from any single entry point.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Parent loggers owned by the memory subsystem. Children (e.g.
# memory_pipeline.scheduler, wingman.service) inherit the handler via the
# standard logger hierarchy, so a handful of lines here covers every module.
_MEMORY_LOGGERS = (
    "memory_pipeline",
    "wingman",
    "memory_manager",
    "memory_store",
    "context_recorder",
)

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 3


def setup_memory_logging(
    log_dir: Path | None = None,
    *,
    level: int = logging.INFO,
    enabled: bool = True,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> Path | None:
    """Attach a rotating file handler to the memory subsystem loggers.

    Returns the resolved log file path, or None if disabled.

    Idempotent: if any memory logger already writes to ``memory.log``, the
    call is a no-op (returns the existing path) instead of stacking handlers.
    """
    if not enabled:
        return None

    if log_dir is None:
        from runtime_paths import data_path
        log_dir = data_path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "memory.log"

    # Already wired? Don't stack a second handler on every logger.
    for name in _MEMORY_LOGGERS:
        lg = logging.getLogger(name)
        if any(
            isinstance(h, RotatingFileHandler)
            and str(h.baseFilename) == str(log_path)
            for h in lg.handlers
        ):
            return log_path

    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    for name in _MEMORY_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.addHandler(handler)

    logging.getLogger(__name__).info(
        "Memory file logging -> %s (level=%s)",
        log_path,
        logging.getLevelName(level),
    )
    return log_path


def setup_memory_logging_from_config(config: object) -> Path | None:
    """Convenience wrapper: read enabled/level from a Config-like object.

    Missing attributes fall back to sane defaults so callers don't need to
    care whether the config dataclass carries the new fields.
    """
    level_name = str(getattr(config, "memory_log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    from runtime_paths import user_root

    project_dir = getattr(config, "project_dir", None) or user_root()
    return setup_memory_logging(
        Path(project_dir) / "data" / "logs",
        level=level,
        enabled=bool(getattr(config, "memory_log_enabled", True)),
    )
