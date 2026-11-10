"""按写作位置求值事实 —— 纯函数，不碰连接。

唯一一条规则：位置 ``P = chapter_after(K)``，闭区间

    valid_from_chapter <= K AND (valid_until IS NULL OR valid_until >= K)

边界是**闭**的：第 12 章写的值，在 ``chapter_after(12)`` 可见，
在写第 13 章时（``chapter_before(13)``，即同一个 K）也可见。

未知是一等公民：``value`` 与 ``unknown_reason`` 二选一。渲染时两者都要
明确写出来，「没说」不能长得像「没有」。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: 未知原因的四个枚举，与 ``storage/writing_db.UNKNOWN_REASONS`` 同步。
UNKNOWN_REASONS = ("nonexistent", "unclaimed", "before_start", "voided")

#: 中文短标签。存枚举、显示中文，不在库里写给人看的话。
UNKNOWN_LABELS = {
    "nonexistent": "此刻尚不存在",
    "unclaimed": "作者尚未设定",
    "before_start": "故事开始前不可知",
    "voided": "已作废",
}

STATUS_KNOWN = "known"
STATUS_UNKNOWN = "unknown"
STATUS_CONFLICT = "conflict"

#: 卡片只带这几个字段；顺序固定，缺的写「未知」。
CARD_FIELDS = ("location", "goal", "state", "knowledge")


def is_visible(row: Mapping[str, Any], chapter: int) -> bool:
    """该事实在 ``chapter_after(chapter)`` 是否成立。"""
    if int(row.get("superseded") or 0):
        return False
    start = int(row.get("valid_from_chapter", 0))
    if start > chapter:
        return False
    until = row.get("valid_until_chapter")
    return until is None or int(until) >= chapter


def visible(rows: Iterable[Mapping[str, Any]], chapter: int) -> list[dict[str, Any]]:
    return [dict(r) for r in rows if is_visible(r, chapter)]


def current(rows: Iterable[Mapping[str, Any]], chapter: int) -> list[dict[str, Any]]:
    """每个 ``(subject_ref, field)`` 在当前位置的有效值。

    同一位置出现两条同起点的事实 = **冲突**，不是随便挑一条。
    冲突项带 ``status='conflict'`` 原样返回，由卡片显式标出 ——
    静默取最新会把「账本坏了」伪装成「一切正常」。
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in visible(rows, chapter):
        key = (str(row.get("subject_ref") or ""), str(row.get("field") or ""))
        groups.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    for (subject_ref, field), items in sorted(groups.items()):
        newest = max(int(r.get("valid_from_chapter", 0)) for r in items)
        top = [r for r in items if int(r.get("valid_from_chapter", 0)) == newest]
        if len(top) > 1:
            for r in top:
                r["status"] = STATUS_CONFLICT
                out.append(r)
            continue
        row = top[0]
        row["status"] = (STATUS_UNKNOWN if row.get("value") is None
                         else STATUS_KNOWN)
        out.append(row)
    return out


def describe(row: Mapping[str, Any]) -> str:
    """一条事实的显示文本。未知必须带原因，不能显示成空串。"""
    if row.get("status") == STATUS_CONFLICT:
        return "**冲突**（同一位置有两条有效值）"
    if row.get("value") is None:
        reason = str(row.get("unknown_reason") or "unclaimed")
        return f"未知（{UNKNOWN_LABELS.get(reason, reason)}）"
    return str(row.get("value"))


def group_by_subject(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("subject_ref") or ""), []).append(dict(row))
    return grouped
