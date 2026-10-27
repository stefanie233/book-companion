"""作者档案 API —— 只读投影，不调模型。

旧接口 ``/api/authors/{book_id}`` 与 ``/api/authors/{book_id}/{author_id}/timeline``
仍在 ``api/routes/graph.py`` 里保持原样；这里是设计稿 05 的新一层：

* ``GET /api/authors``                        搜索/列表
* ``GET /api/authors/{author_id}/dossier``    完整档案
* ``GET /api/authors/{author_id}/voice``      声音分片（懒加载用）
* ``GET /api/authors/{author_id}/sources``    来源分片（抽屉用）
"""

from __future__ import annotations

import logging

from aiohttp import web

from authorgraph.dossier import author_list, build_dossier
from storage.authors_db import AuthorsDB, resolve_authors_db_path

log = logging.getLogger("api")


def _adb(request: web.Request) -> AuthorsDB:
    config = request.app.get("config")
    return AuthorsDB(resolve_authors_db_path(config))


def _int_query(request: web.Request, name: str) -> int | None:
    raw = request.query.get(name)
    if raw in (None, ""):
        return None
    return int(raw)


async def handle_authors_list(request: web.Request) -> web.Response:
    """GET /api/authors?q=史铁&limit=20&offset=0&mode=auto"""
    try:
        limit = _int_query(request, "limit")
        offset = _int_query(request, "offset")
        limit = 20 if limit is None else max(1, min(100, limit))
        offset = 0 if offset is None else max(0, offset)
        mode = (request.query.get("mode") or "auto").strip().lower()
        if mode not in ("auto", "exact", "fuzzy"):
            mode = "auto"
    except ValueError:
        return web.json_response({"error": "limit/offset 必须是整数"}, status=400)
    adb = _adb(request)
    try:
        data = author_list(adb, q=request.query.get("q") or "",
                           limit=limit, offset=offset, mode=mode)
        return web.json_response(data)
    except Exception as exc:                              # noqa: BLE001
        log.warning("author list failed: %s", exc)
        return web.json_response({"error": str(exc), "items": [],
                                  "total": 0}, status=500)
    finally:
        adb.close()


async def handle_author_dossier(request: web.Request) -> web.Response:
    """GET /api/authors/{author_id}/dossier?book_id=&max_chapter=&sections="""
    author_id = request.match_info["author_id"]
    book_id = (request.query.get("book_id") or "").strip()
    try:
        max_chapter = _int_query(request, "max_chapter")
        if max_chapter is not None and max_chapter < 0:
            raise ValueError
    except ValueError:
        return web.json_response({"error": "max_chapter 必须是非负整数"},
                                 status=400)
    adb = _adb(request)
    try:
        data = build_dossier(
            adb, author_id=author_id, book_id=book_id,
            max_chapter=max_chapter, books_db=request.app.get("db"),
            sections=request.query.get("sections") or "")
        if not data.get("found"):
            return web.json_response(data, status=404)
        return web.json_response(data)
    except Exception as exc:                              # noqa: BLE001
        log.warning("author dossier failed: %s", exc, exc_info=True)
        return web.json_response({"error": str(exc), "found": False},
                                 status=500)
    finally:
        adb.close()


async def handle_author_voice(request: web.Request) -> web.Response:
    """GET /api/authors/{author_id}/voice?book_id=&max_chapter=&limit="""
    author_id = request.match_info["author_id"]
    book_id = (request.query.get("book_id") or "").strip()
    try:
        max_chapter = _int_query(request, "max_chapter")
        if max_chapter is not None and max_chapter < 0:
            raise ValueError
    except ValueError:
        return web.json_response({"error": "max_chapter 必须是非负整数"},
                                 status=400)
    adb = _adb(request)
    try:
        data = build_dossier(
            adb, author_id=author_id, book_id=book_id,
            max_chapter=max_chapter, books_db=request.app.get("db"),
            sections={"voice"}, use_cache=False)
        if not data.get("found"):
            return web.json_response(data, status=404)
        voice = data.get("voice") or {}
        voice["author_id"] = author_id
        voice["gate"] = data.get("gate")
        return web.json_response(voice)
    except Exception as exc:                              # noqa: BLE001
        log.warning("author voice failed: %s", exc, exc_info=True)
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        adb.close()


async def handle_author_sources(request: web.Request) -> web.Response:
    """GET /api/authors/{author_id}/sources?book_id=&max_chapter=&target_type=&target_id="""
    author_id = request.match_info["author_id"]
    book_id = (request.query.get("book_id") or "").strip()
    try:
        max_chapter = _int_query(request, "max_chapter")
        if max_chapter is not None and max_chapter < 0:
            raise ValueError
    except ValueError:
        return web.json_response({"error": "max_chapter 必须是非负整数"},
                                 status=400)
    target_type = (request.query.get("target_type") or "").strip()
    target_id = (request.query.get("target_id") or "").strip()
    adb = _adb(request)
    try:
        data = build_dossier(
            adb, author_id=author_id, book_id=book_id,
            max_chapter=max_chapter, books_db=request.app.get("db"),
            sections={"sources"}, use_cache=False)
        if not data.get("found"):
            return web.json_response(data, status=404)
        sources = data.get("sources") or {}
        if target_type or target_id:
            for item in sources.get("items") or []:
                item["evidence"] = [
                    e for e in (item.get("evidence") or [])
                    if ((not target_type
                         or e.get("target_type") == target_type)
                        and (not target_id
                             or str(e.get("target_id")) == target_id))]
            sources["items"] = [x for x in (sources.get("items") or [])
                                if x["evidence"]]
        sources["author_id"] = author_id
        sources["gate"] = data.get("gate")
        sources["conflicts"] = data.get("conflicts") or []
        return web.json_response(sources)
    except Exception as exc:                              # noqa: BLE001
        log.warning("author sources failed: %s", exc, exc_info=True)
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        adb.close()


__all__ = ["handle_authors_list", "handle_author_dossier",
           "handle_author_voice", "handle_author_sources"]
