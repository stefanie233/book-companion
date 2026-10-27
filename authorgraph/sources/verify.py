"""逐字引文校验 —— 采集管线里最该被测量的一步。

设计稿 03 §四.5。核心只有一句：**来源不是证据，引文才是证据。**
模型可以给一条真实存在的 URL，却把内容编出来；URL 校验拦不住这种情况。

``quote_not_found`` 是本层最重要的指标 —— 它直接反映模型有没有在编造
出处。所以：

- 引文为空 → 拒绝（``quote_empty``），不是降级成「没引文也能过」。
- 引文在页面正文里找不到 → 拒绝（``quote_not_found``）。
- 年份不在引文里 → **清空年份**并报 ``year_mismatch``，条目保留。
- 来源角色不支持这个目标 → 拒绝（``role_mismatch``）。
- tier ≥ 4 → 拒绝（``tier_too_low``），只当线索。

正常化只抹**空白与全半角**；不做同义词、不做模糊匹配 —— 模糊匹配会把
「引文对不上」这件事悄悄变成「引文对得上」。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

#: target → 需要的来源角色。``poetics`` 允许作者原话或研究归纳。
_TARGET_ROLE = {
    "biography": "biography",
    "bibliography": "bibliography",
    "relation": "relation",
    "voice": "voice",
    "theme": "theme",
    "reception": "reception",
    "poetics": ("voice", "theme"),
    "context": "context",
}

TARGETS = tuple(_TARGET_ROLE)

#: 引文里没有作者名时，至少要有这些指代词之一，否则标 needs_review。
_PRONOUNS = ("他", "她", "其", "作者", "本人", "我", "自己",
             "he ", "she ", "his ", "her ", "the author", " i ")


def norm_for_match(s: str) -> str:
    """比对用的归一：NFKC + 去所有空白。**不改大小写、不做繁简转换。**"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


def quote_located(quote: str, page_text: str) -> bool:
    q = norm_for_match(quote)
    return bool(q) and q in norm_for_match(page_text)


_CN_DIGITS = "〇一二三四五六七八九"


def _year_in(quote: str, year: int) -> bool:
    """年份是否在引文里。**中文数字单独处理** —— 「一九五一年」是中文来源
    最常见的写法，只比对阿拉伯数字会把正确的年份误清空。"""
    if str(year) in quote:
        return True
    cn = "".join(_CN_DIGITS[int(d)] for d in str(year))
    return cn in quote


def verify_claim(claim: dict[str, Any], page: dict[str, Any],
                 *, roles: tuple[str, ...] | list[str],
                 tier: int) -> dict[str, Any]:
    """验一条 claim。返回 ``{ok, code, detail, claim, defects}``，**不 raise**。

    ``claim`` 里被修过的字段（目前只有 ``year``）会体现在返回值里 ——
    调用方应以返回的 claim 为准落库。
    """
    c = dict(claim)
    target = (c.get("target") or "biography").strip()
    code, detail = "", ""
    defects: list[str] = []
    if target not in _TARGET_ROLE:
        return {"ok": False, "code": "target_unknown", "claim": c,
                "detail": f"未知 target：{target}", "defects": defects}
    if tier >= 4:
        return {"ok": False, "code": "tier_too_low", "claim": c,
                "detail": f"来源 tier={tier}，只能作线索", "defects": defects}
    need = _TARGET_ROLE[target]
    need_roles = need if isinstance(need, tuple) else (need,)
    if not any(r in (roles or ()) for r in need_roles):
        return {"ok": False, "code": "role_mismatch", "claim": c,
                "detail": f"来源角色 {list(roles)} 不支持 target={target}",
                "defects": defects}

    quote = (c.get("evidence_quote") or "").strip()
    if not quote:
        return {"ok": False, "code": "quote_empty", "claim": c,
                "detail": "没有逐字引文 —— 不肯写引文的条目就是编的",
                "defects": defects}
    if not quote_located(quote, page.get("main_text") or ""):
        return {"ok": False, "code": "quote_not_found", "claim": c,
                "detail": f"引文在页面正文中找不到：{quote[:60]!r}",
                "defects": defects}

    year = c.get("year")
    if isinstance(year, int) and not _year_in(quote, year):
        defects.append(f"year_mismatch: {year} 不在引文中，已清空")
        c["year"] = None
        code, detail = code or "year_mismatch", "年份不在引文中"

    if not (c.get("statement") or "").strip():
        return {"ok": False, "code": "empty_statement", "claim": c,
                "detail": "陈述为空", "defects": defects}

    # 引文里连作者名/指代词都没有 —— 可能是同页另一段讲别人的话。
    qlow = f" {norm_for_match(quote)} "
    if not any(norm_for_match(p) in qlow for p in _PRONOUNS):
        defects.append("needs_review: 引文里没有作者名或指代词")
    return {"ok": True, "code": code, "claim": c, "detail": detail,
            "defects": defects}
