"""覆盖度快照与查询计划 —— 「缺什么补什么」，不再一条 query 打天下。

设计稿 03 §二：旧实现只有 ``"{name} 生平 简介"``，它同时有三个问题 ——
身份、书目、声音混在一起搜；补全顺序没有优先级；搜到什么算什么。
这里把它拆成 facet，按「先身份、再书目、最后解释」排序。

**覆盖度不落表**（设计稿 02 §四.2）：它是当前数据的投影，现算。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

#: facet 的补全顺序 —— 顺序本身就是决策（设计稿 03 §四.1）：
#: 身份不明时不补生平（会把同名两个人写成一个）；书目能反哺身份消歧；
#: theme/reception 最后，避免研究者的判断污染事实层。
FACET_ORDER = ("identity", "bibliography", "biography",
               "relations", "voice", "poetics", "theme", "reception")

#: 每条查询模板。中文优先 —— 书库以中文作者为主，外文名会由调用方
#: 决定是否追加原文名查询（见 ``plan_queries`` 的 ``names``）。
_TEMPLATES = {
    "identity": "{name} 原名 笔名 生卒 国籍 简介",
    "bibliography": "{name} 作品 年表 出版",
    "biography": "{name} 生平 年谱 经历",
    "relations": "{name} 师从 友人 同时代 关系",
    "voice": "{name} 访谈 创作谈 演讲 语言 风格",
    "poetics": "{name} 创作谈 文学观 写作",
    "theme": "{name} 主题 母题 研究",
    "reception": "{name} 评论 影响 研究",
}

#: 覆盖度的最低期望值。数字不是为了评分，是为了回答「还缺什么」。
_EXPECT = {"bibliography": 6, "biography": 12, "relations": 4,
           "voice": 8, "poetics": 2, "theme": 3, "reception": 3}


def coverage_snapshot(adb: Any, author_id: str) -> dict[str, Any]:
    """一位作者当前的资料覆盖度（0..1）与缺失项。**不调用 LLM。**"""
    author = adb.get_author(author_id) or {}
    st = adb.stats(author_id)
    recs = adb.records_for(author_id, limit=2000)
    by_kind = Counter((r.get("kind") or "") for r in recs)
    claims = Counter((c.get("facet") or "")
                     for c in adb.claims_for(author_id, limit=1000))
    counts = {
        "bibliography": by_kind.get("work", 0) + by_kind.get("publication", 0),
        "biography": by_kind.get("life_event", 0),
        "relations": by_kind.get("relation", 0),
        "voice": int(st.get("voice_corpus") or 0),
        "poetics": claims.get("poetics", 0),
        "theme": claims.get("theme", 0),
        "reception": claims.get("reception", 0),
        "evidence": int(st.get("evidence") or 0),
    }
    ident_done = sum(bool(author.get(k)) for k in
                     ("birth_year", "death_year", "nationality"))
    ident_done += 1 if st.get("identifiers") else 0
    cov: dict[str, float] = {
        "identity": min(1.0, ident_done / 4),
        "bibliography": min(1.0, counts["bibliography"] / _EXPECT["bibliography"]),
        "biography": min(1.0, counts["biography"] / _EXPECT["biography"]),
        "relations": min(1.0, counts["relations"] / _EXPECT["relations"]),
        "voice": min(1.0, counts["voice"] / _EXPECT["voice"]),
        # 解释性陈述按 author_claims 计数；声音特征（traits）属 M2。
        "poetics": min(1.0, counts["poetics"] / _EXPECT["poetics"]),
        "theme": min(1.0, counts["theme"] / _EXPECT["theme"]),
        "reception": min(1.0, counts["reception"] / _EXPECT["reception"]),
    }
    missing = [f for f in FACET_ORDER
               if f in _EXPECT and cov.get(f, 0) < 0.5]
    return {
        "author_id": author_id,
        "counts": counts,
        "coverage": {k: round(v, 3) for k, v in cov.items()},
        "missing": missing,
        "identity_status": author.get("identity_status") or "provisional",
        "bio_status": author.get("bio_status") or "none",
    }


def plan_queries(author: dict[str, Any], coverage: dict[str, Any], *,
                 facets: list[str] | None = None,
                 max_queries: int = 6,
                 strict_identity: bool = True) -> list[dict[str, str]]:
    """按缺口生成查询。返回 ``[{facet, query, why}]``，**按优先级排序**。

    ``facets`` 显式给出时只跑这些 —— 手动刷某一块（比如只补书目）用它。
    ``strict_identity=True`` 时身份不明就**只跑身份**；管线在 Wikidata 已经
    试过一轮之后传 False —— 否则「身份不明 → 只跑身份 → 仍不明 → 只跑身份」
    会死锁，作者的语料一条也补不进来。
    """
    name = (author.get("canonical_name") or "").strip()
    if not name:
        return []
    wanted = [f for f in FACET_ORDER
              if (facets is None or f in facets)
              and (facets is not None or f in coverage.get("missing", []))]
    # 身份不明时**只跑身份**：先按名字搜生平，会把同名两个人的材料混进
    # 同一个台账，而那是不可逆的（设计稿 03 §四.1）。
    if (facets is None and
            coverage.get("identity_status") != "resolved" and
            coverage.get("coverage", {}).get("identity", 0) < 0.5):
        if strict_identity:
            wanted = ["identity"]
        else:
            wanted = ["identity"] + [f for f in wanted if f != "identity"]
    out: list[dict[str, str]] = []
    for facet in wanted:
        tpl = _TEMPLATES.get(facet)
        if not tpl:
            continue
        out.append({"facet": facet, "query": tpl.format(name=name),
                    "why": _why(facet, coverage)})
    native = (author.get("name_native") or "").strip()
    if native and native != name and len(out) < max_queries:
        out.append({"facet": "identity", "query": f"{native} writer biography",
                    "why": "原文名查询（中文译名覆盖不到的来源）"})
    return out[:max_queries]


def _why(facet: str, coverage: dict[str, Any]) -> str:
    if facet == "identity":
        return ("身份字段/外部 ID 不足 —— 先确认是这个人，再谈生平")
    n = int(coverage.get("counts", {}).get(facet, 0) or 0)
    return f"当前约有 {n} 条，低于期望值" if n else "这一块还是空的"
