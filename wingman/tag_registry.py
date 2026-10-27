"""Tag registry — folksonomy-style self-growing tag system.

Tags have three categories (emotion / theme / behavior). New tags are
classified by the LLM; the registry stores and queries them.

The suggest_tags() method is the LLM integration point — it will be
called by the wingman during moment writing (Phase 3).
"""

from __future__ import annotations

import logging
from typing import Any

from llm_json import loads_tolerant
from wingman.db import WingmanDatabase, _now_ms, _new_id
from wingman.models import TagRow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM prompt for tag suggestion (Phase 3 integration point)
# ---------------------------------------------------------------------------

TAG_SUGGESTION_PROMPT = """\
Given the following user message or moment description, suggest 1-3 tags.
Each tag must have:
  - name: short label (Chinese or English, max 10 chars)
  - category: one of "emotion", "theme", or "behavior"
  - reply_style: one-line hint for how the agent should respond when this tag is active

Categories:
  - emotion: feeling/emotional states (e.g. "curious", "confused", "excited")
  - theme: topics/themes being discussed (e.g. "magical realism", "existentialism")
  - behavior: user interaction patterns (e.g. "deep questioner", "dislikes small talk")

Existing tags (avoid duplicates, prefer reusing existing):
__EXISTING_TAGS__

Text:
__TEXT__

Return ONLY a JSON array (no markdown, no extra text):
[{{"name": "...", "category": "...", "reply_style": "..."}}]
"""

# ---------------------------------------------------------------------------
# TagRegistry
# ---------------------------------------------------------------------------


class TagRegistry:
    """Manage the folksonomy tag system for moments."""

    def __init__(self, db: WingmanDatabase):
        self._db = db

    # -- CRUD ---------------------------------------------------------------

    def register(
        self,
        name: str,
        category: str = "theme",
        reply_style: str = "",
        behavior: str = "normal",
        behavior_prompt: str = "",
        cite_threshold: int = 10,
        default_weight: float = 1.0,
    ) -> str:
        """Create a new tag. Returns tag_id.

        If the tag already exists, increments usage_count and returns existing id.
        New v6 fields (behavior, behavior_prompt, cite_threshold, default_weight)
        are only applied on first creation; re-registering an existing tag does
        not overwrite its behavior settings.
        """
        existing = self._db.get_tag_by_name(name)
        if existing:
            self._db.increment_tag_usage(existing.id)
            return existing.id

        tag = TagRow(
            id=_new_id("tag"),
            name=name,
            category=category,
            reply_style=reply_style,
            behavior=behavior,
            behavior_prompt=behavior_prompt,
            cite_threshold=cite_threshold,
            default_weight=default_weight,
            usage_count=0,
            created_at=_now_ms(),
        )
        self._db.insert_tag(tag)
        return tag.id

    def get(self, tag_id: str) -> TagRow | None:
        conn = self._db._get_conn()
        cur = conn.execute("SELECT * FROM tag_registry WHERE id = ?", (tag_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return self._db._row_to_tag(row)

    def list_all(self, category: str | None = None) -> list[TagRow]:
        return self._db.list_tags(category)

    def get_by_name(self, name: str) -> TagRow | None:
        return self._db.get_tag_by_name(name)

    # -- LLM integration point (Phase 3) ------------------------------------

    def build_suggestion_prompt(self, text: str) -> str:
        """Build the prompt for LLM-driven tag suggestion."""
        existing = self.list_all()
        existing_str = (
            "\n".join(
                f"  - {t.name} ({t.category}): {t.reply_style}"
                for t in existing[:20]
            )
            if existing
            else "  (no existing tags yet)"
        )

        return (
            TAG_SUGGESTION_PROMPT
            .replace("__EXISTING_TAGS__", existing_str)
            .replace("__TEXT__", text[:2000])
        )

    def register_from_llm_output(self, llm_json: str) -> list[str]:
        """Parse LLM tag suggestions and register new ones.

        Returns list of tag_ids (including existing ones for reused names).

        阶梯与裸引号修复统一在 ``llm_json.loads_tolerant``；既接受对象也接受数组，
        故不传 ``expect``。
        """
        parsed = loads_tolerant(llm_json)
        if parsed is None:
            logger.warning("No parseable JSON in LLM tag output")
            return []
        suggestions = [parsed] if isinstance(parsed, dict) else parsed
        if not isinstance(suggestions, list):
            logger.warning("LLM tag output is neither object nor array")
            return []

        tag_ids = []
        for s in suggestions[:3]:
            name = (s.get("name", "")).strip()
            if not name:
                continue
            category = s.get("category", "theme")
            if category not in ("emotion", "theme", "behavior"):
                category = "theme"
            reply_style = s.get("reply_style", "")

            tag_id = self.register(name, category, reply_style)
            tag_ids.append(tag_id)

        return tag_ids

    # -- Moment linkage -----------------------------------------------------

    def link_to_moment(self, moment_id: str, tag_ids: list[str]) -> None:
        """Associate tags with a moment."""
        for tag_id in tag_ids:
            self._db.link_moment_tag(moment_id, tag_id)
            self._db.increment_tag_usage(tag_id)

    def get_tags_for_moment(self, moment_id: str) -> list[TagRow]:
        return self._db.get_tags_for_moment(moment_id)
