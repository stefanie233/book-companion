"""Library endpoints: list / import / upload / chunk / cover / search."""
from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web

from api.book_helpers import _find_next_chunk, _find_prev_chunk, _get_stable_id
from api.routes.graph import _enrich_book_metadata_async
from services.library_service import _hash_file, cover_thumb_path, get_book_context, make_cover_thumb



async def handle_list_books(request: web.Request) -> web.Response:
    """List all imported books in the library."""
    db = request.app["db"]
    books = db.list_books()
    bookbank = db._path.parent / "bookbank"

    enriched: list[dict] = []
    for b in books:
        has_cover = any(
            (bookbank / f"{b.id}_cover.{ext}").exists()
            for ext in ("jpg", "jpeg", "png", "gif", "webp", "svg")
        )
        # Get latest reading position across all sessions for this book
        last_read_at = None
        last_chapter = None
        last_chunk = None
        active = db.get_active_session(b.id)
        if active and active.positions_json:
            import json as _json
            try:
                positions = _json.loads(active.positions_json)
                if positions:
                    last = positions[-1]
                    last_chapter = last.get("chapter")
                    last_chunk = last.get("chunk")
                    last_read_at = last.get("ts", active.started_at)
            except Exception:
                pass

        enriched.append({
            "id": b.id,
            "title": b.title,
            "author": b.author,
            "author_nationality": b.author_nationality,
            "format": b.format,
            "chapter_count": b.chapter_count,
            "total_chars": b.total_chars,
            "status": b.status,
            "stable_book_id": _get_stable_id(db, b.id),
            "has_cover": has_cover,
            "time_created": b.time_created,
            "time_updated": b.time_updated,
            "sort_order": b.sort_order,
            "last_opened_at": b.last_opened_at,
            "last_read_at": last_read_at,
            "last_chapter": last_chapter,
            "last_chunk": last_chunk,
        })

    return web.json_response({"books": enriched})


async def handle_set_book_order(request: web.Request) -> web.Response:
    """Persist the shelf's manual book order.

    Body: {"ids": [...]} — the full ordered id list. Position becomes
    sort_order = index + 1 (0 stays reserved for "never ranked").
    """
    db = request.app["db"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return web.json_response({"error": "ids must be a list of strings"}, status=400)

    try:
        updated = db.set_book_order(ids)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"ok": True, "updated": updated})


async def handle_update_book(request: web.Request) -> web.Response:
    """Manually edit book metadata (title / author).

    电子书源带的书名常不干净；导入时的 LLM 清洗是一次性的、也可能失手，
    这里是人工兜底。校验口径与 metadata_enrich 对齐（书名非空 + 长度上限）。
    """
    from services.metadata_enrich import MAX_AUTHOR, MAX_TITLE

    db = request.app["db"]
    book_id = request.match_info["book_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    updates: dict[str, str] = {}
    if "title" in body:
        title = str(body["title"]).strip()[:MAX_TITLE]
        if not title:
            return web.json_response({"error": "书名不能为空"}, status=400)
        updates["title"] = title
    if "author" in body:
        # 作者允许清空（未知作者）；空串入库，前端统一按 b.author || '' 消费
        updates["author"] = str(body["author"]).strip()[:MAX_AUTHOR]
    if not updates:
        return web.json_response(
            {"error": "nothing to update — supply title and/or author"}, status=400)

    if not db.get_book(book_id):
        return web.json_response({"error": "book not found"}, status=404)

    db.update_book(book_id, **updates)
    fresh = db.get_book(book_id)
    return web.json_response({
        "ok": True,
        "book": {"id": fresh.id, "title": fresh.title, "author": fresh.author},
    })


async def handle_context(request: web.Request) -> web.Response:
    """Return a structured reading map for a book."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    # 「打开即打点」：前端 selectBook() 打开一本书的必经路径就是这里。
    # 与 last_read_at 不同 —— 那个只在真正记录阅读位置（翻页/滚动同步）时
    # 才更新，只点开不翻页的书不会变。书架「最近打开」排序消费这个字段。
    # best-effort：打点失败绝不能连累打开书籍本身。
    try:
        db.touch_book_opened(book_id)
    except Exception:
        pass

    ctx = get_book_context(db, book_id)
    return web.json_response(ctx)


async def handle_chunk(request: web.Request) -> web.Response:
    """Return a specific chunk's content."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    try:
        chapter = int(request.query.get("chapter", 0))
        chunk = int(request.query.get("chunk", 0))
    except ValueError:
        return web.json_response({"error": "invalid chapter/chunk"}, status=400)

    ck = db.get_chunk(book_id, chapter, chunk)
    if not ck:
        return web.json_response(
            {"error": f"chunk not found: ch.{chapter} ck.{chunk}"}, status=404
        )

    ch = db.get_chapter(book_id, chapter)
    total = db.get_chunk_count(book_id, chapter)

    # Adjacent chunks — cross chapter boundaries when needed
    prev_anchor = _find_prev_chunk(db, book_id, chapter, chunk)
    next_anchor = _find_next_chunk(db, book_id, chapter, chunk)

    # Record reading position for persistence — opt-in via ?record=1.
    # The chunk view (real user page turns) passes it. Other callers —
    # notably the native reader fetching anchor text inside
    # jumpToAnchor(skipRecord:true) — must NOT write progress, or the
    # frontend's skip-record exemption is bypassed server-side. Deduped
    # like /api/sync so repeated opens don't bloat positions_json.
    if request.query.get("record") == "1":
        try:
            session = db.get_active_session(book_id)
            if not session:
                session = db.start_reading_session(book_id)
            last = db.get_last_position(session.id)
            if last != (chapter, chunk):
                db.record_position(session.id, chapter, chunk)
        except Exception:
            pass  # Best-effort; don't block chunk loading

    return web.json_response({
        "content": ck.content,
        "chapter_title": ch.title if ch else "",
        "chapter_type": ch.chapter_type if ch else "body",
        "chunk_index": chunk,
        "total_chunks": total,
        "prev_anchor": prev_anchor,
        "next_anchor": next_anchor,
        "char_count": len(ck.content) if ck else 0,
        "progress": {
            "chapter": chapter,
            "chunk": chunk,
            "total_chunks": total,
        },
        "prev": prev_anchor,
        "next": next_anchor,
    })


async def handle_cover(request: web.Request) -> web.Response:
    """Return cover image for a book, or a generated SVG placeholder."""
    from pathlib import Path as _Path

    db = request.app["db"]
    book_id = request.match_info["book_id"]

    book = db.get_book(book_id)
    if not book:
        return web.json_response({"error": "Book not found"}, status=404)

    # Look for extracted cover in bookbank
    bookbank = db._path.parent / "bookbank"
    cover_file = None
    for ext in ("jpg", "jpeg", "png", "gif", "webp", "svg"):
        candidate = bookbank / f"{book_id}_cover.{ext}"
        if candidate.exists():
            cover_file = candidate
            break

    if cover_file:
        # 书架只要 ~400px 宽的缩略图：原图动辄 1500px+/800KB，快速下滑时
        # 下载和解码都跟不上。缩略图缺失时现生成一份（首次请求付一次
        # 代价），生成失败就退回原图 —— 任何情况下都不能少一张封面。
        if request.query.get("thumb") in ("1", "true", "yes"):
            thumb = cover_thumb_path(cover_file)
            if not thumb.exists():
                thumb = await asyncio.to_thread(make_cover_thumb, cover_file)
            if thumb and thumb.exists():
                cover_file = thumb
        content_type = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif",
            "webp": "image/webp", "svg": "image/svg+xml",
        }.get(cover_file.suffix.lstrip("."), "image/jpeg")
        # Covers are immutable per book_id — cache them so shelf
        # re-renders (sorting, row regrouping on resize) don't
        # re-download every image.
        st = cover_file.stat()
        # 与 FileResponse 内部生成的 etag 同格式，否则 304 协商永远对不上
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        headers = {"Cache-Control": "private, max-age=604800"}
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={**headers, "ETag": etag})
        # FileResponse 把读取交给事件循环外的线程，不会在同步 read_bytes
        # 上卡住并发的其余封面请求。
        return web.FileResponse(
            path=cover_file,
            headers={"Content-Type": content_type, **headers},
        )

    # Fallback: generate a placeholder SVG cover
    title = book.title or "Book"
    initials = title[:4]
    svg = _generate_placeholder_cover(initials)
    return web.Response(
        text=svg,
        content_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=300"},
    )


async def handle_book_file(request: web.Request) -> web.Response:
    """Serve the original EPUB file from bookbank for native rendering.

    Only EPUB books are served — MOBI/txt have no native render path and
    the frontend falls back to chunk rendering. The file is resolved from
    bookbank by book_id (the same copy the backend parsed at import),
    never from source_path.
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    book = db.get_book(book_id)
    if not book:
        return web.json_response({"error": "Book not found"}, status=404)
    if (book.format or "").lower() != "epub":
        return web.json_response({"error": "only epub supported"}, status=415)

    bookbank = db._path.parent / "bookbank"
    candidate = bookbank / f"{book_id}.epub"
    try:
        resolved = candidate.resolve()
    except OSError:
        return web.json_response({"error": "file missing"}, status=404)
    try:
        if not resolved.is_relative_to(bookbank.resolve()):
            return web.json_response({"error": "book not found"}, status=404)
    except ValueError:
        return web.json_response({"error": "book not found"}, status=404)
    if not resolved.is_file():
        return web.json_response({"error": "file missing"}, status=404)

    return web.FileResponse(
        path=resolved,
        headers={"Content-Type": "application/epub+zip"},
    )


def _generate_placeholder_cover(text: str) -> str:
    """Generate a warm-toned SVG placeholder cover with title text."""
    import html as _html
    escaped = _html.escape(text)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="240" height="320" viewBox="0 0 240 320">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#4a3828"/>
      <stop offset="50%" stop-color="#5c4430"/>
      <stop offset="100%" stop-color="#3d2e20"/>
    </linearGradient>
  </defs>
  <rect width="240" height="320" fill="url(#bg)" rx="4"/>
  <rect x="8" y="4" width="224" height="312" fill="none" stroke="#6b5d4f" stroke-width="1" rx="3"/>
  <rect x="14" y="10" width="212" height="300" fill="none" stroke="#5a4d40" stroke-width="0.5" rx="2"/>
  <text x="120" y="150" text-anchor="middle" fill="#9a8d7e" font-family="serif" font-size="28" font-weight="bold">{escaped}</text>
  <line x1="60" y1="165" x2="180" y2="165" stroke="#6b5d4f" stroke-width="0.5"/>
</svg>"""


async def handle_import_book(request: web.Request) -> web.Response:
    """Import a book from a server-local file path.

    POST body: {path: "/path/to/your/book.epub"}
    Returns the imported book metadata, or an error.
    """
    db = request.app["db"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    path = body.get("path", "").strip()
    if not path:
        return web.json_response({"error": "path is required"}, status=400)

    from services.library_service import import_book

    try:
        book, is_new = import_book(db, path)
    except FileNotFoundError:
        return web.json_response({"error": f"file not found: {path}"}, status=404)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    # Fire one-shot LLM metadata cleanup for newly imported books (async,
    # non-blocking).  Re-imports (is_new=False) and missing api_client skip.
    if is_new and request.app.get("api_client"):
        asyncio.create_task(_enrich_book_metadata_async(
            request.app["api_client"], db, book.id,
        ))

    return web.json_response({
        "imported": is_new,
        "book": {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "format": book.format,
            "chapter_count": book.chapter_count,
            "total_chars": book.total_chars,
            "status": book.status,
        },
    })


async def handle_scan_folder(request: web.Request) -> web.Response:
    """Scan a folder for EPUB/MOBI files not yet imported.

    GET /api/books/scan-folder?path=/some/folder
    Returns a list of unimported files with size and modification time.
    """
    from pathlib import Path

    db = request.app["db"]
    folder = request.query.get("path", "").strip()
    if not folder:
        # 未显式传 path → 用配置的默认卸货区（.env BOOK_COMPANION_SCAN_FOLDER /
        # settings.json scan_folder），也没有 → 400 让前端引导输入。
        cfg = request.app.get("config")
        folder = (getattr(cfg, "scan_folder", "") or "").strip() if cfg else ""
    if not folder:
        return web.json_response(
            {"error": "未配置扫描文件夹：请传 path，或在 .env 设置 "
                      "BOOK_COMPANION_SCAN_FOLDER"},
            status=400)

    folder_path = Path(folder)
    if not folder_path.is_dir():
        return web.json_response({"error": f"not a directory: {folder}"}, status=404)

    # Collect all EPUB/MOBI files
    epub_files = list(folder_path.glob("*.epub"))
    mobi_files = list(folder_path.glob("*.mobi"))
    all_files = epub_files + mobi_files

    # 去重键必须是文件内容指纹（book_id 就是原文件字节的 MD5），不能看
    # source_path —— 它指向 bookbank/ 里的副本，用户扫描目录里的原路径
    # 永远匹配不上，所以已入库的书会一直出现在待导入列表里。同名不同出版
    # 社/译本的书内容不同 → 指纹不同 → 仍会列出，符合「不同指纹不去重」。
    imported_ids = {b.id for b in db.list_books(limit=10000)}

    def _scan_candidates() -> list[dict]:
        """hashing 是阻塞 IO，放到线程里跑，别冻住事件循环。"""
        rows: list[dict] = []
        for fp in all_files:
            if not fp.is_file():
                continue
            try:
                stat = fp.stat()
                fingerprint = _hash_file(fp)
            except OSError:
                # 读不了的文件仍列出，让 import 阶段给出明确报错
                try:
                    stat = fp.stat()
                except OSError:
                    continue
                fingerprint = ""
            if fingerprint and fingerprint in imported_ids:
                continue
            rows.append({
                "path": str(fp),
                "name": fp.stem,
                "format": fp.suffix.lower().lstrip("."),
                "size_mb": round(stat.st_size / (1024 * 1024), 1),
                "modified_at": int(stat.st_mtime),
            })
        return rows

    unimported = await asyncio.to_thread(_scan_candidates)

    # Sort by name
    unimported.sort(key=lambda f: f["name"].lower())

    return web.json_response({
        "folder": str(folder_path),
        "total_files": len(all_files),
        "already_imported": len(all_files) - len(unimported),
        "unimported": unimported,
    })


async def handle_upload_book(request: web.Request) -> web.Response:
    """Upload a book file from the browser and import it.

    Accepts multipart/form-data with a single file field named "file".
    Supports .epub, .mobi, .txt files up to 100MB.

    Returns the imported book metadata (same format as /api/books/import).
    """
    db = request.app["db"]

    # Parse multipart — grab the first file field
    reader = await request.multipart()
    file_field = None
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            file_field = field
            break
        field = await reader.next()

    if file_field is None:
        return web.json_response(
            {"error": "未找到文件字段。请使用 file 字段上传文件。"},
            status=400,
        )

    filename = file_field.filename or "uploaded.book"
    ext = Path(filename).suffix.lower()
    if ext not in (".epub", ".mobi", ".txt"):
        return web.json_response(
            {"error": f"不支持的文件格式: {ext}。支持: .epub, .mobi, .txt"},
            status=400,
        )

    # Save uploaded file to a temp location in <user>/data/watch/
    from runtime_paths import data_path
    watch_dir = data_path("watch")
    watch_dir.mkdir(parents=True, exist_ok=True)

    import uuid
    temp_name = f"upload_{uuid.uuid4().hex[:8]}{ext}"
    temp_path = watch_dir / temp_name

    size = 0
    with open(temp_path, "wb") as f:
        while True:
            chunk = await file_field.read_chunk(65536)  # 64KB chunks
            if not chunk:
                break
            f.write(chunk)
            size += len(chunk)
            if size > 104_857_600:  # 100MB limit
                f.close()
                temp_path.unlink(missing_ok=True)
                return web.json_response(
                    {"error": "文件过大（上限 100MB）"}, status=413
                )

    if size == 0:
        temp_path.unlink(missing_ok=True)
        return web.json_response({"error": "上传的文件为空"}, status=400)

    from services.library_service import import_book

    try:
        book, is_new = import_book(db, str(temp_path))
    except FileNotFoundError:
        temp_path.unlink(missing_ok=True)
        return web.json_response({"error": "文件保存失败"}, status=500)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        return web.json_response({"error": str(exc)}, status=500)

    # Fire one-shot LLM metadata cleanup for newly imported books (async,
    # non-blocking).  Re-imports (is_new=False) and missing api_client skip.
    if is_new and request.app.get("api_client"):
        asyncio.create_task(_enrich_book_metadata_async(
            request.app["api_client"], db, book.id,
        ))

    # Clean up the temp upload file — import_book already copied to bookbank
    temp_path.unlink(missing_ok=True)

    return web.json_response({
        "imported": is_new,
        "book": {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "format": book.format,
            "chapter_count": book.chapter_count,
            "total_chars": book.total_chars,
            "status": book.status,
        },
    })


async def handle_delete_book(request: web.Request) -> web.Response:
    """Delete a book and all related data (chapters, chunks, reading sessions, files).

    Returns the deleted book's summary for confirmation.
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]

    from services.library_service import delete_book

    try:
        summary = delete_book(db, book_id)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)

    return web.json_response({"deleted": True, **summary})


async def handle_search_book(request: web.Request) -> web.Response:
    """Sentence-level full-text search within a book."""
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    query = request.query.get("q", "").strip()
    if not query or len(query) < 1:
        return web.json_response({"error": "q parameter is required"}, status=400)

    limit = min(int(request.query.get("limit", "30")), 50)
    from services.library_service import search_sentences

    results = search_sentences(db, book_id, query, limit=limit)
    return web.json_response({
        "query": query,
        "book_id": book_id,
        "count": len(results),
        "results": results,
    })


async def handle_search_overview(request: web.Request) -> web.Response:
    """Per-chapter hit-count overview for a search query within a book.

    Query params:
        q       — search string (required, min 1 char)
        current — current chapter index (optional, for proximity sorting)

    Returns a list of chapters that contain hits, each with a hit_count.
    When *current* is provided, chapters are sorted by proximity to it.
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"error": "q parameter is required"}, status=400)

    from services.library_service import search_chapter_overview

    chapters = search_chapter_overview(db, book_id, query)

    # Proximity sort: chapters closest to current reading position first
    current_str = request.query.get("current")
    if current_str:
        try:
            current_ch = int(current_str)
            chapters.sort(
                key=lambda c: (abs(c["chapter_index"] - current_ch),
                                c["chapter_index"])
            )
        except ValueError:
            pass

    total_hits = sum(c["hit_count"] for c in chapters)
    return web.json_response({
        "query": query,
        "book_id": book_id,
        "total_chapters": len(chapters),
        "total_hits": total_hits,
        "chapters": chapters,
    })


async def handle_search_chapter(request: web.Request) -> web.Response:
    """Sentence-level search, with pagination.

    Query params:
        q       — search string (required)
        chapter — chapter index (optional; when omitted, searches the
                  entire book in natural reading order)
        offset  — pagination offset (default 0)
        limit   — page size (default 20, max 50)
    """
    db = request.app["db"]
    book_id = request.match_info["book_id"]
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"error": "q parameter is required"}, status=400)

    chapter_str = request.query.get("chapter")
    chapter_index: int | None = None
    if chapter_str is not None:
        try:
            chapter_index = int(chapter_str)
        except ValueError:
            return web.json_response({"error": "chapter must be an integer"}, status=400)

    offset = max(int(request.query.get("offset", "0")), 0)
    limit = min(max(int(request.query.get("limit", "20")), 1), 50)

    from services.library_service import search_chapter_sentences

    result = search_chapter_sentences(
        db, book_id, query, chapter_index, offset=offset, limit=limit,
    )
    return web.json_response({
        "query": query,
        "book_id": book_id,
        "chapter_index": chapter_index,
        **result,
    })
