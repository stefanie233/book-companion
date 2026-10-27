"""承诺的五类章节义务 —— 纯函数，读取时计算，不落库。

承诺（伏笔 / 钩子 / 读者期待 / 欠债）的状态是**写**进 ``promises`` 的；
「这一章该催什么」是**算**出来的。落库会把「提醒」变成第二份真相，
改一次目标章就要同步两处 —— 与「实体重要级不落库」同一条原则。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

CATEGORIES = ("prohibited_early", "must_resolve", "overdue", "to_plant", "upcoming")

LABELS = {
    "prohibited_early": "禁止提前揭示",
    "must_resolve": "本章必须兑现",
    "overdue": "已逾期",
    "to_plant": "本章必须埋",
    "upcoming": "临近回收",
}

OPEN_STATUSES = ("planned", "planted", "reinforced", "deferred")

#: 每类最多 3 条、合计最多 8 条（见 docs/writegraph/03-context-card.md）。
PER_CATEGORY = 3
TOTAL_CAP = 8


def _is_open(row: Mapping[str, Any]) -> bool:
    return str(row.get("status") or "") not in ("resolved", "abandoned")


def sort_key(row: Mapping[str, Any]) -> tuple:
    """``(-weight, planned_payoff or 大数, id)``；没有目标章的排最后。"""
    payoff = row.get("planned_payoff_chapter")
    return (-int(row.get("weight") or 0),
            int(payoff) if payoff is not None else 10 ** 9,
            str(row.get("id") or ""))


def obligations(rows: Iterable[Mapping[str, Any]], chapter: int,
                horizon: int = 3) -> dict[str, list[dict[str, Any]]]:
    """按五个判据分类，**不做数量裁剪**（裁剪交给 :func:`top`）。"""
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in CATEGORIES}
    for row in rows:
        p = dict(row)
        payoff = p.get("planned_payoff_chapter")
        reveal = p.get("earliest_reveal_chapter")
        status = str(p.get("status") or "")

        # 禁止提前揭示优先于一切：它是安全约束，不是提醒。
        if reveal is not None and int(reveal) > chapter and _is_open(p):
            groups["prohibited_early"].append(p)
        if (payoff is not None and int(payoff) == chapter
                and status in ("planted", "reinforced", "deferred")):
            groups["must_resolve"].append(p)
        elif (payoff is not None and int(payoff) < chapter and _is_open(p)):
            groups["overdue"].append(p)
        elif (payoff is not None
              and chapter < int(payoff) <= chapter + horizon):
            groups["upcoming"].append(p)

        if (p.get("planned_plant_chapter") is not None
                and int(p["planned_plant_chapter"]) == chapter
                and status == "planned"):
            groups["to_plant"].append(p)

    for key in groups:
        groups[key].sort(key=sort_key)
    return groups


def top(groups: Mapping[str, Sequence[Mapping[str, Any]]],
        *, per_category: int = PER_CATEGORY,
        total: int = TOTAL_CAP) -> dict[str, list[dict[str, Any]]]:
    """按类轮转裁剪。安全类先占位，同权重高的先上。"""
    queues = {k: [dict(x) for x in groups.get(k, ())[:per_category]]
              for k in CATEGORIES}
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in CATEGORIES}
    count = 0
    while count < total:
        progressed = False
        for key in CATEGORIES:
            if count >= total:
                break
            if queues[key] and len(out[key]) < per_category:
                out[key].append(queues[key].pop(0))
                count += 1
                progressed = True
        if not progressed:
            break
    return out


def render_lines(groups: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[str]:
    """卡片第 4 栏。空类别不出现，但整栏为空时由调用方写「无」。"""
    lines: list[str] = []
    for key in CATEGORIES:
        items = groups.get(key) or ()
        if not items:
            continue
        lines.append(f"**{LABELS[key]}**")
        for p in items:
            payoff = p.get("planned_payoff_chapter")
            tail = f"，目标第{payoff}章" if payoff is not None else ""
            lines.append(f"- [{p.get('id')}] {p.get('title')}"
                         f"（权重 {p.get('weight')}{tail}）")
    return lines
