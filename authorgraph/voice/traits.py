"""跨批合并、跨批一致度、revision。

一次 run 只调用 1–2 次，所以「跨批」最多是两批。**两批都说的事**才是
稳定的；一批说「短句」另一批说「长句」，正确的产物不是平均，而是
一条 ``variable`` + 两条子证据（设计稿 04 §六.4）—— 风格随场景/时期
变化是真实情况，抹平成均值才是失真。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from authorgraph.voice.normalize import confidence_for

_PUNCT = re.compile(r"[\s。；;，,、]+")


def statement_key(statement: str) -> str:
    return _PUNCT.sub("", statement or "")


def revision_for(evidence_ids: list[int], samples_by_id: dict[int, dict[str, Any]],
                 prompt_version: str, model: str) -> str:
    """同一批语料 + 同一提示词 + 同一模型 → 同一 revision（幂等）。"""
    parts = sorted(f"{cid}:"
                   f"{hashlib.sha1(str(samples_by_id[cid].get('text') or '').encode('utf-8')).hexdigest()}"
                   for cid in evidence_ids if cid in samples_by_id)
    key = "|".join(parts) + "|" + prompt_version + "|" + model
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _evidence_chunks(ev: list[int],
                     samples_by_id: dict[int, dict[str, Any]]) -> set[str]:
    return {str(samples_by_id[c].get("chunk_id") or c)
            for c in ev if c in samples_by_id}


def merge_batches(batches: list[dict[str, Any]], *,
                  samples_by_id: dict[int, dict[str, Any]],
                  scope_type: str = "work", scope_ref: str = "",
                  prompt_version: str = "", model: str = ""
                  ) -> dict[str, Any]:
    """合并 1–2 批归一化结果。返回 ``{traits, source_bound, conflicts,
    defects, revision}``。"""
    defects: list[str] = []
    conflicts: list[str] = []
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    #: 每个维度在各批里的取值集合 —— 判断「跨批一致/冲突」。
    dim_values: dict[str, set[str]] = {}
    dim_batches: dict[str, int] = {}

    for bi, batch in enumerate(batches, 1):
        seen_dims: set[str] = set()
        for t in (batch.get("traits") or []):
            dim = t["dimension"]
            val = t["value"]
            dim_values.setdefault(dim, set()).add(val)
            seen_dims.add(dim)
            key = (dim, val, t["voice_domain"], statement_key(t["statement"]))
            cur = merged.get(key)
            if cur is None:
                merged[key] = {
                    "dimension": dim, "value": val,
                    "voice_domain": t["voice_domain"],
                    "statement": t["statement"],
                    "strength": float(t["strength"]),
                    "transferable": bool(t.get("transferable", True)),
                    "model_confidence": float(t.get("model_confidence") or 0.0),
                    "evidence_ids": list(t["evidence_ids"]),
                    "statuses": [t.get("status") or "provisional"],
                    "batches": [bi],
                }
            else:
                cur["strength"] = max(cur["strength"], float(t["strength"]))
                cur["transferable"] = cur["transferable"] and bool(
                    t.get("transferable", True))
                cur["model_confidence"] = max(cur["model_confidence"],
                                              float(t.get("model_confidence") or 0.0))
                cur["evidence_ids"] = list(dict.fromkeys(
                    cur["evidence_ids"] + list(t["evidence_ids"])))
                cur["statuses"].append(t.get("status") or "provisional")
                if bi not in cur["batches"]:
                    cur["batches"].append(bi)
        for dim in seen_dims:
            dim_batches[dim] = dim_batches.get(dim, 0) + 1

    n_batches = max(1, len([b for b in batches if b.get("traits")]))
    out: list[dict[str, Any]] = []
    for key, t in merged.items():
        dim = t["dimension"]
        vals = dim_values.get(dim) or {t["value"]}
        if n_batches <= 1:
            agreement = 0.85                 # 单批：留一点不确定
        elif len(vals) > 1:
            agreement = 0.4                  # 跨批冲突
        elif dim_batches.get(dim, 1) >= n_batches:
            agreement = 0.9                  # 每批都提到且取值一致
        else:
            agreement = 0.7                  # 有的批没提这个维度
        conf = confidence_for(len(t["evidence_ids"]), agreement)
        chapters = {samples_by_id[c].get("chapter_index")
                    for c in t["evidence_ids"] if c in samples_by_id} - {None}
        status = "active" if (conf >= 0.5
                              and len(_evidence_chunks(t["evidence_ids"],
                                                       samples_by_id)) >= 2
                              and (not chapters or len(chapters) >= 2)) \
            else "provisional"
        if len(vals) > 1 and status == "active":
            status = "provisional"
            conflicts.append(f"{dim} 跨批取值不一致：{' / '.join(sorted(vals))}")
        out.append({
            "dimension": dim, "value": t["value"],
            "voice_domain": t["voice_domain"], "statement": t["statement"],
            "strength": round(t["strength"], 3),
            "transferable": t["transferable"], "confidence": conf,
            "status": status, "evidence_ids": t["evidence_ids"],
            "scope_type": scope_type, "scope_ref": scope_ref,
            "model_confidence": t["model_confidence"],
        })

    # 高置信冲突 → 一条 variable 汇总。**不改写原判断**，只是并列。
    for dim, vals in dim_values.items():
        if len(vals) <= 1:
            continue
        related = [t for t in out if t["dimension"] == dim
                   and t["model_confidence"] >= 0.6]
        if len({t["value"] for t in related}) <= 1:
            continue
        ev = list(dict.fromkeys(cid for t in related for cid in t["evidence_ids"]))
        stmt = ("不同批次给出不同判断：" +
                " / ".join(sorted({t["value"] for t in related})) +
                "；可能是场景或时期差异，不要平均成单一风格。")
        out.append({
            "dimension": dim, "value": "variable",
            "voice_domain": related[0]["voice_domain"], "statement": stmt,
            "strength": round(min(t["strength"] for t in related), 3),
            "transferable": True,
            "confidence": round(max(0.3, min(t["confidence"] for t in related)
                                    * 0.7), 3),
            "status": "provisional", "evidence_ids": ev,
            "scope_type": scope_type, "scope_ref": scope_ref,
            "model_confidence": 0.0,
        })
        for t in related:
            t["status"] = "provisional"
        conflicts.append(f"{dim} 跨批取值不一致 → 已生成 variable 汇总")

    # source_bound：按名字 + 说法去重，证据取并集。
    sb_out: list[dict[str, Any]] = []
    sb_index: dict[tuple[str, str], dict[str, Any]] = {}
    for b in batches:
        for sb in (b.get("source_bound") or []):
            key = (sb.get("name") or "", statement_key(sb.get("statement") or ""))
            cur = sb_index.get(key)
            if cur is None:
                cur = dict(sb)
                cur["evidence_ids"] = list(sb.get("evidence_ids") or [])
                sb_index[key] = cur
                sb_out.append(cur)
            else:
                cur["evidence_ids"] = list(dict.fromkeys(
                    cur["evidence_ids"] + list(sb.get("evidence_ids") or [])))

    all_ev = sorted({cid for t in out for cid in t["evidence_ids"]})
    rev = revision_for(all_ev, samples_by_id, prompt_version, model)
    for b in batches:
        defects.extend(b.get("defects") or [])
    return {"traits": out, "source_bound": sb_out, "conflicts": conflicts,
            "defects": defects, "revision": rev}


__all__ = ["merge_batches", "revision_for", "statement_key"]
