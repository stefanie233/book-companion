"""七栏上下文卡 —— 脚本派生，不手改，不静默补空。

固定七栏、顺序固定。缺**输入源**（比如没有合同）视为装配失败，
不拿空字符串顶上 —— 空合同会让模型自由发挥，而失败会让人看见。

卡片只给「本章真正需要的切片」；其余素材由透镜召回（见 04）。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from writegraph import facts as facts_mod
from writegraph import promises as promises_mod

SECTIONS = (
    ("position", "当前位置"),
    ("long_constraints", "长期约束"),
    ("characters", "核心角色状态"),
    ("promises", "活跃承诺"),
    ("recent", "近三章速记"),
    ("next_chapter", "下一章承诺"),
    ("risks", "连贯性风险"),
)

#: 七栏缺任何一栏都算装配失败。
SECTION_KEYS = tuple(k for k, _ in SECTIONS)

MAX_CHARACTERS = 20
MAX_RISKS = 5


class CardIncomplete(RuntimeError):
    """输入源缺失。宁可报错，不静默给模型一张空卡。"""


def _profile_rules(work: Mapping[str, Any]) -> list[str]:
    raw = work.get("style_profile") or "{}"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    rules: list[str] = []
    # 叙事视角是作品级硬约束，但以前只在生成提示词的格式里有目标字数，
    # 视角从没进过模型的可见上下文 —— 第一人称作品被写成第三人称，
    # 账本再准也救不回来。机械事实，不猜，有就写。
    pov = str(work.get("narrative_pov") or "").strip()
    if pov:
        rules.append(f"叙事视角：{pov}")
    for key in ("hard_rules", "taboos"):
        for item in (raw.get(key) or []):
            text = str(item).strip()
            if text and text not in rules:
                rules.append(text)
    return rules


def _position_lines(work, chapter, contract, prev_ending_state: str) -> list[str]:
    head = f"《{work.get('title', '')}》第{chapter.get('chapter_index')}章"
    if chapter.get("title"):
        head += f" · {chapter['title']}"
    lines = [head]
    story_time = str(contract.get("story_time") or "").strip()
    if story_time:
        lines.append(f"故事内时间：{story_time}")
    if prev_ending_state.strip():
        lines.append(f"上一章停在：{prev_ending_state.strip()[:120]}")
    return lines


def _character_lines(rows, focus_refs, name_of) -> list[str]:
    """``rows`` 必须是**已按当前位置求值过**的 ``facts.current`` 结果。"""
    grouped = facts_mod.group_by_subject(list(rows))
    refs = list(focus_refs or grouped.keys())[:MAX_CHARACTERS]
    lines: list[str] = []
    for ref in refs:
        items = {str(r.get("field")): r for r in grouped.get(ref, [])}
        if not items:
            continue
        label = (name_of or {}).get(ref, ref)
        parts: list[str] = []
        for field_name in facts_mod.CARD_FIELDS:
            row = items.get(field_name)
            parts.append(f"{field_name}："
                         + (facts_mod.describe(row) if row else "未知"))
        lines.append(f"- {label} —— " + "；".join(parts))
    return lines


def _risk_lines(overdue, contract, extra_risks) -> list[str]:
    lines = [f"- [承诺 {p.get('id')}] 逾期未收：{p.get('title')}"
             for p in overdue[:3]]
    for item in (contract.get("forbidden_cross") or [])[:2]:
        lines.append(f"- [合同 forbidden_cross] 禁止触碰：{item}")
    for item in list(extra_risks or [])[:2]:
        lines.append(f"- [{item.get('source', 'review')}] "
                     f"{item.get('issue', '')}")
    return lines[:MAX_RISKS]


def build_card(*, work: Mapping[str, Any], chapter: Mapping[str, Any],
               contract: Mapping[str, Any] | None,
               facts_rows: Sequence[Mapping[str, Any]],
               promise_rows: Sequence[Mapping[str, Any]],
               summaries: Sequence[Mapping[str, Any]] = (),
               focus_refs: Sequence[str] | None = None,
               name_of: Mapping[str, str] | None = None,
               prev_ending_state: str = "",
               extra_risks: Sequence[Mapping[str, Any]] = (),
               position: int | None = None) -> dict[str, Any]:
    """装配七栏卡。

    ``position`` 默认 ``chapter_index - 1``：写第 N 章看到的是
    ``chapter_before(N)``，即第 N-1 章写完之后的状态。
    """
    if contract is None:
        raise CardIncomplete(f"第{chapter.get('chapter_index')}章没有合同")
    chapter_index = int(chapter.get("chapter_index") or 0)
    pos = chapter_index - 1 if position is None else int(position)

    current = facts_mod.current(facts_rows, pos)
    groups = promises_mod.obligations(promise_rows, chapter_index)
    active = promises_mod.top(groups)

    must_hit = [f"- [合同] {x}" for x in (contract.get("must_hit_now") or [])]
    must_hit += [f"- [承诺 {p.get('id')}] 本章必须兑现：{p.get('title')}"
                 for p in active["must_resolve"]]

    recent = [f"- 第{s.get('chapter_index')}章：{s.get('summary', '')}"
              for s in summaries[-3:]]

    sections = [
        {"key": "position",
         "lines": _position_lines(work, chapter, contract, prev_ending_state)},
        {"key": "long_constraints", "lines": _profile_rules(work)},
        {"key": "characters",
         "lines": _character_lines(current, focus_refs, name_of)},
        {"key": "promises", "lines": promises_mod.render_lines(active)},
        {"key": "recent", "lines": recent},
        {"key": "next_chapter", "lines": must_hit[:5]},
        {"key": "risks",
         "lines": _risk_lines(groups["overdue"], contract, extra_risks)},
    ]
    for section in sections:
        if not section["lines"]:
            section["lines"] = ["无"]
    card = {
        "chapter_index": chapter_index,
        "position": pos,
        "sections": sections,
        "obligations": active,
    }
    card["text"] = render_card(card)
    return card


def render_card(card: Mapping[str, Any]) -> str:
    out: list[str] = []
    for idx, (key, title) in enumerate(SECTIONS, start=1):
        section = next((s for s in card["sections"] if s["key"] == key), None)
        if section is None:
            raise CardIncomplete(f"缺栏：{title}")
        out.append(f"## {idx}. {title}")
        out.extend(section["lines"])
        out.append("")
    return "\n".join(out).rstrip() + "\n"
