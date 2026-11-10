"""WriteGraph endpoints: works, chapters, review, writing-git (split out of api_server.py)."""
from __future__ import annotations

import logging

from aiohttp import web

from api.sse import _sse_write_json


# ── WriteGraph ──────────────────────────────────────────────────────────

def _writing_service(request: web.Request):
    """按需建服务；``api_client`` 由 main.py 在启动后注入，每次取最新的。"""
    from services.writing_service import WritingService
    svc = request.app.get("writing_service")
    if svc is None:
        svc = WritingService(
            request.app["config"],
            api_client=request.app.get("api_client"),
            recorder=request.app.get("context_recorder"))
        request.app["writing_service"] = svc
    else:
        svc.api_client = request.app.get("api_client")
        svc.recorder = request.app.get("context_recorder")
    return svc


def _writing_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _style_query(request: web.Request) -> dict | None:
    """``?style_author_id=…&style_scene=…`` → style_ref；没选作者就是 None。"""
    aid = (request.query.get("style_author_id") or "").strip()
    if not aid:
        return None
    return {
        "author_id": aid,
        "scene_type": (request.query.get("style_scene") or "").strip(),
        "period": (request.query.get("style_period") or "").strip(),
        "pov": (request.query.get("style_pov") or "").strip(),
    }


async def handle_writing_works(request: web.Request) -> web.Response:
    return web.json_response({"ok": True,
                              "works": _writing_service(request).list_works()})


async def handle_writing_create_work(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        work_id = _writing_service(request).create_work(
            body.get("title", ""), genre=body.get("genre", ""),
            premise=body.get("premise", ""),
            narrative_pov=body.get("narrative_pov", ""),
            target_chars=int(body.get("target_chars") or 0))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, "work_id": work_id})


async def handle_writing_work(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        detail = _writing_service(request).work_detail(
            request.match_info["work_id"])
    except WritingError as exc:
        return _writing_error(str(exc), 404)
    return web.json_response({"ok": True, **detail})


async def handle_writing_rename_work(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        work = _writing_service(request).rename_work(
            request.match_info["work_id"], str(body.get("title") or ""))
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, "work": work})


async def handle_writing_set_work_pin(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        result = _writing_service(request).set_work_pinned(
            request.match_info["work_id"], bool(body.get("pinned")))
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **result})


async def handle_writing_delete_work(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        result = _writing_service(request).delete_work(
            request.match_info["work_id"],
            confirm_title=str(body.get("confirm_title") or ""))
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **result})


async def handle_writing_map(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = _writing_service(request).map_data(
            request.match_info["work_id"])
    except WritingError as exc:
        return _writing_error(str(exc), 404)
    return web.json_response(result)


async def handle_writing_tension(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = _writing_service(request).tension_curve(
            request.match_info["work_id"])
    except WritingError as exc:
        return _writing_error(str(exc), 404)
    return web.json_response(result)


async def handle_writing_set_tension(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
        result = _writing_service(request).set_tension(
            request.match_info["work_id"], int(body["chapter_index"]),
            int(body["value"]), str(body.get("note") or ""))
    except (KeyError, ValueError, TypeError) as exc:
        return _writing_error(f"chapter_index 与 value 必填：{exc}")
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response(result)


async def handle_writing_create_chapter(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        chapter = _writing_service(request).create_chapter(
            request.match_info["work_id"], title=body.get("title", ""),
            contract=body.get("contract"))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, "chapter": chapter})


async def handle_writing_chapter(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    svc = _writing_service(request)
    try:
        chapter = svc.chapter(request.match_info["work_id"],
                              int(request.match_info["index"]))
    except (WritingError, ValueError) as exc:
        return _writing_error(str(exc), 404)
    return web.json_response({
        "ok": True, "chapter": chapter,
        "save_point": svc.save_point(chapter),
        "contract": svc.contract(chapter["id"])})


async def handle_writing_saved_content(request: web.Request) -> web.Response:
    """最近一个保存点的正文——「回到上次保存」用。"""
    from services.writing_service import WritingError
    try:
        data = _writing_service(request).saved_content(
            request.match_info["work_id"], int(request.match_info["index"]))
    except (WritingError, ValueError) as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 409)
    return web.json_response({"ok": True, **data})


async def handle_writing_rename_chapter(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        chapter = _writing_service(request).rename_chapter(
            request.match_info["work_id"], int(request.match_info["index"]),
            str(body.get("title") or ""))
    except (ValueError, TypeError) as exc:
        return _writing_error(str(exc))
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, "chapter": chapter})


async def handle_writing_context(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    index = int(request.match_info["index"])
    try:
        assembled = _writing_service(request).assemble(
            request.match_info["work_id"], index,
            style_ref=_style_query(request))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **assembled})


async def handle_writing_style_get(request: web.Request) -> web.Response:
    """作品当前的参考风格设置（默认作者 + 原始 style_profile）。"""
    from services.writing_service import WritingError
    try:
        data = _writing_service(request).get_style_profile(
            request.match_info["work_id"])
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **data})


async def handle_writing_style_set(request: web.Request) -> web.Response:
    """设置默认参考作者。P1 存在 works.style_profile，不新增表。"""
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        data = _writing_service(request).set_style_ref(
            request.match_info["work_id"],
            str(body.get("author_id") or ""),
            enabled=bool(body.get("enabled", True)),
            scope_type=str(body.get("scope_type") or "author"),
            scope_ref=str(body.get("scope_ref") or ""),
            scene_type=str(body.get("scene_type") or ""),
            period=str(body.get("period") or ""))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **data})


async def handle_writing_references_get(request: web.Request) -> web.Response:
    """作品选定的参考书与素材形态。"""
    from services.writing_service import WritingError
    try:
        data = _writing_service(request).get_style_profile(
            request.match_info["work_id"])
    except WritingError as exc:
        return _writing_error(str(exc))
    profile = data.get("style_profile") or {}
    return web.json_response({
        "ok": True,
        "reference_books": profile.get("reference_books") or [],
        "reference_mode": profile.get("reference_mode") or "card"})


async def handle_writing_references_set(request: web.Request) -> web.Response:
    """整组替换参考书；``books`` 为空即清空。"""
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        data = _writing_service(request).set_reference_books(
            request.match_info["work_id"], body.get("books") or [],
            mode=str(body.get("mode") or "card"))
    except (ValueError, TypeError) as exc:
        return _writing_error(str(exc))
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **data})


async def handle_writing_style_check(request: web.Request) -> web.Response:
    """生成后风格检查：只报告，不阻断。"""
    from services.writing_service import WritingError
    try:
        result = _writing_service(request).style_check(
            request.match_info["work_id"],
            int(request.match_info["index"]),
            style_ref=_style_query(request))
    except (WritingError, ValueError, TypeError) as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **result})


async def handle_writing_contracts(request: web.Request) -> web.Response:
    """整本的合同收录（写作台左栏「合同」）。"""
    from services.writing_service import WritingError
    try:
        contracts = _writing_service(request).list_contracts(
            request.match_info["work_id"])
    except WritingError as exc:
        return _writing_error(str(exc), 404)
    return web.json_response({"ok": True, "contracts": contracts})


async def handle_writing_docs(request: web.Request) -> web.Response:
    """作品工作区目录（写作台左栏「草稿」）。"""
    from services.writing_service import WritingError
    try:
        data = _writing_service(request).docs_list(
            request.match_info["work_id"])
    except WritingError as exc:
        return _writing_error(str(exc), 404)
    return web.json_response({"ok": True, **data})


async def handle_writing_doc_file(request: web.Request) -> web.Response:
    """读工作区里的一份文件（路径相对作品目录，只认 作品记忆.md / 设定 / 计划稿 / 素材）。"""
    from services.writing_service import WritingError
    path = request.query.get("path") or ""
    try:
        row = _writing_service(request).docs_read(
            request.match_info["work_id"], path)
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **row})


async def handle_writing_doc_create(request: web.Request) -> web.Response:
    """新建工作区文件或文件夹（左栏右键「新建」）。"""
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        row = _writing_service(request).docs_create(
            request.match_info["work_id"], str(body.get("path") or ""),
            kind=str(body.get("kind") or "file"),
            content=str(body.get("content") or ""))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_doc_rename(request: web.Request) -> web.Response:
    """原地改名（父目录不变，页面只给新名字）。"""
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        row = _writing_service(request).docs_rename(
            request.match_info["work_id"], str(body.get("path") or ""),
            str(body.get("name") or ""),
            kind=str(body.get("kind") or "file"))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_doc_delete(request: web.Request) -> web.Response:
    """删除工作区文件或文件夹（文件夹连内容一起删）。"""
    from services.writing_service import WritingError
    try:
        row = _writing_service(request).docs_delete(
            request.match_info["work_id"],
            request.query.get("path") or "",
            kind=request.query.get("kind") or "file")
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_materials(request: web.Request) -> web.Response:
    """全局素材库目录（空项目左栏；有作品时也通过固定入口进来）。"""
    from services.writing_service import WritingError
    try:
        data = _writing_service(request).materials_list()
    except WritingError as exc:
        return _writing_error(str(exc), 404)
    return web.json_response({"ok": True, **data})


async def handle_writing_material_file(request: web.Request) -> web.Response:
    """读一张素材卡。"""
    from services.writing_service import WritingError
    path = request.query.get("path") or ""
    try:
        row = _writing_service(request).materials_read(path)
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **row})


async def handle_writing_material_save(request: web.Request) -> web.Response:
    """页面直接编辑素材卡正文（原料不是正典，允许页面改）。"""
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        row = _writing_service(request).materials_write(
            str(body.get("path") or ""), str(body.get("content") or ""))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_material_create(request: web.Request) -> web.Response:
    """新建素材文件或分类目录。"""
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        row = _writing_service(request).materials_create(
            str(body.get("path") or ""), kind=str(body.get("kind") or "file"),
            content=str(body.get("content") or ""))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_material_intake(request: web.Request) -> web.Response:
    """阅读页「收录为素材」：待归类卡落 待归类/《书名》-时间戳.md。"""
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        row = _writing_service(request).materials_intake_raw(
            excerpt=str(body.get("excerpt") or ""),
            book_id=str(body.get("book_id") or ""),
            book_title=str(body.get("book_title") or ""),
            chapter_index=body.get("chapter_index"),
            chunk_index=body.get("chunk_index"),
            cfi=str(body.get("cfi_range") or body.get("cfi") or ""),
            instruction=str(body.get("instruction") or ""))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_material_rename(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        row = _writing_service(request).materials_rename(
            str(body.get("path") or ""), str(body.get("name") or ""),
            kind=str(body.get("kind") or "file"))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_material_move(request: web.Request) -> web.Response:
    """换分类：保留文件名，消费连线不动。"""
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        row = _writing_service(request).materials_move(
            str(body.get("path") or ""), str(body.get("target_dir") or ""),
            kind=str(body.get("kind") or "file"))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_material_delete(request: web.Request) -> web.Response:
    """删除 = 进回收站。"""
    from services.writing_service import WritingError
    try:
        row = _writing_service(request).materials_delete(
            request.query.get("path") or "",
            kind=request.query.get("kind") or "file")
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_material_empty(request: web.Request) -> web.Response:
    """清空文件夹：里面的素材卡逐张进回收站，文件夹与子分类保留。"""
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        row = _writing_service(request).materials_empty(
            str(body.get("path") or ""))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_material_recycled(request: web.Request) -> web.Response:
    """回收站清单与积压情况。"""
    from services.writing_service import WritingError
    try:
        data = _writing_service(request).materials_recycled()
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **data})


async def handle_writing_material_restore(request: web.Request) -> web.Response:
    """从回收站放回去；不传 target 就回原分类。"""
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        row = _writing_service(request).materials_restore(
            str(body.get("path") or ""), target=str(body.get("target") or ""))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **row})


async def handle_writing_material_purge(request: web.Request) -> web.Response:
    """显式清理回收站。``all=true`` 清空全部（页面垃圾桶按钮，先确认）；
    不传则按年龄清理，默认 90 天（脚本/audit 的保留期语义）。"""
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if body.get("all"):
        days = 0                      # 0 = 不设保留期，全部清掉
    else:
        raw = body.get("days")
        try:
            days = 90 if raw is None else int(raw)
        except (TypeError, ValueError):
            return _writing_error("days 要是整数")
    try:
        data = _writing_service(request).materials_purge_recycled(days=days)
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **data})


async def handle_writing_material_audit(request: web.Request) -> web.Response:
    """素材库盘点：只报告，不改。"""
    from services.writing_service import WritingError
    try:
        days = int(request.query.get("unused_days") or 90)
    except ValueError:
        return _writing_error("unused_days 要是整数")
    try:
        data = _writing_service(request).materials_audit(unused_days=days)
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **data})


async def handle_writing_material_search(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        data = _writing_service(request).materials_search(
            request.query.get("q") or "",
            limit=int(request.query.get("limit") or 10))
    except (WritingError, ValueError) as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **data})


async def handle_writing_material_uses(request: web.Request) -> web.Response:
    """素材的消费历史：按 material_id 或 work_id 查。"""
    from services.writing_service import WritingError
    try:
        data = _writing_service(request).materials_uses(
            material_id=request.query.get("material_id") or "",
            work_id=request.query.get("work_id") or "",
            limit=int(request.query.get("limit") or 100))
    except (WritingError, ValueError) as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **data})


async def handle_writing_contract(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    svc = _writing_service(request)
    try:
        body = await request.json()
        chapter = svc.chapter(request.match_info["work_id"],
                              int(request.match_info["index"]))
        contract = svc.set_contract(chapter["id"], **body)
    except (ValueError, TypeError) as exc:
        return _writing_error(str(exc))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, "contract": contract})


async def handle_writing_content(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    # 自动保存/动作前落盘/暂存与手动保存走同一条写入口，只靠 reason 区分来源。
    # ``reason`` 记来源，``kind`` 决定这一版是暂存稿还是保存点。
    reason = str(body.get("reason") or "manual").strip()
    if reason not in ("manual", "autosave", "flush", "park"):
        reason = "manual"
    kind = str(body.get("kind") or "").strip()
    if kind not in ("draft", "save"):
        kind = None
    try:
        saved = _writing_service(request).save_content(
            request.match_info["work_id"],
            int(request.match_info["index"]),
            body.get("content", ""), reason=reason,
            title=body.get("title"), kind=kind)
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response({"ok": True, **saved})


async def handle_writing_write(request: web.Request) -> web.StreamResponse:
    """POST …/write — SSE：stage / delta / done / error。"""
    from services.writing_service import WritingError
    svc = _writing_service(request)
    work_id = request.match_info["work_id"]
    index = int(request.match_info["index"])
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    try:
        # 「生成」按钮是标准路径，但它不能绕过**单章**的细致覆盖：
        # 细致模式要求动笔前交 research_brief，这个按钮给不出。
        mode = svc.effective_writing_mode(work_id, index, "standard")
        if mode == "detail":
            raise WritingError(
                "这一章是细致模式：请在对话里让写作助手起草"
                "（它会先交 research_brief），或在对话里把这一章降回标准模式。")
        async for event in svc.stream_chapter(
                work_id, index, style_ref=_style_query(request), mode=mode):
            await _sse_write_json(resp, event)
    except WritingError as exc:
        await _sse_write_json(resp, {"event": "error",
                                     "data": {"message": str(exc)}})
    except Exception as exc:                       # noqa: BLE001
        import logging
        logging.getLogger("writegraph").exception("writing failed")
        await _sse_write_json(resp, {"event": "error",
                                     "data": {"message": str(exc)}})
    await resp.write(b"data: [DONE]\n\n")
    return resp


async def handle_writing_settle(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = await _writing_service(request).settle(
            request.match_info["work_id"], int(request.match_info["index"]))
    except WritingError as exc:
        return _writing_error(str(exc))
    status = 200 if result.get("ok") else 422
    return web.json_response(result, status=status)


async def handle_writing_review(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = await _writing_service(request).review_chapter(
            request.match_info["work_id"], int(request.match_info["index"]))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response(result)


async def handle_writing_get_review(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = _writing_service(request).get_review(
            request.match_info["work_id"], int(request.match_info["index"]))
    except (WritingError, ValueError) as exc:
        return _writing_error(str(exc), 404)
    return web.json_response(result)


async def handle_writing_revisions(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = _writing_service(request).list_revisions(
            request.match_info["work_id"], int(request.match_info["index"]))
    except (WritingError, ValueError) as exc:
        return _writing_error(str(exc), 404)
    return web.json_response(result)


async def handle_writing_propose_revision(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = await _writing_service(request).propose_revision(
            request.match_info["work_id"], int(request.match_info["index"]),
            request.match_info["finding_id"])
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response(result)


async def handle_writing_apply_revision(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        result = _writing_service(request).apply_revision(
            request.match_info["work_id"], int(request.match_info["index"]),
            request.match_info["revision_id"],
            expected_hash=body.get("expected_hash"))
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response(result)


async def handle_writing_reject_revision(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = _writing_service(request).reject_revision(
            request.match_info["work_id"], int(request.match_info["index"]),
            request.match_info["revision_id"])
    except WritingError as exc:
        return _writing_error(str(exc))
    return web.json_response(result)


async def handle_writing_vcs_status(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = _writing_service(request).vcs_status(
            request.match_info["work_id"])
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **result})


async def handle_writing_vcs_log(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        limit = min(max(int(request.query.get("limit", "50")), 1), 500)
    except ValueError:
        return _writing_error("limit 必须是整数")
    try:
        commits = _writing_service(request).vcs_log(
            request.match_info["work_id"],
            branch=request.query.get("branch", ""),
            all_branches=request.query.get("all", "").lower() in (
                "1", "true", "yes"),
            chapter_id=request.query.get("chapter_id", ""),
            mark_chapter=request.query.get("mark", "").lower() in (
                "1", "true", "yes"),
            limit=limit)
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, "commits": commits})


async def handle_writing_file_timeline(request: web.Request) -> web.Response:
    """一份工作区文件的 git 历史（左栏「时间线」）。"""
    from services.writing_service import WritingError
    try:
        limit = min(max(int(request.query.get("limit", "50")), 1), 200)
    except ValueError:
        return _writing_error("limit 必须是整数")
    try:
        data = _writing_service(request).vcs_file_timeline(
            request.match_info["work_id"],
            request.query.get("path") or "", limit=limit)
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **data})


async def handle_writing_vcs_diff(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    text = request.query.get("text", "").lower() in ("1", "true", "yes")
    try:
        result = _writing_service(request).vcs_diff(
            request.match_info["work_id"],
            from_ref=request.query.get("from", "HEAD"),
            to_ref=request.query.get("to", "WORK"),
            chapter_id=request.query.get("chapter_id", ""),
            file_path=request.query.get("file_path", ""),
            text=text)
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **result})


async def handle_writing_vcs_commit(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    work_id = request.match_info["work_id"]
    svc = _writing_service(request)
    try:
        commit = svc.vcs_commit(
            work_id, str(body.get("message") or ""),
            author=str(body.get("author") or "user"),
            kind=str(body.get("kind") or "manual"))
        status = svc.vcs_status(work_id)
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, "commit": commit, "status": status})


async def handle_writing_vcs_branches(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        branches = _writing_service(request).vcs_branches(
            request.match_info["work_id"])
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, "branches": branches})


async def handle_writing_vcs_create_branch(
        request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        branch = _writing_service(request).vcs_create_branch(
            request.match_info["work_id"], str(body.get("name") or ""),
            from_ref=str(body.get("from") or "HEAD"))
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, "branch": branch})


async def handle_writing_vcs_checkout(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        status = _writing_service(request).vcs_checkout(
            request.match_info["work_id"], str(body.get("branch") or ""),
            checkpoint=bool(body.get("checkpoint", True)))
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **status})


async def handle_writing_vcs_restore(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        body = await request.json()
    except ValueError:
        return _writing_error("invalid json")
    try:
        result = _writing_service(request).vcs_restore(
            request.match_info["work_id"], str(body.get("commit_id") or ""),
            message=str(body.get("message") or ""))
    except WritingError as exc:
        return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)
    return web.json_response({"ok": True, **result})


# ── 写作 git 扩展指令 ──────────────────────────────────────────────────────

def _vcs_fail(exc: Exception) -> web.Response:
    return _writing_error(str(exc), 404 if "不存在" in str(exc) else 400)


async def _vcs_json(request: web.Request):
    try:
        return await request.json(), None
    except ValueError:
        return None, _writing_error("invalid json")


def _vcs_result(result: dict) -> web.Response:
    """合并类请求有冲突时用 409，前端据此打开冲突裁决界面。"""
    if result.get("conflicts"):
        return web.json_response({"ok": False, **result}, status=409)
    return web.json_response({"ok": True, **result})


async def handle_writing_vcs_refs(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    work_id = request.match_info["work_id"]
    try:
        svc = _writing_service(request)
        status = svc.vcs_status(work_id)
        stashes = svc.vcs_stashes(work_id)
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({
        "ok": True, "branch": status["branch"], "head": status["head"],
        "branches": status["branches"], "tags": status["tags"],
        "stashes": stashes})


async def handle_writing_vcs_reflog(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        limit = min(max(int(request.query.get("limit", "50")), 1), 500)
    except ValueError:
        return _writing_error("limit 必须是整数")
    try:
        rows = _writing_service(request).vcs_reflog(
            request.match_info["work_id"], limit=limit)
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, "entries": rows})


async def handle_writing_vcs_merge_base(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        result = _writing_service(request).vcs_merge_base(
            request.match_info["work_id"],
            request.query.get("a", "HEAD"), request.query.get("b", ""))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, **result})


async def handle_writing_vcs_switch(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        status = _writing_service(request).vcs_switch(
            request.match_info["work_id"],
            str(body.get("name") or ""),
            create=bool(body.get("create")),
            from_ref=str(body.get("from") or "HEAD"),
            checkpoint=bool(body.get("checkpoint", True)))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, **status})


async def handle_writing_vcs_rename_branch(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_rename_branch(
            request.match_info["work_id"], str(body.get("from") or ""),
            str(body.get("to") or ""))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, "branch": result})


async def handle_writing_vcs_delete_branch(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_delete_branch(
            request.match_info["work_id"], str(body.get("name") or ""),
            force=bool(body.get("force")))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, **result})


async def handle_writing_vcs_tags(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        tags = _writing_service(request).vcs_tags(
            request.match_info["work_id"])
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, "tags": tags})


async def handle_writing_vcs_create_tag(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        tag = _writing_service(request).vcs_create_tag(
            request.match_info["work_id"], str(body.get("name") or ""),
            ref=str(body.get("ref") or "HEAD"))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, "tag": tag})


async def handle_writing_vcs_delete_tag(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_delete_tag(
            request.match_info["work_id"], str(body.get("name") or ""))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, **result})


async def handle_writing_vcs_stash_list(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        rows = _writing_service(request).vcs_stashes(
            request.match_info["work_id"])
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, "stashes": rows})


async def handle_writing_vcs_stash_push(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_stash_push(
            request.match_info["work_id"], str(body.get("message") or ""))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, **result})


async def handle_writing_vcs_stash_pop(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_stash_pop(
            request.match_info["work_id"], str(body.get("name") or ""),
            resolutions=body.get("resolutions") or {})
    except WritingError as exc:
        return _vcs_fail(exc)
    return _vcs_result(result)


async def handle_writing_vcs_stash_drop(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_stash_drop(
            request.match_info["work_id"], str(body.get("name") or ""))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, **result})


async def handle_writing_vcs_merge(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_merge(
            request.match_info["work_id"], str(body.get("branch") or ""),
            message=str(body.get("message") or ""),
            resolutions=body.get("resolutions") or {})
    except WritingError as exc:
        return _vcs_fail(exc)
    return _vcs_result(result)


async def handle_writing_vcs_revert(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_revert(
            request.match_info["work_id"], str(body.get("commit_id") or ""),
            message=str(body.get("message") or ""),
            resolutions=body.get("resolutions") or {})
    except WritingError as exc:
        return _vcs_fail(exc)
    return _vcs_result(result)


async def handle_writing_vcs_cherry_pick(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_cherry_pick(
            request.match_info["work_id"], str(body.get("commit_id") or ""),
            message=str(body.get("message") or ""),
            resolutions=body.get("resolutions") or {})
    except WritingError as exc:
        return _vcs_fail(exc)
    return _vcs_result(result)


async def handle_writing_vcs_amend(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        commit = _writing_service(request).vcs_amend(
            request.match_info["work_id"], str(body.get("message") or ""))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, "commit": commit})


async def handle_writing_vcs_discard(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        result = _writing_service(request).vcs_discard_chapter(
            request.match_info["work_id"], str(body.get("chapter_id") or ""))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, **result})


async def handle_writing_feedback(request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    try:
        limit = min(max(int(request.query.get("limit", "100")), 1), 500)
    except ValueError:
        return _writing_error("limit 必须是整数")
    try:
        items = _writing_service(request).list_feedback(
            request.query.get("work_id", ""),
            status=request.query.get("status", ""), limit=limit)
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, "items": items})


async def handle_writing_feedback_status(
        request: web.Request) -> web.Response:
    from services.writing_service import WritingError
    body, err = await _vcs_json(request)
    if err:
        return err
    try:
        row = _writing_service(request).set_feedback_status(
            request.match_info["feedback_id"], str(body.get("status") or ""))
    except WritingError as exc:
        return _vcs_fail(exc)
    return web.json_response({"ok": True, "feedback": row})
