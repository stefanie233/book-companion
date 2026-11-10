"""写作对话工具 —— 让 agent 用自然语言完成建书、建章、写正文、结算、审稿。

工具是**薄壳**：判断与落库全在 ``services.writing_service`` 与
``writegraph`` 的纯函数里。这里只做三件事——解析参数、调用服务、
把结果压成模型能读的一行 JSON。

三个纪律：

1. 工具不 raise：失败返回 ``{"error": ...}``，让模型自己决定下一步。
2. ``ctx`` 是**请求级**的当前现场（work_id / chapter_index）。一次对话里
   先建作品再建章，不必让模型记住 id。
3. 写正文只在 ``writing_draft_chapter`` 里发生，且落库为草稿；
   聊天里不贴整章（``prompts/writing/agent.md`` 会再说一遍）。
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Awaitable, Callable, Mapping

from tools import PermissionMode, ToolExposure, ToolRegistry, ToolSpec
from tools.self import SELF_TOOLSET, install_self_tool
from writegraph import git_selftest

WRITING_TOOLSET = "writing"


def _ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _resolve_work(svc, ctx: dict[str, Any], work_id: str = "",
                  work_title: str = "") -> dict[str, Any]:
    """按 id / 标题 / 当前现场解析作品。解析成功就写回 ctx。"""
    work = None
    if work_id:
        work = svc.db.get_work(work_id)
    elif work_title:
        key = work_title.strip()
        work = next((w for w in svc.list_works()
                     if w["title"].strip() == key), None)
        if work is None:
            work = next((w for w in svc.list_works() if key in w["title"]),
                        None)
    elif ctx.get("work_id"):
        work = svc.db.get_work(str(ctx["work_id"]))
    if work is None:
        # 空项目（没绑定作品）走这里：先把「建书或选书」摆在前头，
        # 别让模型以为是 id 打错了去瞎猜。
        raise ValueError(
            "当前没有绑定作品；先 writing_create_work 建书，"
            "或请作者在左栏选一本（已有作品用 writing_status 看）")
    ctx["work_id"] = work["id"]
    return work


def _resolve_chapter(svc, ctx: dict[str, Any], chapter_index: int | None
                     ) -> dict[str, Any]:
    work_id = str(ctx.get("work_id") or "")
    if not work_id:
        raise ValueError("还没有选定作品")
    index = int(chapter_index if chapter_index is not None
                else ctx.get("chapter_index") or 0)
    if index <= 0:
        raise ValueError("缺少 chapter_index")
    ctx["chapter_index"] = index
    return svc.chapter(work_id, index)


def _work_brief(svc, work: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for c in svc.db.list_chapters(work["id"]):
        contract = svc.db.get_contract(c["id"])
        rows.append({
            "chapter_index": c["chapter_index"], "title": c.get("title", ""),
            "status": c.get("status"), "words": c.get("word_count", 0),
            "has_contract": bool(contract),
            # active = 在写/待写；closed = 已结算，合同只在提示词里留一行。
            "contract_status": str((contract or {}).get("status") or ""),
        })
    return {
        "work_id": work["id"],
        "title": work.get("title", ""),
        "genre": work.get("genre", ""),
        "chapters": rows,
    }


# ── 工具实现 ──────────────────────────────────────────────────────────────

def _tool_status(svc, ctx):
    def run(work_id: str = "", work_title: str = "") -> str:
        try:
            works = svc.list_works()
            brief_list = [{"work_id": w["id"], "title": w.get("title", ""),
                           "chapters": w.get("chapter_count", 0)}
                          for w in works]
            if not work_id and not work_title and not ctx.get("work_id"):
                return _ok(works=brief_list, current=None)
            work = _resolve_work(svc, ctx, work_id, work_title)
            return _ok(works=brief_list, current=_work_brief(svc, work))
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _tool_create_work(svc, ctx):
    def run(title: str, genre: str = "", premise: str = "",
            narrative_pov: str = "", target_chars: int = 0) -> str:
        try:
            existing = next((w for w in svc.list_works()
                             if w["title"].strip() == title.strip()), None)
            if existing is not None:
                ctx["work_id"] = existing["id"]
                return _ok(created=False, work_id=existing["id"],
                           title=existing["title"],
                           message="同名作品已存在，直接使用")
            work_id = svc.create_work(
                title, genre=genre, premise=premise,
                narrative_pov=narrative_pov, target_chars=target_chars)
            ctx["work_id"] = work_id
            ctx.pop("chapter_index", None)
            return _ok(created=True, work_id=work_id, title=title)
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _tool_create_chapter(svc, ctx):
    def run(work_id: str = "", work_title: str = "", title: str = "",
            purpose: str = "", must_hit_now: str = "",
            forbidden_cross: str = "", withhold: str = "",
            ending_state: str = "", target_chars: int = 0) -> str:
        try:
            work = _resolve_work(svc, ctx, work_id, work_title)
            chapter = svc.create_chapter(work["id"], title=title)
            contract_fields: dict[str, Any] = {
                "purpose": purpose or f"{title or chapter['chapter_index']} 推进"}
            if must_hit_now.strip():
                contract_fields["must_hit_now"] = [
                    x.strip() for x in must_hit_now.splitlines() if x.strip()]
            if forbidden_cross.strip():
                contract_fields["forbidden_cross"] = [
                    x.strip() for x in forbidden_cross.splitlines() if x.strip()]
            if withhold.strip():
                contract_fields["withhold"] = [
                    x.strip() for x in withhold.splitlines() if x.strip()]
            if ending_state.strip():
                contract_fields["ending_state"] = ending_state.strip()
            if target_chars:
                contract_fields["target_chars"] = int(target_chars)
            # 合同是硬边界：建章时顺手给一个最小合同，避免「没合同不能写」卡住对话。
            svc.set_contract(chapter["id"], **contract_fields)
            ctx["chapter_index"] = chapter["chapter_index"]
            return _ok(work_id=work["id"],
                       chapter_index=chapter["chapter_index"],
                       title=title, contract=svc.contract(chapter["id"]))
        except (ValueError, TypeError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _tool_set_contract(svc, ctx):
    def run(chapter_index: int | None = None, work_id: str = "",
            purpose: str = "", must_hit_now: str = "",
            forbidden_cross: str = "", withhold: str = "",
            ending_state: str = "",
            next_entry_state: str = "", target_chars: int = 0,
            arc_ref: str = "") -> str:
        try:
            _resolve_work(svc, ctx, work_id)
            chapter = _resolve_chapter(svc, ctx, chapter_index)
            fields: dict[str, Any] = {}
            for key, value in (("purpose", purpose),
                               ("ending_state", ending_state),
                               ("next_entry_state", next_entry_state),
                               ("arc_ref", arc_ref)):
                if value.strip():
                    fields[key] = value.strip()
            for key, value in (("must_hit_now", must_hit_now),
                               ("forbidden_cross", forbidden_cross),
                               ("withhold", withhold)):
                items = [x.strip() for x in value.splitlines() if x.strip()]
                if items:
                    fields[key] = items
            if target_chars:
                fields["target_chars"] = int(target_chars)
            if not fields:
                return _err("合同至少要有 purpose 或 must_hit_now")
            contract = svc.set_contract(chapter["id"], **fields)
            return _ok(chapter_index=chapter["chapter_index"],
                       contract=contract)
        except (ValueError, TypeError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _tool_read_chapter(svc, ctx):
    def run(chapter_index: int | None = None, work_id: str = "") -> str:
        try:
            _resolve_work(svc, ctx, work_id)
            chapter = _resolve_chapter(svc, ctx, chapter_index)
            content = str(chapter.get("content") or "")
            return _ok(chapter_index=chapter["chapter_index"],
                       title=chapter.get("title", ""),
                       status=chapter.get("status"),
                       word_count=chapter.get("word_count", 0),
                       contract=svc.contract(chapter["id"]),
                       content=content if content else "（本章还没有正文）")
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _effective_mode(ctx: dict[str, Any], chapter: Mapping[str, Any]) -> str:
    """单章覆盖 > 会话模式（决策 161）。"""
    mode = str(chapter.get("writing_mode") or ctx.get("writing_mode")
               or "standard").strip().lower()
    return mode if mode in ("standard", "detail") else "standard"


def _tool_set_mode(svc, ctx):
    def run(mode: str = "", chapter_index: int = 0, reason: str = "",
            authorized: bool = False, remember: bool = False) -> str:
        try:
            wanted = str(mode or "").strip().lower()
            if wanted not in ("standard", "detail", "inherit"):
                return _err("mode 只能是 standard / detail / inherit")
            if remember and not authorized:
                return _err("记住授权也要作者同意：先问作者，再传 authorized=true")
            allowed = bool(ctx.get("allow_auto_mode"))
            if not (authorized or allowed):
                return _ok(
                    needs_authorization=True,
                    error="切换写作模式需要作者同意。先问作者；"
                          "作者本轮明确同意后再传 authorized=true。"
                          "作者说「以后你决定」时可传 remember=true。",
                    current=ctx.get("writing_mode") or "standard")
            work = _resolve_work(svc, ctx)
            if int(chapter_index or 0) > 0:
                result = svc.set_chapter_writing_mode(
                    work["id"], int(chapter_index), wanted)
                return _ok(scope="chapter", chapter_index=chapter_index,
                           mode=result["mode"], reason=reason,
                           note="只作用于这一章；下一章回到会话模式。")
            if wanted == "inherit":
                return _err("会话级模式只能用 standard / detail；"
                            "inherit 只用于单章覆盖")
            ctx["writing_mode"] = wanted
            if remember:
                ctx["allow_auto_mode"] = True
            setter = ctx.get("set_writing_mode")
            if callable(setter):
                setter(wanted, bool(remember))
            return _ok(scope="session", mode=wanted, reason=reason,
                       remembered=bool(remember),
                       note="模式已切换。本轮系统提示不会重发；要在这轮"
                            "直接动笔，细致模式下记得带 research_brief。")
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _tool_draft_chapter(svc, ctx, progress=None
                        ) -> Callable[..., Awaitable[str]]:
    async def run(chapter_index: int | None = None, work_id: str = "",
                  extra_instruction: str = "",
                  refs: list | None = None,
                  research_brief: dict | None = None,
                  plan_first: bool = False) -> str:
        try:
            work = _resolve_work(svc, ctx, work_id)
            chapter = _resolve_chapter(svc, ctx, chapter_index)
            if not svc.contract(chapter["id"]):
                return _err("本章还没有合同。先调用 writing_set_contract")
            mode = _effective_mode(ctx, chapter)
            content = ""
            tokens = 0
            reference: dict = {}
            manifest: dict = {}
            decision: dict = {}
            async for event in svc.stream_chapter(
                    work["id"], chapter["chapter_index"],
                    extra_instruction=extra_instruction, refs=refs or [],
                    mode=mode, research_brief=research_brief,
                    plan_first=bool(plan_first)):
                # 进度只报给调用方（SSE 状态行）；正文本身不进对话。
                if progress is not None:
                    try:
                        await progress(event)
                    except Exception:                # noqa: BLE001
                        pass
                if event["event"] == "done":
                    content = event["data"].get("content", "")
                elif event["event"] == "stage" and event["data"].get("stage") == "assembled":
                    tokens = event["data"].get("tokens", 0)
                    reference = event["data"].get("reference") or {}
                    manifest = event["data"].get("manifest") or {}
                    decision = event["data"].get("decision") or {}
                elif event["event"] == "error":
                    return _err(event["data"].get("message", "生成失败"))
            if not content:
                return _err("模型没有返回正文")
            blocks = manifest.get("blocks") or []
            dropped = [{"id": b.get("id"), "reason": b.get("reason") or ""}
                       for b in blocks if b.get("dropped")]
            trimmed = [b.get("id") for b in blocks
                       if str(b.get("reason") or "").startswith("超出块预算")]
            return _ok(
                chapter_index=chapter["chapter_index"], mode=mode,
                word_count=len(content), context_tokens=tokens,
                reference={"count": reference.get("count", 0),
                           "chars": reference.get("chars", 0)},
                context={"dropped": dropped, "truncated": trimmed,
                         "coverage": (decision or {}).get("coverage"),
                         "budget": (decision or {}).get("budget")},
                head=content[:120], tail=content[-120:],
                note="草稿已落库。正文全文不要复述给用户，只说写了什么。")
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _tool_replace_text(svc, ctx):
    def run(anchor_quote: str, replacement: str,
            chapter_index: int | None = None, work_id: str = "") -> str:
        try:
            work = _resolve_work(svc, ctx, work_id)
            chapter = _resolve_chapter(svc, ctx, chapter_index)
            saved = svc.replace_text(work["id"], chapter["chapter_index"],
                                     anchor_quote, replacement)
            return _ok(chapter_index=chapter["chapter_index"], **saved)
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


_OUTCOME_VERDICTS = ("hit", "partial", "miss")


def _parse_outcomes(text: str) -> list[dict[str, str]]:
    """把「每行一条」的兑现结果解析成行。

    格式：``hit|条目原文|正文里的逐字证据``；没做到写 ``miss|条目原文|原因``。
    竖线只在第三段之后允许出现，证据里本来就可能带标点。
    """
    rows: list[dict[str, str]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 2:
            raise ValueError("兑现结果每行写成「hit|条目|逐字证据」，"
                             "没做到用 miss|条目|原因")
        verdict = parts[0].strip().lower()
        if verdict not in _OUTCOME_VERDICTS:
            raise ValueError(f"兑现结果只有 hit / partial / miss，收到：{verdict}")
        item = parts[1].strip()
        if not item:
            raise ValueError("兑现结果缺条目：竖线中间要写合同里的原文")
        rows.append({"item": item, "verdict": verdict,
                     "evidence": (parts[2] if len(parts) > 2 else "").strip()})
    return rows


def _tool_settle(svc, ctx, progress=None) -> Callable[..., Awaitable[str]]:
    async def run(chapter_index: int | None = None, work_id: str = "",
                  outcomes: str = "", note: str = "") -> str:
        try:
            work = _resolve_work(svc, ctx, work_id)
            chapter = _resolve_chapter(svc, ctx, chapter_index)
            # 先解析再跑长调用：格式错不该先等 231 秒。
            declared = _parse_outcomes(outcomes)
            # 结算是一次长调用（实测 231 秒），界面得知道它在干活。
            if progress is not None:
                try:
                    await progress({"event": "stage",
                                    "tool": "writing_settle_chapter",
                                    "data": {"stage": "settling"}})
                except Exception:                    # noqa: BLE001
                    pass
            result = await svc.settle(work["id"], chapter["chapter_index"],
                                      outcomes=declared, note=note)
            if not result.get("ok"):
                # 整份拒绝是设计：把错误原样交回模型，让它重试或如实汇报。
                return _err("结算被拒：" + "；".join(result.get("errors") or []))
            status = svc.memory_status(work["id"])
            dirty = chapter["chapter_index"] in (status.get("dirty_chapters") or [])
            return _ok(
                chapter_index=chapter["chapter_index"],
                written=result.get("written"),
                summary=result.get("delta", {}).get("chapter_summary", ""),
                contract=result.get("contract"),
                git=result.get("git"),
                memory={
                    "needed": dirty,
                    "chapter_index": chapter["chapter_index"],
                    "pending": status.get("pending", 0),
                    "hint": ("收尾前调 writing_memory(action=\"auto\", "
                             f"chapter_index={chapter['chapter_index']}, "
                             "transcript=作者这轮原话)；pending≥3 或到章节/"
                             "弧段边界时再 consolidate。") if dirty else
                            "本章记忆已抽取，不用再调 auto。"})
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _tool_review(svc, ctx) -> Callable[..., Awaitable[str]]:
    async def run(chapter_index: int | None = None, work_id: str = "") -> str:
        try:
            work = _resolve_work(svc, ctx, work_id)
            chapter = _resolve_chapter(svc, ctx, chapter_index)
            result = await svc.review_chapter(work["id"],
                                              chapter["chapter_index"])
            findings = [{"severity": f["severity"], "category": f["category"],
                         "issue": f["issue"], "evidence": f["evidence"][:80]}
                        for f in result.get("findings", [])[:8]]
            goal_review = result.get("goal_review") or {}
            goals = [{"id": item.get("id"), "text": item.get("text"),
                      "status": item.get("status"),
                      "description": item.get("description", "")[:80],
                      "evidence": [e.get("quote", "")[:80]
                                   for e in item.get("evidence") or []][:2]}
                     for item in goal_review.get("items") or []
                     if item.get("status") != "completed"]
            return _ok(chapter_index=chapter["chapter_index"],
                       delivery_status=result.get("delivery_status"),
                       quality_score=result.get("quality_score"),
                       coverage=result.get("coverage"),
                       findings=findings,
                       rejected=len(result.get("rejected") or []),
                       goal_summary=result.get("goal_summary"),
                       goals=goals)
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


# ── 写作 git ──────────────────────────────────────────────────────────────

# ── agent 侧的瘦身视图 ────────────────────────────────────────────────
# 工具结果是上下文的大头：一次 status 带 64 位树/账本哈希，一次 log 每条
# 提交都带两串哈希。模型不需要这些，只保留做判断要用的字段。
# HTTP/前端仍走完整结构，这里只影响 agent。

_SLIM_COMMIT_FIELDS = ("id", "short", "parent_id", "parent2_id", "branch",
                       "kind", "author", "message", "chapter_index",
                       "time_created", "refs", "is_head")


def _tool_references(svc, ctx):
    def _library():
        from storage.db import BookDB
        return BookDB(svc.config.books_db_path)

    def run(action: str = "list", titles: str = "", work_id: str = "",
            mode: str = "card") -> str:
        try:
            library = _library()
            if action == "list":
                rows = library.list_books(limit=60)
                books = []
                for row in rows:
                    log = library.get_import_log_by_book(row.id)
                    books.append({"title": row.title, "book_id": row.id,
                                  "stable_book_id": getattr(log, "stable_book_id", "")})
                return _ok(books=books)
            work = _resolve_work(svc, ctx, work_id)
            wanted = [x.strip() for x in titles.replace("，", ",").split(",")
                      if x.strip()]
            rows = list(library.list_books(limit=200))
            picked = []
            for name in wanted:
                row = next((b for b in rows if b.title.strip() == name), None)
                row = row or next((b for b in rows if name in b.title), None)
                if row is None:
                    return _err(f"书库里找不到《{name}》")
                log = library.get_import_log_by_book(row.id)
                sid = str(getattr(log, "stable_book_id", "") or "")
                if not sid:
                    return _err(f"《{row.title}》没有稳定 id，无法做参考")
                picked.append({"stable_book_id": sid, "book_id": row.id,
                               "label": row.title})
            return _ok(**svc.set_reference_books(work["id"], picked, mode=mode))
        except Exception as exc:                     # noqa: BLE001
            return _err(str(exc))
    return run


def _tool_research(svc, ctx):
    async def run(query: str = "", source: str = "library", limit: int = 6,
                  work_id: str = "", url: str = "", book: str = "",
                  enable_web: bool = False, save: bool = False,
                  remember_web: bool = False) -> str:
        try:
            # 空项目也能查书库：没有绑定作品时不代入任何一本的设定。
            wid = work_id or str(ctx.get("work_id") or "")
            used = int(ctx.get("_web_research_calls") or 0)
            explicit = bool(url) or source == "web"
            if explicit and used >= 2:
                return _err("本轮的联网检索已用满 2 次；先用手上的结果，"
                            "或把要查的资料放进 素材/。")
            if source == "auto" and used >= 2:
                # 预算用满就不再允许 auto 落网，避免第三次检索。
                source = "library"
            result = await svc.research_material(
                wid, query, source=source, limit=limit, url=url,
                enable_web=enable_web, save=save,
                remember_web=remember_web, book=book)
            if result.get("source") == "web":
                ctx["_web_research_calls"] = used + 1
            # 语义分支空了就带上主题键：模型据此用 self_explain 解释是
            # 没绑 embedding、没建索引，还是换了模型。
            if (source == "semantic" and result.get("recall") == "semantic"
                    and not result.get("cards")):
                result = {**result, "self_topic": "semantic-recall"}
            return _ok(**result)
        except (ValueError, RuntimeError) as exc:
            payload = {"ok": False, "error": str(exc)}
            if source == "semantic":
                payload["self_topic"] = "semantic-recall"
            return json.dumps(payload, ensure_ascii=False)
    return run


def _file_marks(svc, work_id: str) -> dict[str, tuple[str, str]]:
    """``{路径: (状态字母, 说明)}``：文件区与版本控制共用的状态表。"""
    try:
        changes = svc.vcs_status(work_id).get("file_changes") or {}
    except Exception:                                # noqa: BLE001
        return {}
    marks: dict[str, tuple[str, str]] = {}
    for rows in changes.values():
        for row in rows:
            path = str(row.get("path") or "")
            if path:
                marks[path] = (str(row.get("status") or ""),
                               str(row.get("status_label") or ""))
    return marks


def _tool_docs(svc, ctx):
    """作品工作区：作品记忆 / 设定 / 计划稿 / 素材。"""

    def run(action: str = "list", path: str = "", content: str = "",
            work_id: str = "", query: str = "", scope: str = "",
            limit: int = 10) -> str:
        try:
            current = str(ctx.get("work_id") or "")
            work = _resolve_work(svc, ctx, work_id)
            cross = bool(current) and work["id"] != current
            if cross:
                # 跨库只读不改变当前现场，也不许写。
                ctx["work_id"] = current
                if action not in ("list", "read", "search"):
                    return _err("别的作品只能只读；写回自己的工作区")
            if action == "list":
                data = svc.docs_list(work["id"])
                files, dirs = data["files"], data["dirs"]
                if cross:
                    files = [f for f in files
                             if str(f.get("path") or "").startswith("素材/")]
                    dirs = [d for d in dirs if d.get("path") == "素材"]
                # 文件区与版本控制共用同一份状态（M/U/D）：列目录时顺手
                # 带上，模型不用再查一次 status 才知道哪份没提交。
                marks = _file_marks(svc, work["id"])
                for row in files:
                    mark = marks.get(str(row.get("path") or ""))
                    if mark:
                        row["status"], row["status_label"] = mark
                return _ok(work_id=work["id"], root=data["root"],
                           files=files, dirs=dirs)
            if action == "search":
                data = svc.docs_search(work["id"], query,
                                       scope="素材" if cross else scope,
                                       limit=limit)
                return _ok(**data)
            if not str(path or "").strip():
                return _err("缺少 path")
            if action == "read":
                if cross and not str(path).replace("\\", "/").startswith("素材/"):
                    return _err("只能读别的作品的 素材/")
                row = svc.docs_read(work["id"], path)
                # 设定文档可能很长，整份倒进上下文会挤掉正典；先给前 8000 字。
                content = str(row.get("content") or "")
                if len(content) > 8000:
                    row["content"] = (content[:8000]
                                      + "\n…（已截断，文件还有 "
                                      + str(len(content) - 8000) + " 字）")
                return _ok(**row)
            if action in ("write", "append"):
                rel = str(path or "").replace("\\", "/").strip()
                if rel == "作品记忆.md":
                    return _err("作品记忆只能由 writing_memory 维护：作者点名时"
                                "用 action=note，结算后用 action=auto")
                row = svc.docs_write(
                    work["id"], path, content,
                    append=action == "append",
                    dedupe=action == "append",
                    check_duplicate=action == "write",
                    log_memory=True)
                return _ok(**{**row, "action": action})
            if action == "delete":
                if str(path or "").replace("\\", "/").strip() == "作品记忆.md":
                    return _err("作品记忆不能删；要清空就等 consolidate 重写")
                return _ok(**svc.docs_delete(work["id"], path))
            return _err(f"未知 action：{action}")
        except Exception as exc:                     # noqa: BLE001
            return _err(str(exc))

    return run


def _tool_materials(svc, ctx):
    """全局素材库：收录 / 检索 / 提升 / 记消费（决策 156）。"""

    def run(action: str = "list", path: str = "", query: str = "",
            material_id: str = "", excerpt: str = "", title: str = "",
            category: str = "待归类", tags: str = "", description: str = "",
            source: str = "author", book_title: str = "", book_id: str = "",
            chapter_index: int | None = None,
            chunk_index: int | None = None, chunk_id: str = "",
            para_start: int | None = None, para_end: int | None = None,
            url: str = "", work_id: str = "", kind: str = "merge",
            target_path: str = "", note: str = "", instruction: str = "",
            name: str = "", target_dir: str = "", target: str = "",
            entry_kind: str = "file", limit: int = 10,
            authorized: bool = False) -> str:
        try:
            current = str(ctx.get("work_id") or "")
            wid = str(work_id or current or "")
            if action == "list":
                return _ok(**svc.materials_list())
            if action == "pending":
                return _ok(**svc.materials_pending(limit=limit))
            if action == "cook":
                return _ok(**svc.materials_cook(
                    path, material_id=material_id, title=title,
                    category=category, tags=tags, description=description))
            if action == "search":
                if not str(query or "").strip():
                    return _err("缺少 query")
                return _ok(**svc.materials_search(query, limit=limit))
            if action == "read":
                return _ok(**svc.materials_read(
                    path, material_id=material_id))
            if action == "rename":
                if not str(path or "").strip():
                    return _err("缺少 path")
                return _ok(**svc.materials_rename(
                    path, name, kind=entry_kind or "file"))
            if action == "move":
                if not str(path or "").strip():
                    return _err("缺少 path")
                if not str(target_dir or "").strip():
                    return _err("缺少 target_dir")
                return _ok(**svc.materials_move(
                    path, target_dir, kind=entry_kind or "file"))
            if action == "recycled":
                return _ok(**svc.materials_recycled())
            if action == "restore":
                if not str(path or "").strip():
                    return _err("缺少 path")
                return _ok(**svc.materials_restore(path, target=target))
            if action == "audit":
                return _ok(**svc.materials_audit())
            if action == "merge":
                if not str(path or "").strip():
                    return _err("merge 缺少 path（短卡）")
                if not str(target or "").strip():
                    return _err("merge 缺少 target（长卡）")
                return _ok(**svc.materials_merge(
                    path, target, authorized=authorized))
            if action == "read_chunk":
                return _ok(**svc.materials_read_chunk(
                    chunk_id=chunk_id, book_id=book_id,
                    chapter_index=chapter_index, chunk_index=chunk_index))
            if action == "intake":
                row = svc.materials_intake(
                    excerpt=excerpt, title=title, category=category,
                    tags=tags, description=description, source=source,
                    book_title=book_title, book_id=book_id,
                    chapter_index=chapter_index, chunk_index=chunk_index,
                    chunk_id=chunk_id, para_start=para_start,
                    para_end=para_end, url=url, work_id=wid,
                    instruction=instruction)
                return _ok(**row)
            if action == "promote":
                if not wid:
                    return _err("先选一本书，再把素材提升进作品")
                return _ok(**svc.materials_promote(
                    material_id, wid, target_path=target_path, note=note,
                    chapter_index=chapter_index))
            if action == "use":
                if not wid:
                    return _err("消费记录要绑作品；先选一本书")
                return _ok(**svc.materials_record_use(
                    material_id, wid, kind=kind,
                    chapter_index=chapter_index, target_path=target_path,
                    note=note))
            if action == "uses":
                return _ok(**svc.materials_uses(
                    material_id=material_id, work_id=wid, limit=limit))
            if action == "delete":
                if not str(path or "").strip():
                    return _err("缺少 path")
                return _ok(**svc.materials_delete(path))
            return _err(f"未知 action：{action}")
        except Exception as exc:                     # noqa: BLE001
            return _err(str(exc))

    return run


def _tool_memory(svc, ctx):
    """两段式记忆维护：抽取 → 合并。只写 作品记忆.md 与 设定/。"""

    async def run(action: str = "status",
                  chapter_index: int | None = None,
                  transcript: str = "", work_id: str = "",
                  target: str = "setting", path: str = "",
                  section: str = "", op: str = "append",
                  text: str = "", old: str = "", evidence: str = "",
                  source: str = "author", delta: dict | None = None,
                  outcomes: list | None = None) -> str:
        def payload(data: dict) -> str:
            if data.get("ok") is False:
                return _err(str(data.get("error")
                                or data.get("status") or "记忆维护失败"))
            return _ok(**{k: v for k, v in data.items() if k != "ok"})

        try:
            work = _resolve_work(svc, ctx, work_id)
            if action == "status":
                return payload(svc.memory_status(work["id"]))
            if action == "note":
                return payload(svc.memory_note(
                    work["id"], target=target, path=path, section=section,
                    op=op, text=text, old=old, evidence=evidence,
                    source=source))
            if action == "audit":
                return payload(svc.memory_audit(work["id"]))
            if action == "extract":
                chapter = _resolve_chapter(svc, ctx, chapter_index)
                return payload(await svc.memory_extract(
                    work["id"], chapter["chapter_index"],
                    transcript=transcript, delta=delta, outcomes=outcomes))
            if action == "consolidate":
                return payload(await svc.memory_consolidate(work["id"]))
            if action == "auto":
                chapter = _resolve_chapter(svc, ctx, chapter_index)
                return payload(await svc.maintain_memory(
                    work["id"], chapter["chapter_index"],
                    transcript=transcript, delta=delta, outcomes=outcomes))
            return _err(f"未知 action：{action}")
        except Exception as exc:                     # noqa: BLE001
            return _err(str(exc))

    return run


def _slim_commit(commit: dict | None) -> dict | None:
    if not commit:
        return commit
    return {k: commit.get(k) for k in _SLIM_COMMIT_FIELDS
            if commit.get(k) not in (None, "", [])}


def _slim_entry(entry: dict | None) -> dict | None:
    if not entry:
        return entry
    return {k: entry.get(k) for k in ("chapter_id", "chapter_index", "title",
                                     "status", "word_count")
            if entry.get(k) is not None}


def _slim_changes(changes: dict | None) -> dict:
    out: dict[str, list] = {}
    for key in ("added", "removed", "changed"):
        rows = []
        for entry in (changes or {}).get(key) or []:
            item = _slim_entry(entry) or {}
            if key == "changed":
                item["fields"] = entry.get("fields") or []
            rows.append(item)
        out[key] = rows
    return out


def _slim_tags(tags) -> list[dict]:
    return [{k: t.get(k) for k in ("name", "commit_id", "short", "message")
             if t.get(k) not in (None, "", [])} for t in tags or []]


def _slim_stashes(rows) -> list[dict]:
    return [{k: r.get(k) for k in ("name", "label", "short", "message")
             if r.get(k) not in (None, "", [])} for r in rows or []]


def _status_head(status: dict | None) -> dict | None:
    """状态里的 head.branch 必须是「当前分支」，不是提交创建时的分支。

    ``_commit_brief`` 带的是提交行上的 branch；branch_switch / merge 之后
    它还是旧分支，和同一份 status 的顶层 branch 自相矛盾（自测
    fb-de23fdd73c7b41ff）。log 里的 commit.branch 仍然保留原义。
    """
    head = _slim_commit((status or {}).get("head"))
    if head and status.get("branch"):
        head["branch"] = status["branch"]
    return head


def _slim_status(status: dict | None) -> dict:
    if not status:
        return {}
    out = {k: status.get(k) for k in
           ("branch", "stash_count", "ledger_dirty", "clean",
            "chapter_counts", "feedback")}
    out["head"] = _status_head(status)
    out["changes"] = _slim_changes(status.get("changes"))
    out["branches"] = [
        {k: b.get(k) for k in ("name", "commit_id", "is_head", "ahead",
                               "behind", "base_short", "message")
         if b.get(k) not in (None, "", [])}
        for b in status.get("branches") or []
    ]
    out["tags"] = _slim_tags(status.get("tags"))
    if status.get("files_dirty"):
        # 作品记忆 / 设定 / 计划稿 / 素材 的未提交改动：告诉模型具体哪几份，
        # 它才能决定要不要把这轮写进历史。
        out["file_changes"] = status.get("file_changes")
        out["file_counts"] = status.get("file_counts")
    return out


def _brief_status(status: dict | None) -> dict:
    """变更操作的随附状态：只留「现在在哪、干不干净、改了几章」。"""
    if not status:
        return {}
    out = {k: status.get(k) for k in
           ("branch", "stash_count", "ledger_dirty", "clean",
            "chapter_counts", "files_dirty", "file_counts")
           if status.get(k) is not None}
    head = _status_head(status)
    if head:
        out["head"] = head
    return out


def _slim_reflog(entries) -> list[dict]:
    """reflog 只留判断要用的：短号、原因、两端说明。"""
    return [{k: e.get(k) for k in
             ("id", "branch", "reason", "commit_short", "commit_message",
              "from_short", "from_message")
             if e.get(k) not in (None, "", [])}
            for e in entries or []]


def _slim_result(result: dict) -> dict:
    out = dict(result)
    if isinstance(out.get("status"), dict):
        # 变更操作只带精简现场；查 branches/tags 仍走 action=status。
        out["status"] = _brief_status(out["status"])
    if isinstance(out.get("commit"), dict):
        out["commit"] = _slim_commit(out["commit"])
    if isinstance(out.get("restored"), dict):
        out["restored"] = _slim_commit(out["restored"])
    return out


def _attach_chapter_previews(out: dict, svc, work_id: str) -> dict:
    """把「这次改动落到哪几章」附在结果里，省掉紧接着的一次读章。

    只给首尾摘录，不给全文；要看全文仍然用 writing_read_chapter。
    """
    if not out or not work_id:
        return out
    for key in ("commit", "restored"):
        commit = out.get(key)
        if isinstance(commit, dict) and commit.get("id"):
            try:
                rows = svc.commit_chapter_previews(work_id, commit["id"])
            except Exception:
                rows = []
            if rows:
                out["chapters"] = rows
            return out
    return out


def _attach_worktree_previews(out: dict, svc, work_id: str) -> dict:
    """预览工作区当前的脏章（stash_pop 之后要看的是工作区，不是 stash 提交）。"""
    if not out or not work_id:
        return out
    try:
        changes = (svc.vcs_status(work_id).get("changes") or {})
        changed = changes.get("changed") or []
    except Exception:
        return out
    rows = []
    for entry in changed:
        cid = str(entry.get("chapter_id") or "")
        if not cid:
            continue
        try:
            row = svc.chapter_preview(work_id, cid)
        except Exception:
            row = None
        if row:
            rows.append(row)
        if len(rows) >= 3:
            break
    if rows:
        out["chapters"] = rows
    return out


def _git_outcome(result: dict[str, Any], svc=None,
                 work_id: str = "", preview: str = "commit") -> str:
    """冲突不是失败：告诉模型「需要裁决」，别让它当报错重试。"""
    if result.get("conflicts"):
        out = {"applied": False, "needs_resolution": True,
               "conflicts": result["conflicts"],
               "hint": "把冲突章和两边差异讲给作者；按作者的选择带 "
                       "resolutions 重新调用同一个 action。不要自己猜。"}
        # 冲突时工作区通常没被改脏；把现场一并给出，别再单独查 status。
        if isinstance(result.get("status"), dict):
            out["status"] = _brief_status(result["status"])
        return _ok(**out)
    out = _slim_result(result)
    if svc is not None and preview == "worktree":
        out = _attach_worktree_previews(out, svc, work_id)
    elif svc is not None:
        out = _attach_chapter_previews(out, svc, work_id)
        # 快进合并没有新提交：结果 HEAD 就是分支 tip，拿它做改动预览。
        if (not out.get("chapters") and result.get("fast_forward")):
            head = (result.get("status") or {}).get("head") or {}
            if head.get("id"):
                try:
                    rows = svc.commit_chapter_previews(work_id, head["id"])
                except Exception:
                    rows = []
                if rows:
                    out["chapters"] = rows
    return _ok(**out)


def _tool_git_read(svc, ctx):
    def run(action: str, work_id: str = "", work_title: str = "",
            branch: str = "", all_branches: bool = False,
            chapter_index: int | None = None, from_ref: str = "",
            to_ref: str = "", limit: int = 50,
            phase: str = "", step: str = "", file_path: str = "") -> str:
        try:
            action = (action or "status").strip()
            if action == "selftest_plan":
                return _ok(plan=git_selftest.plan(phase=phase, step=step))
            if action == "selftest_seed":
                seed = svc.seed_selftest_work()
                ctx["work_id"] = seed["work_id"]
                ctx.pop("chapter_index", None)
                return _ok(**seed)
            if action == "selftest_cleanup":
                wid = work_id or str(ctx.get("work_id") or "")
                result = svc.delete_selftest_work(wid)
                if str(ctx.get("work_id") or "") == wid:
                    ctx.pop("work_id", None)
                return _ok(**result)
            work = _resolve_work(svc, ctx, work_id, work_title)
            wid = str(work["id"])
            chapter_id = ""
            if chapter_index is not None:
                chapter_id = svc.chapter(wid, int(chapter_index))["id"]
            if action == "status":
                status = svc.vcs_status(wid)
                status["feedback"] = svc.feedback_summary(wid)
                return _ok(**_slim_status(status))
            if action == "log":
                commits = svc.vcs_log(
                    wid, branch=branch, all_branches=bool(all_branches),
                    chapter_id=chapter_id, limit=max(1, min(int(limit), 200)))
                return _ok(
                    branch=(svc.vcs_status(wid).get("branch") or "main"),
                    commits=[_slim_commit(c) for c in commits])
            if action == "diff":
                diff = svc.vcs_diff(
                    wid, from_ref=from_ref or "HEAD",
                    to_ref=to_ref or "WORK", chapter_id=chapter_id,
                    file_path=file_path,
                    text=bool(chapter_id or file_path))
                diff["changes"] = _slim_changes(diff.get("changes"))
                return _ok(**diff)
            if action == "reflog":
                return _ok(entries=_slim_reflog(svc.vcs_reflog(
                    wid, limit=max(1, min(int(limit), 100)))))
            if action == "branches":
                return _ok(branches=_slim_status(
                    svc.vcs_status(wid)).get("branches") or [])
            if action == "tags":
                return _ok(tags=_slim_tags(svc.vcs_tags(wid)))
            if action == "stash_list":
                return _ok(stashes=_slim_stashes(svc.vcs_stashes(wid)))
            return _err(f"未知 action：{action}")
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _tool_git_change(svc, ctx):
    def run(action: str, work_id: str = "", work_title: str = "",
            message: str = "", name: str = "", new_name: str = "",
            from_ref: str = "", commit_id: str = "", branch: str = "",
            chapter_index: int | None = None, force: bool = False,
            switch: bool = False, checkpoint: bool = True,
            resolutions: dict | None = None,
            chapters: list[int] | None = None, habit: str = "") -> str:
        try:
            work = _resolve_work(svc, ctx, work_id, work_title)
            wid = str(work["id"])
            action = (action or "").strip()
            if action == "set_habit":
                return _ok(**svc.set_git_habit(wid, habit))
            if action == "commit":
                commit = svc.vcs_commit(wid, message,
                                        chapter_indexes=chapters or None)
                out = {"commit": _slim_commit(commit),
                       "status": _brief_status(svc.vcs_status(wid))}
                # 只提交了一部分时，status.counts 同时告诉你还剩什么。
                out = _attach_chapter_previews(out, svc, wid)
                return _ok(**out)
            if action == "amend":
                commit = svc.vcs_amend(wid, message)
                out = {"commit": _slim_commit(commit),
                       "status": _brief_status(svc.vcs_status(wid))}
                return _ok(**_attach_chapter_previews(out, svc, wid))
            if action == "branch_create":
                created = svc.vcs_create_branch(
                    wid, name, from_ref=from_ref or "HEAD")
                out = {"branch": created, "switched": bool(switch)}
                if switch:
                    out["status"] = _brief_status(
                        svc.vcs_checkout(wid, name, checkpoint=checkpoint))
                return _ok(**out)
            if action == "branch_switch":
                return _ok(**_slim_status(svc.vcs_checkout(
                    wid, name, checkpoint=checkpoint)))
            if action == "branch_rename":
                return _ok(branch=svc.vcs_rename_branch(wid, name, new_name))
            if action == "branch_delete":
                return _ok(**svc.vcs_delete_branch(wid, name, force=force))
            if action == "merge":
                return _git_outcome(svc.vcs_merge(
                    wid, branch, message=message,
                    resolutions=resolutions or {}), svc=svc, work_id=wid)
            if action == "revert":
                return _git_outcome(svc.vcs_revert(
                    wid, commit_id, message=message,
                    resolutions=resolutions or {}), svc=svc, work_id=wid)
            if action == "cherry_pick":
                return _git_outcome(svc.vcs_cherry_pick(
                    wid, commit_id, message=message,
                    resolutions=resolutions or {}), svc=svc, work_id=wid)
            if action == "restore":
                out = _slim_result(svc.vcs_restore(
                    wid, commit_id, message=message))
                return _ok(**_attach_chapter_previews(out, svc, wid))
            if action == "discard_chapter":
                chapter = _resolve_chapter(svc, ctx, chapter_index)
                out = _slim_result(
                    svc.vcs_discard_chapter(wid, chapter["id"]))
                try:
                    preview = svc.chapter_preview(wid, chapter["id"])
                except Exception:
                    preview = None
                if preview:
                    out["chapter"] = preview
                return _ok(**out)
            if action == "tag_create":
                return _ok(tag=svc.vcs_create_tag(
                    wid, name, ref=from_ref or "HEAD"))
            if action == "tag_delete":
                return _ok(**svc.vcs_delete_tag(wid, name))
            if action == "stash_push":
                return _ok(**_slim_result(svc.vcs_stash_push(wid, message)))
            if action == "stash_pop":
                return _git_outcome(svc.vcs_stash_pop(
                    wid, name, resolutions=resolutions or {}),
                    svc=svc, work_id=wid, preview="worktree")
            if action == "stash_drop":
                return _ok(**svc.vcs_stash_drop(wid, name))
            return _err(f"未知 action：{action}")
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


def _tool_report(svc, ctx):
    def run(action: str = "add", kind: str = "issue", area: str = "other",
            severity: str = "normal", title: str = "", body: str = "",
            work_id: str = "", status: str = "",
            feedback_id: str = "", limit: int = 50) -> str:
        try:
            wid = work_id or str(ctx.get("work_id") or "")
            action = (action or "add").strip()
            if action == "add":
                row = svc.report_feedback(
                    wid, kind=kind, area=area, severity=severity,
                    title=title, body=body,
                    context={"chapter_index": ctx.get("chapter_index")})
                return _ok(feedback=row, open=svc.feedback_summary(wid))
            if action == "list":
                return _ok(items=svc.list_feedback(
                    wid, status=status, limit=max(1, min(int(limit), 200))))
            if action == "resolve":
                return _ok(feedback=svc.set_feedback_status(
                    feedback_id, status or "fixed"))
            return _err(f"未知 action：{action}")
        except (ValueError, RuntimeError) as exc:
            return _err(str(exc))
    return run


# ── 轮次验收 ──────────────────────────────────────────────────────────────
# chat.py 的终止判据是「这一轮没有 tool_calls」。模型可以就此收工，但
# 「说了要做」不等于「做成了」。2026-10-29 17:21 的会话里，模型说完
# 「把第一章建出来并写成正文」就停了，正文一个字都没写；下一轮写正文又
# 三次超时，工具只回了个空错误，模型接着就结束了。
#
# 这里只做**机械**核对，不评价文笔、不猜作者意图：
#   1. 收尾文本说要写正文，本章却还是 0 字；
#   2. 本轮工具是基础设施失败（没有 ok 的 {"error": ...}），模型多半没看懂；
#   3. 本 turn 写了正文，但账本没有结算。
# 问题由 chat.py 回灌成一条 user 消息，最多续两轮。

_INTENT_WRITE = re.compile(r"(写(?:成|出)?正文|正文写|动笔|开始写)")
_NEGATED_WRITE = re.compile(
    r"(不|别|没|无需|暂缓|先不|不用|不要)[^。！？\n]{0,5}$")


def _payload(result: str) -> dict[str, Any]:
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _infra_failure(result: str) -> str:
    """基础设施失败：工具根本没跑完（区别于业务拒绝）。

    业务拒绝走 ``{"ok": false, "error": ...}``——模型已经看到原因，让它
    自己决定重试还是汇报；这里只捞没有 ``ok`` 的 ``{"error": ...}``，
    也就是 ToolRegistry / chat.py 兜住未捕获异常后的形态。空错误同样算。
    """
    data = _payload(result)
    if data.get("ok") is False:
        return ""
    if "error" not in data:
        return ""
    return str(data.get("error") or "").strip() or "（空错误）"


#: 结算被拒里「照着提示改就能过」的那一类：引用/格式问题。
#: 证据对不上、事实冲突不在此列——那两类重试只会诱使模型编证据。
_FIXABLE_SETTLE_MARKERS = (
    "不在作品图中", "subject_ref", "entity_ops", "缺 field",
    "未知 op", "不是数组", "不是对象", "since_chapter 越界",
    "缺 until_chapter", "可用的 subject_ref",
)


def _settle_fixable(error: str) -> bool:
    text = str(error or "")
    return any(marker in text for marker in _FIXABLE_SETTLE_MARKERS)


def _promises_write(text: str) -> bool:
    """收尾文本里有没有「要写正文」的承诺（跳过明确的否定句）。"""
    for match in _INTENT_WRITE.finditer(text or ""):
        head = text[max(0, match.start() - 8):match.start()]
        if _NEGATED_WRITE.search(head):
            continue
        return True
    return False


class WritingGuard:
    """写作轮次的机械验收器。一轮一个实例，状态跨轮保留到本 turn 结束。

    ``ctx`` 必须与写作工具共用同一个 dict：工具解析出的 work_id /
    chapter_index 写回后，验收这边立刻看得到。
    """

    def __init__(self, svc: Any, ctx: dict[str, Any]) -> None:
        self.svc = svc
        self.ctx = ctx
        self._drafted: set[int] = set()
        self._settle_attempted: set[int] = set()
        self._settle_nudged: set[int] = set()
        self._settle_retry: dict[int, str] = {}
        self._settle_retry_asked: set[int] = set()
        self._round_failures: list[str] = []

    # ── 循环钩子 ──────────────────────────────────────────────────────

    def begin_round(self) -> None:
        self._round_failures = []

    def observe(self, tool_name: str, result: str) -> None:
        """记录一次工具执行结果。失败只记本轮，成功记账到整个 turn。"""
        failure = _infra_failure(result)
        if failure:
            self._round_failures.append(f"{tool_name} → {failure}")
        data = _payload(result)
        index = int(data.get("chapter_index") or self.ctx.get("chapter_index") or 0)
        if tool_name == "writing_settle_chapter" and index:
            self._settle_attempted.add(index)
            # 引用/格式类拒绝是机械可修的：记下来催一次，别让模型
            # 把它当产品缺陷就此收工（18:55 的会话就是这样停下的）。
            if data.get("ok") is False and _settle_fixable(
                    str(data.get("error") or "")):
                self._settle_retry[index] = str(data.get("error") or "")[:200]
        if (tool_name == "writing_draft_chapter" and data.get("ok")
                and index):
            self._drafted.add(index)

    def check(self, visible_text: str) -> list[str]:
        """返回本轮收尾时未达成的项。空列表 = 可以结束。"""
        issues: list[str] = []
        index = int(self.ctx.get("chapter_index") or 0)

        if (index and index not in self._drafted
                and _promises_write(visible_text)):
            chapter = self._chapter(index)
            if chapter is not None and int(chapter.get("word_count") or 0) == 0:
                issues.append(
                    f"你刚才说要写第{index}章正文，但现在这章还是 0 字，"
                    "也没有调用 writing_draft_chapter。要么现在写，"
                    "要么如实告诉作者还没写。")

        if self._round_failures:
            issues.append(
                "上一轮工具没有执行成功：" + "；".join(self._round_failures[:3])
                + "。要么重试一次，要么把原因如实告诉作者。")

        for done in sorted(self._drafted):
            if done in self._settle_attempted or done in self._settle_nudged:
                continue
            if self._is_settled(done):
                continue
            self._settle_nudged.add(done)
            issues.append(
                f"第{done}章正文已经写了，但账本还没结算——后面几章要读"
                "它的事实与摘要。先调 writing_settle_chapter，并用 outcomes"
                "逐条报本章合同的兑现（hit|条目|逐字证据，没做到写 miss|条目|原因）；"
                "如果作者这轮只要草稿，就在回复里说明一句，不必结算。")

        # 结算被拒但**可以自己修**：把拒绝原文和处方一起回去，只催一次。
        # 事实冲突/证据不在这条线上，那些重试只会诱使模型编证据。
        for idx, err in sorted(self._settle_retry.items()):
            if idx in self._settle_retry_asked:
                continue
            self._settle_retry_asked.add(idx)
            issues.append(
                f"第{idx}章结算被拒，但这是可以自己修的（不是你或产品的"
                f"故障）：{err}。按上面给出的可用 subject_ref 改一版 delta"
                "再调一次 writing_settle_chapter；确实改不动，再把原因如实"
                "告诉作者。")
        return issues

    # ── 现场读取 ──────────────────────────────────────────────────────

    def _chapter(self, index: int) -> dict[str, Any] | None:
        work_id = str(self.ctx.get("work_id") or "")
        if not work_id or index <= 0:
            return None
        try:
            return self.svc.chapter(work_id, index)
        except (ValueError, RuntimeError):
            return None

    def _is_settled(self, index: int) -> bool:
        """账本是否已按当前正文结算：摘要存在且 source_hash 对得上。"""
        chapter = self._chapter(index)
        if chapter is None:
            return False
        want = str(chapter.get("content_hash") or "")
        if not want:
            return False
        try:
            rows = self.svc.db.summaries_for_work(
                str(self.ctx.get("work_id") or ""))
        except Exception:                            # noqa: BLE001
            return False
        return any(str(r.get("chapter_id") or "") == str(chapter.get("id") or "")
                   and str(r.get("source_hash") or "") == want
                   for r in rows)


# ── Schema ────────────────────────────────────────────────────────────────

def _spec(name: str, description: str, parameters: dict,
          execute: Callable[..., Any],
          execute_async: Callable[..., Awaitable[str]] | None = None) -> ToolSpec:
    # 异步实现的工具必须挂到 ``execute_async``：只塞进 ``execute`` 的话，
    # ToolRegistry 会直接返回一个未被 await 的协程对象（json.loads 会炸）。
    if execute_async is None and inspect.iscoroutinefunction(execute):
        execute_async, execute = execute, (lambda **_kw: _err(
            "该工具需要异步执行（不应走同步路径）"))
    return ToolSpec(
        name=name,
        category="writing",
        toolset=WRITING_TOOLSET,
        exposure=ToolExposure.DIRECT,
        description=description,
        parameters=parameters,
        execute=execute,
        execute_async=execute_async,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    )


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": required or []}


_WRITING_RESEARCH_DESC = (
    "派一个素材侦察子代理，去书库/素材库找参考原文，返回可直接传给 "
    "writing_draft_chapter(refs=[...]) 的坐标。子代理只检索、不改稿。"
    "适合多条互不相关的素材方向；只有一两个检索词时自己调 "
    "writing_research 更省。可用类型：writing_researcher。"
)

_REF_FIELDS = ("material_id", "path", "chunk_id", "book_id",
               "chapter_index", "chunk_index", "para_start", "para_end",
               "note")


def _writing_agent_result(svc, ctx: dict[str, Any],
                          payload: dict[str, Any]) -> dict[str, Any]:
    """子代理输出 → 主 agent 能直接用的 ``refs``。

    子代理的最终消息约定是 ``{"refs": [...], "notes": "..."}``；这里再拿
    真实存储把每条坐标试读一遍，读不到的单独列出，不让死坐标流进 draft。
    """
    from llm_json import loads_tolerant

    if str(payload.get("status") or "") != "completed":
        return {"ok": False,
                "error": str(payload.get("error") or "子代理执行失败"),
                "refs": []}
    text = str(payload.get("result_text") or "")
    parsed = loads_tolerant(text, expect=dict)
    parsed = parsed if isinstance(parsed, dict) else {}
    raw_refs = parsed.get("refs")
    raw_refs = raw_refs if isinstance(raw_refs, list) else []
    work_id = str(ctx.get("work_id") or "")
    keep: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw in raw_refs:
        if not isinstance(raw, dict):
            continue
        ref = {k: raw.get(k) for k in _REF_FIELDS
               if raw.get(k) not in (None, "")}
        if not ref:
            continue
        try:
            items, errors = svc.resolve_refs([ref], work_id=work_id)
        except Exception as exc:                     # noqa: BLE001
            items, errors = [], [str(exc)]
        if errors or not items:
            dropped.append({"ref": ref,
                            "error": errors[0] if errors else "读不到"})
            continue
        # 同一块/同一张卡只留一条（子代理可能换个说法重复报同一条）。
        item = items[0]
        if item.get("kind") == "book":
            key = ("book", item.get("chunk_id"))
        else:
            key = ("material", item.get("material_id") or ref.get("path"))
        if key in seen:
            continue
        seen.add(key)
        keep.append(ref)
    out: dict[str, Any] = {
        "ok": True,
        "refs": keep,
        "notes": str(parsed.get("notes") or ""),
    }
    if dropped:
        out["dropped"] = dropped
    if not parsed:
        out["parse_error"] = True
        out["raw"] = text[-1500:]
    return out


def build_writing_registry(base: ToolRegistry, svc: Any,
                           ctx: dict[str, Any],
                           progress: Any = None) -> ToolRegistry:
    """在基础注册表的**副本**上加写作工具，返回请求级注册表。

    不改共享注册表：写作工具闭包持有本次请求的 ``svc`` 与 ``ctx``，
    并发请求之间不能互相看见。``progress`` 是可选异步回调，写正文时
    把 ``stage`` / ``delta`` 事件转给前端状态行（正文不上屏）。
    """
    # 写作现场只需要写作工具；self_explain 是唯一例外（用户会问合同/版本/
    # 作者层）。平台/阅读/图谱/音色工具在这里没有用途；全量暴露会多出
    # 二十多个 schema，既吃提示词预算，也会诱导误调用。
    registry = base.get_registry_for_toolsets([WRITING_TOOLSET, SELF_TOOLSET])
    registry.reading_ctx.update(base.reading_ctx)
    # self_explain 的 vcs 探针要读本轮实时现场；只挂请求级 registry，
    # 不能写进共享 base（并发请求会串作品）。
    registry.reading_ctx.update({
        "page": "writing",
        "writing_ctx": ctx,
        "writing_svc": svc,
    })
    # 在副本上重新安装：闭包必须捕获副本的 reading_ctx。
    install_self_tool(registry)

    registry.register(_spec(
        "writing_status",
        "查看写作现状：所有作品（含章数），以及指定作品的章节状态与合同有无。"
        "不知道有哪些作品、或要确认某章是否已有正文时先调它。",
        _obj({"work_id": {"type": "string", "description": "作品 id；可留空"},
              "work_title": {"type": "string", "description": "作品名；可用标题代替 id"}}),
        _tool_status(svc, ctx)))

    registry.register(_spec(
        "writing_create_work",
        "新建小说作品。用户说「写一本叫《X》的小说」时调用；"
        "同名作品直接复用，不重复建。",
        _obj({"title": {"type": "string", "description": "书名"},
              "genre": {"type": "string", "description": "题材，可空"},
              "premise": {"type": "string", "description": "一句话设定，可空"},
              "narrative_pov": {"type": "string", "description": "视角，可空"},
              "target_chars": {"type": "integer", "description": "单章目标字数，0=默认"}},
             ["title"]),
        _tool_create_work(svc, ctx)))

    registry.register(_spec(
        "writing_create_chapter",
        "新建一章并同时写入最小合同（合同是硬边界，缺了不能生成）。"
        "purpose 一句话；must_hit_now 每行一条必须兑现的事；"
        "「可以写但不解释」的放 withhold，不要混进 forbidden_cross。",
        _obj({"title": {"type": "string", "description": "章标题"},
              "purpose": {"type": "string", "description": "本章推进什么（一句话）"},
              "must_hit_now": {"type": "string", "description": "必须兑现的事，每行一条"},
              "forbidden_cross": {"type": "string", "description": "禁止触碰的事，每行一条"},
              "withhold": {"type": "string",
                           "description": "可以写进场景但不解释的事，每行一条"},
              "ending_state": {"type": "string", "description": "章末必须到达的状态"},
              "target_chars": {"type": "integer", "description": "本章目标字数"},
              "work_id": {"type": "string"}, "work_title": {"type": "string"}}),
        _tool_create_chapter(svc, ctx)))

    registry.register(_spec(
        "writing_set_contract",
        "改已有章节的合同。只传要改的项，未传的保持原样。"
        "改合同等于重新立约：已结清的合同回到「在写」，旧兑现记录作废。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"},
              "purpose": {"type": "string"},
              "must_hit_now": {"type": "string", "description": "每行一条"},
              "forbidden_cross": {"type": "string",
                                  "description": "禁止发生/揭示，每行一条"},
              "withhold": {"type": "string",
                           "description": "可以写但不点破，每行一条"},
              "ending_state": {"type": "string"},
              "next_entry_state": {"type": "string"},
              "target_chars": {"type": "integer"},
              "arc_ref": {"type": "string",
                          "description": "当前弧的引用，如 计划稿/第二卷/第12-20章.md"
                                         "；只写路径，不粘贴正文"}}),
        _tool_set_contract(svc, ctx)))

    registry.register(_spec(
        "writing_read_chapter",
        "读某章正文与合同。要改稿、要接续上一章、要核对事实时先读。"
        "变更工具刚返回的 chapters 首尾预览已经够核对时，不要为了复查再读整章。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"}}),
        _tool_read_chapter(svc, ctx)))

    registry.register(_spec(
        "writing_set_mode",
        "切换写作模式：standard 常规 / detail 细致（动笔前必须交 "
        "research_brief，预算更大）。只有作者明确同意才能切；未授权先返回 "
        "needs_authorization，把话转给作者。chapter_index>0 只覆盖该章；"
        "remember=true 记住「以后可自行切」，必须同时 authorized=true。",
        _obj({"mode": {"type": "string",
                       "enum": ["standard", "detail", "inherit"]},
              "chapter_index": {"type": "integer",
                                "description": "0 = 会话级；>0 = 只覆盖这一章"},
              "reason": {"type": "string",
                         "description": "一句话：为什么这一章需要/不需要细致"},
              "authorized": {"type": "boolean",
                             "description": "作者本轮明确同意才传 true"},
              "remember": {"type": "boolean",
                           "description": "作者说「以后你决定」时传 true"}},
             ["mode"]),
        _tool_set_mode(svc, ctx)))

    registry.register(_spec(
        "writing_draft_chapter",
        "装配上下文并写出一章正文，落库为草稿；需要本章已有合同。"
        "返回只有首尾摘要，不要在回复中复述全文。"
        "refs 放本轮选定的素材坐标，服务端按坐标读回逐字原文注入，不摘要、"
        "不改写；没找到就不传。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"},
              "extra_instruction": {"type": "string",
                                    "description": "作者本轮的额外要求，如「这一章节奏慢一点」"},
              "refs": {
                  "type": "array",
                  "description": "本轮要用的参考素材（从检索结果里挑）",
                  "items": {
                      "type": "object",
                      "properties": {
                          "material_id": {"type": "string",
                                          "description": "素材库卡片的身份"},
                          "path": {"type": "string",
                                   "description": "素材卡的相对路径（可代替 material_id）"},
                          "chunk_id": {"type": "string",
                                       "description": "书库块的 id"},
                          "book_id": {"type": "string"},
                          "chapter_index": {"type": "integer"},
                          "chunk_index": {"type": "integer"},
                          "para_start": {"type": "integer",
                                         "description": "只要块内某几段时的起始段（1-based）"},
                          "para_end": {"type": "integer"},
                          "note": {"type": "string",
                                   "description": "一句：这段借来做什么（可选）"},
                      },
                  },
              },
              "plan_first": {
                  "type": "boolean",
                  "description": "先写短细纲再动笔（多一次调用）",
              },
              "research_brief": {
                  "type": "object",
                  "description": "细致模式必填：动笔前交代取舍（只验形状）",
                  "properties": {
                      "questions": {
                          "type": "array", "items": {"type": "string"},
                          "description": "这一章哪些细节不能靠想象"},
                      "searched": {
                          "type": "array",
                          "items": {
                              "type": "object",
                              "properties": {
                                  "source": {"type": "string",
                                             "description": "materials/library/semantic/web"},
                                  "query": {"type": "string"},
                                  "result": {"type": "string"},
                              },
                          },
                          "description": "查了哪里、查到什么（没有结果也算）"},
                      "gaps": {
                          "type": "array", "items": {"type": "string"},
                          "description": "没找到、需要作者定的；不给 refs 时"
                                         "必须至少写一条说明"},
                      "notes": {"type": "string"},
                  },
              }}),
        _tool_draft_chapter(svc, ctx, progress)))

    registry.register(_spec(
        "writing_replace_text",
        "按唯一锚点替换正文里的一段。anchor_quote 必须逐字摘自已录正文且只出现一次。"
        "用于「把这段改冷一点」这类局部修改；改事实请先和用户确认。",
        _obj({"anchor_quote": {"type": "string"},
              "replacement": {"type": "string"},
              "chapter_index": {"type": "integer"},
              "work_id": {"type": "string"}},
             ["anchor_quote", "replacement"]),
        _tool_replace_text(svc, ctx)))

    registry.register(_spec(
        "writing_settle_chapter",
        "写出正文后调用：从正文抽取变化写进账本（事实/承诺/摘要），"
        "并结清本章合同。outcomes 每行一条：hit|合同条目原文|逐字证据；"
        "没做到写 miss|条目|原因，半成写 partial。"
        "结算被拒时原样告诉作者，不要自己改账本。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"},
              "outcomes": {"type": "string",
                           "description": "每行一条：hit|条目原文|逐字证据；"
                                          "miss|条目原文|原因"},
              "note": {"type": "string",
                       "description": "结清时的一句话（可选）；写「读者新知道：…」"}}),
        _tool_settle(svc, ctx, progress)))

    registry.register(_spec(
        "writing_references",
        "给作品定参考书架（别的书，不是本书正典）。action=list 看书库；"
        "action=set 用 titles（逗号分隔）设检索范围，titles 留空即清空；"
        "这只是检索范围，写稿时不会自动注入。找素材仍用 writing_research(book=…)，"
        "选定后把坐标传给 writing_draft_chapter(refs=…)。参考书只供写法，不供设定。",
        _obj({"action": {"type": "string", "enum": ["list", "set"]},
              "titles": {"type": "string", "description": "书名，逗号分隔"},
              "work_id": {"type": "string"}}),
        _tool_references(svc, ctx)))

    registry.register(_spec(
        "writing_research",
        "找素材：先查素材库（本书 素材/ + 全局库），再查已入库的书，可选 "
        "DeepSeek 联网；返回带出处与逐字引文的候选卡。需要感官细节 / 制度流程 /"
        "行话 / 物象 / 因果解释时用。query 用需求词与场景词，不要用本作人名；"
        "只给 3–10 条候选。候选卡的 excerpt 只是块内命中窗口，不是全文；"
        "片段里没看到要找的句子时，用 writing_materials read_chunk 读整块，"
        "不要据此说书里没有。只借写法与细节，不搬专名、设定、情节。"
        "作者说「照《X》」时用 book 限定范围；空项目也能查书库与素材库。"
        "关键词查不到、又说不清词时用 source=semantic（需已建向量索引）。"
        "联网必须作者同意（本轮明确说「上网查」传 enable_web=true，或作品开关"
        "已开），每轮最多 2 次；save=true 留下出处，已知网址用 url 抓页。",
        _obj({"query": {"type": "string",
                        "description": "要找什么，如「码头夜班的工钱与行话」；"
                                       "semantic 可以用一整句意图"},
              "source": {"type": "string",
                         "enum": ["materials", "library", "semantic",
                                  "web", "auto"],
                         "description": "materials=素材库；library=书库关键词（默认）；"
                                        "semantic=书库语义近邻；web=只联网；"
                                        "auto=素材库→书库，已授权才联网"},
              "url": {"type": "string",
                      "description": "直接抓这一页（需联网授权）；给了 url 就不搜"},
              "book": {"type": "string",
                       "description": "限定书名（可逗号分隔多本）；"
                                      "作者说「照《X》」时用"},
              "enable_web": {"type": "boolean",
                             "description": "作者本轮明确要求联网时才传 true"},
              "remember_web": {"type": "boolean",
                               "description": "只有作者说「以后都允许联网」"
                                              "才传 true，把开关记进作品"},
              "save": {"type": "boolean",
                       "description": "把联网结果落进 素材/网络/<日期>-<词>.md"},
              "limit": {"type": "integer",
                        "description": "候选条数，默认 6，建议 3–10"},
              "work_id": {"type": "string"}}),
        _tool_research(svc, ctx)))

    registry.register(_spec(
        "writing_docs",
        "维护作品工作区（作品记忆/设定/计划稿/素材，都进写作 git）。作品记忆"
        "只写长期约定；设定只写作者说清楚的，写作时只带索引，正文用 read；"
        "计划稿/素材不进上下文；别的作品只读且只认 素材/。list 看目录（带 M/U "
        "状态）；read 读一份；search 找关键词（scope=设定/素材/计划稿）；write "
        "覆盖（同内容已存在会拒绝）；append 追加（重复行自动跳过）；delete 删除"
        "（作品记忆不能删）。作品记忆不许 write/append，作者点名用 "
        "writing_memory(note)，结算后用 auto。",
        _obj({"action": {"type": "string",
                         "enum": ["list", "read", "search", "write",
                                  "append", "delete"]},
              "path": {"type": "string",
                       "description": "相对路径：作品记忆.md、设定/人物/人物A.md、"
                                      "计划稿/结局备选.md、素材/观察/笔记.md"},
              "query": {"type": "string",
                        "description": "search 的关键词，空格分隔多个"},
              "scope": {"type": "string",
                        "description": "search/list 的分区：设定 / 素材 / 计划稿；"
                                       "空=全部"},
              "limit": {"type": "integer",
                        "description": "search 返回条数，默认 10，建议 5–15"},
              "content": {"type": "string",
                          "description": "write/append 的正文（Markdown）"},
              "work_id": {"type": "string"}},
             ["action"]),
        _tool_docs(svc, ctx)))

    registry.register(_spec(
        "writing_materials",
        "维护全局素材库（不进写作 git，不自动进上下文）"
        "list/search/read/read_chunk 查（后两个读卡/读块）；"
        "intake 收录（书块坐标以库内原文为准）；pending 列待归类卡；"
        "cook 补抬头并迁出待归类；promote 复制进作品 素材/；"
        "use 记消费（shortlist/merge/inline）；uses 查消费；"
        "rename/move 改卡片或分类；recycled/restore 回收站；"
        "audit 只报告（含近重复、超期）；merge 短卡并入长卡"
        "（需 authorized=true）；delete 进回收站（先问）。"
        "read/cook 可只给 material_id。分类是主目录树"
        "（人物/场景/社会/语言/时代/写法…），一卡一主分类，其余进 tags；"
        "说明只写这段是什么。不要自动加工/合并/清理",
        _obj({"action": {"type": "string",
                         "enum": ["list", "search", "read", "read_chunk",
                                  "intake", "pending", "cook",
                                  "promote", "use", "uses",
                                  "rename", "move", "recycled", "restore",
                                  "audit", "merge", "delete"]},
              "path": {"type": "string",
                       "description": "read/delete/cook/merge 的路径；"
                                      "merge 是短卡；read/cook 也可传 material_id"},
              "query": {"type": "string", "description": "search 的关键词"},
              "material_id": {"type": "string",
                              "description": "卡片身份（摘录哈希）；"
                                             "read/cook/promote/use 用"},
              "excerpt": {"type": "string",
                          "description": "intake 的逐字原文；有 chunk 引用时"
                                         "以库内原文为准"},
              "title": {"type": "string", "description": "卡片标题"},
              "category": {"type": "string",
                           "description": "主分类路径，如 情感/爱情/离别；"
                                          "不确定写 待归类"},
              "tags": {"type": "string",
                       "description": "其余维度，逗号或空格分隔"},
              "description": {"type": "string",
                              "description": "这段是什么（中性说明），不写"
                                             "适用情景"},
              "instruction": {"type": "string",
                              "description": "作者点名收录时的原话"},
              "source": {"type": "string",
                         "enum": ["author", "book", "web"],
                         "description": "来源类型；书引用用 book"},
              "book_title": {"type": "string"},
              "book_id": {"type": "string"},
              "chapter_index": {"type": "integer"},
              "chunk_index": {"type": "integer"},
              "chunk_id": {"type": "string",
                           "description": "语义检索结果里的 chunk_id"},
              "para_start": {"type": "integer",
                             "description": "intake 只收块内某几段时的起始段"
                                            "（1-based，含本段）；先 read_chunk"},
              "para_end": {"type": "integer",
                           "description": "结束段，含本段；不传则只收 "
                                          "para_start 那一段"},
              "url": {"type": "string", "description": "source=web 的网址"},
              "work_id": {"type": "string"},
              "name": {"type": "string",
                       "description": "rename 的新名字（卡片可以不带 .md）"},
              "target_dir": {"type": "string",
                             "description": "move 的目标分类，如 情感/爱情"},
              "target": {"type": "string",
                         "description": "restore 目标路径；merge 的长卡（into）"},
              "authorized": {"type": "boolean",
                             "description": "merge 必须作者同意才传 true"},
              "entry_kind": {"type": "string", "enum": ["file", "dir"],
                             "description": "rename/move 的是卡片还是分类目录"},
              "kind": {"type": "string",
                       "enum": ["shortlist", "merge", "inline"],
                       "description": "消费方式：进作品素材 / 进设定 / 进正文"},
              "target_path": {"type": "string",
                              "description": "提升到作品时的目标路径，默认"
                                             " 素材/<标题>.md"},
              "note": {"type": "string",
                       "description": "这部作品怎么改的（一句话）"},
              "limit": {"type": "integer",
                        "description": "search/uses 返回条数，默认 10"}}),
        _tool_materials(svc, ctx)))

    registry.register(_spec(
        "writing_memory",
        "维护作品记忆（两段式）：note 立即留痕（作者点名/纠正）、extract "
        "抽候选、consolidate 合并进 作品记忆.md 与 设定/、auto 先抽取（pending≥3 "
        "才自动合并，也可显式 consolidate）、status 看状态、audit 只报告重复。"
        "结算后调 auto；transcript 传作者原话。只写这两处，不碰计划稿/素材/正文。",
        _obj({"action": {"type": "string",
                         "enum": ["status", "note", "extract", "consolidate",
                                  "auto", "audit"]},
              "work_id": {"type": "string"},
              "chapter_index": {"type": "integer"},
              "transcript": {"type": "string",
                             "description": "作者这一轮的改动/要求原话；没有就留空"},
              "target": {"type": "string",
                         "enum": ["bible", "setting", "plan"],
                         "description": "note 目标层"},
              "path": {"type": "string",
                       "description": "note 文件；bible 不传"},
              "section": {"type": "string",
                          "description": "note 小节；可空"},
              "op": {"type": "string", "enum": ["append", "replace", "delete"],
                     "description": "note 动作"},
              "text": {"type": "string", "description": "note 内容"},
              "old": {"type": "string", "description": "replace 旧原文"},
              "evidence": {"type": "string", "description": "作者原话/证据"}}),
        _tool_memory(svc, ctx)))

    registry.register(_spec(
        "writing_review_chapter",
        "审这一章：6 域 10 判据评分 + 带逐字证据的问题清单。只诊断，不改稿。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"}}),
        _tool_review(svc, ctx)))

    registry.register(_spec(
        "writing_git",
        "写作 git 查询（只有 selftest_seed/cleanup 会建/删临时自测作品）。"
        "status 看工作区/分支/标签（完整现场已在 system prompt，缺 "
        "branches/tags 才查）；log 看历史；diff 看两版或两分支差异"
        "（带 file_path 只看单文件逐行 text_diff；返回目标侧改动章的 head/tail "
        "预览）；reflog 找回切走前的位置；branches/tags/stash_list 列引用；"
        "selftest_plan 取自测清单（phase=A/B/C/D 只取一段，或 step=C4 只取一"
        "步）；selftest_seed 新建临时自测作品；selftest_cleanup 删除"
        "（只认「版本自测-」前缀）。",
        _obj({"action": {"type": "string",
                         "enum": ["status", "log", "diff", "reflog",
                                  "branches", "tags", "stash_list",
                                  "selftest_plan", "selftest_seed",
                                  "selftest_cleanup"]},
              "work_id": {"type": "string"},
              "work_title": {"type": "string"},
              "branch": {"type": "string", "description": "log 的分支过滤"},
              "all_branches": {"type": "boolean"},
              "chapter_index": {"type": "integer",
                                "description": "log/diff 只看这一章"},
              "from_ref": {"type": "string",
                           "description": "diff 起点；默认 HEAD，可写分支名/提交 id/'main...岔路A'"},
              "to_ref": {"type": "string", "description": "diff 终点；默认 WORK"},
              "file_path": {"type": "string",
                            "description": "diff 只看这份工作区文件，返回逐行 text_diff"},
              "limit": {"type": "integer"},
              "phase": {"type": "string",
                        "description": "selftest_plan 的阶段 A/B/C/D；只返回这一段"},
              "step": {"type": "string",
                       "description": "selftest_plan 的步骤 id，如 C4"}},
             ["action"]),
        _tool_git_read(svc, ctx)))

    registry.register(_spec(
        "writing_git_change",
        "写作 git 变更。commit/amend 存档与修正（工作区与正文一起进历史；"
        "chapters 限定章）；branch_create/switch/rename/delete 管分支"
        "（create 可 switch=true）；merge/revert/cherry_pick/restore/"
        "discard_chapter/tag_*/stash_* 按名执行。set_habit 改版本习惯"
        "（chapter 每章结算后自动存档 / manual 只在作者要求时存档），"
        "只有作者明确要求才调。成功后返回精简 status + 前 3 章预览；"
        "commit/amend 返回 commit.id，revert/restore 直接用，不要再查 log。"
        "返回 needs_resolution 时先讲冲突，再带 resolutions 重调。"
        "restore/branch_delete(force) 是重动作，作者明说才做。",
        _obj({"action": {"type": "string",
                         "enum": ["commit", "amend", "branch_create",
                                  "branch_switch", "branch_rename",
                                  "branch_delete", "merge", "revert",
                                  "cherry_pick", "restore",
                                  "discard_chapter", "tag_create",
                                  "tag_delete", "stash_push", "stash_pop",
                                  "stash_drop", "set_habit"]},
              "habit": {"type": "string", "enum": ["chapter", "manual"],
                        "description": "set_habit 的目标习惯"},
              "work_id": {"type": "string"},
              "work_title": {"type": "string"},
              "message": {"type": "string",
                          "description": "存档说明：写「改了什么」，不要写「更新」"},
              "name": {"type": "string",
                       "description": "分支/标签/暂存名；暂存也可传 stash@{0}、短号或完整 commit id"},
              "new_name": {"type": "string", "description": "分支改名后的名字"},
              "from_ref": {"type": "string",
                           "description": "branch_create/tag_create 的起点；默认 HEAD"},
              "commit_id": {"type": "string",
                            "description": "revert/cherry_pick/restore 的目标存档；"
                                           "完整 id 或 8 位短号均可，优先用上一步"
                                           "返回的 commit.id / parent_id"},
              "branch": {"type": "string", "description": "merge 要合进来的分支"},
              "chapter_index": {"type": "integer",
                                "description": "discard_chapter 的章号"},
              "chapters": {"type": "array", "items": {"type": "integer"},
                           "description": "commit 只存档这几章（章号）。"
                           "自己这轮只改了一部分章时用它，别把作者手改的章一起存档"},
              "force": {"type": "boolean",
                        "description": "branch_delete 强制删除未合并分支"},
              "switch": {"type": "boolean",
                         "description": "branch_create 后是否立刻切过去"},
              "checkpoint": {"type": "boolean",
                             "description": "切分支前是否自动存档，默认 true"},
              "resolutions": {"type": "object",
                              "description": "冲突裁决：{conflict.key: 'ours'|'theirs'|'base'}"}},
             ["action"]),
        _tool_git_change(svc, ctx)))

    registry.register(_spec(
        "writing_report",
        "记录功能问题或建议（可检索、可关单的反馈，不是聊天）。"
        "自测或使用中发现行为与说明不符、报错、缺少常用操作、体验问题时，"
        "用 action=add 记下；title 一句话，body 写哪一步/期望/实际/复现。"
        "list 查看待处理项；resolve 关单。不要记录「写作内容好不好」——"
        "那是审稿工具的事。",
        _obj({"action": {"type": "string",
                         "enum": ["add", "list", "resolve"]},
              "kind": {"type": "string",
                       "enum": ["issue", "suggestion", "question"]},
              "area": {"type": "string",
                       "description": "git / draft / settle / review / ui / other"},
              "severity": {"type": "string",
                           "enum": ["low", "normal", "high"]},
              "title": {"type": "string"},
              "body": {"type": "string"},
              "work_id": {"type": "string"},
              "status": {"type": "string",
                         "enum": ["open", "acknowledged", "fixed", "wontfix"]},
              "feedback_id": {"type": "string"},
              "limit": {"type": "integer"}},
             ["action"]),
        _tool_report(svc, ctx)))

    # ── 子代理：素材侦察兵 ────────────────────────────────────────────
    # 基础注册表里的 Agent 闭包指向**基础**工具表（没有写作工具），所以
    # 在写作页必须重新注册一份：闭包捕获这份请求级 registry，子代理才能
    # 看到 writing_research。streaming=False —— 子代理的中间推理不该顺着
    # 工具结果回流进主上下文，只交回压好的坐标。
    from tools.agent import _register_agent_tool
    _register_agent_tool(
        registry,
        getattr(svc, "api_client", None),
        getattr(svc, "config", None),
        default_type="writing_researcher",
        allowed_types={"writing_researcher"},
        description=_WRITING_RESEARCH_DESC,
        streaming=False,
        result_transform=lambda payload: _writing_agent_result(svc, ctx, payload),
    )
    return registry
