"""块预算编译 —— 确定性排序、截断、丢弃，不用 LLM 压缩。

规则四条（docs/writegraph/03-context-card.md）：

1. 必需块装不下也要保留；仍装不下就报错，**宁可失败不可静默丢正典**。
2. 可选块按 ``priority`` **降序**保留（数值越大越重要）；同优先级按
   ``drop_order`` 先丢（数值越小越先丢）。
3. 失败语义分开：``block`` / ``degrade`` / ``omit``。
4. 参考素材永远第一个丢。

> 排序语义在 AuthorGraph M4 修正：旧实现按 ``priority`` 升序迭代，与
> 「priority 越高越重要」相反，导致参考素材反而排在作品图前面。修完
> 才允许 ``author_voice`` 按真实重要性落位；两套语义不能并存。
"""

from __future__ import annotations

from typing import Any, Mapping

#: id, required, priority, drop_order, failure, budget(tokens)
BLOCKS = (
    ("chapter_contract", True, 100, None, "block", 800),
    ("facts_current", True, 100, None, "block", 900),
    ("prev_tail", True, 95, None, "block", 700),
    ("context_card", True, 90, None, "block", 1200),
    ("work_graph_hood", False, 70, 5, "degrade", 700),
    ("user_preferences", False, 60, 4, "degrade", 400),
    ("recent_summaries", False, 50, 3, "degrade", 500),
    ("author_voice", False, 40, 2, "omit", 700),
    ("reference_material", False, 30, 1, "omit", 900),
    # 别本书的素材：比本书历史段落更远一层，永远先丢。
    ("external_reference", False, 25, 0, "omit", 900),
)

SPECS = {b[0]: {"required": b[1], "priority": b[2], "drop_order": b[3],
                "failure": b[4], "budget": b[5]} for b in BLOCKS}

DEFAULT_TOTAL_BUDGET = 6000


class ProtectedContextOverflow(RuntimeError):
    """必需块超预算。继续写等于让模型自由发挥，不如失败。"""


def estimate_tokens(text: str) -> int:
    """中文按 1 字 ≈ 0.5 token 粗估；只用于块选择，不用于计费。"""
    return max(1, len(text or "") // 2)


def _titled(entry: Mapping[str, Any]) -> str:
    return f"## [{entry['id']}]\n{entry['text']}".rstrip()


def _clip(text: str, token_budget: int) -> str:
    """按整行截断，末尾显式标记 —— 截断不能长得像原文结束。"""
    limit = max(1, token_budget * 2)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut + "\n…（超出块预算，已截断）"


def compile_blocks(blocks: Mapping[str, str], *,
                   budget: int = DEFAULT_TOTAL_BUDGET,
                   specs: Mapping[str, Mapping[str, Any]] | None = None
                   ) -> dict[str, Any]:
    """输入 ``{block_id: text}``，返回 ``{text, tokens, manifest}``。

    未在 ``BLOCKS`` 里登记的块一律拒绝 —— 新块必须先定失败语义，
    不能悄悄混进来。必需块必须显式给（空串也算给了），缺一个就报错。
    """
    table = specs or SPECS
    unknown = set(blocks) - set(table)
    if unknown:
        raise ValueError(f"未登记的块：{sorted(unknown)}")
    missing = [bid for bid, spec in table.items()
               if spec["required"] and bid not in blocks]
    if missing:
        raise ProtectedContextOverflow(f"缺必需块：{sorted(missing)}")

    entries: list[dict[str, Any]] = []
    for bid, text in blocks.items():
        spec = table[bid]
        entries.append({"id": bid, "text": text or "",
                        "tokens": estimate_tokens(text),
                        "required": bool(spec["required"]),
                        "priority": spec["priority"],
                        "drop_order": spec["drop_order"],
                        "failure": spec["failure"],
                        "budget": spec["budget"]})

    # 可选块先按自己的预算截断（保留整行）；必需块不截 —— 正典只许摘要，
    # 不许按比例切，切掉的可能正好是本章唯一禁令。
    for entry in entries:
        if not entry["required"] and entry["tokens"] > entry["budget"]:
            entry["text"] = _clip(entry["text"], entry["budget"])
            entry["tokens"] = estimate_tokens(entry["text"])
            entry["reason"] = "超出块预算，已截断"

    required = sum(e["tokens"] for e in entries if e["required"])
    if required > budget:
        raise ProtectedContextOverflow(
            f"必需块 {required} tokens 超过总预算 {budget}")

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    used = required
    optional = sorted(
        (e for e in entries if not e["required"]),
        key=lambda e: (-e["priority"],
                       e["drop_order"] if e["drop_order"] is not None else 999))
    for entry in optional:
        if used + entry["tokens"] <= budget:
            used += entry["tokens"]
            kept.append(entry)
        else:
            entry["reason"] = "预算不足，已丢弃"
            dropped.append(entry)

    ordered = [e for e in entries if e["required"]] + kept
    text = "\n\n".join(_titled(e) for e in ordered)
    dropped_ids = {e["id"] for e in dropped}
    manifest = {
        "total_tokens": used,
        "blocks": [{"id": e["id"], "tokens": e["tokens"],
                    "protected": e["required"],
                    "dropped": e["id"] in dropped_ids,
                    "reason": e.get("reason", "")}
                   for e in entries],
    }
    return {"text": text, "tokens": used, "manifest": manifest}
