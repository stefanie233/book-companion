"""Review + Recall hooks.

Registers two handlers (currently disabled — pending redesign):
  - on_session_start: batch review of unreviewed past sessions
  - pre_turn: recall detection — when user asks about history, search
    moments + sessions.keywords and inject matching memories

The old handlers referenced deleted wingman subsystems (ReviewService,
MomentService, query_moments_by_keywords).  Registration is commented
out.  When hooks are re-enabled, the handlers must be rewritten against
the current memory API (memory_search / memory_read / review_sessions).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Recall intent patterns (rule-based first pass)
# ---------------------------------------------------------------------------

_RECALL_PATTERNS: list[str] = [
    # Chinese
    r"上次", r"之前聊", r"记得吗", r"回顾", r"那次", r"以前说",
    r"上回", r"前一次", r"聊过", r"讨论过", r"提到过", r"说过",
    r"想起来", r"回忆", r"过去", r"之前有", r"前面聊", r"早先",
    # English
    r"remember", r"recall", r"last time", r"before",
    r"previous", r"earlier", r"discussed",
]

_MOMENT_TYPE_LABELS: dict[str, str] = {
    "preference": "偏好",
    "insight": "洞察",
    "correction": "修正",
    "boundary": "边界",
}


def _detect_recall_intent(user_input: str) -> bool:
    """Check if user input signals intent to recall past conversations."""
    if not user_input:
        return False
    return any(re.search(p, user_input) for p in _RECALL_PATTERNS)


def _extract_search_keywords(user_input: str) -> list[str]:
    """Extract potential search keywords from user input.

    Simple n-gram extraction for Chinese + word split for English.
    Returns up to 5 keywords, longest first.
    """
    keywords: list[str] = []
    seen: set[str] = set()

    # Chinese: 2-4 char n-grams containing CJK
    cleaned = re.sub(r"[^一-鿿\w]", "", user_input)
    for n in (4, 3, 2):
        for i in range(len(cleaned) - n + 1):
            ng = cleaned[i:i + n]
            if ng not in seen and re.search(r"[一-鿿]", ng):
                seen.add(ng)
                keywords.append(ng)

    # English words >= 3 chars
    for word in re.findall(r"[a-zA-Z]{3,}", user_input):
        if word.lower() not in seen:
            seen.add(word.lower())
            keywords.append(word.lower())

    keywords.sort(key=len, reverse=True)
    return keywords[:5]


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------


def register_hooks(pipeline) -> None:
    """Register on_session_start and pre_turn handlers.

    Currently both handlers are disabled.  Their old implementations were
    removed when the wingman dormant subsystems (ReviewService, MomentService,
    context_brief, etc.) were deleted (2026-06).

    When re-enabled, the handlers should use the current memory API:
      - memory_search / memory_read for recall
      - review_sessions tool (sub-agent driven) for batch review
    """

    # on_session_start disabled — batch review moved to review_sessions agent tool
    # pre_turn disabled — recall now driven by memory_search tool
