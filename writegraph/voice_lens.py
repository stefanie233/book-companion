"""作者声音透镜 —— 纯函数：打分、去重、裁剪，不读库、不渲染。

设计稿 06 §三：**检索键是当前场景，不是作者 ID**。同一个作者在不同
作品/时期/场景里的写法差别很大，所以这里只做「在已通过可见性闸门的
候选里，挑出对本章表达层最有用的几段」。

上游 ``authorgraph.voice.select`` 已经保证合规（正文层、小说/非虚构、
``transferable=1``）；本模块再按场景重排，并做设计要求的去重与冲突排除。
下游 ``writegraph.voice_card`` 只负责排版。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from authorgraph.voice.vocab import VALUES, VOICE_DIMENSIONS

#: 场景相近组：精确命中记 1.0，同组记 0.5。没有「同义场景」的词表，
#: 只有这三组是不言自明的（都在写人物内部 / 都在写对话 / 都在写场面）。
_SCENE_GROUPS = (
    frozenset({"introspection", "emotional_peak", "memory"}),
    frozenset({"dialogue", "argument"}),
    frozenset({"description", "landscape", "action", "quiet", "opening",
               "transition", "ending"}),
)

MAX_RULE_CHARS = 40
MAX_SAMPLE_CHARS = 300
MAX_RULES = 8
MAX_SAMPLES = 5

#: 维度 × 规范值 → 一句写作提示。**用映射而不是模型原话**：原话可能带
#: 作者专名、角色口癖或生平事实，一旦进 prompt 就违反设计稿 06 §四.2。
#: 这里只写表达层，不写「必须/禁止」。
RULE_TEXT: dict[str, dict[str, str]] = {
    "narrative_distance": {
        "intimate": "叙述距离极近，贴着人物的感官与身体写。",
        "close": "叙述距离偏近，用身体感受和具体动作承载情绪。",
        "medium": "叙述距离适中，少替人物解释内心。",
        "distant": "叙述保持距离，让事实与动作自己说话。",
    },
    "sentence_rhythm": {
        "short": "句子以短句为主，少用总结句和升华句。",
        "balanced": "长短句交替，避免连续三个长句。",
        "long": "允许长句延展，但一句只推进一件事。",
        "varied": "句长随场景变化，紧张处收短。",
    },
    "paragraph_shape": {
        "fragmented": "段落偏短、可断裂，转场不做解释性过渡。",
        "flowing": "段落连贯流动，靠动作与意象衔接。",
        "blocky": "段落成块，一段写完一个完整推进。",
        "list_like": "可用清单式短段，但每段要有新信息。",
    },
    "dialogue_density": {
        "low": "对话少，留白多，让沉默参与叙事。",
        "medium": "对话与叙述大致均衡。",
        "high": "对话密集，冲突尽量在台词里发生。",
    },
    "figurative_language": {
        "sparse": "比喻克制，一个场景最多一处。",
        "medium": "比喻适量，优先用日常物件作比。",
        "dense": "意象密集，但每个比喻都要落在具体物上。",
    },
    "emotional_expression": {
        "explicit": "情绪可以直接命名，但要紧贴触发它的事。",
        "action_mediated": "情绪不直接命名，用停顿、动作、物件承接。",
        "ironic": "用反讽拉开情绪，避免直接抒情。",
        "withheld": "情绪留白，只写可见的动作与后果。",
    },
    "information_reveal": {
        "front_loaded": "关键信息前置，先给结果再补过程。",
        "delayed": "关键信息延后，让读者先看见异常。",
        "fragmented": "信息碎片化给出，靠读者自行拼接。",
        "gradual": "信息渐进释放，每段只多给一点。",
    },
    "lexical_texture": {
        "plain": "用词朴素，少形容词，让动词承担重量。",
        "literary": "用词偏书面，允许文雅但不堆砌。",
        "colloquial": "用词口语化，允许方言与语气词。",
        "archaic": "用词古旧，注意与叙述语境一致。",
        "technical": "允许术语，但首次出现要让人读懂。",
        "mixed": "语体可混，但一段之内保持统一。",
    },
    "humor_irony": {
        "none": "不刻意制造幽默。",
        "dry": "冷幽默，点到即止，不解释笑点。",
        "warm": "温和的幽默，来自人物关系而非段子。",
        "absurd": "荒诞感来自情境错位，不靠夸张形容词。",
        "bitter": "苦涩的反讽，少用感叹。",
    },
    "closure_pattern": {
        "resolved": "段末收束，给读者一个明确的落点。",
        "open": "段末留白，不解释、不总结。",
        "elliptical": "段末用省略与跳跃结束，余味重于交代。",
        "reversal": "段末可轻微反转，但不要每段都翻。",
    },
}


def _clean(text: Any, limit: int) -> str:
    """压平空白再截断；prompt 里的换行会把「一条规则」拆成两段。"""
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip("，。；、 ") + "…"


def _scene_tag(target: str, actual: str) -> float:
    if not target:
        return 0.0
    if actual == target:
        return 1.0
    for group in _SCENE_GROUPS:
        if target in group and actual in group:
            return 0.5
    return 0.0


def _keywords(scene_profile: Mapping[str, Any],
              contract: Mapping[str, Any] | None) -> list[str]:
    """场景关键词：显式给的 + 合同里能判定的短词。

    合同句子往往很长，整句命中几乎不可能；把 ``must_hit_now`` /
    ``must_preserve`` 里 2–12 字的片段当关键词，命中率才有意义。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (scene_profile.get("keywords") or []):
        text = str(raw).strip()
        if 2 <= len(text) <= 16 and text not in seen:
            seen.add(text)
            out.append(text)
    if contract:
        for key in ("must_hit_now", "must_preserve"):
            for raw in (contract.get(key) or []):
                text = "".join(str(raw).split())
                if 2 <= len(text) <= 12 and text not in seen:
                    seen.add(text)
                    out.append(text)
    return out[:12]


def _forbidden_terms(contract: Mapping[str, Any] | None) -> list[str]:
    """``forbidden_cross`` 里可机械判定的短词。整句太长，先剥掉禁用词头。"""
    if not contract:
        return []
    out: list[str] = []
    for raw in (contract.get("forbidden_cross") or []):
        text = "".join(str(raw).split())
        for prefix in ("不能写", "不得写", "禁止", "避免", "不要写", "不许写"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        if 2 <= len(text) <= 16:
            out.append(text)
    return out


def _score(sample: Mapping[str, Any], scene_profile: Mapping[str, Any],
           keywords: Sequence[str]) -> float:
    """``0.40 tag + 0.25 keyword + 0.15 period + 0.10 pov + 0.10 quality``。"""
    target = str(scene_profile.get("scene_type") or "")
    tag = _scene_tag(target, str(sample.get("scene_type") or ""))
    excerpt = str(sample.get("excerpt") or "")
    if keywords:
        hits = sum(1 for k in keywords if k in excerpt)
        kw = min(1.0, hits / len(keywords))
    else:
        kw = 0.0
    period = str(scene_profile.get("period") or "")
    pd = 1.0 if period and str(sample.get("period") or "") == period else 0.0
    pov = str(scene_profile.get("pov") or "")
    pm = 1.0 if pov and str(sample.get("pov") or "") == pov else 0.0
    tier = int(sample.get("source_tier") or 3)
    quality = {1: 1.0, 2: 0.9, 3: 0.75}.get(tier, 0.5)
    return round(0.40 * tag + 0.25 * kw + 0.15 * pd + 0.10 * pm
                 + 0.10 * quality, 4)


def rank_traits(traits: Iterable[Mapping[str, Any]], *,
                max_rules: int = MAX_RULES) -> tuple[list[dict[str, Any]],
                                                     list[str]]:
    """选规则：每维度一条，只认规范值；不认识的值不猜。"""
    warnings: list[str] = []
    picked: dict[str, dict[str, Any]] = {}
    skipped = 0
    for trait in traits:
        dim = str(trait.get("dimension") or "")
        value = str(trait.get("value") or "")
        if dim not in VOICE_DIMENSIONS:
            skipped += 1
            continue
        if trait.get("transferable") in (0, False):
            skipped += 1
            continue
        domain = str(trait.get("voice_domain") or "unknown")
        if domain not in ("fiction", "nonfiction"):
            skipped += 1
            continue
        rule = RULE_TEXT.get(dim, {}).get(value)
        # ``variable`` 与词表外的值没有可靠映射；用模型原话会带专名。
        if not rule or value not in VALUES.get(dim, ()):
            skipped += 1
            continue
        current = picked.get(dim)
        conf = float(trait.get("confidence") or 0.0)
        if current is None or conf > float(current.get("confidence") or 0.0):
            picked[dim] = {
                "dimension": dim, "value": value,
                "text": _clean(rule, MAX_RULE_CHARS),
                "confidence": round(conf, 3),
                "trait_id": trait.get("trait_id") or "",
            }
    if skipped:
        warnings.append(f"有 {skipped} 条文风特征不可迁移或没有可靠映射，未采用")
    order = {dim: i for i, dim in enumerate(VOICE_DIMENSIONS)}
    rules = sorted(picked.values(),
                   key=lambda r: (-float(r["confidence"]),
                                  order.get(r["dimension"], 99)))
    return rules[:max_rules], warnings


def rank_samples(samples: Iterable[Mapping[str, Any]],
                 scene_profile: Mapping[str, Any] | None = None, *,
                 contract: Mapping[str, Any] | None = None,
                 max_samples: int = MAX_SAMPLES,
                 max_chars: int = 1200) -> tuple[list[dict[str, Any]],
                                                 list[str]]:
    """打分 → 排除合同禁忌 → 去重 → 按字符预算截断。"""
    sp = dict(scene_profile or {})
    keywords = _keywords(sp, contract)
    forbidden = _forbidden_terms(contract)
    warnings: list[str] = []

    scored: list[dict[str, Any]] = []
    blocked = 0
    for sample in samples:
        excerpt = _clean(sample.get("excerpt"), MAX_SAMPLE_CHARS)
        if len(excerpt) < 20:
            continue
        haystack = excerpt + " " + str(sample.get("scene_note") or "")
        if any(term in haystack for term in forbidden):
            blocked += 1
            continue
        item = dict(sample)
        item["excerpt"] = excerpt
        item["score"] = _score(sample, sp, keywords)
        scored.append(item)
    if blocked:
        warnings.append(f"有 {blocked} 段音与合同禁忌冲突，已排除")

    scored.sort(key=lambda s: (-float(s.get("score") or 0.0),
                               int(s.get("corpus_id") or 0)))

    # 同一章、同一段原文只留分数最高的一条 —— 同一段话出现两次不是两份证据。
    seen_chapter: set[tuple[str, int]] = set()
    seen_hash: set[str] = set()
    picked: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for item in scored:
        chapter = item.get("chapter_index")
        chapter_key = (str(item.get("stable_book_id") or ""),
                       int(chapter)) if chapter is not None else None
        if chapter_key is not None and chapter_key in seen_chapter:
            continue
        content_hash = str(item.get("content_hash") or "")
        if content_hash and content_hash in seen_hash:
            continue
        text = str(item.get("excerpt") or "")
        if picked and used + len(text) > max_chars:
            truncated = True
            continue
        if chapter_key is not None:
            seen_chapter.add(chapter_key)
        if content_hash:
            seen_hash.add(content_hash)
        used += len(text)
        picked.append(item)
        if len(picked) >= max_samples:
            break
    if truncated:
        warnings.append("例证超出参考风格预算，后面的样本已省略")
    return picked, warnings


def build_lens(samples: Iterable[Mapping[str, Any]],
               traits: Iterable[Mapping[str, Any]],
               *, scene_profile: Mapping[str, Any] | None = None,
               contract: Mapping[str, Any] | None = None,
               max_rules: int = MAX_RULES,
               max_samples: int = MAX_SAMPLES,
               max_sample_chars: int = 1200) -> dict[str, Any]:
    """合一次透镜结果。空结果合法 —— 调用方据此整块省略。"""
    rules, warn_rules = rank_traits(traits, max_rules=max_rules)
    picked, warn_samples = rank_samples(
        samples, scene_profile, contract=contract, max_samples=max_samples,
        max_chars=max_sample_chars)
    warnings = warn_rules + warn_samples
    if not rules and not picked:
        warnings.append("没有可进写作的作者声音")
    return {
        "rules": rules,
        "samples": picked,
        "warnings": warnings,
        "scene": dict(scene_profile or {}),
        "stats": {"rules": len(rules), "samples": len(picked),
                  "sample_chars": sum(len(s["excerpt"]) for s in picked)},
    }


__all__ = ["RULE_TEXT", "build_lens", "rank_samples", "rank_traits"]
