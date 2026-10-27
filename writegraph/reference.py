"""外部参考素材：从 BookGraph 与参考书原文里取「可转写的写法与细节」。

与 ``lens.py`` 的区别：lens 召回**本书自己的历史段落**；这里召回**别的书**。
两条边界：

1. 参考素材只进 ``external_reference`` 块，永远排在正典之后，且可丢弃；
2. 只给写法与细节，不给设定 —— 渲染时去专名，物理分区，并返回泄漏词表。

检索键是**场景意图**，不是本作人名：人名只在本作里有意义，拿去检索参考书
只会搅进噪声，还会招来专名泄漏。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import jieba.posseg as pseg
except Exception:                                    # pragma: no cover
    pseg = None

KEEP_POS = ("n", "v", "vn", "a", "an", "s", "f", "t")
GENERIC = set("""我们 你们 他们 她们 自己 一个 一种 一些 这个 那个 这样 那样
什么 怎么 因为 所以 但是 可是 而且 然后 已经 还是 就是 不是 没有 可以
应该 需要 必须 不要 不能 时候 地方 事情 东西 感觉 好像 仿佛 起来 出来
这里 那里 现在 过去 之后 之前 第一 第二 第三 叙述 故事 本章 正文
回到 发现 进入 整理 留下 最后 一次 一件 开始 出现 知道 觉得 看着""".split())


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def query_terms(text: str, *, exclude: Iterable[str] = (),
                limit: int = 12) -> list[str]:
    """名词/动词/形容词；扣掉本作人名；按出现次数排序。"""
    banned = {str(x) for x in exclude if str(x)}
    counts: dict[str, int] = {}
    pairs = pseg.cut(text or "") if pseg is not None else []
    for word, flag in pairs:
        word = word.strip()
        if len(word) < 2 or word in GENERIC or word.isdigit():
            continue
        if word in banned or not flag.startswith(KEEP_POS):
            continue
        counts[word] = counts.get(word, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def _search(conn, select: str, where: str, terms: Sequence[str], *,
            limit: int) -> list[dict[str, Any]]:
    """每个词单独 LIKE，合并；按命中的不同词数排序。"""
    marks = where.count("?")
    found: dict[Any, dict[str, Any]] = {}
    for term in list(terms)[:10]:
        try:
            rows = conn.execute(select + " WHERE " + where,
                                tuple([f"%{term}%"] * marks)).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            item = dict(row)
            found.setdefault(item.get("id") or item.get("chunk_id"), item)
    scored = []
    for item in found.values():
        blob = " ".join(str(v) for v in item.values())
        hits = {t for t in terms if t and t in blob}
        item["_hits"] = len(hits)
        scored.append(item)
    scored.sort(key=lambda r: -r["_hits"])
    return scored[:limit]


def recall_book(graph_path: Path, books_db: Path, book: Mapping[str, Any],
                terms: Sequence[str], *, limit: int = 6) -> dict[str, Any]:
    """一本参考书：观察、引文、边、原文各取一点。"""
    out: dict[str, Any] = {"label": book.get("label") or "",
                           "stable_book_id": book.get("stable_book_id") or "",
                           "observations": [], "quotes": [], "edges": [],
                           "text": [], "names": []}
    if graph_path.exists():
        conn = _ro(graph_path)
        try:
            obs = _search(
                conn,
                "SELECT o.id, n.canonical_name AS node_name, o.field, o.value,"
                " o.evidence_text FROM graph_observations o"
                " LEFT JOIN nodes n ON n.id=o.node_id",
                "(o.value LIKE ? OR o.evidence_text LIKE ?)", terms, limit=limit)
            # 旁注式的「这一说法出自…」「存疑」是提取器的话，不是人物素材。
            obs = [o for o in obs if not any(
                mark in str(o.get("value") or "")
                for mark in ("这一说法", "转述", "存疑", "原文"))]
            weight = {"personality": 3, "motivation": 3, "role_in_story": 2,
                      "speaking_style": 2, "appearance": 1}
            obs.sort(key=lambda o: (-o["_hits"],
                                    -weight.get(str(o.get("field")), 1),
                                    -len(str(o.get("value") or ""))))
            out["observations"] = obs
            out["quotes"] = _search(
                conn,
                "SELECT id, character_name, quote_text, scene_context"
                " FROM graph_quotes",
                "(quote_text LIKE ? OR scene_context LIKE ?)", terms, limit=limit)
            out["edges"] = _search(
                conn,
                "SELECT e.id, e.kind, e.evidence_text,"
                " s.canonical_name AS source_name,"
                " t.canonical_name AS target_name FROM edges e"
                " LEFT JOIN nodes s ON s.id=e.source_id"
                " LEFT JOIN nodes t ON t.id=e.target_id",
                "(e.evidence_text LIKE ? OR e.kind LIKE ?)", terms, limit=limit)
            out["names"] = [str(r[0]) for r in conn.execute(
                "SELECT canonical_name FROM nodes WHERE kind!='chapter'"
                " AND length(canonical_name)>=2").fetchall()]
        finally:
            conn.close()
    if books_db.exists() and book.get("book_id"):
        conn = _ro(books_db)
        try:
            found: dict[str, dict[str, Any]] = {}
            for term in list(terms)[:10]:
                rows = conn.execute(
                    "SELECT id, chapter_index, content FROM book_chunks"
                    " WHERE book_id=? AND char_count>=120 AND content LIKE ?"
                    " LIMIT 120", (book["book_id"], f"%{term}%")).fetchall()
                for row in rows:
                    found.setdefault(row["id"], dict(row))
            scored = []
            for item in found.values():
                hits = {t for t in terms if t in (item.get("content") or "")}
                if len(hits) < 2 and len(found) > 3:
                    continue
                item["_hits"] = len(hits)
                scored.append(item)
            scored.sort(key=lambda r: -r["_hits"])
            out["text"] = scored[:3]
        finally:
            conn.close()
    return out


def recall(config, books: Sequence[Mapping[str, Any]],
           terms: Sequence[str]) -> dict[str, Any]:
    """全部参考书。任何一本读失败都只跳过它，不阻塞写作。"""
    graphs = Path(config.graphs_dir)
    books_db = Path(config.books_db_path)
    packs = []
    for book in books:
        sid = str(book.get("stable_book_id") or "")
        if not sid:
            continue
        try:
            packs.append(recall_book(graphs / f"graph_{sid}.db", books_db,
                                     book, terms))
        except Exception:                            # noqa: BLE001
            continue
    return {"terms": list(terms), "books": packs,
            "relevance": relevance(packs, terms)}


def relevance(packs: Sequence[Mapping[str, Any]],
              terms: Sequence[str]) -> dict[str, Any]:
    """粗门：语料里到底有几个检索词。低到一定程度就别注入。"""
    blobs = []
    for pack in packs:
        for key in ("observations", "quotes", "edges"):
            for item in pack.get(key) or []:
                blobs.append(str(item.get("value") or item.get("evidence_text")
                                 or item.get("quote_text") or ""))
        for item in pack.get("text") or []:
            blobs.append(str(item.get("content") or ""))
    hits = sorted({t for t in terms if any(t in b for b in blobs)})
    return {"hits": hits, "covered": len(hits),
            "coverage": round(len(hits) / max(1, len(terms)), 3),
            "ok": len(hits) >= 2}


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip("，。；、 ") + "…"


def _take(items: Sequence[Mapping[str, Any]], n: int) -> list[Mapping[str, Any]]:
    seen: set[str] = set()
    out: list[Mapping[str, Any]] = []
    for item in items:
        key = _clean(item.get("value") or item.get("evidence_text")
                     or item.get("quote_text") or "", 160)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= n:
            break
    return out


def render(pack: Mapping[str, Any], *, max_chars: int = 1100) -> str:
    """确定性素材块。没有素材返回空串 —— 空块由预算层丢弃。"""
    lines = ["## 参考素材（另一个故事；只借观察与细节，"
             "不得搬用设定、人名、地名、作品名）"]
    for book in pack.get("books") or []:
        obs = _take(book.get("observations") or [], 2)
        quotes = _take(book.get("quotes") or [], 2)
        edges = _take(book.get("edges") or [], 2)
        texts = _take(book.get("text") or [], 1)
        if not (obs or quotes or edges or texts):
            continue
        lines.append(f"### 《{book.get('label') or '参考书'}》")
        for item in obs:
            who = _clean(item.get("node_name"), 12)
            value = _clean(item.get("value"), 90)
            lines.append(f"- 人物质感：{who}：{value}" if who
                         else f"- 人物质感：{value}")
        for item in edges:
            ev = _clean(item.get("evidence_text"), 90)
            lines.append(f"- 关系/事件：{item.get('source_name') or ''}"
                         f" —{item.get('kind')}→ "
                         f"{item.get('target_name') or ''}｜{ev}")
        for item in quotes:
            quote = _clean(item.get("quote_text"), 100)
            ctx = _clean(item.get("scene_context"), 40)
            lines.append(f"- 对话：「{quote}」"
                         + (f"（{ctx}）" if ctx else ""))
        for item in texts:
            lines.append(f"- 原文（第{item.get('chapter_index')}章）："
                         + _clean(item.get("content"), 160))
        if len("\n".join(lines)) > max_chars:
            break
    body = "\n".join(lines)
    return body if len(body) > 100 else ""


def skeleton(pack: Mapping[str, Any], *, max_chars: int = 2400) -> str:
    """给 LLM 蒸馏看的候选池。"""
    return render(pack, max_chars=max_chars)


def render_distilled(card: str, labels: Sequence[str]) -> str:
    body = (card or "").strip()
    if not body:
        return ""
    head = ("## 参考素材 · " + "、".join(f"《{x}》" for x in labels if x)
            + "（另一个故事；只借观察与细节，不得搬用设定与人名）")
    return head + "\n" + body


def leak_terms(pack: Mapping[str, Any], *, min_len: int = 2) -> list[str]:
    """参考书里的**专名**（人名/地名/机构），用来在成稿后查「搬设定」。

    百科式的普通名词（主任、上帝、中年女人）不是泄漏源；只看 jieba 的
    nr/ns/nz。没有词性标注时退化为长度过滤，不硬猜。
    """
    out: set[str] = set()
    for book in pack.get("books") or []:
        for name in book.get("names") or []:
            name = str(name).strip()
            if len(name) < min_len or name.isdigit():
                continue
            if pseg is not None:
                flags = [flag for word, flag in pseg.cut(name)
                         if word.strip() == name]
                if flags and not flags[0].startswith(("nr", "ns", "nz")):
                    continue
            out.add(name)
    return sorted(out)


__all__ = ["query_terms", "recall", "recall_book", "relevance", "render",
           "render_distilled", "skeleton", "leak_terms"]
