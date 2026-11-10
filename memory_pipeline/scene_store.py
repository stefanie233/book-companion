"""L2 scene block storage — per-book narrative .md files + JSON index.

Each scene block is one book (L2-1: 按书组织), stored as a .md file with a
META header and section-based content (阅读轨迹 / 核心讨论 / 用户的理解 / 待续·延伸).

Directory layout::

    data/scenes/
      scene_index.json         # index of all scene blocks
      局外人.md                # one scene block per book
      _archive/                # cold books moved here (L2-3 冷热分层)

book-centric for Liubai. Pure file I/O — no LLM calls here.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default scene block sections (order preserved in .md output)
DEFAULT_SECTIONS = ["阅读轨迹", "核心讨论", "用户的理解", "待续·延伸"]

# Cold-tiering: books not updated for this many days are archived
DEFAULT_COLD_DAYS = 30

_META_START = "-----META-START-----"
_META_END = "-----META-END-----"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify_book(book: str) -> str:
    """Convert a book name to a safe filename (Windows + POSIX safe)."""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", book).strip("._")
    return cleaned or "unnamed_book"


@dataclass
class SceneBlock:
    """A single L2 scene block (one book)."""

    book: str
    summary: str = ""
    heat: int = 0
    created: str = ""
    updated: str = ""
    filename: str = ""
    status: str = "active"  # active | archived
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return _slugify_book(self.book)

    def to_index_dict(self) -> dict[str, Any]:
        return {
            "book": self.book,
            "summary": self.summary,
            "heat": self.heat,
            "created": self.created,
            "updated": self.updated,
            "filename": self.filename,
            "status": self.status,
        }

    @classmethod
    def from_index_dict(cls, d: dict[str, Any]) -> "SceneBlock":
        return cls(
            book=d.get("book", ""),
            summary=d.get("summary", ""),
            heat=d.get("heat", 0),
            created=d.get("created", ""),
            updated=d.get("updated", ""),
            filename=d.get("filename", ""),
            status=d.get("status", "active"),
        )


class SceneStore:
    """Manages L2 scene blocks on disk: index + .md files + cold archiving.

    Thread-safety: not needed — all calls happen on the asyncio event loop
    (L2 pipeline) or synchronously in tool handlers.
    """

    def __init__(self, scenes_dir: Path, *, cold_days: int = DEFAULT_COLD_DAYS):
        self._dir = Path(scenes_dir)
        self._archive_dir = self._dir / "_archive"
        self._index_path = self._dir / "scene_index.json"
        self._cold_days = cold_days
        self._cache: list[SceneBlock] | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    def initialize(self) -> None:
        """Create the scenes directory if needed."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

    # ── Index I/O ─────────────────────────────────────────────────────

    def load_index(self) -> list[SceneBlock]:
        """Load scene index (cached). Returns list of SceneBlock."""
        if self._cache is not None:
            return self._cache
        if not self._index_path.exists():
            self._cache = []
            return self._cache
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            blocks = [SceneBlock.from_index_dict(d) for d in data.get("scenes", [])]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("SceneStore: failed to load index, starting fresh: %s", e)
            blocks = []
        self._cache = blocks
        return blocks

    def save_index(self, blocks: list[SceneBlock]) -> None:
        """Persist the scene index to disk and refresh cache."""
        self._cache = blocks
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "scenes": [b.to_index_dict() for b in blocks],
        }
        self._index_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Queries ───────────────────────────────────────────────────────

    def list_active(self) -> list[SceneBlock]:
        """Active scene blocks (excludes archived), newest-updated first."""
        blocks = [b for b in self.load_index() if b.status == "active"]
        blocks.sort(key=lambda b: b.updated, reverse=True)
        return blocks

    def get(self, book: str) -> SceneBlock | None:
        """Get a scene block by book name (any status)."""
        for b in self.load_index():
            if b.book == book:
                return b
        return None

    def get_by_filename(self, filename: str) -> SceneBlock | None:
        for b in self.load_index():
            if b.filename == filename:
                return b
        return None

    # ── .md file I/O ──────────────────────────────────────────────────

    def _scene_path(self, block: SceneBlock, *, archived: bool = False) -> Path:
        dir_ = self._archive_dir if archived else self._dir
        return dir_ / (block.filename or f"{block.slug}.md")

    def read_scene_file(self, block: SceneBlock) -> str | None:
        """Read a scene block's .md content, or None if missing."""
        path = self._scene_path(block)
        if not path.exists():
            # Fall back to archive dir
            path = self._scene_path(block, archived=True)
            if not path.exists():
                return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("SceneStore: failed to read %s: %s", path, e)
            return None

    def write_scene_file(self, block: SceneBlock) -> None:
        """Write a scene block's .md file (META header + sections)."""
        block.filename = block.filename or f"{block.slug}.md"
        lines = [
            _META_START,
            f"book: {block.book}",
            f"summary: {block.summary}",
            f"heat: {block.heat}",
            f"created: {block.created}",
            f"updated: {block.updated}",
            f"status: {block.status}",
            _META_END,
            "",
        ]
        for section in DEFAULT_SECTIONS:
            content = (block.sections.get(section) or "").strip()
            lines.append(f"## {section}")
            lines.append("")
            if content:
                lines.append(content)
                lines.append("")
        text = "\n".join(lines).rstrip() + "\n"
        path = self._scene_path(block, archived=(block.status == "archived"))
        self._dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def parse_scene_file(self, text: str) -> dict[str, Any]:
        """Parse a .md scene file into {book, summary, heat, sections, ...}.

        Used by the L2 extractor to feed existing block content to the LLM.
        """
        meta: dict[str, Any] = {}
        sections: dict[str, str] = {}
        current_section: str | None = None
        section_buf: list[str] = []

        in_meta = False
        for line in text.splitlines():
            if line.strip() == _META_START:
                in_meta = True
                continue
            if line.strip() == _META_END:
                in_meta = False
                continue
            if in_meta:
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip()] = val.strip()
                continue
            if line.startswith("## "):
                if current_section and section_buf:
                    sections[current_section] = "\n".join(section_buf).strip()
                current_section = line[3:].strip()
                section_buf = []
            elif current_section:
                section_buf.append(line)
        if current_section and section_buf:
            sections[current_section] = "\n".join(section_buf).strip()

        return {
            "book": meta.get("book", ""),
            "summary": meta.get("summary", ""),
            "heat": int(meta.get("heat", 0) or 0),
            "created": meta.get("created", ""),
            "updated": meta.get("updated", ""),
            "sections": sections,
        }

    # ── Mutation ──────────────────────────────────────────────────────

    def upsert(
        self,
        block: SceneBlock,
        *,
        existing: SceneBlock | None = None,
    ) -> SceneBlock:
        """Insert or update a scene block. Preserves created time + filename.

        heat is incremented by 1 on update (touched by new memories).
        """
        now = _now_iso()
        if existing is not None:
            block.created = existing.created
            block.filename = existing.filename
            block.heat = existing.heat + 1
            block.status = "active"
        else:
            block.created = block.created or now
            block.filename = block.filename or f"{block.slug}.md"
            block.heat = max(block.heat, 0)
        block.updated = now

        # Update in index (replace or append)
        blocks = self.load_index()
        new_blocks = [b for b in blocks if b.book != block.book]
        new_blocks.append(block)
        self.save_index(new_blocks)

        # Write the .md file
        self.write_scene_file(block)
        return block

    def delete(self, book: str) -> None:
        """Remove a scene block from the index (file kept on disk)."""
        blocks = [b for b in self.load_index() if b.book != book]
        self.save_index(blocks)

    # ── Cold tiering (L2-3: 软上限 + 冷热分层) ────────────────────────

    def archive_cold(self, now_iso: str | None = None) -> list[str]:
        """Archive active scene blocks not updated within cold_days.

        Archived blocks are moved to data/scenes/_archive/ and excluded
        from navigation (list_active). This bounds the active set without
        a hard count cap — 活跃不限, 冷书归档.

        Returns list of archived book names.
        """
        try:
            now = datetime.fromisoformat(now_iso or _now_iso())
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except ValueError:
            return []

        archived: list[str] = []
        blocks = self.load_index()
        changed = False
        for b in blocks:
            if b.status != "active" or not b.updated:
                continue
            try:
                updated = datetime.fromisoformat(b.updated)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age_days = (now - updated).days
            except ValueError:
                continue
            if age_days > self._cold_days:
                b.status = "archived"
                archived.append(b.book)
                changed = True

        if changed:
            self.save_index(blocks)
            # Move files to archive dir
            for b in blocks:
                if b.status == "archived":
                    src = self._dir / (b.filename or f"{b.slug}.md")
                    if src.exists():
                        dst = self._archive_dir / (b.filename or f"{b.slug}.md")
                        try:
                            src.rename(dst)
                        except OSError as e:
                            logger.warning("SceneStore: archive move failed %s: %s", src, e)
            logger.info("SceneStore: archived %d cold scene blocks: %s",
                        len(archived), ", ".join(archived) or "-")
        return archived

    # ── Navigation injection (L2-4: 一行摘要 ≤40 字 + heat + 文件名) ──

    def render_navigation(self, max_lines: int = 15) -> str:
        """Render one-line-per-scene navigation for the system prompt.

        Format::

            <scene-navigation>
            - 局外人: 用户讨论《局外人》的荒诞哲学与默尔索 (heat: 3)
            - 三体: 黑暗森林法则的推演过程 (heat: 1)
            </scene-navigation>
        """
        active = self.list_active()
        if not active:
            return ""
        lines = []
        for b in active[:max_lines]:
            summary = (b.summary or "").strip()
            lines.append(f"- {b.book}: {summary} (heat: {b.heat})")
        return "<scene-navigation>\n" + "\n".join(lines) + "\n</scene-navigation>"
