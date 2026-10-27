"""写作编排 —— 一条有事务边界的流水线（docs/writegraph/06-pipeline.md）。

两种入口（对话工具 / 工作台 HTTP）都落到这里，不各写一套。
判断全在 ``writegraph/`` 的纯函数里，本模块只负责「取数 → 拼 → 调 LLM →
落库」，以及把失败变成可读的状态。
"""

from __future__ import annotations

import logging
import re
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

from llm_json import loads_tolerant
from authorgraph.voice.select import eligible_traits, select_for_scene
from storage.authors_db import AuthorsDB, resolve_authors_db_path
from storage.writing_db import WritingDB
import usage_log
from writegraph import budget as budget_mod
from writegraph import card as card_mod
from writegraph import facts as facts_mod
from writegraph import lens as lens_mod
from writegraph import review as review_mod
from writegraph import state as state_mod
from writegraph import versioning as vcs
from writegraph import voice_card as voice_card_mod
from writegraph import voice_check as voice_check_mod
from writegraph import voice_lens as voice_lens_mod

log = logging.getLogger("writegraph")


class WritingError(RuntimeError):
    """可展示给用户的失败（缺合同、没配模型、结算被拒）。"""


#: 把参考书候选压成「可迁移的细节卡」。去专名、不总结剧情、不写指导语。
#: 只给观察与细节；写法/信息给法建议会和合同抢戏——基准里两处
#: 合同违规都来自这类建议。
REFERENCE_DISTILL = """你在为一位中文小说作者准备「可迁移的细节素材卡」。
输入是另一本书的片段与人物观察。你要把它们拆成这位作者能直接借用的
观察方式与细节，不能让他照搬原书，也不能让他照着写情节。

输出纯文本，不超过 520 字，固定两节：

【可借用的观察】2-3 条，只讲「看什么、怎么看得见」：视角落在哪个部位、
用哪个感官锚定、在哪个动作上停住。不写情节走向，不写人物关系判断，
不写「先抑后扬」这类叙事策略，不给结局或点题的比喻。

【可转化的细节】3-5 条，具体、可感的物象或动作，能放进另一个故事。

硬规则：
1. 不得出现原书的人名、地名、作品名；
2. 不得总结原书剧情，不得出现可被照搬的完整对白；
3. 不写「应该」「建议」这类指导语，直接写素材本身；
4. 找不到就少写，不要编。
"""

#: 必须排在最后：素材块也会给「只输出正文」的收尾指令，长度纪律要压过它。
LENGTH_PROMPT = """
## 长度纪律

目标 {target} 字，下限 {floor} 字。写到结尾时如果还差，
回到本章最关键的那个瞬间，补一段可感的动作、对白或物件细节；
不要用总结、抒情或重复来凑字数。
"""

#: 只在本次装配带外部参考素材时追加，避免每章都背一段用不上的说明。
MATERIAL_PROMPT = """
## 参考素材怎么用

- 素材只提供**写法与细节质感**，不是本书设定，也不是情节建议。
- 不得搬用其中的人名、地名、作品名或情节，也不要把人物关系搬过来。
- 至少把两条素材转化成**本文人物与场景**的具体动作或物件；不要照抄原句。
- 素材与本章无关时，忽略它。为了用而用，比不用更糟。
- 素材的优先级低于正典、合同与人物状态；合同已经定好「何时揭示、
  是否解释往事」的，一律照合同，素材不得改变信息给法。
- 不要用素材里的意象给本章点题或收尾；结尾落在此刻的动作或物件上。
- **素材是材料，不是任务。** 不要制定写作计划、步骤或提纲，
  也不要复述素材；只输出本章正文。
"""


#: 推理块。runtime 把 DeepSeek 的 reasoning_content 包成 <think>…</think>
#: 混在正文流里 —— 不剥掉的话，草稿和结算 JSON 里都会混进思维链。
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN = re.compile(r"<think>.*\Z", re.DOTALL)


def strip_think(text: str) -> str:
    """去掉推理块；未闭合的（截断）从 ``<think>`` 起全丢。"""
    text = _THINK_BLOCK.sub("", text or "")
    return _THINK_OPEN.sub("", text)


class ThinkStreamFilter:
    """流式剥推理块。标签可能跨 chunk，所以要留住可能是半个标签的尾巴。"""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self.pending = ""
        self.in_think = False

    def feed(self, chunk: str) -> str:
        self.pending += chunk or ""
        out: list[str] = []
        while self.pending:
            if self.in_think:
                idx = self.pending.find(self._CLOSE)
                if idx < 0:
                    keep = len(self._CLOSE) - 1
                    self.pending = self.pending[-keep:] if keep else ""
                    return "".join(out)
                self.pending = self.pending[idx + len(self._CLOSE):]
                self.in_think = False
                continue
            idx = self.pending.find(self._OPEN)
            if idx < 0:
                hold = 0
                for n in range(min(len(self._OPEN) - 1, len(self.pending)), 0, -1):
                    if self.pending.endswith(self._OPEN[:n]):
                        hold = n
                        break
                cut = len(self.pending) - hold
                out.append(self.pending[:cut])
                self.pending = self.pending[cut:]
                return "".join(out)
            out.append(self.pending[:idx])
            self.pending = self.pending[idx + len(self._OPEN):]
            self.in_think = True
        return "".join(out)

    def flush(self) -> str:
        if self.in_think:
            self.pending = ""
            return ""
        rest, self.pending = self.pending, ""
        return rest


class WritingService:
    def __init__(self, config, *, db: WritingDB | None = None,
                 api_client=None, recorder=None, recorder_session: str = ""):
        self.config = config
        self.db = db or WritingDB(config.writing_db_path)
        self.api_client = api_client
        # 飞行记录仪：写作的 4 类 LLM 调用不在 ReAct 循环里，
        # 不在这里显式记就完全看不见（提示词、思维链、响应全丢）。
        self.recorder = recorder
        self.recorder_session = recorder_session

    def with_scope(self, recorder=None, session_id: str = "") -> "WritingService":
        """请求级浅拷贝：共享 db / api_client，只换记录仪与会话标签。"""
        clone = WritingService.__new__(WritingService)
        clone.__dict__.update(self.__dict__)
        if recorder is not None:
            clone.recorder = recorder
        if session_id:
            clone.recorder_session = session_id
        return clone

    def _record_llm(self, *, kind: str, work_id: str, chapter_index: int,
                    system: str, user: str, response: str) -> None:
        """把一次写作 LLM 调用原样记进飞行记录仪（含思维链）。"""
        if self.recorder is None:
            return
        session_id = self.recorder_session or f"writing:{work_id or 'unknown'}"
        try:
            self.recorder.record_round(
                session_id=session_id,
                turn_index=int(chapter_index or 0),
                react_round=0,
                system_prompt=system,
                messages=[{"role": "user", "content": user}],
                tools_count=0,
                mode=f"writing.{kind}",
                book_id=work_id,
                response_text=response,
            )
        except Exception:                     # noqa: BLE001
            log.warning("writegraph: flight recorder failed", exc_info=True)

    # ── 作品与章节 ────────────────────────────────────────────────────────

    def list_works(self) -> list[dict[str, Any]]:
        return self.db.list_works()

    def create_work(self, title: str, *, genre: str = "", premise: str = "",
                    narrative_pov: str = "", target_chars: int = 0,
                    style_profile: Mapping[str, Any] | None = None) -> str:
        title = (title or "").strip()
        if not title:
            raise WritingError("作品名不能为空")
        return self.db.create_work(
            title, genre=genre, premise=premise,
            narrative_pov=narrative_pov, target_chars=target_chars,
            style_profile=style_profile)

    def work_detail(self, work_id: str) -> dict[str, Any]:
        work = self.db.get_work(work_id)
        if work is None:
            raise WritingError("作品不存在")
        chapters = self.db.list_chapters(work_id)
        # 左栏是「作品 → 卷 → 章 → 场景」的树。场景只带树需要的身份与
        # 状态，不把目标/冲突正文塞进列表接口；整本一次取，避免按章 N+1。
        by_chapter: dict[str, list[dict[str, Any]]] = {}
        for scene in self.db.list_scenes_for_work(work_id):
            by_chapter.setdefault(str(scene["chapter_id"]), []).append({
                "id": scene["id"],
                "scene_index": scene["scene_index"],
                "title": scene.get("title") or "",
                "status": scene.get("status") or "planned",
            })
        for chapter in chapters:
            chapter["scenes"] = by_chapter.get(str(chapter["id"]), [])
        return {"work": work, "chapters": chapters,
                "volumes": self.db.list_volumes(work_id)}

    def rename_work(self, work_id: str, title: str) -> dict[str, Any]:
        """改书名。空名拒绝——书名是作品在列表与提示词里的身份。"""
        title = (title or "").strip()
        if not title:
            raise WritingError("作品名不能为空")
        if self.db.get_work(work_id) is None:
            raise WritingError("作品不存在")
        self.db.update_work_title(work_id, title)
        return {"id": work_id, "title": title}

    def set_work_pinned(self, work_id: str, pinned: bool) -> dict[str, Any]:
        """写作台置顶。排序偏好，不写进写作 git。"""
        self._work_or_raise(work_id)
        self.db.set_work_pinned(work_id, bool(pinned))
        return {"id": work_id, "pinned": bool(pinned)}

    def delete_work(self, work_id: str, *,
                    confirm_title: str = "") -> dict[str, Any]:
        """删整本作品。书名是确认凭据，防止误删/裸调接口。

        正文版本、场景、提交、分支引用都由外键级联删除；删完不可恢复。
        """
        work = self.db.get_work(work_id)
        if work is None:
            raise WritingError("作品不存在")
        title = str(work.get("title") or "")
        if (confirm_title or "").strip() != title:
            raise WritingError("删除确认不匹配：请传作品名")
        self.db.delete_work(work_id)
        return {"deleted": work_id, "title": title}

    def rename_chapter(self, work_id: str, chapter_index: int,
                       title: str) -> dict[str, Any]:
        """改章标题。空串合法（= 未命名），只是展示层给个占位。"""
        chapter = self.chapter(work_id, chapter_index)
        title = (title or "").strip()
        self.db.update_chapter_title(chapter["id"], title)
        return {"chapter_index": chapter_index, "title": title}

    def create_chapter(self, work_id: str, *, title: str = "",
                       contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
        work = self.db.get_work(work_id)
        if work is None:
            raise WritingError("作品不存在")
        chapters = self.db.list_chapters(work_id)
        index = (chapters[-1]["chapter_index"] + 1) if chapters else 1
        chapter_id = self.db.add_chapter(work_id, index, title=title)
        if contract is not None:
            self.db.set_contract(chapter_id, **dict(contract))
        return self.db.get_chapter_by_id(chapter_id) or {}

    def chapter(self, work_id: str, chapter_index: int) -> dict[str, Any]:
        chapter = self.db.get_chapter(work_id, chapter_index)
        if chapter is None:
            raise WritingError(f"第{chapter_index}章不存在")
        return chapter

    def contract(self, chapter_id: str) -> dict[str, Any] | None:
        return self.db.get_contract(chapter_id)

    def set_contract(self, chapter_id: str, **fields: Any) -> dict[str, Any]:
        try:
            self.db.set_contract(chapter_id, **fields)
        except (ValueError, TypeError) as exc:
            raise WritingError(str(exc)) from exc
        return self.db.get_contract(chapter_id) or {}

    #: 这些来源只算「暂存稿」，不算保存点。
    DRAFT_REASONS = frozenset({"autosave", "flush", "park"})

    def save_content(self, work_id: str, chapter_index: int, content: str, *,
                     reason: str = "manual", title: str | None = None,
                     kind: str | None = None) -> dict[str, Any]:
        chapter = self.chapter(work_id, chapter_index)
        if title is not None and title != chapter.get("title"):
            self.db.update_chapter_title(chapter["id"], title)
        if kind is None:
            kind = "draft" if reason in self.DRAFT_REASONS else "save"
        return self.db.save_draft(work_id, chapter["id"], content,
                                  reason=reason, kind=kind)

    def save_point(self, chapter: Mapping[str, Any]) -> dict[str, Any]:
        """最近一个保存点的摘要（不含正文），给「有没有只暂存过的改动」。"""
        row = self.db.latest_saved_version(str(chapter.get("id") or ""))
        if row is None:
            return {}
        return {"version": int(row.get("version") or 0),
                "content_hash": row.get("content_hash") or "",
                "time_created": int(row.get("time_created") or 0)}

    def saved_content(self, work_id: str,
                      chapter_index: int) -> dict[str, Any]:
        """「回到上次保存」用的正文：最近一个保存点。"""
        chapter = self.chapter(work_id, chapter_index)
        row = self.db.latest_saved_version(str(chapter["id"]))
        if row is None:
            raise WritingError("这一章还没有保存过")
        return {"chapter_index": chapter_index,
                "version": int(row.get("version") or 0),
                "content": row.get("content") or "",
                "content_hash": row.get("content_hash") or "",
                "time_created": int(row.get("time_created") or 0)}

    # ── 写作 git（项目级提交 + 分支）─────────────────────────────────────

    _BRANCH_RE = re.compile(r"^[\w一-鿿][\w一-鿿-]{0,39}$")
    _TAG_RE = re.compile(r"^[\w一-鿿][\w一-鿿.\-]{0,39}$")

    def _work_or_raise(self, work_id: str) -> dict[str, Any]:
        work = self.db.get_work(work_id)
        if work is None:
            raise WritingError("作品不存在")
        return work

    def _resolve_commit(self, work_id: str, ref: str) -> dict[str, Any] | None:
        """ref：``''``/``EMPTY`` 空树、``HEAD``、分支名、完整或短 commit id。"""
        ref = (ref or "").strip()
        if ref in ("", "EMPTY"):
            return None
        if ref == "HEAD":
            head = self.db.get_head(work_id)
            return (self.db.get_commit(head["commit_id"])
                    if head.get("commit_id") else None)
        row = self.db.get_ref(work_id, ref)
        if row:
            return self.db.get_commit(row["commit_id"])
        commit = self.db.get_commit(ref)
        if commit and commit.get("work_id") == work_id:
            return commit
        for c in self.db.list_commits(work_id, limit=5000):
            if c["id"].startswith(ref) or vcs.short_id(c["id"]) == ref:
                return c
        raise WritingError(f"找不到提交或分支：{ref}")

    def _commit_ref_or_raise(self, work_id: str,
                             ref: str) -> dict[str, Any]:
        """工具层常拿到短号（日志里就显示 8 位），这里统一解析。"""
        try:
            commit = self._resolve_commit(work_id, ref)
        except WritingError as exc:
            raise WritingError("提交不存在") from exc
        if commit is None or commit.get("work_id") != work_id:
            raise WritingError("提交不存在")
        return commit

    def _resolve_state(self, work_id: str, ref: str) -> dict[str, Any]:
        commit = self._resolve_commit(work_id, ref)
        if commit is None:
            return {"tree": [], "ledger": {}, "commit": None}
        try:
            tree = json.loads(commit.get("tree_json") or "[]")
            ledger = json.loads(commit.get("ledger_json") or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise WritingError(f"提交内容损坏：{commit.get('id')}") from exc
        return {"tree": tree, "ledger": ledger, "commit": commit}

    def _current_state(self, work_id: str) -> dict[str, Any]:
        return {"tree": self.db.current_tree(work_id),
                "ledger": self.db.snapshot_ledger(work_id), "commit": None}

    @staticmethod
    def _commit_brief(commit: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not commit:
            return None
        return {
            "id": commit.get("id"), "short": vcs.short_id(commit.get("id") or ""),
            "parent_id": commit.get("parent_id") or "",
            "parent2_id": commit.get("parent2_id") or "",
            "branch": commit.get("branch_name"),
            "kind": commit.get("kind"), "author": commit.get("author"),
            "message": commit.get("message") or "",
            "chapter_index": commit.get("chapter_index") or 0,
            "time_created": commit.get("time_created") or 0,
            "tree_hash": commit.get("tree_hash") or "",
            "ledger_hash": commit.get("ledger_hash") or "",
        }

    # ── 图查询 ────────────────────────────────────────────────────────────

    def _commit_map(self, work_id: str, limit: int = 5000
                    ) -> dict[str, dict[str, Any]]:
        return {c["id"]: c for c in self.db.list_commits(work_id,
                                                         limit=limit)}

    def _ref_decorations(self, work_id: str) -> dict[str, list[dict[str, str]]]:
        """提交 id → 指向它的分支/标签（暂存不装饰，它在单独一栏）。"""
        out: dict[str, list[dict[str, str]]] = {}
        for row in self.db.list_refs(work_id):
            if row.get("kind") == "stash":
                continue
            out.setdefault(str(row.get("commit_id")), []).append(
                {"name": str(row.get("name")), "kind": str(row.get("kind"))})
        return out

    def _reachable_ids(self, work_id: str,
                       commits: Mapping[str, Mapping[str, Any]]) -> set[str]:
        """从所有引用与 HEAD 出发可达的提交。悬空的旧提交不进历史。"""
        reach: set[str] = set()
        for row in self.db.list_refs(work_id):
            reach |= vcs.ancestor_ids(commits, str(row.get("commit_id") or ""))
        head = self.db.get_head(work_id)
        reach |= vcs.ancestor_ids(commits, head.get("commit_id"))
        return reach

    @staticmethod
    def _tree_map(commit: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not commit:
            return {}
        try:
            entries = json.loads(commit.get("tree_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            return {}
        return {str(e.get("chapter_id")): dict(e) for e in entries}

    # ── 变更后的「核对视图」 ────────────────────────────────────────────
    # agent 查一章正文要花一整轮 ReAct（整章重发），而它多数时候只想确认
    # 「刚才那处改动落没落上」。这里给首尾摘录 + 字数 + hash，短章足够核对；
    # 要看全文仍然走 writing_read_chapter，不改变那个工具的语义。

    @staticmethod
    def _preview_row(chapter: Mapping[str, Any],
                     size: int = 120) -> dict[str, Any]:
        content = str(chapter.get("content") or "")
        return {
            "chapter_index": chapter.get("chapter_index"),
            "title": chapter.get("title") or "",
            "word_count": chapter.get("word_count", 0),
            "content_hash": chapter.get("content_hash") or "",
            "head": content[:size],
            "tail": content[-size:] if len(content) > size else "",
        }

    def chapter_preview(self, work_id: str, chapter_id: str,
                        *, size: int = 120) -> dict[str, Any] | None:
        chapter = self.db.get_chapter_by_id(chapter_id)
        if chapter is None or chapter.get("work_id") != work_id:
            return None
        return self._preview_row(chapter, size)

    def commit_chapter_previews(self, work_id: str, commit_id: str, *,
                                limit: int = 3,
                                size: int = 120) -> list[dict[str, Any]]:
        """这条提交改到的章（相对第一父节点），带首尾摘录。"""
        commit = self.db.get_commit(str(commit_id or ""))
        if commit is None or commit.get("work_id") != work_id:
            return []
        parent = (self.db.get_commit(str(commit.get("parent_id") or ""))
                  if commit.get("parent_id") else None)
        before = self._tree_map(parent)
        after = self._tree_map(commit)
        out: list[dict[str, Any]] = []
        for cid, entry in after.items():
            old = before.get(cid)
            if old is not None and vcs.entry_key(old) == vcs.entry_key(entry):
                continue
            chapter = self.db.get_chapter_by_id(cid)
            if chapter is None:
                continue
            out.append(self._preview_row(chapter, size))
            if len(out) >= limit:
                break
        return out

    def vcs_status(self, work_id: str) -> dict[str, Any]:
        """工作区 vs HEAD：改了哪些章、账本是否脏、各分支差几个提交。"""
        self._work_or_raise(work_id)
        head = self.db.get_head(work_id)
        commit = (self.db.get_commit(head["commit_id"])
                  if head.get("commit_id") else None)
        current = self._current_state(work_id)
        base = self._resolve_state(work_id, "HEAD")
        changes = vcs.diff_trees(base["tree"], current["tree"])
        head_ledger_hash = (commit.get("ledger_hash") if commit
                            else vcs.ledger_hash({}))
        ledger_dirty = vcs.ledger_hash(current["ledger"]) != head_ledger_hash
        clean = not (changes["added"] or changes["removed"]
                     or changes["changed"] or ledger_dirty)

        commits = self._commit_map(work_id)
        here = head.get("commit_id")
        branches = []
        for row in self.db.list_branches(work_id):
            ahead, behind = vcs.ahead_behind(commits, here, row.get("commit_id"))
            base_id = vcs.merge_base(commits, here, row.get("commit_id"))
            branches.append({**row,
                             "is_head": row.get("commit_id") == here,
                             "ahead": ahead, "behind": behind,
                             "base_id": base_id or "",
                             "base_short": vcs.short_id(base_id or "")})
        tags = self.db.list_refs(work_id, kind="tag")
        stashes = self.db.list_refs(work_id, kind="stash")
        return {
            "branch": head.get("branch_name") or "main",
            "head": self._commit_brief(commit),
            "branches": branches,
            "tags": tags,
            "stash_count": len(stashes),
            "changes": changes,
            "counts": {k: len(v) for k, v in changes.items()},
            "ledger_dirty": ledger_dirty,
            "clean": clean,
        }

    def vcs_log(self, work_id: str, *, branch: str = "",
                all_branches: bool = False, chapter_id: str = "",
                mark_chapter: bool = False,
                limit: int = 50) -> list[dict[str, Any]]:
        """历史。默认只看当前分支；``all_branches`` 看全部分支的并集。

        ``chapter_id`` 默认是过滤器：只留改过这一章的提交。``mark_chapter``
        改成标注——所有提交都留下，挨个带 ``touches`` 真假。版本图要的是完整
        的父子链，删行会把泳道画断，所以图谱模式走标注而不是过滤。

        悬空提交（amend 掉的旧版本、已删分支上的提交）不出现在这里——
        它们只从 reflog 找。
        """
        self._work_or_raise(work_id)
        head = self.db.get_head(work_id)
        name = (head.get("branch_name") or "main") if branch.strip() in (
            "", "*") else branch.strip()
        commits = self._commit_map(work_id)
        if all_branches:
            reachable = self._reachable_ids(work_id, commits)
        else:
            ref = self.db.get_ref(work_id, name)
            tip = (ref or {}).get("commit_id")
            if tip is None and name == (head.get("branch_name") or "main"):
                tip = head.get("commit_id")
            reachable = vcs.ancestor_ids(commits, tip)
        rows = sorted((commits[cid] for cid in reachable
                       if cid in commits
                       and commits[cid].get("kind") != "stash"),
                      key=lambda c: (int(c.get("time_created") or 0),
                                     str(c.get("id"))),
                      reverse=True)[:limit]
        refs = self._ref_decorations(work_id)
        touches = None
        if chapter_id and mark_chapter:
            touches = self._chapter_touch_map(rows, commits, chapter_id)
        out = []
        for c in rows:
            if chapter_id and not mark_chapter:
                if not self._commit_touches(c, chapter_id, commits):
                    continue
            item = self._commit_brief(c) or {}
            item["refs"] = refs.get(c["id"], [])
            item["is_head"] = c["id"] == head.get("commit_id")
            if touches is not None:
                item["touches"] = touches.get(c["id"], False)
            out.append(item)
        return out

    def _chapter_touch_map(self, rows: Sequence[Mapping[str, Any]],
                           commits: Mapping[str, Mapping[str, Any]],
                           chapter_id: str) -> dict[str, bool]:
        """整页一次算完「这条提交动没动这一章」。

        每棵树只解析一遍：单章历史原来是一条一条 ``_commit_touches``，
        提交一多就把同一份 tree_json 反复 json.loads。
        """
        cache: dict[str, dict[str, Any]] = {}

        def tree(cid: str) -> dict[str, Any]:
            if cid not in cache:
                commit = commits.get(cid)
                cache[cid] = self._tree_map(commit) if commit else {}
            return cache[cid]

        out: dict[str, bool] = {}
        for c in rows:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            now = vcs.entry_key(tree(cid).get(str(chapter_id)))
            old = vcs.entry_key(
                tree(str(c.get("parent_id") or "")).get(str(chapter_id)))
            out[cid] = now != old
        return out

    def _commit_touches(self, commit: Mapping[str, Any], chapter_id: str,
                        commits: Mapping[str, Mapping[str, Any]]) -> bool:
        """某章在这条提交里是否变了（相对第一父节点）。单章历史用。"""
        now_key = vcs.entry_key(self._tree_map(commit).get(str(chapter_id)))
        parent = commits.get(str(commit.get("parent_id") or ""))
        old_key = vcs.entry_key(self._tree_map(parent).get(str(chapter_id)))
        return now_key != old_key

    @staticmethod
    def _max_index(tree: Sequence[Mapping[str, Any]]) -> int:
        return max((int(c.get("chapter_index") or 0) for c in tree), default=0)

    def vcs_commit(self, work_id: str, message: str, *,
                   author: str = "user", kind: str = "manual",
                   allow_empty: bool = False,
                   chapter_indexes: Sequence[int] | None = None
                   ) -> dict[str, Any]:
        """提交工作区。

        ``chapter_indexes`` 给了就只提交这几章（其余章沿用 HEAD 的版本指针），
        等于 git 里「只 add 这几个文件」——作者手改的章和 agent 这次的改动
        可以分成两次提交，不混在一起。账本是整本书一份，无法按章切分，
        所以账本改动始终随这次提交一起走。
        """
        self._work_or_raise(work_id)
        status = self.vcs_status(work_id)
        if status["clean"] and not allow_empty:
            raise WritingError("没有需要提交的改动")
        head = self.db.get_head(work_id)
        current = self._current_state(work_id)
        tree = current["tree"]
        base = self._resolve_state(work_id, "HEAD")
        wanted = sorted({int(i) for i in (chapter_indexes or [])})
        if wanted:
            known = {int(c.get("chapter_index") or 0) for c in tree}
            missing = [i for i in wanted if i not in known]
            if missing:
                raise WritingError(
                    "章不存在：" + "、".join(str(i) for i in missing))
            keep = set(wanted)
            base_map = {str(e.get("chapter_id")): e for e in base["tree"]}
            tree = [e if int(e.get("chapter_index") or 0) in keep
                    else base_map.get(str(e.get("chapter_id")), e)
                    for e in tree]
            picked = vcs.diff_trees(base["tree"], tree)
            if not (picked["added"] or picked["removed"] or picked["changed"]):
                raise WritingError("选中的章节没有改动")
        branch = head.get("branch_name") or "main"
        commit = self.db.create_commit(
            work_id, tree=tree, ledger=current["ledger"],
            parent_id=head.get("commit_id"), branch_name=branch,
            kind=kind, author=author,
            message=(message or "").strip() or "未命名提交",
            chapter_index=self._max_index(tree))
        return self._commit_brief(commit) or {}

    def vcs_amend(self, work_id: str, message: str = "") -> dict[str, Any]:
        """修正最近一次提交（``commit --amend``）：换说明或把漏改的带上。

        旧提交不删、不改，只是不再被分支引用——和 git 一样从 reflog 还能找到。
        """
        self._work_or_raise(work_id)
        head = self.db.get_head(work_id)
        commit = (self.db.get_commit(head["commit_id"])
                  if head.get("commit_id") else None)
        if commit is None:
            raise WritingError("还没有提交可以修正")
        current = self._current_state(work_id)
        new_message = ((message or "").strip()
                       or str(commit.get("message") or ""))
        branch = head.get("branch_name") or "main"
        amended = self.db.create_commit(
            work_id, tree=current["tree"], ledger=current["ledger"],
            parent_id=commit.get("parent_id") or None,
            parent2_id=commit.get("parent2_id") or None,
            branch_name=branch, kind="amend", author="user",
            message=new_message,
            chapter_index=self._max_index(current["tree"]),
            log_from=commit["id"])
        return self._commit_brief(amended) or {}

    def _auto_commit(self, work_id: str, message: str, *,
                     kind: str = "auto", author: str = "agent"
                     ) -> dict[str, Any] | None:
        """只在工作区确实有改动时提交；干净的检查点不产生空提交。"""
        if self.vcs_status(work_id)["clean"]:
            return None
        return self.vcs_commit(work_id, message, author=author, kind=kind)

    def vcs_branches(self, work_id: str) -> list[dict[str, Any]]:
        self._work_or_raise(work_id)
        return self.db.list_branches(work_id)

    def _check_branch_name(self, name: str, *, work_id: str) -> str:
        name = (name or "").strip()
        if (not self._BRANCH_RE.match(name)
                or name in ("HEAD", "WORK", "EMPTY")):
            raise WritingError("分支名只能用中英文、数字、下划线、连字符（1-40 字）")
        if self.db.get_ref(work_id, name):
            raise WritingError(f"分支已存在：{name}")
        return name

    def vcs_create_branch(self, work_id: str, name: str, *,
                          from_ref: str = "HEAD") -> dict[str, Any]:
        self._work_or_raise(work_id)
        name = self._check_branch_name(name, work_id=work_id)
        target = self._resolve_commit(work_id, from_ref)
        if target is None:
            raise WritingError("还没有可以分支的提交")
        self.db.upsert_ref(work_id, name, target["id"])
        return {"name": name, "commit_id": target["id"],
                "short": vcs.short_id(target["id"])}

    def vcs_switch(self, work_id: str, name: str, *,
                   create: bool = False, from_ref: str = "HEAD",
                   checkpoint: bool = True) -> dict[str, Any]:
        """``switch``：``create=True`` 即 ``switch -c``（建好立刻跳过去）。"""
        if create:
            self.vcs_create_branch(work_id, name, from_ref=from_ref)
        return self.vcs_checkout(work_id, name, checkpoint=checkpoint)

    def vcs_checkout(self, work_id: str, branch: str, *,
                     checkpoint: bool = True) -> dict[str, Any]:
        self._work_or_raise(work_id)
        ref = self.db.get_ref(work_id, branch)
        if ref is None:
            raise WritingError(f"分支不存在：{branch}")
        if ref.get("kind") != "branch":
            raise WritingError(f"{branch} 不是分支")
        target = self.db.get_commit(ref["commit_id"])
        if target is None:
            raise WritingError("分支指向的提交不存在")
        head = self.db.get_head(work_id)
        current_branch = head.get("branch_name") or "main"
        if head.get("commit_id") != target["id"]:
            if not self.vcs_status(work_id)["clean"]:
                if not checkpoint:
                    raise WritingError("工作区有未提交的改动，先提交或允许自动提交")
                self.vcs_commit(
                    work_id,
                    f"切换分支前自动提交（{current_branch}）",
                    author="system", kind="checkpoint")
                head = self.db.get_head(work_id)
            self._apply_state(work_id, target, reason="branch")
        # 即使两个分支指向同一个提交，所在分支也必须换过去。
        before = head.get("commit_id")
        self.db.set_head(work_id, branch, target["id"])
        if before != target["id"] or current_branch != branch:
            self.db.log_head(work_id, branch_name=branch,
                             commit_id=target["id"], from_commit_id=before,
                             reason="checkout")
        return self.vcs_status(work_id)

    def vcs_rename_branch(self, work_id: str, old: str,
                          new: str) -> dict[str, Any]:
        self._work_or_raise(work_id)
        ref = self.db.get_ref(work_id, old)
        if ref is None or ref.get("kind") != "branch":
            raise WritingError(f"分支不存在：{old}")
        new = self._check_branch_name(new, work_id=work_id)
        self.db.rename_ref(work_id, old, new)
        head = self.db.get_head(work_id)
        return {"name": new, "old": old,
                "branch": head.get("branch_name") or "main",
                "commit_id": ref.get("commit_id")}

    def vcs_delete_branch(self, work_id: str, name: str, *,
                          force: bool = False) -> dict[str, Any]:
        """``branch -d``：未合并的分支先拦一下，``force`` 才真删。"""
        self._work_or_raise(work_id)
        ref = self.db.get_ref(work_id, name)
        if ref is None or ref.get("kind") != "branch":
            raise WritingError(f"分支不存在：{name}")
        head = self.db.get_head(work_id)
        if (head.get("branch_name") or "main") == name:
            raise WritingError("不能删除当前所在的分支")
        if not force:
            commits = self._commit_map(work_id)
            merged = vcs.ancestor_ids(commits, head.get("commit_id"))
            if str(ref.get("commit_id")) not in merged:
                raise WritingError(
                    f"{name} 上还有没合并的提交；确认放弃请用强制删除")
        # 切换分支时的自动检查点只挂在这个分支上：force 删掉会让「切走前的
        # 未提交改动」失去唯一引用，而作者以为它还在。删除前先留一个
        # lost-* 标签，至少还能从标签恢复（自测 fb-d5416a5c7fa44c72）。
        tip_id = str(ref.get("commit_id") or "")
        tip = self.db.get_commit(tip_id) if tip_id else None
        recovered = ""
        if tip and str(tip.get("kind") or "") == "checkpoint":
            commits = self._commit_map(work_id)
            reachable = False
            for other in self.db.list_refs(work_id):
                if str(other.get("name")) == name:
                    continue
                oid = str(other.get("commit_id") or "")
                if oid and tip_id in vcs.ancestor_ids(commits, oid):
                    reachable = True
                    break
            if not reachable:
                base = f"lost-{name}"[:36]
                tag = base
                n = 2
                while self.db.get_ref(work_id, tag):
                    tag = f"{base}-{n}"
                    n += 1
                self.db.upsert_ref(work_id, tag, tip_id, kind="tag")
                recovered = tag
        self.db.delete_ref(work_id, name)
        out = {"deleted": name, "commit_id": ref.get("commit_id")}
        if recovered:
            out["recovered_tag"] = recovered
            out["hint"] = (
                f"{name} 的 tip 是切换分支时的自动检查点；已保留标签 "
                f"{recovered}，确认不要可 tag_delete 删除")
        return out

    # ── 标签：定稿里程碑 ──────────────────────────────────────────────────

    def vcs_tags(self, work_id: str) -> list[dict[str, Any]]:
        self._work_or_raise(work_id)
        out = []
        for row in self.db.list_refs(work_id, kind="tag"):
            out.append({**row, "short": vcs.short_id(str(row.get("commit_id") or ""))})
        return out

    def vcs_create_tag(self, work_id: str, name: str, *,
                       ref: str = "HEAD") -> dict[str, Any]:
        self._work_or_raise(work_id)
        name = (name or "").strip()
        if not self._TAG_RE.match(name):
            raise WritingError("标签名只能用中英文、数字、点、下划线、连字符（1-40 字）")
        if self.db.get_ref(work_id, name):
            raise WritingError(f"标签或分支已存在：{name}")
        target = self._resolve_commit(work_id, ref or "HEAD")
        if target is None:
            raise WritingError("还没有可以打标签的提交")
        self.db.upsert_ref(work_id, name, target["id"], kind="tag")
        return {"name": name, "commit_id": target["id"],
                "short": vcs.short_id(target["id"])}

    def vcs_delete_tag(self, work_id: str, name: str) -> dict[str, Any]:
        self._work_or_raise(work_id)
        ref = self.db.get_ref(work_id, name)
        if ref is None or ref.get("kind") != "tag":
            raise WritingError(f"标签不存在：{name}")
        self.db.delete_ref(work_id, name)
        return {"deleted": name}

    # ── 暂存：收起手头的改动 ──────────────────────────────────────────────

    def _stash_rows(self, work_id: str) -> list[dict[str, Any]]:
        rows = self.db.list_refs(work_id, kind="stash")
        rows.sort(key=lambda r: int(r.get("time_updated") or 0), reverse=True)
        out = []
        for idx, row in enumerate(rows):
            out.append({**row, "index": idx, "label": f"stash@{{{idx}}}",
                        "short": vcs.short_id(str(row.get("commit_id") or ""))})
        return out

    def vcs_stashes(self, work_id: str) -> list[dict[str, Any]]:
        self._work_or_raise(work_id)
        return self._stash_rows(work_id)

    def _resolve_stash_ref(self, work_id: str, ref: str) -> dict[str, Any]:
        """暂存引用：``stash@{0}`` / ``stash/cm-…`` / 完整或短 commit id。

        agent 看到的是 ``stash@{0}`` 和 8 位短号，工具不能只认内部 ref 名。
        """
        ref = (ref or "").strip()
        row = self.db.get_ref(work_id, ref)
        if row is not None and row.get("kind") == "stash":
            return row
        match = re.fullmatch(r"stash@\{(\d+)\}", ref)
        if match:
            rows = self._stash_rows(work_id)
            index = int(match.group(1))
            if 0 <= index < len(rows):
                found = self.db.get_ref(work_id, str(rows[index]["name"]))
                if found is not None:
                    return found
        try:
            commit = self._resolve_commit(work_id, ref)
        except WritingError:
            commit = None
        if commit is not None:
            found = self.db.get_ref(work_id, f"stash/{commit['id']}")
            if found is not None and found.get("kind") == "stash":
                return found
        raise WritingError("暂存不存在")

    def vcs_stash_push(self, work_id: str, message: str = ""
                       ) -> dict[str, Any]:
        """把当前改动收进一条 stash 快照，工作区回到 HEAD。"""
        self._work_or_raise(work_id)
        head = self.db.get_head(work_id)
        if not head.get("commit_id"):
            raise WritingError("先提交一次，才能暂存改动")
        if self.vcs_status(work_id)["clean"]:
            raise WritingError("工作区没有改动")
        current = self._current_state(work_id)
        branch = head.get("branch_name") or "main"
        snapshot = self.db.create_commit(
            work_id, tree=current["tree"], ledger=current["ledger"],
            parent_id=head.get("commit_id"), branch_name=branch,
            kind="stash", author="user",
            message=(message or "").strip() or "暂存",
            chapter_index=self._max_index(current["tree"]),
            move_head=False)
        self.db.upsert_ref(work_id, f"stash/{snapshot['id']}",
                           snapshot["id"], kind="stash")
        base = self.db.get_commit(head["commit_id"])
        if base is not None:
            self._apply_state(work_id, base, reason="stash")
        return {"commit": self._commit_brief(snapshot) or {},
                "status": self.vcs_status(work_id)}

    def vcs_stash_pop(self, work_id: str, name: str,
                      resolutions: Mapping[str, Any] | None = None
                      ) -> dict[str, Any]:
        """恢复一条 stash。

        只重放「暂存后发生的那部分改动」（stash 的父状态 → stash），不是
        整棵树覆盖——否则暂存之后提交的别的章会被悄悄退回旧稿。
        """
        self._work_or_raise(work_id)
        ref = self._resolve_stash_ref(work_id, name)
        if not self.vcs_status(work_id)["clean"]:
            raise WritingError("工作区有未提交的改动，先提交或丢弃再恢复暂存")
        snapshot = self.db.get_commit(ref["commit_id"])
        if snapshot is None:
            raise WritingError("暂存内容已损坏")
        parent_id = snapshot.get("parent_id")
        if not parent_id:
            raise WritingError("暂存缺少父状态，无法安全恢复")
        result = self._three_way(
            work_id, base_ref=parent_id, theirs_ref=snapshot["id"],
            ours_ref="HEAD", resolutions=resolutions or {},
            reason="stash-pop")
        if not result["applied"]:
            return {"conflicts": result["conflicts"],
                    "base": result["base"], "ours": result["ours"],
                    "theirs": result["theirs"]}
        # name 可能传的是 stash@{0} / 短号；真正要删的是解析后的 ref。
        self.db.delete_ref(work_id, str(ref["name"]))
        return {"restored": self._commit_brief(snapshot) or {},
                "status": self.vcs_status(work_id)}

    def vcs_stash_drop(self, work_id: str, name: str) -> dict[str, Any]:
        self._work_or_raise(work_id)
        ref = self._resolve_stash_ref(work_id, name)
        self.db.delete_ref(work_id, str(ref["name"]))
        return {"dropped": ref["name"]}

    # ── 丢弃：只回退一章的未提交改动 ─────────────────────────────────────

    def vcs_discard_chapter(self, work_id: str,
                            chapter_id: str) -> dict[str, Any]:
        """``checkout -- <章>``：这一章回到 HEAD，别的章与账本不动。"""
        self._work_or_raise(work_id)
        state = self._resolve_state(work_id, "HEAD")
        entry = next((e for e in state["tree"]
                      if str(e.get("chapter_id")) == str(chapter_id)), None)
        if entry is None:
            raise WritingError("这一章还不在上次提交里，先把整篇提交或删除")
        chapter = self.db.get_chapter_by_id(chapter_id)
        if chapter is None or chapter.get("work_id") != work_id:
            raise WritingError("章节不存在")
        content = self._version_content(entry.get("version_id"))
        same = ((chapter.get("content") or "") == content
                and chapter.get("title") == (entry.get("title") or "")
                and chapter.get("status") == (entry.get("status") or "planned"))
        if same:
            raise WritingError("这一章没有可丢弃的改动")
        self.db.save_draft(work_id, chapter_id, content, reason="discard")
        self.db.set_chapter_meta(
            chapter_id, title=entry.get("title") or "",
            status=entry.get("status") or "planned",
            volume_id=entry.get("volume_id") if "volume_id" in entry
            else chapter.get("volume_id"))
        self.db.delete_summary(chapter_id)
        return {"status": self.vcs_status(work_id)}

    # ── reflog / 合并基 ──────────────────────────────────────────────────

    def vcs_reflog(self, work_id: str, limit: int = 50) -> list[dict[str, Any]]:
        self._work_or_raise(work_id)
        out = []
        for row in self.db.list_head_log(work_id, limit=limit):
            commit = (self.db.get_commit(row["commit_id"])
                      if row.get("commit_id") else None)
            before = (self.db.get_commit(row["from_commit_id"])
                      if row.get("from_commit_id") else None)
            out.append({
                "id": row["id"], "branch": row.get("branch_name") or "",
                "reason": row.get("reason") or "",
                "commit_id": row.get("commit_id") or "",
                "commit_short": vcs.short_id(row.get("commit_id") or ""),
                "commit_message": (commit or {}).get("message") or "",
                "from_id": row.get("from_commit_id") or "",
                "from_short": vcs.short_id(row.get("from_commit_id") or ""),
                "from_message": (before or {}).get("message") or "",
                "time_created": row.get("time_created") or 0,
            })
        return out

    def vcs_merge_base(self, work_id: str, a: str, b: str
                       ) -> dict[str, Any]:
        self._work_or_raise(work_id)
        commits = self._commit_map(work_id)
        left = self._resolve_commit(work_id, a)
        right = self._resolve_commit(work_id, b)
        base_id = vcs.merge_base(
            commits, (left or {}).get("id"), (right or {}).get("id"))
        ahead, behind = vcs.ahead_behind(
            commits, (left or {}).get("id"), (right or {}).get("id"))
        return {"base": self._commit_brief(commits.get(base_id or "")),
                "ahead": ahead, "behind": behind,
                "from": self._commit_brief(left),
                "to": self._commit_brief(right)}

    def _apply_state(self, work_id: str, commit: Mapping[str, Any], *,
                     reason: str) -> dict[str, Any]:
        """把某个提交的正文与账本落到工作区。只覆盖提交里存在的章。"""
        target = self._resolve_state(work_id, str(commit.get("id")))
        chapters = {c["id"]: c for c in self.db.list_chapters(work_id)}
        for entry in target["tree"]:
            chapter = chapters.get(entry.get("chapter_id"))
            if chapter is None:
                continue
            version = self.db.get_version(str(entry.get("version_id") or ""))
            content = (version or {}).get("content") or ""
            if (chapter.get("content") or "") != content:
                self.db.save_draft(work_id, chapter["id"], content,
                                   reason=reason)
                # 摘要由正文派生；正文换了，旧摘要不再可信。
                self.db.delete_summary(chapter["id"])
            volume_id = (entry["volume_id"] if "volume_id" in entry
                         else chapter.get("volume_id"))
            self.db.set_chapter_meta(
                chapter["id"], title=entry.get("title") or "",
                status=entry.get("status") or "planned",
                volume_id=volume_id)
        self.db.replace_ledger(work_id, target["ledger"])
        return target

    def vcs_restore(self, work_id: str, commit_id: str, *,
                    message: str = "") -> dict[str, Any]:
        """回滚 = 把目标状态落到工作区，并在当前分支上留一条新提交。"""
        self._work_or_raise(work_id)
        target = self._commit_ref_or_raise(work_id, commit_id)
        head = self.db.get_head(work_id)
        status = self.vcs_status(work_id)
        head_state = self._resolve_state(work_id, "HEAD")
        target_state = self._resolve_state(work_id, target["id"])
        same_tree = not any(vcs.diff_trees(head_state["tree"],
                                           target_state["tree"]).values())
        same_ledger = (vcs.ledger_hash(head_state["ledger"])
                       == vcs.ledger_hash(target_state["ledger"]))
        if status["clean"] and same_tree and same_ledger:
            raise WritingError("已经在目标状态上")
        # 脏工作区先检查点；即使目标是 HEAD，也走同一条路——否则检查点
        # 成了 HEAD，回滚完工作区反而显示「有未提交改动」。
        parent = head.get("commit_id")
        if not status["clean"]:
            checkpoint = self.vcs_commit(work_id, "回滚前自动提交",
                                         author="system", kind="checkpoint")
            parent = checkpoint["id"]
        self._apply_state(work_id, target, reason="restore")
        current = self._current_state(work_id)
        created = self.db.create_commit(
            work_id, tree=current["tree"], ledger=current["ledger"],
            parent_id=parent, branch_name=head.get("branch_name") or "main",
            kind="restore", author="user",
            message=(message or "").strip()
            or f"回滚到 {vcs.short_id(target['id'])}",
            chapter_index=max((int(c.get("chapter_index") or 0)
                               for c in current["tree"]), default=0))
        return {"commit": self._commit_brief(created),
                "status": self.vcs_status(work_id)}

    def _chapter_content(self, state: Mapping[str, Any],
                         chapter_id: str) -> str:
        for entry in state.get("tree") or []:
            if str(entry.get("chapter_id")) == str(chapter_id):
                return self._version_content(entry.get("version_id"))
        return ""

    def _version_content(self, version_id: Any) -> str:
        return (self.db.get_version(str(version_id or "")) or {}).get(
            "content") or ""

    # ── 三路合并：merge / revert / cherry-pick 共用 ──────────────────────
    # base 是共同祖先，ours 是当前分支，theirs 是要合进来的那边。
    # 「回滚某次提交」= 把那次提交的 parent 当作 theirs；
    # 「摘取某次提交」= 把那次提交当作 theirs。同一套代码。

    @staticmethod
    def _meta_key(entry: Mapping[str, Any] | None) -> tuple | None:
        if entry is None:
            return None
        return tuple(entry.get(f) for f in
                     ("chapter_index", "title", "status", "volume_id"))

    @staticmethod
    def _entry_brief(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if entry is None:
            return None
        return {"version_id": entry.get("version_id"),
                "content_hash": entry.get("content_hash") or "",
                "word_count": entry.get("word_count") or 0,
                "title": entry.get("title") or "",
                "status": entry.get("status") or "",
                "chapter_index": entry.get("chapter_index") or 0}

    def _chapter_conflict(self, cid: str, base_e, ours_e,
                          theirs_e) -> dict[str, Any]:
        shown = theirs_e or ours_e or base_e or {}
        return {
            "kind": "chapter", "key": f"chapter:{cid}",
            "chapter_id": cid,
            "chapter_index": int(shown.get("chapter_index") or 0),
            "title": str(shown.get("title") or ""),
            "base": self._entry_brief(base_e),
            "ours": self._entry_brief(ours_e),
            "theirs": self._entry_brief(theirs_e),
            # 裁决界面给一眼能认出改了什么，不必再拉一次 diff。
            "ours_preview": self._version_content(
                (ours_e or {}).get("version_id"))[:160],
            "theirs_preview": self._version_content(
                (theirs_e or {}).get("version_id"))[:160],
        }

    def _merge_tree(self, base: Mapping[str, Any], ours: Mapping[str, Any],
                    theirs: Mapping[str, Any],
                    resolutions: Mapping[str, Any]
                    ) -> tuple[list[dict], dict[str, str], list[dict]]:
        """按章三路合并。返回 (合并后的树, 需要新写入的正文, 冲突清单)。"""
        base_t = {str(e.get("chapter_id")): e for e in base.get("tree") or []}
        ours_t = {str(e.get("chapter_id")): e for e in ours.get("tree") or []}
        theirs_t = {str(e.get("chapter_id")): e for e in theirs.get("tree") or []}
        ids = set(ours_t) | set(theirs_t)
        merged: list[dict[str, Any]] = []
        texts: dict[str, str] = {}
        conflicts: list[dict[str, Any]] = []
        for cid in sorted(ids, key=lambda k: int(
                (theirs_t.get(k) or ours_t.get(k) or {}).get("chapter_index") or 0)):
            b, o, t = base_t.get(cid), ours_t.get(cid), theirs_t.get(cid)
            # 存储层没有「删除章节」这条路：某一侧缺这一章时按「未变」处理，
            # 否则会把它从合并后的树里抹掉，工作区却删不掉，两边永远不一致。
            if o is None:
                chosen = t
                if chosen is not None:
                    merged.append(dict(chosen))
                continue
            if t is None:
                merged.append(dict(o))
                continue
            kb, ko, kt = (vcs.entry_key(b), vcs.entry_key(o),
                          vcs.entry_key(t))
            if ko == kt:
                chosen = o or t
            elif ko == kb:
                chosen = t
            elif kt == kb:
                chosen = o
            else:
                chosen, text = self._resolve_chapter_conflict(
                    cid, b, o, t, resolutions)
                if text is not None:
                    texts[cid] = text
                if chosen is None and not text:
                    conflicts.append(self._chapter_conflict(cid, b, o, t))
                    continue
            if chosen is not None:
                merged.append(dict(chosen))
        return merged, texts, conflicts

    def _resolve_chapter_conflict(self, cid: str, b, o, t,
                                  resolutions: Mapping[str, Any]
                                  ) -> tuple[dict | None, str | None]:
        """单章冲突：先看用户裁决，再试正文级三路合并，都不行返回 (None, None)。"""
        choice = resolutions.get(f"chapter:{cid}")
        if choice == "ours":
            return (o, None)
        if choice == "theirs":
            return (t, None)
        if choice == "base":
            return (b, None)
        if isinstance(choice, Mapping) and choice.get("mode") == "text":
            entry = dict(o or t or b or {})
            return (entry, str(choice.get("content") or ""))
        if self._meta_key(o) != self._meta_key(t):
            return (None, None)
        merged_text, ok = vcs.merge3_text(
            self._version_content((b or {}).get("version_id")),
            self._version_content((o or {}).get("version_id")),
            self._version_content((t or {}).get("version_id")))
        if not ok:
            return (None, None)
        return (dict(o or t or {}), merged_text)

    def _merge_ledger(self, base: Mapping[str, Any], ours: Mapping[str, Any],
                      theirs: Mapping[str, Any],
                      resolutions: Mapping[str, Any]
                      ) -> tuple[dict[str, list], list[dict]]:
        """按主键三路合并账本。同键两侧改得不一样才算冲突。"""
        merged: dict[str, list] = {}
        conflicts: list[dict[str, Any]] = []
        for collection, key_name in vcs.LEDGER_KEYS.items():
            b = {str(r.get(key_name)): r
                 for r in base.get("ledger", {}).get(collection) or []}
            o = {str(r.get(key_name)): r
                 for r in ours.get("ledger", {}).get(collection) or []}
            t = {str(r.get(key_name)): r
                 for r in theirs.get("ledger", {}).get(collection) or []}
            rows = []
            for key in sorted(set(o) | set(t)):
                bo, oo, tt = b.get(key), o.get(key), t.get(key)
                if oo == tt:
                    pick = oo or tt
                elif oo == bo:
                    pick = tt
                elif tt == bo:
                    pick = oo
                else:
                    res = resolutions.get(f"ledger:{collection}:{key}")
                    if res == "ours":
                        pick = oo
                    elif res == "theirs":
                        pick = tt
                    elif res == "base":
                        pick = bo
                    else:
                        pick = None
                        conflicts.append({
                            "kind": "ledger", "collection": collection,
                            "id": key, "key": f"ledger:{collection}:{key}",
                            "label": vcs.ledger_label(collection, oo or tt or bo or {}),
                        })
                if pick is not None:
                    rows.append(pick)
            merged[collection] = rows
        return merged, conflicts

    def _apply_merged(self, work_id: str, merged_tree: Sequence[Mapping],
                      texts: Mapping[str, str],
                      merged_ledger: Mapping[str, Any], *,
                      reason: str) -> list[dict[str, Any]]:
        """把合并结果落到工作区。返回最终树（带新的 version_id）。"""
        chapters = {c["id"]: c for c in self.db.list_chapters(work_id)}
        final: list[dict[str, Any]] = []
        for entry in merged_tree:
            cid = str(entry.get("chapter_id"))
            chapter = chapters.get(cid)
            if chapter is None:
                continue
            if cid in texts and (chapter.get("content") or "") != texts[cid]:
                saved = self.db.save_draft(work_id, cid, texts[cid],
                                           reason=reason)
                entry = dict(entry, version_id=saved.get("version_id"),
                             version=saved.get("version"),
                             content_hash=saved.get("content_hash"),
                             word_count=saved.get("word_count"))
                self.db.delete_summary(cid)
            else:
                content = self._version_content(entry.get("version_id"))
                if (chapter.get("content") or "") != content:
                    self.db.save_draft(work_id, cid, content, reason=reason)
                    self.db.delete_summary(cid)
            volume_id = (entry["volume_id"] if "volume_id" in entry
                         else chapter.get("volume_id"))
            self.db.set_chapter_meta(
                cid, title=entry.get("title") or "",
                status=entry.get("status") or "planned", volume_id=volume_id)
            final.append(dict(entry))
        self.db.replace_ledger(work_id, merged_ledger)
        return final

    def _three_way(self, work_id: str, *, base_ref: str, theirs_ref: str,
                   ours_ref: str, resolutions: Mapping[str, Any], reason: str
                   ) -> dict[str, Any]:
        base = self._resolve_state(work_id, base_ref)
        ours = self._resolve_state(work_id, ours_ref)
        theirs = self._resolve_state(work_id, theirs_ref)
        merged_tree, texts, tree_conflicts = self._merge_tree(
            base, ours, theirs, resolutions)
        merged_ledger, ledger_conflicts = self._merge_ledger(
            base, ours, theirs, resolutions)
        conflicts = tree_conflicts + ledger_conflicts
        if conflicts:
            return {"applied": False, "conflicts": conflicts,
                    "base": self._commit_brief(base.get("commit")) or {},
                    "ours": self._commit_brief(ours.get("commit")) or {},
                    "theirs": self._commit_brief(theirs.get("commit")) or {}}
        final_tree = self._apply_merged(
            work_id, merged_tree, texts, merged_ledger, reason=reason)
        return {"applied": True, "tree": final_tree, "ledger": merged_ledger,
                "text_count": len(texts)}

    def _dirty_checkpoint(self, work_id: str, message: str) -> None:
        """合并/回滚/摘取前先把手头改动保住——和切分支同一策略。"""
        self._auto_commit(work_id, message, kind="checkpoint", author="system")

    def vcs_merge(self, work_id: str, branch: str, *,
                  message: str = "",
                  resolutions: Mapping[str, Any] | None = None
                  ) -> dict[str, Any]:
        """把 ``branch`` 合进当前分支。能快进就快进，否则做三路合并。"""
        self._work_or_raise(work_id)
        branch = (branch or "").strip()
        ref = self.db.get_ref(work_id, branch)
        if ref is None or ref.get("kind") != "branch":
            raise WritingError(f"分支不存在：{branch}")
        head = self.db.get_head(work_id)
        current = head.get("branch_name") or "main"
        if branch == current:
            raise WritingError("不能把分支合并到它自己")
        theirs = self.db.get_commit(ref["commit_id"])
        if theirs is None:
            raise WritingError("分支指向的提交不存在")

        ours_id = head.get("commit_id")
        if not ours_id:
            self._apply_state(work_id, theirs, reason="merge")
            self.db.upsert_ref(work_id, current, theirs["id"])
            self.db.set_head(work_id, current, theirs["id"])
            self.db.log_head(work_id, branch_name=current,
                             commit_id=theirs["id"], from_commit_id="",
                             reason="merge")
            return {"fast_forward": True, "commit": None,
                    "status": self.vcs_status(work_id)}

        commits = self._commit_map(work_id)
        base_id = vcs.merge_base(commits, ours_id, theirs["id"])
        if base_id == theirs["id"]:
            raise WritingError(f"{branch} 的改动已经都在当前分支里了")
        if base_id == ours_id:
            self._dirty_checkpoint(work_id, f"合并 {branch} 前自动提交")
            self._apply_state(work_id, theirs, reason="merge")
            self.db.upsert_ref(work_id, current, theirs["id"])
            self.db.set_head(work_id, current, theirs["id"])
            self.db.log_head(work_id, branch_name=current,
                             commit_id=theirs["id"], from_commit_id=ours_id,
                             reason="merge-ff")
            return {"fast_forward": True, "commit": None,
                    "status": self.vcs_status(work_id)}

        self._dirty_checkpoint(work_id, f"合并 {branch} 前自动提交")
        head = self.db.get_head(work_id)
        ours_id = head.get("commit_id")
        result = self._three_way(
            work_id, base_ref=base_id or "EMPTY", theirs_ref=theirs["id"],
            ours_ref=ours_id, resolutions=resolutions or {}, reason="merge")
        if not result["applied"]:
            return {"fast_forward": False, "conflicts": result["conflicts"],
                    "base": result["base"], "ours": result["ours"],
                    "theirs": result["theirs"],
                    "status": self.vcs_status(work_id)}
        ours_state = self._resolve_state(work_id, ours_id)
        changes = vcs.diff_trees(ours_state["tree"], result["tree"])
        if (not any(changes.values())
                and vcs.ledger_hash(ours_state["ledger"])
                == vcs.ledger_hash(result["ledger"])):
            raise WritingError(f"{branch} 的改动当前分支已经有了，合并不会产生新内容")
        created = self.db.create_commit(
            work_id, tree=result["tree"], ledger=result["ledger"],
            parent_id=ours_id, parent2_id=theirs["id"], branch_name=current,
            kind="merge", author="user",
            message=(message or "").strip() or f"合并 {branch} 到 {current}",
            chapter_index=self._max_index(result["tree"]))
        return {"fast_forward": False,
                "commit": self._commit_brief(created) or {},
                "status": self.vcs_status(work_id)}

    def _apply_single_commit(self, work_id: str, target_id: str, *,
                             mode: str, message: str,
                             resolutions: Mapping[str, Any] | None
                             ) -> dict[str, Any]:
        """revert / cherry-pick 的共同实现。"""
        target = self._commit_ref_or_raise(work_id, target_id)
        parent = (self.db.get_commit(target.get("parent_id"))
                  if target.get("parent_id") else None)
        if mode == "revert" and parent is None:
            raise WritingError("根提交没有可撤销的父状态")
        self._dirty_checkpoint(
            work_id, f"{'撤销' if mode == 'revert' else '摘取'}"
                     f" {vcs.short_id(target['id'])} 前自动提交")
        head = self.db.get_head(work_id)
        ours_id = head.get("commit_id")
        if not ours_id:
            raise WritingError("当前分支还没有提交")
        if mode == "revert":
            base_ref, theirs_ref = target_id, parent["id"]
        else:
            base_ref, theirs_ref = (parent or {}).get("id") or "EMPTY", target_id
        result = self._three_way(
            work_id, base_ref=base_ref, theirs_ref=theirs_ref,
            ours_ref=ours_id, resolutions=resolutions or {}, reason=mode)
        if not result["applied"]:
            return {"conflicts": result["conflicts"],
                    "base": result["base"], "ours": result["ours"],
                    "theirs": result["theirs"]}
        ours_state = self._resolve_state(work_id, ours_id)
        changes = vcs.diff_trees(ours_state["tree"], result["tree"])
        noop = (not any(changes.values())
                and vcs.ledger_hash(ours_state["ledger"])
                == vcs.ledger_hash(result["ledger"]))
        if noop:
            raise WritingError(
                "这次提交已经撤销过了" if mode == "revert"
                else "这次提交的改动已经在当前分支上了")
        branch = head.get("branch_name") or "main"
        created = self.db.create_commit(
            work_id, tree=result["tree"], ledger=result["ledger"],
            parent_id=ours_id, branch_name=branch, kind=mode, author="user",
            message=(message or "").strip() or (
                f"撤销 {vcs.short_id(target['id'])}"
                if mode == "revert"
                else f"摘取 {vcs.short_id(target['id'])}"),
            chapter_index=self._max_index(result["tree"]))
        return {"commit": self._commit_brief(created) or {},
                "status": self.vcs_status(work_id)}

    def vcs_revert(self, work_id: str, commit_id: str, *,
                   message: str = "",
                   resolutions: Mapping[str, Any] | None = None
                   ) -> dict[str, Any]:
        """撤销**某一次**提交的改动，历史里留一条反向提交（不是 reset）。"""
        self._work_or_raise(work_id)
        return self._apply_single_commit(
            work_id, commit_id, mode="revert", message=message,
            resolutions=resolutions)

    def vcs_cherry_pick(self, work_id: str, commit_id: str, *,
                        message: str = "",
                        resolutions: Mapping[str, Any] | None = None
                        ) -> dict[str, Any]:
        """把别处（通常是岔路）的一次提交摘到当前分支。"""
        self._work_or_raise(work_id)
        return self._apply_single_commit(
            work_id, commit_id, mode="cherry-pick", message=message,
            resolutions=resolutions)

    def vcs_diff(self, work_id: str, *, from_ref: str = "HEAD",
                 to_ref: str = "WORK", chapter_id: str = "",
                 text: bool = False) -> dict[str, Any]:
        """差异。``from='main...岔路A'`` 按 git 的三点语义：从合并基算起。"""
        self._work_or_raise(work_id)
        if "..." in (from_ref or ""):
            left, _, right = (from_ref or "").partition("...")
            commits = self._commit_map(work_id)
            a = self._resolve_commit(work_id, left or "HEAD")
            b = self._resolve_commit(work_id, right or "HEAD")
            base_id = vcs.merge_base(commits, (a or {}).get("id"),
                                     (b or {}).get("id"))
            from_ref = base_id or "EMPTY"
            # 与 git 一致：A...B 的终点是 B，不取默认的 WORK。
            if (right or "").strip():
                to_ref = right.strip()
        base = self._resolve_state(work_id, from_ref or "HEAD")
        target = (self._current_state(work_id)
                  if (to_ref or "WORK") == "WORK"
                  else self._resolve_state(work_id, to_ref))
        changes = vcs.diff_trees(base["tree"], target["tree"])
        result: dict[str, Any] = {
            "from": self._commit_brief(base["commit"]) or {
                "id": "EMPTY", "short": "EMPTY", "message": "空树"},
            "to": self._commit_brief(target["commit"]) or {
                "id": "WORK", "short": "WORK", "message": "工作区"},
            "changes": changes,
            "counts": {k: len(v) for k, v in changes.items()},
            "ledger": vcs.diff_ledger(base["ledger"], target["ledger"]),
        }
        # 目标侧（A→B 的 B）改动章的首尾预览：核对差异方向不用再切分支/
        # 读章。B3 的 pass 就是靠它直接判断「是岔路甲而不是风从码头来」。
        previews = []
        for entry in changes.get("changed") or []:
            cid = str(entry.get("chapter_id") or "")
            if not cid:
                continue
            content = self._chapter_content(target, cid)
            previews.append({
                "chapter_index": entry.get("chapter_index"),
                "title": entry.get("title") or "",
                "head": content[:120],
                "tail": content[-120:] if len(content) > 120 else "",
            })
        if previews:
            result["previews"] = previews
        if chapter_id:
            old = self._chapter_content(base, chapter_id)
            new = self._chapter_content(target, chapter_id)
            from_label = (result["from"] or {}).get("short") or "before"
            to_label = (result["to"] or {}).get("short") or "after"
            result["chapter_id"] = chapter_id
            result["text_diff"] = vcs.unified_text_diff(
                old, new, from_label=from_label, to_label=to_label)
            result["changed"] = old != new
        return result

    # ── git 自测：固定的种子作品 ──────────────────────────────────────────

    #: 正文与锚点都写死，agent 才能用「把 X 换成 Y」精确制造差异。
    SELFTEST_CHAPTERS = (
        ("第一章 码头", "甲行：风从码头来。\n乙行：他数着潮声。\n丙行：远处灯灭了。"),
        ("第二章 船期", "丁行：船期写在木牌上。\n戊行：老周没有抬头。"),
        ("第三章 雨", "己行：雨落在篷布上。\n庚行：他把信折好。"),
    )
    SELFTEST_ANCHORS = {
        "甲": "风从码头来", "乙": "他数着潮声", "丙": "远处灯灭了",
        "丁": "船期写在木牌上", "戊": "老周没有抬头",
        "己": "雨落在篷布上", "庚": "他把信折好",
    }

    def seed_selftest_work(self) -> dict[str, Any]:
        """建一个自测专用作品：固定三章正文 + 合同 + 基线提交。

        只给 ``writing_git`` 的 ``selftest_seed`` 用。每次新建，绝不碰作者
        正在写的书——破坏性步骤需要一块可以随便弄脏的地方。
        """
        title = f"版本自测-{time.strftime('%m%d-%H%M%S')}"
        work_id = self.create_work(
            title, premise="写作 git 自测专用作品，可随时删除")
        chapters = []
        for index, (chapter_title, text) in enumerate(
                self.SELFTEST_CHAPTERS, start=1):
            chapter = self.create_chapter(work_id, title=chapter_title)
            self.save_content(work_id, index, text, reason="selftest")
            self.set_contract(chapter["id"], purpose=f"自测：{chapter_title}")
            chapters.append({"chapter_index": index,
                             "chapter_id": chapter["id"],
                             "title": chapter_title,
                             "text": text})
        baseline = self.vcs_commit(work_id, "自测基线")
        return {"work_id": work_id, "title": title, "chapters": chapters,
                "anchors": dict(self.SELFTEST_ANCHORS),
                "baseline_commit": baseline["id"],
                "branch": "main",
                "note": "这是自测专用作品；结束后可以让作者决定是否保留。"}

    def delete_selftest_work(self, work_id: str) -> dict[str, Any]:
        """只删 selftest_seed 建的临时作品；标题前缀是最后一道保险。"""
        work = self.db.get_work(work_id)
        if work is None:
            raise WritingError("作品不存在")
        title = str(work.get("title") or "")
        if not title.startswith("版本自测-"):
            raise WritingError("只允许清理「版本自测-」开头的临时作品")
        self.db.delete_work(work_id)
        return {"deleted": work_id, "title": title}

    # ── agent 反馈日志（问题 / 建议）──────────────────────────────────────

    _FEEDBACK_KINDS = ("issue", "suggestion", "question")
    _FEEDBACK_STATUSES = ("open", "acknowledged", "fixed", "wontfix")

    def report_feedback(self, work_id: str = "", *, kind: str = "issue",
                        area: str = "other", severity: str = "normal",
                        title: str = "", body: str = "",
                        context: Mapping[str, Any] | None = None
                        ) -> dict[str, Any]:
        """记一条功能问题/建议。不是聊天记录，是可检索、可关单的反馈。"""
        title = (title or "").strip()
        if not title:
            raise WritingError("反馈标题不能为空")
        if kind not in self._FEEDBACK_KINDS:
            raise WritingError(f"未知类型：{kind}")
        if severity not in ("low", "normal", "high"):
            raise WritingError(f"未知严重程度：{severity}")
        if work_id and self.db.get_work(work_id) is None:
            raise WritingError("作品不存在")
        return self.db.add_feedback(
            work_id=work_id, source="agent", kind=kind, area=area or "other",
            severity=severity, title=title, body=body or "",
            context=context)

    def list_feedback(self, work_id: str = "", *, status: str = "",
                      limit: int = 100) -> list[dict[str, Any]]:
        return self.db.list_feedback(work_id=work_id, status=status,
                                     limit=limit)

    def set_feedback_status(self, feedback_id: str,
                            status: str) -> dict[str, Any]:
        if status not in self._FEEDBACK_STATUSES:
            raise WritingError(f"未知状态：{status}")
        row = self.db.set_feedback_status(feedback_id, status)
        if row is None:
            raise WritingError("反馈不存在")
        return row

    def feedback_summary(self, work_id: str = "") -> dict[str, int]:
        rows = self.list_feedback(work_id, limit=1000)
        out = {"open": 0, "high": 0}
        for row in rows:
            if row.get("status") == "open":
                out["open"] += 1
                if row.get("severity") == "high":
                    out["high"] += 1
        return out

    def _git_scene(self, work_id: str, *, commit_limit: int = 6) -> list[str]:
        """「我在哪个分支、工作区脏不脏、刚提交了什么」的紧凑现场。

        跨轮对话只回灌最近 3 条纯文本，工具结果不进历史。把这几行放进
        system prompt，agent 续写时就不必先花一轮 status/log 重新摸路。
        """
        try:
            status = self.vcs_status(work_id)
        except Exception:
            return []
        head = status.get("head") or {}
        lines = [
            f"- 分支 {status.get('branch') or 'main'}"
            f"｜HEAD {head.get('short') or '-'}"
            f"「{str(head.get('message') or '')[:36]}」"
            f"｜{'干净' if status.get('clean') else '有未提交改动'}"
            f"｜暂存 {status.get('stash_count', 0)} 条"
        ]
        dirty: list[str] = []
        changes = status.get("changes") or {}
        for key, label in (("added", "新增"), ("changed", "改"),
                           ("removed", "删")):
            for entry in changes.get(key) or []:
                dirty.append(f"第{entry.get('chapter_index')}章{label}")
        if dirty:
            lines.append("- 未提交：" + "、".join(dirty[:6]))
        try:
            commits = self.vcs_log(work_id, limit=commit_limit)
        except Exception:
            commits = []
        if commits:
            lines.append("- 最近提交：")
            for c in commits:
                lines.append(
                    f"  - {c.get('short') or ''} {c.get('kind') or ''}"
                    f"「{str(c.get('message') or '')[:36]}」")
        return lines

    def agent_prompt(self, work_id: str = "",
                     chapter_index: int = 0) -> str:
        """对话模式的 system prompt：身份规则 + **当前写作现场**。

        现场信息由服务端给，不让模型先调工具查一遍——与扮演模式
        「你在哪本书里是事实」同一条。
        """
        parts = [self._prompt("agent.md")]
        works = self.list_works()
        if not works:
            parts.append("## 当前写作现场\n还没有任何作品。等作者说书名。")
            return "\n\n".join(parts)

        lines = [f"- {w['title']}（{w.get('chapter_count', 0)} 章）"
                 f"　id={w['id']}" for w in works[:12]]
        parts.append("## 当前写作现场\n### 作品\n" + "\n".join(lines))

        work = self.db.get_work(work_id) if work_id else None
        if work is None:
            work = works[0]
        chapters = self.db.list_chapters(work["id"])
        if chapters:
            rows = []
            for c in chapters[-12:]:
                contract = self.db.get_contract(c["id"])
                rows.append(
                    f"- 第{c['chapter_index']}章 {c.get('title') or '未命名'}"
                    f"｜{c.get('status')}｜{c.get('word_count', 0)} 字"
                    f"｜{'有合同' if contract else '缺合同'}")
            parts.append(f"### 《{work['title']}》的章节\n" + "\n".join(rows))
        else:
            parts.append(f"### 《{work['title']}》\n还没有章节。")

        target = next((c for c in chapters
                       if c["chapter_index"] == int(chapter_index or 0)), None)
        if target is not None:
            contract = self.db.get_contract(target["id"]) or {}
            parts.append(
                f"### 作者当前打开\n第{target['chapter_index']}章"
                f" {target.get('title') or '未命名'}｜"
                f"{target.get('status')}｜{target.get('word_count', 0)} 字\n"
                f"合同：{_contract_text(contract, target['chapter_index'])}")

        scene = self._git_scene(work["id"])
        if scene:
            parts.append(f"### 《{work['title']}》的版本现场\n"
                         + "\n".join(scene))

        # 自测作品通常不是前端当前打开的那本（前端选的是作者的书）。
        # 单独列出最近两个「版本自测-」作品，续做清单时不必先 status。
        selftests = [w for w in works
                     if str(w.get("title") or "").startswith("版本自测-")
                     and w["id"] != work["id"]][:2]
        if selftests:
            blocks = []
            for w in selftests:
                scene = self._git_scene(w["id"], commit_limit=5)
                if scene:
                    blocks.append(f"#### {w['title']}　id={w['id']}\n"
                                  + "\n".join(scene))
            if blocks:
                anchors = "，".join(
                    f"{k}={v}" for k, v in self.SELFTEST_ANCHORS.items())
                parts.append(
                    "### 最近的自测作品\n" + "\n\n".join(blocks)
                    + "\n\n初始锚点（只对还没被改过的行有效；被改过的行以"
                      "最新提交/上一步预览为准）：" + anchors)
        return "\n\n".join(parts)

    def replace_text(self, work_id: str, chapter_index: int,
                     anchor_quote: str, replacement: str) -> dict[str, Any]:
        """唯一锚点替换。与提案制修订同一道护栏，只是不经审稿。"""
        chapter = self.chapter(work_id, chapter_index)
        content = str(chapter.get("content") or "")
        if not anchor_quote or content.count(anchor_quote) != 1:
            raise WritingError("anchor_quote 必须在正文中恰好出现一次")
        new_content = content.replace(anchor_quote, replacement, 1)
        return self.db.save_draft(work_id, chapter["id"], new_content,
                                  reason="manual")

    def _prompt(self, name: str) -> str:
        path = Path(self.config.project_dir) / "prompts" / "writing" / name
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WritingError(f"提示词缺失：{path}") from exc

    # ── 作者声音透镜（M4）────────────────────────────────────────────────

    #: 合同文本 → 场景类型。只认显眼词，认不出就不给 scene_type（不猜）。
    _STYLE_SCENE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
        (("对话", "对白", "争论", "吵架", "谈判"), "dialogue"),
        (("回忆", "记忆", "往事", "闪回"), "memory"),
        (("战斗", "打斗", "追击", "逃跑", "搏斗"), "action"),
        (("风景", "景物", "景色", "环境描写"), "landscape"),
        (("内心", "独白", "自省", "反思"), "introspection"),
        (("情绪", "崩溃", "痛哭", "高潮"), "emotional_peak"),
        (("章末", "结尾", "收束"), "ending"),
        (("开场", "开篇", "抵达"), "opening"),
        (("路上", "途中", "过渡"), "transition"),
        (("沉默", "静场", "停顿"), "quiet"),
        (("交代", "说明", "介绍背景"), "description"),
    )

    def _log_reference_diag(self, work: Mapping[str, Any],
                            chapter: Mapping[str, Any],
                            diag: Mapping[str, Any]) -> None:
        """召回诊断落 JSONL：门是否通过、命中几个词、卡有没有真注入。

        只做观测，不影响写作；日志写失败也不阻塞。
        """
        try:
            path = (Path(self.config.project_dir) / "data" / "writing"
                    / "reference_cards" / "diagnostics.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "work_id": str(work.get("id") or ""),
                "chapter": int(chapter.get("chapter_index") or 0),
                **{k: v for k, v in (diag or {}).items() if k != "card"},
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:                     # noqa: BLE001
            log.warning("writegraph: reference diag log failed: %s", exc)

    @staticmethod
    def _profile_of(work: Mapping[str, Any]) -> dict[str, Any]:
        raw = work.get("style_profile") or "{}"
        if isinstance(raw, Mapping):
            return dict(raw)
        try:
            parsed = json.loads(str(raw))
        except (json.JSONDecodeError, TypeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    def _infer_scene(self, contract: Mapping[str, Any] | None) -> str:
        if not contract:
            return ""
        parts = [str(contract.get("purpose") or ""),
                 str(contract.get("exclusive_event") or ""),
                 str(contract.get("ending_state") or "")]
        parts += [str(x) for x in (contract.get("must_hit_now") or [])]
        text = " ".join(parts)
        for hints, scene in self._STYLE_SCENE_HINTS:
            if any(h in text for h in hints):
                return scene
        return ""

    def _resolve_style_ref(self, work: Mapping[str, Any],
                           override: Mapping[str, Any] | None
                           ) -> dict[str, Any] | None:
        """显式请求优先；否则取作品 ``style_profile.author_refs`` 里启用的那位。"""
        if override:
            ref = dict(override)
            if str(ref.get("author_id") or "").strip():
                return ref
        profile = self._profile_of(work)
        refs = [dict(r) for r in (profile.get("author_refs") or [])
                if isinstance(r, Mapping) and r.get("author_id")
                and r.get("enabled", True)]
        return refs[0] if refs else None

    def _build_author_voice(self, work: Mapping[str, Any],
                            contract: Mapping[str, Any],
                            style_ref: Mapping[str, Any]
                            ) -> tuple[str, dict[str, Any]]:
        """取数 → 透镜 → 排块。**任何失败都返回空块，绝不阻塞写作。**"""
        ref = dict(style_ref or {})
        author_id = str(ref.get("author_id") or "").strip()
        diag: dict[str, Any] = {"enabled": True, "author_id": author_id,
                                "code": "", "warnings": []}
        if not author_id:
            diag["code"] = "no_author"
            return "", diag
        adb = None
        try:
            path = resolve_authors_db_path(self.config)
            if not path.exists():
                # 不要为了「查一下」就建出一个空库。
                diag["code"] = "author_db_unavailable"
                return "", diag
            adb = AuthorsDB(path)
            author = adb.get_author(author_id)
            if author is None:
                diag["code"] = "author_not_found"
                return "", diag
            name = str(author.get("canonical_name") or "").strip()
            diag["author_name"] = name
            summary = adb.voice_status_summary(author_id)
            status = str(summary.get("status") or "none")
            diag["voice_status"] = status
            diag["book_status"] = summary.get("counts") or {}
            if status == "none":
                diag["code"] = "no_voice_samples"
                return "", diag
            if status == "pending":
                diag["code"] = "voice_pending"
                return "", diag
            if status == "failed":
                diag["code"] = "voice_failed"
                return "", diag
            if status == "stale":
                diag["code"] = "voice_stale"
                return "", diag

            scene = (str(ref.get("scene_type") or "").strip()
                     or self._infer_scene(contract))
            scene_profile = {
                "author_id": author_id,
                "scene_type": scene,
                "period": str(ref.get("period") or "").strip(),
                "pov": (str(ref.get("pov") or "").strip()
                        or str(work.get("narrative_pov") or "").strip()),
                "keywords": list(ref.get("keywords") or []),
            }
            picked = select_for_scene(adb, author_id, scene_profile,
                                      limit=40, trait_limit=12)
            lens = voice_lens_mod.build_lens(
                picked["samples"], picked["traits"],
                scene_profile=scene_profile, contract=contract)
            partial = status == "partial"
            text = voice_card_mod.render(name, lens, partial=partial)
            # 诊断报「实际排进块里的数量」，不是透镜候选数 —— 卡片还会
            # 按预算裁一遍，两者可能差一条。
            shown = voice_card_mod.counts(text)
            diag.update({
                "scene": scene,
                "partial": partial,
                "rules": shown["rules"],
                "samples": shown["samples"],
                "sample_chars": lens["stats"]["sample_chars"],
                "warnings": list(diag["warnings"]) + list(
                    picked.get("warnings") or []) + list(lens["warnings"]),
            })
            if not text:
                diag["code"] = ("no_transferable_traits"
                                if picked.get("traits")
                                else "no_voice_samples")
            return text, diag
        except Exception as exc:                     # noqa: BLE001
            # 作者库损坏/路径不可写都不该拦住写作。
            log.warning("writegraph: author voice unavailable: %s", exc)
            diag["code"] = "author_db_unavailable"
            diag["warnings"].append(str(exc))
            return "", diag
        finally:
            if adb is not None:
                adb.close()

    def get_style_profile(self, work_id: str) -> dict[str, Any]:
        work = self._work_or_raise(work_id)
        profile = self._profile_of(work)
        return {"author_refs": profile.get("author_refs") or [],
                "style_profile": profile}

    def set_style_ref(self, work_id: str, author_id: str = "", *,
                      enabled: bool = True, scope_type: str = "author",
                      scope_ref: str = "", scene_type: str = "",
                      period: str = "") -> dict[str, Any]:
        """P1 不新增表：默认选择进 ``works.style_profile.author_refs``。

        最多两位；同一位作者重复设置就更新那一条，不叠加。清空传空 id。
        """
        work = self._work_or_raise(work_id)
        profile = self._profile_of(work)
        refs = [dict(r) for r in (profile.get("author_refs") or [])
                if isinstance(r, Mapping)]
        author_id = (author_id or "").strip()
        if not author_id:
            # 空 id = 清空默认选择；不能用「过滤掉空 id」来实现，
            # 那样一条都删不掉。
            refs = []
        else:
            refs = [r for r in refs
                    if str(r.get("author_id") or "") != author_id]
            if len(refs) >= 2:
                raise WritingError("一个作品最多两位参考作者；先清掉一位")
            refs.append({"author_id": author_id,
                         "scope_type": scope_type or "author",
                         "scope_ref": scope_ref or "",
                         "scene_type": scene_type or "",
                         "period": period or "",
                         "enabled": bool(enabled)})
        profile["author_refs"] = refs
        self.db.update_work_style_profile(work_id, profile)
        return self.get_style_profile(work_id)

    def set_reference_books(self, work_id: str,
                            books: Sequence[Mapping[str, Any]], *,
                            mode: str = "card") -> dict[str, Any]:
        """作品选定的参考书（别的书，不是本书正典）。最多 4 本。"""
        work = self._work_or_raise(work_id)
        profile = self._profile_of(work)
        clean: list[dict[str, Any]] = []
        for item in books:
            if not isinstance(item, Mapping):
                continue
            sid = str(item.get("stable_book_id") or "").strip()
            if not sid:
                continue
            clean.append({"stable_book_id": sid,
                          "book_id": str(item.get("book_id") or ""),
                          "label": str(item.get("label") or ""),
                          "enabled": bool(item.get("enabled", True))})
        profile["reference_books"] = clean[:4]
        profile["reference_mode"] = mode if mode in ("card", "raw") else "card"
        self.db.update_work_style_profile(work_id, profile)
        return {"reference_books": profile["reference_books"],
                "reference_mode": profile["reference_mode"]}

    def style_check(self, work_id: str, chapter_index: int, *,
                    style_ref: Mapping[str, Any] | None = None
                    ) -> dict[str, Any]:
        """生成后检查：只报告，不阻断（设计稿 06 §六）。"""
        work = self._work_or_raise(work_id)
        chapter = self.chapter(work_id, chapter_index)
        content = str(chapter.get("content") or "")
        ref = self._resolve_style_ref(work, style_ref)
        if not ref:
            result = voice_check_mod.check(content)
            result["note"] = "没有选择参考作者；只做通用检查。"
            return result
        author_id = str(ref.get("author_id") or "").strip()
        adb = None
        try:
            path = resolve_authors_db_path(self.config)
            if not path.exists():
                return {"enabled": False, "author_id": author_id,
                        "findings": [], "metrics": {},
                        "note": "作者库不存在，跳过风格检查"}
            adb = AuthorsDB(path)
            author = adb.get_author(author_id) if author_id else None
            traits = (eligible_traits(adb, author_id, limit=12)
                      if author else [])
            result = voice_check_mod.check(content, traits=traits,
                                          author_id=author_id)
            result["author_name"] = (author or {}).get("canonical_name") or ""
            return result
        except Exception as exc:                     # noqa: BLE001
            log.warning("writegraph: style check failed: %s", exc)
            return {"enabled": False, "author_id": author_id,
                    "findings": [], "metrics": {},
                    "note": f"风格检查失败：{exc}"}
        finally:
            if adb is not None:
                adb.close()

    # ── 外部参考素材 ──────────────────────────────────────────────────────

    def reference_books(self, work: Mapping[str, Any]) -> list[dict[str, Any]]:
        """作品选定的参考书（``style_profile.reference_books``）。"""
        profile = self._profile_of(work)
        return [dict(b) for b in (profile.get("reference_books") or [])
                if isinstance(b, Mapping) and b.get("enabled", True)]

    def _reference_cache_path(self, work_id: str, chapter_index: int,
                              terms: Sequence[str],
                              books: Sequence[Mapping[str, Any]]) -> Path:
        import hashlib

        key = "|".join(sorted(str(b.get("stable_book_id") or "")
                              for b in books)) + "||" + "|".join(terms)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        # 卡模板换了就不能复用旧卡。
        shape = "v2"
        return (Path(self.config.project_dir) / "data" / "writing"
                / "reference_cards" / work_id
                / f"{chapter_index}-{shape}-{digest}.txt")

    async def distill_reference(self, work: Mapping[str, Any],
                                contract: Mapping[str, Any],
                                chapter: Mapping[str, Any],
                                facts_rows: Sequence[Mapping[str, Any]]
                                ) -> tuple[str, dict[str, Any]]:
        """把候选素材蒸馏成「写法卡」。失败返回空串，调用方退回原文渲染。"""
        from writegraph import reference as reference_mod

        books = self.reference_books(work)
        diag: dict[str, Any] = {"enabled": bool(books), "mode": "card"}
        if not books or self.api_client is None:
            diag["code"] = "no_books" if not books else "no_client"
            return "", diag
        try:
            exclude = {str(f.get("subject_ref") or "") for f in facts_rows}
            exclude |= {str(work.get("title") or ""),
                        str(chapter.get("title") or "")}
            parts = [str(contract.get("purpose") or ""),
                     str(work.get("premise") or "")]
            for key in ("must_hit_now", "must_preserve", "forbidden_cross"):
                parts.extend(str(x) for x in (contract.get(key) or []))
            terms = reference_mod.query_terms("\n".join(parts),
                                              exclude=exclude)
            path = self._reference_cache_path(
                str(work.get("id") or ""), int(chapter.get("chapter_index") or 0),
                terms, books)
            if path.exists():
                diag.update({"cached": True, "chars": len(
                    path.read_text(encoding="utf-8"))})
                return path.read_text(encoding="utf-8"), diag
            pack = reference_mod.recall(self.config, books, terms)
            rel = pack.get("relevance") or {}
            diag["relevance"] = rel
            if not rel.get("ok"):
                diag["code"] = "low_relevance"
                return "", diag
            # 只给观察与细节：写法/信息给法建议会和合同抢戏，
            # 基准里两处合同违规都来自这类建议。
            card = await self._complete(
                REFERENCE_DISTILL, reference_mod.skeleton(pack),
                scenario="writing.reference", kind="reference",
                work_id=str(work.get("id") or ""),
                chapter_index=int(chapter.get("chapter_index") or 0))
            card = strip_think(card).strip()
            if card:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(card, encoding="utf-8")
            diag["chars"] = len(card)
            return card, diag
        except Exception as exc:                     # noqa: BLE001
            log.warning("writegraph: reference distill failed: %s", exc)
            diag["code"] = "error"
            diag["warnings"] = [str(exc)]
            return "", diag

    def _build_reference(self, work: Mapping[str, Any],
                         contract: Mapping[str, Any],
                         chapter: Mapping[str, Any],
                         facts_rows: Sequence[Mapping[str, Any]],
                         override: Mapping[str, Any] | None = None
                         ) -> tuple[str, dict[str, Any]]:
        """召回别的书的写法与细节。失败只降级，绝不阻塞写作。"""
        from writegraph import reference as reference_mod

        cfg = dict(override or {})
        books = list(cfg.get("books") or self.reference_books(work))
        diag: dict[str, Any] = {"enabled": bool(books), "books":
                                [b.get("label") or b.get("stable_book_id", "")
                                 for b in books]}
        if not books:
            diag["code"] = "no_books"
            return "", diag
        try:
            exclude = {str(f.get("subject_ref") or "") for f in facts_rows}
            exclude |= {str(work.get("title") or ""),
                        str(chapter.get("title") or "")}
            parts = [str(contract.get("purpose") or ""),
                     str(work.get("premise") or "")]
            for key in ("must_hit_now", "must_preserve", "forbidden_cross"):
                parts.extend(str(x) for x in (contract.get(key) or []))
            terms = list(cfg.get("terms") or reference_mod.query_terms(
                "\n".join(parts), exclude=exclude))
            pack = reference_mod.recall(self.config, books, terms)
            rel = pack.get("relevance") or {}
            diag.update({"terms": terms, "relevance": rel})
            if not rel.get("ok"):
                diag["code"] = "low_relevance"
                return "", diag
            card = str(cfg.get("card") or "")
            mode = str(cfg.get("mode")
                       or self._profile_of(work).get("reference_mode")
                       or "card")
            if card:
                text = reference_mod.render_distilled(card, diag["books"])
            elif mode == "raw":
                text = reference_mod.render(
                    pack, max_chars=int(cfg.get("max_chars") or 1100))
            else:
                # 预览时还没有蒸馏卡：不要先展示原文，否则预览与实际
                # 生成用的素材不是同一份。生成路径会显式传 card。
                diag["code"] = "card_pending"
                return "", diag
            diag["chars"] = len(text)
            diag["leak_terms"] = reference_mod.leak_terms(pack)[:80]
            if not text:
                diag["code"] = "empty"
            return text, diag
        except Exception as exc:                     # noqa: BLE001
            log.warning("writegraph: reference recall failed: %s", exc)
            diag["code"] = "error"
            diag["warnings"] = [str(exc)]
            return "", diag

    # ── 上下文装配 ────────────────────────────────────────────────────────

    def assemble(self, work_id: str, chapter_index: int, *,
                 focus_refs: Sequence[str] | None = None,
                 budget: int | None = None,
                 style_ref: Mapping[str, Any] | None = None,
                 reference: Mapping[str, Any] | None = None,
                 reference_card: str = ""
                 ) -> dict[str, Any]:
        work = self.db.get_work(work_id)
        if work is None:
            raise WritingError("作品不存在")
        chapter = self.chapter(work_id, chapter_index)
        contract = self.db.get_contract(chapter["id"])
        if contract is None:
            raise WritingError("本章还没有合同，先写合同再生成")

        # 覆盖草稿前留检查点：生成失败/不满意时可以回到原稿。
        self._auto_commit(work_id, f"生成第{chapter_index}章前的自动提交")

        prev = self.db.get_chapter(work_id, chapter_index - 1)
        prev_tail = (prev or {}).get("content", "")[-700:]
        prev_contract = (self.db.get_contract(prev["id"]) if prev else None)
        prev_ending = (prev_contract or {}).get("ending_state", "")
        position = chapter_index - 1

        facts_rows = self.db.get_facts(work_id, up_to_chapter=position)
        current = facts_mod.current(facts_rows, position)
        promise_rows = self.db.get_promises(work_id)
        summaries = self.db.recent_summaries(work_id, chapter_index)

        card = card_mod.build_card(
            work=work, chapter=chapter, contract=contract,
            facts_rows=facts_rows, promise_rows=promise_rows,
            summaries=summaries, focus_refs=focus_refs,
            prev_ending_state=prev_ending, position=position)

        query = lens_mod.build_query(contract=contract, prev_tail=prev_tail,
                                     entities=focus_refs or ())
        raw: list[dict[str, Any]] = []
        fts = lens_mod.fts_query(query)
        if fts:
            raw = self.db.search_chunks(
                work_id, fts, before_chapter=chapter_index - 3, limit=60)
        ranked = lens_mod.rank(raw, chapter=chapter_index,
                               entity_names=tuple(focus_refs or ()))

        blocks = {
            "chapter_contract": _contract_text(contract, chapter_index),
            "facts_current": _facts_text(current),
            "prev_tail": prev_tail,
            "context_card": card["text"],
            "reference_material": lens_mod.render_material(ranked),
        }
        ref_cfg = dict(reference or {})
        if reference_card:
            ref_cfg["card"] = reference_card
        ref_text, ref_diag = self._build_reference(
            work, contract, chapter, facts_rows, ref_cfg)
        if ref_text:
            blocks["external_reference"] = ref_text

        style_info: dict[str, Any] | None = None
        style = self._resolve_style_ref(work, style_ref)
        if style is not None:
            voice_text, style_info = self._build_author_voice(
                work, contract, style)
            if voice_text:
                blocks["author_voice"] = voice_text
        compiled = budget_mod.compile_blocks(
            blocks, budget=budget or budget_mod.DEFAULT_TOTAL_BUDGET)
        result = {
            "chapter_index": chapter_index,
            "text": compiled["text"],
            "tokens": compiled["tokens"],
            "manifest": compiled["manifest"],
            "card": card,
            "references": ranked[:10],
            "reference": ref_diag,
        }
        if style_info is not None:
            result["style"] = style_info
        return result

    # ── 写与结算 ──────────────────────────────────────────────────────────

    async def stream_chapter(self, work_id: str, chapter_index: int, *,
                             extra_instruction: str = "",
                             style_ref: Mapping[str, Any] | None = None,
                             cancel_event=None) -> AsyncIterator[dict[str, Any]]:
        """流式写一章。事件：``stage`` / ``delta`` / ``done`` / ``error``。"""
        if self.api_client is None:
            raise WritingError("未配置模型，无法生成正文")
        chapter = self.chapter(work_id, chapter_index)
        contract = self.db.get_contract(chapter["id"])
        work = self.db.get_work(work_id) or {}
        profile = self._profile_of(work)
        ref_mode = str(profile.get("reference_mode") or "card")
        reference_card = ""
        ref_diag: dict[str, Any] = {}
        if ref_mode == "card":
            facts_rows = self.db.get_facts(work_id,
                                           up_to_chapter=chapter_index - 1)
            reference_card, ref_diag = await self.distill_reference(
                work, contract or {}, chapter, facts_rows)
        assembled = self.assemble(
            work_id, chapter_index, style_ref=style_ref,
            reference={"mode": ref_mode, "card": reference_card},
            reference_card=reference_card)
        ref_info = dict(assembled.get("reference") or {})
        if ref_diag:
            ref_info["distill"] = ref_diag
        assembled["reference"] = ref_info
        self._log_reference_diag(work, chapter, ref_info)
        yield {"event": "stage",
               "data": {"stage": "assembled", "tokens": assembled["tokens"],
                        "manifest": assembled["manifest"],
                        "style": assembled.get("style"),
                        "reference": assembled.get("reference")}}

        target = int((contract or {}).get("target_chars")
                     or work.get("target_chars") or 2000)
        system = self._prompt("chapter.md").replace(
            "{target_chars}", str(target))
        if (assembled.get("reference") or {}).get("chars"):
            system += MATERIAL_PROMPT
        system += LENGTH_PROMPT.format(target=target,
                                       floor=int(target * 0.8))
        user_content = assembled["text"]
        if extra_instruction.strip():
            # 作者本轮的额外要求不是合同，放在最后、标明优先级低于正典。
            user_content += ("\n\n## 作者本轮要求（不覆盖正典与合同）\n"
                             + extra_instruction.strip())
        messages = [{"role": "user", "content": user_content}]

        buf: list[str] = []
        visible = ThinkStreamFilter()
        yield {"event": "stage", "data": {"stage": "writing"}}
        with usage_log.source_scope("writing"):
            async for delta, _tools, _usage in self.api_client.stream(
                    system, messages, [], scenario="writing.draft"):
                if cancel_event is not None and cancel_event.is_set():
                    break
                if delta:
                    buf.append(delta)      # 原始流（含思维链）留给飞行记录仪
                    shown = visible.feed(delta)
                    if shown:
                        yield {"event": "delta", "data": {"text": shown}}
        tail = visible.flush()
        if tail:
            yield {"event": "delta", "data": {"text": tail}}

        raw = "".join(buf)
        self._record_llm(kind="draft", work_id=work_id,
                         chapter_index=chapter_index, system=system,
                         user=user_content, response=raw)
        content = strip_think(raw).strip()
        if not content:
            raise WritingError("模型没有返回正文")
        # 默认关：基准里“按质检单扩写”会为了补长度改动信息给法。
        if profile.get("auto_repair", False):
            issues = self._draft_issues(content, target)
            if issues:
                yield {"event": "stage", "data": {"stage": "revising",
                                                  "issues": issues}}
                try:
                    revised = await self._complete(
                        self._prompt("revise_draft.md"),
                        assembled["text"] + "\n\n## 本章草稿\n" + content
                        + "\n\n## 质检单\n"
                        + "\n".join(f"- {x}" for x in issues),
                        scenario="writing.revise", kind="revise",
                        work_id=work_id, chapter_index=chapter_index)
                    revised = revised.strip()
                    # 修订稿明显短于原稿多半是截断或删戏：宁可留原稿。
                    if len(revised) >= len(content) * 0.8:
                        content = revised
                except Exception as exc:             # noqa: BLE001
                    log.warning("writegraph: auto repair failed: %s", exc)
        saved = self.db.save_draft(work_id, chapter["id"], content,
                                   reason="draft")
        yield {"event": "done",
               "data": {"chapter_index": chapter_index,
                        "content": content, **saved}}

    async def settle(self, work_id: str, chapter_index: int) -> dict[str, Any]:
        """结算：模型交 delta，宿主校验后落库。整份拒绝，不做部分接受。"""
        chapter = self.chapter(work_id, chapter_index)
        content = str(chapter.get("content") or "")
        if not content.strip():
            raise WritingError("正文为空，无从结算")

        existing_facts = self.db.get_facts(work_id)
        existing_promises = self.db.get_promises(work_id)
        known = {str(f.get("subject_ref")) for f in existing_facts}

        open_lines = [f"- {p['id']}｜{p['title']}"
                      for p in existing_promises
                      if p.get("status") not in ("resolved", "abandoned")]
        user = content
        if open_lines:
            user += "\n\n## 未回收承诺\n" + "\n".join(open_lines)

        raw = await self._complete(self._prompt("settle.md"), user,
                                   scenario="writing.settle", kind="settle",
                                   work_id=work_id,
                                   chapter_index=chapter_index)
        parsed = loads_tolerant(raw, expect=dict)
        if not isinstance(parsed, dict):
            raise WritingError("结算输出不是 JSON 对象")

        for op in parsed.get("entity_ops") or []:
            if isinstance(op, Mapping) and op.get("op") == "create":
                name = str(op.get("name") or "").strip()
                if name:
                    known.add(name)

        checked = state_mod.validate_delta(
            parsed, chapter_index=chapter_index, chapter_text=content,
            known_refs=known, existing_facts=existing_facts,
            existing_promises=existing_promises)
        if not checked.ok:
            return {"ok": False, "errors": checked.errors}

        applied = self.db.apply_delta(
            work_id, chapter["id"], chapter_index, delta=checked.delta,
            content_hash=str(chapter.get("content_hash") or ""))
        tension = parsed.get("tension")
        if isinstance(tension, (int, float)):
            self.db.set_tension(work_id, chapter["id"],
                                max(0, min(100, int(tension))),
                                source="ai", note="结算时估算")
        # 结算成功 = 正文与账本都稳定了，自动留一个可回滚的提交。
        self._auto_commit(work_id, f"第{chapter_index}章结算",
                          kind="auto", author="agent")
        return {"ok": True, **applied, "delta": checked.delta}

    # ── 审稿与修订（P2）──────────────────────────────────────────────────

    def _model_name(self) -> str:
        client_config = getattr(self.api_client, "config", None)
        return str(getattr(client_config, "model", "") or "")

    def _review_context(self, work_id: str, chapter_index: int) -> str:
        chapter = self.chapter(work_id, chapter_index)
        contract = self.db.get_contract(chapter["id"]) or {}
        facts_rows = self.db.get_facts(work_id,
                                       up_to_chapter=chapter_index - 1)
        current = facts_mod.current(facts_rows, chapter_index - 1)
        promises = [p for p in self.db.get_promises(work_id)
                    if p.get("status") not in ("resolved", "abandoned")]
        prev = self.db.get_chapter(work_id, chapter_index - 1) or {}

        parts = [_contract_text(contract, chapter_index),
                 "## 位置化事实\n" + _facts_text(current)]
        if promises:
            parts.append("## 未回收承诺\n" + "\n".join(
                f"- {p['id']}｜{p['title']}｜目标第{p.get('planned_payoff_chapter')}章"
                for p in promises))
        if prev.get("content"):
            parts.append("## 上一章结尾\n" + prev["content"][-600:])
        return "\n\n".join(parts)

    async def review_chapter(self, work_id: str,
                             chapter_index: int) -> dict[str, Any]:
        chapter = self.chapter(work_id, chapter_index)
        content = str(chapter.get("content") or "")
        if not content.strip():
            raise WritingError("正文为空，无法审稿")
        context = self._review_context(work_id, chapter_index)
        raw = await self._complete(
            self._prompt("review.md"),
            f"{context}\n\n## 本章正文\n{content}",
            scenario="writing.review", kind="review",
            work_id=work_id, chapter_index=chapter_index)
        parsed = loads_tolerant(raw, expect=dict)
        if not isinstance(parsed, dict):
            raise WritingError("审稿输出不是 JSON 对象")
        result = review_mod.normalize_review(parsed, content)
        run_id = self.db.save_review(
            chapter["id"], str(chapter.get("content_hash") or ""),
            model=self._model_name(), result=result)
        return {"ok": True, "run_id": run_id, "chapter_index": chapter_index,
                **result}

    def get_review(self, work_id: str, chapter_index: int) -> dict[str, Any]:
        chapter = self.chapter(work_id, chapter_index)
        run = self.db.get_review(chapter["id"])
        if run is None:
            return {"ok": True, "review": None}
        if run.get("content_hash") != chapter.get("content_hash"):
            run["status"] = "stale"
        return {"ok": True, "review": run}

    # ── 故事地图与紧张度（P3）────────────────────────────────────────────

    def map_data(self, work_id: str) -> dict[str, Any]:
        work = self.db.get_work(work_id)
        if work is None:
            raise WritingError("作品不存在")
        volumes = self.db.list_volumes(work_id)
        chapters = self.db.list_chapters(work_id)
        promises = self.db.get_promises(work_id)
        summaries = {s["chapter_id"]: s.get("summary", "")
                     for s in self.db.summaries_for_work(work_id)}
        tensions = {t["chapter_id"]: t for t in self.db.get_tensions(work_id)}

        nodes: list[dict[str, Any]] = [
            {"id": "work", "kind": "work", "label": work["title"]}]
        for volume in volumes:
            nodes.append({"id": f"vol:{volume['id']}", "kind": "volume",
                          "label": volume.get("title") or
                          f"第{volume['volume_index']}卷"})
        for chapter in chapters:
            tension = tensions.get(chapter["id"], {})
            nodes.append({
                "id": f"ch:{chapter['id']}", "kind": "chapter",
                "label": f"{chapter['chapter_index']}. "
                         f"{chapter.get('title') or '未命名'}",
                "chapter_index": chapter["chapter_index"],
                "status": chapter.get("status"), "words": chapter.get("word_count"),
                "summary": summaries.get(chapter["id"], ""),
                "tension": tension.get("value")})
            for scene in self.db.list_scenes(chapter["id"]):
                nodes.append({"id": f"sc:{scene['id']}", "kind": "scene",
                              "label": scene.get("title") or
                              f"场景{scene['scene_index']}",
                              "chapter_index": chapter["chapter_index"]})
        for promise in promises:
            nodes.append({
                "id": f"pm:{promise['id']}", "kind": "promise",
                "label": promise["title"], "status": promise.get("status"),
                "weight": promise.get("weight")})

        edges: list[dict[str, Any]] = []
        by_index = {c["chapter_index"]: c for c in chapters}
        for chapter in chapters:
            parent = (f"vol:{chapter['volume_id']}"
                      if chapter.get("volume_id") else "work")
            edges.append({"from": parent, "to": f"ch:{chapter['id']}",
                          "kind": "contains"})
        for promise in promises:
            if promise.get("planned_payoff_chapter") in by_index:
                chapter = by_index[promise["planned_payoff_chapter"]]
                edges.append({"from": f"ch:{chapter['id']}",
                              "to": f"pm:{promise['id']}",
                              "kind": "must_payoff"})
            planted = promise.get("planted_chapter")
            if planted is None:
                planted = promise.get("planned_plant_chapter")
            if planted in by_index:
                chapter = by_index[planted]
                edges.append({"from": f"pm:{promise['id']}",
                              "to": f"ch:{chapter['id']}",
                              "kind": "planted_in"})
        return {"ok": True, "work": work, "nodes": nodes, "edges": edges}

    def tension_curve(self, work_id: str) -> dict[str, Any]:
        if self.db.get_work(work_id) is None:
            raise WritingError("作品不存在")
        chapters = self.db.list_chapters(work_id)
        tensions = {t["chapter_id"]: t for t in self.db.get_tensions(work_id)}
        points = []
        for chapter in chapters:
            row = tensions.get(chapter["id"])
            points.append({
                "chapter_id": chapter["id"],
                "chapter_index": chapter["chapter_index"],
                "title": chapter.get("title") or "",
                "value": row["value"] if row else None,
                "source": row["source"] if row else "",
                "flags": [],
            })
        values = [p["value"] for p in points]
        known = [v for v in values if v is not None]
        global_max = max(known) if known else None
        hints: list[str] = []

        for i in range(2, len(points)):
            window = points[i - 2:i + 1]
            vals = [p["value"] for p in window]
            if any(v is None for v in vals):
                continue
            if max(vals) - min(vals) <= 3:
                for point in window:
                    if "flat" not in point["flags"]:
                        point["flags"].append("flat")
                hints.append(
                    f"第{window[0]['chapter_index']}–"
                    f"{window[-1]['chapter_index']}章连续平坝")

        for volume in self.db.list_volumes(work_id):
            members = [c for c in chapters
                       if c.get("volume_id") == volume["id"]]
            if not members:
                continue
            last = members[-1]
            value = next((p["value"] for p in points
                          if p["chapter_id"] == last["id"]), None)
            if (value is not None and global_max is not None
                    and value < global_max - 8):
                for point in points:
                    if point["chapter_id"] == last["id"]:
                        point["flags"].append("weak_volume_end")
                hints.append(
                    f"第{last['chapter_index']}章（卷末）比全局峰值低 8 分以上")
        return {"ok": True, "points": points, "hints": hints,
                "global_max": global_max}

    def set_tension(self, work_id: str, chapter_index: int, value: int,
                    note: str = "") -> dict[str, Any]:
        chapter = self.chapter(work_id, chapter_index)
        try:
            self.db.set_tension(work_id, chapter["id"], value,
                                source="user", note=note)
        except ValueError as exc:
            raise WritingError(str(exc)) from exc
        return self.tension_curve(work_id)

    async def propose_revision(self, work_id: str, chapter_index: int,
                               finding_id: str) -> dict[str, Any]:
        chapter = self.chapter(work_id, chapter_index)
        finding = self.db.get_finding(finding_id)
        if finding is None:
            raise WritingError("问题条目不存在")
        content = str(chapter.get("content") or "")
        evidence = str(finding.get("evidence") or "")
        anchor = content.find(evidence)
        if anchor < 0:
            raise WritingError("证据已不在当前正文中，请重新审稿")
        excerpt = content[max(0, anchor - 800):anchor + len(evidence) + 800]
        user = (f"## 审稿问题\n严重度 {finding['severity']}｜"
                f"{finding['category']}\n问题：{finding['issue']}\n"
                f"方向：{finding.get('fix_hint') or '（未给）'}\n"
                f"原句：{evidence}\n\n## 正文片段\n{excerpt}")
        raw = await self._complete(self._prompt("revise.md"), user,
                                   scenario="writing.revise", kind="revise",
                                   work_id=work_id,
                                   chapter_index=chapter_index)
        parsed = loads_tolerant(raw, expect=dict)
        if not isinstance(parsed, dict):
            raise WritingError("修订输出不是 JSON 对象")
        if parsed.get("blocked"):
            raise WritingError("模型判定这是事实冲突，请作者处理")
        quote = str(parsed.get("anchor_quote") or "").strip()
        replacement = str(parsed.get("replacement") or "")
        if not quote or not replacement:
            raise WritingError("修订提案缺锚点或替换文本")
        revision = self.db.create_revision(
            chapter["id"], run_id=finding.get("run_id"),
            anchor_quote=quote, replacement=replacement,
            action=str(parsed.get("action") or "custom"))
        return {"ok": True, "revision": revision}

    def list_revisions(self, work_id: str, chapter_index: int
                       ) -> dict[str, Any]:
        chapter = self.chapter(work_id, chapter_index)
        return {"ok": True,
                "revisions": self.db.list_revisions(chapter["id"])}

    def apply_revision(self, work_id: str, chapter_index: int,
                       revision_id: str,
                       expected_hash: str | None = None) -> dict[str, Any]:
        chapter = self.chapter(work_id, chapter_index)
        # 应用修订前留检查点：改坏了可以回到改前。
        self._auto_commit(work_id, f"第{chapter_index}章修订前自动提交")
        try:
            result = self.db.apply_revision(revision_id,
                                            expected_hash=expected_hash)
        except ValueError as exc:
            raise WritingError(str(exc)) from exc
        return {"ok": True, "chapter_id": chapter["id"],
                "content": result["content"],
                "content_hash": result["content_hash"],
                "version": result["version"],
                "revision": result["revision"]}

    def reject_revision(self, work_id: str, chapter_index: int,
                        revision_id: str) -> dict[str, Any]:
        self.chapter(work_id, chapter_index)
        self.db.reject_revision(revision_id)
        return {"ok": True}

    def _draft_issues(self, content: str, target: int) -> list[str]:
        """只收确定性、可修的硬问题；拿不准的不进质检单。"""
        issues: list[str] = []
        if target > 0 and len(content) < target * 0.8:
            issues.append(
                f"字数不足：现在 {len(content)} 字，目标 {target} 字，"
                f"至少写到 {int(target * 0.8)} 字")
        try:
            from writegraph import deslop
            report = deslop.detect(content)
            for item in report.get("issues") or []:
                if item.get("tier") != "blocking":
                    continue
                message = str(item.get("message") or "")
                example = (item.get("examples") or [""])[0]
                if message:
                    issues.append(
                        f"套话/句式：{message}"
                        + (f"（如「{example}」）" if example else ""))
                if len(issues) >= 4:
                    break
        except Exception as exc:                     # noqa: BLE001
            log.warning("writegraph: deslop check failed: %s", exc)
        return issues[:5]

    async def _complete(self, system: str, user: str, *,
                        scenario: str = "", kind: str = "complete",
                        work_id: str = "", chapter_index: int = 0) -> str:
        if self.api_client is None:
            raise WritingError("未配置模型，无法结算")
        buf: list[str] = []
        with usage_log.source_scope("writing"):
            async for delta, _tools, _usage in self.api_client.stream(
                    system, [{"role": "user", "content": user}], [],
                    scenario=scenario):
                if delta:
                    buf.append(delta)
        raw = "".join(buf)
        self._record_llm(kind=kind, work_id=work_id,
                         chapter_index=chapter_index, system=system,
                         user=user, response=raw)
        # 思维链不进 JSON：不剥掉的话 loads_tolerant 会在 <think> 上崩。
        return strip_think(raw)


def _contract_text(contract: Mapping[str, Any], chapter_index: int) -> str:
    lines = [f"第{chapter_index}章合同"]
    fields = (("目的", "purpose"), ("独占事件", "exclusive_event"),
              ("章末状态", "ending_state"),
              ("下场可承接", "next_entry_state"))
    for label, key in fields:
        if contract.get(key):
            lines.append(f"- {label}：{contract[key]}")
    for label, key in (("必须兑现", "must_hit_now"),
                       ("必须保留", "must_preserve"),
                       ("禁止触碰", "forbidden_cross")):
        values = contract.get(key) or []
        if values:
            lines.append(f"- {label}：" + "；".join(str(v) for v in values))
    return "\n".join(lines)


def _facts_text(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "（账本里还没有任何事实）"
    lines = [f"- {r.get('subject_ref')} · {r.get('field')}："
             f"{facts_mod.describe(r)}" for r in rows]
    return "\n".join(lines)
