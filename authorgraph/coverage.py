"""作者档案的覆盖度 —— 纯函数，不查库、不调模型。

覆盖度不是考试分数，是**「还缺什么」的索引**。所以每个分项都同时返回
原始计数与缺口名；UI 的 chips 用计数，``missing`` 用缺口名。

权重按设计稿 05 §三.3：事实 0.30 / 作品 0.15 / 声音 0.30 / 关系 0.15 /
来源 0.10。**声音与事实同权**：作者档案的价值一半在「他是谁」，一半在
「他怎么说话」。
"""

from __future__ import annotations

from typing import Any, Iterable

WEIGHTS = {
    "facts": 0.30,
    "works": 0.15,
    "voice": 0.30,
    "relations": 0.15,
    "sources": 0.10,
}

#: 各项「满格」的经验阈值。不是 KPI —— 到了就够用，再采收益递减。
EXPECTED = {
    "facts": 12,          # 有逐字证据的编年条目
    "works": 6,           # 不同作品
    "samples": 12,        # 声音样本
    "scenes": 5,          # 不同场景
    "relations": 6,       # 已确认关系
    "sources": 8,         # 已核验来源
}

#: 12 类场景。缺口按这个顺序报，稳定可测。
SCENE_ORDER = ("opening", "transition", "dialogue", "description", "action",
               "introspection", "emotional_peak", "quiet", "argument",
               "landscape", "memory", "ending", "other")


def _ratio(n: float, full: float) -> float:
    return max(0.0, min(1.0, float(n) / float(full))) if full else 0.0


def coverage_block(*, records: int = 0, records_with_evidence: int = 0,
                   works: int = 0, voice_samples: int = 0, scenes: int = 0,
                   scene_types: Iterable[str] = (), relations: int = 0,
                   verified_sources: int = 0, sources: int = 0,
                   traits: int = 0, dimensions: int = 0) -> dict[str, Any]:
    """算五个分项 + 加权总分 + 最缺的 2–3 项。

    ``scene_types`` 用来报具体缺口（``voice.scene:ending``），比只说
    「声音不够」有用 —— 用户知道该去补哪类段落。
    """
    present = {str(s) for s in (scene_types or ()) if s}
    missing_scenes = [s for s in SCENE_ORDER if s not in present and s != "other"]

    facts = _ratio(records_with_evidence, EXPECTED["facts"])
    works_v = _ratio(works, EXPECTED["works"])
    # 声音 = 量 × 场景覆盖。只堆同一类场景的段落不算「有声」。
    voice = _ratio(voice_samples, EXPECTED["samples"]) * _ratio(
        scenes, EXPECTED["scenes"])
    relations_v = _ratio(relations, EXPECTED["relations"])
    sources_v = _ratio(verified_sources, EXPECTED["sources"])

    parts = {"facts": facts, "works": works_v, "voice": voice,
             "relations": relations_v, "sources": sources_v}
    overall = sum(parts[k] * WEIGHTS[k] for k in WEIGHTS)

    gaps: list[tuple[float, str]] = []
    if records_with_evidence < EXPECTED["facts"]:
        gaps.append((WEIGHTS["facts"] * (1 - facts),
                     "facts" if records_with_evidence else "facts.none"))
    if works < EXPECTED["works"]:
        gaps.append((WEIGHTS["works"] * (1 - works_v), "works"))
    if voice_samples == 0:
        gaps.append((WEIGHTS["voice"], "voice.none"))
    elif scenes < EXPECTED["scenes"]:
        gaps.append((WEIGHTS["voice"] * (1 - voice), "voice.scene"))
    elif missing_scenes:
        gaps.append((WEIGHTS["voice"] * (1 - voice),
                     f"voice.scene:{missing_scenes[0]}"))
    if relations == 0:
        gaps.append((WEIGHTS["relations"], "relations"))
    elif relations < EXPECTED["relations"]:
        gaps.append((WEIGHTS["relations"] * (1 - relations_v), "relations"))
    if verified_sources < EXPECTED["sources"]:
        gaps.append((WEIGHTS["sources"] * (1 - sources_v), "sources"))
    gaps.sort(key=lambda x: (-x[0], x[1]))
    missing = [name for _, name in gaps[:3]]

    return {
        "facts": round(facts, 4),
        "works": round(works_v, 4),
        "voice": round(voice, 4),
        "relations": round(relations_v, 4),
        "sources": round(sources_v, 4),
        "overall": round(overall, 4),
        "missing": missing,
        "counts": {
            "records": int(records),
            "records_with_evidence": int(records_with_evidence),
            "works": int(works),
            "voice_samples": int(voice_samples),
            "scenes": int(scenes),
            "relations": int(relations),
            "sources": int(sources),
            "verified_sources": int(verified_sources),
            "traits": int(traits),
            "dimensions": int(dimensions),
        },
        "missing_scenes": missing_scenes,
    }


__all__ = ["WEIGHTS", "EXPECTED", "SCENE_ORDER", "coverage_block"]
