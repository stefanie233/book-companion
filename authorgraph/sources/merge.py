"""合并、去重与冲突 —— 同一件事被三个网站说了，不该落三行。

设计稿 03 §四.6：

- 归一化 ``statement`` 后，同一 ``(kind, year, statement)`` 是**一条**，
  证据合并到同一条上（证据可以多，陈述只留一份）。
- 同一事件不同说法 → 都保留，UI 归组；**不自动合并语义**。
- 与已有台账冲突（同一句话、年份不同）→ 不覆盖，记 ``conflict`` 上报。
  旧条目多半来自作者自己的字，权威性高于网页 —— 但网页可能是对的，
  所以只报告，不裁决（与项目「只报告不覆盖」同一条）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_TRAIL_PUNCT = re.compile(r"[。．.！!？?；;，,、：:\s]+$")


def norm_statement(s: str) -> str:
    """陈述的匹配键：NFKC、折叠空白、去尾部标点。"""
    t = unicodedata.normalize("NFKC", s or "")
    t = re.sub(r"\s+", "", t)
    return _TRAIL_PUNCT.sub("", t)


def claim_key(claim: dict[str, Any]) -> tuple[str, int | None, str]:
    return ((claim.get("kind") or "life_event"),
            claim.get("year") if isinstance(claim.get("year"), int) else None,
            norm_statement(claim.get("statement") or ""))


def index_existing(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按归一化陈述索引已有条目 —— 用于发现「同一句话不同年份」。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        out.setdefault(norm_statement(r.get("statement") or ""), []).append(r)
    return out


def classify(claim: dict[str, Any],
             existing: dict[str, list[dict[str, Any]]]) -> str:
    """``new`` / ``duplicate`` / ``conflict``。

    ``duplicate`` 指**完全同键**（kind+year+statement）；``conflict`` 指
    同一句话在台账里是另一个年份。
    """
    st = norm_statement(claim.get("statement") or "")
    if not st:
        return "invalid"
    rows = existing.get(st) or []
    if not rows:
        return "new"
    year = claim.get("year")
    kind = claim.get("kind") or "life_event"
    for r in rows:
        if (r.get("kind") or "life_event") == kind and r.get("year") == year:
            return "duplicate"
    return "conflict"


def merge_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批内去重：同键合并证据，保留先到的陈述与更强的置信度。

    ``claims`` 每条形如::

        {"kind","year","year_end","statement","target",
         "evidence": [{"source_id","quote","locator","relation","confidence"}]}

    返回去重后的列表（顺序按首次出现）。
    """
    merged: dict[tuple[str, int | None, str], dict[str, Any]] = {}
    order: list[tuple[str, int | None, str]] = []
    for c in claims:
        k = claim_key(c)
        if k not in merged:
            merged[k] = dict(c, evidence=list(c.get("evidence") or []))
            order.append(k)
            continue
        cur = merged[k]
        seen = {(e.get("source_id"), e.get("quote"))
                for e in cur.get("evidence") or []}
        for e in (c.get("evidence") or []):
            if (e.get("source_id"), e.get("quote")) not in seen:
                cur["evidence"].append(e)
        cur["confidence"] = max(float(cur.get("confidence") or 0),
                                float(c.get("confidence") or 0))
    return [merged[k] for k in order]
