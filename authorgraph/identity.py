"""身份解析与消歧 —— **先确认是这个人，再谈他的生平。**

设计稿 03 §五。旧实现只靠 ``name_key`` 合并，同名不同人会并成一个，
而「并错了」是不可逆的：两套语料、两套编年从此互相污染。

本模块把身份拆成两步：

1. **找候选**（``wikidata_candidates``）：Wikidata 按名字搜实体，取 QID、
   生卒、别名、作品、国籍。网络失败返回空列表 —— 不编。
2. **判定**（``decide``）：**纯函数**，按设计稿的分值表打分。没有外部 ID
   且只有名字相同 → ``provisional``；两个高分候选打平 → ``conflict``。
   任何情况下都**不自动合并** —— 合并是写操作，得由人点头。

Wikidata 只是候选来源之一；VIAF/ISNI/OpenLibrary 以后可接同一套打分。
"""

from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import aiohttp

log = logging.getLogger("authorgraph")

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_USER_AGENT = "BookCompanion/0.1 (author identity lookup)"

#: Wikidata 的「人类」实体。
_Q_HUMAN = "Q5"
#: 生年 / 卒年 / 国籍 / 代表作 / 职业。
_P_BIRTH, _P_DEATH, _P_COUNTRY, _P_WORK, _P_OCCUPATION = (
    "P569", "P570", "P27", "P800", "P106")


def norm_name(s: str) -> str:
    """名字归一：只用于比较，不改存储（与 ``name_key`` 同一条）。"""
    return "".join(unicodedata.normalize("NFKC", s or "").split()).casefold()


@dataclass
class Candidate:
    """一个候选身份。``score`` 由 ``score_candidate`` 现算，不落库。"""

    scheme: str = "wikidata"
    value: str = ""                    # QID / VIAF ID
    label: str = ""
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    birth_year: int | None = None
    death_year: int | None = None
    nationality: str = ""
    works: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme, "value": self.value, "label": self.label,
            "aliases": self.aliases, "description": self.description,
            "birth_year": self.birth_year, "death_year": self.death_year,
            "nationality": self.nationality, "works": self.works,
        }


def _year_of(claim: dict[str, Any]) -> int | None:
    try:
        t = claim["mainsnak"]["datavalue"]["value"]["time"]     # +1951-01-04T…
        y = int(str(t).lstrip("+")[:4])
        return y if 0 < y < 3000 else None
    except Exception:                                     # noqa: BLE001
        return None


def _qids(claims: dict[str, Any], prop: str) -> list[str]:
    out: list[str] = []
    for c in (claims.get(prop) or []):
        try:
            out.append(c["mainsnak"]["datavalue"]["value"]["id"])
        except Exception:                                 # noqa: BLE001
            continue
    return out


def score_candidate(cand: Candidate, *, name: str,
                    birth_year: int | None = None,
                    death_year: int | None = None,
                    books: list[str] | None = None,
                    nationality: str = "",
                    known_identifier: str = "") -> float:
    """设计稿 03 §五.2 的分值表。**纯函数**，零 token、可单测。

    故意不给「描述里有作家」加分：那是 Wikidata 的标签，不是我们的证据。
    """
    score = 0.0
    if known_identifier and cand.value == known_identifier:
        return 1.0
    names = {norm_name(cand.label)} | {norm_name(a) for a in cand.aliases}
    if norm_name(name) in names:
        score += 0.30
    elif any(norm_name(name) in n or n in norm_name(name)
             for n in names if n):
        score += 0.10
    if birth_year and cand.birth_year:
        score += 0.25 if birth_year == cand.birth_year else -0.25
    if death_year and cand.death_year:
        score += 0.20 if death_year == cand.death_year else -0.20
    if books and cand.works:
        wl = {norm_name(w) for w in cand.works}
        if any(norm_name(b) in w or w in norm_name(b)
               for b in books if b for w in wl if w):
            score += 0.25
    if nationality and cand.nationality and (
            nationality in cand.nationality or cand.nationality in nationality):
        score += 0.10
    return round(score, 3)


@dataclass
class Decision:
    status: str = "provisional"        # resolved / provisional / conflict
    candidate: Candidate | None = None
    score: float = 0.0
    margin: float = 0.0
    note: str = ""


#: Wikidata 连不上时的熔断。中文环境里它是常见情况；不快跳过的话，
#: 批量补全会在每位作者身上等两次超时（zh + en）。
_WIKI_DOWN_UNTIL = 0.0


async def _api(session: aiohttp.ClientSession, params: dict[str, Any],
               *, timeout: float = 10.0) -> dict[str, Any]:
    p = dict(params, format="json", origin="*")
    async with session.get(
            WIKIDATA_API, params=p,
            headers={"User-Agent": _USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"wikidata HTTP {resp.status}")
        return await resp.json()


def _label(entity: dict[str, Any], langs: tuple[str, ...] = ("zh", "en")) -> str:
    labels = entity.get("labels") or {}
    for lang in langs:
        if (labels.get(lang) or {}).get("value"):
            return labels[lang]["value"]
    for v in labels.values():
        if isinstance(v, dict) and v.get("value"):
            return v["value"]
    return ""


def _aliases(entity: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for lang, items in (entity.get("aliases") or {}).items():
        for it in (items or []):
            v = (it or {}).get("value")
            if v and v not in out:
                out.append(v)
    return out[:12]


async def wikidata_candidates(session: aiohttp.ClientSession, name: str, *,
                              limit: int = 6) -> list[Candidate]:
    """按名字找 Wikidata 候选。**网络失败返回空列表**，不猜、不回落训练记忆。"""
    global _WIKI_DOWN_UNTIL
    if not (name or "").strip():
        return []
    if time.monotonic() < _WIKI_DOWN_UNTIL:
        return []
    ids: list[str] = []
    failures = 0
    for lang in ("zh", "en"):
        try:
            data = await _api(session, {
                "action": "wbsearchentities", "search": name,
                "language": lang, "uselang": lang, "type": "item",
                "limit": limit})
        except Exception as exc:                      # noqa: BLE001
            failures += 1
            log.warning("wikidata search failed (%s): %s", name, exc)
            continue
        for r in (data.get("search") or []):
            qid = r.get("id") or ""
            if qid and qid not in ids:
                ids.append(qid)
        if ids:
            break
    if failures >= 2:
        _WIKI_DOWN_UNTIL = time.monotonic() + 600
        log.warning("wikidata 连不上，身份查询暂停 10 分钟")
    if not ids:
        return []
    try:
        ents = await _api(session, {
            "action": "wbgetentities", "ids": "|".join(ids[:limit]),
            "props": "claims|labels|aliases|descriptions|sitelinks",
            "languages": "zh|en"})
    except Exception as exc:                          # noqa: BLE001
        log.warning("wikidata entities failed (%s): %s", name, exc)
        return []
    raw = ents.get("entities") or {}
    cands: list[Candidate] = []
    ref_ids: set[str] = set()
    for qid in ids:
        e = raw.get(qid) or {}
        claims = e.get("claims") or {}
        p31 = _qids(claims, "P31")
        if p31 and _Q_HUMAN not in p31:
            continue                                  # 不是人（地名/作品/机构）
        c = Candidate(
            value=qid, label=_label(e), aliases=_aliases(e),
            description=((e.get("descriptions") or {}).get("zh")
                          or (e.get("descriptions") or {}).get("en")
                          or {}).get("value", ""),
            birth_year=next((_year_of(x) for x in (claims.get(_P_BIRTH) or [])
                             if _year_of(x)), None),
            death_year=next((_year_of(x) for x in (claims.get(_P_DEATH) or [])
                             if _year_of(x)), None),
        )
        ref_ids.update(_qids(claims, _P_WORK))
        ref_ids.update(_qids(claims, _P_COUNTRY))
        c.works = _qids(claims, _P_WORK)
        c.nationality = (_qids(claims, _P_COUNTRY) or [""])[0]
        cands.append(c)
    if not cands or not ref_ids:
        return cands
    # 作品/国籍都是 QID，取一次标签（zh 优先）填成人能读的名字。
    try:
        lab = await _api(session, {
            "action": "wbgetentities", "ids": "|".join(sorted(ref_ids)[:50]),
            "props": "labels", "languages": "zh|en"})
        labels = {q: _label(v) for q, v in (lab.get("entities") or {}).items()}
    except Exception:                                 # noqa: BLE001
        labels = {}
    for c in cands:
        c.works = [labels.get(q, "") for q in (c.works or []) if labels.get(q)]
        c.nationality = labels.get(c.nationality, "") or ""
    return cands


def decide(scored: list[tuple[Candidate, float]], *,
           unique_exact: bool = False) -> Decision:
    """按分值下结论。**只在证据足够时给 resolved**，其余按 provisional 走。

    ``unique_exact`` 由调用方在「搜索结果只有一个、且标签与名字逐字相同」时
    置真。没有它，新作者会陷入死锁：库里没有生年 → 分数到不了 0.8 →
    身份永远 provisional → 设计稿又要求「身份不明不跑生平」→ 生年永远补不上。
    """
    if not scored:
        return Decision(note="没有候选")
    ranked = sorted(scored, key=lambda x: x[1], reverse=True)
    top_c, top_s = ranked[0]
    second_s = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = round(top_s - second_s, 3)
    if unique_exact and len(ranked) == 1:
        return Decision("resolved", top_c, top_s, margin,
                        "唯一精确同名候选（无竞争）")
    if top_s >= 0.80 and margin >= 0.20:
        return Decision("resolved", top_c, top_s, margin, "高分且拉开差距")
    if len(ranked) > 1 and top_s >= 0.80 and second_s >= 0.80 and margin < 0.10:
        return Decision("conflict", top_c, top_s, margin,
                        "两个候选都高分且几乎打平 —— 需人工确认")
    return Decision("provisional", top_c, top_s, margin,
                    "证据不足以确认唯一身份")
