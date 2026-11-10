"""两段式记忆维护的写作版：第一阶段抽取，第二阶段合并。

Phase 1 只看一章 + 合同 + 结算 + 作者对话，产出结构化的候选；
Phase 2 看当前 作品记忆/设定 + 待合并记录 + 作者改动，产出可校验的 ops。
两段都不直接写文件；落盘由 memory_guard 做。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from llm_json import loads_tolerant

#: 一段输入的上限，防止把整本书塞进一次调用。
MAX_TRANSCRIPT_CHARS = 12000
MAX_SETTINGS_CHARS = 40000


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _contract_lines(contract: Mapping[str, Any] | None) -> str:
    if not contract:
        return "（还没有合同）"
    lines = [f"- {label}：{contract[key]}"
             for label, key in (("目的", "purpose"),
                                ("独占事件", "exclusive_event"),
                                ("章末状态", "ending_state"),
                                ("下场可承接", "next_entry_state"),
                                ("弧线", "arc_ref"))
             if contract.get(key)]
    for label, key in (("必须兑现", "must_hit_now"),
                       ("必须保留", "must_preserve"),
                       ("禁止触碰", "forbidden_cross")):
        if contract.get(key):
            lines.append(f"- {label}：" + "；".join(
                str(x) for x in contract[key]))
    return "\n".join(lines) or "（合同为空）"


def build_extract_input(chapter: Mapping[str, Any],
                        contract: Mapping[str, Any] | None, *,
                        delta: Any = None, outcomes: Any = None,
                        transcript: str = "") -> str:
    """Phase 1 的 user message：一章的现场 + 作者原话。"""
    content = str(chapter.get("content") or "")
    if len(content) > MAX_TRANSCRIPT_CHARS:
        content = content[:MAX_TRANSCRIPT_CHARS] + "\n…（正文已截断）"
    parts = [
        (f"# 第{chapter.get('chapter_index')}章 "
         f"{chapter.get('title') or ''}").rstrip(),
        "## 正文\n" + (content or "（空）"),
        "## 合同\n" + _contract_lines(contract),
    ]
    if delta:
        parts.append("## 结算结果\n" + json.dumps(
            delta, ensure_ascii=False, default=str)[:6000])
    if outcomes:
        parts.append("## 合同兑现申报\n" + json.dumps(
            outcomes, ensure_ascii=False, default=str)[:3000])
    if transcript.strip():
        parts.append("## 作者对话\n"
                     + transcript.strip()[:MAX_TRANSCRIPT_CHARS])
    return "\n\n".join(parts)


def parse_extract(raw: str) -> dict[str, Any]:
    """模型输出 → 规范化记录；解析失败按 no-op 处理，不让坏 JSON 污染记忆。"""
    parsed = loads_tolerant(raw, expect=dict)
    if not isinstance(parsed, Mapping):
        return {"no_op": True, "error": "parse_failed",
                "rollout_summary": "", "preferences": [],
                "settings": [], "plans": [], "materials": []}
    out = {
        "no_op": bool(parsed.get("no_op")),
        "rollout_summary": _text(parsed.get("rollout_summary"))[:4000],
        "preferences": [x for x in _as_list(parsed.get("preferences"))
                        if isinstance(x, Mapping) and _text(x.get("signal"))],
        "settings": [x for x in _as_list(parsed.get("settings"))
                     if isinstance(x, Mapping) and _text(x.get("text"))],
        "plans": [x for x in _as_list(parsed.get("plans"))
                  if isinstance(x, Mapping) and _text(x.get("text"))],
        "materials": [x for x in _as_list(parsed.get("materials"))
                      if isinstance(x, Mapping) and _text(x.get("text"))],
    }
    if not any(out[key] for key in
               ("preferences", "settings", "plans", "materials")):
        out["no_op"] = True
    return out


def build_consolidate_input(*, bible: str, settings: str,
                            pending: Sequence[Mapping[str, Any]],
                            changes: Mapping[str, Sequence[str]],
                            diff: str = "",
                            audit: Mapping[str, Any] | None = None) -> str:
    """Phase 2 的 user message：当前层 + 待合并记录 + 作者改动（遗忘信号）。"""
    payload = [{"chapter_index": int(row.get("chapter_index") or 0),
                "payload": row.get("payload")}
               for row in pending]
    parts = [
        "# 当前作品记忆\n" + (bible.strip() or "（空）"),
        "# 当前设定\n" + (settings.strip() or "（空）"),
        "# 待合并的抽取记录\n" + json.dumps(
            payload, ensure_ascii=False, default=str),
        "# 作者自上次合并以来的改动\n" + "\n".join(
            f"- {label}：{'、'.join(items) or '无'}"
            for label, items in (("新增", changes.get("added") or []),
                                 ("修改", changes.get("changed") or []),
                                 ("删除", changes.get("removed") or []))),
        "# 改动 diff\n" + (diff.strip() or "（没有内容级 diff；只有路径）"),
        "# 机械审计（只报告）\n" + json.dumps(
            dict(audit or {}), ensure_ascii=False, default=str)[:5000],
    ]
    return "\n\n".join(parts)


def parse_consolidate(raw: str) -> dict[str, Any]:
    """模型输出 → ops。坏 JSON 按 no-op 处理，不落任何文件。"""
    parsed = loads_tolerant(raw, expect=dict)
    if not isinstance(parsed, Mapping):
        return {"no_op": True, "bible": "", "bible_ops": [], "writes": [],
                "appends": [], "patches": [], "merges": [], "deletes": [],
                "notes": "parse_failed"}
    writes = []
    for item in _as_list(parsed.get("writes")):
        if isinstance(item, Mapping):
            writes.append({"path": _text(item.get("path")),
                           "content": str(item.get("content") or "")})
    bible_ops = []
    for item in _as_list(parsed.get("bible_ops")):
        if isinstance(item, Mapping):
            bible_ops.append({"op": _text(item.get("op")),
                              "section": _text(item.get("section")),
                              "content": str(item.get("content") or ""),
                              "text": _text(item.get("text"))})
    appends = []
    for item in _as_list(parsed.get("appends")):
        if isinstance(item, Mapping):
            appends.append({"path": _text(item.get("path")),
                            "section": _text(item.get("section")),
                            "content": str(item.get("content") or "")})
    patches = []
    for item in _as_list(parsed.get("patches")):
        if isinstance(item, Mapping):
            patches.append({"path": _text(item.get("path")),
                            "old": str(item.get("old") or ""),
                            "new": str(item.get("new") or "")})
    merges = []
    for item in _as_list(parsed.get("merges")):
        if isinstance(item, Mapping):
            merges.append({"from": _text(item.get("from")),
                           "into": _text(item.get("into"))})
    deletes = []
    for item in _as_list(parsed.get("deletes")):
        path = _text(item.get("path") if isinstance(item, Mapping) else item)
        if path:
            deletes.append(path)
    return {
        "no_op": bool(parsed.get("no_op")),
        "bible": str(parsed.get("bible") or ""),
        "bible_ops": bible_ops,
        "writes": writes,
        "appends": appends,
        "patches": patches,
        "merges": merges,
        "deletes": deletes,
        "notes": _text(parsed.get("notes"))[:500],
    }
