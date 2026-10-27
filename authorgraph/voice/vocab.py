"""声音层的词表与归一 —— 一个**纯配置**模块，不碰库、不调模型。

为什么要独立一份：模型输出里的值有三类（认识的 / 同义的 / 不认识的），
三类的处置不同（收下 / 归一到规范值 / 收成 ``variable`` 并报）。把这个
判断散在提示词和落库两边，迟早会出现「提示词说 close，库里存 贴近」。
"""

from __future__ import annotations

from storage.authors_db import (NARRATIVE_LAYERS, SCENE_TYPES,
                                VOICE_DIMENSIONS, VOICE_DOMAINS)

#: 提示词 + 抽取规则的版本。进 ``author_voice_runs.prompt_version`` 与
#: trait revision。**改提示词、抽样规则、归一化规则都要改它** ——
#: 否则代码变了、内容 hash 没变，「同一批语料重跑」会被误判成命中缓存。
PROMPT_VERSION = "author-voice-v3"

#: 维度 → 允许的规范值。``variable`` 是**所有维度共用的兜底**：
#: 设计稿说「不认识的值收成 variable 并报」，而多数维度的词表里没有它 ——
#: 这不是矛盾，是「词表是提示词的上界，variable 是数据的出口」。
VALUES: dict[str, tuple[str, ...]] = {
    "narrative_distance": ("intimate", "close", "medium", "distant"),
    "sentence_rhythm": ("short", "balanced", "long", "varied"),
    "paragraph_shape": ("fragmented", "flowing", "blocky", "list_like"),
    "dialogue_density": ("low", "medium", "high"),
    "figurative_language": ("sparse", "medium", "dense"),
    "emotional_expression": ("explicit", "action_mediated", "ironic",
                             "withheld"),
    "information_reveal": ("front_loaded", "delayed", "fragmented",
                           "gradual"),
    "lexical_texture": ("plain", "literary", "colloquial", "archaic",
                        "technical", "mixed"),
    "humor_irony": ("none", "dry", "warm", "absurd", "bitter"),
    "closure_pattern": ("resolved", "open", "elliptical", "reversal"),
}

#: 同义说法 → 规范值。只收**确定等价**的（中英文、连字符/下划线变体、
#: 常见缩写）；近义词不在这里合并 —— 那会改变判断，不是归一。
_ALIASES: dict[str, dict[str, str]] = {
    "narrative_distance": {
        "intimate": "intimate", "亲密": "intimate", "极近": "intimate",
        "close": "close", "贴近": "close", "近": "close", "近距": "close",
        "medium": "medium", "中": "medium", "适中": "medium",
        "distant": "distant", "远": "distant", "疏离": "distant",
        "冷静": "distant",
    },
    "sentence_rhythm": {
        "short": "short", "短": "short", "短句": "short",
        "balanced": "balanced", "均衡": "balanced", "中等": "balanced",
        "long": "long", "长": "long", "长句": "long",
        "varied": "varied", "变化": "varied", "长短交错": "varied",
    },
    "paragraph_shape": {
        "fragmented": "fragmented", "碎片": "fragmented", "断裂": "fragmented",
        "flowing": "flowing", "流动": "flowing", "连贯": "flowing",
        "blocky": "blocky", "块状": "blocky", "大段": "blocky",
        "list_like": "list_like", "listlike": "list_like", "列表": "list_like",
    },
    "dialogue_density": {
        "low": "low", "低": "low", "少": "low",
        "medium": "medium", "中": "medium", "适中": "medium",
        "high": "high", "高": "high", "多": "high",
    },
    "figurative_language": {
        "sparse": "sparse", "少": "sparse", "稀疏": "sparse", "克制": "sparse",
        "medium": "medium", "中": "medium",
        "dense": "dense", "密": "dense", "密集": "dense",
    },
    "emotional_expression": {
        "explicit": "explicit", "直接": "explicit", "外显": "explicit",
        "action_mediated": "action_mediated", "actionmediated": "action_mediated",
        "动作承接": "action_mediated", "动作": "action_mediated",
        "ironic": "ironic", "反讽": "ironic",
        "withheld": "withheld", "克制": "withheld", "留白": "withheld",
    },
    "information_reveal": {
        "front_loaded": "front_loaded", "frontloaded": "front_loaded",
        "前置": "front_loaded", "先给结论": "front_loaded",
        "delayed": "delayed", "延迟": "delayed",
        "fragmented": "fragmented", "碎片": "fragmented",
        "gradual": "gradual", "渐进": "gradual",
    },
    "lexical_texture": {
        "plain": "plain", "朴素": "plain", "平实": "plain",
        "literary": "literary", "文学": "literary", "书面": "literary",
        "colloquial": "colloquial", "口语": "colloquial",
        "archaic": "archaic", "古旧": "archaic", "文言": "archaic",
        "technical": "technical", "技术": "technical", "术语": "technical",
        "mixed": "mixed", "混合": "mixed",
    },
    "humor_irony": {
        "none": "none", "无": "none",
        "dry": "dry", "冷": "dry", "冷幽默": "dry",
        "warm": "warm", "温和": "warm",
        "absurd": "absurd", "荒诞": "absurd",
        "bitter": "bitter", "苦涩": "bitter", "尖刻": "bitter",
    },
    "closure_pattern": {
        "resolved": "resolved", "闭合": "resolved", "解决": "resolved",
        "open": "open", "开放": "open", "留白": "open",
        "elliptical": "elliptical", "省略": "elliptical", "断句": "elliptical",
        "reversal": "reversal", "反转": "reversal",
    },
}

#: 视角。**不进 trait**（设计稿 04 §五补充）—— 同一作者可能多视角。
POVS = ("first", "third_limited", "third_omniscient", "second",
        "mixed", "unknown")

#: 时期。只在同一位作者跨时期差异明显时使用。
PERIODS = ("early", "mid", "late")

#: 写作透镜只认这两类叙事层（设计稿 06 §3）。
LENS_LAYERS = ("narration", "essay")


def norm_value(dimension: str, raw: str) -> tuple[str, str]:
    """``(规范值, 缺陷码)``；缺陷码为空 = 认识。

    不认识的维度返回 ``("", "unknown_dimension")`` —— 调用方负责丢弃。
    """
    dim = (dimension or "").strip()
    if dim not in VOICE_DIMENSIONS:
        return "", "unknown_dimension"
    val = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not val:
        return "variable", "unknown_value"
    if val == "variable" or val == "未知":
        return "variable", ""
    if val in VALUES.get(dim, ()):
        return val, ""
    alias = _ALIASES.get(dim, {}).get(val)
    if alias:
        return alias, ""
    return "variable", "unknown_value"


def norm_scene(raw: str) -> str:
    val = (raw or "").strip().lower()
    return val if val in SCENE_TYPES else "other"


def norm_layer(raw: str) -> str:
    val = (raw or "").strip().lower()
    return val if val in NARRATIVE_LAYERS else "narration"


def norm_pov(raw: str) -> str:
    val = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    alias = {"第一人称": "first", "第三人称": "third_limited",
             "third": "third_limited", "omniscient": "third_omniscient",
             "limited": "third_limited", "第一": "first", "第三": "third_limited"}
    if val in POVS:
        return val
    return alias.get(val, "unknown")


def norm_period(raw: str) -> str:
    val = (raw or "").strip().lower()
    alias = {"早期": "early", "中期": "mid", "晚期": "late",
             "后期": "late", "初期": "early"}
    if val in PERIODS:
        return val
    return alias.get(val, "")


def norm_domain(raw: str, fallback: str = "unknown") -> tuple[str, bool]:
    """``(规范域, 是否推断)``。**``paratext/public`` 不会被推断出来**。"""
    val = (raw or "").strip().lower()
    if val in VOICE_DOMAINS:
        return val, False
    return (fallback if fallback in VOICE_DOMAINS else "unknown"), True


__all__ = [
    "LENS_LAYERS", "PERIODS", "POVS", "PROMPT_VERSION", "SCENE_TYPES",
    "VALUES", "VOICE_DIMENSIONS", "VOICE_DOMAINS", "NARRATIVE_LAYERS",
    "norm_domain", "norm_layer", "norm_period", "norm_pov", "norm_scene",
    "norm_value",
]
