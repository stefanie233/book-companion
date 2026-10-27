"""AuthorGraph tools + author-deepen helpers."""
from __future__ import annotations

import json
from typing import Any
from permissions import PermissionMode
from tools.registry import ToolRegistry, ToolSpec, ToolExposure


#: 子代理抓过的页面（canonical_url → page）。宿主收结果时用它校验引文：
#: ``execute_agent`` 与 ``_deepen_store`` 在同一个进程，模块级缓存让
#: 「agent 说它引了哪一句」变成**可核**，而不是信任它的转述。
_FETCHED: dict[str, dict[str, Any]] = {}
_FETCHED_MAX = 24


def _remember_page(page: dict[str, Any]) -> str:
    from authorgraph.sources.registry import canonical_url
    keys = {page.get("canonical_url") or "",
            canonical_url(page.get("url") or "")} - {""}
    for k in keys:
        _FETCHED[k] = page
    while len(_FETCHED) > _FETCHED_MAX:
        _FETCHED.pop(next(iter(_FETCHED)))
    return page.get("canonical_url") or ""


def _deepen_store(raw: str, name: str, ident: dict, config: Any,
                  question: str) -> str:
    """把子代理的产出收下来。**出处校验从域名升级到逐字引文。**

    子代理现在多一个 ``author_web_fetch``：先抓页，再引用。落库时：

    - ``source=web`` 必须 ① 网址在它自己列出的 ``sources`` 里、
      ② 这一页真的被抓过、③ 引文能在抓到的正文里逐字找到。
      缺任何一条 → 降级 ``model`` 并报，**不写 web**。
    - 有引文的记录同时写 ``author_evidence``（target=record）——
      这是它与旧通道最大的区别：以后作者页能点回原句。
    """
    import json as _json

    from authorgraph.sources.registry import (canonical_url, roles_of,
                                              source_kind_of, tier_of)
    from authorgraph.sources.verify import quote_located
    from storage.authors_db import (AuthorsDB, SOURCE_TRUST,
                                    resolve_authors_db_path)

    try:
        out = _json.loads(raw)
    except Exception:                                    # noqa: BLE001
        return _json.dumps({"error": "子代理没返回可解析的结果"},
                           ensure_ascii=False)
    if out.get("status") == "failed":
        return _json.dumps({"error": out.get("error") or "子代理失败"},
                           ensure_ascii=False)
    from llm_json import loads_tolerant
    parsed = loads_tolerant(out.get("result_text") or "", expect=dict)
    if not parsed:
        return _json.dumps({
            "answer": (out.get("result_text") or "").strip()[:2000],
            "stored": 0,
            "note": "子代理没按 JSON 输出，所以什么都没落库（原话如上）。",
        }, ensure_ascii=False)

    allowed = {canonical_url(u) for u in (parsed.get("sources") or []) if u}
    defects: list[str] = []
    added = skipped = 0
    try:
        adb = AuthorsDB(resolve_authors_db_path(config))
        try:
            author = adb.find_author(name) if name else None
            if not author:
                return _json.dumps({
                    "answer": parsed.get("answer") or "", "stored": 0,
                    "note": "台账里没有这个人，查到的没地方挂 —— "
                            "先用采集把作者建起来。"}, ensure_ascii=False)
            aid = author["author_id"]
            for r in (parsed.get("records") or []):
                st = str(r.get("statement") or "").strip()
                if not st:
                    continue
                src = str(r.get("source") or "model")
                url = str(r.get("url") or "").strip()
                quote = str(r.get("evidence_quote") or "").strip()
                cu = canonical_url(url)
                evidence: dict[str, Any] | None = None
                if src == "web":
                    page = _FETCHED.get(cu) if cu else None
                    if not cu or cu not in allowed:
                        defects.append(f"fabricated_source：{st[:24]!r} 的"
                                       f"网址不在本次结果里 → model")
                        src, url = "model", ""
                    elif page is None:
                        defects.append(f"page_not_fetched：{st[:24]!r} 只给了"
                                       f"网址、没有抓正文 → model")
                        src, url = "model", ""
                    elif not quote or not quote_located(
                            quote, page.get("main_text") or ""):
                        defects.append(f"quote_not_found：{st[:24]!r} 的引文"
                                       f"不在页面正文里 → model")
                        src, url = "model", ""
                    else:
                        tier, kind = tier_of(url), source_kind_of(url)
                        sid = adb.upsert_source(
                            url=url, canonical_url=cu,
                            title=page.get("title") or "",
                            publisher=page.get("publisher") or "",
                            source_tier=tier, source_kind=kind,
                            language=page.get("language") or "",
                            content_hash=page.get("content_hash") or "",
                            content_excerpt=page.get("content_excerpt") or "")
                        adb.add_source_roles(sid, roles_of(kind, tier))
                        evidence = {"source_id": sid, "quote": quote,
                                    "locator": str(r.get("locator") or ""),
                                    "confidence": 0.6}
                year = r.get("year")
                year = int(year) if isinstance(year, int) or (
                    isinstance(year, str) and year.strip().isdigit()) else None
                created = adb.add_record(
                    aid, str(r.get("kind") or "life_event"), st, year=year,
                    year_end=None,
                    evidence_excerpt=quote if src == "web" else "",
                    stable_book_id=ident.get("stable_book_id"),
                    source=src, trust=SOURCE_TRUST.get(src))
                added += 1 if created else 0
                skipped += 0 if created else 1
                if evidence:
                    rid = adb.add_record_ex(
                        aid, str(r.get("kind") or "life_event"), st, year=year,
                        stable_book_id=ident.get("stable_book_id"),
                        source=src, trust=SOURCE_TRUST.get(src))
                    if rid is not None:
                        adb.add_evidence(aid, "record", str(rid), **evidence)
            if added:
                adb.mark_dirty(aid)
        finally:
            adb.close()
    except Exception as exc:                             # noqa: BLE001
        return _json.dumps({"answer": parsed.get("answer") or "",
                            "stored": 0,
                            "error": f"落库失败：{exc}"},
                           ensure_ascii=False)
    return _json.dumps({
        "answer": parsed.get("answer") or "",
        "stored": added, "skipped": skipped,
        "conflicts": parsed.get("conflicts") or [],
        "defects": defects,
        "note": "已记进作者台账，下次 author_lookup 就能查到。"
                if added else "没有新增条目（可能都已记过）。",
    }, ensure_ascii=False)


def _deepen_task(ident: dict, name: str, question: str,
                 known: dict) -> str:
    """给子代理的任务书。**把台账已有的东西一并给它** —— 不给自己就会
    重搜一遍已经知道的事，然后把同一件事用另一种说法再写一条。"""
    lines = [f"# 要查的作者\n{name or '（这本书的署名作者）'}", ""]
    if ident.get("title"):
        lines.append(f"（读者正在读《{ident['title']}》）")
    lines.append("")
    if known.get("found"):
        lines += ["# 台账里已经有的（**不要重搜、不要重记**）",
                  f"- 位置：{known.get('position') or '未知'}"]
        card = known.get("card") or ""
        lines += [card[:3000]] if card else []
    else:
        lines += ["# 台账里还没有这个人", "（所以更要靠 web_search，别凭记忆写）"]
    lines += ["", "# 读者的问题", question, "",
              "# 工作顺序（不要跳步）",
              "1. author_lookup 确认台账里已经有什么（已有的不要重搜）。",
              "2. web_search 找候选页面；只信这次返回的网址。",
              "3. 对准备引用的 1–3 个页面 **调用 author_web_fetch 抓正文**。",
              "4. 引用必须从抓到的 text 里**逐字复制**一段连续文字，"
              "填进 evidence_quote。",
              "5. 按 JSON 输出；没有抓过正文、或引文对不上，就把 source 写成 "
              "model（系统会在落库时逐字校验，对不上会降级）。"]
    return "\n".join(lines)


async def _author_deepen(*, registry: Any, api_client: Any, config: Any,
                         name: str, question: str,
                         reading_ctx: dict | None = None) -> str:
    """对话触发通道：fork 一个 ``author_scout`` 子代理把问题查清楚。

    **与 ``author_lookup`` 的区别不是快慢，是「要不要留下东西」**：
    lookup 只读台账；这一个查完**把结果写回台账** —— 一次投入，
    下次同一个问题就退化成毫秒级的 lookup。所以它是唯一配得上子代理的
    作者层工序（判据：需不需要去够它没拿到的东西）。

    三条纪律与工序 6 完全一致，因为它们落的是同一张表：
    ① 没网址的不许冒充 ``web``（降级成 ``model`` 并报）；
    ② 不覆盖已有条目，冲突上报；
    ③ 搜不到就说搜不到，不用训练数据充当搜索结果。
    """
    ctx = reading_ctx if reading_ctx is not None else {}
    db = ctx.get("db")
    book_id = ctx.get("book_id", "")
    if not db or not book_id:
        return json.dumps({"error": "当前没有打开的书籍"}, ensure_ascii=False)
    q = (question or "").strip()
    if not q:
        return json.dumps({"error": "没给出要查的问题"}, ensure_ascii=False)

    from authorgraph.card import load_author_card
    from authorgraph.scope import book_identity
    from storage.authors_db import AuthorsDB, resolve_authors_db_path
    try:
        ident = book_identity(db, book_id)
        want = (name or "").strip() or (ident.get("author") or "").strip()
        adb = AuthorsDB(resolve_authors_db_path(config))
        try:
            known = load_author_card(
                adb, name=want, stable_book_id=ident["stable_book_id"],
                reader_chapter=ctx.get("max_chapter"))
        finally:
            adb.close()
    except Exception as exc:                             # noqa: BLE001
        return json.dumps({"error": f"作者层读取失败：{exc}"},
                          ensure_ascii=False)

    task = _deepen_task(ident, want, q, known)
    from sub_agent import execute_agent
    raw = await execute_agent(
        description=f"深挖作者：{want or '（当前书的作者）'}",
        prompt=task, subagent_type="author_scout",
        api_client=api_client, tool_registry=registry, config=config,
        # **显式传**：过滤出来的是全新 registry，不传的话子代理里的
        # author_lookup 会以为「没打开书」，一路返回空。
        reading_ctx=dict(ctx))
    return _deepen_store(raw, want, ident, config, q)


def _register_author_tools(registry: ToolRegistry, api_client, config) -> None:
    # ── AuthorGraph: 作者层 ────────────────────────────────────────────
    #
    # 与 BookGraph 的分工：图谱装**世界里**发生过什么，作者层装**世界外面**
    # 那个人是谁。作者不在图谱里不是漏提，是按设计他不该在那儿
    # （``docs/graph-vision.md`` §四：序跋是元层，元层不进 nodes/edges）。

    def _tool_author_lookup(name: str = "") -> str:
        """查作者台账。**剧透门只从上下文取，模型给不了** —— 与
        ``graph_node`` 同一条：门的开关不能交到模型手上。"""
        ctx = registry.reading_ctx
        db = ctx.get("db")
        book_id = ctx.get("book_id", "")
        if not db or not book_id:
            return json.dumps({"error": "当前没有打开的书籍"},
                              ensure_ascii=False)
        try:
            from authorgraph.card import load_author_card
            from authorgraph.scope import book_identity
            from storage.authors_db import AuthorsDB, resolve_authors_db_path
            ident = book_identity(db, book_id)
            want = (name or "").strip() or (ident.get("author") or "").strip()
            if not want:
                return json.dumps(
                    {"error": "这本书没有署名作者，也没给出要查的名字"},
                    ensure_ascii=False)
            adb = AuthorsDB(resolve_authors_db_path())
            try:
                out = load_author_card(
                    adb, name=want,
                    stable_book_id=ident["stable_book_id"],
                    reader_chapter=ctx.get("max_chapter"))
            finally:
                adb.close()
            if not out.get("found"):
                out["queried"] = want
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:                         # noqa: BLE001
            return json.dumps({"error": f"作者层查询失败：{exc}"},
                              ensure_ascii=False)

    registry.register(ToolSpec(
        name="author_lookup",
        toolset="author_graph",
        exposure=ToolExposure.DEFERRED,
        category="author",
        description=(
            "查一个作者是谁：国籍、生卒、在这本书里占什么位置（作者本人/"
            "借角色说话/整理者/记录者/不在场），以及已知的生平条目和他"
            "自己的话（带原文出处）。\n"
            "**看两处标记**：身份状态为「待确认」时不要把别处的同名材料"
            "当成本人；网页来源标「未核验」的条目只有网址、没有逐字引文，"
            "不能当作已知事实引用。\n"
            "用于：读者问「作者是谁」「作者怎么看」「作者的经历」时；"
            "扮演作者前先了解这个人。\n"
            "不传 name 则查当前这本书的署名作者。\n"
            "**剧透门由系统按读者当前进度自动施加**：还没读到的生平不会"
            "返回，这不是缺数据。作者层没有台账时会如实说没有 —— 那时"
            "不要凭训练数据编造他的生平，明确告诉读者你不掌握。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "作者名，可留空（留空 = 当前书的署名作者）",
                },
            },
        },
        execute=_tool_author_lookup,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── 抓页面（把「网址」升级成「逐字引文」的那一步）────────────────────
    # 搜索工具返回的 content 是摘要（原生搜索甚至是模型二次总结），
    # 不能拿它当引文比对源。子代理必须先抓正文，再引用。
    async def _fetch_async(**kwargs: Any) -> str:
        url = str(kwargs.get("url") or "").strip()
        if not url:
            return json.dumps({"error": "没给网址"}, ensure_ascii=False)
        import aiohttp
        from authorgraph.sources.fetch import fetch_page
        try:
            async with aiohttp.ClientSession() as session:
                page = await fetch_page(session, url)
        except Exception as exc:                         # noqa: BLE001
            return json.dumps({"status": "error", "url": url,
                               "error": str(exc)}, ensure_ascii=False)
        if page.get("status") != "ok":
            return json.dumps({
                "status": page.get("status"), "url": url,
                "error": page.get("error") or "",
                "note": "抓不到正文就不能当证据 —— 换一页，"
                        "或者如实告诉读者你不知道。"}, ensure_ascii=False)
        _remember_page(page)
        return json.dumps({
            "status": "ok", "url": page.get("url"),
            "canonical_url": page.get("canonical_url"),
            "title": page.get("title"), "language": page.get("language"),
            "text": page.get("main_text"),
            "note": "引用时 evidence_quote 必须从 text 里逐字复制连续片段；"
                    "不能改写、不能拼接、不能用省略号。"},
            ensure_ascii=False)

    def _fetch_sync(**kwargs: Any) -> str:
        return json.dumps({"error": "author_web_fetch 必须走异步路径"},
                          ensure_ascii=False)

    registry.register(ToolSpec(
        name="author_web_fetch",
        toolset="author_graph",
        exposure=ToolExposure.DEFERRED,
        category="author",
        description=(
            "抓取一个网页的正文（最多 4000 字），用于引用前核对原文。\n"
            "**顺序：web_search 找到候选 → 抓页 → 只引用 text 里逐字存在的句子。**\n"
            "没有抓过正文的页面不能写 source=web —— 系统会在落库时校验引文，"
            "对不上就降级成 model（不可注入）。\n"
            "抓不到（robots/付费墙/超时）会如实返回状态，不要改用记忆编造。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "要抓取并引用的网页地址"},
            },
            "required": ["url"],
        },
        execute=_fetch_sync,
        execute_async=_fetch_async,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── 作者层深挖（对话触发通道）────────────────────────────────────────
    # **全层唯一用子代理的地方。** 另外五道工序的输入都完全确定（序跋 /
    # 已有的书），而这个要**自己去够** —— 够搜索结果、够别的书、够台账。
    # 判据正是「需不需要去够它没拿到的东西」（与 notes.py / verify.py /
    # incremental.py 三次「用普通调用」的先例互为反面）。
    def _deepen_sync(**kwargs: Any) -> str:
        return json.dumps(
            {"error": "author_deepen 必须走异步路径（它要 fork 子代理）"},
            ensure_ascii=False)

    async def _deepen_async(**kwargs: Any) -> str:
        return await _author_deepen(
            registry=registry, api_client=api_client, config=config,
            name=kwargs.get("name") or "",
            question=kwargs.get("question") or "")

    registry.register(ToolSpec(
        name="author_deepen",
        toolset="author_graph",
        exposure=ToolExposure.DEFERRED,
        category="author",
        description=(
            "深挖一位作者：fork 一个子代理去联网查，并把查到的东西"
            "**记进作者台账**（下次不用再查）。\n"
            "**先用 author_lookup**。只有台账确实答不了时才用它 —— "
            "它比 author_lookup 慢得多，而且会花掉一次联网调用。\n"
            "用于：读者追问作者的某段经历/某部作品，而 author_lookup 里没有。\n"
            "子代理会 **抓取页面正文** 再引用；落库的条目必须有逐字引文，"
            "系统会拿引文回页面里核对，对不上就降级成「模型推测」"
            "（**不会**出现在扮演里）。\n"
            "搜不到就如实说搜不到 —— 不要用你的训练数据充当查到的结果。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "作者名，可留空（留空 = 当前书的署名作者）"},
                "question": {"type": "string",
                             "description": "要查清楚的问题（用读者的原话即可）"},
            },
            "required": ["question"],
        },
        execute=_deepen_sync,
        execute_async=_deepen_async,
        permission_mode=PermissionMode.READ_ONLY,
    ))
