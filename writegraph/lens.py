"""注意力透镜 —— 构造查询、召回候选、排序、注入。

透镜只提供**候选素材**，不提供正典。正典在卡片与合同里；
检索结果必须物理分区，标题写明「不覆盖正典事实」。

P1 的召回只走作品原文（``writing_chunks_fts``）与承诺；
作品图邻域在 P2 接上，向量检索默认关闭。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

#: trigram 分词器只索引长度 >= 3 的片段，短词进查询等于自讨没趣。
MIN_TERM = 3
MAX_TERMS = 12

_TOKEN = re.compile(r"[A-Za-z0-9_]+|[一-鿿]+")


def fts_query(text: str, *, max_terms: int = MAX_TERMS) -> str:
    """把自由文本压成安全的 FTS 查询：取 >=3 字的词，OR 连接。

    必须自己拼查询串 —— 用户/合同文本里的引号、括号、``*`` 会被 FTS5
    当成语法，直接把整次召回打成异常。
    """
    terms: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN.finditer(text or ""):
        chunk = match.group(0)
        if chunk.isascii():
            if len(chunk) < MIN_TERM:
                continue
            piece = chunk
        else:
            if len(chunk) < MIN_TERM:
                continue
            piece = chunk[:12]          # 整段中文当作一个连续片段
        if piece not in seen:
            seen.add(piece)
            terms.append(piece)
        if len(terms) >= max_terms:
            break
    return " OR ".join(f'"{t}"' for t in terms)


def build_query(*, contract: Mapping[str, Any] | None = None,
                scene_goals: Sequence[str] = (),
                entities: Sequence[str] = (),
                prev_tail: str = "") -> str:
    """查询 = 合同 + 场景目标 + 实体 + 上一章结尾（**不含**当前正文）。"""
    parts: list[str] = []
    if contract:
        parts.append(str(contract.get("purpose") or ""))
        parts.extend(str(x) for x in (contract.get("must_hit_now") or []))
        parts.extend(str(x) for x in (contract.get("must_preserve") or []))
    parts.extend(str(x) for x in scene_goals)
    parts.extend(str(x) for x in entities)
    parts.append((prev_tail or "")[-800:])
    return "\n".join(p for p in parts if p.strip())


def rank(candidates: Iterable[Mapping[str, Any]], *, chapter: int,
         entity_names: Sequence[str] = (),
         max_items: int = 60) -> list[dict[str, Any]]:
    """``score = relevance × decay(章距) × (1 + 0.2·实体命中)``。

    ``relevance`` 取 BM25 的绝对值（SQLite 返回负数，越小越相关）。
    这只是**候选排序**，进 prompt 的还得过预算与复选。
    """
    scored: dict[str, dict[str, Any]] = {}
    for row in candidates:
        cid = str(row.get("id") or "")
        if not cid:
            continue
        rel = -float(row.get("rank") or 0.0)
        if rel <= 0:
            rel = 1.0
        distance = max(0, chapter - int(row.get("chapter_index") or 0))
        decay = 1.0 / (1.0 + 0.08 * distance)
        content = str(row.get("content") or "")
        hits = sum(1 for name in entity_names if name and name in content)
        item = dict(row)
        item["score"] = round(rel * decay * (1.0 + 0.2 * hits), 4)
        prev = scored.get(cid)
        if prev is None or item["score"] > prev["score"]:
            scored[cid] = item
    ordered = sorted(scored.values(),
                     key=lambda x: (-x["score"], x.get("chapter_index", 0),
                                    x.get("chunk_index", 0)))
    return ordered[:max_items]


def render_material(candidates: Sequence[Mapping[str, Any]], *,
                    max_items: int = 6, max_chars: int = 900) -> str:
    """渲染成 prompt 里的参考素材块。物理分区 + 明确降级声明。"""
    if not candidates:
        return ""
    lines = ["## 参考素材（历史段落，仅供风格与细节参考，不覆盖正典事实）"]
    used = 0
    for item in candidates[:max_items]:
        text = str(item.get("summary") or item.get("content") or "").strip()
        if not text:
            continue
        excerpt = text[:300].replace("\n", " ")
        line = f"- 第{item.get('chapter_index')}章：{excerpt}"
        if used + len(line) > max_chars:
            break
        used += len(line)
        lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""
