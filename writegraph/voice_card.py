"""把透镜结果排成 prompt 里的一块 —— **纯排版**，不读库、不做判断。

设计稿 06 §四：物理分区、只约束表达层、不出现第一人称扮演、不写生平事实。
块总预算 700 tokens；这里按字符预算收口（中文 1 字 ≈ 0.5 token）。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

SCENE_LABEL = {
    "opening": "开篇", "transition": "过渡", "dialogue": "对话",
    "description": "描写", "action": "动作", "introspection": "内省",
    "emotional_peak": "情绪高点", "quiet": "静场", "argument": "论辩",
    "landscape": "景物", "memory": "回忆", "ending": "收束", "other": "其他",
}

#: 1400 字 ≈ 700 tokens。留一点给标题与脚注。
DEFAULT_MAX_CHARS = 1350


def _label(scene: str) -> str:
    return SCENE_LABEL.get(scene or "", scene or "正文")


def render(author_name: str, lens: Mapping[str, Any], *,
           partial: bool = False,
           max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """渲染 ``## [author_voice]`` 的正文。空规则 + 空例证 → 空串。"""
    rules: Sequence[Mapping[str, Any]] = lens.get("rules") or []
    samples: Sequence[Mapping[str, Any]] = lens.get("samples") or []
    name = (author_name or "").strip() or "参考作者"
    if not rules and not samples:
        return ""

    head = f"## 参考风格：{name}（仅表达层，不覆盖本书正典）"
    footer = ("注意：以上只约束表达方式；人物、事实、时间线、承诺仍以本书"
              "正典为准。")
    lines = [head]
    if partial:
        lines.append("样本不足，仅供轻参考。")

    if rules:
        lines.append("")
        lines.append("写作倾向：")
        for index, rule in enumerate(rules, start=1):
            lines.append(f"{index}. {rule.get('text') or ''}")

    # 例证是**可裁**的；脚注是**不可裁**的。先算前缀与脚注占位，再放例证，
    # 否则预算一紧就把「以正典为准」这句删掉，读者只看到一段风格指令。
    prefix = "\n".join(lines)
    budget = max_chars - len(footer) - 2
    examples: list[str] = []
    truncated = False
    for sample in samples:
        scene = _label(str(sample.get("scene_type") or ""))
        excerpt = str(sample.get("excerpt") or "").strip()
        if not excerpt:
            continue
        line = f"- [{scene} | 书里] “{excerpt}”"
        used = len(prefix) + sum(len(x) + 1 for x in examples)
        if used + len(line) + 1 > budget:
            truncated = True
            break
        examples.append(line)
    if examples:
        lines.append("")
        lines.append("例证：")
        lines.extend(examples)
    if truncated:
        lines.append("…（例证超出参考风格预算，后面的已省略）")
    lines.append("")
    lines.append(footer)
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # 兜底：仍然只切中间，保留脚注。
    room = max(1, max_chars - len(footer) - 2)
    cut = text[:room]
    nl = cut.rfind("\n")
    if nl > room // 2:
        cut = cut[:nl]
    return cut + "\n" + footer


_RULE_LINE = re.compile(r"^\d+\. ")


def counts(text: str) -> dict[str, int]:
    """数**实际排进块里**的规则与例证，给诊断用。

    透镜的候选数可能大于卡片裁完后的数量；诊断说 4 条、块里只有 3 条，
    会让人以为丢了一段而查不到原因。
    """
    lines = (text or "").splitlines()
    return {"rules": sum(1 for line in lines if _RULE_LINE.match(line)),
            "samples": sum(1 for line in lines if line.startswith("- ["))}


__all__ = ["DEFAULT_MAX_CHARS", "SCENE_LABEL", "counts", "render"]
