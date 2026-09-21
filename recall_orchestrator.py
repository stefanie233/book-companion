"""Web 端统一记忆召回编排器 — 声明式通道表（腾讯编排表方案）。

每个通道声明【id / 围栏标签 / 预算优先级 / fetch】四元组，形成可审查的
编排表（强可追溯性）。稳定块（persona / 场景导航 / 工具指南）不在本模块
——本轮核心范围不动 Web 稳定层。

动态通道（fetch 全部返回已包围栏标签的文本，空串表示无命中）：

  l1-memories        <relevant-memories>     L1 原子记忆（wingman.prefetch）
  user-highlights    <user-highlights>       用户收藏（annotations type=highlight，全库）
  user-notes         <user-notes>            用户笔记（annotations type=note，全库）
  graph-observations <graph-observations>    图谱观察（现有预取收编）

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


def _fetch_graph(query: str, ctx: dict[str, Any]) -> str:
    """图谱观察混合预取 — 收编自 api_server 原有内联逻辑（行为不变）。"""
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
    try:
        from storage.graph_db import GraphDB
        gdb = GraphDB(gpath)
        try:
            obs = gdb.search_observations(
                book_id, query, limit=5, max_chapter=ctx.get("max_chapter"),
            )
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
        lines.append(f"  [{o.get('field', '')}] {o.get('value', '')}{src_tag}")
    lines.append("</graph-observations>")
    return "\n".join(lines)


# ── 声明式通道表（可审查） ─────────────────────────────────────────────────

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
