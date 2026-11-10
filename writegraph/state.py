"""delta 校验与归约 —— 纯函数，模型提交什么、宿主接受什么，全在这里定。

模型只提交**增量提案**，不直接落库。宿主负责：

1. 分配承诺 id（模型只给临时 ref）；
2. 校验 ``subject_ref`` 是作品图里的真实节点；
3. 校验 ``evidence_text`` **逐字**出现在本章正文；
4. 检测同位置同字段的冲突；
5. 任何一步失败 → 整份 delta 拒绝（正文照常保存，章节标 ``settle_failed``）。

第 5 条是刻意的：**部分接受 = 账本里混进半份真相**，比整份拒绝难查得多。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from storage.writing_db import (FACT_SOURCES, PROMISE_KINDS, UNKNOWN_REASONS)

FACT_OPS = ("set", "unknown", "close")

#: 来源权威级别（决策 180）。低级别来源不得覆盖高级别来源的开放事实。
#: ``author`` 只能由作者侧入口写入；结算路径声明 author 会被直接拒绝。
AUTHORITY_RANK = {"author": 3, "import": 2, "derived": 2, "delta": 1}
SOURCE_LABELS = {"author": "作者", "import": "导入", "derived": "派生",
                 "delta": "结算"}
PROMISE_OPS = ("plant", "advance", "reinforce", "defer", "resolve",
               "abandon", "mention")


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class DeltaResult:
    ok: bool
    delta: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)


def _evidence_ok(evidence: str, text: str) -> bool:
    """逐字出现，不是模糊匹配、不是分词包含。"""
    return bool(evidence) and evidence in text


def validate_delta(delta: Any, *, chapter_index: int, chapter_text: str,
                   known_refs: Iterable[str] | None = None,
                   existing_facts: Sequence[Mapping[str, Any]] = (),
                   existing_promises: Sequence[Mapping[str, Any]] = (),
                   require_evidence: bool = True,
                   allow_author: bool = False) -> DeltaResult:
    """校验并归一化。``known_refs=None`` 表示跳过节点存在性检查（测试用）。

    ``allow_author`` 只给作者侧入口（作者亲手录入事实）用；结算路径保持
    False，模型不能自己声明 ``source_kind='author'``（决策 180）。
    """
    result = DeltaResult(ok=True)
    if not isinstance(delta, Mapping):
        result.add("delta 不是对象")
        return result

    refs = None if known_refs is None else set(known_refs)
    fact_ops = delta.get("fact_ops") or []
    promise_ops = delta.get("promise_ops") or []
    if not isinstance(fact_ops, list):
        result.add("fact_ops 不是数组")
        fact_ops = []
    if not isinstance(promise_ops, list):
        result.add("promise_ops 不是数组")
        promise_ops = []

    seen_slots: set[tuple[str, str, int]] = set()
    seen_facts: set[tuple[str, str]] = set()
    # 现有事实的起点，用于检测「同一位置两条有效值」。
    occupied: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    for row in existing_facts:
        if int(row.get("superseded") or 0):
            continue
        key = (str(row.get("subject_ref")), str(row.get("field")),
               int(row.get("valid_from_chapter") or 0))
        occupied.setdefault(key, row)
    # 当前位置仍然开放的权威事实（作者 / 导入 / 派生）；低权威来源不得覆盖。
    position = max(0, int(chapter_index) - 1)
    authoritative: dict[tuple[str, str], tuple[str, Mapping[str, Any]]] = {}
    for row in existing_facts:
        if int(row.get("superseded") or 0):
            continue
        if int(row.get("valid_from_chapter") or 0) > position:
            continue
        until = row.get("valid_until_chapter")
        if until is not None and int(until) < position:
            continue
        source = str(row.get("source_kind") or "delta")
        rank = AUTHORITY_RANK.get(source, 1)
        if rank <= 1:
            continue
        key = (str(row.get("subject_ref") or ""), str(row.get("field") or ""))
        current = authoritative.get(key)
        if current is None or AUTHORITY_RANK.get(current[0], 1) < rank:
            authoritative[key] = (source, row)
    open_promise_ids = {str(p.get("id")) for p in existing_promises}
    planted_refs: set[str] = set()

    # 引用不到的主体。逐条报错的处方相同，最后合并成一行给模型：
    # 只报「不在作品图中」等于让模型盲猜，它唯一能做的就是自造 id。
    unknown_refs: set[str] = set()
    norm_facts: list[dict[str, Any]] = []
    for idx, op in enumerate(fact_ops):
        label = f"fact_ops[{idx}]"
        if not isinstance(op, Mapping):
            result.add(f"{label} 不是对象")
            continue
        kind = str(op.get("op") or "")
        if kind not in FACT_OPS:
            result.add(f"{label} 未知 op {kind!r}")
            continue
        subject_ref = str(op.get("subject_ref") or "")
        field_name = str(op.get("field") or "")
        if not subject_ref or not field_name:
            result.add(f"{label} 缺 subject_ref/field")
            continue
        if refs is not None and subject_ref not in refs:
            result.add(f"{label} subject_ref 不在作品图中：{subject_ref}")
            unknown_refs.add(subject_ref)

        item = dict(op)
        item["subject_ref"] = subject_ref
        item["field"] = field_name

        if kind == "close":
            if op.get("until_chapter") is None:
                result.add(f"{label} close 缺 until_chapter")
                continue
            held = authoritative.get((subject_ref, field_name))
            if held is not None:
                result.add(
                    f"{label} 想关闭{SOURCE_LABELS.get(held[0], held[0])}"
                    f"来源的事实（{subject_ref}.{field_name}）；低权威来源"
                    "不能改写高权威来源，请保留原值或由作者确认")
            item["until_chapter"] = int(op["until_chapter"])
            norm_facts.append(item)
            continue

        since = int(op.get("since_chapter", chapter_index))
        if since < 0 or since > chapter_index:
            result.add(f"{label} since_chapter 越界：{since}")
        slot = (subject_ref, field_name, since)
        existing = occupied.get(slot)
        if slot in seen_slots:
            result.add(f"{label} 同一位置重复定义：{subject_ref}.{field_name}@{since}")
        elif existing is not None:
            # 同一章重结算（改稿后再结算）：允许覆盖本章上一版产生的值，
            # 旧行由 apply 标 superseded（保留行，不参与求值）。跨章仍拒绝。
            if int(existing.get("source_chapter") or -1) != int(chapter_index):
                result.add(
                    f"{label} 同一位置重复定义：{subject_ref}.{field_name}@{since}")
            else:
                item["resettle"] = True
        seen_slots.add(slot)

        if (subject_ref, field_name) in seen_facts:
            result.add(f"{label} 同一份 delta 里重复改同一字段")
        seen_facts.add((subject_ref, field_name))

        source = str(op.get("source_kind") or "delta")
        if source not in FACT_SOURCES:
            result.add(f"{label} 未知 source_kind {source!r}")
        if source == "author" and not allow_author:
            result.add(
                f"{label} 结算路径不能声明 source_kind='author'；"
                "作者事实由作者侧录入，结算只提交派生/结算值")
        held = authoritative.get((subject_ref, field_name))
        if held is not None and AUTHORITY_RANK.get(source, 1) < AUTHORITY_RANK.get(held[0], 1):
            result.add(
                f"{label} 会覆盖{SOURCE_LABELS.get(held[0], held[0])}来源的"
                f"事实（{subject_ref}.{field_name}）；低权威来源不能改写"
                "高权威来源，请保留原值或由作者确认")
        evidence = str(op.get("evidence_text") or "")
        if require_evidence and source != "author" and not _evidence_ok(
                evidence, chapter_text):
            result.add(f"{label} evidence_text 未在本章正文中逐字出现")

        if op.get("value") is not None and op.get("unknown_reason") is not None:
            result.add(f"{label} value 与 unknown_reason 不能同时给")

        if kind == "set":
            value = op.get("value")
            if value is None or str(value) == "":
                result.add(f"{label} set 缺 value")
        else:
            reason = str(op.get("unknown_reason") or "")
            if reason not in UNKNOWN_REASONS:
                result.add(f"{label} unknown_reason 非法：{reason!r}")
        item["source_kind"] = source
        item["since_chapter"] = since
        norm_facts.append(item)

    if unknown_refs and refs is not None:
        # 处方：要么用已有主体，要么在本份 delta 的 entity_ops 里建。
        # 不写这条，模型只知道「错了」，下次还会自造 id。
        if refs:
            result.add(
                "可用的 subject_ref：" + "、".join(sorted(refs)[:12])
                + "。新登场的主体先放进 entity_ops 的 create（name 即 id），"
                  "fact_ops 再引用这个 name")
        else:
            result.add(
                "作品里还没有任何主体。新登场的人物/地点/物品一律放进 "
                "entity_ops 的 create（name 即 id），不要自造 subject_ref")

    norm_promises: list[dict[str, Any]] = []
    for idx, op in enumerate(promise_ops):
        label = f"promise_ops[{idx}]"
        if not isinstance(op, Mapping):
            result.add(f"{label} 不是对象")
            continue
        kind = str(op.get("op") or "")
        if kind not in PROMISE_OPS:
            result.add(f"{label} 未知 op {kind!r}")
            continue
        item = dict(op)
        ref = str(op.get("id") or op.get("ref") or "")

        if kind == "plant":
            title = str(op.get("title") or "").strip()
            if not title:
                result.add(f"{label} plant 缺 title")
            pkind = str(op.get("kind") or "foreshadow")
            if pkind not in PROMISE_KINDS:
                result.add(f"{label} 未知承诺类别 {pkind!r}")
            weight = int(op.get("weight", 5))
            if not 1 <= weight <= 10:
                result.add(f"{label} weight 越界：{weight}")
            evidence = str(op.get("evidence_text") or "")
            if require_evidence and not _evidence_ok(evidence, chapter_text):
                result.add(f"{label} evidence_text 未在本章正文中逐字出现")
            if ref:
                planted_refs.add(ref)
        else:
            if not ref:
                result.add(f"{label} 缺 id")
            elif ref not in open_promise_ids and ref not in planted_refs:
                result.add(f"{label} 引用未知承诺：{ref}")
        norm_promises.append(item)

    return DeltaResult(
        ok=result.ok,
        errors=result.errors,
        delta={
            "chapter_summary": str(delta.get("chapter_summary") or "").strip(),
            "key_events": list(delta.get("key_events") or []),
            "fact_ops": norm_facts,
            "promise_ops": norm_promises,
            "entity_ops": list(delta.get("entity_ops") or []),
        },
    )
