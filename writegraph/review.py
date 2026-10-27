"""审稿归一化与评分 —— 纯函数。

三条原则（docs/writegraph/05-quality-loop.md）：

- **只诊断不改稿**：这里只产出 findings 与分数，不产生替换文本；
- **问题不扣分**：分数只看判据，issue 数量只生成清单；
- **没有逐字证据就不算结论**：引文对不上 → ``inconclusive``，不得分。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from writegraph import deslop

#: 6 域 / 10 判据，权重合计 100。
DOMAINS = (
    ("coherence", "连贯与逻辑", 20,
     ("time_scene", "knowledge_boundary", "causality")),
    ("character", "角色与关系", 15,
     ("character_fidelity", "relation_state")),
    ("plot", "情节与承诺", 20,
     ("promise_payoff", "chapter_change")),
    ("pacing", "节奏与场景", 15, ("scene_function",)),
    ("prose", "文风与表达", 15, ("narrative_voice",)),
    ("canon", "正典与事实", 15, ("canon_consistency",)),
)

CRITERIA = {c: (domain, detail, weight / len(criteria))
            for domain, detail, weight, criteria in DOMAINS
            for c in criteria}

SEVERITIES = ("S1", "S2", "S3", "S4")
CATEGORIES = tuple(d[0] for d in DOMAINS)

VERDICTS = ("pass", "partial", "fail", "inconclusive")

#: 判据满分（每条 5 分）。
MAX_POINTS = 5.0

DELIVERY_BLOCKED = "blocked"
DELIVERY_INCONCLUSIVE = "inconclusive"
DELIVERY_PASS = "pass"
DELIVERY_REVISE = "revise"

MIN_COVERAGE = 0.8
PASS_SCORE = 70.0


def hard_gate(content: str, findings: Sequence[Mapping[str, Any]] = (),
              issues: Sequence[Mapping[str, Any]] | None = None) -> str:
    """返回 ``blocked`` 或 ``pass``。只看机械可判的事。"""
    if not (content or "").strip():
        return DELIVERY_BLOCKED
    if len(content.strip()) < 200:
        return DELIVERY_BLOCKED
    if any(f.get("_gate") == "blocked" for f in findings):
        return DELIVERY_BLOCKED
    items = issues if issues is not None else deslop.detect(content)["issues"]
    if any(i["kind"] in ("refusal", "engine-word") for i in items):
        return DELIVERY_BLOCKED
    return DELIVERY_PASS


def score(evals: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """``quality = earned / evaluated_max × 100``；分母只算已评估的判据。"""
    earned = 0.0
    evaluated = 0.0
    inconclusive: list[str] = []
    for criterion, (_domain, _detail, weight) in CRITERIA.items():
        item = evals.get(criterion)
        if not item:
            inconclusive.append(criterion)
            continue
        verdict = str(item.get("verdict") or "inconclusive")
        evidence = str(item.get("evidence") or "").strip()
        if verdict == "inconclusive":
            inconclusive.append(criterion)
            continue
        points = float(item.get("earned", 0.0))
        if points > 0 and not evidence:
            inconclusive.append(criterion)   # 无证据的得分不算数
            continue
        points = max(0.0, min(MAX_POINTS, points))
        evaluated += weight
        earned += weight * points / MAX_POINTS
    quality = round(earned / evaluated * 100, 1) if evaluated else None
    return {
        "quality_score": quality,
        "coverage": round(evaluated / 100.0, 3),
        "inconclusive": inconclusive,
    }


def normalize_findings(raw: Iterable[Any], content: str
                       ) -> tuple[list[dict[str, Any]], list[str]]:
    """逐条验证据。对不上就**作废**，不降级、不猜测。"""
    kept: list[dict[str, Any]] = []
    rejected: list[str] = []
    for item in raw or []:
        if not isinstance(item, Mapping):
            rejected.append("findings 条目不是对象")
            continue
        evidence = str(item.get("evidence") or "").strip()
        if not evidence:
            rejected.append("缺 evidence")
            continue
        if evidence not in content:
            rejected.append(f"引文不在正文中：{evidence[:24]}")
            continue
        start = content.find(evidence)
        severity = str(item.get("severity") or "S3").upper()
        if severity not in SEVERITIES:
            severity = "S3"
        category = str(item.get("category") or "coherence")
        if category not in CATEGORIES:
            category = "coherence"
        kept.append({
            "severity": severity,
            "category": category,
            "location": (f"{start}-{start + len(evidence)}"
                         if content.count(evidence) == 1 else "ambiguous"),
            "evidence": evidence,
            "issue": str(item.get("issue") or "").strip(),
            "fix_hint": str(item.get("fix_hint") or "").strip(),
        })
    return kept, rejected


def _deslop_findings(text: str, issues: Sequence[Mapping[str, Any]]
                     ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """有逐字例子的转 finding，没有的只进 signals（不能当结论）。"""
    findings: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    for issue in issues:
        example = next((e for e in issue["examples"] if e and e in text), "")
        if not example:
            signals.append(issue)
            continue
        severity = "S1" if issue["tier"] == deslop.BLOCKING else "S3"
        findings.append({
            "severity": severity,
            "category": "prose",
            "location": f"{text.find(example)}-{text.find(example) + len(example)}",
            "evidence": example,
            "issue": issue["message"],
            "fix_hint": "按提示改这一处，不要整段重写",
        })
    return findings, signals


def delivery_status(quality: float | None, coverage: float,
                    gate: str, findings: Sequence[Mapping[str, Any]],
                    inconclusive: Sequence[str]) -> str:
    if gate == DELIVERY_BLOCKED:
        return DELIVERY_BLOCKED
    if any(f.get("severity") == "S1" for f in findings):
        return DELIVERY_BLOCKED
    if (gate != DELIVERY_PASS or coverage < MIN_COVERAGE
            or inconclusive or quality is None):
        return DELIVERY_INCONCLUSIVE
    if quality >= PASS_SCORE:
        return DELIVERY_PASS
    return DELIVERY_REVISE


def normalize_review(payload: Mapping[str, Any], content: str) -> dict[str, Any]:
    """把模型输出变成可落库的一次审稿。**所有减法都在这里**。"""
    evals: dict[str, dict[str, Any]] = {}
    for item in payload.get("criteria") or []:
        if not isinstance(item, Mapping):
            continue
        cid = str(item.get("criterion") or item.get("id") or "")
        if cid not in CRITERIA:
            continue
        verdict = str(item.get("verdict") or "inconclusive")
        if verdict not in VERDICTS:
            verdict = "inconclusive"
        evals[cid] = {"verdict": verdict,
                      "earned": item.get("earned", 0),
                      "evidence": item.get("evidence", "")}

    deslop_result = deslop.detect(content)
    findings, rejected = normalize_findings(
        payload.get("findings") or [], content)
    mechanical, signals = _deslop_findings(content, deslop_result["issues"])
    findings.extend(mechanical)

    scored = score(evals)
    gate = hard_gate(content, findings, deslop_result["issues"])
    delivery = delivery_status(scored["quality_score"], scored["coverage"],
                               gate, findings, scored["inconclusive"])
    return {
        "criteria": [{"criterion": c, "domain": CRITERIA[c][0],
                      "verdict": evals.get(c, {}).get("verdict", "missing")}
                     for c in CRITERIA],
        "quality_score": scored["quality_score"],
        "coverage": scored["coverage"],
        "gate_status": gate,
        "delivery_status": delivery,
        "findings": findings,
        "rejected": rejected,
        "signals": signals,
        "deslop": deslop_result,
    }
