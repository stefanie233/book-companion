"""Library management tools: import, edit, delete, version control."""
from __future__ import annotations

import json
from typing import Any
from permissions import PermissionMode
from tools.registry import ToolRegistry, ToolSpec, ToolExposure


def _register_library_tools(registry: ToolRegistry, db: Any, api_client: Any = None) -> None:
    """Register library management tools (import, list, read, search).

    These tools operate on the SQLite book library rather than raw files.
    ``db`` is the Database instance for book CRUD.  ``api_client`` is only
    needed by the LLM-backed enrichment tool.
    """
    from services.library_service import import_book, clean_book, split_book_chapters, format_import_summary
    from services.library_service import search_library as svc_search
    from services.library_service import edit_book, edit_chapter, remove_chapter, manage_categories, delete_book
    from services.library_service import replace_in_chapter
    from services.library_service import list_versions, rollback_chapter
    from services.epub_import_service import extract_epub_skeleton, import_epub_with_split

    def _tool_import_book(path: str, category_id: str = "") -> str:
        """Import an EPUB/MOBI book into the library."""
        try:
            cat = category_id if category_id else None
            book, is_new = import_book(db, path, category_id=cat)
            prefix = "" if is_new else "(already imported — skipped)\n"
            return prefix + format_import_summary(book)
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {path}"})
        except Exception as exc:
            return json.dumps({"error": f"Import failed: {exc}"})

    def _tool_library_list(status: str = "") -> str:
        """List all imported books."""
        try:
            filt = status if status else None
            books = db.list_books(status=filt)
            if not books:
                return "(no books in library — use import_book to add one)"
            lines = [f"📚 Library ({len(books)} books)", ""]
            for b in books:
                status_icon = "✓" if b.status == "clean" else "○"
                # Include chunk count if available
                try:
                    chunk_count = db.get_chunk_count(b.id)
                except Exception:
                    chunk_count = 0
                chunk_info = f", {chunk_count:,} chunks" if chunk_count else ""
                lines.append(
                    f"  {status_icon} [{b.format.upper()}] {b.title}  "
                    f"({b.chapter_count} ch{chunk_info}, {b.total_chars:,} chars)"
                )
                lines.append(f"       id: {b.id}  status: {b.status}")
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"List failed: {exc}"})

    def _tool_library_read(book_id: str, chapter_index: int = 0,
                           chunk_index: int = 0, max_chars: int = 0) -> str:
        """Read a chunk from an imported book chapter stored in the database.

        Each chapter is divided into chunks of ~800 characters. Use
        chunk_index to advance through the chapter (0-based). Omit
        max_chars to read the entire chunk; the chunk is pre-sized.
        """
        try:
            # Try chunk-based read first
            chunk = db.get_chunk(book_id, chapter_index, chunk_index)
            if chunk is not None:
                text = chunk.content
                if max_chars > 0 and len(text) > max_chars:
                    text = text[:max_chars] + "\n… (truncated)"
                total = db.get_chunk_count(book_id, chapter_index)
                ch = db.get_chapter(book_id, chapter_index)
                title = ch.title if ch else f"Chapter {chapter_index + 1}"
                header = f"## {title}  [{chunk_index + 1}/{total}]\n\n"
                return header + text

            # Fallback: old books without chunks — read chapter.content
            ch = db.get_chapter(book_id, chapter_index)
            if ch is None:
                return f"(chapter {chapter_index} not found for book {book_id})"
            text = ch.content
            if max_chars > 0 and len(text) > max_chars:
                cutoff = text[:max_chars]
                last_break = cutoff.rfind("\n\n")
                if last_break > max_chars // 2:
                    text = cutoff[:last_break + 1] + "\n… (truncated)"
                else:
                    text = cutoff + "\n… (truncated)"
            header = f"## {ch.title}\n\n" if ch.title else ""
            return header + text
        except Exception as exc:
            return json.dumps({"error": f"Read failed: {exc}"})

    def _tool_library_search(query: str, max_results: int = 20) -> str:
        """Full-text search across all imported books."""
        try:
            results = svc_search(db, query, limit=max_results)
            if not results:
                return f"(no matches for '{query}')"
            lines = [
                f"Search results for '{query}' ({len(results)} matches)",
                "（每条都带坐标，可直接用于 library_read。"
                "摘要 = 这一块在讲什么；标「片段」的是还没生成摘要的块）",
                "",
            ]
            for r in results:
                # 坐标必须打出来：只给章节名，模型拿不到 chapter_index，
                # 就只能 library_list 找书、再挨个章号试读 —— 实测为一次
                # 扮演回合白白多花两轮（先列 21 本书，再读错一章代跋）。
                lines.append(
                    f"[{r['book_title']} — "
                    f"{r['chapter_title'] or 'Ch.' + str(r['chapter_index'] + 1)}] "
                    f"book_id={r['book_id']} "
                    f"chapter_index={r['chapter_index']} "
                    f"chunk_index={r['chunk_index']}"
                )
                # 有简述就给简述 —— 检索结果回答的是「哪一块在讲这件事」，
                # 不必把原文片段也塞进来（需要原文时按坐标 library_read）。
                summary = (r.get("summary") or "").strip()
                if summary:
                    lines.append(f"  摘要：{summary}")
                else:
                    lines.append(f"  片段：…{r['snippet']}…")
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"Search failed: {exc}"})

    registry.register(ToolSpec(
        name="import_book",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Import an EPUB or MOBI book into the library database. "
            "Parses all chapters, cleans the content, and stores them for "
            "fast future access. Returns a summary with book_id for use "
            "with library_read and library_search. "
            "After import, the book-cleaner skill automatically runs to "
            "remove headers, page numbers, and other artifacts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub or .mobi file",
                },
                "category_id": {
                    "type": "string",
                    "description": "Optional category ID for organizing books",
                },
            },
            "required": ["path"],
        },
        execute=_tool_import_book,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── epub_import ──────────────────────────────────────────────────

    def _tool_epub_import(path: str, method: str = "", selector: str = "",
                           category_id: str = "", book_id: str = "",
                           chapter_types: str = "",
                           metadata_title: str = "", metadata_author: str = "",
                           metadata_nationality: str = "") -> str:
        """LLM-assisted EPUB import with smart chapter boundary detection.

        Two-step workflow built into the tool description — the agent
        reads the description and follows the recipe.
        """
        import json as _json

        # Step 1: skeleton extraction (no method provided)
        if not method:
            try:
                result = extract_epub_skeleton(path)
                if "error" in result:
                    return _json.dumps({"error": result["error"]})
                return _json.dumps({
                    "step": 1,
                    "title": result["title"],
                    "author": result["author"],
                    "filename": result["filename"],
                    "spine_count": result["spine_count"],
                    "toc": result["toc"],
                    "skeleton": result["skeleton"],
                    "source_spine_index": result["source_spine_index"],
                    "structural": result.get("structural", {}),
                    "candidates": result.get("candidates", []),
                    "instructions": (
                        "Analyse the HTML skeleton above. Identify the heading "
                        "pattern used for chapter/section titles. Then call "
                        "epub_import again with:\n"
                        "  method='h_tags' (if headings use <h1>-<h6> tags)\n"
                        "  method='font_size' + selector='N' (if headings use "
                        "<font size='N'><b> pattern)\n"
                        "  method='css_class' + selector='classname' (if headings "
                        "use CSS classes like 'chaptertitle')\n"
                        "For font_size, the selector is the font size number "
                        "(e.g. '5'). For css_class, the selector is the class "
                        "name value.\n\n"
                        "OPTIONAL CHAPTER CLASSIFICATION: examine the 'candidates' "
                        "list (front/back suspicious spine items with label + "
                        "~200-char preview). Decide each one's chapter_type from "
                        "the 10 allowed values: cover, copyright, toc, preface, "
                        "body, postscript, appendix, bibliography, index, notes. "
                        "On the Step 2 call, pass chapter_types as a JSON string "
                        "mapping spine_index to type, e.g. "
                        "chapter_types='{\"0\":\"preface\",\"4\":\"postscript\"}'. "
                        "Only include entries you are confident about — omit the "
                        "rest to fall back to automatic detection.\n\n"
                        "OPTIONAL METADATA CLEANUP: use the 'filename' above "
                        "together with 'title' and 'author'. Strip marketing "
                        "noise (e.g. '作家榜经典', '某某推荐', '完整版') from the "
                        "title and any 著/编/译 suffixes from the author name; "
                        "keep the book's real title verbatim otherwise. "
                        "Determine the author's nationality in standard Chinese "
                        "(e.g. '法国', '美国', '日本'). Only fill what you are "
                        "sure about — leave nationality empty otherwise, never "
                        "guess. On the Step 2 call pass metadata_title='…', "
                        "metadata_author='…', metadata_nationality='…' to "
                        "override the OPF values. Omit any field you are unsure "
                        "about to keep the OPF value."
                    ),
                }, ensure_ascii=False)
            except FileNotFoundError:
                return _json.dumps({"error": f"File not found: {path}"})
            except Exception as exc:
                return _json.dumps({"error": f"Skeleton extraction failed: {exc}"})

        # Step 2: import with splitting
        try:
            if method not in ("h_tags", "font_size", "css_class"):
                return _json.dumps({
                    "error": (
                        f"Unknown method: {method}. "
                        "Must be 'h_tags', 'font_size', or 'css_class'."
                    ),
                })
            if method in ("font_size", "css_class") and not selector:
                return _json.dumps({
                    "error": (
                        f"Method '{method}' requires a selector. "
                        "For font_size, pass the font size number (e.g. '5'). "
                        "For css_class, pass the class name (e.g. 'chaptertitle-c')."
                    ),
                })

            cat = category_id if category_id else None
            bid = book_id if book_id else None
            book, is_new = import_epub_with_split(
                path, method, selector, db,
                category_id=cat, book_id=bid, chapter_types=chapter_types,
                metadata_title=metadata_title, metadata_author=metadata_author,
                metadata_nationality=metadata_nationality,
            )
            prefix = "(re-imported — overwritten)\n" if bid else ""
            if not is_new and not bid:
                prefix = "(already imported — skipped)\n"
            return prefix + format_import_summary(book)
        except FileNotFoundError:
            return _json.dumps({"error": f"File not found: {path}"})
        except Exception as exc:
            return _json.dumps({"error": f"EPUB import failed: {exc}"})

    registry.register(ToolSpec(
        name="epub_import",
        category="book",
        toolset="library_edit",
        # 旧的「两步人工切分」工作流：只在真的要导书时才用得上，却带着
        # 3k 字符的描述（≈790 tokens）挂在每一轮。新链路是 import_book
        # （也是 DEFERRED），导入意图先走 tool_search 即可。
        exposure=ToolExposure.DEFERRED,
        description=(
            "Import an EPUB book with intelligent chapter boundary detection. "
            "TWO-STEP WORKFLOW:\n"
            "  Step 1: call with path only → returns structural summary + HTML "
            "skeleton showing the book's heading structure (tags, CSS classes, "
            "font sizes). Analyse this skeleton to identify the heading pattern.\n"
            "  Step 2: call again with path + method + selector → mechanically "
            "splits every spine file at detected heading boundaries and imports "
            "to the library.\n"
            "  Step 2b (optional): pass chapter_types='{\"0\":\"preface\","
            "\"4\":\"postscript\"}' to classify front/back matter (see Step 1 "
            "'candidates'). Omit to let automatic detection (EPUB structure "
            "signals + label heuristics) decide.\n"
            "  Step 2c (optional): pass metadata_title/metadata_author/"
            "metadata_nationality to clean marketing noise from the OPF "
            "title/author (see Step 1 'filename').\n\n"
            "METHODS (choose one based on skeleton analysis):\n"
            "  'h_tags'   — split at <h1>-<h6> boundaries (no selector needed)\n"
            "  'font_size' — split at <font size='N'><b>…</b></font> headings "
            "(selector = font size number, e.g. '5')\n"
            "  'css_class' — split at elements with a specific CSS class "
            "(selector = class name, e.g. 'chaptertitle-c')\n\n"
            "CACHING: once imported, the method+selector are saved per book. "
            "Re-imports skip Step 1 and use the cached settings. "
            "Use book_id= to force-reimport with the same method.\n\n"
            "FALLBACK: the original import_book tool is still available for "
            "EPUBs that already have correct chapter splits in the NCX/spine."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the .epub file.",
                },
                "method": {
                    "type": "string",
                    "enum": ["", "h_tags", "font_size", "css_class"],
                    "description": (
                        "Heading detection method. Omit for Step 1 (skeleton "
                        "extraction). Required for Step 2 (import)."
                    ),
                },
                "selector": {
                    "type": "string",
                    "description": (
                        "Method-specific selector: font size number for "
                        "'font_size' method, CSS class name for 'css_class' "
                        "method. Not needed for 'h_tags'."
                    ),
                },
                "category_id": {
                    "type": "string",
                    "description": "Optional category ID for organising books.",
                },
                "book_id": {
                    "type": "string",
                    "description": (
                        "Optional book_id for re-import (overwrites existing "
                        "chapters). Omit for first-time import."
                    ),
                },
                "chapter_types": {
                    "type": "string",
                    "description": (
                        "Optional JSON string mapping spine_index to "
                        "chapter_type, e.g. '{\"0\":\"preface\",\"4\":"
                        "\"postscript\"}'. Valid values: cover, copyright, toc, "
                        "preface, body, postscript, appendix, bibliography, "
                        "index, notes. See Step 1 'candidates' for the spine "
                        "indices to classify. Omit to fall back to automatic "
                        "detection."
                    ),
                },
                "metadata_title": {
                    "type": "string",
                    "description": (
                        "Optional cleaned book title from Step 1 metadata "
                        "cleanup; overrides the OPF title."
                    ),
                },
                "metadata_author": {
                    "type": "string",
                    "description": (
                        "Optional cleaned author name (no 著/编/译 suffixes); "
                        "overrides the OPF creator."
                    ),
                },
                "metadata_nationality": {
                    "type": "string",
                    "description": (
                        "Optional author nationality in standard Chinese "
                        "(e.g. '法国'); empty to skip."
                    ),
                },
            },
            "required": ["path"],
        },
        execute=_tool_epub_import,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── Book metadata enrichment (one-shot LLM cleanup) ─────────────────
    _enrich_in_flight: set[str] = set()

    def _fmt_change(before: str, after: str, limit: int = 60) -> str:
        """'旧 → 新' with truncation, or just the unchanged value."""
        if after and after != before:
            return f"{before[:limit]} → {after[:limit]}"
        return before[:limit]

    async def _tool_enrich_metadata(book_id: str = "", all_books: bool = False) -> str:
        """LLM metadata cleanup for one book or the whole library."""
        if not api_client:
            return json.dumps(
                {"error": "Metadata enrichment unavailable (no api_client)."},
                ensure_ascii=False,
            )
        from services.metadata_enrich import enrich_book_metadata

        if book_id:
            b = db.get_book(book_id)
            if not b:
                return json.dumps({"error": f"Book not found: {book_id}"}, ensure_ascii=False)
            targets = [b]
        elif all_books:
            targets = db.list_books(limit=10000)
            if not targets:
                return json.dumps({"result": "No books in the library."}, ensure_ascii=False)
        else:
            return json.dumps({
                "error": "Provide book_id to enrich one book, or set all_books=true for the whole library.",
            }, ensure_ascii=False)

        results: list[dict] = []
        for b in targets:
            if b.id in _enrich_in_flight:
                continue
            _enrich_in_flight.add(b.id)
            try:
                before = (b.title, b.author or "", b.author_nationality or "")
                ok = await enrich_book_metadata(api_client, db, b.id)
                after = db.get_book(b.id)
                aft = (after.title, after.author or "", after.author_nationality or "")
                changed = ok and aft != before
                results.append({
                    "book_id": b.id,
                    "title": _fmt_change(before[0], aft[0]),
                    "author": _fmt_change(before[1], aft[1]),
                    "nationality": aft[2],
                    "status": "changed" if changed else ("ok" if ok else "failed"),
                })
            finally:
                _enrich_in_flight.discard(b.id)

        return json.dumps({
            "processed": len(results),
            "results": results,
        }, ensure_ascii=False)

    registry.register(ToolSpec(
        name="enrich_book_metadata",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Clean book metadata via one LLM call per book: strip marketing "
            "noise from the title (e.g. '作家榜经典：局外人（…北岛推荐版本！…）' "
            "→ '局外人'), normalize the author name (drop 著/编/译 suffixes "
            "and nationality brackets like （美） or [美]), and fill in the "
            "author's nationality (standard Chinese, empty when unsure). "
            "Never renames a book, never guesses. Use when book titles or "
            "authors look messy on the shelf or reading page. One book at a "
            "time (book_id) or the whole library (all_books=true)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": (
                        "Book id to enrich (see library_list for ids). "
                        "Omit it and set all_books=true to enrich every book."
                    ),
                },
                "all_books": {
                    "type": "boolean",
                    "description": (
                        "Enrich the whole library (one LLM call per book). "
                        "Only when book_id is empty."
                    ),
                },
            },
        },
        execute=lambda book_id="", all_books=False: json.dumps(
            {"error": "enrich_book_metadata is async-only — call via the runtime."},
            ensure_ascii=False,
        ),
        execute_async=_tool_enrich_metadata,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    registry.register(ToolSpec(
        name="library_list",
        category="book",
        toolset="library_query",
        description=(
            "List all books imported into the library. "
            "Shows title, format, chapter count, chunk count, "
            "character count, and import status for each book. "
            "Chunks are the reading unit (~800 chars each); "
            "a book with 12 chapters and 256 chunks means the "
            "longest chapter has ~30 reading units."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: 'raw' or 'clean' (default: all)",
                },
            },
        },
        execute=_tool_library_list,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="library_read",
        category="book",
        toolset="library_query",
        description=(
            "Read a chunk of ~800 chars from an imported book chapter. "
            "Use book_id from library_list, and chapter_index (0-based) "
            "to pick a chapter. Each chapter is pre-split into chunks of "
            "~800 characters — use chunk_index (0-based) to advance "
            "through the chapter. The response header shows progress "
            "like [3/12] = chunk 3 of 12 in this chapter. "
            "Much faster than read_book since no file parsing is needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (returned by import_book or library_list)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index (default: 0)",
                },
                "chunk_index": {
                    "type": "integer",
                    "description": "0-based chunk index within the chapter (default: 0). Increment to read the next chunk.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Optional truncation limit (default: 0 = read entire chunk)",
                },
            },
            "required": ["book_id"],
        },
        execute=_tool_library_read,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    registry.register(ToolSpec(
        name="library_search",
        category="book",
        toolset="library_query",
        description=(
            "Full-text search across all imported books in the library. "
            "Searches within reading chunks (~800 chars) using FTS5 trigram "
            "index for fast keyword and phrase matching. "
            "Returns excerpted passages with book, chapter, and chunk location. "
            "Use this to find where a topic, character, or quote appears "
            "across your entire library. Suggested limit: 3-5 results."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keyword or phrase",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 20)",
                },
            },
            "required": ["query"],
        },
        execute=_tool_library_search,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_annotation_search(query: str, limit: int = 5,
                                annotation_type: str = "") -> str:
        """Search the reader's highlights & notes across ALL books (全库跨书).

        Recall channel 兜底: 注入的 <user-highlights>/<user-notes> 不足时
        主动跨书检索用户收藏与笔记。
        """
        try:
            from wingman.service import WingmanMemoryProvider
            terms = WingmanMemoryProvider._extract_keywords(query)
            t = (annotation_type or "").strip()
            types = {
                "highlight": ("highlight",),
                "note": ("note",),
            }.get(t, ("highlight", "note"))
            rows = db.search_annotations(
                terms, types=types, limit=min(int(limit), 20),
            )
            if not rows:
                return "(没有匹配的收藏/笔记)"
            lines = [f"📌 收藏/笔记命中 ({len(rows)} 条)"]
            for a in rows:
                try:
                    b = db.get_book(a.book_id) if a.book_id else None
                    title = b.title if b else (a.stable_book_id or "?")
                except Exception:
                    title = a.stable_book_id or "?"
                kind = "笔记" if a.type in ("note", "both") else "收藏"
                text = (a.note_text or a.selected_text or "").strip()
                if len(text) > 120:
                    text = text[:117] + "…"
                lines.append(f"- [{kind}] 《{title}》 ch.{a.chapter_index} ck.{a.chunk_index}")
                lines.append(f"    {text}")
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"annotation_search failed: {exc}"})

    registry.register(ToolSpec(
        name="annotation_search",
        category="book",
        toolset="library_query",
        exposure=ToolExposure.DIRECT,
        description=(
            "跨书搜索读者收藏的高亮与笔记（annotations 表，全库）。"
            "按关键词匹配选中文本/笔记正文，当前在读书的结果排前。"
            "当上下文注入的 <user-highlights>/<user-notes> 不够时，"
            "或读者提到某段之前收藏/批注过的文字时使用。"
            "Suggested limit: 3-5."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词——想找的收藏/笔记主题或原文片段",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限（默认 5）",
                },
                "annotation_type": {
                    "type": "string",
                    "enum": ["", "highlight", "note"],
                    "description": "限定类型：highlight=收藏 / note=笔记 / 空=两者",
                },
            },
            "required": ["query"],
        },
        execute=_tool_annotation_search,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_library_split(book_id: str,
                            dry_run: bool = True,
                            force: bool = False) -> str:
        """Auto-detect chapter boundaries and split a single-chapter book into chapters."""
        try:
            result = split_book_chapters(db, book_id, force=force, dry_run=dry_run)
            if "error" in result:
                return f"Split failed: {result['error']}"
            if result.get("skipped"):
                return f"Skipped: {result['reason']}"
            mode = "[DRY RUN] " if dry_run else ""
            lines = [
                f"{mode}Split '{result['title']}' into {result['new_chapter_count']} chapters:",
                "",
            ]
            for ch in result["chapters"]:
                lines.append(
                    f"  [{ch['index']}] {ch['title'][:60]} "
                    f"({ch['chars']:,} chars)"
                )
            if dry_run:
                lines.append("")
                lines.append("Review the plan, then run with dry_run=false to apply.")
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"Split failed: {exc}"})

    registry.register(ToolSpec(
        name="library_split_chapters",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Re-split a book's chapters using auto-detected boundaries. "
            "Newly imported books are already auto-chunked into ~800-char "
            "reading units; use this only when chapter boundaries are clearly "
            "wrong. Uses regex patterns (第X章, Chapter X, etc.) to find "
            "chapter headings. ALWAYS use dry_run=true first to preview."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID to split (from library_list or import_book)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview the split plan without applying (default: true)",
                },
                "force": {
                    "type": "boolean",
                    "description": "Force re-split even if book already has multiple chapters",
                },
            },
            "required": ["book_id"],
        },
        execute=_tool_library_split,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    # ── Library edit tools ──────────────────────────────────────────────

    def _tool_library_edit_book(book_id: str, title: str = "",
                                 author: str = "", category_id: str = "") -> str:
        """Edit a book's metadata."""
        try:
            fields = {}
            if title:
                fields["title"] = title
            if author:
                fields["author"] = author
            if category_id:
                fields["category_id"] = category_id
            if not fields:
                return "Error: at least one of title, author, or category_id must be provided"
            book = edit_book(db, book_id, **fields)
            return format_import_summary(book)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Edit book failed: {exc}"})

    registry.register(ToolSpec(
        name="library_edit_book",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Edit metadata for an imported book in the library. "
            "You can update the title, author, and/or category assignment. "
            "At least one of title, author, or category_id is required."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list or import_book)",
                },
                "title": {
                    "type": "string",
                    "description": "New title for the book",
                },
                "author": {
                    "type": "string",
                    "description": "New author name",
                },
                "category_id": {
                    "type": "string",
                    "description": "New category ID (from library_manage_categories list)",
                },
            },
            "required": ["book_id"],
        },
        execute=_tool_library_edit_book,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_edit_chapter(book_id: str, chapter_index: int,
                                    content: str = "", title: str = "") -> str:
        """Edit a chapter's content or title."""
        try:
            ct = content if content else None
            tl = title if title else None
            if ct is None and tl is None:
                return "Error: at least one of content or title must be provided"
            result = edit_chapter(db, book_id, chapter_index, content=ct, title=tl)
            lines = [
                f"✓ Chapter {result['chapter_index']} updated",
                f"  Title: '{result['before']['title']}' → '{result['after']['title']}'",
                f"  Chars: {result['before']['char_count']:,} → {result['after']['char_count']:,}",
                f"  Book total: {result['book_total_chars']:,} chars",
            ]
            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Edit chapter failed: {exc}"})

    registry.register(ToolSpec(
        name="library_edit_chapter",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Edit the content or title of a specific chapter in an imported book. "
            "At least one of content or title must be provided. "
            "The chapter_index is 0-based (first chapter = 0)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list or import_book)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index to edit",
                },
                "content": {
                    "type": "string",
                    "description": "New content for the chapter",
                },
                "title": {
                    "type": "string",
                    "description": "New title for the chapter",
                },
            },
            "required": ["book_id", "chapter_index"],
        },
        execute=_tool_library_edit_chapter,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_replace(book_id: str, chapter_index: int,
                               old_text: str, new_text: str = "",
                               mode: str = "literal",
                               dry_run: bool = True,
                               context: int = 50) -> str:
        """Find-and-replace text within a chapter — like WPS/Word replace."""
        try:
            if not old_text:
                return json.dumps({"error": "old_text cannot be empty"})
            result = replace_in_chapter(db, book_id, chapter_index,
                                        old_text, new_text,
                                        mode=mode, dry_run=dry_run,
                                        context=context)

            label = "[DRY RUN] " if dry_run else ""
            lines = [
                f"{label}Replace in '{result['chapter_title']}' "
                f"(Ch.{result['chapter_index'] + 1}): "
                f"{result['count']} match(es) found",
                f"  Mode: {result['mode']}",
                f"  Chars: {result['chars_before']:,} → {result['chars_after']:,} "
                f"({result['chars_delta']:+,})",
                "",
            ]

            if result["count"] == 0:
                lines.append("  (no matches found — check old_text and try again)")
            else:
                for i, p in enumerate(result["preview"], 1):
                    lines.append(f"  [{i}] {p['before']}")
                    lines.append(f"      {p['after']}")
                    lines.append("")
                if result.get("preview_capped"):
                    remaining = result["total_matches"] - len(result["preview"])
                    lines.append(f"  ... and {remaining} more match(es)")
                    lines.append("")

            if dry_run and result["count"] > 0:
                lines.append("Review the changes, then run with dry_run=false to apply.")
            elif not dry_run and result["count"] > 0:
                lines.append(f"Applied {result['count']} replacement(s).")

            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Replace failed: {exc}"})

    registry.register(ToolSpec(
        name="library_replace",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Find-and-replace text within a book chapter — like WPS/Word replace. "
            "Supports progressive preview: start with a small context to scan, "
            "then expand to verify surrounding text before applying.\n"
            "  • literal (default) — exact string match, safe and predictable\n"
            "  • regex — Python regex pattern for advanced find/replace\n"
            "ALWAYS use dry_run=true first to preview the changes, "
            "then confirm with the user before running with dry_run=false. "
            "Use context=200 (or higher) when you need to verify large surrounding "
            "text, or context=50 for a quick overview scan."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list or import_book)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index to search within",
                },
                "old_text": {
                    "type": "string",
                    "description": "Text to find (literal string, or regex pattern if mode='regex')",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text (default: empty string = delete matches)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["literal", "regex"],
                    "description": "Search mode: 'literal' for exact match (default), 'regex' for pattern matching",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview changes without applying (default: true). Set to false to actually apply.",
                },
                "context": {
                    "type": "integer",
                    "description": "Chars of surrounding context in preview (default 50). Use 200+ to see more surrounding text for verification, or 20-30 for a quick scan. Progressive: start small, then expand.",
                },
            },
            "required": ["book_id", "chapter_index", "old_text"],
        },
        execute=_tool_library_replace,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_delete_chapter(book_id: str, chapter_index: int) -> str:
        """Delete a chapter from a book."""
        try:
            result = remove_chapter(db, book_id, chapter_index)
            lines = [
                f"🗑 Deleted chapter {result['removed']['chapter_index']}: "
                f"'{result['removed']['title']}' ({result['removed']['char_count']:,} chars)",
                f"Book now has {result['book_after']['chapter_count']} chapters, "
                f"{result['book_after']['total_chars']:,} total chars",
            ]
            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Delete chapter failed: {exc}"})

    registry.register(ToolSpec(
        name="library_delete_chapter",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Delete a specific chapter from an imported book. "
            "This operation cannot be undone — confirm with the user first. "
            "The chapter_index is 0-based. Remaining chapters keep their indices "
            "(no reindexing, so there may be gaps)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list or import_book)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "0-based chapter index to delete",
                },
            },
            "required": ["book_id", "chapter_index"],
        },
        execute=_tool_library_delete_chapter,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_delete_book(book_id: str, confirm: str = "") -> str:
        """Delete an entire book and all its chapters from the library."""
        try:
            # Safety: require explicit confirmation
            book = db.get_book(book_id)
            if book is None:
                return json.dumps({"error": f"Book not found: {book_id}"})
            expected = f"delete {book.title}"
            if confirm.strip().lower() != expected.lower():
                return json.dumps({
                    "error": "Confirmation required. This will permanently delete "
                             f"'{book.title}' ({book.chapter_count} chapters, "
                             f"{book.total_chars:,} chars). "
                             f"Pass confirm=\"{expected}\" to proceed.",
                    "needs_confirmation": True,
                })
            result = delete_book(db, book_id)
            lines = [
                f"Deleted '{result['title']}' by {result['author'] or 'Unknown'}",
                f"  Format: {result['format'].upper()}",
                f"  Chapters: {result['chapter_count']}",
                f"  Total chars: {result['total_chars']:,}",
            ]
            if result.get("local_copy_removed"):
                lines.append(f"  Local copy removed: {result['local_copy_removed']}")
            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Delete book failed: {exc}"})

    registry.register(ToolSpec(
        name="library_delete_book",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Delete an entire book and all its chapters from the library. "
            "This operation is IRREVERSIBLE — all chapter data will be lost. "
            "ALWAYS confirm with the user before calling this tool. "
            "Requires confirm=\"delete <book_title>\" as a safety check."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID to delete (from library_list)",
                },
                "confirm": {
                    "type": "string",
                    "description": "Must be 'delete <book title>' exactly — a safety check to prevent accidental deletion",
                },
            },
            "required": ["book_id", "confirm"],
        },
        execute=_tool_library_delete_book,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _tool_library_manage_categories(action: str, category_id: str = "",
                                         name: str = "", sort_order: int = 0) -> str:
        """Manage book categories: create, update, delete, or list them."""
        try:
            kwargs = {}
            if category_id:
                kwargs["category_id"] = category_id
            if name:
                kwargs["name"] = name
            if sort_order:
                kwargs["sort_order"] = sort_order
            result = manage_categories(db, action, **kwargs)

            if action == "list":
                if result["count"] == 0:
                    return "(no categories)"
                lines = [f"📁 Categories ({result['count']})", ""]
                for cat in result["categories"]:
                    lines.append(f"  {cat['id']}  {cat['name']}  (order: {cat['sort_order']})")
                return "\n".join(lines)
            elif action == "create":
                cat = result["category"]
                return f"✓ Created category '{cat['name']}' (id: {cat['id']})"
            elif action == "update":
                return f"✓ Updated category {result['category_id']}: {', '.join(result['updated_fields'])}"
            elif action == "delete":
                return f"✓ Deleted category {result['category_id']}. {result.get('note', '')}"
            return json.dumps(result)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Category operation failed: {exc}"})

    registry.register(ToolSpec(
        name="library_manage_categories",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Manage book categories: create, rename, delete, or list them. "
            "Use this to organize books by genre, topic, or reading status.\n"
            "  action='create' — create a new category (name required)\n"
            "  action='update' — rename or reorder a category (category_id + name/sort_order)\n"
            "  action='delete' — delete a category (books under it become uncategorized)\n"
            "  action='list'   — list all existing categories"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "delete", "list"],
                    "description": "Category operation to perform",
                },
                "category_id": {
                    "type": "string",
                    "description": "Category ID (required for update and delete)",
                },
                "name": {
                    "type": "string",
                    "description": "Category name (required for create, optional for update)",
                },
                "sort_order": {
                    "type": "integer",
                    "description": "Sort order for display ordering (optional)",
                },
            },
            "required": ["action"],
        },
        execute=_tool_library_manage_categories,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    # ── Version control tools ──────────────────────────────────────────

    def _tool_library_list_versions(book_id: str, chapter_index: int = -1,
                                     limit: int = 50) -> str:
        """List version history for a chapter or all chapters."""
        try:
            ci = chapter_index if chapter_index >= 0 else None
            versions = list_versions(db, book_id, chapter_index=ci, limit=limit)
            if not versions:
                scope = f"Chapter {chapter_index}" if chapter_index >= 0 else "Book"
                return f"(no version history for {scope} {book_id})"

            lines = [f"Version History ({len(versions)} entries)", ""]
            for v in versions:
                delta = v["new_char_count"] - v["old_char_count"]
                delta_str = f"{delta:+,}"
                lines.append(
                    f"  {v['version_id']}  Ch.{v['chapter_index']}  "
                    f"{v['operation']:20s}  "
                    f"{v['old_char_count']:,} -> {v['new_char_count']:,} chars  "
                    f"({delta_str})"
                )
            return "\n".join(lines)
        except Exception as exc:
            return json.dumps({"error": f"List versions failed: {exc}"})

    registry.register(ToolSpec(
        name="library_list_versions",
        category="book",
        toolset="library_query",
        exposure=ToolExposure.DEFERRED,
        description=(
            "List version history for a book's chapters. Shows every edit/replace/"
            "delete operation with before/after char counts. "
            "Use chapter_index to filter to a specific chapter, or omit to see all. "
            "Each version has a version_id for use with library_rollback."
        ),
        parameters={
            "type": "object",
            "properties": {
                "book_id": {
                    "type": "string",
                    "description": "Book ID (from library_list)",
                },
                "chapter_index": {
                    "type": "integer",
                    "description": "Filter to a specific chapter (0-based). Omit or use -1 to see all chapters.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum versions to return (default: 50)",
                },
            },
            "required": ["book_id"],
        },
        execute=_tool_library_list_versions,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_library_rollback(version_id: str, dry_run: bool = True) -> str:
        """Rollback a chapter to a previous version."""
        try:
            result = rollback_chapter(db, version_id, dry_run=dry_run)
            label = "[DRY RUN] " if dry_run else ""
            lines = [
                f"{label}Rollback to {version_id}",
                f"  Operation: {result['operation']}",
                f"  Chapter: {result['chapter_index']}",
                f"  Current chars: {result['current_chars']:,}",
                f"  Restored chars: {result['restored_chars']:,}",
                f"  Delta: {result['chars_delta']:+,}",
                f"  Snapshot hash: {result['snapshot_hash']}",
            ]
            return "\n".join(lines)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"Rollback failed: {exc}"})

    registry.register(ToolSpec(
        name="library_rollback",
        category="book",
        toolset="library_edit",
        exposure=ToolExposure.DEFERRED,
        description=(
            "Rollback a chapter to a previous version from its snapshot. "
            "Use library_list_versions first to find the version_id to restore. "
            "ALWAYS use dry_run=true first to preview the rollback details, "
            "then confirm with the user before dry_run=false. "
            "Rollback itself creates a new version (so it's reversible)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "version_id": {
                    "type": "string",
                    "description": "Version ID to rollback to (from library_list_versions)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview the rollback without applying (default: true)",
                },
            },
            "required": ["version_id"],
        },
        execute=_tool_library_rollback,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    return registry
