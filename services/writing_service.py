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
from types import SimpleNamespace
from typing import Any, AsyncIterator, Mapping, Sequence

from llm_json import loads_tolerant
from runtime import StreamTruncated
from authorgraph.voice.select import eligible_traits, select_for_scene
from storage.authors_db import AuthorsDB, resolve_authors_db_path
from storage.writing_db import GIT_HABITS, WritingDB
import usage_log
from writegraph import budget as budget_mod
from writegraph import card as card_mod
from writegraph import facts as facts_mod
from writegraph import goals as goals_mod
from writegraph import lens as lens_mod
from writegraph import outline as outline_mod
from writegraph import reference as reference_mod
from writegraph import review as review_mod
from writegraph import state as state_mod
from writegraph import versioning as vcs
from writegraph import voice_card as voice_card_mod
from writegraph import voice_check as voice_check_mod
from writegraph import voice_lens as voice_lens_mod

log = logging.getLogger("writegraph")


class WritingError(RuntimeError):
    """可展示给用户的失败（缺合同、没配模型、结算被拒）。"""


#: 写作子模式（决策 161）。``detail`` 只抬证据门槛与预算，不换流水线。
WRITING_MODES = ("standard", "detail")


def normalize_writing_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in WRITING_MODES else "standard"


def _mode_label(mode: str) -> str:
    # 界面叫法：standard = 高效，detail = 细致（决策 161）。
    return "细致" if mode == "detail" else "高效"


def validate_research_brief(brief: Any, refs: Sequence[Any] | None,
                            mode: str) -> dict[str, Any]:
    """把 agent 交的取舍说明压成可存的形状；细致模式缺说明直接拒绝。

    只验形状，不判质量、不设数量配额：``questions`` / ``searched`` /
    ``gaps`` 都可以短，但细致模式下「没给 refs 又什么都没说」不接受。
    """
    mode = normalize_writing_mode(mode)
    if brief is None:
        brief = {}
    if not isinstance(brief, Mapping):
        raise WritingError("research_brief 要是一个对象")
    questions = [str(x).strip() for x in (brief.get("questions") or [])
                 if str(x).strip()]
    searched = []
    for row in brief.get("searched") or []:
        if isinstance(row, Mapping):
            item = {k: str(row.get(k) or "").strip()
                    for k in ("source", "query", "result")}
            if any(item.values()):
                searched.append(item)
        elif str(row).strip():
            searched.append({"source": "", "query": str(row).strip(),
                             "result": ""})
    gaps = [str(x).strip() for x in (brief.get("gaps") or [])
            if str(x).strip()]
    notes = str(brief.get("notes") or "").strip()
    out = {"questions": questions, "searched": searched, "gaps": gaps}
    if notes:
        out["notes"] = notes
    if mode == "detail" and not (refs or gaps):
        raise WritingError(
            "细致模式动笔前要交 research_brief：要么给 refs，"
            "要么在 gaps 里说明这一章为什么不需要外部素材")
    return out


#: 正文输出预算：目标字数 ×1.8 + 推理开销，撞上限时翻倍重试。
#: 端点的默认额度经常就是 4096，2500 字的中文正文加推理链必然撞线。
_DRAFT_MIN_TOKENS = 4096
_DRAFT_MAX_TOKENS = 16384
_DRAFT_BUDGET_RETRIES = 2


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
    #: 工作区文件在「更改」里的状态字母（VS Code 式）。写作 git 没有暂存区
    #: （一次提交是整份工作区快照，决策 127），所以不存在 A（已加索引）：
    #: 新文件一律是 U（未跟踪）。
    FILE_STATUS = {
        "changed": ("M", "已修改"),
        "added": ("U", "未跟踪"),
        "removed": ("D", "已删除"),
    }

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
        # 工作区是普通文件，库删了它不会自己走；顺手清掉，别留孤儿目录。
        project_dir = getattr(self.config, "project_dir", None)
        if project_dir is not None:
            try:
                import shutil

                from writegraph import docs as docs_mod
                root = docs_mod.workspace_root(project_dir, work_id)
                if root.exists():
                    shutil.rmtree(root, ignore_errors=True)
            except OSError:
                log.warning("writegraph: 删除工作区失败：%s", work_id)
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

    def effective_writing_mode(self, work_id: str, chapter_index: int,
                               session_mode: str = "standard") -> str:
        """会话模式 + 单章覆盖（决策 161）。"""
        mode = normalize_writing_mode(session_mode)
        try:
            chapter = self.db.get_chapter(work_id, chapter_index)
        except Exception:                            # noqa: BLE001
            chapter = None
        override = str((chapter or {}).get("writing_mode") or "")
        return normalize_writing_mode(override) if override else mode

    def set_chapter_writing_mode(self, work_id: str, chapter_index: int,
                                 mode: str) -> dict[str, Any]:
        """设/清单章覆盖。``inherit`` 清空覆盖。"""
        chapter = self.chapter(work_id, chapter_index)
        wanted = str(mode or "").strip().lower()
        if wanted == "inherit":
            self.db.set_chapter_writing_mode(chapter["id"], "")
            return {"ok": True, "chapter_index": chapter_index,
                    "mode": "inherit"}
        value = normalize_writing_mode(wanted)
        self.db.set_chapter_writing_mode(chapter["id"], value)
        return {"ok": True, "chapter_index": chapter_index, "mode": value}

    def _effective_mode(self, work_id: str, chapter_index: int,
                        session_mode: str) -> str:
        if not work_id:
            return normalize_writing_mode(session_mode)
        return self.effective_writing_mode(work_id, chapter_index,
                                           session_mode)

    def contract(self, chapter_id: str) -> dict[str, Any] | None:
        return self.db.get_contract(chapter_id)

    @staticmethod
    def contract_stats(contract: Mapping[str, Any] | None) -> dict[str, Any]:
        """合同的兑现盘点（只数申报行，不判证据真假）。"""
        return contract_stats(contract)

    def set_contract(self, chapter_id: str, **fields: Any) -> dict[str, Any]:
        try:
            self.db.set_contract(chapter_id, **fields)
        except (ValueError, TypeError) as exc:
            raise WritingError(str(exc)) from exc
        return self.db.get_contract(chapter_id) or {}

    def list_contracts(self, work_id: str) -> list[dict[str, Any]]:
        """整本的合同（写作台「合同」页签）。只读——写合同仍归写作助手。"""
        self._work_or_raise(work_id)
        return self.db.list_contracts(work_id)

    # ── 作品工作区（作品记忆 / 设定 / 计划稿 / 素材）──────────────────────
    # 落盘在 data/writing/works/<work_id>/，并且和正文一起进写作 git：
    # 设定与正文是同一本书，回滚到某个版本时设定也该回到那时（决策 127）。

    def docs_root(self, work_id: str) -> Path:
        work = self._work_or_raise(work_id)
        project_dir = getattr(self.config, "project_dir", None)
        if project_dir is None:
            raise WritingError("没有工作目录配置，工作区不可用")
        try:
            from writegraph import docs as docs_mod
            root = docs_mod.workspace_root(project_dir, work_id)
            return docs_mod.ensure_workspace(root, str(work.get("title") or ""))
        except OSError as exc:
            raise WritingError(f"工作区不可用：{exc}") from exc

    def docs_list(self, work_id: str) -> dict[str, Any]:
        from writegraph import docs as docs_mod
        root = self.docs_root(work_id)
        return {"work_id": work_id, "root": str(root),
                "files": docs_mod.list_files(root),
                "dirs": docs_mod.list_dirs(root)}

    def docs_read(self, work_id: str, path: str) -> dict[str, Any]:
        from writegraph import docs as docs_mod
        root = self.docs_root(work_id)
        try:
            return docs_mod.read_file(root, path)
        except docs_mod.DocError as exc:
            raise WritingError(str(exc)) from exc

    def docs_write(self, work_id: str, path: str, content: str, *,
                   append: bool = False, dedupe: bool = False,
                   check_duplicate: bool = False,
                   log_memory: bool = False) -> dict[str, Any]:
        from writegraph import docs as docs_mod
        from writegraph import memory_guard
        root = self.docs_root(work_id)
        try:
            rel = docs_mod.normalize(path)

            def remember(text: str, op: str, old: str = "") -> None:
                """作者点名直写设定 → 记一条 pending，交给 consolidate 去重。"""
                if not (log_memory and text.strip()
                        and rel.startswith(docs_mod.DOCS_DIR + "/")):
                    return
                try:
                    self.memory_note(
                        work_id, target="setting", path=rel, op=op,
                        text=text, old=old, evidence="agent 直接写入",
                        source="agent")
                except Exception as exc:             # noqa: BLE001
                    log.warning("writegraph: log direct setting failed: %s",
                                exc)

            try:
                prior = docs_mod.read_file(root, rel)["content"]
            except docs_mod.DocError:
                prior = ""

            if check_duplicate:
                dupes = memory_guard.find_content_duplicates(
                    root, content, exclude=rel)
                if dupes:
                    raise WritingError(
                        "内容与已有设定重复：" + "、".join(dupes)
                        + "；先合并或改写，别新建第二份")
            if append:
                try:
                    existing = docs_mod.read_file(root, rel)["content"]
                except docs_mod.DocError:
                    existing = ""
                addition, skipped = memory_guard.append_deduped(
                    existing, content)
                if not addition.strip():
                    row = docs_mod.read_file(root, rel)
                    row.pop("content", None)
                    return {**row, "action": "append", "exists": True,
                            "skipped_duplicates": skipped}
                row = docs_mod.write_file(root, rel, addition, append=True)
                remember(addition, "append")
                return {**row, "action": "append", "exists": False,
                        "skipped_duplicates": skipped}
            row = docs_mod.write_file(root, rel, content)
            remember(content, "replace" if prior.strip() else "append",
                     old=prior if prior.strip() else "")
            return row
        except docs_mod.DocError as exc:
            raise WritingError(str(exc)) from exc

    def docs_create(self, work_id: str, path: str, *, kind: str = "file",
                    content: str = "") -> dict[str, Any]:
        """新建文件或文件夹（页面右键「新建」）。"""
        from writegraph import docs as docs_mod
        if kind not in ("file", "dir"):
            raise WritingError("条目类型只能是 file / dir")
        root = self.docs_root(work_id)
        try:
            if kind == "dir":
                row = docs_mod.create_dir(root, path)
            else:
                row = docs_mod.create_file(root, path, content)
        except docs_mod.DocError as exc:
            raise WritingError(str(exc)) from exc
        return {"work_id": work_id, **row}

    def docs_rename(self, work_id: str, path: str, new_name: str, *,
                    kind: str = "file") -> dict[str, Any]:
        """原地改名：页面只给新名字，父目录不变。"""
        from writegraph import docs as docs_mod
        root = self.docs_root(work_id)
        try:
            row = docs_mod.rename_entry(root, path, new_name, kind=kind)
        except docs_mod.DocError as exc:
            raise WritingError(str(exc)) from exc
        return {"work_id": work_id, **row}

    def docs_delete(self, work_id: str, path: str, *,
                    kind: str = "file") -> dict[str, Any]:
        from writegraph import docs as docs_mod
        root = self.docs_root(work_id)
        try:
            return docs_mod.delete_entry(root, path, kind=kind)
        except docs_mod.DocError as exc:
            raise WritingError(str(exc)) from exc

    def docs_search(self, work_id: str, query: str, *, scope: str = "",
                    limit: int = 10) -> dict[str, Any]:
        """按关键词找 设定 / 素材 / 计划稿 里的条目（决策 129）。"""
        from writegraph import docs as docs_mod
        root = self.docs_root(work_id)
        try:
            return {"work_id": work_id,
                    **docs_mod.search(root, query, scope=scope, limit=limit)}
        except docs_mod.DocError as exc:
            raise WritingError(str(exc)) from exc

    # ── 全局素材库（决策 156）──────────────────────────────────────────────
    # 不属于任何作品、不进写作 git、不自动进上下文。生产主要两条：作者点名
    # 收录，以及写作时召回并实际用上的一段顺手沉淀；消费靠一张连线表，
    # 素材本体始终是文件。

    def materials_root(self) -> Path:
        from writegraph import materials as mat
        project_dir = getattr(self.config, "project_dir", None)
        if project_dir is None:
            raise WritingError("没有工作目录配置，素材库不可用")
        try:
            return mat.ensure_library(mat.library_root(project_dir))
        except OSError as exc:
            raise WritingError(f"素材库不可用：{exc}") from exc

    def materials_list(self) -> dict[str, Any]:
        from writegraph import materials as mat
        root = self.materials_root()
        cards = mat.list_cards(root)
        return {"root": str(root),
                "files": cards,
                "dirs": mat.list_dirs(root),
                # 阅读页收集的待归类卡还堆在 待归类：页面用它显示「整理一下」气泡。
                "pending_count": sum(1 for c in cards if c.get("raw"))}

    def _resolve_material_path(self, path: str = "",
                               material_id: str = "") -> str:
        """素材身份 → 相对路径：read/cook 既认 path 也认 material_id。"""
        rel = str(path or "").strip()
        if rel:
            return rel
        mid = str(material_id or "").strip()
        if not mid:
            return ""
        from writegraph import materials as mat
        return mat.find_by_id(self.materials_root(), mid)

    def _material_path_or_raise(self, path: str = "",
                                material_id: str = "") -> str:
        rel = self._resolve_material_path(path, material_id)
        if rel:
            return rel
        mid = str(material_id or "").strip()
        if mid:
            raise WritingError(f"找不到素材卡：{mid}")
        raise WritingError("缺少 path（也可以给 material_id）")

    def materials_read(self, path: str = "", *,
                       material_id: str = "") -> dict[str, Any]:
        """读一张卡：path 优先；只给素材身份时按身份找路径。"""
        from writegraph import materials as mat
        rel = self._material_path_or_raise(path, material_id)
        try:
            return mat.read_card(self.materials_root(), rel)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_write(self, path: str, content: str) -> dict[str, Any]:
        """页面编辑卡片正文。抬头里的身份行不许删——消费记录靠它。"""
        from writegraph import materials as mat
        root = self.materials_root()
        try:
            old = mat.read_card(root, path)
        except mat.MaterialError:
            old = {}
        mid = str(old.get("material_id") or "")
        if mid and f"素材 {mid}" not in str(content or ""):
            raise WritingError("卡片抬头不能删：身份和消费记录都靠它")
        try:
            return mat.write_text(root, path, content)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_create(self, path: str, *, kind: str = "file",
                         content: str = "") -> dict[str, Any]:
        from writegraph import materials as mat
        if kind not in ("file", "dir"):
            raise WritingError("条目类型只能是 file / dir")
        try:
            root = self.materials_root()
            if kind == "dir":
                return mat.create_dir(root, path)
            return mat.create_file(root, path, content)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_rename(self, path: str, new_name: str, *,
                         kind: str = "file") -> dict[str, Any]:
        from writegraph import materials as mat
        try:
            return mat.rename_entry(self.materials_root(), path, new_name,
                                    kind=kind)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_move(self, path: str, target_dir: str, *,
                       kind: str = "file") -> dict[str, Any]:
        """换分类：作者改主意时不用删了重收，消费连线不动。"""
        from writegraph import materials as mat
        try:
            return mat.move_entry(self.materials_root(), path, target_dir,
                                  kind=kind)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_delete(self, path: str, *, kind: str = "file") -> dict[str, Any]:
        """删除 = 进回收站。素材库没有 git，硬删就真的找不回来了。"""
        from writegraph import materials as mat
        try:
            return mat.recycle(self.materials_root(), path, kind=kind)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_merge(self, path: str, into: str, *,
                        authorized: bool = False) -> dict[str, Any]:
        """把包含型短卡并入长卡；必须先过作者许可门（authorized）。"""
        from writegraph import materials as mat
        try:
            plan = mat.merge_plan(self.materials_root(), path, into)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc
        uses = self.db.material_use_stats(str(plan.get("from_id") or ""))
        plan["uses"] = int(uses.get("events") or 0)
        if plan["uses"]:
            raise WritingError(
                f"短卡已被作品消费 {plan['uses']} 次，V1 不自动合并；"
                "先请作者确认消费连线怎么处理，或保留两张。")
        if not authorized:
            return {"ok": True, "needs_authorization": True, **plan}
        try:
            return mat.merge_cards(self.materials_root(), path, into)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_empty(self, path: str) -> dict[str, Any]:
        """清空文件夹：逐张进回收站，保留文件夹与子分类。"""
        from writegraph import materials as mat
        try:
            return mat.empty_dir(self.materials_root(), path)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_recycled(self) -> dict[str, Any]:
        from writegraph import materials as mat
        root = self.materials_root()
        return {"items": mat.list_recycled(root),
                "summary": mat.recycled_summary(root)}

    def materials_restore(self, recycled: str, *,
                          target: str = "") -> dict[str, Any]:
        from writegraph import materials as mat
        try:
            return mat.restore(self.materials_root(), recycled, target=target)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_purge_recycled(self, *, days: int | None = None) -> dict[str, Any]:
        """显式清理回收站。页面/脚本才调，agent 工具不暴露。"""
        from writegraph import materials as mat
        return mat.purge_recycled(
            self.materials_root(),
            days=mat.RECYCLE_RETENTION_DAYS if days is None else days)

    def materials_audit(self, *, unused_days: int = 90) -> dict[str, Any]:
        """盘点：未消费、近重复、坏抬头、回收站积压。只报告，不改。"""
        from writegraph import materials as mat
        counts = {mid: int(row.get("events") or 0)
                  for mid, row in self.db.material_use_counts().items()}
        data = mat.audit(self.materials_root(), use_counts=counts,
                         unused_days=unused_days)
        # healthy 只回答「有没有需要人看一眼的东西」；近重复与待归类超期
        # 也算，否则 agent 看到 healthy=true 就不会去读候选。
        attention: list[str] = []
        for key, label in (("broken_count", "坏抬头"),
                           ("duplicate_id_count", "身份重复"),
                           ("near_duplicate_count", "近重复候选"),
                           ("identity_mismatch_count", "原文改过")):
            if int(data.get(key) or 0):
                attention.append(f"{label} {int(data.get(key) or 0)}")
        if int((data.get("recycled") or {}).get("overdue") or 0):
            attention.append(
                f"回收站超期 {int(data['recycled']['overdue'])}")
        if int(data.get("pending_over_days") or 0):
            attention.append(
                f"待归类超期 {int(data['pending_over_days'])}")
        data["attention"] = attention
        data["healthy"] = not attention
        return data

    def materials_search(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        from writegraph import materials as mat
        try:
            data = mat.search(self.materials_root(), query, limit=limit)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc
        # 与 search_materials / writing_research 的字段对齐，调用方不用记两套。
        data["count"] = len(data.get("hits") or [])
        return data

    def _books_db_path(self) -> Path:
        return Path(getattr(
            self.config, "books_db_path",
            Path(self.config.project_dir) / "data" / "books.db"))

    def materials_intake(
            self, *, excerpt: str = "", title: str = "",
            category: str = "待归类", tags: Any = "",
            description: str = "", source: str = "author",
            book_title: str = "", book_id: str = "",
            chapter_index: int | None = None,
            chunk_index: int | None = None, chunk_id: str = "",
            para_start: int | None = None, para_end: int | None = None,
            url: str = "", origin: str = "", work_id: str = "",
            session_id: str = "", instruction: str = "",
    ) -> dict[str, Any]:
        """收一张素材卡；能给 chunk 引用就取逐字原文，拿不到才用传入摘录。

        ``para_start`` / ``para_end`` 是块内段落序号（1-based，含两端）：
        阅读工具按 chunk 取原文，但索引粒度到段——作者要的是块里那封信，
        不是整块连带的后文。原文字符串永远从库里切，不接受模型转述。
        """
        from writegraph import materials as mat
        source = str(source or "author").strip() or "author"
        title = str(title or "").strip()
        description = str(description or "").strip()
        book_ref = bool(chunk_id or book_id)
        para_span = ""
        para_total = 0
        if source == "book" or book_ref:
            chunk = mat.read_book_chunk(
                self._books_db_path(), chunk_id=str(chunk_id or ""),
                book_id=str(book_id or ""), chapter_index=chapter_index,
                chunk_index=chunk_index)
            content = str(chunk.get("content") or "") if chunk else ""
            if chunk:
                excerpt = content or excerpt
                book_id = str(chunk.get("book_id") or book_id)
                book_title = str(chunk.get("book_title") or book_title)
                if chunk.get("chapter_index") is not None:
                    chapter_index = int(chunk["chapter_index"])
                if chunk.get("chunk_index") is not None:
                    chunk_index = int(chunk["chunk_index"])
            if para_start or para_end:
                if not content:
                    raise WritingError(
                        "找不到这块原文，没法按段落取；先确认 book_id / 章 / 块")
                try:
                    excerpt, first, last, para_total = mat.paragraph_span(
                        content, para_start, para_end)
                except mat.MaterialError as exc:
                    raise WritingError(str(exc)) from exc
                para_span = (f"第 {first} 段" if first == last
                             else f"第 {first}–{last} 段")
            source_line = f"书《{book_title or '未知'}》"
            if chapter_index is not None:
                source_line += f"第 {int(chapter_index)} 章"
            if chunk_index is not None:
                source_line += f" · 块 #{int(chunk_index)}"
            source_ref = source_line
            if para_span:
                source_line += f" · {para_span}"
        elif source == "web":
            source_line = f"网页 · {url}" if url else "网页"
            source_ref = source_line
        else:
            source_line = "作者自述"
            source_ref = source_line
        if not origin and work_id:
            work = self.db.get_work(str(work_id))
            if work:
                origin = f"作品《{work.get('title') or work_id}》"
        try:
            row = mat.intake(
                self.materials_root(), excerpt=excerpt, title=title,
                category=category or "待归类", tags=tags,
                description=description, source_line=source_line,
                source_ref=source_ref, origin=origin,
                session_id=session_id or self.recorder_session,
                instruction=instruction)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc
        if para_total:
            row["para_span"] = para_span
            row["para_count"] = para_total
        return row

    def materials_intake_raw(
            self, *, excerpt: str = "", book_id: str = "",
            book_title: str = "", chapter_index: int | None = None,
            chunk_index: int | None = None, cfi: str = "",
            instruction: str = "") -> dict[str, Any]:
        """阅读页「收录为素材」：待归类卡直接落 ``待归类/《书名》-时间戳.md``。

        摘录就是选中的那段逐字文字，**不拿 chunk 原文覆盖**（那是 intake 的
        加工语义）；章/块只进来源行，方便以后回查。
        """
        from writegraph import materials as mat
        text = str(excerpt or "").strip()
        if not text:
            raise WritingError("摘录不能为空")
        title = str(book_title or "").strip()
        if not title and book_id:
            title = mat.book_title(self._books_db_path(), str(book_id))
        title = title or "未知书籍"
        chapter = None
        if chapter_index is not None:
            try:
                chapter = int(chapter_index)
            except (TypeError, ValueError):
                chapter = None
        chunk = None
        if chunk_index is not None:
            try:
                chunk = int(chunk_index)
            except (TypeError, ValueError):
                chunk = None
        source_line = f"书《{title}》"
        if chapter is not None:
            source_line += f"第 {chapter} 章"
        if chunk is not None:
            source_line += f" · 块 #{chunk}"
        try:
            return mat.intake_raw(
                self.materials_root(), excerpt=text, book_title=title,
                source_line=source_line, location=cfi,
                origin="阅读页收录", session_id=self.recorder_session,
                instruction=instruction)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_pending(self, *, limit: int = 20) -> dict[str, Any]:
        """待加工的待归类卡列表（最早收的在前）。"""
        from writegraph import materials as mat
        return mat.pending_cards(self.materials_root(), limit=limit)

    def materials_cook(self, path: str = "", *, material_id: str = "",
                       title: str = "", category: str = "待归类",
                       tags: str = "", description: str = "") -> dict[str, Any]:
        """待归类卡 → 已加工卡：补标题/分类/标签/说明，同一张卡换目录。

        「加工这张」时作者常只给素材身份；path 为空也可以按 material_id 找。
        """
        from writegraph import materials as mat
        rel = self._material_path_or_raise(path, material_id)
        try:
            return mat.cook(self.materials_root(), rel, title=title,
                            category=category, tags=tags,
                            description=description)
        except mat.MaterialError as exc:
            raise WritingError(str(exc)) from exc

    def materials_read_chunk(
            self, *, chunk_id: str = "", book_id: str = "",
            chapter_index: int | None = None,
            chunk_index: int | None = None) -> dict[str, Any]:
        """按段落编号读回一块原文：模型抄编号，不数、不转述。"""
        from writegraph import materials as mat
        chunk = mat.read_book_chunk(
            self._books_db_path(), chunk_id=str(chunk_id or ""),
            book_id=str(book_id or ""), chapter_index=chapter_index,
            chunk_index=chunk_index)
        if not chunk:
            cid = str(chunk_id or "").strip()
            hint = ""
            if cid:
                try:
                    rel = self._resolve_material_path("", cid)
                except WritingError:
                    rel = ""
                if rel:
                    hint = (f"；{cid} 是素材卡 id（{rel}），不是书块 id，"
                            "读素材卡请用 action=read material_id=…")
            raise WritingError(
                "找不到这块原文；先确认 book_id / 章 / 块" + hint)
        paras = mat.split_paragraphs(str(chunk.get("content") or ""))
        return {
            "chunk_id": chunk.get("id") or "",
            "book_id": chunk.get("book_id") or "",
            "book_title": chunk.get("book_title") or "",
            "chapter_index": chunk.get("chapter_index"),
            "chunk_index": chunk.get("chunk_index"),
            "count": len(paras),
            "paragraphs": [{"n": i, "text": text}
                           for i, text in enumerate(paras, 1)],
            "hint": "只要其中连续几段时，intake 传 para_start / para_end"
                    "（含两端）；整块收录不传。",
        }

    def materials_record_use(
            self, material_id: str, work_id: str, *, kind: str = "merge",
            chapter_index: int | None = None, target_path: str = "",
            note: str = "", session_id: str = "",
    ) -> dict[str, Any]:
        """记一条消费：kind 只认 shortlist / merge / inline。"""
        kind = str(kind or "merge").strip()
        if kind not in ("shortlist", "merge", "inline"):
            raise WritingError("kind 只能是 shortlist / merge / inline")
        material_id = str(material_id or "").strip()
        if not material_id:
            raise WritingError("缺少 material_id")
        work = self._work_or_raise(str(work_id or ""))
        chapter_id = None
        if chapter_index is not None:
            chapter = self.db.get_chapter(work["id"], int(chapter_index))
            if chapter:
                chapter_id = str(chapter.get("id") or "") or None
        row = self.db.add_material_use(
            material_id, work["id"], kind=kind, chapter_id=chapter_id,
            target_path=str(target_path or ""), note=str(note or ""),
            session_id=str(session_id or self.recorder_session or ""))
        return {"use": row, "stats": self.db.material_use_stats(material_id)}

    def materials_uses(self, *, material_id: str = "", work_id: str = "",
                       limit: int = 100) -> dict[str, Any]:
        return {"uses": self.db.list_material_uses(
            material_id=str(material_id or ""), work_id=str(work_id or ""),
            limit=limit)}

    def materials_promote(self, material_id: str, work_id: str, *,
                          target_path: str = "", note: str = "",
                          chapter_index: int | None = None,
                          session_id: str = "") -> dict[str, Any]:
        """把库里的卡复制进作品 ``素材/``（作品自足，原件留在库里）。

        复制而不是挂链接：库被清理或换机后，这本书的历史仍说得清出处。
        """
        from writegraph import materials as mat
        material_id = str(material_id or "").strip()
        if not material_id:
            raise WritingError("缺少 material_id")
        work = self._work_or_raise(str(work_id or ""))
        root = self.materials_root()
        source_path = mat.find_by_id(root, material_id)
        if not source_path:
            raise WritingError(f"素材库里找不到 {material_id}")
        card = mat.read_card(root, source_path)
        title = str(card.get("title") or "素材")
        target = str(target_path or "").strip()
        if not target:
            name = re.sub(r'[\\/:*?"<>|\s]+', "-", title).strip("-")[:40] or "素材"
            target = f"素材/{name}.md"
        if not target.startswith("素材/"):
            raise WritingError("提升的目标必须放在 素材/ 下")
        from writegraph import docs as docs_mod
        try:
            target_file = docs_mod.resolve(self.docs_root(work["id"]), target)
        except docs_mod.DocError as exc:
            raise WritingError(str(exc)) from exc
        if target_file.exists():
            raise WritingError(f"作品里已有 {target}；换个名字或先处理旧的")
        content = str(card.get("content") or "")
        lines = content.splitlines()
        # 出处行插在抬头块末尾，不是正文里最后一个引用块（原文通常也有 >）。
        insert_at = 0
        in_head = False
        for idx, line in enumerate(lines):
            if line.startswith(">"):
                in_head = True
                insert_at = idx + 1
            elif in_head:
                break
        lines.insert(insert_at, f"> 取自素材库：{source_path}")
        body = "\n".join(lines).rstrip() + "\n"
        try:
            written = self.docs_write(work["id"], target, body)
        except Exception as exc:                     # noqa: BLE001
            raise WritingError(str(exc)) from exc
        use = self.materials_record_use(
            material_id, work["id"], kind="shortlist", target_path=target,
            note=note, chapter_index=chapter_index, session_id=session_id)
        return {"path": written.get("path") or target,
                "material_id": material_id, "source_path": source_path,
                **use}

    def search_materials(self, query: str, *, work_id: str = "",
                         limit: int = 6) -> dict[str, Any]:
        """搜素材：全局库 + 本书 ``素材/``。不扫别的作品（跨库要作者点名）。"""
        from writegraph import materials as mat
        limit = max(1, min(12, int(limit)))
        lib = mat.search(self.materials_root(), query, limit=limit)
        cards: list[dict[str, Any]] = []
        for hit in lib.get("hits") or []:
            cards.append({
                "library": "global",
                "path": hit.get("path"),
                "material_id": hit.get("material_id"),
                "title": hit.get("title"),
                "category": hit.get("category"),
                "tags": hit.get("tags") or [],
                "excerpt": hit.get("excerpt"),
                "description": hit.get("description"),
                "source_line": hit.get("source_line"),
            })
        work_id = str(work_id or "").strip()
        if work_id:
            try:
                found = self.docs_search(work_id, query, scope="素材",
                                         limit=limit)
            except Exception:                        # noqa: BLE001
                found = {}
            for hit in found.get("hits") or []:
                path = str(hit.get("path") or "")
                if not path:
                    continue
                try:
                    row = self.docs_read(work_id, path)
                    card = mat.parse_card(str(row.get("content") or ""), path)
                except Exception:                    # noqa: BLE001
                    card = {}
                cards.append({
                    "library": "work",
                    "path": path,
                    "material_id": card.get("material_id") or "",
                    "title": card.get("title") or hit.get("first_line") or path,
                    "category": mat.category_of(path),
                    "tags": card.get("tags") or [],
                    "excerpt": card.get("excerpt") or hit.get("snippet") or "",
                    "description": card.get("description") or "",
                    "source_line": card.get("source_line") or "",
                })
        # 同一张卡可能既在库里又被提升进作品：按 material_id 去重，库里优先。
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for card in cards:
            key = str(card.get("material_id") or "")
            key = key or f"{card.get('library')}:{card.get('path')}"
            if key in seen:
                continue
            seen.add(key)
            uniq.append(card)
        uniq = uniq[:limit]
        return {
            "source": "materials",
            "recall": "keyword",
            "terms": lib.get("terms") or [],
            "cards": uniq,
            "count": len(uniq),
            "note": ("素材库与本书 素材/ 的结果；仍是候选，不自动进正文。"
                     if uniq else
                     "素材库里没有相关卡片；可以换词，或让作者/你收一段。"),
        }

    # ── 记忆维护（两段式：抽取 → 合并，决策 133）────────────────────

    async def memory_extract(self, work_id: str, chapter_index: int, *,
                             transcript: str = "", delta: Any = None,
                             outcomes: Any = None) -> dict[str, Any]:
        """Phase 1：从一章的现场抽候选，落进 work_memory_log（不碰文件）。"""
        from writegraph import memory_write
        if self.api_client is None:
            raise WritingError("未配置模型，无法抽取记忆")
        chapter = self.chapter(work_id, chapter_index)
        contract = self.db.get_contract(chapter["id"]) or {}
        # agent 不必回传结算结果：成功结算后 DB 里已有 state_delta 与
        # outcomes。显式传入仍优先（比如从旧章补提取）。
        if delta is None:
            summary = self.db.get_summary(chapter["id"])
            if summary and summary.get("state_delta"):
                delta = summary["state_delta"]
        if outcomes is None:
            outcomes = contract.get("outcomes") or None
        user = memory_write.build_extract_input(
            chapter, contract, delta=delta, outcomes=outcomes,
            transcript=transcript)
        system = self._prompt("memory_extract.md")
        data: dict[str, Any] = {}
        for _attempt in range(2):
            raw = await self._complete(system, user,
                                       scenario="writing.memory.extract",
                                       kind="memory_extract", work_id=work_id,
                                       chapter_index=chapter_index)
            data = memory_write.parse_extract(raw)
            if data.get("error") != "parse_failed":
                break
        if data.get("error") == "parse_failed":
            raise WritingError("记忆抽取输出无法解析")
        status = "skipped" if data.get("no_op") else "pending"
        log_id = self.db.add_memory_log(work_id, chapter_index, data,
                                        status=status)
        self._clear_dirty_chapter(work_id, chapter_index)
        return {"ok": True, "log_id": log_id, "status": status,
                "no_op": bool(data.get("no_op")), "extracted": data}

    async def memory_consolidate(self, work_id: str, *,
                                 force: bool = False) -> dict[str, Any]:
        """Phase 2：把待合并记录并进 作品记忆 / 设定，机械闸门判过才落盘。"""
        from writegraph import docs as docs_mod
        from writegraph import memory_guard, memory_write
        pending = self.db.list_memory_logs(work_id, status="pending")
        if not pending and not force:
            return {"ok": True, "status": "nothing_to_merge"}
        root = self.docs_root(work_id)
        state = self.db.get_memory_state(work_id)
        try:
            old = json.loads(str(state.get("manifest") or "{}"))
        except (TypeError, ValueError):
            old = {}
        try:
            old_baseline = json.loads(str(state.get("baseline") or "{}"))
        except (TypeError, ValueError):
            old_baseline = {}
        if not isinstance(old_baseline, Mapping):
            old_baseline = {}
        current = memory_guard.snapshot(root)
        changes = memory_guard.diff_manifest(old, memory_guard.manifest(root))
        diff = memory_guard.diff_snapshot(old_baseline, current)
        audit = memory_guard.audit_workspace(root)
        last = max([int(row.get("chapter_index") or 0)
                    for row in pending] or [0])
        user = memory_write.build_consolidate_input(
            bible=docs_mod.read_bible(root, max_chars=4000),
            settings=docs_mod.dump_section(
                root, docs_mod.DOCS_DIR, max_total=40000),
            pending=pending, changes=changes, diff=diff, audit=audit)
        system = self._prompt("memory_consolidate.md")
        errors: list[str] = []
        applied: dict[str, Any] | None = None
        for _attempt in range(2):
            prompt = user
            if errors:
                prompt += ("\n\n## 上一次的校验错误（必须修正）\n"
                           + "\n".join(f"- {e}" for e in errors))
            raw = await self._complete(
                system, prompt, scenario="writing.memory.consolidate",
                kind="memory_consolidate", work_id=work_id,
                chapter_index=last)
            parsed = memory_write.parse_consolidate(raw)
            if parsed.get("notes") == "parse_failed":
                errors = ["输出不是可解析的 JSON 对象"]
                continue
            checked = memory_guard.validate_ops(parsed, root)
            if checked.ok:
                applied = memory_guard.apply_ops(root, checked)
                if applied.get("errors"):
                    errors = [str(x) for x in applied["errors"]]
                    applied = None
                    continue
                break
            errors = checked.errors
        if applied is None:
            return {"ok": False, "status": "guard_failed", "errors": errors}
        ids = [str(row.get("id") or "") for row in pending]
        self.db.mark_memory_logs(ids, status="consolidated")
        self.db.set_memory_state(work_id,
                                 manifest=memory_guard.manifest(root),
                                 last_chapter=last,
                                 baseline=memory_guard.snapshot(root))
        return {"ok": True, "status": "consolidated",
                "merged": len(pending), "applied": applied}

    async def maintain_memory(self, work_id: str, chapter_index: int, *,
                              transcript: str = "", delta: Any = None,
                              outcomes: Any = None, threshold: int = 3,
                              force: bool = False) -> dict[str, Any]:
        """先抽候选；pending 到阈值或 force 时才合并。失败只回报，不影响主流程。"""
        try:
            extracted = await self.memory_extract(
                work_id, chapter_index, transcript=transcript,
                delta=delta, outcomes=outcomes)
            pending = len(self.db.list_memory_logs(work_id, status="pending"))
            if force or pending >= max(1, int(threshold or 1)):
                merged = await self.memory_consolidate(work_id)
            else:
                merged = {"ok": True, "status": "pending",
                          "pending": pending, "threshold": threshold}
            return {"ok": True, "extract": extracted,
                    "consolidate": merged}
        except Exception as exc:                     # noqa: BLE001
            log.warning("writegraph: memory maintenance failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def memory_note(self, work_id: str, *, target: str = "setting",
                    path: str = "", section: str = "", op: str = "append",
                    text: str = "", old: str = "", evidence: str = "",
                    source: str = "author") -> dict[str, Any]:
        """作者点名/纠正的即时留痕；零 LLM，等下一次 consolidate 合并。"""
        from writegraph import docs as docs_mod
        from writegraph import memory_guard
        target = str(target or "setting").strip()
        op = str(op or "append").strip()
        source = str(source or "author").strip()
        if target not in ("bible", "setting", "plan"):
            raise WritingError("target 只能是 bible / setting / plan")
        if op not in ("append", "replace", "delete"):
            raise WritingError("op 只能是 append / replace / delete")
        if source not in ("author", "agent"):
            raise WritingError("source 只能是 author / agent")
        if not str(text or "").strip() and op != "delete":
            raise WritingError("note 缺少 text")
        if op == "replace" and not str(old or "").strip():
            raise WritingError("replace 需要 old（要改掉的原文）")
        root = self.docs_root(work_id)
        if target == "bible":
            if str(path or "").strip():
                raise WritingError("bible 不应带 path")
            rel = docs_mod.BIBLE_NAME
        else:
            if not str(path or "").strip():
                raise WritingError("setting/plan 需要 path")
            try:
                rel = docs_mod.normalize(path)
            except docs_mod.DocError as exc:
                raise WritingError(str(exc)) from exc
            root_dir = docs_mod.DOCS_DIR if target == "setting" else docs_mod.PLANS_DIR
            if not rel.startswith(root_dir + "/"):
                raise WritingError(f"target={target} 只能写 {root_dir}/")
        digest = memory_guard.note_hash(
            target, section, text, evidence=evidence, old=old)
        existing = self.db.find_memory_log_by_hash(
            work_id, digest, status="pending")
        if existing:
            return {"ok": True, "duplicate": True,
                    "note_id": existing.get("id"),
                    "pending": len(self.db.list_memory_logs(
                        work_id, status="pending"))}
        payload = {"target": target, "path": rel, "section": section,
                   "op": op, "text": text, "old": old,
                   "evidence": evidence}
        note_id = self.db.add_memory_log(
            work_id, 0, payload, status="pending", kind="note",
            source=source, target=target, path=rel, section=section,
            op=op, content_hash=digest)
        return {"ok": True, "duplicate": False, "note_id": note_id,
                "pending": len(self.db.list_memory_logs(
                    work_id, status="pending"))}

    def memory_audit(self, work_id: str) -> dict[str, Any]:
        """机械审计：重复文件/近重复/同主题/作品记忆重复 bullet/pending 重复。"""
        from writegraph import memory_guard
        root = self.docs_root(work_id)
        audit = memory_guard.audit_workspace(root)
        pending = self.db.list_memory_logs(work_id, status="pending",
                                           limit=200)
        by_hash: dict[str, list[str]] = {}
        for row in pending:
            digest = str(row.get("content_hash") or "")
            if digest:
                by_hash.setdefault(digest, []).append(str(row.get("id") or ""))
        duplicates = [ids for ids in by_hash.values() if len(ids) > 1]
        audit["pending"] = len(pending)
        audit["pending_duplicates"] = duplicates
        audit["ok"] = True
        audit["work_id"] = work_id
        return audit

    def _dirty_chapters(self, work_id: str) -> list[int]:
        state = self.db.get_memory_state(work_id)
        raw = state.get("dirty_chapters") or "[]"
        try:
            rows = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            rows = []
        if not isinstance(rows, list):
            return []
        return sorted({int(x) for x in rows if isinstance(x, (int, float))
                       or str(x).lstrip("-").isdigit()})

    def _mark_dirty_chapter(self, work_id: str, chapter_index: int) -> None:
        chapters = self._dirty_chapters(work_id)
        if int(chapter_index) not in chapters:
            chapters.append(int(chapter_index))
            self.db.set_dirty_chapters(work_id, chapters)

    def _clear_dirty_chapter(self, work_id: str, chapter_index: int) -> None:
        chapters = self._dirty_chapters(work_id)
        if int(chapter_index) in chapters:
            chapters.remove(int(chapter_index))
            self.db.set_dirty_chapters(work_id, chapters)

    def memory_reminder(self, work_id: str) -> str:
        """下一轮 user 消息里的「结算了但没抽取」提醒；没有就返回空串。"""
        chapters = self._dirty_chapters(work_id)
        if not chapters:
            return ""
        labels = "、".join(f"第{n}章" for n in chapters[:6])
        more = "（还有更多）" if len(chapters) > 6 else ""
        first = chapters[0]
        return ("<memory-pending>\n"
                f"{labels}{more}已结算但还没抽取记忆。收尾前调 "
                f"writing_memory(action=\"auto\", chapter_index={first}, "
                "transcript=作者这轮原话)；pending≥3 或到章节/弧段边界时再 "
                "consolidate。\n</memory-pending>")

    def memory_status(self, work_id: str) -> dict[str, Any]:
        state = self.db.get_memory_state(work_id)
        pending = self.db.list_memory_logs(work_id, status="pending")
        return {"ok": True, "work_id": work_id,
                "pending": len(pending),
                "last_chapter": int(state.get("last_chapter") or -1),
                "last_consolidated_at": int(
                    state.get("last_consolidated_at") or 0),
                "dirty_chapters": self._dirty_chapters(work_id)}

    def _workspace_section(self, work: Mapping[str, Any]) -> str:
        """system prompt 里的工作区：作品记忆全文 + 分区索引（带未列出计数）。"""
        from writegraph import docs as docs_mod
        try:
            root = self.docs_root(str(work["id"]))
            bible = docs_mod.read_bible(root, max_chars=3000)
            index = docs_mod.overview(root, material_limit=0, plan_limit=0)
        except (WritingError, OSError):
            return ""
        lines = ["### 《" + str(work.get("title") or "未命名") + "》的工作区",
                 "#### 作品记忆",
                 bible.strip() or "（还是空的：用 writing_docs 写第一版）"]
        lines.extend(index)
        lines.append('要正文用 writing_docs(action="read")；找具体条目用 '
                     'writing_docs(action="search")。计划稿与素材不自动进上下文。')
        return "\n".join(lines)

    def _work_docs_block(self, work: Mapping[str, Any], *,
                         context_text: str = "") -> tuple[str, dict[str, Any]]:
        """写作上下文：作品记忆 + 按本章现场选中的设定；空工作区返回空串。"""
        from writegraph import docs as docs_mod
        try:
            root = self.docs_root(str(work["id"]))
            bible = docs_mod.read_bible(root, max_chars=2000)
            docs_text, manifest = docs_mod.select_for_chapter(
                root, context_text=context_text)
        except (WritingError, OSError):
            return "", {}
        parts = []
        if bible:
            parts.append("## 作品记忆\n" + bible)
        if docs_text:
            parts.append(docs_text)
        return "\n\n".join(parts), manifest

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
        raise WritingError(f"找不到存档或分支：{ref}")

    def _commit_ref_or_raise(self, work_id: str,
                             ref: str) -> dict[str, Any]:
        """工具层常拿到短号（日志里就显示 8 位），这里统一解析。"""
        try:
            commit = self._resolve_commit(work_id, ref)
        except WritingError as exc:
            raise WritingError("存档不存在") from exc
        if commit is None or commit.get("work_id") != work_id:
            raise WritingError("存档不存在")
        return commit

    def _resolve_state(self, work_id: str, ref: str) -> dict[str, Any]:
        commit = self._resolve_commit(work_id, ref)
        if commit is None:
            # 空树（还没提交过 / EMPTY）：它不是工作区，读正文只能按「没有」。
            return {"tree": [], "ledger": {}, "commit": None,
                    "files": {}, "files_tracked": False, "worktree": False}
        try:
            tree = json.loads(commit.get("tree_json") or "[]")
            ledger = json.loads(commit.get("ledger_json") or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise WritingError(f"存档内容损坏：{commit.get('id')}") from exc
        tracked = bool(commit.get("files_hash"))
        return {"tree": tree, "ledger": ledger, "commit": commit,
                "files": self.db.get_commit_files(str(commit["id"]))
                if tracked else {},
                "files_tracked": tracked, "worktree": False}

    def _current_state(self, work_id: str) -> dict[str, Any]:
        files = self._workspace_files(work_id)
        return {"tree": self.db.current_tree(work_id),
                "ledger": self.db.snapshot_ledger(work_id), "commit": None,
                "worktree": True,
                "files": vcs.file_manifest(files), "files_tracked": True}

    def _state_file_text(self, work_id: str, state: Mapping[str, Any],
                         path: str) -> str:
        """某个版本里一份文件的正文。工作区读磁盘，提交按 hash 取 blob。

        空树（``commit is None`` 且不是工作区）没有正文：早先拿 ``commit is
        None`` 当「工作区」，结果从 EMPTY 算差异时读的是磁盘，新旧一样，
        新文件的逐行差异永远是空的。
        """
        if state.get("worktree"):
            from writegraph import docs as docs_mod
            try:
                return docs_mod.read_file(self.docs_root(work_id),
                                          path)["content"]
            except (docs_mod.DocError, WritingError, OSError):
                return ""
        digest = (state.get("files") or {}).get(path)
        if not digest:
            return ""
        return self.db.get_file_blob(work_id, str(digest)) or ""

    # ── 工作区文件（作品记忆 / 设定 / 计划稿 / 素材）──────────────────────
    # 它们是普通文件，但和正文同属一本书：提交时一起进图，回滚/切分支时一起
    # 跟着走。状态比较只比 hash 清单，回放时才把正文写回磁盘。

    def _workspace_files(self, work_id: str) -> dict[str, str]:
        """工作区当前内容：``{路径: 正文}``。工作区不可用时按空处理。"""
        from writegraph import docs as docs_mod
        try:
            root = self.docs_root(work_id)
        except (WritingError, OSError):
            return {}
        out: dict[str, str] = {}
        for row in docs_mod.list_files(root):
            try:
                out[str(row["path"])] = docs_mod.read_file(
                    root, str(row["path"]))["content"]
            except docs_mod.DocError:
                continue
        return out

    @staticmethod
    def _file_label(path: str) -> str:
        """版本面板上的名字：不带 .md。"""
        from writegraph import docs as docs_mod
        return docs_mod.label(str(path))

    def _file_change_rows(self, paths: Sequence[str], op: str = ""
                          ) -> list[dict[str, Any]]:
        """文件行。给了 ``op``（status 而不是 diff）就带上状态字母。"""
        letter, label = self.FILE_STATUS.get(op, ("", ""))
        rows = [{"path": p, "label": self._file_label(p), "kind": "file"}
                for p in paths]
        if letter:
            for row in rows:
                row["status"] = letter
                row["status_label"] = label
        return rows

    @staticmethod
    def _files_changed(state: Mapping[str, Any],
                       files: Mapping[str, str] | None) -> bool:
        """合并/回滚后判断文件有没有变化（用于「不会产生新内容」的拦截）。"""
        if files is None:
            return False
        return vcs.file_manifest(files) != state.get("files", {})

    def _materialize_files(self, work_id: str,
                           files: Mapping[str, str]) -> None:
        """把提交里的文件回放到磁盘：写回旧内容，删掉这个版本里没有的。"""
        from writegraph import docs as docs_mod
        root = self.docs_root(work_id)
        current = {str(row["path"]) for row in docs_mod.list_files(root)}
        for path, text in sorted(files.items()):
            old = None
            if path in current:
                try:
                    old = docs_mod.read_file(root, path)["content"]
                except docs_mod.DocError:
                    old = None
            if old != text:
                docs_mod.write_file(root, path, text)
        for path in sorted(current - set(files)):
            try:
                docs_mod.delete_file(root, path)
            except docs_mod.DocError:
                continue

    @staticmethod
    def _commit_tracks_files(commit: Mapping[str, Any] | None) -> bool:
        return bool(commit and commit.get("files_hash"))

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
        file_diff = vcs.diff_files(base["files"], current["files"])
        files_dirty = any(file_diff.values())
        clean = not (changes["added"] or changes["removed"]
                     or changes["changed"] or ledger_dirty or files_dirty)

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
            "chapter_counts": {k: len(v) for k, v in changes.items()},
            "file_changes": {
                op: self._file_change_rows(paths, op)
                for op, paths in file_diff.items()},
            "file_counts": {k: len(v) for k, v in file_diff.items()},
            "files_dirty": files_dirty,
            "ledger_dirty": ledger_dirty,
            "clean": clean,
        }

    #: 版本习惯的中文一行（决策 182）。提示词与工具返回共用同一份措辞。
    GIT_HABIT_LABELS = {"chapter": "每章结算后自动留一版",
                        "manual": "只在作者要求时存档"}

    def git_habit(self, work_id: str) -> str:
        """这本书的版本习惯。旧库/脏值一律退回默认 ``chapter``。"""
        work = self.db.get_work(work_id) or {}
        value = str(work.get("git_habit") or "chapter")
        return value if value in GIT_HABITS else "chapter"

    def set_git_habit(self, work_id: str, habit: str) -> dict[str, Any]:
        """改版本习惯。只应由作者明确要求触发，agent 不自行判断。"""
        self._work_or_raise(work_id)
        try:
            self.db.set_work_git_habit(work_id, habit)
        except ValueError as exc:
            raise WritingError(str(exc)) from exc
        value = self.git_habit(work_id)
        return {"ok": True, "git_habit": value,
                "label": self.GIT_HABIT_LABELS.get(value, value)}

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

    def vcs_file_timeline(self, work_id: str, path: str, *,
                          limit: int = 50) -> dict[str, Any]:
        """一份工作区文件的 git 历史（VS Code 式「时间线」）。

        每个提交只与第一父节点比较，等价于 ``git log -- <path>``。未提交的
        改动作为最上面一条 ``WORK`` 出现，刚改完也能在时间线里看到。
        """
        from writegraph import docs as docs_mod
        self._work_or_raise(work_id)
        try:
            rel = docs_mod.normalize(path)
        except docs_mod.DocError as exc:
            raise WritingError(str(exc)) from exc
        head = self.db.get_head(work_id)
        tip = head.get("commit_id") or ""
        commits = self._commit_map(work_id)
        reachable = vcs.ancestor_ids(commits, tip)
        rows = sorted((commits[cid] for cid in reachable
                       if cid in commits
                       and commits[cid].get("kind") != "stash"),
                      key=lambda c: (int(c.get("time_created") or 0),
                                     str(c.get("id"))),
                      reverse=True)
        entries: list[dict[str, Any]] = []
        for commit in rows:
            if not commit.get("files_hash"):
                continue
            cur = self.db.get_commit_files(str(commit["id"])).get(rel)
            parent = (self.db.get_commit(str(commit.get("parent_id")))
                      if commit.get("parent_id") else None)
            old = None
            if parent and parent.get("files_hash"):
                old = self.db.get_commit_files(str(parent["id"])).get(rel)
            if cur == old:
                continue
            op = "added" if old is None else (
                "removed" if cur is None else "changed")
            item = self._commit_brief(commit) or {}
            item["op"] = op
            entries.append(item)
            if len(entries) >= limit:
                break
        head_files = self.db.get_commit_files(tip) if tip else {}
        disk_hash = None
        try:
            disk = docs_mod.read_file(self.docs_root(work_id), rel)["content"]
            disk_hash = vcs.file_manifest({rel: disk})[rel]
        except (WritingError, docs_mod.DocError, OSError):
            disk_hash = None
        if disk_hash != head_files.get(rel):
            op = ("added" if rel not in head_files
                  else ("removed" if disk_hash is None else "changed"))
            entries.insert(0, {
                "id": "WORK", "short": "WORK", "parent_id": tip,
                "branch": head.get("branch_name") or "main",
                "kind": "work", "author": "user", "message": "未存档的更改",
                "chapter_index": 0,
                "time_created": int(time.time() * 1000),
                "tree_hash": "", "ledger_hash": "",
                "op": op, "is_work": True,
            })
        return {"path": rel, "tracked": bool(tip), "entries": entries}

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
            raise WritingError("没有需要存档的改动")
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
        # 整次提交带上工作区文件；「只提交几章」等于 git 只 add 了正文，
        # 文件沿用 HEAD 的清单（旧提交没跟踪就继续不跟踪）。
        if wanted:
            base_commit = base.get("commit")
            files = (self.db.get_commit_file_contents(
                str(base_commit["id"]))
                if self._commit_tracks_files(base_commit) else None)
        else:
            files = self._workspace_files(work_id)
        branch = head.get("branch_name") or "main"
        commit = self.db.create_commit(
            work_id, tree=tree, ledger=current["ledger"],
            parent_id=head.get("commit_id"), branch_name=branch,
            kind=kind, author=author,
            message=(message or "").strip() or "未命名存档",
            chapter_index=self._max_index(tree), files=files)
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
            raise WritingError("还没有存档可以修正")
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
            log_from=commit["id"], files=self._workspace_files(work_id))
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
            raise WritingError("还没有可以分支的存档")
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
            raise WritingError("分支指向的存档不存在")
        head = self.db.get_head(work_id)
        current_branch = head.get("branch_name") or "main"
        if head.get("commit_id") != target["id"]:
            if not self.vcs_status(work_id)["clean"]:
                if not checkpoint:
                    raise WritingError("工作区有未存档的改动，先存档或允许自动存档")
                self.vcs_commit(
                    work_id,
                    f"切换分支前自动存档（{current_branch}）",
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
                    f"{name} 上还有没合并的存档；确认放弃请用强制删除")
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
            raise WritingError("还没有可以打标签的存档")
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
            raise WritingError("先存档一次，才能暂存改动")
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
            move_head=False, files=self._workspace_files(work_id))
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
            raise WritingError("工作区有未存档的改动，先存档或丢弃再恢复暂存")
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
            raise WritingError("这一章还不在上次存档里，先把整篇存档或删除")
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
        # 旧提交没有文件清单：不动磁盘，避免把作者的工作区清空。
        if target["files_tracked"]:
            self._materialize_files(
                work_id, self.db.get_commit_file_contents(str(commit.get("id"))))
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
        same_files = (not target_state["files_tracked"]
                      or head_state["files"] == target_state["files"])
        if status["clean"] and same_tree and same_ledger and same_files:
            raise WritingError("已经在目标状态上")
        # 脏工作区先检查点；即使目标是 HEAD，也走同一条路——否则检查点
        # 成了 HEAD，回滚完工作区反而显示「有未提交改动」。
        parent = head.get("commit_id")
        if not status["clean"]:
            checkpoint = self.vcs_commit(work_id, "回滚前自动存档",
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
                               for c in current["tree"]), default=0),
            files=self._workspace_files(work_id))
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

    def _merge_files(self, work_id: str, base: Mapping[str, Any],
                     ours: Mapping[str, Any], theirs: Mapping[str, Any],
                     resolutions: Mapping[str, Any]
                     ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """按路径三路合并工作区文件（作品记忆 / 设定 / 计划稿 / 素材）。

        两边都改了同一份：先看裁决，再试正文级三路合并；仍不行报冲突。
        一侧删除、另一侧改动也按冲突处理，和 git 的 modify/delete 一样。
        """
        def content(state: Mapping[str, Any], path: str) -> str | None:
            digest = state["files"].get(path)
            if digest is None:
                return None
            return self.db.get_file_blob(work_id, str(digest))

        def conflict(path: str) -> dict[str, Any]:
            return {"kind": "file", "key": f"file:{path}", "path": path,
                    "label": self._file_label(path),
                    "ours_preview": (content(ours, path) or "")[:160],
                    "theirs_preview": (content(theirs, path) or "")[:160]}

        merged: dict[str, str] = {}
        conflicts: list[dict[str, Any]] = []
        paths = set(base["files"]) | set(ours["files"]) | set(theirs["files"])
        for path in sorted(paths):
            b, o, t = (base["files"].get(path), ours["files"].get(path),
                       theirs["files"].get(path))
            if o == t:
                pick: str | None = o
            elif o == b:
                pick = t
            elif t == b:
                pick = o
            elif o is None or t is None:
                pick = None
                conflicts.append(conflict(path))
            else:
                choice = resolutions.get(f"file:{path}")
                if choice in ("ours", "theirs", "base"):
                    pick = {"ours": o, "theirs": t, "base": b}[choice]
                else:
                    text, ok = vcs.merge3_text(
                        content(base, path) or "",
                        content(ours, path) or "",
                        content(theirs, path) or "")
                    if ok:
                        merged[path] = text
                        continue
                    pick = None
                    conflicts.append(conflict(path))
            if pick is None:
                continue
            text = self.db.get_file_blob(work_id, str(pick))
            if text is not None:
                merged[path] = text
        return merged, conflicts

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
                      files: Mapping[str, str] | None = None,
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
        if files is not None:
            self._materialize_files(work_id, files)
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
        # 三边都跟踪文件才做文件级合并；有旧提交（未跟踪）时保留当前磁盘内容，
        # 不拿一份空清单去覆盖作者刚写的设定。
        if (base["files_tracked"] and ours["files_tracked"]
                and theirs["files_tracked"]):
            merged_files, file_conflicts = self._merge_files(
                work_id, base, ours, theirs, resolutions)
        else:
            merged_files, file_conflicts = (
                self.db.get_file_contents(work_id, ours["files"])
                if ours["files_tracked"] else self._workspace_files(work_id)), []
        conflicts = tree_conflicts + ledger_conflicts + file_conflicts
        if conflicts:
            return {"applied": False, "conflicts": conflicts,
                    "base": self._commit_brief(base.get("commit")) or {},
                    "ours": self._commit_brief(ours.get("commit")) or {},
                    "theirs": self._commit_brief(theirs.get("commit")) or {}}
        final_tree = self._apply_merged(
            work_id, merged_tree, texts, merged_ledger,
            files=merged_files, reason=reason)
        return {"applied": True, "tree": final_tree, "ledger": merged_ledger,
                "files": merged_files, "text_count": len(texts)}

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
            raise WritingError("分支指向的存档不存在")

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
            self._dirty_checkpoint(work_id, f"合并 {branch} 前自动存档")
            self._apply_state(work_id, theirs, reason="merge")
            self.db.upsert_ref(work_id, current, theirs["id"])
            self.db.set_head(work_id, current, theirs["id"])
            self.db.log_head(work_id, branch_name=current,
                             commit_id=theirs["id"], from_commit_id=ours_id,
                             reason="merge-ff")
            return {"fast_forward": True, "commit": None,
                    "status": self.vcs_status(work_id)}

        self._dirty_checkpoint(work_id, f"合并 {branch} 前自动存档")
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
                == vcs.ledger_hash(result["ledger"])
                and not self._files_changed(ours_state, result.get("files"))):
            raise WritingError(f"{branch} 的改动当前分支已经有了，合并不会产生新内容")
        created = self.db.create_commit(
            work_id, tree=result["tree"], ledger=result["ledger"],
            parent_id=ours_id, parent2_id=theirs["id"], branch_name=current,
            kind="merge", author="user",
            message=(message or "").strip() or f"合并 {branch} 到 {current}",
            chapter_index=self._max_index(result["tree"]),
            files=result.get("files"))
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
            raise WritingError("根存档没有可撤销的父状态")
        self._dirty_checkpoint(
            work_id, f"{'撤销' if mode == 'revert' else '摘取'}"
                     f" {vcs.short_id(target['id'])} 前自动存档")
        head = self.db.get_head(work_id)
        ours_id = head.get("commit_id")
        if not ours_id:
            raise WritingError("当前分支还没有存档")
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
                == vcs.ledger_hash(result["ledger"])
                and not self._files_changed(ours_state, result.get("files")))
        if noop:
            raise WritingError(
                "这次存档已经撤销过了" if mode == "revert"
                else "这次存档的改动已经在当前分支上了")
        branch = head.get("branch_name") or "main"
        created = self.db.create_commit(
            work_id, tree=result["tree"], ledger=result["ledger"],
            parent_id=ours_id, branch_name=branch, kind=mode, author="user",
            message=(message or "").strip() or (
                f"撤销 {vcs.short_id(target['id'])}"
                if mode == "revert"
                else f"摘取 {vcs.short_id(target['id'])}"),
            chapter_index=self._max_index(result["tree"]),
            files=result.get("files"))
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
                 file_path: str = "", text: bool = False) -> dict[str, Any]:
        """差异。``from='main...岔路A'`` 按 git 的三点语义：从合并基算起。

        ``file_path`` 给定时只算这一份工作区文件的逐行差异（时间线点击用）。
        """
        self._work_or_raise(work_id)
        file_path = str(file_path or "").strip()
        if file_path:
            from writegraph import docs as docs_mod
            try:
                file_path = docs_mod.normalize(file_path)
            except docs_mod.DocError as exc:
                raise WritingError(str(exc)) from exc
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
            "chapter_counts": {k: len(v) for k, v in changes.items()},
            "files": {
                op: self._file_change_rows(paths)
                for op, paths in vcs.diff_files(
                    base["files"], target["files"]).items()},
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
        if file_path:
            old = self._state_file_text(work_id, base, file_path)
            new = self._state_file_text(work_id, target, file_path)
            from_label = (result["from"] or {}).get("short") or "before"
            to_label = (result["to"] or {}).get("short") or "after"
            result["file_path"] = file_path
            result["text_diff"] = vcs.unified_text_diff(
                old, new, from_label=from_label, to_label=to_label)
            result["changed"] = old != new
        return result

    # ── git 自测：固定的种子作品 ──────────────────────────────────────────

    #: 正文与锚点都写死，agent 才能用「把 X 换成 Y」精确制造差异。
    SELFTEST_CHAPTERS = (
        ("第一章 码头", "甲行：风从码头来。\n乙行：他数着潮声。\n丙行：远处灯灭了。"),
        ("第二章 船期", "丁行：船期写在木牌上。\n戊行：配角没有抬头。"),
        ("第三章 雨", "己行：雨落在篷布上。\n庚行：他把信折好。"),
    )
    SELFTEST_ANCHORS = {
        "甲": "风从码头来", "乙": "他数着潮声", "丙": "远处灯灭了",
        "丁": "船期写在木牌上", "戊": "配角没有抬头",
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
        habit = self.git_habit(work_id)
        lines = [
            f"- 版本习惯：{self.GIT_HABIT_LABELS.get(habit, habit)}"
            f"（{habit}；只有作者明确要求时才改）",
            f"- 分支 {status.get('branch') or 'main'}"
            f"｜HEAD {head.get('short') or '-'}"
            f"「{str(head.get('message') or '')[:36]}」"
            f"｜{'干净' if status.get('clean') else '有未存档改动'}"
            f"｜暂存 {status.get('stash_count', 0)} 条"
        ]
        dirty: list[str] = []
        changes = status.get("changes") or {}
        for key, label in (("added", "新增"), ("changed", "改"),
                           ("removed", "删")):
            for entry in changes.get(key) or []:
                dirty.append(f"第{entry.get('chapter_index')}章{label}")
        if dirty:
            lines.append("- 未存档：" + "、".join(dirty[:6]))
        # 工作区文件也带状态字母（与前端文件区的 M/U/D 同一份）：
        # agent 一眼就知道哪份设定没提交，不必先花一轮 list。
        files = [f"{row.get('status') or ''} {row.get('path')}"
                 for key in ("changed", "added", "removed")
                 for row in (status.get("file_changes") or {}).get(key) or []]
        if files:
            lines.append("- 未存档文件：" + "、".join(files[:6]))
        try:
            commits = self.vcs_log(work_id, limit=commit_limit)
        except Exception:
            commits = []
        if commits:
            lines.append("- 最近存档：")
            for c in commits:
                lines.append(
                    f"  - {c.get('short') or ''} {c.get('kind') or ''}"
                    f"「{str(c.get('message') or '')[:36]}」")
        return lines

    def _previous_contract_misses(self, work_id: str,
                                  chapter_index: int) -> list[str]:
        """上一章结清时没兑现（或少申报）的条目。

        结清不等于做完：未兑现的条目必须有个去处，否则这份合同只是被扔掉了。
        这一行把去处摆到 agent 面前，让它写进本章合同或变成承诺。
        """
        if int(chapter_index or 0) <= 1:
            return []
        prev = self.db.get_chapter(work_id, int(chapter_index) - 1)
        if not prev:
            return []
        contract = self.db.get_contract(prev["id"])
        if not contract or str(contract.get("status") or "active") != "closed":
            return []
        missed = [str(row.get("item") or "")
                  for row in (contract.get("outcomes") or [])
                  if isinstance(row, Mapping)
                  and str(row.get("verdict") or "") in ("miss", "partial")]
        missed.extend(contract_stats(contract)["missing"])
        return [item for item in missed if item]

    def agent_prompt(self, work_id: str = "",
                     chapter_index: int = 0,
                     mode: str = "standard") -> str:
        """对话模式的 system prompt：身份规则 + **当前写作现场**。

        现场信息由服务端给，不让模型先调工具查一遍——与扮演模式
        「你在哪本书里是事实」同一条。

        ``mode`` 是会话级写作子模式；当前章若在 ``chapters.writing_mode``
        上有单章覆盖，以覆盖为准（决策 161）。
        """
        works = self.list_works()
        mode = self._effective_mode(work_id, chapter_index, mode)
        parts = [self._prompt("agent.md")]
        if mode == "detail":
            parts.append(self._prompt("mode_detail.md"))
        if not works:
            parts.append("## 当前写作现场\n还没有任何作品。等作者说书名。"
                         f"\n写作模式：{_mode_label(mode)}")
            return "\n\n".join(parts)

        # 作品目录只报数量，不注入书名与 id：空项目会话里列出别人的书名，
        # 等于把无关作品的第一印象塞进系统提示，模型会自己挑一本代入。
        # 需要清单时由模型调 writing_status（它本来就返回全部作品）。
        parts.append(
            "## 当前写作现场\n"
            f"作品目录不在这里展开（已建 {len(works)} 本，书名与 id 要看时调 "
            "writing_status）。")

        work = self.db.get_work(work_id) if work_id else None
        if work is None:
            # 空项目：作者显式选了「不绑定作品」时，不代入任何一本的现场。
            # 以前这里回退到 works[0]，等于把最近一本的设定偷偷注入，
            # 作者只能靠「新建一本书」才拿到干净的上下文。
            parts.append(
                "### 空项目\n"
                "没有绑定作品：不代入任何一本的书名、设定、章节、工作区或"
                "版本现场。先和作者把想法聊清楚；要落成作品时用 "
                "writing_create_work 建书，或请作者在左栏选一本"
                "（已有作品用 writing_status 看）。")
        else:
            parts.extend(self._work_scene_parts(work, int(chapter_index or 0)))
        parts.append(
            "### 写作模式\n" + _mode_label(mode)
            + "（会话级；单章覆盖见章行）。"
            + ("动笔前要交 research_brief。" if mode == "detail" else ""))
        # 不再单独注入「最近的自测作品」：自测现场（分支 / HEAD / 提交 /
        # 锚点）本来就能用 writing_git(status) 现查，只有作者本人和 Claude
        # 跑自测，把别人的作品现场塞进系统提示没有收益，反而带书名污染。
        return "\n\n".join(parts)

    def memory_delta(self, work_id: str) -> str:
        """上次 consolidate 之后的工作区增删改；没有就返回空串。"""
        from writegraph import memory_guard
        state = self.db.get_memory_state(work_id)
        try:
            old = json.loads(str(state.get("manifest") or "{}"))
        except (TypeError, ValueError):
            old = {}
        if not old:
            return ""
        root = self.docs_root(work_id)
        changes = memory_guard.diff_manifest(old, memory_guard.manifest(root))
        if not any(changes.values()):
            return ""
        lines = ["<workspace-changes>",
                 "上次合并后的工作区有改动："]
        for label, key in (("新增", "added"), ("修改", "changed"),
                           ("删除", "removed")):
            rows = changes.get(key) or []
            if rows:
                lines.append(f"- {label}：" + "、".join(rows[:8])
                             + ("…" if len(rows) > 8 else ""))
        try:
            baseline = json.loads(str(state.get("baseline") or "{}"))
        except (TypeError, ValueError):
            baseline = {}
        if isinstance(baseline, Mapping) and baseline:
            diff = memory_guard.diff_snapshot(
                baseline, memory_guard.snapshot(root), max_chars=800)
            if diff:
                lines.append("改动摘要：\n" + diff[:800])
        lines.append("要看全文用 writing_docs(action=\"read\", path=…)。")
        lines.append("</workspace-changes>")
        return "\n".join(lines)

    def _work_scene_parts(self, work: dict[str, Any],
                          chapter_index: int) -> list[str]:
        """一本书的现场块：章节、当前章合同、工作区、版本现场。

        只在有绑定作品时调用。空项目（``work_id`` 为空）走
        ``agent_prompt`` 的空项目分支，**绝不回退到「最近一本」**——
        回退就是把别人的设定偷偷注入，作者只能靠新建一本书才得到干净上下文。
        """
        parts: list[str] = []
        chapters = self.db.list_chapters(work["id"])
        situation = self._situation_lines(work, chapters, chapter_index)
        if situation:
            parts.append("### 现在在哪\n" + "\n".join(situation))
        if chapters:
            rows = []
            for c in chapters[-12:]:
                contract = self.db.get_contract(c["id"])
                if not contract:
                    mark = "缺合同"
                elif str(contract.get("status") or "active") == "closed":
                    mark = "已结清"
                else:
                    mark = "有合同"
                rows.append(
                    f"- 第{c['chapter_index']}章 {c.get('title') or '未命名'}"
                    f"｜{c.get('status')}｜{c.get('word_count', 0)} 字"
                    f"｜{mark}")
            parts.append(f"### 《{work['title']}》的章节\n" + "\n".join(rows))
        else:
            parts.append(f"### 《{work['title']}》\n还没有章节。")

        target = next((c for c in chapters
                       if c["chapter_index"] == int(chapter_index or 0)), None)
        if target is not None:
            contract = self.db.get_contract(target["id"])
            lines = [f"第{target['chapter_index']}章"
                     f" {target.get('title') or '未命名'}｜"
                     f"{target.get('status')}｜{target.get('word_count', 0)} 字"]
            if contract is None:
                lines.append("合同：还没有。写正文前先给这一章立合同。")
            elif str(contract.get("status") or "active") == "closed":
                lines.append("合同：" + _contract_summary(
                    contract, target["chapter_index"]))
            else:
                lines.append("合同：" + _contract_text(
                    contract, target["chapter_index"]))
            missed = self._previous_contract_misses(
                work["id"], target["chapter_index"])
            if missed:
                lines.append("上一章合同未兑现：" + "、".join(missed[:3])
                             + "（要么写进本章合同，要么转成承诺）")
            open_promises = [p for p in self.db.get_promises(work["id"])
                             if p.get("status") not in ("resolved", "abandoned")]
            if open_promises:
                lines.append("未回收承诺：" + "；".join(
                    str(p.get("title") or p.get("id") or "")
                    for p in open_promises[:5]))
            parts.append("### 作者当前打开\n" + "\n".join(lines))

        workspace = self._workspace_section(work)
        if workspace:
            parts.append(workspace)

        scene = self._git_scene(work["id"])
        if scene:
            parts.append(f"### 《{work['title']}》的版本现场\n"
                         + "\n".join(scene))
        return parts

    def _situation_lines(self, work: Mapping[str, Any],
                         chapters: Sequence[Mapping[str, Any]],
                         chapter_index: int) -> list[str]:
        """「现在在哪」：已有 / 缺 / 可做 / 建议。

        只给事实与可选行为，不强制下一步（决策 142）。作者可能写了两章才
        回头补世界，也可能一直聊不落库——这个块只回答「现在手上有什么、
        下一步缺什么」，不替作者排流水线。
        """
        from writegraph import docs as docs_mod

        pos = int(chapter_index or 0)
        if pos <= 0:
            pos = (int(chapters[-1].get("chapter_index") or 0) + 1
                   if chapters else 1)

        # 工作区计数。读不到就当没有，不阻断现场。
        root = None
        bible_chars = docs_n = plans_n = mats_n = 0
        facets: list[dict[str, Any]] = []
        try:
            root = self.docs_root(str(work["id"]))
            bible_chars = len(docs_mod.read_bible(root, max_chars=1_000_000))
            docs_n = len(docs_mod.section_files(root, docs_mod.DOCS_DIR))
            plans_n = len(docs_mod.section_files(root, docs_mod.PLANS_DIR))
            mats_n = len(docs_mod.section_files(root, docs_mod.MATERIALS_DIR))
            facets = docs_mod.world_facets(root)
        except (WritingError, OSError):
            pass
        world_exists = bool(
            root is not None
            and (root / docs_mod.DOCS_DIR / docs_mod.WORLD_NAME).is_file())

        by_name = {str(f.get("name")): f for f in facets}
        unwritten = [name for name in docs_mod.WORLD_FACETS
                     if name not in by_name
                     or (not by_name[name].get("has_content")
                         and not by_name[name].get("status"))]
        decided = sum(1 for f in facets if f.get("status") == "定")
        open_facets = sum(1 for f in facets if f.get("status") == "悬")
        hidden = sum(1 for f in facets if f.get("status") == "隐")
        author_only = sum(1 for f in facets
                          if f.get("visibility") == "仅作者")

        # 章节与合同：一次取齐，后面对 target / prev 直接查表。
        rows = [(c, self.db.get_contract(str(c.get("id") or "")))
                for c in chapters]
        by_index = {int(c.get("chapter_index") or 0): ct for c, ct in rows}
        drafted = [c for c, _ in rows
                   if int(c.get("word_count") or 0) > 0]
        active = [c for c, ct in rows
                  if ct is not None
                  and str(ct.get("status") or "active") != "closed"]
        closed = [c for c, ct in rows
                  if ct is not None
                  and str(ct.get("status") or "") == "closed"]

        promises = [p for p in self.db.get_promises(work["id"])
                    if p.get("status") not in ("resolved", "abandoned")]
        overdue = [p for p in promises
                   if p.get("planned_payoff_chapter")
                   and int(p["planned_payoff_chapter"]) < pos]

        target = next((c for c in chapters
                       if int(c.get("chapter_index") or 0) == pos), None)
        prev = next((c for c in chapters
                     if int(c.get("chapter_index") or 0) == pos - 1), None)
        target_ct = by_index.get(pos)
        prev_ct = by_index.get(pos - 1)
        prev_unsettled = (
            prev is not None
            and int(prev.get("word_count") or 0) > 0
            and (prev_ct is None
                 or str(prev_ct.get("status") or "active") != "closed"))

        lines: list[str] = []
        lines.append(
            f"- 已有：作品记忆 {bible_chars} 字；设定 {docs_n} 份；"
            f"计划稿 {plans_n}；素材 {mats_n}")
        unwritten_txt = "、".join(unwritten[:3])
        if len(unwritten) > 3:
            unwritten_txt += f" 等 {len(unwritten)} 项"
        if not world_exists:
            world_txt = "还没有 `设定/世界.md`"
        else:
            bits = [f"定 {decided} / 悬 {open_facets} / 隐 {hidden}"]
            if author_only:
                bits.append(f"仅作者 {author_only}")
            if unwritten:
                bits.append("未写：" + unwritten_txt)
            world_txt = "；".join(bits)
        lines.append("- 世界层：" + world_txt)
        ledger = (f"- 账本：章节 {len(chapters)}（正文 {len(drafted)}）；"
                  f"合同 {len(active)} 在写 / {len(closed)} 已结；"
                  f"承诺 {len(promises)} 未回收")
        if overdue:
            ledger += f"（逾期 {len(overdue)}）"
        lines.append(ledger)

        # 上一章的「读者新知道」：优先从摘要第二句取，退回合同 close_note。
        # 这是减法唯一的跨章依据（决策 142），没有就不硬凑。
        reader_known = ""
        prev_summary = ""
        if prev is not None:
            try:
                recent = self.db.recent_summaries(work["id"], pos, limit=1)
            except Exception:                        # noqa: BLE001
                recent = []
            if recent and int(recent[-1].get("chapter_index") or 0) == pos - 1:
                prev_summary = str(recent[-1].get("summary") or "")
        if prev_summary and "读者新知道：" in prev_summary:
            reader_known = prev_summary.split("读者新知道：", 1)[1].strip()
        if not reader_known:
            note = str((prev_ct or {}).get("close_note") or "").strip()
            if note:
                reader_known = note.split("读者新知道：", 1)[-1].strip()
        if reader_known:
            reader_known = reader_known.splitlines()[0].strip()
            if len(reader_known) > 120:
                reader_known = reader_known[:117].rstrip() + "…"
            lines.append("- 读者已知：" + reader_known)

        missing: list[str] = []
        if prev_unsettled:
            missing.append(f"第{pos - 1}章结算")
        if target is not None:
            if target_ct is None:
                missing.append(f"第{pos}章合同")
            elif int(target.get("word_count") or 0) == 0:
                missing.append(f"第{pos}章正文")
            elif str(target_ct.get("status") or "active") != "closed":
                missing.append(f"第{pos}章结算")
        elif chapters:
            missing.append(f"第{pos}章（还没建）")
        else:
            missing.append(f"第{pos}章合同")
        if unwritten:
            missing.append("世界层 " + unwritten_txt)
        if len(missing) > 3:
            missing = missing[:3] + [f"等 {len(missing)} 项"]
        lines.append("- 缺：" + ("；".join(missing) if missing
                                 else "没有明显缺口"))

        can = ["立世界", "立人", "定调", "搭骨", "落章", "找素材"]
        if target is not None:
            if target_ct is None:
                can.append("立合同")
            elif int(target.get("word_count") or 0) == 0:
                can.append("写")
            elif str(target_ct.get("status") or "active") != "closed":
                can.append("结算")
        if drafted:
            can.append("回看")
        lines.append("- 可做：" + " / ".join(dict.fromkeys(can)))

        if prev_unsettled:
            advice = f"第{pos - 1}章还没结算；先结算再写下一章"
        elif target is not None and target_ct is None:
            advice = (f"第{pos}章还没合同；先问三件事"
                      "（结局 / 必须发生 / 不点破），再立合同")
        elif target is not None and int(target.get("word_count") or 0) == 0:
            advice = "合同已立；可以直接写，写前确认「不点破」清单"
        elif (target is not None
              and str(target_ct.get("status") or "active") != "closed"):
            advice = f"第{pos}章还没结算；先结算再写下一章"
        elif not chapters:
            advice = "还没有章节；先和作者定这一章要改变什么，再立合同"
        elif overdue:
            advice = "有承诺逾期；先决定回收、推迟还是放弃"
        elif unwritten:
            advice = (f"世界层还有「{unwritten[0]}」没写；写下一章前补"
                      "最相关的一条，其余边写边长")
        elif promises:
            advice = "按作者当前意图继续；写下一章前看一眼未回收承诺"
        else:
            advice = "按作者当前意图继续；不要为了补全世界层停下来"
        lines.append("- 建议：" + advice)
        return lines

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
            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "work_id": str(work.get("id") or ""),
                "chapter": int(chapter.get("chapter_index") or 0),
                **{k: v for k, v in (diag or {}).items() if k != "card"},
            }
            from log_lifecycle import append_jsonl
            append_jsonl(path, record, max_bytes=2 * 1024 * 1024,
                         backup_count=2)
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
        """作品的参考书架：只定检索范围，不在写稿时自动注入。

        不按本数截断。``mode`` 只为兼容旧调用保留，已不参与写作路径。
        """
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
        profile["reference_books"] = clean
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

    def web_research_enabled(self, work_id: str) -> bool:
        """联网开关（存 ``style_profile``，默认关）。"""
        if not work_id:
            return False
        work = self.db.get_work(work_id)
        if work is None:
            return False
        return bool(self._profile_of(work).get("web_research_enabled", False))

    def set_web_research(self, work_id: str, enabled: bool) -> dict[str, Any]:
        """按作品开关联网；这是作者授权，不是 agent 自己开的。"""
        self._work_or_raise(work_id)
        work = self.db.get_work(work_id) or {}
        profile = self._profile_of(work)
        profile["web_research_enabled"] = bool(enabled)
        self.db.update_work_style_profile(work_id, profile)
        return {"work_id": work_id, "web_research_enabled": bool(enabled)}

    def _web_config(self) -> Any:
        """联网走 DeepSeek：当前主模型是它就用它，否则从控制台连接里找一条。

        写作链路的主模型可能是别家（控制台按调用点绑定），联网不该因此失效；
        但也**不静默换供应商**——找不到 DeepSeek 连接就原样返回，让调用方
        报「当前端点不是 deepseek.com」。
        """
        from services import web_research_service as web_mod

        base = getattr(self.api_client, "config", None) or self.config
        if web_mod.deepseek_endpoint(base) is not None:
            return base
        try:
            from provider_store import get_active_store

            store = get_active_store(self.config)
            if store is not None:
                for provider in store.list_providers():
                    url = str(provider.get("base_url") or "").strip()
                    if "deepseek.com" not in url:
                        continue
                    cfg = SimpleNamespace(
                        api_base=url,
                        api_key=store.reveal_key(
                            str(provider.get("id") or "")))
                    if web_mod.deepseek_endpoint(cfg) is not None:
                        return cfg
        except Exception as exc:                     # noqa: BLE001
            log.warning("writegraph: web provider lookup failed: %s", exc)
        return base

    def _save_web_cards(self, work_id: str, query: str,
                        cards: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """联网卡落盘：有作品进作品 ``素材/网络/``，空项目进全局素材库。"""
        from services import web_research_service as web_mod
        from writegraph import docs as docs_mod

        if not work_id:
            # 空项目没有作品工作区，但素材库是全局的：正好收在这里。
            from writegraph import materials as mat
            return mat.save_web_cards(self.materials_root(), query, cards)
        name = re.sub(r'[\\/:*?"<>|\s]+', "-", (query or "").strip())
        name = name.strip("-")[:40] or "检索"
        path = f"素材/网络/{time.strftime('%Y-%m-%d')}-{name}.md"
        root = self.docs_root(work_id)
        target = root / path
        body = web_mod.cards_markdown(query, [dict(c) for c in cards])
        if target.exists():
            body = "\n" + body
        docs_mod.write_file(root, path, body, append=target.exists())
        return {"saved": path, "saved_paths": [path]}

    def resolve_book_ids(self, names: Sequence[str]) -> list[str]:
        """书名 → 书库 id（先精确再子串）；找不到直接报错，不猜。"""
        from storage.db import BookDB

        wanted = [str(name or "").strip() for name in names]
        wanted = [name for name in wanted if name]
        if not wanted:
            return []
        db = BookDB(self._books_db_path())
        rows = list(db.list_books(limit=500))
        ids: list[str] = []
        for name in wanted:
            row = next((b for b in rows
                        if str(b.title or "").strip() == name), None)
            row = row or next((b for b in rows
                               if name in str(b.title or "")), None)
            if row is None:
                raise WritingError(f"书库里找不到《{name}》")
            ids.append(str(row.id))
        return ids

    async def research_material(
            self, work_id: str, query: str = "", *,
            source: str = "library", limit: int = 6, url: str = "",
            enable_web: bool = False, save: bool = False,
            remember_web: bool = False, book: str = "",
    ) -> dict[str, Any]:
        """找书外素材：书库跨书检索（关键词 / 语义）+（可选）DeepSeek 联网（决策 142 / 148）。

        与 ``writing_references`` 的分工：这里查「一条细节」，不绑定参考书；
        那边只是书架范围。``book`` 把检索限定在一本或几本书里（书名，
        逗号分隔），用于「照《X》的写法」。检索键是需求词，不是本作人名。

        ``source``：``library`` 关键词查书库；``semantic`` 语义近邻查书库；
        ``web`` 只联网；``auto`` 先关键词书库，空了且联网开关已开才走网。
        ``url`` 直接抓页（也受联网开关约束）。联网是作者授权，agent 不能
        自己开——``enable_web`` 只在作者本轮明确要求时传，作品开关存
        ``style_profile.web_research_enabled``。
        """
        from services import web_research_service as web_mod
        from writegraph import reference as reference_mod

        if source not in ("auto", "library", "semantic", "web", "materials"):
            raise WritingError(
                "source 只能是 materials / library / semantic / web / auto")
        query = str(query or "").strip()
        url = str(url or "").strip()
        if not query and not url:
            raise WritingError("检索词不能为空")
        book_names = [name.strip() for name in
                      str(book or "").replace("，", ",").split(",")
                      if name.strip()]
        book_ids = self.resolve_book_ids(book_names) if book_names else []
        # 空项目（没绑定作品）也能查书库与素材库；联网落盘进全局素材库。
        work = self._work_or_raise(work_id) if work_id else None
        if remember_web and enable_web and work is not None:
            # 两个信号都要：单次授权不落库，remember 单独传也不算授权。
            self.set_web_research(str(work["id"]), True)
        allowed = bool(enable_web) or (
            work is not None and self.web_research_enabled(str(work["id"])))
        limit = max(1, min(12, int(limit)))
        books_db = Path(getattr(
            self.config, "books_db_path",
            Path(self.config.project_dir) / "data" / "books.db"))

        if source == "materials":
            if save:
                raise WritingError(
                    "save 只用于联网结果；素材库的结果已经在库里了")
            return self.search_materials(
                query, work_id=str(work["id"]) if work else "",
                limit=limit)

        if source == "auto" and not url and not book_ids:
            # 素材库最便宜，且是作者已经挑过一遍的：先看它。
            # 给了具体网址就跳过：作者要的是抓这一页。
            # 限定了书就不先翻素材库：作者要的是那一本。
            mine = self.search_materials(
                query, work_id=str(work["id"]) if work else "",
                limit=limit)
            if mine.get("count"):
                mine["note"] = ("素材库里已有结果，未查书库，也未联网。"
                                + str(mine.get("note") or ""))
                return mine

        if source == "semantic":
            if save:
                raise WritingError(
                    "save 只用于联网结果；语义近邻来自已入库的书，"
                    "记下书名与章号即可")
            from writegraph import semantic as semantic_mod
            result = await semantic_mod.search(
                self.config, query, limit=limit, books_db=books_db,
                book_ids=book_ids or None)
            if book_names:
                result["book_scope"] = book_names
            return result

        def finish_web(result: dict[str, Any]) -> dict[str, Any]:
            cards = result.get("cards") or []
            if save and cards:
                label = query or str(
                    (cards[0] or {}).get("title") or url)
                saved = self._save_web_cards(
                    str(work["id"]) if work else "", label, cards)
                result.update(saved)
                result["saved_to"] = ("素材库" if work is None
                                      else "作品 素材/网络/")
            return result

        if url:
            if not allowed:
                raise WritingError(
                    "联网抓页需要作者同意：先问作者，或打开作品的联网开关")
            return finish_web(await web_mod.fetch(self._web_config(), url))

        if source == "web":
            if not allowed:
                raise WritingError(
                    "联网需要作者同意：先问作者，或打开作品的联网开关")
            return finish_web(await web_mod.search(
                self._web_config(), query, count=limit))

        exclude: set[str] = set()
        if work is not None:
            facts = self.db.get_facts(str(work["id"]))
            exclude |= {str(f.get("subject_ref") or "") for f in facts}
            exclude |= {str(work.get("title") or "")}
        terms = reference_mod.query_terms(query, exclude=exclude, limit=8)
        cards = (reference_mod.search_library(
            books_db, terms, limit=limit, book_ids=book_ids or None)
            if terms else [])
        lib = {
            "source": "library", "recall": "keyword",
            "terms": terms, "cards": cards, "count": len(cards),
            "note": ("只借写法与细节，不搬专名、设定、情节；引文以出处为准。"
                     if cards else
                     "书库里没有足够相关的段落；换词，或请作者把资料放进 素材/。"),
        }
        if book_names:
            lib["book_scope"] = book_names
        if source == "library":
            return lib

        # auto：书库优先；书库空且联网已授权才走网。
        if cards:
            lib["note"] += "书库已有结果，未联网；要联网可再调 source=web。"
            return lib
        if not allowed:
            lib["note"] = ("书库里没有足够相关的段落。联网需要作者同意："
                           "先问作者，或打开作品的联网开关。")
            return lib
        result = await web_mod.search(self._web_config(), query, count=limit)
        result["note"] += "书库无结果，已改查联网。"
        return finish_web(result)

    def resolve_refs(self, refs: Sequence[Mapping[str, Any]] | None,
                     work_id: str = ""
                     ) -> tuple[list[dict[str, Any]], list[str]]:
        """把 agent 给的素材坐标读回逐字原文。

        读工具语义：agent 指位置（书块坐标 / 素材卡身份 / 作品素材路径），
        服务端负责读文件并渲染；不接受转述、不蒸馏、不跨条合并。读不到的
        坐标原样报回去，让 agent 自己改，不当成静默降级。

        ``material_id`` 只查全局素材库；``path`` 先查全局库，再查**本书**
        ``素材/``（``library=work`` 的检索结果就是这种卡，身份不在全局库里）。
        """
        from writegraph import materials as mat

        root = self.materials_root()
        books_db = self._books_db_path()
        out: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[tuple[Any, ...]] = set()
        for raw in refs or []:
            if not isinstance(raw, Mapping):
                errors.append("refs 里的每一条都要是对象")
                continue
            note = str(raw.get("note") or "")
            mid = str(raw.get("material_id") or "").strip()
            path = str(raw.get("path") or "").strip()
            if mid or path:
                card: dict[str, Any] | None = None
                rel = ""
                origin = "素材库"
                problem = ""
                if mid:
                    rel = mat.find_by_id(root, mid)
                    if rel:
                        try:
                            card = mat.read_card(root, rel)
                        except mat.MaterialError as exc:
                            problem = str(exc)
                if card is None and path:
                    try:
                        card = mat.read_card(root, path)
                        rel = path
                    except mat.MaterialError as exc:
                        problem = problem or str(exc)
                if card is None and path and work_id:
                    # 本书 素材/：作品工作区里的卡没有全局素材库身份。
                    try:
                        row = self.docs_read(work_id, path)
                    except WritingError:
                        row = None
                    if row is not None:
                        card = mat.parse_card(
                            str(row.get("content") or ""), path)
                        card["excerpt"] = (card.get("excerpt")
                                           or str(row.get("content") or ""))
                        rel = path
                        origin = "作品素材"
                if card is None:
                    errors.append(problem or f"素材库里找不到 {mid or path}")
                    continue
                key = ("material", str(card.get("material_id") or rel))
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "kind": "material",
                    "material_id": str(card.get("material_id") or ""),
                    "label": origin + " · " + (
                        str(card.get("source_line") or "")
                        or str(card.get("title") or rel)),
                    "text": str(card.get("excerpt") or ""),
                    "note": note,
                })
                continue
            chunk_id = str(raw.get("chunk_id") or "").strip()
            book_id = str(raw.get("book_id") or "").strip()
            chapter_index = raw.get("chapter_index")
            chunk_index = raw.get("chunk_index")
            if not chunk_id and not (book_id and chapter_index is not None
                                     and chunk_index is not None):
                errors.append("每条 ref 要给 material_id / path，"
                              "或 chunk_id / book_id + 章 + 块")
                continue
            row = mat.read_book_chunk(
                books_db, chunk_id=chunk_id, book_id=book_id,
                chapter_index=chapter_index, chunk_index=chunk_index)
            if not row:
                errors.append("找不到原文块：" + (
                    chunk_id or f"{book_id}#{chapter_index}/{chunk_index}"))
                continue
            text = str(row.get("content") or "")
            label = (f"书《{row.get('book_title') or '未知'}》"
                     f"第 {int(row.get('chapter_index') or 0)} 章"
                     f" · 块 #{int(row.get('chunk_index') or 0)}")
            para_start = raw.get("para_start")
            para_end = raw.get("para_end")
            if para_start or para_end:
                try:
                    text, first, last, _total = mat.paragraph_span(
                        text, para_start, para_end)
                except mat.MaterialError as exc:
                    errors.append(str(exc))
                    continue
                label += (f" · 第 {first} 段" if first == last
                          else f" · 第 {first}–{last} 段")
            key = ("book", str(row.get("id") or ""),
                   int(para_start or 0), int(para_end or 0))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "kind": "book",
                "label": label,
                "text": text,
                "note": note,
                "book_id": str(row.get("book_id") or ""),
                "chapter_index": row.get("chapter_index"),
                "chunk_index": row.get("chunk_index"),
                "chunk_id": str(row.get("id") or ""),
            })
        return out, errors

    # ── 上下文装配 ────────────────────────────────────────────────────────

    def assemble(self, work_id: str, chapter_index: int, *,
                 focus_refs: Sequence[str] | None = None,
                 budget: int | None = None,
                 style_ref: Mapping[str, Any] | None = None,
                 refs: Sequence[Mapping[str, Any]] | None = None,
                 mode: str = "standard"
                 ) -> dict[str, Any]:
        work = self.db.get_work(work_id)
        if work is None:
            raise WritingError("作品不存在")
        chapter = self.chapter(work_id, chapter_index)
        contract = self.db.get_contract(chapter["id"])
        if contract is None:
            raise WritingError("本章还没有合同，先写合同再生成")

        # 覆盖草稿前留检查点：生成失败/不满意时可以回到原稿。
        self._auto_commit(work_id, f"生成第{chapter_index}章前的自动存档")

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

        terms = lens_mod.query_terms(query)
        material_evidence = lens_mod.material_evidence(ranked, terms)
        blocks = {
            "chapter_contract": _contract_text(contract, chapter_index),
            "facts_current": _facts_text(current),
            "prev_tail": prev_tail,
            "context_card": card["text"],
            "reference_material": material_evidence["text"],
        }
        ref_items, ref_errors = self.resolve_refs(refs, work_id=work_id)
        if ref_errors:
            raise WritingError("有些素材引用读不到：" + "；".join(ref_errors[:6]))
        ref_text = reference_mod.render_refs(ref_items)
        if ref_text:
            blocks["external_reference"] = ref_text
        ref_diag: dict[str, Any] = {
            "mode": "refs",
            "count": len(ref_items),
            "chars": len(ref_text),
            "items": [{"kind": item.get("kind"), "label": item.get("label")}
                      for item in ref_items],
        }

        # 作者的工作区（作品记忆 + 设定）。放不进预算时整块退让：
        # 合同与账本是正典，设定是补充。设定只带这一章用得上的。
        docs_context = "\n".join([
            str(chapter.get("title") or ""),
            _contract_text(contract, chapter_index),
            str(blocks.get("facts_current") or ""),
            str(blocks.get("prev_tail") or ""),
            str(blocks.get("context_card") or ""),
        ])
        docs_text, docs_manifest = self._work_docs_block(
            work, context_text=docs_context)
        if docs_text:
            blocks["work_docs"] = docs_text

        style_info: dict[str, Any] | None = None
        style = self._resolve_style_ref(work, style_ref)
        if style is not None:
            voice_text, style_info = self._build_author_voice(
                work, contract, style)
            if voice_text:
                blocks["author_voice"] = voice_text
        profile = budget_mod.profile_for(mode)
        compiled = budget_mod.compile_blocks(
            blocks, budget=budget or profile["total"],
            specs=profile["specs"])
        result = {
            "chapter_index": chapter_index,
            "text": compiled["text"],
            "tokens": compiled["tokens"],
            "manifest": compiled["manifest"],
            "decision": compiled["decision"],
            "card": card,
            "references": ranked[:10],
            "material_evidence": material_evidence,
            "reference": ref_diag,
            "work_docs": docs_manifest,
        }
        if style_info is not None:
            result["style"] = style_info
        return result

    # ── 写与结算 ──────────────────────────────────────────────────────────

    async def stream_chapter(self, work_id: str, chapter_index: int, *,
                             extra_instruction: str = "",
                             style_ref: Mapping[str, Any] | None = None,
                             refs: Sequence[Mapping[str, Any]] | None = None,
                             mode: str = "standard",
                             research_brief: Mapping[str, Any] | None = None,
                             plan_first: bool = False,
                             cancel_event=None) -> AsyncIterator[dict[str, Any]]:
        """流式写一章。事件：``stage`` / ``delta`` / ``done`` / ``error``。

        ``refs`` 是 agent 本轮选定的素材坐标；服务端只按坐标读回逐字
        原文。不再在这里替 agent 召回或蒸馏——找素材是 agent 的事。

        ``mode`` 是本次动笔的有效模式（会话模式 + 单章覆盖，由调用方算好）。
        细致模式要求 ``research_brief``：只验形状，不验质量（决策 161）。
        """
        if self.api_client is None:
            raise WritingError("未配置模型，无法生成正文")
        mode = normalize_writing_mode(mode)
        brief = validate_research_brief(research_brief, refs, mode)
        chapter = self.chapter(work_id, chapter_index)
        contract = self.db.get_contract(chapter["id"])
        work = self.db.get_work(work_id) or {}
        profile = self._profile_of(work)          # auto_repair 等作品开关
        assembled = self.assemble(
            work_id, chapter_index, style_ref=style_ref, refs=refs,
            mode=mode)
        self._log_reference_diag(work, chapter,
                                 assembled.get("reference") or {})
        yield {"event": "stage",
               "data": {"stage": "assembled", "tokens": assembled["tokens"],
                        "manifest": assembled["manifest"],
                        "decision": assembled.get("decision"),
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
        if plan_first:
            # 决策 176：只排计划，不写正文。细纲失败就整轮失败，
            # 不静默跳过、不假装按细纲生成。
            yield {"event": "stage", "data": {"stage": "outline"}}
            try:
                outline_text = await self._complete(
                    outline_mod.INSTRUCTION, assembled["text"],
                    scenario="writing.outline", kind="outline",
                    work_id=work_id, chapter_index=chapter_index,
                    max_tokens=1200)
            except Exception as exc:                 # noqa: BLE001
                raise WritingError(f"短细纲生成失败：{exc}") from exc
            outline_text = outline_text.strip()
            if not outline_text:
                raise WritingError("短细纲为空，未继续写正文")
            user_content += "\n\n" + outline_mod.render_block(outline_text)
            yield {"event": "stage",
                   "data": {"stage": "outlined", "chars": len(outline_text)}}
        if extra_instruction.strip():
            # 作者本轮的额外要求不是合同，放在最后、标明优先级低于正典。
            user_content += ("\n\n## 作者本轮要求（不覆盖正典与合同）\n"
                             + extra_instruction.strip())
        messages = [{"role": "user", "content": user_content}]

        # 输出预算：目标字数 ×1.8 + 推理开销。撞上限时翻倍重来——
        # 同样的预算再问一遍只会再撞一次（决策 62 的截断判据）。
        budget = max(_DRAFT_MIN_TOKENS, int(target * 1.8) + 1200)
        buf: list[str] = []
        visible = ThinkStreamFilter()
        for attempt in range(_DRAFT_BUDGET_RETRIES + 1):
            buf.clear()
            visible = ThinkStreamFilter()
            # 每次尝试都重发 writing：调用方据此把已收字数清零。
            yield {"event": "stage",
                   "data": {"stage": "writing", "attempt": attempt + 1,
                            "budget": budget}}
            try:
                with usage_log.source_scope("writing"):
                    async for delta, _tools, _usage in self.api_client.stream(
                            system, messages, [], scenario="writing.draft",
                            max_tokens=budget):
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        if delta:
                            buf.append(delta)  # 原始流（含思维链）留给飞行记录仪
                            shown = visible.feed(delta)
                            if shown:
                                yield {"event": "delta", "data": {"text": shown}}
            except StreamTruncated as exc:
                if attempt >= _DRAFT_BUDGET_RETRIES:
                    raise WritingError(str(exc)) from exc
                budget = min(_DRAFT_MAX_TOKENS, budget * 2)
                log.warning("writegraph: draft truncated, retry with %d", budget)
                yield {"event": "stage",
                       "data": {"stage": "retrying", "budget": budget}}
                continue
            break
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
        # 取舍说明跟这一稿一起存：审稿时看得到「声明要借什么、还缺什么」。
        # 只入库，不进写作 git（过程元数据不是正典）。
        payload = dict(brief)
        payload["reference"] = assembled.get("reference") or {}
        try:
            self.db.add_chapter_brief(
                work_id, chapter["id"], chapter_index, mode, payload,
                ref_count=int((assembled.get("reference") or {}).get("count")
                              or 0))
        except Exception as exc:                     # noqa: BLE001
            log.warning("writegraph: brief persist failed: %s", exc)
        yield {"event": "done",
               "data": {"chapter_index": chapter_index, "mode": mode,
                        "content": content, **saved}}

    def _subject_state_block(self, facts_rows: Sequence[Mapping[str, Any]],
                             chapter_index: int) -> str:
        """结算上下文里的「核心角色状态」。

        与角色卡同一套求值（``facts.current`` + ``group_by_subject``），
        但只列主体与字段值：模型要的是**能当 subject_ref 用的 id**，
        不是叙事文本。空账本必须明说，否则模型只能自造 id。
        """
        current = facts_mod.current(facts_rows, chapter_index)
        grouped = facts_mod.group_by_subject(current)
        if not grouped:
            return ("（还没有任何主体。本章新登场的人物/地点/物品一律放进 "
                    "entity_ops 的 create，不要自造 subject_ref。）")
        lines: list[str] = []
        for ref in sorted(grouped)[:30]:
            items = {str(r.get("field")): r for r in grouped[ref]}
            kind = str(grouped[ref][0].get("subject_type") or "")
            parts = [f"{name}：{facts_mod.describe(items[name])}"
                     for name in facts_mod.CARD_FIELDS if name in items]
            head = f"- {ref}" + (f"（{kind}）" if kind else "")
            lines.append(head + (" —— " + "；".join(parts) if parts else ""))
        return "\n".join(lines)

    async def settle(self, work_id: str, chapter_index: int, *,
                     outcomes: Sequence[Mapping[str, Any]] | None = None,
                     note: str = "") -> dict[str, Any]:
        """结算：模型交 delta，宿主校验后落库。整份拒绝，不做部分接受。

        结算成功 = 这一章结束：合同一并结清（``outcomes`` 是 agent 申报的
        逐条兑现结果），之后它只以一行状态留在系统提示词里。
        """
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
        # settle.md 要求 subject_ref 只能用「核心角色状态」里的 id，可这一段
        # 以前根本没装配进来：模型看不到任何合法 id，只能自造（2026-10-29
        # 18:55 的会话里它造了 tmp-char-1，校验必拒）。
        user += "\n\n## 核心角色状态（subject_ref 只能用这里的 id）\n" \
                + self._subject_state_block(existing_facts, chapter_index)
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
        # 结算成功 = 正文与账本都稳定了。是否顺手留一版由作品习惯决定
        # （决策 182）：默认每章一版；作者说「我自己管版本」后不再自动提交。
        habit = self.git_habit(work_id)
        committed = None
        if habit == "chapter":
            committed = self._auto_commit(
                work_id, f"第{chapter_index}章结算",
                kind="auto", author="agent")
        contract = self.db.close_contract(
            chapter["id"], outcomes=outcomes, note=note)
        self._mark_dirty_chapter(work_id, chapter_index)
        return {"ok": True, **applied, "delta": checked.delta,
                "git": {"habit": habit, "committed": committed is not None},
                "contract": (None if contract is None else {
                    "status": contract.get("status"),
                    "summary": _contract_summary(contract, chapter_index),
                    "stats": contract_stats(contract)})}

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
        brief = self.db.latest_chapter_brief(chapter["id"])
        if brief:
            payload = brief.get("payload") or {}
            lines = []
            if payload.get("questions"):
                lines.append("问题：" + "；".join(payload["questions"]))
            for row in payload.get("searched") or []:
                lines.append(
                    f"查过：{row.get('source') or '?'}｜{row.get('query') or ''}"
                    f" → {row.get('result') or ''}")
            if payload.get("gaps"):
                lines.append("没找到 / 需作者定：" + "；".join(payload["gaps"]))
            ref = payload.get("reference") or {}
            if ref.get("count"):
                labels = [str(i.get("label") or "")
                          for i in (ref.get("items") or [])][:6]
                lines.append(f"声明要借的素材（{ref.get('count')} 条）："
                             + "；".join(x for x in labels if x))
            if lines:
                parts.append("## 本章素材取舍（动笔前声明）\n"
                             + "\n".join(f"- {x}" for x in lines))
        return "\n\n".join(parts)

    def _goal_author_sources(self, work_id: str) -> list[str]:
        """审稿用的作者原文：作品记忆 + 全部设定，只用来找必现标记。"""
        from writegraph import docs as docs_mod
        try:
            root = self.docs_root(work_id)
            return [docs_mod.read_bible(root, max_chars=6000),
                    docs_mod.dump_section(root, "设定", max_total=40000)]
        except (WritingError, OSError, docs_mod.DocError):
            return []

    async def review_chapter(self, work_id: str,
                             chapter_index: int) -> dict[str, Any]:
        chapter = self.chapter(work_id, chapter_index)
        content = str(chapter.get("content") or "")
        if not content.strip():
            raise WritingError("正文为空，无法审稿")
        context = self._review_context(work_id, chapter_index)
        contract = self.db.get_contract(chapter["id"])
        goals = goals_mod.freeze_goals(
            contract, chapter_index,
            author_sources=self._goal_author_sources(work_id))
        goal_block = goals_mod.render_prompt_block(goals)
        user_message = "\n\n".join(
            part for part in (context, goal_block, f"## 本章正文\n{content}")
            if part)
        raw = await self._complete(
            self._prompt("review.md"), user_message,
            scenario="writing.review", kind="review",
            work_id=work_id, chapter_index=chapter_index)
        parsed = loads_tolerant(raw, expect=dict)
        if not isinstance(parsed, dict):
            raise WritingError("审稿输出不是 JSON 对象")
        result = review_mod.normalize_review(parsed, content)
        goal_review = goals_mod.normalize_goal_reviews(
            parsed.get("goalReviews", parsed.get("goal_reviews")), goals,
            content)
        result["goal_review"] = goal_review
        result["goal_summary"] = goals_mod.summary_line(goal_review)
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
        self._auto_commit(work_id, f"第{chapter_index}章修订前自动存档")
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
                        work_id: str = "", chapter_index: int = 0,
                        max_tokens: int | None = None) -> str:
        if self.api_client is None:
            raise WritingError("未配置模型，无法结算")
        buf: list[str] = []
        kwargs: dict[str, Any] = {"scenario": scenario}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        with usage_log.source_scope("writing"):
            async for delta, _tools, _usage in self.api_client.stream(
                    system, [{"role": "user", "content": user}], [],
                    **kwargs):
                if delta:
                    buf.append(delta)
        raw = "".join(buf)
        self._record_llm(kind=kind, work_id=work_id,
                         chapter_index=chapter_index, system=system,
                         user=user, response=raw)
        # 思维链不进 JSON：不剥掉的话 loads_tolerant 会在 <think> 上崩。
        return strip_think(raw)


def contract_stats(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """合同的兑现盘点：申报了几条、几条兑现/勉强/未兑现、哪几条没申报。

    只数 ``outcomes`` 里的行，不核对证据是否真在正文里——那是之后校验器的事，
    这里先让「做了什么」可读。
    """
    rows = list((contract or {}).get("outcomes") or [])
    items = [str(x) for x in ((contract or {}).get("must_hit_now") or [])]
    counts = {"hit": 0, "partial": 0, "miss": 0}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        verdict = str(row.get("verdict") or "")
        if verdict in counts:
            counts[verdict] += 1
        item = str(row.get("item") or "").strip()
        if item:
            seen.add(item)
    return {"total": len(items), **counts, "declared": len(rows),
            "missing": [item for item in items if item not in seen]}


def _contract_summary(contract: Mapping[str, Any], chapter_index: int) -> str:
    """已结清合同在系统提示词里的一行摘要。全文不再进来——写完了就不该占现场。"""
    stats = contract_stats(contract)
    head = f"第{chapter_index}章合同已结清"
    if stats["total"]:
        head += f"｜兑现 {stats['hit']}/{stats['total']}"
        if stats["partial"]:
            head += f"｜勉强 {stats['partial']}"
        if stats["miss"]:
            head += f"｜未兑现 {stats['miss']}"
        if stats["missing"]:
            head += f"｜未申报 {len(stats['missing'])}"
    note = str(contract.get("close_note") or "").strip()
    if note:
        head += "｜" + note[:40]
    return head


def _contract_text(contract: Mapping[str, Any], chapter_index: int) -> str:
    lines = [f"第{chapter_index}章合同"]
    arc = str(contract.get("arc_ref") or "").strip()
    if arc:
        lines.append(f"- 弧线：{arc}")
    fields = (("目的", "purpose"), ("独占事件", "exclusive_event"),
              ("章末状态", "ending_state"),
              ("下场可承接", "next_entry_state"))
    for label, key in fields:
        if contract.get(key):
            lines.append(f"- {label}：{contract[key]}")
    for label, key in (("必须兑现", "must_hit_now"),
                       ("必须保留", "must_preserve"),
                       ("禁止触碰", "forbidden_cross"),
                       ("不点破", "withhold")):
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
