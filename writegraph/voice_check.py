"""生成后的风格检查 —— 只报告，不改写，不阻断交付（设计稿 06 §六）。

检查的是「这一章是否偏离了你选择的参考风格」，不是给文风打分。
所有指标都是确定性计算；LLM 复评若以后接入，只能作为 ``judgment``，
不能冒充硬指标。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

_SENTENCE_END = re.compile(r"[。！？!?…]+")
_DIALOGUE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"")
_SUMMARY_STARTS = ("总之", "总而言之", "其实", "这意味着", "由此可见",
                   "换句话说", "也就是说", "总的来说", "综上", "可见")
_EMOTION_WORDS = ("难过", "愤怒", "害怕", "恐惧", "幸福", "伤心", "痛苦",
                  "高兴", "悲伤", "焦虑", "绝望", "喜悦")

SHORT_PARA = 50
LONG_PARA = 150


def _sentences(text: str) -> list[str]:
    out = []
    for piece in _SENTENCE_END.split(text):
        piece = piece.strip()
        if piece:
            out.append(piece)
    return out


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in (text or "").splitlines() if p.strip()]


def metrics(text: str) -> dict[str, Any]:
    """确定性指标。分母统一用「非空白字符数」。"""
    raw = text or ""
    compact = "".join(raw.split())
    chars = len(compact)
    if not chars:
        return {"chars": 0, "sentences": 0, "avg_sentence_len": 0.0,
                "dialogue_ratio": 0.0, "summary_sentence_ratio": 0.0,
                "emotion_density": 0.0, "paragraphs": 0,
                "short_para_ratio": 0.0, "long_para_ratio": 0.0,
                "repeated_opening_ratio": 0.0}
    sentences = _sentences(raw)
    dialogue_chars = sum(len("".join(m.group(0).split()))
                         for m in _DIALOGUE.finditer(raw))
    summary = sum(1 for s in sentences
                  if any(s.startswith(mark) for mark in _SUMMARY_STARTS))
    emotion = sum(compact.count(word) for word in _EMOTION_WORDS)
    paras = _paragraphs(raw)
    short = sum(1 for p in paras if len(p) < SHORT_PARA)
    long = sum(1 for p in paras if len(p) > LONG_PARA)
    repeated = sum(1 for prev, cur in zip(paras, paras[1:])
                   if len(prev) >= 4 and len(cur) >= 4
                   and prev[:4] == cur[:4])
    return {
        "chars": chars,
        "sentences": len(sentences),
        "avg_sentence_len": round(chars / max(1, len(sentences)), 2),
        "dialogue_ratio": round(dialogue_chars / chars, 4),
        "summary_sentence_ratio": round(summary / max(1, len(sentences)), 4),
        "emotion_density": round(emotion / chars * 1000, 2),
        "paragraphs": len(paras),
        "short_para_ratio": round(short / max(1, len(paras)), 4),
        "long_para_ratio": round(long / max(1, len(paras)), 4),
        "repeated_opening_ratio": round(
            repeated / max(1, len(paras) - 1), 4),
    }


def _evidence_paragraphs(text: str, predicate) -> list[str]:
    return [f"第{i}段" for i, p in enumerate(_paragraphs(text), start=1)
            if predicate(p)][:5]


def _repeated_paragraphs(text: str) -> list[str]:
    paras = _paragraphs(text)
    out = []
    for i in range(1, len(paras)):
        prev, cur = paras[i - 1], paras[i]
        if len(prev) >= 4 and len(cur) >= 4 and prev[:4] == cur[:4]:
            out.append(f"第{i + 1}段")
    return out[:5]


def _finding(code: str, severity: str, message: str,
             evidence: list[str] | None = None) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message,
            "evidence": evidence or []}


def _trait_values(traits: Iterable[Mapping[str, Any]]
                  ) -> dict[str, str]:
    out: dict[str, str] = {}
    for trait in traits:
        if str(trait.get("status") or "active") not in ("active", "provisional"):
            continue
        if trait.get("transferable") in (0, False):
            continue
        dim = str(trait.get("dimension") or "")
        if dim and dim not in out:
            out[dim] = str(trait.get("value") or "")
    return out


def check(text: str, *, traits: Iterable[Mapping[str, Any]] = (),
          author_id: str = "") -> dict[str, Any]:
    """返回 ``{enabled, author_id, findings, metrics, note}``。

    ``severity`` 只有 ``info/warning``：这是提示，不是门禁。
    """
    m = metrics(text)
    if not m["chars"]:
        return {"enabled": False, "author_id": author_id,
                "findings": [], "metrics": m, "note": "正文为空，无从检查"}

    findings: list[dict[str, Any]] = []
    values = _trait_values(traits)
    enabled = bool(values)

    # ── 通用检查（与是否选参考风格无关）──────────────────────────────
    if m["summary_sentence_ratio"] > 0.03:
        evidence = _evidence_paragraphs(
            text, lambda p: any(s.strip().startswith(mark)
                                for s in _sentences(p)
                                for mark in _SUMMARY_STARTS))
        findings.append(_finding(
            "summary_sentence_density", "warning",
            f"本章总结句比例 {m['summary_sentence_ratio']:.0%}，"
            "参考风格建议低于 3%。", evidence))
    if m["repeated_opening_ratio"] > 0.20 and m["paragraphs"] >= 5:
        findings.append(_finding(
            "repeated_openings", "warning",
            f"相邻段落开头重复 {m['repeated_opening_ratio']:.0%}，"
            "读起来像套模板。", _repeated_paragraphs(text)))

    # ── 对照选中的文风特征 ────────────────────────────────────────────
    rhythm = values.get("sentence_rhythm")
    avg = m["avg_sentence_len"]
    if rhythm == "short" and avg > 18:
        findings.append(_finding(
            "sentence_rhythm", "warning",
            f"平均句长 {avg:.1f} 字，参考风格偏短句。"))
    elif rhythm == "long" and avg < 16:
        findings.append(_finding(
            "sentence_rhythm", "info",
            f"平均句长 {avg:.1f} 字，参考风格允许更长的句子。"))
    elif rhythm == "balanced" and not 12 <= avg <= 28:
        findings.append(_finding(
            "sentence_rhythm", "info",
            f"平均句长 {avg:.1f} 字，参考风格长短句更均衡。"))

    dialogue = values.get("dialogue_density")
    ratio = m["dialogue_ratio"]
    if dialogue == "low" and ratio > 0.15:
        findings.append(_finding(
            "dialogue_density", "warning",
            f"对话占比 {ratio:.0%}，参考风格对话更少。"))
    elif dialogue == "high" and ratio < 0.08:
        findings.append(_finding(
            "dialogue_density", "warning",
            f"对话占比 {ratio:.0%}，参考风格对话更密。"))
    elif dialogue == "medium" and (ratio < 0.04 or ratio > 0.35):
        findings.append(_finding(
            "dialogue_density", "info",
            f"对话占比 {ratio:.0%}，参考风格大致对半。"))

    shape = values.get("paragraph_shape")
    if shape == "fragmented" and m["short_para_ratio"] < 0.40:
        findings.append(_finding(
            "paragraph_shape", "info",
            f"短段占比 {m['short_para_ratio']:.0%}，参考风格段落更碎。"))
    elif shape == "blocky" and m["short_para_ratio"] > 0.60:
        findings.append(_finding(
            "paragraph_shape", "info",
            f"短段占比 {m['short_para_ratio']:.0%}，参考风格段落更成块。"))

    emotion = values.get("emotional_expression")
    density = m["emotion_density"]
    if emotion in ("withheld", "action_mediated") and density > 5:
        findings.append(_finding(
            "emotional_expression", "warning",
            f"每千字直说情绪 {density:.1f} 次，参考风格更借动作与留白。"))
    elif emotion == "explicit" and density < 1.0:
        findings.append(_finding(
            "emotional_expression", "info",
            f"每千字直说情绪 {density:.1f} 次，参考风格情绪更外露。"))

    note = "" if enabled else "没有可用的参考文风特征，只做通用检查。"
    return {"enabled": enabled, "author_id": author_id,
            "findings": findings, "metrics": m, "note": note}


__all__ = ["check", "metrics"]
