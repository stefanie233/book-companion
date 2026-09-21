"""ToolSearch — BM25-based deferred tool discovery.

When the LLM needs a tool that isn't in the visible list, it calls
``tool_search`` which triggers a BM25 search over deferred tools.

Supports both English and Chinese queries via jieba tokenization.
Falls back to simple whitespace tokenization if dependencies are missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolSearchEntry:
    """Lightweight entry for BM25 indexing — name, description, toolset, and category."""
    name: str
    description: str
    toolset: str
    category: str = ""


class ToolSearchEngine:
    """BM25 search engine for deferred tool discovery.

    Builds a full-text index over deferred tool names, descriptions,
    and toolset labels. Caches the index after first build.
    """

    def __init__(self, deferred_tools: list[Any], language: str = "zh"):
        """Initialize with deferred ToolSpec or ToolSearchEntry list.

        ``deferred_tools`` accepts either ``ToolSpec`` or ``ToolSearchEntry``
        objects — both have ``name``, ``description``, and ``toolset`` attrs.
        """
        self._tools = deferred_tools
        self._language = language
        self._bm25 = None
        self._build_index()

    # ── Tokenization ────────────────────────────────────────────────────

    def _tokenize(self, text: str) -> list[str]:
        """Tokenize text for BM25.

        Uses jieba for Chinese (language="zh"), simple split for English.
        Falls back gracefully if jieba is not installed.
        """
        if self._language == "zh":
            try:
                import jieba
                return list(jieba.cut(text))
            except ImportError:
                pass
        return text.lower().split()

    # ── Index building ──────────────────────────────────────────────────

    def _build_index(self) -> None:
        """Build BM25 index from deferred tool descriptions."""
        if not self._tools:
            return
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            return

        corpus: list[str] = []
        for t in self._tools:
            name = getattr(t, "name", "")
            desc = getattr(t, "description", "")
            ts = getattr(t, "toolset", "")
            cat = getattr(t, "category", "")
            # Combine all fields into one document for BM25
            doc = f"{name} {desc} {ts} {cat}"
            corpus.append(doc)

        tokenized = [self._tokenize(doc) for doc in corpus]
        self._bm25 = BM25Okapi(tokenized) if tokenized else None

    # ── Search ──────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search for matching deferred tools. Returns ranked list of dicts.

        Each result has: ``name``, ``description``, ``toolset``, ``score``.
        Empty list if BM25 is unavailable or no tools match.
        """
        if not self._bm25 or not self._tools:
            return []

        tokens = self._tokenize(query)
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

        results: list[dict[str, Any]] = []
        for idx, score in ranked[:limit]:
            if score <= 0:
                continue
            t = self._tools[idx]
            results.append({
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", ""),
                "toolset": getattr(t, "toolset", ""),
                "category": getattr(t, "category", ""),
                "score": round(score, 3),
            })
        return results
