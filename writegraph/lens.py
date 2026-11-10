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


def query_terms(text: str, *, max_terms: int = 12) -> list[str]:
    """把查询文本压成「逐字命中词」，供证据窗口定位用。

    与 ``fts_query`` 不同，这里不拼 SQL，只做子串匹配，所以短一些也安全；
    中文连续串先整体保留，避免把「沈砚」拆成单字。
    """
    terms: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN.finditer(text or ""):
        chunk = match.group(0)
        if len(chunk) < 2:
            continue
        piece = chunk if chunk.isascii() else chunk[:8]
        if piece not in seen:
            seen.add(piece)
            terms.append(piece)
        if len(terms) >= max_terms:
            break
    return terms


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


_PARAGRAPH_SPLIT = re.compile(r"\r?\n\s*\r?\n")


def paragraphs(text: str) -> list[str]:
    """按空行切段；只用于**取证据窗口**，不改写原文。"""
    return [p.strip() for p in _PARAGRAPH_SPLIT.split(text or "") if p.strip()]


def merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相邻的段区间，保证同一段只出现一次。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def dedupe_contained(passages: Sequence[str]) -> list[str]:
    """被更大片段完全包含的窗口删掉 —— 同一段话不进两次 prompt。"""
    out: list[str] = []
    for index, passage in enumerate(passages):
        contained = any(
            other_index != index and passage in other
            and (len(other) > len(passage) or other_index < index)
            for other_index, other in enumerate(passages))
        if not contained:
            out.append(passage)
    return out


def evidence_passages(content: str, terms: Sequence[str], *,
                      window: int = 1, max_windows: int = 3,
                      max_chars: int = 900) -> tuple[list[str], int]:
    """按查询词定位命中段，取「命中段 ± window 段」并合并。

    返回 ``(passages, matched)``。命中数只用于诊断，不参与事实判定。
    取不到命中时返回空 —— 调用方再决定回退摘要还是省略。
    """
    source = paragraphs(content)
    if not source:
        return [], 0
    normalized = [t.strip().lower() for t in terms if len(str(t).strip()) >= 2]
    if not normalized:
        return [], 0
    windows: list[tuple[int, int]] = []
    matched = 0
    for index, paragraph in enumerate(source):
        low = paragraph.lower()
        if any(term in low for term in normalized):
            matched += 1
            windows.append((max(0, index - window),
                            min(len(source) - 1, index + window)))
    if not windows:
        return [], 0
    # 多命中时优先靠近结尾的窗口：越靠后越接近本章时点。
    windows = merge_windows(windows)[-max_windows:]
    passages = dedupe_contained(["\n\n".join(source[s:e + 1])
                                 for s, e in windows])
    kept: list[str] = []
    used = 0
    for passage in passages:
        if used + len(passage) > max_chars:
            break
        kept.append(passage)
        used += len(passage)
    return kept, matched


def material_evidence(candidates: Sequence[Mapping[str, Any]],
                      terms: Sequence[str], *, max_items: int = 6,
                      max_chars: int = 900) -> dict[str, Any]:
    """渲染参考素材，并给出**纳入了什么 / 省略了什么**的收据。

    - 有命中词：取证据窗口（命中段 ±1），比截 300 字更像「这段为什么进来」；
    - 无命中：退回整段摘要的前 300 字，但若不是 summary 而是原文，
      仍按窗口取不到就明确省略，不把无关整章塞进来。
    """
    lines = ["## 参考素材（历史段落，仅供风格与细节参考，不覆盖正典事实）"]
    included: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    used = 0
    for item in list(candidates)[:max_items]:
        chapter = item.get("chapter_index")
        body = str(item.get("content") or "").strip()
        summary = str(item.get("summary") or "").strip()
        passages, matched = evidence_passages(body, terms,
                                              max_chars=max_chars)
        if not passages and summary:
            passages, matched = [summary[:300].replace("\n", " ")], 0
        elif not passages and body and not terms:
            # 没有查询词时保持旧行为：退回首段摘要式截取。
            passages = [body[:300].replace("\n", " ")]
        if not passages:
            omitted.append({"chapter": chapter,
                            "reason": "no-relevant-passage" if body else
                                      "empty-source"})
            continue
        excerpt = "\n".join(passages)
        line = f"- 第{chapter}章：{excerpt}"
        if used + len(line) > max_chars:
            omitted.append({"chapter": chapter, "reason": "budget"})
            continue
        used += len(line)
        lines.append(line)
        included.append({"chapter": chapter, "chars": len(excerpt),
                         "matched": matched,
                         "source": "window" if matched else "summary"})
    text = "\n".join(lines) if len(lines) > 1 else ""
    return {"text": text, "included": included, "omitted": omitted,
            "chars": len(text)}


def render_material(candidates: Sequence[Mapping[str, Any]], *,
                    max_items: int = 6, max_chars: int = 900,
                    terms: Sequence[str] = ()) -> str:
    """渲染成 prompt 里的参考素材块。物理分区 + 明确降级声明。"""
    return material_evidence(candidates, terms, max_items=max_items,
                             max_chars=max_chars)["text"]
