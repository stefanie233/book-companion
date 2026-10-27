"""把一批模型输出收成落库形状 —— 词表、证据范围、置信度三道。

**不认识的照报，不装作没判定**（与 ``harvest.normalize_result`` 同一条）。
三条这里独有的规则：

1. **证据必须来自本批。** 模型引用一个没给它的 ``corpus_id``，说明它在
   编或者串了上下文；整条证据剔除，一条不剩就整条 trait 丢弃。
2. **一条证据不算特征。** 只有 1 段证据时 ``strength ≤ 0.4`` 且标
   ``provisional``（设计稿 04 §四.4）—— 一段话解释不了「稳定写法」。
3. **``voice_domain`` 缺失要推断并记账。** 序跋/访谈写得再像也不是小说
   文风；推断出来的值必须带 ``inferred_domain``，让消费端知道它弱一层。
"""

from __future__ import annotations

from typing import Any

from authorgraph.voice.vocab import (NARRATIVE_LAYERS, norm_domain,
                                     norm_layer, norm_period, norm_pov,
                                     norm_scene, norm_value)

#: 来源层级是书（tier 1）—— 声音只从书里取，所以基数是 1.0。
_BASE = 1.0


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _f(v: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def _ids(v: Any) -> list[int]:
    out: list[int] = []
    for x in (v if isinstance(v, list) else []):
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in out:
            out.append(i)
    return out


def confidence_for(evidence_count: int, agreement: float) -> float:
    """``base × 证据充分度 × 跨批一致度``（设计稿 04 §六.3）。

    2 条 / 3–5 条 / 6+ 条 → 0.7 / 0.85 / 1.0。**不做「科学精确」的分数**，
    端上只显示高/中/低与证据数。
    """
    if evidence_count >= 6:
        suff = 1.0
    elif evidence_count >= 3:
        suff = 0.85
    elif evidence_count >= 2:
        suff = 0.7
    else:
        suff = 0.4
    return round(_BASE * suff * max(0.0, min(1.0, agreement)), 3)


def normalize_batch(parsed: dict[str, Any], *,
                    samples_by_id: dict[int, dict[str, Any]],
                    default_domain: str = "unknown",
                    agreement: float = 0.85
                    ) -> dict[str, Any]:
    """收一批。``samples_by_id`` 是 ``{corpus_id: sample}``（只含本批）。"""
    defects: list[str] = []
    samples: list[dict[str, Any]] = []
    for i, s in enumerate((parsed.get("samples") or [])[:24], 1):
        if not isinstance(s, dict):
            defects.append(f"sample#{i}: 不是对象")
            continue
        try:
            cid = int(s.get("corpus_id"))
        except (TypeError, ValueError):
            defects.append(f"sample#{i}: corpus_id 不是整数")
            continue
        if cid not in samples_by_id:
            defects.append(f"sample#{i}: corpus_id={cid} 不在本批输入里")
            continue
        layer = norm_layer(s.get("narrative_layer"))
        if layer not in NARRATIVE_LAYERS:
            layer = "narration"
        samples.append({
            "corpus_id": cid,
            "scene_type": norm_scene(s.get("scene_type")),
            "narrative_layer": layer,
            "pov": norm_pov(s.get("pov")),
            "period": norm_period(s.get("period")),
            "notes": _s(s.get("notes"))[:300],
        })

    traits: list[dict[str, Any]] = []
    for i, t in enumerate((parsed.get("traits") or [])[:32], 1):
        if not isinstance(t, dict):
            defects.append(f"trait#{i}: 不是对象")
            continue
        dim = _s(t.get("dimension"))
        value, code = norm_value(dim, _s(t.get("value")))
        if code == "unknown_dimension":
            defects.append(f"trait#{i}: 未知维度 {dim!r}，已丢弃")
            continue
        if code == "unknown_value":
            defects.append(f"trait#{i}: {dim} 的取值词表外 → variable")
        statement = _s(t.get("statement"))
        if not statement:
            defects.append(f"trait#{i}: 缺 statement，已丢弃")
            continue

        ev = [cid for cid in _ids(t.get("evidence_corpus_ids"))
              if cid in samples_by_id]
        dropped = [cid for cid in _ids(t.get("evidence_corpus_ids"))
                   if cid not in samples_by_id]
        if dropped:
            defects.append(f"trait#{i}: 证据 {dropped} 不在本批，已剔除")
        if not ev:
            defects.append(f"trait#{i}: 没有有效证据，已丢弃")
            continue

        domain, inferred = norm_domain(_s(t.get("voice_domain")),
                                       fallback=default_domain)
        if inferred:
            defects.append(
                f"trait#{i}: voice_domain 缺失 → 按来源推断为 {domain}")
        elif (domain != default_domain
              and default_domain in ("fiction", "nonfiction")):
            # 来源层（position）是硬事实，模型给的域与之冲突时要留下账 ——
            # 不覆盖判断，但消费端能看到「这条与书的性质不一致」。
            defects.append(
                f"trait#{i}: voice_domain={domain} 与来源层 "
                f"{default_domain} 不一致")
        chunks = {samples_by_id[c].get("chunk_id") for c in ev}
        chapters = {samples_by_id[c].get("chapter_index") for c in ev} - {None}
        strength = _f(t.get("strength"))
        status = "active"
        if len(chunks) < 2:
            strength = min(strength, 0.4)
            status = "provisional"
            defects.append(f"trait#{i}: 只有 1 段证据 → provisional（strength≤0.4）")
        elif chapters and len(chapters) < 2:
            # 同一章的两段可能只是同一处文字的两次切片。设计稿要求
            # 「≥2 个不同章节/来源」；单章作品因此只能是 provisional。
            status = "provisional"
            defects.append(f"trait#{i}: 证据全在同一章 → provisional")
        conf = confidence_for(len(ev), agreement)
        if conf < 0.5:
            status = "provisional"
        traits.append({
            "dimension": dim, "value": value or "variable",
            "voice_domain": domain, "statement": statement,
            "strength": round(strength, 3),
            "transferable": bool(t.get("transferable", True)),
            "confidence": conf, "status": status,
            "evidence_ids": ev, "model_confidence": _f(t.get("confidence")),
        })

    source_bound: list[dict[str, Any]] = []
    for i, sb in enumerate((parsed.get("source_bound") or [])[:12], 1):
        if not isinstance(sb, dict):
            defects.append(f"source_bound#{i}: 不是对象")
            continue
        name = _s(sb.get("name"))
        statement = _s(sb.get("statement"))
        if not (name or statement):
            continue
        ev = [cid for cid in _ids(sb.get("evidence_corpus_ids"))
              if cid in samples_by_id]
        source_bound.append({
            "name": name, "type": _s(sb.get("type")) or "other",
            "statement": statement, "evidence_ids": ev,
        })

    for d in (parsed.get("defects") or [])[:16]:
        txt = _s(d) if not isinstance(d, dict) else _s(d.get("note") or d)
        if txt:
            defects.append(f"模型自报：{txt}")

    return {"samples": samples, "traits": traits,
            "source_bound": source_bound, "defects": defects}


__all__ = ["confidence_for", "normalize_batch"]
