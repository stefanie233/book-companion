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
from typing import Any, Awaitable, Callable

from tools import PermissionMode, ToolExposure, ToolRegistry, ToolSpec
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
        raise ValueError("找不到作品；先调用 writing_create_work")
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
    chapters = svc.db.list_chapters(work["id"])
    return {
        "work_id": work["id"],
        "title": work.get("title", ""),
        "genre": work.get("genre", ""),
        "chapters": [
            {"chapter_index": c["chapter_index"], "title": c.get("title", ""),
             "status": c.get("status"), "words": c.get("word_count", 0),
             "has_contract": bool(svc.db.get_contract(c["id"]))}
            for c in chapters
        ],
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
            forbidden_cross: str = "", ending_state: str = "",
            target_chars: int = 0) -> str:
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
            forbidden_cross: str = "", ending_state: str = "",
            next_entry_state: str = "", target_chars: int = 0) -> str:
        try:
            _resolve_work(svc, ctx, work_id)
            chapter = _resolve_chapter(svc, ctx, chapter_index)
            fields: dict[str, Any] = {}
            for key, value in (("purpose", purpose),
                               ("ending_state", ending_state),
                               ("next_entry_state", next_entry_state)):
                if value.strip():
                    fields[key] = value.strip()
            for key, value in (("must_hit_now", must_hit_now),
                               ("forbidden_cross", forbidden_cross)):
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


def _tool_draft_chapter(svc, ctx) -> Callable[..., Awaitable[str]]:
    async def run(chapter_index: int | None = None, work_id: str = "",
                  extra_instruction: str = "") -> str:
        try:
            work = _resolve_work(svc, ctx, work_id)
            chapter = _resolve_chapter(svc, ctx, chapter_index)
            if not svc.contract(chapter["id"]):
                return _err("本章还没有合同。先调用 writing_set_contract")
            content = ""
            tokens = 0
            async for event in svc.stream_chapter(
                    work["id"], chapter["chapter_index"],
                    extra_instruction=extra_instruction):
                if event["event"] == "done":
                    content = event["data"].get("content", "")
                elif event["event"] == "stage" and event["data"].get("stage") == "assembled":
                    tokens = event["data"].get("tokens", 0)
                elif event["event"] == "error":
                    return _err(event["data"].get("message", "生成失败"))
            if not content:
                return _err("模型没有返回正文")
            return _ok(
                chapter_index=chapter["chapter_index"],
                word_count=len(content), context_tokens=tokens,
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


def _tool_settle(svc, ctx) -> Callable[..., Awaitable[str]]:
    async def run(chapter_index: int | None = None, work_id: str = "") -> str:
        try:
            work = _resolve_work(svc, ctx, work_id)
            chapter = _resolve_chapter(svc, ctx, chapter_index)
            result = await svc.settle(work["id"], chapter["chapter_index"])
            if not result.get("ok"):
                # 整份拒绝是设计：把错误原样交回模型，让它重试或如实汇报。
                return _err("结算被拒：" + "；".join(result.get("errors") or []))
            return _ok(chapter_index=chapter["chapter_index"],
                       written=result.get("written"),
                       summary=result.get("delta", {}).get("chapter_summary", ""))
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
            return _ok(chapter_index=chapter["chapter_index"],
                       delivery_status=result.get("delivery_status"),
                       quality_score=result.get("quality_score"),
                       coverage=result.get("coverage"),
                       findings=findings,
                       rejected=len(result.get("rejected") or []))
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
           ("branch", "stash_count", "ledger_dirty", "clean", "counts",
            "feedback")}
    out["head"] = _status_head(status)
    out["changes"] = _slim_changes(status.get("changes"))
    out["branches"] = [
        {k: b.get(k) for k in ("name", "commit_id", "is_head", "ahead",
                               "behind", "base_short", "message")
         if b.get(k) not in (None, "", [])}
        for b in status.get("branches") or []
    ]
    out["tags"] = _slim_tags(status.get("tags"))
    return out


def _brief_status(status: dict | None) -> dict:
    """变更操作的随附状态：只留「现在在哪、干不干净、改了几章」。"""
    if not status:
        return {}
    out = {k: status.get(k) for k in
           ("branch", "stash_count", "ledger_dirty", "clean", "counts")
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
            phase: str = "", step: str = "") -> str:
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
                    text=bool(chapter_id))
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
            chapters: list[int] | None = None) -> str:
        try:
            work = _resolve_work(svc, ctx, work_id, work_title)
            wid = str(work["id"])
            action = (action or "").strip()
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


def build_writing_registry(base: ToolRegistry, svc: Any,
                           ctx: dict[str, Any]) -> ToolRegistry:
    """在基础注册表的**副本**上加写作工具，返回请求级注册表。

    不改共享注册表：写作工具闭包持有本次请求的 ``svc`` 与 ``ctx``，
    并发请求之间不能互相看见。
    """
    # 写作现场只需要写作工具。平台/阅读/图谱/音色工具在这里没有用途；
    # 全量暴露会多出二十多个 schema，既吃提示词预算，也会诱导误调用。
    registry = base.get_registry_for_toolsets([WRITING_TOOLSET])
    registry.reading_ctx.update(base.reading_ctx)

    registry.register(_spec(
        "writing_status",
        "查看写作现状：所有作品（含章数），以及指定作品的章节状态与合同有无。"
        "不知道有哪些作品、或要确认某章是否已有正文时先调它。",
        _obj({"work_id": {"type": "string", "description": "作品 id；可留空"},
              "work_title": {"type": "string", "description": "作品名；可用标题代替 id"}}),
        _tool_status(svc, ctx)))

    registry.register(_spec(
        "writing_create_work",
        "新建小说作品。用户说「写一本叫《X》的小说」时调用。"
        "同名作品已存在则直接复用，不重复建。",
        _obj({"title": {"type": "string", "description": "书名"},
              "genre": {"type": "string", "description": "题材，可空"},
              "premise": {"type": "string", "description": "一句话设定，可空"},
              "narrative_pov": {"type": "string", "description": "视角，可空"},
              "target_chars": {"type": "integer", "description": "单章目标字数，0=默认"}},
             ["title"]),
        _tool_create_work(svc, ctx)))

    registry.register(_spec(
        "writing_create_chapter",
        "新建一章并同时写入最小合同（合同是写作硬边界，缺了就不能生成）。"
        "合同项越具体越好：purpose 一句话，must_hit_now 每行一条必须兑现的事。",
        _obj({"title": {"type": "string", "description": "章标题"},
              "purpose": {"type": "string", "description": "本章推进什么（一句话）"},
              "must_hit_now": {"type": "string", "description": "必须兑现的事，每行一条"},
              "forbidden_cross": {"type": "string", "description": "禁止触碰的事，每行一条"},
              "ending_state": {"type": "string", "description": "章末必须到达的状态"},
              "target_chars": {"type": "integer", "description": "本章目标字数"},
              "work_id": {"type": "string"}, "work_title": {"type": "string"}}),
        _tool_create_chapter(svc, ctx)))

    registry.register(_spec(
        "writing_set_contract",
        "改已有章节的合同。用户说「这章必须让 X 发生」「别提前揭示 Y」时调用。"
        "只传要改的项，未传的保持原样。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"},
              "purpose": {"type": "string"},
              "must_hit_now": {"type": "string", "description": "每行一条"},
              "forbidden_cross": {"type": "string", "description": "每行一条"},
              "ending_state": {"type": "string"},
              "next_entry_state": {"type": "string"},
              "target_chars": {"type": "integer"}}),
        _tool_set_contract(svc, ctx)))

    registry.register(_spec(
        "writing_read_chapter",
        "读某章正文与合同。要改稿、要接续上一章、要核对事实时先读。"
        "变更工具刚返回的 chapters 首尾预览已经够核对时，不要为了复查再读整章。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"}}),
        _tool_read_chapter(svc, ctx)))

    registry.register(_spec(
        "writing_draft_chapter",
        "装配上下文并写出一章正文，落库为草稿。需要本章已有合同。"
        "这是唯一会生成整章正文的工具；返回里只有首尾摘要，不要在回复中复述全文。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"},
              "extra_instruction": {"type": "string",
                                    "description": "作者本轮的额外要求，如「这一章节奏慢一点」"}}),
        _tool_draft_chapter(svc, ctx)))

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
        "从正文抽取本章产生了什么变化，写进账本（事实/承诺/摘要）。"
        "写完正文后调用；结算被拒时把原因告诉用户，不要自己改账本。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"}}),
        _tool_settle(svc, ctx)))

    registry.register(_spec(
        "writing_references",
        "给作品选参考书（别的书，不是本书正典）。action=list 先看书库有哪些书；"
        "action=set 用 titles 传书名（逗号分隔，最多 4 本），下次写稿会把它们的"
        "写法与细节蒸馏成素材卡；titles 留空即清空。参考书只供写法，不供设定。",
        _obj({"action": {"type": "string", "enum": ["list", "set"]},
              "titles": {"type": "string", "description": "书名，逗号分隔"},
              "mode": {"type": "string", "enum": ["card", "raw"],
                       "description": "card=蒸馏素材卡（默认，省注意力）；raw=原文摘录"},
              "work_id": {"type": "string"}}),
        _tool_references(svc, ctx)))

    registry.register(_spec(
        "writing_review_chapter",
        "审这一章：6 域 10 判据评分 + 带逐字证据的问题清单。只诊断，不改稿。",
        _obj({"chapter_index": {"type": "integer"},
              "work_id": {"type": "string"}}),
        _tool_review(svc, ctx)))

    registry.register(_spec(
        "writing_git",
        "写作 git 的查询操作（只有 selftest_seed / selftest_cleanup "
        "会新建或删除临时自测作品）。"
        "action=status 看工作区/分支/标签/暂存概览"
        "（system prompt 的版本现场已给分支/HEAD/clean/最近提交，"
        "需要 branches/tags 或完整改动列表时才查）；"
        "log 看历史（all_branches=true 看全部分支，chapter_index 只看某章）；"
        "diff 看两版或两分支差异（from_ref 支持 'main...岔路A'；"
        "返回目标侧改动章的 head/tail 预览，可直接核对差异方向）；"
        "reflog 找回切走前的位置；branches/tags/stash_list 列引用；"
        "selftest_plan 取写作 git 自测清单（清单长，带 phase=A/B/C/D "
        "只取当前阶段，或带 step=C4 只取一步）；"
        "selftest_seed 新建一个临时自测作品；"
        "selftest_cleanup 删除自测作品（只认「版本自测-」前缀）。"
        "作者说「看看版本」「这章改过几次」「岔路改了啥」时用它。",
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
              "limit": {"type": "integer"},
              "phase": {"type": "string",
                        "description": "selftest_plan 的阶段 A/B/C/D；只返回这一段"},
              "step": {"type": "string",
                       "description": "selftest_plan 的步骤 id，如 C4"}},
             ["action"]),
        _tool_git_read(svc, ctx)))

    registry.register(_spec(
        "writing_git_change",
        "写作 git 的变更操作。"
        "commit/amend 提交与修正；commit 带 chapters 时只提交这几章"
        "（其余章留在工作区，等价于 git 只 add 这几个文件）；"
        "branch_create/branch_switch/branch_rename/"
        "branch_delete 管分支（branch_create 带 switch=true 即建好就切）；"
        "merge 合分支；revert 撤销某次提交；cherry_pick 摘某次提交；"
        "restore 回到某一版；discard_chapter 只丢弃一章的未提交改动；"
        "tag_create/tag_delete 打里程碑；stash_push/stash_pop/stash_drop 暂存。"
        "merge/revert/cherry_pick/stash_pop 返回 needs_resolution=true 时，"
        "先把冲突章和两边差异讲给作者，再按作者选择带 resolutions 重新调用。"
        "改正文的变更（commit/amend/merge/revert/cherry_pick/restore/"
        "discard_chapter/stash_pop）成功后返回精简 status（分支/HEAD/clean/"
        "改动计数）和 chapters（本次改到的前 3 章：章号/字数/首尾预览）；"
        "能从这里核对就不要再查 status 或 read_chapter。commit/amend 返回 "
        "commit.id，merge/revert/restore 也带 commit；revert/restore 要的"
        "提交号优先用上一步返回的 commit.id / commit.parent_id，不要为了"
        "拿它再查 log。"
        "restore、branch_delete(force) 是重动作，作者明确要求才做。",
        _obj({"action": {"type": "string",
                         "enum": ["commit", "amend", "branch_create",
                                  "branch_switch", "branch_rename",
                                  "branch_delete", "merge", "revert",
                                  "cherry_pick", "restore",
                                  "discard_chapter", "tag_create",
                                  "tag_delete", "stash_push", "stash_pop",
                                  "stash_drop"]},
              "work_id": {"type": "string"},
              "work_title": {"type": "string"},
              "message": {"type": "string",
                          "description": "提交说明：写「改了什么」，不要写「更新」"},
              "name": {"type": "string",
                       "description": "分支/标签/暂存名；暂存也可传 stash@{0}、短号或完整 commit id"},
              "new_name": {"type": "string", "description": "分支改名后的名字"},
              "from_ref": {"type": "string",
                           "description": "branch_create/tag_create 的起点；默认 HEAD"},
              "commit_id": {"type": "string",
                            "description": "revert/cherry_pick/restore 的目标提交；"
                                           "完整 id 或 8 位短号均可，优先用上一步"
                                           "返回的 commit.id / parent_id"},
              "branch": {"type": "string", "description": "merge 要合进来的分支"},
              "chapter_index": {"type": "integer",
                                "description": "discard_chapter 的章号"},
              "chapters": {"type": "array", "items": {"type": "integer"},
                           "description": "commit 只提交这几章（章号）。"
                           "自己这轮只改了一部分章时用它，别把作者手改的章一起提交"},
              "force": {"type": "boolean",
                        "description": "branch_delete 强制删除未合并分支"},
              "switch": {"type": "boolean",
                         "description": "branch_create 后是否立刻切过去"},
              "checkpoint": {"type": "boolean",
                             "description": "切分支前是否自动提交，默认 true"},
              "resolutions": {"type": "object",
                              "description": "冲突裁决：{conflict.key: 'ours'|'theirs'|'base'}"}},
             ["action"]),
        _tool_git_change(svc, ctx)))

    registry.register(_spec(
        "writing_report",
        "记录一条功能问题或建议（不是聊天记录，是可检索、可关单的反馈）。"
        "自测或使用中发现：行为与说明不符、报错、缺少常用操作、体验问题，"
        "就用 action=add 记下来。title 一句话；body 写清哪一步、期望什么、"
        "实际什么、怎么复现。action=list 查看待处理项；resolve 关单。"
        "不要记录「写作内容好不好」——那是审稿工具的事。",
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
    return registry
