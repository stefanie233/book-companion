"""从图谱提取的产物里取声音候选 —— **只读，不改 orchestrator 主流程**。

Scout 已经读过全书，让它顺手选十几段（设计稿 08 §三.3）。这里做两件事：

1. 读 ``graph_db.meta["scout_plan"]``（已有缓存）。
2. **宽容**：老计划没有 ``voice_candidates`` → 返回空，走确定性抽样；
   ``chunk_id`` 不在本书 / 块被排除 / 词表外的 ``scene_hint`` 都收掉并报。

不做「没候选就重跑 Scout」—— 那会把一次旁路变成一次全书调用。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from authorgraph.voice.vocab import SCENE_TYPES


def scout_plan(graph_db: Any) -> dict[str, Any]:
    """取缓存的 Scout 计划；没有/坏了返回空 dict。"""
    if graph_db is None:
        return {}
    try:
        raw = graph_db.get_meta("scout_plan")
    except Exception:                                   # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        plan = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return plan if isinstance(plan, dict) else {}


def head_scan_meta(graph_db: Any) -> dict[str, Any]:
    """取缓存的 ``head_scan``（块级 copyright/promo/toc 判定）。"""
    if graph_db is None:
        return {}
    try:
        raw = graph_db.get_meta("head_scan")
        data = json.loads(raw) if raw else {}
    except Exception:                                   # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def plan_hash(plan: dict[str, Any]) -> str:
    if not plan:
        return ""
    return hashlib.sha1(
        json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def extraction_run_id(graph_db: Any, book_id: str = "") -> str:
    """最近一次提取的 run_id（软引用）。取不到就空着，不编。"""
    if graph_db is None or not book_id:
        return ""
    try:
        rows = graph_db.get_recent_progress(book_id, limit=1)
    except Exception:                                   # noqa: BLE001
        rows = []
    for r in rows or []:
        rid = (r.get("run_id") or "").strip()
        if rid:
            return rid
    return ""


def voice_candidates(plan: dict[str, Any], *,
                     valid_ids: set[str],
                     excluded_ids: set[str] | None = None,
                     max_candidates: int = 20
                     ) -> tuple[list[dict[str, Any]], list[str]]:
    """Scout 候选 → 可用候选。返回 ``(candidates, warnings)``。"""
    warnings: list[str] = []
    excluded = excluded_ids or set()
    raw = plan.get("voice_candidates")
    if not isinstance(raw, list):
        if plan:
            warnings.append("Scout 计划里没有 voice_candidates —— 走确定性抽样")
        return [], warnings
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, c in enumerate(raw, 1):
        if not isinstance(c, dict):
            warnings.append(f"voice_candidate#{i}: 不是对象")
            continue
        cid = str(c.get("chunk_id") or "").strip()
        if not cid or cid in seen:
            continue
        if cid not in valid_ids:
            # 可能属于被排除的块（版权/目录）或不在本书 —— 两种都不采。
            warnings.append(f"候选块 {cid} 不在可采样集合里")
            continue
        if cid in excluded:
            warnings.append(f"候选块 {cid} 被 Scout 排除，已跳过")
            continue
        hint = str(c.get("scene_hint") or "").strip().lower()
        # 词表外/没给 → 留空，让 sampling 用启发式；不要塞 ``other``
        # 把「模型没判」伪装成「模型判了 other」。
        hint = hint if hint in SCENE_TYPES else ""
        out.append({"chunk_id": cid, "scene_hint": hint,
                    "why": str(c.get("why") or "")[:120]})
        seen.add(cid)
        if len(out) >= max_candidates:
            break
    return out, warnings


__all__ = ["extraction_run_id", "head_scan_meta", "plan_hash", "scout_plan",
           "voice_candidates"]
