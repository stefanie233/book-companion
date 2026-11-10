"""作者档案 API —— 投影读取 + 用户驱动的细节采集。

旧接口 ``/api/authors/{book_id}`` 与 ``/api/authors/{book_id}/{author_id}/timeline``
仍在 ``api/routes/graph.py`` 里保持原样；这里是设计稿 05 的新一层：

* ``GET /api/authors``                        搜索/列表
* ``GET /api/authors/{author_id}/dossier``    完整档案
* ``GET /api/authors/{author_id}/voice``      声音分片（懒加载用）
* ``GET /api/authors/{author_id}/sources``    来源分片（抽屉用）
* ``GET /api/authors/{author_id}/plans``      攻略（含黑白名单）
* ``PATCH /api/authors/{author_id}/plans/{plan_id}``  只有用户能改名单
* ``POST /api/authors/{author_id}/jobs``      生成攻略/抽取细节/闭合钩子
* ``GET /api/authors/{author_id}/jobs``       该作者最近的任务
* ``GET /api/authors/jobs/{job_id}``          任务进度（轮询）
* ``POST /api/authors/jobs/{job_id}/cancel``  取消任务

采集是长任务，走 ``api/author_jobs.py`` 的独立子进程 —— 不在服务进程里跑。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from api.author_jobs import ACTIONS, AuthorJobRunner
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


# ── 攻略与采集任务 ────────────────────────────────────────────────────────

def _job_runner(request: web.Request) -> AuthorJobRunner:
    runner = request.app.get("author_jobs")
    if runner is None:
        cfg = request.app.get("config")
        from runtime_paths import user_root
        root = getattr(cfg, "project_dir", "") or user_root()
        runner = AuthorJobRunner(Path(root))
        request.app["author_jobs"] = runner
    return runner


def _book_titles(db: Any, plans: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    if db is None:
        return out
    for p in plans:
        bid = (p.get("book_id") or "").strip()
        if not bid or bid in out:
            continue
        try:
            book = db.get_book(bid)
            out[bid] = (getattr(book, "title", "") or "") if book else ""
        except Exception:                                  # noqa: BLE001
            out[bid] = ""
    return out


async def handle_author_plans(request: web.Request) -> web.Response:
    """GET /api/authors/{author_id}/plans —— 全部攻略，黑名单也给。"""
    author_id = request.match_info["author_id"]
    adb = _adb(request)
    try:
        if not adb.get_author(author_id):
            return web.json_response({"error": "作者不存在"}, status=404)
        plans = adb.plans_for(author_id, open_only=False)
        titles = _book_titles(request.app.get("db"), plans)
        items = [{
            "plan_id": p.get("plan_id"),
            "target": p.get("target") or "",
            "scope": p.get("scope") or "",
            "stable_book_id": p.get("stable_book_id") or "",
            "book_id": p.get("book_id") or "",
            "book_title": titles.get(p.get("book_id") or "", ""),
            "hypothesis": p.get("hypothesis") or "",
            "expected_basis": p.get("expected_basis") or "",
            "anchor_required": p.get("anchor_required") or "",
            "expected_value": p.get("expected_value") or "unknown",
            "expected_cost": p.get("expected_cost") or 1,
            "verification_action": p.get("verification_action") or "",
            "list_type": p.get("list_type") or "grey",
            "status": p.get("status") or "proposed",
            "created_by": p.get("created_by") or "",
            "prompt_version": p.get("prompt_version") or "",
            "time_created": p.get("time_created"),
        } for p in plans]
        counts = {"plans": len(items)}
        for key in ("grey", "white", "black"):
            counts[key] = sum(1 for x in items if x["list_type"] == key)
        counts["pending"] = sum(1 for x in items
                                if x["status"] in ("proposed", "confirmed")
                                and x["list_type"] != "black")
        return web.json_response({"author_id": author_id, "items": items,
                                  "counts": counts})
    except Exception as exc:                              # noqa: BLE001
        log.warning("author plans failed: %s", exc, exc_info=True)
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        adb.close()


async def handle_author_plan_list(request: web.Request) -> web.Response:
    """PATCH .../plans/{plan_id} ``{"list_type": "grey|white|black"}``"""
    author_id = request.match_info["author_id"]
    plan_id = request.match_info["plan_id"]
    try:
        body = await request.json()
    except Exception:                                     # noqa: BLE001
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    list_type = str((body or {}).get("list_type") or "").strip()
    if list_type not in ("grey", "white", "black"):
        return web.json_response({"error": "list_type 只能是 grey/white/black"},
                                 status=400)
    adb = _adb(request)
    try:
        plan = adb.get_plan(plan_id)
        if not plan or plan.get("author_id") != author_id:
            return web.json_response({"error": "攻略不存在"}, status=404)
        adb.set_plan_list(plan_id, list_type)
        return web.json_response({"plan_id": plan_id,
                                  "list_type": list_type})
    except Exception as exc:                              # noqa: BLE001
        log.warning("author plan list failed: %s", exc, exc_info=True)
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        adb.close()


async def handle_author_job_start(request: web.Request) -> web.Response:
    """POST .../jobs ``{"action": "plan|extract|close_hooks", "force": bool}``"""
    author_id = request.match_info["author_id"]
    try:
        body = await request.json()
    except Exception:                                     # noqa: BLE001
        body = {}
    action = str((body or {}).get("action") or "").strip()
    if action not in ACTIONS:
        return web.json_response({"error": "action 只能是 "
                                  + "/".join(ACTIONS)}, status=400)
    force = bool((body or {}).get("force"))
    adb = _adb(request)
    try:
        author = adb.get_author(author_id)
    finally:
        adb.close()
    if not author:
        return web.json_response({"error": "作者不存在"}, status=404)
    job, busy = await _job_runner(request).start(
        author_id=author_id,
        author_name=author.get("canonical_name") or "",
        action=action, force=force)
    if busy:
        return web.json_response(
            {"error": "已有任务在跑，等它结束或先取消", "job": busy},
            status=409)
    return web.json_response({"job": job}, status=202)


async def handle_author_jobs(request: web.Request) -> web.Response:
    """GET .../jobs?limit=5 —— 该作者最近的任务（不含完整日志）。"""
    author_id = request.match_info["author_id"]
    limit = _int_query(request, "limit")
    limit = 5 if limit is None else max(1, min(20, limit))
    items = _job_runner(request).list_for(author_id, limit=limit)
    return web.json_response({"author_id": author_id, "items": items})


async def handle_author_job(request: web.Request) -> web.Response:
    """GET /api/authors/jobs/{job_id} —— 任务进度与日志尾部。"""
    job = _job_runner(request).get(request.match_info["job_id"])
    if not job:
        return web.json_response({"error": "任务不存在（服务可能重启过）"},
                                 status=404)
    return web.json_response({"job": job})


async def handle_author_job_cancel(request: web.Request) -> web.Response:
    """POST /api/authors/jobs/{job_id}/cancel"""
    job = await _job_runner(request).cancel(request.match_info["job_id"])
    if not job:
        return web.json_response({"error": "任务不存在"}, status=404)
    return web.json_response({"job": job})


__all__ = ["handle_authors_list", "handle_author_dossier",
           "handle_author_voice", "handle_author_sources",
           "handle_author_plans", "handle_author_plan_list",
           "handle_author_job_start", "handle_author_jobs",
           "handle_author_job", "handle_author_job_cancel"]
