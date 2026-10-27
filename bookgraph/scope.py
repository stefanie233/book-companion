"""叙事域（narrative scope）—— 让「短篇集 / 散文集」的每一篇各自成一个世界。

问题（用户原话）：短篇小说集会**跨章节串流**，导致文本污染 —— 提取器把第 1 篇
的人物、台词、事件带到第 3 篇去。

诊断（2026-10-10 实测，**推翻了设计稿的一条前提**）：

| 来源 | 设计稿的用法 | 实测 |
|---|---|---|
| Scout 的 `narrative_units` | 「它已读过全书，优先采用」 | ❌ **它是情节节拍，不是世界**。窄门 8 个单元＝序幕/发展/高潮/尾声；人类简史 4 个＝四次革命；大棋局 7 个＝各章。拿它划域会把每一本长篇小说按幕切开，跨幕连线全断 |
| `book_chapters` 顶层单元 | 机械先验 | ✅ `level=0 且 in_toc=1` 的单元正是「篇」 |
| 书名 + 作者 | —— | ❌ 判不出集子还是长篇 |

所以域由**两份判断共同**决定，缺一不可：

1. **语义裁决**：`context_setter` 拿顶层单元标题（等于目录）判 `narrative_structure`
   ∈ {single, collection}。它本来就是「世界观边界设定者」，这是同一类问题；
   而它原来只拿得到书名+作者，加目录对它的**全部**输出都是净增益。
2. **机械守卫**：顶层单元的标题若多数带序号（第X章 / 第X部分 / 上篇 / （一）），
   说明它们是**同一部作品的分章**而非独立篇目 —— 此时**以机械判断为准**，
   并把冲突报出来（不偷偷选一边，同 `selection.py` 的约定）。

两份都点头才划域。任何一份缺席 → **全书单域**，长篇的行为完全不变（惰性）。

纯函数：不碰库、不调 LLM。调用方把章节表、块表、语义裁决查出来传进来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bookgraph.selection import NON_BODY_TYPES

# 语义裁决的两个取值（context_setter 的 narrative_structure 字段）。
SINGLE = "single"
COLLECTION = "collection"

# 顶层单元：level == 0（最外层）且 in_toc == 1（命中书目录）。
# -1 是「无从判断」，不参与 —— 拿不准就不动它。
TOP_LEVEL = 0
IN_TOC = 1

# 带序号的标题。用于机械守卫：**独立篇目**的名字是「秋天的怀念」，
# **同一部作品的分章**才叫「第三章」「第一部分」「上篇」「（二）」。
_ORDINAL_RE = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章回节篇部卷讲集]"
    r"|(?:上|中|下)\s*[篇部卷]"
    r"|[(（]\s*[0-9一二三四五六七八九十]+\s*[)）]"
    # 整个标题就是一个数字 —— 学术书的「一 / 二 / 三」分节，绝不是篇名。
    # 实测来源：反杜林论（引论 / 一 / 二 / 三）。注意别误伤「记忆与印象1」。
    r"|[0-9一二三四五六七八九十百零〇两]+\s*$)"
)

# 带序号的单元占比达到这个线，就认定「这是一部作品的分章」。
# 阈值刻意偏低：误判成「单域」只是退回改动前的行为，误判成「多域」却会把
# 长篇小说按幕切开、跨幕连线全断 —— 两边代价不对称，所以要偏保守。
# 实测锚点：我与地坛 0/11（真集子）；反杜林论 3/6（引论/一/二/三，分节）。
ORDINAL_MAJORITY = 0.5

# 单个单元最多能占全书正文的比例。集子的每一篇都完整而体量相当；
# 若有一个单元吞掉半本书，那是「分节」不是「篇」。
# 实测来源：反杜林论（引论 / 一 / 二 / 三，其中「三」吞掉全书其余部分）。
MAX_UNIT_SHARE = 0.5


@dataclass(frozen=True)
class Scope:
    """一个叙事世界。``chunk_ids`` 是它的全部成员，**批次不得跨域**。"""

    scope_id: str
    label: str
    chapter_indices: tuple[int, ...]
    chunk_ids: tuple[str, ...]
    source: str = ""

    @property
    def is_whole_book(self) -> bool:
        return self.source == SINGLE or self.source == "fallback"


@dataclass
class ScopePlan:
    """域划分结果。``by_chunk`` 是批次构造与索引过滤的唯一依据。"""

    scopes: list[Scope] = field(default_factory=list)
    by_chunk: dict[str, str] = field(default_factory=dict)   # chunk_id -> scope_id
    source: str = SINGLE          # "collection" | "single" | "fallback"
    reason: str = ""
    conflicts: list[str] = field(default_factory=list)
    units: list[dict] = field(default_factory=list)          # 机械先验，供报告

    @property
    def is_split(self) -> bool:
        return len(self.scopes) > 1

    def scope_of(self, chunk_id: str) -> str:
        return self.by_chunk.get(chunk_id, "")

    def get(self, scope_id: str) -> Scope | None:
        for s in self.scopes:
            if s.scope_id == scope_id:
                return s
        return None

    def summary(self) -> str:
        if not self.is_split:
            return f"单域（{self.source}）：{self.reason}"
        return (f"{len(self.scopes)} 域（{self.source}）："
                f"{'、'.join(s.label for s in self.scopes[:4])}"
                + ("…" if len(self.scopes) > 4 else ""))


def _attr(row, key: str):
    if isinstance(row, dict):
        return row.get(key)
    if hasattr(row, "keys"):
        return row[key] if key in row.keys() else None
    return getattr(row, key, None)


def top_level_units(chapters) -> list[dict]:
    """机械先验：顶层正文单元（= 目录里的「篇」）。

    `level == 0 and in_toc == 1` 且体例不是非正文。版权页/目录页也是
    level=0 & in_toc=1，靠 `NON_BODY_TYPES` 摘掉。
    """
    out: list[dict] = []
    for ch in chapters or []:
        if _attr(ch, "level") != TOP_LEVEL or _attr(ch, "in_toc") != IN_TOC:
            continue
        ctype = _attr(ch, "chapter_type") or "body"
        if ctype in NON_BODY_TYPES:
            continue
        ci = _attr(ch, "chapter_index")
        if ci is None:
            continue
        out.append({"chapter_index": int(ci),
                    "title": (_attr(ch, "title") or "").strip()})
    out.sort(key=lambda u: u["chapter_index"])
    return out


def ordinal_ratio(units: list[dict]) -> float:
    """带序号的单元占比。0.0 表示「篇篇名字都不同」→ 像集子。"""
    if not units:
        return 0.0
    n = sum(1 for u in units if _ORDINAL_RE.match(u.get("title") or ""))
    return n / len(units)


def _assign(chunks, units: list[dict]) -> dict[int, list]:
    """把块归到**章号不大于它的最后一个**顶层单元；早于第一个单元的归第一个。

    这是「就近前置」—— EPUB 里一个长 spine 文档被切出来的续块（`part0003
    split 002` 之类，level/in_toc 都是 -1）本来就紧跟在它所属的那一篇后面。
    """
    by_unit: dict[int, list] = {u["chapter_index"]: [] for u in units}
    first = units[0]["chapter_index"]
    for ch in chunks:
        ci = _attr(ch, "chapter_index")
        if ci is None:
            continue
        owner = first
        for u in units:
            if u["chapter_index"] <= ci:
                owner = u["chapter_index"]
            else:
                break
        by_unit[owner].append(ch)
    return by_unit


_SINGLE_HINTS = ("single", "one_world", "unified", "novel", "monograph")
_COLLECTION_HINTS = ("collection", "anthology", "essay", "story", "集")


def normalize_narrative_structure(raw: str) -> tuple[str, str]:
    """把 ``narrative_structure`` 收成 ``single`` / ``collection`` / ``""``。

    返回 ``(值, 备注)``；**备注非空 = 原值不认识，调用方必须报出来**。

    为什么要宽容：实测同一本书，火山引擎回 ``collection``，DeepSeek 官方回
    ``essay_collection``。严格相等判断下后者落进「语义裁决缺席」分支 ——
    **静默退回单域**，跨篇串流原样回来，而且不报任何错。这类「模型换了个说法
    → 功能静默失效」是这一轮里第三次撞见，所以这里连「不认识」也要说出来，
    不能装作是「没判定」。
    """
    s = re.sub(r"[\s\-/]+", "_", (raw or "").strip().lower())
    if not s:
        return "", ""
    if s in (SINGLE, COLLECTION):
        return s, ""
    if any(h in s for h in _COLLECTION_HINTS):
        return COLLECTION, f"叙事结构原值 {raw!r} 不认识，按集类处理"
    if any(h in s for h in _SINGLE_HINTS):
        return SINGLE, f"叙事结构原值 {raw!r} 不认识，按单一叙事处理"
    return "", f"叙事结构原值 {raw!r} 不认识，退回单域（宁可不划，不可错划）"


def plan_scopes(chapters, chunks, *, narrative_structure: str = "",
                chunk_id_of=None) -> ScopePlan:
    """决定这本书是一个世界，还是几个世界。

    *chunks* 应当是**已经裁剪过**的块（被归属分类裁掉的块不参与划域）。
    *narrative_structure* 来自 `context_setter` 的语义裁决；空串（老缓存 /
    没跑过）一律当 `single` —— 宁可不划，不可错划。
    """
    units = top_level_units(chapters)
    plan = ScopePlan(units=units)
    cid_of = chunk_id_of or (lambda c: _attr(c, "id"))

    def _whole_book(source: str, reason: str) -> ScopePlan:
        plan.source = source
        plan.reason = reason
        ids = tuple(str(cid_of(c)) for c in chunks)
        cis = tuple(sorted({_attr(c, "chapter_index") for c in chunks
                            if _attr(c, "chapter_index") is not None}))
        plan.scopes = [Scope("s0", "全书", cis, ids, source)]
        plan.by_chunk = {cid: "s0" for cid in ids}
        return plan

    verdict, note = normalize_narrative_structure(narrative_structure)
    if note:
        plan.conflicts.append(note)
    if verdict != COLLECTION:
        why = {"single": "语义裁决：单一叙事"}.get(
            verdict, "语义裁决缺席（未判定/老缓存）")
        return _whole_book(SINGLE, why)

    if len(units) < 2:
        plan.conflicts.append(
            f"语义裁决说是集类，但顶层正文单元只有 {len(units)} 个 —— 无从划域")
        return _whole_book("fallback", f"顶层单元不足 2（实得 {len(units)}）")

    ratio = ordinal_ratio(units)
    if ratio >= ORDINAL_MAJORITY:
        plan.conflicts.append(
            f"语义裁决说是集类，但 {ratio:.0%} 的顶层单元标题带序号"
            f"（如「{next(u['title'] for u in units if _ORDINAL_RE.match(u['title']))}」）"
            "—— 那是同一部作品的分章，不是独立篇目")
        return _whole_book("fallback", f"标题多带序号（{ratio:.0%} ≥ "
                                       f"{ORDINAL_MAJORITY:.0%}）")

    by_unit = _assign(chunks, units)
    total = sum(len(v) for v in by_unit.values()) or 1
    biggest_ci, biggest = max(by_unit.items(), key=lambda kv: len(kv[1]))
    if len(biggest) / total > MAX_UNIT_SHARE:
        title = next((u["title"] for u in units
                      if u["chapter_index"] == biggest_ci), "?")
        plan.conflicts.append(
            f"语义裁决说是集类，但单元「{title}」吞掉了 "
            f"{len(biggest) / total:.0%} 的正文 —— 集子的每一篇都是完整而"
            "体量相当的作品，吞掉半本的只可能是分节")
        return _whole_book("fallback", f"单单元占比过高（{len(biggest) / total:.0%}）")

    scopes: list[Scope] = []
    for i, u in enumerate(units):
        members = by_unit.get(u["chapter_index"]) or []
        if not members:
            continue
        label = u["title"] or f"第{u['chapter_index'] + 1}章"
        scopes.append(Scope(
            scope_id=f"s{len(scopes)}", label=label,
            chapter_indices=tuple(sorted({_attr(c, "chapter_index")
                                          for c in members})),
            chunk_ids=tuple(str(cid_of(c)) for c in members),
            source=COLLECTION))
    if len(scopes) < 2:
        plan.conflicts.append("划分后只剩一个域，退回全书单域")
        return _whole_book("fallback", "划分后只剩一个域")

    plan.source = COLLECTION
    plan.reason = (f"{len(scopes)} 篇各自成域（顶层单元 {len(units)} 个，"
                   f"带序号 {ratio:.0%}）")
    plan.scopes = scopes
    for s in scopes:
        for cid in s.chunk_ids:
            plan.by_chunk[cid] = s.scope_id
    return plan


def scope_brief(scope: Scope, plan: ScopePlan) -> str:
    """每批 user message 里的**域简报**：我在哪个世界、这个世界有几篇。

    单域时返回空串 —— 长篇拿到的提示词与改动前逐字一致（惰性）。
    """
    if not plan.is_split or scope is None:
        return ""
    idx = plan.scopes.index(scope) + 1
    return (
        f"## 本批所在的篇目\n"
        f"- 第 {idx}/{len(plan.scopes)} 篇「{scope.label}」"
        f"（第 {'、'.join(str(i + 1) for i in scope.chapter_indices)} 章）\n"
        f"- 全书是**集类**（{plan.reason}），各篇自成一体。本批正文**只属于这一篇**：\n"
        f"  不要把它与书中其他篇目的人物 / 事件 / 台词互相连线 ——\n"
        f"  其他篇里出现同名的人，除非本批正文明确说他们是同一个，否则就是另一个人。\n"
        f"- 跨篇存在的实体（作者、叙述者「我」、反复出现的至亲）会在**各自的篇里**\n"
        f"  被分别提到，你只管按本批正文说，不要替读者合并。"
    )


def plan_to_meta(plan: ScopePlan, chunks) -> dict:
    """按**坐标**（章号, 块号）落库 —— chunk_id 会在重导时变，坐标不会。

    坐标约定沿用 head_scan 的 `F#<chapter_index>-<chunk_index>`。
    """
    coord = {str(_attr(c, "id")): f"F#{_attr(c, 'chapter_index')}-{_attr(c, 'chunk_index')}"
             for c in chunks}
    return {
        "source": plan.source,
        "reason": plan.reason,
        "conflicts": plan.conflicts,
        "scopes": [
            {"scope_id": s.scope_id, "label": s.label,
             "chapters": list(s.chapter_indices),
             "cells": [coord[cid] for cid in s.chunk_ids if cid in coord]}
            for s in plan.scopes
        ],
    }
