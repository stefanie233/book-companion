"""确定性分层抽样 —— 不整本喂模型，也不随机。

三条硬约束（设计稿 04 §三.3 / 08 §四）：

1. **确定性**：同一本书、同一参数，每次抽到同一批窗口。用
   ``sha1(stable_book_id:chunk_id:start:end)`` 排序，不用 ``random()``。
2. **分层**：书内位置（前/中/后）、章节类型、对话密度、场景、时期都要覆盖。
   只选高潮会让文风失真 —— 安静段不是凑数。
3. **预算**：每段 200–500 字，每次 ≤12 段 / ≤8000 字（设计稿 08 §四.3）。

场景标签是**启发式提示**，不是判断：模型在输出里可以纠正。规则只看
引号比例、标点密度和词表命中，不做语义理解 —— 语义判断是下一次调用的活。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from authorgraph.voice.scope import ChunkView
from authorgraph.voice.vocab import SCENE_TYPES

#: 每次 run 的硬上限（设计稿 08 §四.3）。
MAX_SAMPLES = 12
MAX_INPUT_CHARS = 8_000
MIN_WINDOW_CHARS = 200
MAX_WINDOW_CHARS = 500

#: 场景覆盖要求：至少 5 类，至少 2 个安静/日常段（设计稿 08 §九）。
MIN_SCENE_KINDS = 5
MIN_QUIET = 2
_QUIET_LIKE = ("quiet", "introspection", "description", "landscape")

_QUOTE_OPEN = "“「『"
_QUOTE_CLOSE = "”」』"

_MARKERS: dict[str, tuple[str, ...]] = {
    "memory": ("那年", "小时候", "从前", "记得", "后来", "多年以后",
               "回忆", "那时", "当年"),
    "introspection": ("我想", "我觉得", "心里", "忽然", "仿佛", "似乎",
                      "感到", "意识到", "知道", "明白"),
    "emotional_peak": ("哭", "笑", "喊", "痛", "怕", "怒", "颤抖",
                       "眼泪", "沉默", "吼"),
    "landscape": ("山", "河", "天空", "阳光", "风", "雨", "树", "街道",
                  "田野", "夜色", "雪", "屋顶"),
    "description": ("墙", "门", "窗", "桌", "椅", "房间", "衣服", "脸",
                    "眼睛", "手", "头发", "灯"),
    "action": ("走", "跑", "抓", "推", "拉", "打", "冲", "转身", "抬",
               "站起来", "退", "摔"),
    "transition": ("第二天", "后来", "从此", "不久", "与此同时", "这时",
                   "过了一会儿", "片刻", "天亮"),
    "argument": ("争论", "辩", "反驳", "质问", "回答", "说", "问"),
}


def quote_share(text: str) -> float:
    """引号内文字占全文的比例（中英文引号都算）。"""
    total = len(text or "")
    if not total:
        return 0.0
    inside = 0
    depth = 0
    for ch in text:
        if ch in _QUOTE_OPEN:
            depth += 1
        elif ch in _QUOTE_CLOSE:
            depth = max(0, depth - 1)
        elif ch == '"':                 # ASCII 直引号没有方向，按开/关交替
            depth = 0 if depth else 1
        elif depth:
            inside += 1
    return inside / total


def scene_hint(text: str, *, band: str = "middle",
               is_chapter_end: bool = False) -> str:
    """规则启发式场景标签。**主标签只有一个**；次标签在调用侧不进库。"""
    t = text or ""
    n = max(len(t), 1)
    q = quote_share(t)
    punct = sum(t.count(ch) for ch in "。！？!?；;") / n

    scores = {k: sum(1 for w in words if w in t) for k, words in _MARKERS.items()}
    scores["dialogue"] = q * 40
    scores["quiet"] = (1.5 if punct < 0.02 else 0.0) + (1.0 if q < 0.03 else 0.0)
    if band == "front":
        scores["opening"] = scores.get("opening", 0) + 2.0
    if is_chapter_end:
        scores["ending"] = scores.get("ending", 0) + 2.0
    if band == "back":
        scores["ending"] = scores.get("ending", 0) + 0.8

    # 对话优先于其它弱信号：引号占了四分之一以上就不可能是纯描写。
    if q >= 0.25:
        return "dialogue"
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] <= 0.5:
        return "other"
    if best[0] == "description" and q >= 0.08:
        return "argument"
    return best[0]


def _hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")
                        ).hexdigest()


def _paragraph_offsets(chunk: ChunkView) -> list[int]:
    starts = [off for off, ln in chunk.paragraph_offsets
              if ln > 0 and 0 <= off < len(chunk.text)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    return sorted(set(starts))


def window_for(chunk: ChunkView, stable_book_id: str = "", *,
               max_chars: int = MAX_WINDOW_CHARS,
               min_chars: int = MIN_WINDOW_CHARS) -> dict[str, Any]:
    """从一块里取一个**逐字**窗口。返回 ``{start,end,text,key}``。

    窗口必须能原样在块里找到 —— ``author_corpus`` 只存 excerpt，
    重跑时靠它回到 ``book_chunks`` 重新定位（与 ``harvest`` 同一条）。
    """
    text = chunk.text or ""
    if len(text) <= max_chars:
        start, end = 0, len(text)
    else:
        starts = _paragraph_offsets(chunk)
        pick = int(_hash(stable_book_id, chunk.chunk_id, "start")[:8], 16)
        pos = pick % len(starts)
        start = starts[pos]
        end = min(len(text), start + max_chars)
        # 从段落起点往后攒到够长；攒不够就往前借到 min_chars。
        if end - start < min_chars:
            start = max(0, end - min_chars)
        # 收尾尽量落在句读上，不把一句话切成两半。
        if end < len(text):
            cut = max(text.rfind(ch, start + min_chars // 2, end)
                      for ch in "。！？!?\n")
            if cut > start + min_chars // 2:
                end = cut + 1
    body = text[start:end]
    key = _hash(stable_book_id, chunk.chunk_id, start, end)[:16]
    return {"start": start, "end": end, "text": body, "key": key}


def _chapter_end_map(chunks: list[ChunkView]) -> dict[str, bool]:
    last: dict[int, int] = {}
    for c in chunks:
        last[c.chapter_index] = max(last.get(c.chapter_index, -1), c.chunk_index)
    return {c.chunk_id: c.chunk_index == last.get(c.chapter_index)
            for c in chunks}


def sample_windows(chunks: list[ChunkView], *, stable_book_id: str = "",
                   candidates: list[dict[str, Any]] | None = None,
                   max_samples: int = MAX_SAMPLES,
                   max_input_chars: int = MAX_INPUT_CHARS
                   ) -> tuple[list[dict[str, Any]], list[str]]:
    """选窗口。返回 ``(samples, warnings)``，samples 按阅读顺序排。

    候选只当建议：仍要过一遍可用性、每章配额与场景覆盖 ——
    Scout 可能把 12 个候选全选在开头（提示词里明确禁止，但**不靠它自律**）。

    每个 sample 的形状::

        {chunk_id, chapter_index, chunk_index, chapter_title, band,
         start, end, text, scene_hint, why, key}
    """
    warnings: list[str] = []
    max_samples = max(1, min(int(max_samples), MAX_SAMPLES))
    by_id = {c.chunk_id: c for c in chunks}
    ch_end = _chapter_end_map(chunks)
    per_chapter: dict[int, int] = {}
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    # 每章配额不是固定 2：短篇集/散文集一章可能只有两块，而长篇三章
    # 就装下全书 —— 固定 2 会把 12 段的预算卡在 6 段（实测《房思琪》）。
    # 按章数摊，下限 2 保分散，上限就是总预算。
    n_chapters = max(1, len({c.chapter_index for c in chunks}))
    per_chapter_cap = max(2, min(max_samples,
                                 (max_samples + n_chapters - 1) // n_chapters + 1))

    def _add(chunk: ChunkView, hint: str = "", why: str = "") -> bool:
        if (chunk.chunk_id in used
                or per_chapter.get(chunk.chapter_index, 0) >= per_chapter_cap):
            return False
        win = window_for(chunk, stable_book_id)
        if hint not in SCENE_TYPES:
            hint = ""
        scene = hint or scene_hint(chunk.text, band=chunk.band,
                                   is_chapter_end=ch_end.get(chunk.chunk_id, False))
        out.append({
            "chunk_id": chunk.chunk_id,
            "chapter_index": chunk.chapter_index,
            "chunk_index": chunk.chunk_index,
            "chapter_title": chunk.chapter_title,
            "band": chunk.band,
            "start": win["start"], "end": win["end"], "text": win["text"],
            "scene_hint": scene, "why": why, "key": win["key"],
        })
        used.add(chunk.chunk_id)
        per_chapter[chunk.chapter_index] = per_chapter.get(chunk.chapter_index, 0) + 1
        return True

    # 1) Scout 候选优先（仍受每章 2 段与总数上限约束）
    for c in (candidates or []):
        if len(out) >= max_samples:
            break
        cid = str(c.get("chunk_id") or "").strip()
        chunk = by_id.get(cid)
        if chunk is None:
            warnings.append(f"候选块 {cid or '(空)'} 不在可采样集合里，已跳过")
            continue
        _add(chunk, str(c.get("scene_hint") or ""), str(c.get("why") or ""))
    if candidates and not out:
        warnings.append("Scout 候选全部不可用 —— 退回确定性抽样")

    # 2) 确定性兜底：先补场景种类，再补数量
    order = sorted(chunks, key=lambda c: _hash(stable_book_id, c.chunk_id))
    hints = {c.chunk_id: scene_hint(
        c.text, band=c.band, is_chapter_end=ch_end.get(c.chunk_id, False))
        for c in order}
    seen_scenes = {s["scene_hint"] for s in out}
    for c in order:
        if len(out) >= max_samples:
            break
        if hints[c.chunk_id] not in seen_scenes:
            if _add(c, hints[c.chunk_id], "确定性补场景"):
                seen_scenes.add(hints[c.chunk_id])
    for c in order:
        if len(out) >= max_samples:
            break
        _add(c, hints[c.chunk_id], "确定性补量")

    # 3) 预算：总字数超了就从尾部丢（尾部是补量来的，先丢它）。
    total = sum(len(s["text"]) for s in out)
    while total > max_input_chars and len(out) > 1:
        gone = out.pop()
        total -= len(gone["text"])
        warnings.append(f"输入预算 {max_input_chars} 字用尽，去掉 {gone['chunk_id']}")

    scenes = {s["scene_hint"] for s in out}
    quiet = sum(1 for s in out if s["scene_hint"] in _QUIET_LIKE)
    if len(scenes) < MIN_SCENE_KINDS:
        warnings.append(
            f"场景覆盖只有 {len(scenes)} 类（目标 ≥{MIN_SCENE_KINDS}）——"
            f"样本可能偏向单一写法")
    if quiet < MIN_QUIET:
        warnings.append(f"安静段只有 {quiet} 段（目标 ≥{MIN_QUIET}）"
                        f"—— 全书可能缺少这类段落，不是抽样的错")

    out.sort(key=lambda s: (s["chapter_index"], s["chunk_index"], s["start"]))
    return out, warnings


__all__ = ["MAX_INPUT_CHARS", "MAX_SAMPLES", "MIN_SCENE_KINDS",
           "MIN_WINDOW_CHARS", "MAX_WINDOW_CHARS", "quote_share",
           "sample_windows", "scene_hint", "window_for"]
