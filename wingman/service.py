"""WingmanMemoryProvider — implements MemoryProvider ABC.

Provides the 5 memory_* tools (search/read/write/list_by_tag/list_tags)
backed by memory.db SQLite storage.

This is the sole external MemoryProvider registered in MemoryManager.
All other wingman subsystems (moment/profile/ContextBrief/gate check)
have been removed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from memory_provider import MemoryProvider
from wingman.db import WingmanDatabase
from wingman.models import MemoryEntryRow
from wingman.tag_registry import TagRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_structured_content(raw: str) -> tuple[str, str, str]:
    """Parse structured memory content into (body, keywords, summary).

    Expected format::

        <content>正文内容</content>
        <keyword>kw1 kw2 kw3</keyword>
        <summary>一句话总结</summary>

    Returns (body, keywords, summary).  Falls back to raw text as body
    with empty keywords/summary if no <content> tag is found.
    """
    import re

    body = ""
    keywords = ""
    summary = ""

    # Try to extract <content> block
    m = re.search(r"<content>\s*(.*?)\s*</content>", raw, re.DOTALL)
    if m:
        body = m.group(1).strip()

    # Try to extract <keyword> block
    m = re.search(r"<keyword>\s*(.*?)\s*</keyword>", raw, re.DOTALL)
    if m:
        keywords = m.group(1).strip()

    # Try to extract <summary> block
    m = re.search(r"<summary>\s*(.*?)\s*</summary>", raw, re.DOTALL)
    if m:
        summary = m.group(1).strip()

    if body:
        return (body, keywords, summary)

    # No structured tags — use raw text as body
    return (raw, "", "")


# ---------------------------------------------------------------------------
# WingmanMemoryProvider
# ---------------------------------------------------------------------------


class WingmanMemoryProvider(MemoryProvider):
    """Wingman memory provider — provides 5 memory_* tools for persistent memory.

    Implements MemoryProvider ABC. Registered as the single external provider
    in MemoryManager. Provides:
      - memory_search / memory_read / memory_write / memory_list_by_tag / memory_list_tags
    """

    name = "wingman"

    def __init__(self, wingman_db_path: Path, config: Any = None, api_client: Any = None):
        """
        Args:
            wingman_db_path: path to memory.db (e.g., data/memory.db)
            config: project Config instance
            api_client: API client with async complete() for L1 extraction
        """
        self._db = WingmanDatabase(wingman_db_path)
        self._config = config
        self._api_client = api_client

        # Sub-services (initialized in initialize())
        self._tags: TagRegistry | None = None
        self._scheduler: Any = None  # MemoryPipelineScheduler
        self._scene_store: Any = None  # SceneStore (L2 scene blocks)

        # Runtime state
        self._session_id: str = ""
        self._initialized: bool = False

    # ── MemoryProvider ABC: Core lifecycle ──────────────────────────────

    def is_available(self) -> bool:
        return self._initialized

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize wingman for a session.

        kwargs may include:
          - api_client: API client for L1 extraction (passed from initialize_all)
          - config: project Config instance
          - session: Session instance
        """

        # Initialize database
        self._db.initialize()

        # Initialize sub-services
        self._tags = TagRegistry(self._db)

        # Pick up api_client from kwargs if not set in __init__
        if self._api_client is None:
            self._api_client = kwargs.get("api_client")

        # Initialize L1/L2 pipeline scheduler (if api_client available)
        if self._api_client is not None:
            from memory_pipeline.scheduler import MemoryPipelineScheduler

            cfg = kwargs.get("config", self._config)
            every_n = getattr(cfg, "memory_pipeline_every_n", 5)
            idle_timeout = getattr(cfg, "memory_pipeline_idle_timeout", 120)
            model = getattr(cfg, "memory_pipeline_model", None)
            l2_debounce = getattr(cfg, "memory_pipeline_l2_debounce", 90)
            l3_debounce = getattr(cfg, "memory_pipeline_l3_debounce", 5)
            persona_trigger_every_n = getattr(
                cfg, "memory_pipeline_persona_trigger_every_n", 50,
            )
            persona_max_chars = getattr(
                cfg, "memory_pipeline_persona_max_chars", 2000,
            )
            persona_priority_floor = getattr(
                cfg, "memory_pipeline_persona_priority_floor", 60,
            )

            # Scenes dir: data/scenes under project dir (or a configured path)
            scenes_dir = getattr(cfg, "memory_pipeline_scenes_dir", None)
            project_dir = getattr(cfg, "project_dir", None)
            if not scenes_dir and project_dir is not None:
                scenes_dir = str(Path(project_dir) / "data" / "scenes")

            # Persona working copy: data/memory/persona.md (or configured path)
            persona_path = getattr(cfg, "memory_pipeline_persona_path", None)
            if not persona_path and project_dir is not None:
                persona_path = str(Path(project_dir) / "data" / "memory" / "persona.md")

            self._scheduler = MemoryPipelineScheduler(
                self._api_client,
                self._db,
                scenes_dir=scenes_dir,
                every_n=every_n,
                idle_timeout=idle_timeout,
                model=model,
                l2_debounce=l2_debounce,
                persona_path=persona_path,
                persona_trigger_every_n=persona_trigger_every_n,
                persona_max_chars=persona_max_chars,
                persona_priority_floor=persona_priority_floor,
                l3_debounce=l3_debounce,
            )
            self._scheduler.set_session_id(session_id)

            # Keep a direct handle on SceneStore for navigation + search
            self._scene_store = self._scheduler._scene_store
        else:
            logger.warning("WingmanMemoryProvider: no api_client, L1/L2 pipeline disabled")

        self._session_id = session_id
        self._initialized = True

        logger.info("WingmanMemoryProvider initialized session=%s", session_id)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return memory retrieval tool schemas for the LLM.

        All five tools are exposed as DEFERRED — the agent discovers them
        via the memory usage guide in the system prompt, not via the main
        tool list. This keeps the static tool list lean.
        """
        return [
            {
                "name": "memory_search",
                "description": (
                    "搜索持久化记忆库。返回 preview、keywords、tags、引用次数。\n"
                    "根据命中量选择后续策略：\n"
                    "≤3 条 → 直接 memory_read 查看详情\n"
                    "4-10 条 → 先看 preview+keywords 筛选，只 read 最相关的 1-3 条\n"
                    "≥10 条或 limit 被触及 → 关键词太宽，用两趟法：\n"
                    "  一趟：查看各结果的 keywords，精选 2-3 个最精准的词\n"
                    "  二趟：用精选词重新搜索，或转 memory_list_by_tag 按标签浏览\n"
                    "复杂探索时优先两趟法——先摸清记忆空间的关键词分布，再深入。\n"
                    "有些话题是成长心事不必主动提起，有些话题不喜欢被过度使用——"
                    "请根据 cited_count、tags、visibility 自行拿捏。\n"
                    "suggested limit: 3-5"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词或自然语言查询",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回结果数量上限 (default: 5)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memory_read",
                "description": (
                    "读取指定记忆的完整内容。自动递增引用计数。"
                    "先通过 preview 确认本条值得展开——content 可能较长，"
                    "不应盲目 read 搜索结果中的每一条，只读最相关的 1-3 条。"
                    "留意 visibility 和 cited_count，对 wound 标签谨慎引用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memory_id": {
                            "type": "string",
                            "description": "记忆ID（从 memory_search 结果中获取）",
                        },
                    },
                    "required": ["memory_id"],
                },
            },
            {
                "name": "memory_list_by_tag",
                "description": (
                    "按标签浏览某主题下的完整记忆链。返回 preview+keywords+元数据，按时间倒序。"
                    "广度浏览的最佳方式——先用 memory_list_tags 了解有哪些标签。"
                    "suggested limit: 5-10"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tag": {
                            "type": "string",
                            "description": "标签名称",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回数量上限 (default: 10)",
                            "default": 10,
                        },
                    },
                    "required": ["tag"],
                },
            },
            {
                "name": "memory_list_tags",
                "description": (
                    "列出所有已注册标签及其行为类型(normal/wound/forbidden)。"
                    "广度浏览和两趟检索的入口——先看全局标签分布，"
                    "再选感兴趣的用 memory_list_by_tag 展开。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "memory_write",
                "description": (
                    "写入一条新的持久化记忆到记忆库。content 使用结构化格式：\n"
                    "<content>正文内容</content>\n"
                    "<keyword>空格分隔的关键词</keyword>\n"
                    "<summary>一句话总结（可选）</summary>\n"
                    "标准路径：memory_search（查重）→ search_sessions（定位会话）"
                    "→ read_session（读内容）→ memory_write（写入）。"
                    "应在调用前先使用 memory_search 检查是否已有类似记忆，避免重复。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": (
                                "结构化记忆内容：\n"
                                "<content>正文</content>\n"
                                "<keyword>搜索关键词</keyword>\n"
                                "<summary>一句话总结（可选）</summary>"
                            ),
                        },
                        "keywords": {
                            "type": "string",
                            "description": (
                                "空格分隔的搜索关键词。包含 2-5 个词，覆盖本条记忆的"
                                "核心主题词——既可以是精准区分词（如 '黑暗森林美学'），"
                                "也可以是归类词（如 '刘慈欣'）。"
                                "归类词放 tags，但 keyword 里也放一份有助于 memory_search 发现。"
                            ),
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "标签用于聚合同类记忆（如 '村上春树', '科幻', '阅读偏好'）。"
                            ),
                        },
                        "visibility": {
                            "type": "string",
                            "enum": ["normal", "wound"],
                            "description": "可见性：normal 正常，wound 涉及情感创伤/敏感话题",
                        },
                    },
                    "required": ["content"],
                },
            },
        ]

    def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **kwargs
    ) -> str:
        """Dispatch memory tool calls to the appropriate handler."""
        import json

        if not self._initialized:
            return json.dumps({"error": "Wingman not initialized"}, ensure_ascii=False)

        handlers = {
            "memory_search": self._handle_memory_search,
            "memory_read": self._handle_memory_read,
            "memory_list_by_tag": self._handle_memory_list_by_tag,
            "memory_list_tags": self._handle_memory_list_tags,
            "memory_write": self._handle_memory_write,
        }

        handler = handlers.get(tool_name)
        if handler is None:
            return json.dumps({"error": f"Unknown wingman tool: {tool_name}"}, ensure_ascii=False)

        try:
            return handler(args)
        except Exception as e:
            logger.warning("Memory tool %s failed: %s", tool_name, e)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def shutdown(self) -> None:
        """Clean shutdown: flush pipeline + close DB connection."""
        if self._initialized:
            # Flush any remaining buffered messages
            if self._scheduler is not None:
                self._scheduler.flush()
            self._db.close()
            self._initialized = False
            logger.info("WingmanMemoryProvider shut down")

    # ── System prompt: scene navigation (L2-4) ────────────────────────

    def system_prompt_block(self) -> str:
        """Return L2 scene navigation for the static system prompt.

        One line per active book scene block (summary ≤40 chars + heat),
        giving the LLM a stable "what have we read & discussed" index.
        See SceneStore.render_navigation().
        """
        if not self._initialized or self._scene_store is None:
            return ""
        try:
            nav = self._scene_store.render_navigation()
        except Exception as e:
            logger.warning("Scene navigation render failed: %s", e)
            return ""
        if not nav:
            return ""
        return (
            "\n\n### 阅读场景（按书组织的叙事块）\n"
            "以下列出按书聚合的阅读场景及其一句话摘要。"
            "如需详情用 memory_search 顺带获取对应场景块内容。\n"
            f"{nav}"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """L1 atomic-memory recall for the upcoming turn (填此前空壳).

        Returns UNWRAPPED memory lines (no fence tag) — each consumer wraps:
          - CLI: runtime wraps in <memory-context> (build_memory_context_block)
          - Web: recall_orchestrator wraps in <relevant-memories>

        Recalls up to 5 memories by keyword relevance + recency. Empty string
        when nothing matches.
        """
        if not self._initialized or self._db is None:
            return ""
        query = (query or "").strip()
        if not query:
            return ""
        try:
            rows = self._search_memory_rows(query, limit=5)
        except Exception as e:
            logger.warning("WingmanMemoryProvider prefetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines = []
        for mem in rows:
            text = (mem.summary or "").strip() or (mem.content or "").strip()
            if len(text) > 120:
                text = text[:117] + "…"
            lines.append(f"- {text}")
        return "\n".join(lines)

    # ── L1 pipeline hooks ────────────────────────────────────────────

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        """L0 capture: buffer conversation messages for L1 extraction."""
        if self._scheduler is not None:
            self._scheduler.notify_conversation(user_content, assistant_content)

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Session end: flush remaining buffer through L1 pipeline."""
        if self._scheduler is not None:
            self._scheduler.flush()

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Pre-compress: trigger L1 extraction before old messages are discarded.

        Returns empty string (no text to inject into compression prompt).
        """
        if self._scheduler is not None:
            return self._scheduler.on_pre_compress(messages)
        return ""

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        """Reset wingman state for a new session."""
        if not self._initialized:
            return

        self._session_id = new_session_id
        logger.info(
            "Wingman session switched → %s (reset=%s)",
            new_session_id, reset,
        )

    # ── Memory tool handlers ──────────────────────────────────────────

    def _search_memory_rows(self, query: str, *, limit: int = 5) -> list[MemoryEntryRow]:
        """Shared L1 search core — keyword hits first, LIKE fallback, recency-sorted.

        Used by both the memory_search tool and prefetch() so the two don't
        diverge in ranking behavior.
        """
        keywords = self._extract_keywords(query)
        results = self._db.search_memories_by_keywords(keywords, limit=limit * 2)
        if not results:
            # Fallback: broader LIKE search
            results = self._db.search_memories(query, limit=limit * 2)
        return results[:limit]

    def _handle_memory_search(self, args: dict[str, Any]) -> str:
        """Handle memory_search — keyword search with recency-based ranking.

        When the query matches an L2 book scene block, its summary (and
        section content) is appended as `scenes` — 读场景块走 memory_search
        顺带返回 (L2-4).
        """
        import json

        query = str(args.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "query is required"}, ensure_ascii=False)

        limit = min(int(args.get("limit", 5)), 20)

        # Shared search core (keyword → LIKE fallback → recency sort)
        top = self._search_memory_rows(query, limit=limit)

        items = []
        for mem in top:
            items.append({
                "id": mem.id,
                "preview": mem.summary or mem.content[:80],
                "keywords": mem.keywords,
                "tags": [t.name for t in self._db.get_tags_for_memory(mem.id)],
                "visibility": mem.visibility,
                "cited_count": mem.cited_count,
                "source_session_id": mem.source_session_id,
            })

        # L2: attach matching scene blocks (顺带返回)
        scenes = self._match_scene_blocks(query)

        resp: dict[str, Any] = {"results": items, "count": len(items)}
        if scenes:
            resp["scenes"] = scenes
        return json.dumps(resp, ensure_ascii=False)

    def _match_scene_blocks(self, query: str) -> list[dict[str, Any]]:
        """Find active scene blocks whose book/summary/sections match the query.

        Returns list of {book, summary, heat, filename, sections} for the LLM
        to read the scene block content without a separate tool.
        """
        if self._scene_store is None:
            return []
        try:
            active = self._scene_store.list_active()
        except Exception as e:
            logger.warning("Scene match failed: %s", e)
            return []

        q = query.strip().lower()
        if not q:
            return []

        matched: list[dict[str, Any]] = []
        for block in active:
            haystack = " ".join([
                block.book,
                block.summary,
                " ".join(block.sections.values()),
            ]).lower()
            if q in haystack or q in block.book.lower():
                matched.append({
                    "book": block.book,
                    "summary": block.summary,
                    "heat": block.heat,
                    "filename": block.filename,
                    "sections": block.sections,
                })
                if len(matched) >= 3:
                    break
        return matched

    def _handle_memory_read(self, args: dict[str, Any]) -> str:
        """Handle memory_read — fetch full content, increment cited_count."""
        import json

        memory_id = str(args.get("memory_id", "")).strip()
        if not memory_id:
            return json.dumps({"error": "memory_id is required"}, ensure_ascii=False)

        mem = self._db.get_memory_by_id(memory_id)
        if mem is None:
            return json.dumps({"error": f"Memory not found: {memory_id}"}, ensure_ascii=False)

        # Increment citation counter
        self._db.increment_memory_cited(memory_id)

        tags = self._db.get_tags_for_memory(memory_id)

        return json.dumps({
            "id": mem.id,
            "content": mem.content,
            "visibility": mem.visibility,
            "status": mem.status,
            "cited_count": mem.cited_count + 1,  # +1 for this read
            "tags": [{"name": t.name, "category": t.category, "behavior": t.behavior}
                      for t in tags],
            "source_session_id": mem.source_session_id,
        }, ensure_ascii=False)

    def _handle_memory_list_by_tag(self, args: dict[str, Any]) -> str:
        """Handle memory_list_by_tag — browse memories under a tag."""
        import json

        tag = str(args.get("tag", "")).strip()
        if not tag:
            return json.dumps({"error": "tag is required"}, ensure_ascii=False)

        limit = min(int(args.get("limit", 10)), 30)

        # Check if tag exists
        tag_row = self._db.get_tag_by_name(tag)
        if tag_row is None:
            return json.dumps({"error": f"Tag not found: {tag}", "results": [], "count": 0}, ensure_ascii=False)

        # No programmatic visibility filtering — LLM sees all and decides
        memories = self._db.get_memories_by_tag(tag, limit=limit)

        items = []
        for mem in memories:
            items.append({
                "id": mem.id,
                "preview": mem.summary or mem.content[:80],
                "keywords": mem.keywords,
                "visibility": mem.visibility,
                "cited_count": mem.cited_count,
            })

        return json.dumps({
            "tag": {"name": tag_row.name, "category": tag_row.category,
                     "behavior": tag_row.behavior},
            "results": items,
            "count": len(items),
        }, ensure_ascii=False)

    def _handle_memory_list_tags(self, args: dict[str, Any]) -> str:
        """Handle memory_list_tags — list all tags with behavior info."""
        import json

        tags = self._db.list_tags()
        items = []
        for t in tags:
            # Count memories per tag
            memories = self._db.get_memories_by_tag(t.name, limit=1000)
            items.append({
                "name": t.name,
                "category": t.category,
                "behavior": t.behavior,
                "behavior_prompt": t.behavior_prompt,
                "cite_threshold": t.cite_threshold,
                "default_weight": t.default_weight,
                "usage_count": t.usage_count,
                "memory_count": len(memories),
            })

        return json.dumps({"tags": items, "count": len(items)}, ensure_ascii=False)

    def _handle_memory_write(self, args: dict[str, Any]) -> str:
        """Handle memory_write — insert a new memory entry from sub-agent review.

        Supports structured content format:
          <content>body</content>
          <keyword>kw1 kw2</keyword>
          <summary>one-line summary</summary>

        Falls back to plain-text content if no tags are detected.
        """
        import json
        import re

        raw = str(args.get("content", "")).strip()
        if not raw or len(raw) < 10:
            return json.dumps({"error": "content is required (min 10 chars)"}, ensure_ascii=False)

        # ── Parse structured content ──────────────────────────────────
        content, keywords, summary = _parse_structured_content(raw)

        # Fallback: explicit keywords parameter or embed in content
        if not keywords:
            kw_param = str(args.get("keywords", "")).strip()
            if kw_param:
                keywords = kw_param
                content = f"{content}\n\n[关键词: {keywords}]"

        if not content or len(content) < 10:
            return json.dumps({"error": "content body is required (min 10 chars)"}, ensure_ascii=False)

        tag_names: list[str] = args.get("tags", [])
        if isinstance(tag_names, str):
            tag_names = [t.strip() for t in tag_names.split(",") if t.strip()]
        visibility = str(args.get("visibility", "normal"))
        if visibility not in ("normal", "wound", "forbidden"):
            visibility = "normal"
        source_session_id = str(args.get("source_session_id", self._session_id))

        memory_id = self._db.insert_memory_entry_simple(
            content=content, summary=summary, keywords=keywords,
            visibility=visibility, source_session_id=source_session_id,
        )

        # Register and link tags
        tag_count = 0
        if tag_names:
            tag_ids: list[str] = []
            for tag_name in tag_names:
                name = str(tag_name).strip()
                if not name:
                    continue
                behavior = "wound" if visibility == "wound" else "normal"
                cite_threshold = 3 if visibility == "wound" else 10
                tag_id = self._tags.register(
                    name, category="theme",
                    behavior=behavior, cite_threshold=cite_threshold,
                )
                tag_ids.append(tag_id)
            if tag_ids:
                self._db.insert_memory_tags(memory_id, tag_ids)
                tag_count = len(tag_ids)

        return json.dumps({
            "success": True,
            "memory_id": memory_id,
            "tags_linked": tag_count,
        }, ensure_ascii=False)

    @staticmethod
    def _extract_keywords(query: str) -> list[str]:
        """Extract search keywords from a query string.

        For Chinese: 2-4 char n-grams from the query.
        For English: words >= 3 chars.
        """
        keywords: list[str] = []
        # Add the full query as a keyword
        keywords.append(query)

        # Chinese n-grams (2-4 chars)
        for n in (4, 3, 2):
            for i in range(len(query) - n + 1):
                chunk = query[i:i + n]
                # Only add if it contains CJK characters
                if any('一' <= c <= '鿿' for c in chunk):
                    keywords.append(chunk)

        # English words
        for word in query.split():
            if len(word) >= 3 and word.isalpha():
                keywords.append(word)

        # Deduplicate, keep order
        seen: set[str] = set()
        deduped = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                deduped.append(kw)
        return deduped[:10]
