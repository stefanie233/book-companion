"""本章目标清单 —— 软件冻结，模型只填三态。

借自 Pagekin 的 chapter-goal-review（决策 175）。核心约束三条：

1. **清单由软件冻结**：来源是合同的 ``must_hit_now`` / ``must_preserve``
   与作者在设定里写的 ``【第N章必现】`` 标记。模型不得增删条目、改写目标。
2. **三态语义**：``completed`` / ``unmet`` / ``unknown``。``unmet`` 需要正文
   给出明确的延期、拒绝或相反结果；只写准备或部分完成不算；没写到只能判
   ``unknown``，不能断言「没发生」。
3. **证据必须逐字且唯一**：引文在正文里对不上或出现多次，一律降级
   ``unknown`` 并标注人工核实，不猜测、不拼接。

这份清单只描述「作者要它发生的事」，不是评分项；与 6 域 10 判据分开存放。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

GOAL_VERSION = 1
GOAL_STATUSES = ("completed", "unmet", "unknown")

#: 与 Pagekin 对齐：独占一行、行首标记、章号精确匹配。
_MARKER = re.compile(r"^\s*【第(\d+)章必现】\s*(\S(?:.*\S)?)\s*$")

MAX_GOAL_ITEMS = 40


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def marker_goals(author_sources: Sequence[str], chapter_index: int) -> list[str]:
    """从设定/作品记忆全文里挑出属于本章的必现标记，保持出现顺序。"""
    out: list[str] = []
    for source in author_sources or ():
        for line in str(source or "").splitlines():
            match = _MARKER.match(line)
            if match and int(match.group(1)) == int(chapter_index):
                out.append(match.group(2))
    return out


def freeze_goals(contract: Mapping[str, Any] | None,
                 chapter_index: int, *,
                 author_sources: Sequence[str] = ()) -> dict[str, Any]:
    """冻结本章清单。没有条目不是错误，但要显式报 ``not_configured``。"""
    contract = contract or {}
    items: list[dict[str, str]] = []
    for index, text in enumerate(_as_list(contract.get("must_hit_now"))):
        items.append({"id": f"ch{chapter_index}:must_hit:{index + 1}",
                      "text": text, "source": "contract.must_hit_now"})
    for index, text in enumerate(_as_list(contract.get("must_preserve"))):
        items.append({"id": f"ch{chapter_index}:preserve:{index + 1}",
                      "text": text, "source": "contract.must_preserve"})
    for index, text in enumerate(marker_goals(author_sources, chapter_index)):
        items.append({"id": f"ch{chapter_index}:mustShow:{index + 1}",
                      "text": text, "source": "author.marker"})
    items = items[:MAX_GOAL_ITEMS]
    return {
        "version": GOAL_VERSION,
        "chapter_number": int(chapter_index),
        "coverage": "complete" if items else "not_configured",
        "items": items,
    }


def render_prompt_block(goals: Mapping[str, Any]) -> str:
    """拼进审稿 user message 的清单与判定规则；空清单返回空串。"""
    items = list(goals.get("items") or [])
    if not items:
        return ""
    checklist = json.dumps(
        {"chapter_number": goals.get("chapter_number"),
         "items": [{"id": i["id"], "text": i["text"]} for i in items]},
        ensure_ascii=False)
    return f"""## 本章目标逐项核对（软件冻结清单）

在同一个 JSON 根对象里增加 `goalReviews` 数组，按 id 逐项返回：

```json
{{"id": "原始 id", "status": "completed|unmet|unknown",
  "description": "逐个说明目标里的子动作、前章同一事件的终态、本章的动作与后果",
  "evidence": [{{"quote": "当前正文逐字引文"}}]}}
```

- 不得删除条目、改写目标或自创 id；清单有几项就返回几项。
- 先分清「本章要新发生」「明确要求回顾」「只要求维持状态」；要求本章推进时，
  旧事复述不能替代；作者只要求维持时，不强造新动作或代价。
- `unmet`：正文有明确的延期、拒绝或相反结果。准备、承诺、部分完成都不算完成。
- `unknown`：未提及、无法判断或证据不足。**不能以「没写到」断言「没发生」。**
- `completed` / `unmet` 必须附当前正文逐字、且全文只出现一次的引文；
  `unknown` 可 `evidence: []`。引文对不上会被机械校验降级为 `unknown`。
- 目标原文与作者确认设定冲突时，无论正文怎么写都判 `unknown`，并在 description 说明。
- 汇总：任一 `unmet` → `unmet`；否则任一 `unknown` → `unknown`；全部完成才 `completed`。

冻结清单：{checklist}"""


def _locate(quote: Any, content: str) -> dict[str, Any] | None:
    """逐字且唯一才算证据；允许模型多包一层中文引号。"""
    text = str(quote or "").strip()
    if not text:
        return None
    for candidate in (text, text[1:-1] if len(text) > 2 else ""):
        if not candidate:
            continue
        start = content.find(candidate)
        if start < 0:
            continue
        if content.find(candidate, start + 1) >= 0:
            return None
        return {"quote": candidate, "start": start,
                "end": start + len(candidate)}
    return None


def normalize_goal_reviews(raw: Any, goals: Mapping[str, Any],
                           content: str) -> dict[str, Any]:
    """机械校验模型的三态结论；任何不合规都降级 unknown 并说明。"""
    candidates = raw if isinstance(raw, list) else []
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for item in candidates:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            by_id.setdefault(item["id"], []).append(item)

    invalid = not isinstance(raw, list)
    items: list[dict[str, Any]] = []
    for goal in goals.get("items") or []:
        matches = by_id.get(goal["id"], [])
        candidate = matches[0] if len(matches) == 1 else None
        status = str(candidate.get("status") or "") if candidate else ""
        description = str(candidate.get("description") or "").strip() if candidate else ""
        evidence: list[dict[str, Any]] = []
        valid = bool(candidate and status in GOAL_STATUSES and description
                     and not (set(candidate) - {"id", "status", "description",
                                                "evidence"}))
        if valid and candidate is not None:
            raw_evidence = candidate.get("evidence")
            if not isinstance(raw_evidence, list):
                valid = False
            else:
                for entry in raw_evidence:
                    located = (_locate(entry.get("quote"), content)
                               if isinstance(entry, Mapping) else None)
                    if located is None:
                        valid = False
                        break
                    evidence.append(located)
                if status != "unknown" and not evidence:
                    valid = False
        if not valid:
            invalid = True
            items.append({
                "id": goal["id"], "text": goal["text"],
                "status": "unknown",
                "description": "该目标缺少有效的逐项判断或正文证据，请人工核实。",
                "evidence": [],
            })
            continue
        items.append({
            "id": goal["id"], "text": goal["text"],
            "status": status,
            "description": description,
            "evidence": evidence,
        })

    statuses = [item["status"] for item in items]
    if "unmet" in statuses:
        overall = "unmet"
    elif "unknown" in statuses:
        overall = "unknown"
    elif statuses:
        overall = "completed"
    else:
        overall = "not_configured"
    return {
        "version": GOAL_VERSION,
        "chapter_number": goals.get("chapter_number"),
        "coverage": ("unknown" if invalid else goals.get("coverage")),
        "overall": overall,
        "items": items,
    }


def summary_line(result: Mapping[str, Any]) -> str:
    """给工具回执/UI 用的一行摘要。"""
    counts = {"completed": 0, "unmet": 0, "unknown": 0}
    for item in result.get("items") or []:
        counts[item.get("status", "unknown")] = \
            counts.get(item.get("status", "unknown"), 0) + 1
    if not result.get("items"):
        return "本章未配置可核对的目标"
    return (f"目标核对：完成 {counts['completed']} / 未达成 {counts['unmet']}"
            f" / 待核实 {counts['unknown']}")
