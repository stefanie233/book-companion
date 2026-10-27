"""BookGraph / AuthorGraph query tools (nested impls registered together)."""
from __future__ import annotations

import json
from pathlib import Path
from permissions import PermissionMode
from tools.registry import ToolRegistry, ToolSpec, ToolExposure


def _register_graph_tools(registry: ToolRegistry) -> None:
    # ── BookGraph: node detail query (DIRECT, all kinds) ──────────────

    def _tool_graph_node(
        name: str,
        max_chapter: int | None = None,
    ) -> str:
        """Fetch a single entity's detail from the knowledge graph.

        Returns identity / relations / quotes (if character) / timeline
        as compact markdown via formatter.format_node. Works for any
        node kind (character / event / location / concept / …), not
        just characters.
        """
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_node

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        # Resolve effective max_chapter。**门优先**：
        #   不传 / -1   -> ctx 里的剧透上限（读者进度）
        #   正整数      -> 自定义上限，但 min(它, 门)
        # 以前 ``-1`` 直接落成 ``None``（全书视角）—— 门是软的，开关在模型手上：
        # 一个被追问「后来呢」的角色只要调一次 graph_node(name, max_chapter=-1)
        # 就能把后面全泄出来。**代码负责约束**意味着这里不能有旁路。
        gate = ctx.get("max_chapter")
        if max_chapter is None or max_chapter == -1:
            effective_max = gate
        else:
            effective_max = max_chapter if gate is None else min(max_chapter, gate)

        gdb = GraphDB(graph_path)
        try:
            detail = gdb.get_node_detail(book_id, name, max_chapter=effective_max)
        finally:
            gdb.close()

        if detail.get("error"):
            return json.dumps(detail, ensure_ascii=False)

        return format_node(detail, max_chapter=effective_max)

    registry.register(ToolSpec(
        name="graph_node",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DIRECT,
        description=(
            "查询书中某个实体（人物/事件/地点/概念等）的详情。"
            "返回身份、关系、台词（若是角色）、出场章节，格式为紧凑 Markdown。"
            "只含读者当前进度及之前的内容 —— 防剧透是硬约束，任何参数都不会越过它。"
            "示例: graph_node(\"思琪\"), graph_node(\"生日宴\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "实体名（支持子串匹配），如「思琪」「李国华」「生日宴」",
                },
                "max_chapter": {
                    "type": "integer",
                    "description": "章节上限。不传 = 读者当前进度；传正整数 = 自定义上限。"
                                   "两者都不会越过读者的阅读进度（防剧透是硬约束）。",
                },
            },
            "required": ["name"],
        },
        execute=_tool_graph_node,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # -- BookGraph + reading tools (DIRECT) -------------------------
    # These tools read per-request context from registry.reading_ctx,
    # populated by api_server for all modes (not just role-play).

    def _tool_graph_search(query: str) -> str:
        """Search the knowledge graph for matching entities."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_search

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")
        gdb = GraphDB(graph_path)
        try:
            results = gdb.search_all(
                book_id, query, max_chapter=max_chapter, limit=10,
            )
        finally:
            gdb.close()

        return format_search(results, query=query)

    registry.register(ToolSpec(
        name="graph_search",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DIRECT,
        description=(
            "搜索书中的实体（人物/事件/地点/概念等）。"
            "返回实体名称、类型、描述和首次出场章节，格式为紧凑 Markdown。"
            "当读者提到某个人或事、你想确认书中细节、或需要回忆角色关系时使用。"
            "找到实体后可用 graph_node 查详情。"
            "示例: graph_search(\"思琪\"), graph_search(\"生日宴\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词——人物名、事件名、地点或概念",
                },
            },
            "required": ["query"],
        },
        execute=_tool_graph_search,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── BookGraph: context query (PRIMARY, DIRECT) ─────────────────────
    # Composes search + node + baked trace. One call gives the LLM a
    # focused subgraph so it doesn't need to chain graph_node/trace.
    # Mirrors codegraph's context tool (always-loaded, bakes trace in).

    def _tool_graph_context(question: str) -> str:
        """Answer a natural-language question using the knowledge graph."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_context

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")

        # Extract candidate entity names from the question.
        # jieba splits Chinese; 2-grams catch names jieba breaks
        # (e.g. "思琪" -> "思"+"琪" -> 2-gram "思琪" recovers it).
        import jieba
        tokens = [t.strip() for t in jieba.cut(question) if t.strip()]
        candidates: set[str] = set()
        for t in tokens:
            if len(t) >= 2:
                candidates.add(t)
        for i in range(len(tokens) - 1):
            gram = (tokens[i] + tokens[i + 1]).strip()
            if len(gram) >= 2:
                candidates.add(gram)

        gdb = GraphDB(graph_path)
        try:
            # Search each candidate, collect entry points (cap 5)
            entries: list[dict] = []
            seen_names: set[str] = set()
            for cand in candidates:
                if len(entries) >= 5:
                    break
                results = gdb.search_all(book_id, cand, max_chapter=max_chapter, limit=2)
                for r in results:
                    nm = r["name"]
                    if nm in seen_names:
                        continue
                    detail = gdb.get_node_detail(book_id, nm, max_chapter=max_chapter)
                    if not detail.get("error"):
                        entries.append(detail)
                        seen_names.add(nm)
                    if len(entries) >= 5:
                        break

            if not entries:
                return f"未在图谱中找到与「{question}」相关的实体。"

            # Bake paths between entry pairs (cap 3 paths)
            paths: list[dict] = []
            for i in range(len(entries)):
                for j in range(i + 1, len(entries)):
                    if len(paths) >= 3:
                        break
                    p = gdb.find_path(
                        book_id,
                        entries[i]["canonical_name"],
                        entries[j]["canonical_name"],
                        max_chapter=max_chapter,
                    )
                    if p.get("hops"):
                        paths.append(p)

            # Collect quotes from entry characters (cap 6)
            quotes: list[dict] = []
            for e in entries:
                if e.get("kind") == "character":
                    for q in e.get("quotes", [])[:3]:
                        quotes.append({**q, "character": e["canonical_name"]})
                if len(quotes) >= 6:
                    break
        finally:
            gdb.close()

        return format_context(
            {"question": question, "entries": entries, "paths": paths, "quotes": quotes},
            max_chapter=max_chapter,
        )

    registry.register(ToolSpec(
        name="graph_context",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DIRECT,
        description=(
            "用自然语言提问查图谱：入口实体 + 关系 + 关系链 + 相关引文，一次返回。"
            "当读者问「X 和 Y 什么关系」「书里的 XX 主题」、或需要聚焦了解某块内容时使用。"
            "这是查图谱的首选工具，不必再分别调 graph_search/graph_node/graph_trace。"
            "示例: graph_context(\"思琪和伊纹的关系\"), graph_context(\"书中的暴力场景\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "自然语言问题，如「思琪和伊纹的关系」「亚历山大和第欧根尼的互动」",
                },
            },
            "required": ["question"],
        },
        execute=_tool_graph_context,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_current_passage() -> str:
        """Return the chunk of text the reader is currently on."""
        ctx = registry.reading_ctx
        db = ctx.get("db")
        book_id = ctx.get("book_id", "")
        if not db or not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        ch_idx = ctx.get("chapter_index", 0)
        ck_idx = ctx.get("chunk_index", 0)
        # 扮演模式**从不带 position**（前端只在阅读页发），于是这里默认取
        # ch0/ck0 —— 而 ch0 常常是目录。旧实现就这样把「目录」当成
        # 「读者正在看的段落」交给了模型，还在 think 里被当成章节线索用。
        has_position = ctx.get("has_position", True)   # 缺键 = 旧调用方，按有位置处理
        note = ""
        if not has_position:
            last_ch = ctx.get("reading_chapter")
            if not isinstance(last_ch, int) or last_ch < 0:
                return json.dumps(
                    {"note": "读者当前不在阅读页，没有可用的当前位置。"
                             "要引用原文请用 library_search 检索，再用 library_read 读取。"},
                    ensure_ascii=False,
                )
            # 只知道章：给这章开头，并且**说清楚这不是「正在看的一段」**
            ch_idx, ck_idx = last_ch, 0
            note = "读者此刻不在阅读页，这是 ta 上次读到的章节开头"

        ck = db.get_chunk(book_id, ch_idx, ck_idx)
        if not ck:
            return json.dumps({"error": "无法获取当前段落"}, ensure_ascii=False)

        book = db.get_book(book_id)
        ch = db.get_chapter(book_id, ch_idx)
        total = db.get_chunk_count(book_id, ch_idx)
        passage = ck.content[:1500]
        if len(ck.content) > 1500:
            passage += "\n…（段落较长，已截断）"

        header = (
            f"《{book.title if book else book_id}》 | "
            f"{ch.title if ch else '第' + str(ch_idx + 1) + '章'} | "
            f"第 {ck_idx + 1}/{total} 块"
            + (f" | ⚠️ {note}" if note else "")
            + "\n\n"
        )
        return header + passage

    registry.register(ToolSpec(
        name="current_passage",
        category="reading",
        toolset="file_reading",
        exposure=ToolExposure.DIRECT,
        description=(
            "获取读者当前正在阅读的段落原文。"
            "当你需要确认读者正在看什么内容、或者读者提到「这一段」时使用。"
            "返回当前章节标题、位置信息和段落文本（最长 1500 字符）。"
            "不需要参数。"
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        execute=_tool_current_passage,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── BookGraph: DEFERRED tools (discovered via tool_search) ─────────

    def _tool_graph_quotes(character: str) -> str:
        """Return a character's quotes from the knowledge graph."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_quotes

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")
        gdb = GraphDB(graph_path)
        try:
            quotes = gdb.get_quotes_by_character(
                book_id, character, max_chapter=max_chapter, limit=15,
            )
        finally:
            gdb.close()

        return format_quotes(quotes, character=character)

    registry.register(ToolSpec(
        name="graph_quotes",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DEFERRED,
        description=(
            "回忆某个角色的经典台词。返回台词文本、场景上下文和出处章节。"
            "当你想引用角色的话、或读者问「XX 说过什么」时使用。"
            "示例: graph_quotes(\"思琪\"), graph_quotes(\"李国华\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "character": {
                    "type": "string",
                    "description": "角色名字，如「思琪」「李国华」",
                },
            },
            "required": ["character"],
        },
        execute=_tool_graph_quotes,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_graph_trace(from_name: str, to_name: str) -> str:
        """Find the relation path between two entities."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_trace

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")
        gdb = GraphDB(graph_path)
        try:
            path = gdb.find_path(book_id, from_name, to_name, max_chapter=max_chapter)
        finally:
            gdb.close()

        return format_trace(path)

    registry.register(ToolSpec(
        name="graph_trace",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DEFERRED,
        description=(
            "查找两个实体之间的关系路径（最短路径，≤4 跳）。"
            "返回 A 到 B 经过哪些实体和关系。"
            "当你想理清「X 怎么卷入 Y」「X 和 Y 什么关系」时使用。"
            "示例: graph_trace(\"思琪\", \"伊纹\"), graph_trace(\"亚历山大\", \"第欧根尼\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_name": {
                    "type": "string",
                    "description": "起点实体名",
                },
                "to_name": {
                    "type": "string",
                    "description": "终点实体名",
                },
            },
            "required": ["from_name", "to_name"],
        },
        execute=_tool_graph_trace,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_graph_timeline(name: str) -> str:
        """Show a node's chapter appearance timeline."""
        ctx = registry.reading_ctx
        book_id = ctx.get("book_id", "")
        if not book_id:
            return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)

        graph_path_str = ctx.get("graph_db_path", "")
        if not graph_path_str:
            return json.dumps({"error": "本书没有知识图谱数据"}, ensure_ascii=False)

        from pathlib import Path
        from storage.graph_db import GraphDB
        from bookgraph.formatter import format_timeline

        graph_path = Path(graph_path_str)
        if not graph_path.exists():
            return json.dumps(
                {"error": "本书知识图谱尚未建立。请先在书架中运行图谱分析。"},
                ensure_ascii=False,
            )

        max_chapter = ctx.get("max_chapter")
        gdb = GraphDB(graph_path)
        try:
            detail = gdb.get_node_detail(book_id, name, max_chapter=max_chapter)
        finally:
            gdb.close()

        if detail.get("error"):
            return json.dumps(detail, ensure_ascii=False)

        return format_timeline(detail, max_chapter=max_chapter)

    registry.register(ToolSpec(
        name="graph_timeline",
        category="knowledge",
        toolset="book_graph",
        exposure=ToolExposure.DEFERRED,
        description=(
            "查看某个实体在哪些章节出场。返回已见出场和后续出场（防剧透）。"
            "当你想确认「这个角色/事件在第几章出现」时使用。"
            "示例: graph_timeline(\"思琪\"), graph_timeline(\"生日宴\")"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "实体名",
                },
            },
            "required": ["name"],
        },
        execute=_tool_graph_timeline,
        permission_mode=PermissionMode.READ_ONLY,
    ))
