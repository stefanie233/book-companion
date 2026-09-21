"""Token counting for console 用量分析 — HuggingFace tokenizers + fallback.

Loads a tokenizer.json lazily and caches it **per path** (multi-vendor:
switching models between 智谱/DeepSeek/MiMo no longer reloads a 6-20MB
file on every turn). Any failure — library missing, file corrupt, path
wrong — degrades to the project's long-held chars//4 estimate, flagged
via the ``estimated`` return value.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_cache: dict[str, Any] = {}  # path -> Tokenizer | None（None = 加载失败，负缓存）


def _load(path: str):
    """Import + load on demand; returns the tokenizer or None."""
    if path in _cache:
        return _cache[path]
    tok = None
    try:
        from tokenizers import Tokenizer  # lazy: optional dependency

        tok = Tokenizer.from_file(path)
    except Exception as exc:
        logger.warning("tokenizer load failed for %s: %s", path, exc)
        tok = None
    _cache[path] = tok
    return tok


def clear_cache() -> None:
    """Drop all cached tokenizers (profile edits call this so a fixed
    path or newly downloaded file takes effect without a restart)."""
    _cache.clear()


def count_tokens(text: str, tokenizer_path: str) -> tuple[int, bool]:
    """Return ``(tokens, estimated)``. ``estimated=True`` means the
    chars//4 fallback answered because the real tokenizer is unusable."""
    if not text:
        return 0, False
    tok = _load(tokenizer_path) if tokenizer_path else None
    if tok is None:
        return len(text) // 4, True
    try:
        return len(tok.encode(text).ids), False
    except Exception:
        return len(text) // 4, True
