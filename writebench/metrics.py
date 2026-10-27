"""确定性指标：长度、去 AI 味、重复、合同关键词、专名泄漏。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Mapping

from writegraph import deslop

META = re.compile(
    r"^\s*(?:#{1,3}\s*)?(?:第[一二三四五六七八九十\d]+章|以下是|正文[:：]|"
    r"本章完|（本章完）|标题[:：]|解题步骤|写作计划|写作路线|提纲|思路)")
PLAN_LEAK = re.compile(r"(?:^|\n)\s*#{1,3}\s*(?:正文|解题步骤|写作计划|提纲)")
DIALOGUE = re.compile(r"[“「](.*?)[”」]", re.S)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。！？!?…]+", text or "") if s.strip()]


def _ngrams(text: str, n: int = 6) -> list[str]:
    clean = re.sub(r"\s+", "", text or "")
    return [clean[i:i + n] for i in range(max(0, len(clean) - n + 1))]


def repetition(text: str) -> dict[str, Any]:
    grams = Counter(_ngrams(text, 6))
    repeated = [(g, c) for g, c in grams.most_common(12) if c >= 4]
    sents = _sentences(text)
    dup = len(sents) - len(set(sents))
    return {"repeated_6grams": len(repeated), "top": repeated[:5],
            "duplicate_sentences": dup, "sentences": len(sents)}


def copy_ratio(text: str, reference: str, n: int = 8) -> float:
    """成稿里有多少 8 字窗口在参考素材里原样出现过。"""
    if not reference:
        return 0.0
    grams = set(_ngrams(text, n))
    ref = set(_ngrams(reference, n))
    return round(len(grams & ref) / max(1, len(grams)), 4)


def mechanical(case: Mapping[str, Any], text: str, *,
               reference: str = "") -> dict[str, Any]:
    text = text or ""
    check = case.get("check") or {}
    target = int((case.get("contract") or {}).get("target_chars") or 0)
    sentences = _sentences(text)
    lens = [len(s) for s in sentences] or [0]
    mean = sum(lens) / len(lens)
    var = sum((x - mean) ** 2 for x in lens) / len(lens)
    cv = (var ** 0.5) / mean if mean else 0.0
    dialogue = sum(len(m) for m in DIALOGUE.findall(text))
    must = []
    for group in check.get("must_hit_keywords") or []:
        alts = [group] if isinstance(group, str) else list(group)
        must.append(any(k in text for k in alts))
    forbidden = [k for k in check.get("forbidden_keywords") or [] if k in text]
    leaks = [k for k in check.get("leak_names") or [] if k in text]
    first = len(re.findall(r"我", text))
    third = len(re.findall(r"[他她]", text))
    pov = str((case.get("work") or {}).get("narrative_pov") or "")
    if "第一人称" in pov:
        pov_ok = first >= max(3, third)
    elif "第三人称" in pov:
        pov_ok = third > first
    else:
        pov_ok = None
    des = deslop.detect(text)
    return {
        "chars": len(text),
        "target": target,
        "length_ratio": round(len(text) / target, 3) if target else None,
        "meta_leak": bool(META.search(text)) or bool(PLAN_LEAK.search(text)),
        "first_lines": text.strip().splitlines()[:1],
        "paragraphs": len([p for p in text.split("\n") if p.strip()]),
        "sentences": len(sentences),
        "sentence_cv": round(cv, 3),
        "dialogue_ratio": round(dialogue / max(1, len(text)), 3),
        "first_person": first,
        "third_person": third,
        "pov": pov,
        "pov_ok": pov_ok,
        "must_hit": must,
        "must_hit_rate": round(sum(must) / len(must), 3) if must else None,
        "forbidden_hits": forbidden,
        "leak_hits": leaks,
        "deslop_blocking": des["blocking"],
        "deslop_issues": len(des["issues"]),
        "deslop_top": des["issues"][:6],
        "repetition": repetition(text),
        "ending": text.strip()[-90:],
        "copy_ratio": copy_ratio(text, reference),
    }


def hard_score(m: Mapping[str, Any]) -> float:
    """客观硬指标（0–10）。只罚能机械判定的错，不替代文学判断。"""
    score = 10.0
    if m.get("meta_leak"):
        score -= 10
    if m.get("must_hit_rate") is not None:
        score -= (1.0 - float(m["must_hit_rate"])) * 4
    score -= 3.0 * len(m.get("forbidden_hits") or [])
    score -= 2.0 * len(m.get("leak_hits") or [])
    if m.get("pov_ok") is False:
        score -= 5
    ratio = m.get("length_ratio")
    if ratio is not None:
        if ratio < 0.8:
            score -= (0.8 - ratio) * 10
        elif ratio > 1.25:
            score -= (ratio - 1.25) * 10
    score -= 1.0 * float(m.get("deslop_blocking") or 0)
    return round(max(0.0, score), 2)
