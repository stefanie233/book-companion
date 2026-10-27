"""参考素材召回（实验版）。

从 BookGraph 与参考书原文里取出「可被转写」的素材：人物质感、关系事件、
对话样本、原文片段。**只给写法与细节，不给设定**——渲染时明确分区，
并统计专名泄漏，交给打分器判断。

这里先做确定性检索；LLM 蒸馏成「素材卡」是另一条配方（见 runner）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

try:
    import jieba
except Exception:                                    # pragma: no cover
    jieba = None

_ASCII = re.compile(r"[A-Za-z]{2,}")

#: 检索时不该当关键词的虚词/叙事套话。
STOP = set("""我们 你们 他们 她们 自己 一个 一种 一些 这个 那个 这样 那样
什么 怎么 因为 所以 但是 可是 而且 然后 已经 还是 就是 不是 没有 可以
应该 需要 必须 不要 不能 时候 地方 事情 东西 感觉 好像 仿佛 起来 出来
这里 那里 现在 过去 之后 之前 第一 第二 第三 叙述 故事 本章 正文""".split())


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


KEEP_POS = ("n", "v", "vn", "a", "an", "s", "f", "t")


def query_terms(text: str, *, limit: int = 16) -> list[str]:
    """只保留名词/动词/形容词；「回到」「发现」这类通用动词不进查询。"""
    generic = STOP | {"回到", "发现", "进入", "整理", "留下", "最后", "一次",
                      "一件", "已经", "开始", "出现", "知道", "觉得", "看着"}
    counts: dict[str, int] = {}
    if jieba is not None:
        try:
            import jieba.posseg as pseg
            pairs = list(pseg.cut(text or ""))
        except Exception:                            # noqa: BLE001
            pairs = [(w, "n") for w in jieba.cut(text or "")]
    else:
        pairs = [(w, "n") for w in re.split(r"[^\w一-鿿]+", text or "")]
    for word, flag in pairs:
        word = word.strip()
        if len(word) < 2 or word in generic or word.isdigit():
            continue
        if _ASCII.fullmatch(word) or not flag.startswith(KEEP_POS):
            continue
        counts[word] = counts.get(word, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def _hits(text: str, terms: Iterable[str]) -> int:
    return sum(1 for t in terms if t and t in (text or ""))


def _search(conn, select: str, where: str, terms: list[str], *,
            limit: int) -> list[dict[str, Any]]:
    """每个词单独跑一次 LIKE，合并去重后按命中数排序。"""
    marks = where.count("?")
    found: dict[Any, tuple[int, dict[str, Any]]] = {}
    for term in terms[:10]:
        try:
            rows = conn.execute(select + " WHERE " + where,
                                tuple([f"%{term}%"] * marks)).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            item = dict(row)
            key = item.get("id") or item.get("chunk_id")
            score = _hits(json.dumps(item, ensure_ascii=False), terms)
            if key not in found or score > found[key][0]:
                found[key] = (score, item)
    return [x for _, x in sorted(found.values(), key=lambda kv: -kv[0])][:limit]


def recall_graph(graph_path: Path, terms: list[str], *,
                 limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    """四类素材各取一点：观察、引文、边、节点。"""
    out = {"observations": [], "quotes": [], "edges": [], "nodes": []}
    if not graph_path.exists():
        return out
    conn = _conn(graph_path)
    try:
        out["observations"] = _search(
            conn,
            "SELECT o.id, n.canonical_name AS node_name, o.field, o.value,"
            " o.evidence_text, o.anchor_chapter FROM graph_observations o"
            " LEFT JOIN nodes n ON n.id=o.node_id",
            "(o.value LIKE ? OR o.evidence_text LIKE ?)", terms, limit=limit)
        out["quotes"] = _search(
            conn,
            "SELECT id, character_name, quote_text, scene_context, chunk_id"
            " FROM graph_quotes",
            "(quote_text LIKE ? OR scene_context LIKE ?)", terms, limit=limit)
        out["edges"] = _search(
            conn,
            "SELECT e.id, e.kind, e.evidence_text, s.canonical_name AS source_name,"
            " t.canonical_name AS target_name FROM edges e"
            " LEFT JOIN nodes s ON s.id=e.source_id"
            " LEFT JOIN nodes t ON t.id=e.target_id",
            "(e.evidence_text LIKE ? OR e.kind LIKE ?)", terms, limit=limit)
        out["nodes"] = _search(
            conn,
            "SELECT id, canonical_name, kind, description, personality,"
            " motivation FROM nodes",
            "(canonical_name LIKE ? OR description LIKE ?"
            " OR personality LIKE ? OR motivation LIKE ?)", terms, limit=limit)
    finally:
        conn.close()
    return out


def recall_text(books_db: Path, book_id: str, terms: list[str], *,
                limit: int = 4, min_chars: int = 120) -> list[dict[str, Any]]:
    """参考书原文段落。trigram 对两字词无效，直接 LIKE。

    排序看**命中了几个不同的词**，不是命中次数 —— 一段话反复出现「母亲」
    不等于它有用；同时出现「母亲 / 缝纫机 / 抽屉」才是。
    """
    if not books_db.exists():
        return []
    conn = _conn(books_db)
    found: dict[str, dict[str, Any]] = {}
    try:
        for term in terms[:12]:
            rows = conn.execute(
                "SELECT id, chapter_index, chunk_index, content FROM book_chunks"
                f" WHERE book_id={_quote(book_id)} AND char_count>={int(min_chars)}"
                " AND content LIKE ? LIMIT 200", (f"%{term}%",)).fetchall()
            for row in rows:
                found[row["id"]] = dict(row)
    finally:
        conn.close()
    scored = []
    for item in found.values():
        text = item.get("content") or ""
        distinct = {t for t in terms if t in text}
        if len(distinct) < 2 and len(found) > limit:
            continue
        item["hits"] = sorted(distinct)
        item["score"] = (len(distinct), sum(text.count(t) for t in terms))
        scored.append(item)
    scored.sort(key=lambda r: (-r["score"][0], -r["score"][1],
                               -min(len(r.get("content") or ""), 1200)))
    return scored[:limit]


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"
