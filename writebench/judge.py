"""LLM 评审：rubric、连续性、成对比较。证据逐字校验后才计分。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from llm_json import loads_tolerant
from writebench.llm import Chat, endpoint

PROMPTS = Path(__file__).with_name("prompts")

CRAFT = ("scene_concreteness", "character_logic", "prose_quality", "coherence")
ALL = CRAFT + ("contract_fidelity",)
WEIGHTS = {"scene_concreteness": 0.25, "character_logic": 0.20,
           "prose_quality": 0.25, "coherence": 0.15, "contract_fidelity": 0.15}


def _norm(text: str) -> str:
    return re.sub(r"[\s“”「」『』\"'.,，。；：!！?？…—\-]+", "", text or "")


def _evidence_ok(evidence: str, draft: str) -> bool:
    ev = _norm(evidence)
    return bool(ev) and ev in _norm(draft)


def _task_text(case: Mapping[str, Any], draft: str,
               *, with_reference: bool = True) -> str:
    work = case.get("work") or {}
    contract = case.get("contract") or {}
    lines = [f"## 作品\n《{work.get('title')}》 | {work.get('genre', '')}"
             f" | {work.get('narrative_pov', '')}\n{work.get('premise', '')}",
             "## 本章合同",
             f"目的：{contract.get('purpose', '')}"]
    for key, label in (("must_hit_now", "必须发生"),
                       ("must_preserve", "必须保留"),
                       ("forbidden_cross", "禁止触碰")):
        for item in contract.get(key) or []:
            lines.append(f"- {label}：{item}")
    if contract.get("ending_state"):
        lines.append(f"- 结尾状态：{contract['ending_state']}")
    for fact in case.get("facts") or []:
        value = fact.get("value") or f"未知（{fact.get('unknown_reason')}）"
        lines.append(f"- 事实：{fact['subject_ref']}.{fact['field']} = {value}")
    for promise in case.get("promises") or []:
        lines.append(f"- 承诺 {promise['id']}：{promise['title']}"
                     f"（{promise.get('status')}）")
    if case.get("brief"):
        lines.append("## 任务意图\n" + case["brief"])
    lines.append("## 草稿\n" + draft)
    return "\n".join(lines)


def extract_block(assembled: str, name: str) -> str:
    """从装配文本里取一个 ``## [name]`` 块，给评委看模型当时看到了什么。"""
    marker = f"## [{name}]"
    start = (assembled or "").find(marker)
    if start < 0:
        return ""
    rest = assembled[start + len(marker):]
    nxt = rest.find("\n## [")
    return rest[:nxt].strip() if nxt >= 0 else rest.strip()


async def rubric(chat: Chat, case: Mapping[str, Any], draft: str, *,
                 judge_model: tuple[str, str],
                 with_material: bool, with_style: bool,
                 reference: str = "", voice: str = "") -> dict[str, Any]:
    system = (PROMPTS / "judge.md").read_text(encoding="utf-8")
    task = _task_text(case, draft)
    if with_material and reference:
        task += ("\n\n## 本章可用的参考素材（评委可见）\n" + reference)
    if with_style and voice:
        task += ("\n\n## 本章给定的参考风格（评委可见）\n" + voice)
    raw = await chat.complete(endpoint(*judge_model), system, task,
                              temperature=0.0, max_tokens=1600,
                              json_mode=True, timeout=150)
    data = loads_tolerant(raw, expect=dict) or {}
    out: dict[str, Any] = {"criteria": {}, "raw": raw[:2000]}
    for name in ALL:
        item = (data.get("criteria") or {}).get(name) or {}
        score = max(0, min(10, int(float(item.get("score") or 0))))
        evidence = str(item.get("evidence") or "")
        ok = _evidence_ok(evidence, draft)
        out["criteria"][name] = {
            "score": score if ok else 0,
            "raw_score": score, "evidence_ok": ok,
            "evidence": evidence, "why": str(item.get("why") or "")}
    for name, present in (("material_transfer", with_material),
                          ("style_fidelity", with_style)):
        if not present:
            out[name] = None
            continue
        item = data.get(name) or {}
        score = max(0, min(10, int(float(item.get("score") or 0))))
        evidence = str(item.get("evidence") or "")
        out[name] = {"score": score if _evidence_ok(evidence, draft) else 0,
                     "raw_score": score,
                     "evidence_ok": _evidence_ok(evidence, draft),
                     "evidence": evidence,
                     "why": str(item.get("why") or "")}
    violations = [v for v in (data.get("contract_violations") or [])
                  if isinstance(v, Mapping)
                  and _evidence_ok(str(v.get("evidence") or ""), draft)]
    out["contract_violations"] = violations
    out["findings"] = [f for f in (data.get("findings") or [])
                       if isinstance(f, Mapping)
                       and _evidence_ok(str(f.get("evidence") or ""), draft)]
    out["composite"] = composite(out)
    return out


def composite(result: Mapping[str, Any]) -> float:
    """0–10 加权分；缺的专项不计入，其余按比例放大。"""
    parts = {k: float((result.get("criteria") or {}).get(k, {}).get("score") or 0)
             for k in ALL}
    weights = dict(WEIGHTS)
    extra = 0.0
    if result.get("material_transfer") is not None:
        extra += 0.15
        parts["material"] = float(result["material_transfer"]["score"])
    if result.get("style_fidelity") is not None:
        extra += 0.10
        parts["style"] = float(result["style_fidelity"]["score"])
    scale = 1.0 - extra
    total = sum(parts[k] * w * scale for k, w in weights.items())
    if "material" in parts:
        total += parts["material"] * 0.15
    if "style" in parts:
        total += parts["style"] * 0.10
    penalty = min(3.0, 0.75 * len(result.get("contract_violations") or []))
    return round(max(0.0, total - penalty), 2)


async def consistency(chat: Chat, case: Mapping[str, Any], draft: str, *,
                      judge_model: tuple[str, str]) -> dict[str, Any]:
    system = (PROMPTS / "consistency.md").read_text(encoding="utf-8")
    context = _task_text(case, draft)
    raw = await chat.complete(endpoint(*judge_model), system, context,
                              temperature=0.0, max_tokens=1200, json_mode=True,
                              timeout=150)
    data = loads_tolerant(raw, expect=dict) or {}
    violations = []
    for item in data.get("violations") or []:
        if not isinstance(item, Mapping):
            continue
        if not _evidence_ok(str(item.get("evidence") or ""), draft):
            continue
        violations.append(item)
    return {"violations": violations, "raw": raw[:1500],
            "count": len(violations)}


async def pairwise(chat: Chat, case: Mapping[str, Any], a: str, b: str, *,
                   judge_model: tuple[str, str],
                   reference: str = "") -> dict[str, Any]:
    """返回 A/B 视角下的胜者；调用方负责交换位置再跑一次。"""
    system = (PROMPTS / "pairwise.md").read_text(encoding="utf-8")
    contract = case.get("contract") or {}
    head = [f"## 任务\n{case.get('brief', '')}",
            f"## 合同\n目的：{contract.get('purpose', '')}"]
    for key in ("must_hit_now", "forbidden_cross"):
        for item in contract.get(key) or []:
            head.append(f"- {item}")
    if reference:
        head.append("## 本章可用的参考素材\n" + reference)
    user = "\n".join(head) + f"\n\n## 草稿 A\n{a}\n\n## 草稿 B\n{b}"
    raw = await chat.complete(endpoint(*judge_model), system, user,
                              temperature=0.0, max_tokens=700, json_mode=True,
                              timeout=150)
    data = loads_tolerant(raw, expect=dict) or {}
    return {"winner": str(data.get("winner") or "tie"),
            "margin": str(data.get("margin") or ""),
            "reason": str(data.get("reason") or ""),
            "evidence_a": str(data.get("evidence_a") or ""),
            "evidence_b": str(data.get("evidence_b") or "")}
