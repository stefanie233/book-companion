"""Recorder — write Explorer results to graph.db.

Pure SQL, no LLM calls. Handles name→node_id resolution,
dedup via ON CONFLICT, and merge candidate persistence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bookgraph.invariants import (OFF_VOCAB, finality_hit, normalize_kind,
                                  referenced_names)
from storage.graph_db import GraphDB

log = logging.getLogger("bookgraph")


@dataclass
class RecordStats:
    """Stats from recording one batch."""
    new_nodes: int = 0
    new_observations: int = 0
    new_edges: int = 0
    new_aliases: int = 0
    merge_candidates: int = 0
    new_quotes: int = 0
    errors: int = 0
    unresolved: int = 0          # 解析不到节点的引用（落 graph_unresolved）
    # 下面三项是**回炉用的缺陷清单**（P4 拿它去问 agent）。不是统计，是问题。
    orphan_dropped: list[dict] = field(default_factory=list)  # 声明了却没人引用
    kind_drift: list[dict] = field(default_factory=list)      # 同名换了 kind
    kind_off_vocab: list[str] = field(default_factory=list)   # 词表外的 kind
    # 被引用却没声明的名字 —— 兜底建了节点（kind=unknown），免得边整条蒸发
    unresolved_admitted: list[str] = field(default_factory=list)
    # 身份说明里带结局词（`invariants.finality_hit`）。骨架趟漏掉的实体
    # 由批次补 description —— 那句话会无条件进角色卡（不按章过滤），
    # 所以它和骨架的身份说明是同一类风险，只是没人查过。
    identity_warnings: list[dict] = field(default_factory=list)


#: 兜底建出来的节点用什么 kind。不编一个假的 —— 「不知道」就说不知道，
#: 体检器会把它当词表外的值报出来。
UNKNOWN_KIND = "unknown"


def _first_ref_chunk(result: dict) -> dict[str, str]:
    """``{名字: 第一次引用它的 chunk_id}``。

    兜底建的节点也要有锚点：没锚点的节点「点节点跳原文」跳不过去，剧透门
    也算不出章号 —— 那就成了二等公民。名字是从哪一块引用来的，就锚在哪一块。
    """
    out: dict[str, str] = {}

    def _note(name, cid) -> None:
        name = (name or "").strip() if isinstance(name, str) else ""
        cid = str(cid or "").strip()
        if name and cid and name not in out:
            out[name] = cid

    for ed in result.get("new_edges") or []:
        for k in ("source", "target"):
            _note(ed.get(k), ed.get("chunk_id"))
    for ob in result.get("new_observations") or []:
        _note(ob.get("node_name"), ob.get("evidence_chunk_id"))
    for q in result.get("quotes") or []:
        _note(q.get("character_name"), q.get("chunk_id"))
        for r in (q.get("related_nodes") or []):
            _note(r, q.get("chunk_id"))
    return out


def _first_mention(name: str, chunk_ids: list[str],
                   chunk_text: dict[str, str]) -> str:
    """批次内**正文里最先出现这个名字**的块。

    ``first_appearance_chunk_id`` 是**导出量**，不是判断。让模型填它，实测
    它填的是「这条断言引的 evidence 所在块」而不是首现块 —— 证伪器为此报了
    14 条质疑（占全部质疑 10%），每次还要烧一整轮重提取去改一个系统自己
    一秒就能算对的字段。谁在正文里先出现，逐块扫一遍就知道。
    """
    for cid in chunk_ids:
        body = chunk_text.get(cid)
        if body and name in body:
            return cid
    return ""


def record_result(
    db: GraphDB,
    book_id: str,
    chapter_node_ids: dict[int, str],
    chunk_ids: list[str],
    result: dict,
    batch_id: str = "",
    *,
    vocabulary: set[str] | None = None,
    key_characters: list[str] | None = None,
    chunk_text: dict[str, str] | None = None,
) -> RecordStats:
    """Write Explorer result to graph.db. Returns stats.

    Args:
        chapter_node_ids: {chapter_index: node_id} for chapter nodes
        chunk_ids: list of chunk_ids in this batch (for marking done)
        result: parsed JSON from Explorer
        batch_id: orchestrator 的真实批次 id。以前这里是 ``batch-{首块前12位}``
            —— 一个**假的** id，按批回炉/标记时对不上号。
        vocabulary: 本书的 kind 词表（`CORE_KINDS ∪ context_setter.suggested_kinds`）。
            超出词表的 kind 原样保留但记进 ``kind_off_vocab``。
        key_characters: Scout 点名的关键人物 —— 它们**无条件准入**（一本小说的
            主角可能在某一批里一条关系都没挂上，但那不代表他不是实体）。
        chunk_text: {chunk_id: 正文}。有了它，``first_appearance_chunk_id``
            由**系统逐块扫正文**算出，不再采信模型的猜测（见
            ``_first_mention``）。没给就退回模型值/引用块兜底。

    **准入闸**：``new_nodes`` 的语义是「**声明的**实体」。只有被别人引用过的
    才真的建节点 —— 孤儿由构造消灭（实测存量 17% 的节点 degree=0，全是
    「先写节点、边没连上就没人管」留下的）。没人引用的进 ``orphan_dropped``，
    回炉时要求 agent 二选一：补上关系，或撤回。
    """
    stats = RecordStats()

    def _unresolved(name: str, kind: str, chunk: str = "") -> None:
        """记一条解析不到的引用。**不再 log.debug 一句就丢**。"""
        if not name:
            return
        db.add_unresolved(book_id, name, kind, chunk)
        stats.unresolved += 1

    # name → node_id cache (seeded with chapter nodes)
    # Chapter nodes use their title as canonical_name
    name_cache: dict[str, str] = {}
    # We can't reverse chapter_index → title here, so chapter nodes
    # are looked up by name via db.name_to_node_id when needed.

    # 名字 → 第一次引用它的 chunk。两个用途：给兜底节点补锚点，以及给
    # **模型忘了写** first_appearance_chunk_id 的声明补锚点。实测 120 个真实
    # 节点里 10 个（8%）没锚点 —— 没锚点就定位不到，剧透门对它失效。
    anchors = _first_ref_chunk(result)
    _ct = chunk_text or {}

    def _anchor(name: str, claimed: str = "") -> str:
        """锚点优先级：**正文首现** → 模型自报 → 首次被引用的块。"""
        return (_first_mention(name, chunk_ids, _ct)
                or (claimed or "").strip() or anchors.get(name, ""))

    # ── 声明 → 准入 ───────────────────────────────────────────────────────
    declared: dict[str, dict] = {}
    for nn in result.get("new_nodes", []):
        name = (nn.get("canonical_name") or "").strip()
        if not name:
            continue
        kind, note = normalize_kind(nn.get("kind") or "", vocabulary)
        if note == OFF_VOCAB:
            stats.kind_off_vocab.append((nn.get("kind") or "").strip())
        declared[name] = {
            "kind": kind,
            # 提示词让模型写 ``why``（「它在这本书里做了什么」），这里过去
            # 只读 ``description`` —— 字段名对不上，**所有节点的身份说明
            # 一直是空的**。下一批提取时索引里只剩一个名字，模型没有任何
            # 线索判断「冯母」是不是「冯世杰的母亲」。
            "description": (nn.get("description") or nn.get("why") or "").strip(),
            "first_chunk": _anchor(name,
                                   nn.get("first_appearance_chunk_id") or ""),
            "why": (nn.get("why") or "").strip(),
        }

    # **本批正文**引用到的名字。兜底建节点只看这个 —— Scout 的 key_characters
    # 是**全书**名单（实测 34 个），把它混进来会让第一批就把全书角色全建成
    # 节点，且全是 degree=0（那正是我们要消灭的孤儿）。
    batch_refs = referenced_names(result)
    # 准入判定另算：Scout 点名的角色即使本批没引用，声明了也不算孤儿。
    referenced = set(batch_refs)
    referenced.update(n.strip() for n in (key_characters or []) if n and n.strip())
    # 声明了、但这一批没有任何东西引用它 —— 不建节点，记进缺陷清单
    for name, d in declared.items():
        if name not in referenced:
            stats.orphan_dropped.append({
                "name": name, "kind": d["kind"], "why": d["why"]})

    for name in declared:
        if name not in referenced:
            continue
        d = declared[name]
        prev_kind = db.node_kind(book_id, name)
        hit = finality_hit(d["description"])
        if hit:
            # 不拦、不改写 —— 只报。改写会把「她后来嫁给爱德华」这种合法
            # 身份句削成半句；这条数据的正确处置是人看，不是正则猜。
            stats.identity_warnings.append(
                {"name": name, "word": hit, "from": "batch"})
        node_id = db.upsert_node(book_id, name, d["kind"], d["description"],
                                 d["first_chunk"])
        name_cache[name] = node_id
        stats.new_nodes += 1
        if prev_kind and prev_kind != d["kind"]:
            # 节点是同一个（SQL 层按同名复用），但说法在漂 —— 报出来
            stats.kind_drift.append(
                {"name": name, "kept": prev_kind, "offered": d["kind"]})

    # ── 兜底：被引用却没声明的名字，建节点，别丢边 ────────────────────────
    # 提取器的心智模型是「声明**新**实体、引用**已有**实体」，系统要的是
    # 「凡引用必声明」—— 这条缝两个不同模型都摔（实测《我与地坛》丢掉的
    # 全是「我」「母亲」「地坛」，28 条边只落地 3 条）。提示词已写硬约束，
    # 这里再兜一层：按第二课的定义，边的端点**就是**实体（它做事了）。
    # kind 记 `unknown` 并上报 —— 宁可说「不知道」，不编一个假的。
    for name in sorted(batch_refs - set(declared)):
        if not name or db.name_to_node_id(book_id, name):
            continue
        name_cache[name] = db.upsert_node(
            book_id, name, UNKNOWN_KIND, "", _anchor(name))
        stats.unresolved_admitted.append(name)

    # ── Insert observations (event model) ─────────────────────────────────
    for ob in result.get("new_observations", []):
        node_name = (ob.get("node_name") or "").strip()
        field = (ob.get("field") or "").strip()
        value = (ob.get("value") or "").strip()
        if not node_name or not field or not value:
            continue
        node_id = name_cache.get(node_name) or db.name_to_node_id(book_id, node_name)
        if not node_id:
            stats.errors += 1
            _unresolved(node_name, "observation", str(ob.get("evidence_chunk_id") or ""))
            continue
        event_type = (ob.get("event_type") or "perspective").strip()
        subject = ob.get("subject")
        evidence = ob.get("evidence_chunk_id")
        evidence_text = (ob.get("evidence") or "").strip()
        # Resolve anchor chapter from evidence chunk (internal method)
        anchor_chapter = db._chapter_of_chunk(evidence) if evidence else None
        obs_tags = [t.strip() for t in (ob.get("tags") or []) if t and t.strip()]
        for t in obs_tags:
            db.register_tag(book_id, t, event_type)
        db.add_observation(
            book_id, node_id, field, value,
            event_type=event_type, subject=subject,
            anchor_chapter=anchor_chapter, evidence=evidence, tags=obs_tags,
            evidence_text=evidence_text,
        )
        stats.new_observations += 1

    # ── Insert aliases ────────────────────────────────────────────────────
    for al in result.get("new_aliases", []):
        alias = (al.get("alias") or "").strip()
        canon = (al.get("canonical_name") or "").strip()
        if not alias or not canon:
            continue
        node_id = name_cache.get(canon) or db.name_to_node_id(book_id, canon)
        if node_id:
            db.add_alias(book_id, alias, node_id)
            name_cache[alias] = node_id
            stats.new_aliases += 1
        else:
            _unresolved(canon, "alias")

    # ── Insert edges ──────────────────────────────────────────────────────
    for ed in result.get("new_edges", []):
        src = (ed.get("source") or "").strip()
        tgt = (ed.get("target") or "").strip()
        kind = (ed.get("kind") or "mentions").strip()
        chunk_id = ed.get("chunk_id")
        event_type = (ed.get("event_type") or "fact").strip()
        subject = ed.get("subject")
        edge_tags = [t.strip() for t in (ed.get("tags") or []) if t and t.strip()]
        if not src or not tgt:
            continue
        src_id = name_cache.get(src) or db.name_to_node_id(book_id, src)
        tgt_id = name_cache.get(tgt) or db.name_to_node_id(book_id, tgt)
        if not src_id or not tgt_id:
            stats.errors += 1
            _unresolved(src if not src_id else tgt, "edge", str(chunk_id or ""))
            continue
        for t in edge_tags:
            db.register_tag(book_id, t, event_type)
        db.add_edge(book_id, src_id, tgt_id, kind, chunk_id,
                    event_type=event_type, subject=subject, tags=edge_tags,
                    evidence_text=(ed.get("evidence") or "").strip())
        stats.new_edges += 1

    # ── Insert merge candidates ───────────────────────────────────────────
    for mc in result.get("merge_candidates", []):
        name_a = mc.get("name_a", "")
        name_b = mc.get("name_b", "")
        try:
            confidence = float(mc.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = mc.get("reason", "")
        node_id_a = name_cache.get(name_a) or db.name_to_node_id(book_id, name_a)
        db.add_merge_candidate(book_id, name_a, name_b, confidence, reason, node_id_a)
        stats.merge_candidates += 1

    # ── Insert quotes ────────────────────────────────────────────────────
    fallback_chunk = chunk_ids[0] if chunk_ids else ""
    for q in result.get("quotes", []):
        char_name = (q.get("character_name") or "").strip()
        quote_text = (q.get("quote_text") or "").strip()
        scene = (q.get("scene_context") or "").strip()
        related_names = q.get("related_nodes") or []
        # 每条引文带**自己的** chunk。以前整批共用批首块，而批首是该批最早的
        # 一块 —— 后面章节的台词因此被标成前面章节的，剧透过滤会放行它们。
        q_chunk = str(q.get("chunk_id") or "").strip() or fallback_chunk
        if not char_name or not quote_text:
            continue
        # Resolve related node names → node_ids (same resolver as edges)
        related_ids: list[str] = []
        for rname in related_names:
            rname_clean = (rname or "").strip()
            if not rname_clean:
                continue
            nid = name_cache.get(rname_clean) or db.name_to_node_id(book_id, rname_clean)
            if nid:
                related_ids.append(nid)
            else:
                _unresolved(rname_clean, "quote_related", q_chunk)
        if db.add_quote(book_id, char_name, quote_text, scene,
                        q_chunk, batch_id, related_ids):
            stats.new_quotes += 1

    # ── Mark chunks done ──────────────────────────────────────────────────
    node_count = db.total_node_count(book_id)
    for chunk_id in chunk_ids:
        db.mark_chunk_done(chunk_id, node_count)

    db.get_conn().commit()
    return stats
