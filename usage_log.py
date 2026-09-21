"""Unified LLM usage logging — the data source behind console 用量分析.

Every LLM call funnels into :func:`record` from two non-overlapping
points: ``OpenAIClient.stream``/``complete`` (chat, sub-agent, memory
pipeline, enrichment, archive) and ``bookgraph.logging.log_llm_call``
(extraction bypasses OpenAIClient). Attribution comes from ContextVars
so each pipeline opts in with one ``set_source`` line at its entry.

Best-effort by contract: a logging failure must never break the calling
pipeline, so everything is swallowed.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path

from storage.db_base import _now_ms
from storage.db_session import SessionDB

log = logging.getLogger(__name__)

_SOURCE: ContextVar[str] = ContextVar("usage_source", default="chat")
_REF_ID: ContextVar[str] = ContextVar("usage_ref_id", default="")

_store: SessionDB | None = None


def set_source(source: str, ref_id: str = "") -> Token:
    """Attribute LLM calls in this context to ``source`` until the token
    is reset. Call at pipeline entry: ``token = set_source("memory")``."""
    _REF_ID.set(ref_id)
    return _SOURCE.set(source)


@contextmanager
def source_scope(source: str, ref_id: str = ""):
    """Scoped attribution — restores the previous source on exit, safe to
    nest (a sub-agent inside the main loop, a pipeline inside a tool)."""
    t_src = _SOURCE.set(source)
    t_ref = _REF_ID.set(ref_id)
    try:
        yield
    finally:
        _SOURCE.reset(t_src)
        _REF_ID.reset(t_ref)


def init_for_tests(path: Path) -> None:
    """Point the singleton at a test database (scripts/test_usage_log.py)."""
    global _store
    _store = SessionDB(Path(path))
    _store.initialize()


def get_store() -> SessionDB:
    """Lazy shared SessionDB — resolved relative to the project root so
    both CLI (main.py) and api_server shapes land on data/sessions.db."""
    global _store
    if _store is None:
        _store = SessionDB(Path(__file__).parent / "data" / "sessions.db")
        _store.initialize()
    return _store


def record(
    *,
    model: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    status: str = "ok",
    source: str | None = None,
    ref_id: str | None = None,
    dedup_key: str | None = None,
) -> None:
    """Append one usage row. Never raises; source/ref_id default to the
    ContextVars set by the surrounding pipeline."""
    try:
        get_store().insert_usage_log(
            ts=_now_ms(),
            model=model or "",
            source=source if source is not None else _SOURCE.get(),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            session_id=ref_id if ref_id is not None else _REF_ID.get(),
            status=status,
            dedup_key=dedup_key,
        )
    except Exception:
        log.warning("usage_log.record failed", exc_info=True)


