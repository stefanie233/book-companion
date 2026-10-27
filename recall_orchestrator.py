"""Web 端统一记忆召回编排器 — 声明式通道表（腾讯编排表方案）。

每个通道声明【id / 围栏标签 / 预算优先级 / fetch】四元组，形成可审查的
编排表（强可追溯性）。稳定块（persona / 场景导航 / 工具指南）不在本模块
——本轮核心范围不动 Web 稳定层。

动态通道（fetch 全部返回已包围栏标签的文本，空串表示无命中）：

  l1-memories        <relevant-memories>     L1 原子记忆（wingman.prefetch）
  user-highlights    <user-highlights>       用户收藏（annotations type=highlight，全库）
  user-notes         <user-notes>            用户笔记（annotations type=note，全库）
  graph-observations <graph-observations>    图谱观察（现有预取收编）
  reader-notes       <reader-notes>          agent 自己的读者笔记（三档渐进披露，
                                             按 reading_chapter 取下界）

单通道失败不阻塞其他（对齐腾讯 allSettled 语义），仅 log。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 预算优先级（低先丢）：relevant-memories → user-notes → user-highlights → graph
_PRIORITY_L1 = 50
_PRIORITY_NOTES = 60
_PRIORITY_HIGHLIGHTS = 70
_PRIORITY_GRAPH = 80
# 读者笔记排在 L1 之后、用户笔记之前：它是 agent 自己的东西，
# 预算紧时该让位给**用户的**笔记与收藏。但它比纯记忆召回更贴题。
_PRIORITY_READER = 55


# ── 单通道 fetch 实现 ─────────────────────────────────────────────────────

def _fetch_l1(query: str, ctx: dict[str, Any]) -> str:
    """L1 原子记忆召回 — 走 memory_manager.prefetch_all（wingman.prefetch 已实现）。"""
    runtime = ctx.get("runtime")
    if not query or runtime is None:
        return ""
    mm = getattr(runtime, "memory_manager", None)
    if mm is None:
        return ""
    try:
        recall = (mm.prefetch_all(query) or "").strip()
    except Exception as e:
        logger.warning("recall: l1-memories fetch failed: %s", e)
        return ""
    if not recall:
        return ""
    return (
        "<relevant-memories>\n"
        "以下是当前对话召回的相关记忆，不代表当前任务进程，仅作为参考：\n\n"
        f"{recall}\n"
        "</relevant-memories>"
    )


def _book_title(books_db: Any, book_id: str | None) -> str:
    """Resolve a book title from chunk-level book_id (best-effort)."""
    if not book_id:
        return ""
    try:
        row = books_db.get_book(book_id)
        return (row.title or "") if row else ""
    except Exception:
        return ""


def _fetch_annotations(query: str, ctx: dict[str, Any], *, types: tuple[str, ...],
                       tag: str, kind: str) -> str:
    """用户收藏 / 笔记全库召回 — 关键词展开 + LIKE 检索 + 当前书加权。"""
    books_db = ctx.get("books_db")
    if not query or books_db is None:
        return ""
    try:
        # 关键词展开复用 wingman 的纯静态函数（避免重复实现）
        from wingman.service import WingmanMemoryProvider
        terms = WingmanMemoryProvider._extract_keywords(query)
        rows = books_db.search_annotations(
            terms,
            types=types,
            limit=5,
            current_stable_book_id=ctx.get("current_stable_book_id"),
        )
    except Exception as e:
        logger.warning("recall: %s fetch failed: %s", tag, e)
        return ""
    if not rows:
        return ""
    lines = [tag]
    for a in rows:
        text = (a.note_text or "").strip() if kind == "note" else (a.selected_text or "").strip()
        if not text:
            text = (a.selected_text or a.note_text or "").strip()
        if len(text) > 120:
            text = text[:117] + "…"
        title = _book_title(books_db, a.book_id)
        loc = f"《{title}》" if title else f"书[{a.stable_book_id[-8:] if a.stable_book_id else '?'}]"
        cur = " ★" if a.stable_book_id and a.stable_book_id == ctx.get("current_stable_book_id") else ""
        lines.append(f"  [{a.id}] {loc} ch.{a.chapter_index} ck.{a.chunk_index}{cur}: {text}")
    lines.append(f"</{tag[1:]}")
    return "\n".join(lines)


# 检索时丢弃的词：单字虚词、疑问词、口语填充。留着只会让 `value LIKE %的%`
# 命中一大片，把真正贴题的观察挤掉。
_QUERY_STOP = frozenset(
    "的 了 是 在 我 你 他 她 它 们 这 那 什么 怎么 为什么 吗 呢 吧 啊 呀 和 与 "
    "或 就 都 也 还 很 太 有 没 一个 这个 那个 一下 时候 觉得 知道 说 想 可以 "
    "是不是 有没有 关于 里面 之后 之前 然后".split()
)


def _query_terms(text: str, limit: int = 6) -> list[str]:
    """把用户消息切成**可检索的词**（jieba + 2-gram，长词优先）。

    为什么必须有这一步：``search_observations`` 是**关键词**接口 ——
    ``observations_fts`` 是 trigram，``MATCH '<整句>'`` 退化成**整句子串搜索**，
    LIKE 兜底同理（``value LIKE '%整句%'``）。实测 6 个真实问法**全部 0 命中**
    （而 ``母亲`` / ``地坛`` / ``轮椅`` 这类关键词分别有 5 / 1 / 5 条）。
    也就是说这条通道**一直是死的**，只在「用户整条消息恰好是某条观察的子串」
    时才亮 —— 和 ``graph_unresolved`` 全库 0 行是同一类：通道在，但从不说活。

    分词放在调用方（与 ``tools._tool_graph_context`` 同一分层），不塞进
    ``GraphDB`` —— 检索层不该 import 分词器。
    """
    if not text:
        return []
    try:
        import jieba
        toks = [t.strip() for t in jieba.cut(text) if t.strip()]
    except Exception:
        toks = text.split()
    words = [t for t in toks if len(t) >= 2 and t not in _QUERY_STOP]
    # 2-gram 救 jieba 切碎的专名（「思琪」被切成「思」+「琪」时靠它捞回来）。
    # **排在真词之后** —— 曾经按长度排序，于是「什么关系」这种 4 字垃圾排到了
    # 「母亲」「地坛」前面，取满 5 条就断，实测「母亲和地坛有什么关系」返回
    # 4 条 personality、地坛一条没有。长度不等于具体度。
    grams = []
    for i in range(len(toks) - 1):
        g = (toks[i] + toks[i + 1]).strip()
        if len(g) >= 2 and g not in _QUERY_STOP:
            grams.append(g)
    return list(dict.fromkeys(words + grams))[:limit]


def _fetch_graph(query: str, ctx: dict[str, Any]) -> str:
    """图谱观察混合预取 —— 分词后逐词检索，合并去重取前 5。"""
    runtime = ctx.get("runtime")
    gpath = ctx.get("graph_db_path")
    if not query or runtime is None or gpath is None:
        return ""
    book_id = getattr(runtime, "current_book_id", "") or ""
    if not book_id:
        return ""
    gpath = Path(gpath)
    if not gpath.exists():
        return ""
    terms = _query_terms(query)
    if not terms:
        return ""
    gate = ctx.get("max_chapter")
    obs: list[dict] = []
    seen: set[tuple] = set()
    try:
        from storage.graph_db import GraphDB
        gdb = GraphDB(gpath)
        try:
            # ① 实体优先。观察是**挂在节点上**的，按实体取比按文本匹配更贴题，
            #    而且值本身常常不带主语（「隐忍克制：儿子发脾气时悄悄躲出去」），
            #    按实体取才能把「这是谁」一起带上。
            hit_nodes: set[str] = set()
            for t in terms:
                for ent in gdb.search_nodes(book_id, t, chapter_to=gate, limit=2):
                    nid = ent["node_id"]
                    if nid in hit_nodes:
                        continue
                    hit_nodes.add(nid)
                    rows, _, _ = gdb.get_observations(book_id, nid, gate)
                    for o in rows[:2]:          # 每个实体最多 2 条，让多个实体都有份
                        key = (o.get("field", ""), (o.get("value") or "")[:48])
                        if key in seen:
                            continue
                        seen.add(key)
                        obs.append({**o, "who": ent["canonical_name"]})
                    if len(obs) >= 5:
                        break
                if len(obs) >= 5:
                    break
            # ② 关键词兜底。补①够不着的那类：实体自己**没有观察**，但别人的
            #    观察里提到了它（实测「地坛」「生命」都是这样）。
            if len(obs) < 5:
                for t in terms:
                    got = 0
                    for o in gdb.search_observations(book_id, t, limit=3, max_chapter=gate):
                        key = (o.get("field", ""), (o.get("value") or "")[:48])
                        if key in seen:
                            continue
                        seen.add(key)
                        obs.append(o)
                        got += 1
                        if got >= 2:
                            break
                    if len(obs) >= 5:
                        break
            obs = obs[:5]
        finally:
            gdb.close()
    except Exception as e:
        logger.warning("recall: graph-observations fetch failed: %s", e)
        return ""
    if not obs:
        return ""
    lines = ["<graph-observations>"]
    for o in obs:
        src_tag = " (对话推断)" if o.get("source") == "dialogue" else ""
        who = o.get("who")
        tag = f"{o.get('field', '')}·{who}" if who else o.get("field", "")
        lines.append(f"  [{tag}] {o.get('value', '')}{src_tag}")
    lines.append("</graph-observations>")
    return "\n".join(lines)


# ── 读者笔记：渐进式披露 ──────────────────────────────────────────────────

def _fetch_reader_notes(query: str, ctx: dict[str, Any]) -> str:
    """读者笔记 —— 三档渐进披露（见 ``docs/reader-notes-design.md``）。

    | 档 | 条件 | 给什么 |
    |---|---|---|
    | 远 | 还没读到那一章 | 只报计数，不报内容 |
    | 近 | 读过了、但已经走远 | 索引：几条、在哪块、贴在哪句下 |
    | 极近 | 同章 / 相邻章 | **全量正文** |

    这是「读者」和「评论家」的分界：笔记是**当时不知道后来**写的，
    所以披露必须按阅读进度取下界。

    **拿不到阅读进度时一律按「远」处理** —— 不知道读者在哪，就不给正文。
    宁可少说，不可剧透。
    """
    gpath = ctx.get("graph_db_path")
    book_id = getattr(ctx.get("runtime"), "current_book_id", "") or ""
    if not query or not gpath or not book_id:
        return ""
    gpath = Path(gpath)
    if not gpath.exists():
        return ""
    try:
        from storage.graph_db import GraphDB
        gdb = GraphDB(gpath)
        try:
            notes = gdb.get_notes(book_id)
        finally:
            gdb.close()
    except Exception as e:
        logger.warning("recall: reader-notes fetch failed: %s", e)
        return ""
    if not notes:
        return ""
    return _render_reader_notes(notes, ctx.get("reading_chapter"),
                               _scope_index(ctx.get("books_db"), book_id))


def _scope_index(books_db: Any, book_id: str) -> dict[int, tuple[int, str]]:
    """章号 → ``(所属篇的代表章号, 篇名)``。

    **续块必须归到它所属的那一篇。** 不归的话有两个后果，实测都出现过：
    计数里冒出「《part0003 split 002》 2 篇」这种切分残留的名字；
    以及「读到《我二十一岁那年》」漏掉它的续块（章号差得远，被判成「远」）。
    """
    if books_db is None:
        return {}
    try:
        from bookgraph.notes import iter_scopes
        out: dict[int, tuple[int, str]] = {}
        for title, idxs in iter_scopes(books_db, book_id):
            for ci in idxs:
                out[ci] = (idxs[0], title)
        return out
    except Exception:
        return {}


def _render_reader_notes(notes: list[dict], x: int | None,
                         scope: dict[int, tuple[int, str]]) -> str:
    """三档渲染。``x`` = 阅读进度章号，``None`` = 不知道（一律当「远」）。

    距离算在**篇**上，不是裸章号 —— 见 ``_scope_index``。
    """
    def _rep(ci: Any) -> Any:
        got = scope.get(ci) if isinstance(ci, int) else None
        return got[0] if got else ci

    def _t(ci: Any) -> str:
        got = scope.get(ci) if isinstance(ci, int) else None
        return f"《{got[1]}》" if got and got[1] else f"ch{ci}"

    if isinstance(x, int) and scope:
        x = _rep(x)
    near: list[dict] = []
    mid: list[dict] = []
    far: list[dict] = []
    for n in notes:
        ci = n.get("chapter_index")
        if x is None or not isinstance(ci, int):
            far.append(n)
            continue
        r = _rep(ci)
        if abs(r - x) <= 1:
            near.append(n)
        elif r <= x:
            mid.append(n)
        else:
            far.append(n)

    lines = ["<reader-notes>",
             "以下是**你自己**读这本书时留下的笔记 —— 不是用户的，也不是待办。",
             "除非用户正好问到，否则不必主动提起。", ""]
    if x is None:
        lines += ["（拿不到阅读进度，本轮只报数目，不展开任何内容）", ""]
    if near:
        lines.append("### 你正读到这一带 —— 完整笔记")
        for n in near:
            lines.append(f"- 贴：{n.get('quote_text', '')}")
            lines.append(f"  你写的：{n.get('text', '')}")
        lines.append("")
    if mid:
        lines.append("### 你读过的地方，留过这些（只给索引）")
        for n in mid:
            lines.append(f"- {_t(n.get('chapter_index'))} · "
                         f"块 {str(n.get('chunk_id') or '')[:8]} · "
                         f"贴：{str(n.get('quote_text') or '')[:30]}")
        lines.append("")
    if far:
        by_scope: dict[Any, int] = {}
        for n in far:
            r = _rep(n.get("chapter_index"))
            by_scope[r] = by_scope.get(r, 0) + 1
        lines.append("### 别处还留过（按篇计数）")
        lines.append("  " + "、".join(
            f"{_t(r)} {c} 条" for r, c in
            sorted(by_scope.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))))
    lines.append("</reader-notes>")
    return "\n".join(lines)

@dataclass(frozen=True)
class RecallChannel:
    """单通道声明：【id / 围栏标签 / 预算优先级 / fetch】四元组。"""

    id: str
    tag: str
    priority: int      # 预算优先级（低先丢）
    fetch: Callable[[str, dict[str, Any]], str]


RECALL_CHANNELS: tuple[RecallChannel, ...] = (
    RecallChannel("l1-memories", "<relevant-memories>", _PRIORITY_L1, _fetch_l1),
    RecallChannel("user-notes", "<user-notes>", _PRIORITY_NOTES,
                  lambda q, c: _fetch_annotations(q, c, types=("note",), tag="<user-notes>", kind="note")),
    RecallChannel("user-highlights", "<user-highlights>", _PRIORITY_HIGHLIGHTS,
                  lambda q, c: _fetch_annotations(q, c, types=("highlight",), tag="<user-highlights>", kind="highlight")),
    RecallChannel("graph-observations", "<graph-observations>", _PRIORITY_GRAPH, _fetch_graph),
    RecallChannel("reader-notes", "<reader-notes>", _PRIORITY_READER, _fetch_reader_notes),
)


def fetch_dynamic_channels(query: str, *, ctx: dict[str, Any]) -> list[tuple[int, str, str]]:
    """按通道表依次 fetch，返回 [(priority, tag, content)]，按 priority 升序。

    单通道失败不阻塞其他（返回空串跳过）。调用方负责把 (tag, content) 并入
    context_parts 并在 drop_order 中登记对应标签。
    """
    out: list[tuple[int, str, str]] = []
    for ch in RECALL_CHANNELS:
        try:
            content = ch.fetch(query, ctx)
        except Exception as e:  # 通道级兜底（fetch 内部已 try，这里防御）
            logger.warning("recall: channel %s crashed: %s", ch.id, e)
            continue
        if content and content.strip():
            out.append((ch.priority, ch.tag, content))
    out.sort(key=lambda x: x[0])
    return out
