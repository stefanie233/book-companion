"""去 AI 味 —— 纯 Python 确定性检测器，不调模型、不自动改稿。

词表是**检索入口，不是禁词表**：命中只生成提示，不判失败。
只有 ``tier='blocking'`` 的七式与工程词泄漏会进入审稿的硬问题清单，
其余密度项按「绝对次数 + 每千字」双门槛报。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

BLOCKING = "blocking"
DENSITY = "density"
DEGENERATION = "degeneration"

#: 七式。每条给一个正则和一句人话。
_BLOCKING_RULES = (
    ("not-is", re.compile(r"不是[^。！？\n]{1,40}?[，,]?\s*(?:而是|是)"),
     "「不是 A，而是 B」：改成直接陈述 B"),
    ("reverse-not", re.compile(r"[^。！？\n]{1,30}是[^。！？\n]{0,20}[，,]\s*不是"),
     "「是 A，不是 B」：同上，留肯定句"),
    ("negation-parade", re.compile(r"没有[^。！？\n]{1,20}[，,]\s*没有[^。！？\n]{1,20}[，,]\s*只有"),
     "「没有 X，没有 Y，只有 Z」：拆成具体动作"),
    ("voice-contrast", re.compile(r"[^。！？\n]{1,14}(?:却|但)[^。！？\n]{0,16}(?:不容置疑|不可忽视|令人|让人|压过)"),
     "「声音不大，却带着……的力量」：删掉评价，留可见细节"),
    ("em-dash", re.compile(r"——"),
     "破折号：改成动作、停顿或完整句"),
    ("trailer-ending", re.compile(r"(?:他|她|他们|她们)不知道的是|故事才(?:刚刚)?开始|真正的[^。！？\n]{0,10}才(?:刚刚)?开始|而这只是开始"),
     "预告式结尾：删除，或落回具体情境"),
    ("ending-summary", re.compile(r"这一(?:夜|天|刻)[，,][^。！？\n]{0,20}(?:注定|无人|再也)"),
     "章末总结腔：删除，让场景自己收尾"),
)

#: 套式微动作 / 反应 / 套词 / 比喻。
_MICRO_ACTIONS = re.compile(r"点了点头|叹了口气|皱了皱眉|抿了抿嘴|顿了顿|笑了笑|摇了摇头|咬了咬牙")
_REACTIONS = re.compile(r"心里一紧|心头一震|呼吸一滞|瞳孔一缩|浑身一震|后背发凉|心中一凛")
_CLICHES = re.compile(r"空气(?:仿佛)?凝固|时间(?:仿佛)?静止|不易察觉|微不可察|若有若无|肉眼可见")
_HEDGES = re.compile(r"仿佛|似乎|像是|某种程度|不知为何")
_METAPHORS = re.compile(r"像|如同|仿佛|宛如|好似")

#: 模型退化信号 —— 直接暴露「这是生成物」。
_REFUSAL = re.compile(r"作为(?:一个)?AI|我无法继续|此处省略|TODO|待补充")
_ENGINE_WORDS = re.compile(r"细纲|情节点|卷纲|伏笔|读者预期|第\d+章")
_VERBS = re.compile(r"拿起|放下|转身|推开|走到|站起|坐下|抬头|低头|看向|伸手|点头|摇头|开口|皱眉|叹气")
_END_PUNCT = "。！？…」』\"'）)】”’"


@dataclass
class DeslopIssue:
    kind: str
    tier: str
    message: str
    count: int = 1
    examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "tier": self.tier,
                "message": self.message, "count": self.count,
                "examples": self.examples[:3]}


def _examples(pattern: re.Pattern[str], text: str, limit: int = 3) -> list[str]:
    return [m.group(0).strip() for m in list(pattern.finditer(text))[:limit]]


def _rate(count: int, chars: int) -> float:
    return count * 1000.0 / max(1, chars)


def _sentences(text: str) -> list[list[str]]:
    """按段落切句，段落边界重置「连续短句」的计数。"""
    out: list[list[str]] = []
    for para in (text or "").split("\n"):
        parts = [s.strip() for s in re.split(r"[。！？]", para) if s.strip()]
        if parts:
            out.append(parts)
    return out


def _blocking(text: str) -> list[DeslopIssue]:
    issues: list[DeslopIssue] = []
    for kind, pattern, message in _BLOCKING_RULES:
        hits = pattern.findall(text)
        count = len(hits)
        if count:
            issues.append(DeslopIssue(kind, BLOCKING, message, count,
                                      _examples(pattern, text)))
    return issues


def _density(text: str) -> list[DeslopIssue]:
    chars = len(text)
    issues: list[DeslopIssue] = []

    runs = 0
    for para in _sentences(text):
        streak = 0
        for sentence in para:
            streak = streak + 1 if len(sentence) <= 5 else 0
            if streak == 6:
                runs += 1
    if runs:
        issues.append(DeslopIssue(
            "short-sentence-run", DENSITY,
            "连续 6 个叙述短句（每句 ≤5 字）：并成长句或删", runs))

    long_paras = [p for p in text.split("\n") if len(p) > 200]
    if long_paras:
        issues.append(DeslopIssue(
            "long-paragraph", DENSITY,
            "单段超过 200 字：在动作或视角切换处断开", len(long_paras),
            [p[:60] + "…" for p in long_paras[:2]]))

    checks = (
        ("micro-action-repeat", _MICRO_ACTIONS, 5, 6.0,
         "微动作复读（点头/叹气/皱眉……）：换成这一场特有的动作"),
        ("formulaic-reaction", _REACTIONS, 4, 1.5,
         "套式反应（心里一紧/瞳孔一缩……）：写身体做了什么"),
        ("hedge-density", _HEDGES, 3, 4.0,
         "「仿佛/似乎/像是」过密：能确定就直说"),
        ("cliche-density", _CLICHES, 8, 12.0,
         "套词密度过高：换成可验证的细节"),
        ("metaphor-density", _METAPHORS, 7, 3.0,
         "比喻密度过高：留最准的一处"),
    )
    for kind, pattern, floor, rate_floor, message in checks:
        count = len(pattern.findall(text))
        if count >= floor and _rate(count, chars) >= rate_floor:
            issues.append(DeslopIssue(kind, DENSITY, message, count,
                                      _examples(pattern, text)))

    for para in text.split("\n"):
        verbs = len(_VERBS.findall(para))
        separators = len(re.findall(r"[、，]", para))
        if verbs >= 5 and separators >= 4:
            issues.append(DeslopIssue(
                "action-list", DENSITY,
                "同段通用动作清单：挑两个关键动作，其余删掉", verbs,
                [para[:60] + "…"]))
            break
    return issues


def _degeneration(text: str) -> list[DeslopIssue]:
    issues: list[DeslopIssue] = []

    counts: dict[str, int] = {}
    for para in _sentences(text):
        for sentence in para:
            if len(sentence) >= 20:
                counts[sentence] = counts.get(sentence, 0) + 1
    repeated = {s: n for s, n in counts.items() if n >= 3}
    if repeated:
        issues.append(DeslopIssue(
            "verbatim-repeat", DEGENERATION,
            "长句逐字复读（≥3 次）", sum(repeated.values()),
            [s[:60] for s in list(repeated)[:2]]))

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    adjacent = sum(1 for a, b in zip(lines, lines[1:]) if a == b)
    if adjacent:
        issues.append(DeslopIssue(
            "adjacent-repeat", DEGENERATION,
            "相邻整行重复", adjacent))

    tail = text.rstrip()
    if tail and tail[-1] not in _END_PUNCT:
        issues.append(DeslopIssue(
            "no-terminal-punct", DEGENERATION,
            "结尾没有终止标点，正文可能被截断", 1, [tail[-30:]]))

    refusals = _REFUSAL.findall(text)
    if refusals:
        issues.append(DeslopIssue(
            "refusal", BLOCKING, "出现拒绝/占位语", len(refusals),
            list(refusals)[:3]))

    engine = _ENGINE_WORDS.findall(text)
    if engine:
        issues.append(DeslopIssue(
            "engine-word", BLOCKING,
            "正文泄漏工程词（细纲/卷纲/伏笔/读者/章号）：直接暴露生成过程",
            len(engine), list(engine)[:3]))
    return issues


def detect(text: str) -> dict[str, Any]:
    """跑全部确定性检查。返回可直接渲染/落库的结构。"""
    text = text or ""
    issues = _blocking(text) + _density(text) + _degeneration(text)
    order = {BLOCKING: 0, DEGENERATION: 1, DENSITY: 2}
    issues.sort(key=lambda i: (order[i.tier], -i.count, i.kind))
    return {
        "chars": len(text),
        "issues": [i.as_dict() for i in issues],
        "blocking": sum(1 for i in issues if i.tier == BLOCKING),
    }
