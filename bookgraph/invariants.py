"""提取不变量 —— 纯代码体检 agent 给出的图谱，返回**问题清单**。

这不是打分器，是**提问器**。每条发现都是一个具体的问题：「这个节点没有任何
关系、也没有任何观察，它凭什么是一个实体？」—— 问题回给 agent，由它决定
补上关系还是撤回。

为什么要有这一层：提取结果现在**零校验**直接落库（导入侧早就有
``services/structure_invariants.py`` 这一层了）。而 agent 的结论会漂，机械
不变量不含观点、不会漂，正好把漂移逼到台面上。

**先测量再动手**：本模块的第一批消费者是 ``scripts/graph_invariants.py``，
用它把「哪里真的坏了」钉成数字，而不是靠推断。

纯函数模块：不碰库、不调 LLM、不抛异常。调用方负责把数据查出来传进来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 硬编码在消费端里的核心 kind —— 任何一本书的 kind 词表都必须是它的超集。
# 依据：incremental._get_character_names 与 get_node_index_two_tier 用
# kind='character' 取人；get_node_detail 的引文分支判 kind=='character'；
# 前端 graph-viz.js 的 KIND_COLORS 以这几个键取色。改名或去掉会**静默**
# 弄坏对话观察提取与角色内核，所以单列成不变量。
CORE_KINDS = frozenset({"character", "event", "location", "concept", "item", "chapter"})

#: **持久关系**的词表。与「这一场谁对谁做了什么」相对：`asks` / `gazes_at`
#: 是一幕戏里的动作，`loves` / `sibling_of` / `protects` 是跨场景成立的关系。
#: 角色卡（``format_role_kernel``）只该展示后者 —— 实测旧图里一条
#: 「朱莉叶特 --asks--> 杰罗姆」会比「阿莉莎 --loves--> 杰罗姆」更早出现在
#: 前三名，因为它出现在前面的章节里，而旧实现只按章号排序。
#:
#: 骨架趟（``bookgraph/skeleton.py``）把这份词表当**硬约束**；逐批提取器
#: 暂时是自由词，所以这里也用作**分类器**：在这里面 = 持久关系。
RELATION_KINDS = frozenset({
    "parent_of", "sibling_of", "spouse_of", "relative_of", "adopted_by",
    "friend_of", "ally_of", "rival_of", "enemy_of", "mentor_of",
    "student_of", "colleague_of", "servant_of", "patron_of",
    "loves", "hates", "desires", "fears", "trusts", "distrusts",
    "admires", "despises", "envies", "pities",
    "protects", "harms", "betrays", "deceives", "obeys", "commands",
    "rescues", "abandons", "pursues", "avoids",
    "writes_to", "speaks_with", "confides_in", "advises", "reproaches",
    "accuses", "thanks", "preaches",
    "lives_in", "visits", "travels_to", "born_in", "dies_in", "marries",
    "elopes_with", "cares_for", "authored", "studies_at", "works_at",
    "serves_in", "owns", "gives", "plays", "names_after",
    # 旧图里常见的写法（不加 _of 的亲缘词），一并认作持久关系。
    "loves", "cousin_of", "sister_of", "brother_of", "father_of",
    "mother_of", "son_of", "daughter_of", "husband_of", "wife_of",
    "married_to", "cares_for", "depends_on", "rivals", "mentors",
})


def is_durable_relation(kind: str) -> bool:
    return (kind or "").strip().lower() in RELATION_KINDS


#: 身份说明（`nodes.description`）里**不许出现**的词。
#:
#: 这句话会**无条件**进角色卡（``format_role_kernel`` 不按章过滤），所以
#: 写下「最终在疗养院病逝」等于在第一章摊开结局。提示词已经要求只写
#: 「一出场就成立」的事，模型仍会漏 —— 这里是机械兜底，数出来别让它静默
#: 过去。**词表是启发式，不是判据**：它的作用是把可疑条目捞出来给人看，
#: 不是替人决定删不删。
FINALITY_WORDS = (
    "去世", "病逝", "死去", "死亡", "死了", "逝世", "离世", "过世",
    "亡故", "已故", "殒命", "丧生", "牺牲", "殉", "安葬", "下葬",
    "临终", "临死", "死后", "遗孀", "寡妇", "嫁给", "娶了",
    "最终", "后来", "最后",
)


def finality_hit(text: str) -> str:
    """返回命中的第一个「结局词」，没命中返回空串。"""
    for word in FINALITY_WORDS:
        if word in (text or ""):
            return word
    return ""

# 「kind 漂移」的形状：核心词后面挂括号/斜杠补充说明，如
# ``character (archetypal)``、``character (historical context)``。
# 实测这是同一实体裂成多节点的主因之一。
_KIND_VARIANT_RE = re.compile(r"^([a-z_]+)\s*[(（/·\-–—]")

_WS_RE = re.compile(r"\s+")
# 引文里的省略号是**摘录标记**，不是原文。它把一句话拆成几段，逐字比对整串
# 必然失败 —— 实测这类引文会被判成「全书找不到」，然后进回炉（见 _check_quotes）。
_ELLIPSIS_RE = re.compile(r"……|…|\.\.\.")


def _quote_parts(text: str) -> list[str]:
    """把带省略号的引文拆成几段（已去空白）。"""
    return [p for p in (_WS_RE.sub("", x) for x in _ELLIPSIS_RE.split(text or ""))
            if p]


def _quote_in(quote: str, haystack: str) -> bool:
    """引文是否在 haystack 里 —— 省略号段落**按顺序**分别出现即算命中。"""
    if not quote or not haystack:
        return False
    parts = _quote_parts(quote)
    if not parts:
        return False
    pos = 0
    for part in parts:
        i = haystack.find(part, pos)
        if i < 0:
            return False
        pos = i + len(part)
    return True

# 与 CORE_KINDS 语义无争议的同义词。实测（全库 9 本）kind 有 70 多种，绝大多数
# 只用过一两次 —— 其中最大的一团是 `person`(332) 与 `character`(520) 并存：
# 两者指的是同一类东西，而消费端只认 `character`。于是用了 `person` 的那本书，
# **角色优先级与角色内核静默失效**。
#
# 只收「目标明确在 CORE_KINDS 内、且源词与它指的是同一类东西」的映射。
# 拿不准的一律不收 —— 留在词表外，由调用方报出来（`organization`(353)、
# `treatment`(114) 这些就不收：硬塞进 concept 是把信息改坏，不是归一）。
KIND_SYNONYMS: dict[str, str] = {
    # 中文 kind：实测《道德经》有 ``事件``(4) 与 ``文献``(1)。只收前者 ——
    # 「屈原被放逐事件」「老子出走事件」就是 event，没有第二种读法；
    # ``文献``（吕氏春秋）到底该进 item 还是 book 拿不准，按上面的规矩留在
    # 词表外报出来。
    "事件": "event",
    "person": "character", "people": "character", "human": "character",
    "historical_figure": "character", "mythological_figure": "character",
    "biblical_figure": "character", "fictional_character": "character",
    "deity": "character", "philosopher": "character", "author": "character",
    "place": "location",
    "object": "item", "artifact": "item", "artwork": "item", "tool": "item",
    "theme": "concept", "symbol": "concept",
}

OFF_VOCAB = "off_vocab"


def normalize_kind(kind: str, vocabulary: set[str] | None = None) -> tuple[str, str]:
    """把提取器给的 kind 归一。返回 ``(kind, 说明)``，说明为空 = 原样通过。

    三步，全是**确定性**的：
    1. 变体剥离 —— ``character (archetypal)`` → ``character``（实测 106 个）
    2. 同义词归并 —— ``person`` → ``character``
    3. 词表外 —— **原样保留**，返回 ``OFF_VOCAB`` 让调用方记进缺陷清单

    第 3 步刻意不兜底：把不认识的 kind 硬塞成某个值，等于把「这是什么东西」
    这个信息改坏。词表外是要**报出来**的，不是要**抹掉**的。
    """
    k = (kind or "").strip()
    if not k:
        return "unknown", OFF_VOCAB
    k = _WS_RE.sub(" ", k)
    if k in CORE_KINDS:
        return k, ""

    m = _KIND_VARIANT_RE.match(k)
    if m and m.group(1) in CORE_KINDS:
        return m.group(1), ""

    syn = KIND_SYNONYMS.get(k.lower())
    if syn:
        return syn, ""

    if vocabulary and k in vocabulary:
        return k, ""
    return k, OFF_VOCAB


@dataclass
class Finding:
    """一条机械发现。``question`` 是回给 agent 的原话。

    ``count`` 是**涉及的条目数**（几个节点 / 几条边），不是发现条数 ——
    一条发现可以概括一百个孤儿节点，汇总时要按条目数算。
    """

    code: str
    question: str
    detail: str = ""
    count: int = 1
    #: ``False`` = **提示项**：机械层已经接住了，不单独触发回炉。
    #: 回炉是整个系统里最贵的一步（一次完整提取 + 一次证伪），不该为
    #: 「写库时就会被归一掉的东西」花掉。提示项仍然进反馈文本 —— 真因为
    #: 别的原因回炉时，提取器照样学得到。
    blocking: bool = True

    def render(self) -> str:
        return f"[{self.code}] {self.question}" + (f"（{self.detail}）" if self.detail else "")


@dataclass
class Report:
    """一次体检的结果。``skipped`` 明说哪几项没查成 —— 沉默不是成功。"""

    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.findings

    def by_code(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.code] = out.get(f.code, 0) + 1
        return out


def check_graph(graph: dict, *, admitted: set[str] | None = None,
                chunk_text: dict[str, str] | None = None,
                book_text: str | None = None) -> Report:
    """体检一份图谱，返回**问题清单**（空 = 没发现问题）。

    *graph* 是 ``{"nodes", "edges", "observations", "quotes", "chunks", "aliases"}``
    几份行的字典（行可以是 sqlite3.Row、dict 或对象，本模块只按列名取值）。

    *chunk_text* 是 ``{chunk_id: 正文}``，*book_text* 是全书正文。缺了它们，
    引文那两项查不了 —— 那时会记进 ``skipped``，而不是假装通过。
    """
    rep = Report()
    nodes = _rows(graph.get("nodes"))
    edges = _rows(graph.get("edges"))
    obs = _rows(graph.get("observations"))
    quotes = _rows(graph.get("quotes"))
    chunks = _rows(graph.get("chunks"))
    aliases = _rows(graph.get("aliases"))

    # 章节节点不是提取产物，不参与体检（它是 init_chapter_nodes 造的骨架）。
    entities = [n for n in nodes if _s(n, "kind") != "chapter"]
    rep.counts = {"nodes": len(entities), "edges": len(edges),
                  "observations": len(obs), "quotes": len(quotes),
                  "chunks": len(chunks)}

    chunk_ids = {_s(c, "chunk_id") for c in chunks}
    rep.findings += _check_dangling(edges, obs, quotes, chunk_ids)
    rep.findings += _check_orphans(entities, edges, obs, quotes, aliases, admitted)
    rep.findings += _check_kinds(entities)
    rep.findings += _check_claims(edges, obs, chunk_text)
    rep.findings += _check_quote_anchors(quotes, chunk_text)
    rep.findings += _check_unresolved(graph.get("unresolved"))
    if chunk_text:
        found, qstats = _check_quotes(quotes, chunk_text, _norm_ws(book_text))
        rep.findings += found
        rep.counts.update(qstats)
        if not book_text:
            rep.skipped.append(
                "quote 的「书里别处有吗」没查（没给 book_text，"
                "锚错块与全书找不到分不开）")
    else:
        rep.skipped.append("quote 核对（没给 chunk_text，无法核对逐字）")
    return rep


def _norm_ws(text: str | None) -> str:
    return _WS_RE.sub("", text) if text else ""


def referenced_names(result: dict) -> set[str]:
    """一批提取结果里**被别人引用过**的所有名字。

    准入集合刻意不只是「边的端点」—— 只认边会让只有观察的角色、只有台词的
    角色、别名指向的实体全部消失，而 ``format_role_kernel`` 与 ``get_node_detail``
    正是按这些入口取角色的。
    """
    refs: set[str] = set()

    def _add(v) -> None:
        s = (v or "").strip() if isinstance(v, str) else ""
        if s:
            refs.add(s)

    for ed in result.get("new_edges") or []:
        _add(ed.get("source"))
        _add(ed.get("target"))
    for ob in result.get("new_observations") or []:
        _add(ob.get("node_name"))
    for al in result.get("new_aliases") or []:
        _add(al.get("canonical_name"))
    for q in result.get("quotes") or []:
        _add(q.get("character_name"))
        for r in (q.get("related_nodes") or []):
            _add(r)
    return refs


def unresolved_refs(result: dict, known: set[str] | None = None
                    ) -> list[dict]:
    """被引用、却既没声明也不在已有图谱里的名字 —— 它们的边会被整条丢掉。

    这是**最贵的一种缺陷**：不是少一个字段，是少一整条关系。实测《我与地坛》
    某一批 28 条边只落地 3 条，其余全折在 7 个这样的名字上（我 / 母亲 / 地坛
    / 老夫老妻 …）—— 提取器按提示词「索引里的实体不用重复声明」行事，而库里
    根本没有它们，于是谁也不建节点。

    所以它必须进缺陷清单回炉，而不是静静躺在 ``graph_unresolved`` 里。
    """
    declared = {(n.get("canonical_name") or "").strip()
                for n in (result.get("new_nodes") or [])}
    declared.discard("")
    known = known or set()
    missing = sorted(referenced_names(result) - declared - known)
    return [{"reference_name": n, "reference_kind": "candidate"} for n in missing]


def _check_unresolved(rows) -> list[Finding]:
    """解析不到的引用 —— 它们是**已知的缺口**，不该只躺在日志里。

    这条不是「错误」，是「知情」：图谱少了一条边，而我们知道少了哪一条。
    """
    rows = _rows(rows)
    if not rows:
        return []
    by_kind: dict[str, int] = {}
    for r in rows:
        k = _s(r, "reference_kind") or "?"
        by_kind[k] = by_kind.get(k, 0) + 1
    names = [_s(r, "reference_name") for r in rows]
    return [Finding(
        "unresolved_reference",
        f"{len(rows)} 个引用没解析到节点 —— 它们所在的边/观察/别名整条没有落库。"
        "把没声明的名字补进 new_nodes（或改成已有的叫法），否则这条关系不存在。",
        detail="、".join(f"{k}×{v}" for k, v in sorted(by_kind.items()))
               + "；样本 " + "、".join(names[:8]),
        count=len(rows))]


# ── 取值助手：sqlite3.Row / dict / 对象都能用 ─────────────────────────────

def _rows(raw) -> list:
    return list(raw) if raw else []


def _s(row, key: str) -> str:
    """取字符串列。行可以是 Row / dict / 对象；缺列得空串。"""
    try:
        if isinstance(row, dict):
            v = row.get(key)
        elif hasattr(row, "keys"):
            v = row[key] if key in row.keys() else None
        else:
            v = getattr(row, key, None)
    except Exception:
        v = None
    return "" if v is None else str(v)


# ── 各项检查 ──────────────────────────────────────────────────────────────

def _check_dangling(edges, obs, quotes, chunk_ids: set[str]) -> list[Finding]:
    """锚点指不到任何块 —— 断言就没有出处，翻书复核也做不到。"""
    out: list[Finding] = []
    for table, rows, col in (("边", edges, "chunk_id"),
                             ("观察", obs, "evidence"),
                             ("引文", quotes, "chunk_id")):
        bad = [r for r in rows if _s(r, col) and _s(r, col) not in chunk_ids]
        if bad:
            sample = sorted({_s(r, col) for r in bad})[:3]
            out.append(Finding(
                "dangling_anchor",
                f"{len(bad)} 条{table}的出处指向了不存在的块。",
                detail=f"样本 {sample}（共 {len({_s(r, col) for r in bad})} 个不同的块 id）",
                count=len(bad)))
    return out


def _check_orphans(entities, edges, obs, quotes, aliases,
                   admitted: set[str] | None = None) -> list[Finding]:
    """无边的节点 —— 它在这本书里没做过任何事。

    这正是第 2 课的机械形式：实体因做事而存在。被观察、被引文、被别名引用
    的节点不算孤儿（它们以别的方式参与了）。

    *admitted* 是**无条件准入**的名字（Scout 点名的关键人物）：主角可能在一批
    里一条关系都没挂上（他只是在场），那不代表他不是实体。
    """
    touched: set[str] = set(admitted or ())
    for e in edges:
        touched.add(_s(e, "source_id"))
        touched.add(_s(e, "target_id"))
    for o in obs:
        touched.add(_s(o, "node_id"))
    for a in aliases:
        touched.add(_s(a, "node_id"))
    quote_chars = {_s(q, "character_name") for q in quotes}

    orphans = [n for n in entities
               if _s(n, "id") not in touched
               and _s(n, "canonical_name") not in touched
               and _s(n, "canonical_name") not in quote_chars]
    if not orphans:
        return []
    names = [_s(n, "canonical_name") for n in orphans]
    return [Finding(
        "orphan_node",
        f"{len(orphans)} 个节点没有任何关系、观察、别名或引文 —— "
        f"它们凭什么算这本书的实体？",
        detail="样本 " + "、".join(names[:12]), count=len(orphans),
        # **提示项。** 落库层按构造丢弃孤儿（只给被引用的名字建节点），
        # 这个发现说的是「已经处理完了的事」。它曾经是阻塞项，后果是每一批
        # 都因为「声明了一个没人引用的地点」被判为未经验证 —— 沉默不是成功，
        # 但「假警报」也不是。真要有人管，看 defects.orphan_dropped。
        blocking=False)]


def _check_kinds(entities) -> list[Finding]:
    """同名多 kind + kind 漂移。名字是身份，kind 只是属性。

    **这三条都是提示项，不触发回炉** —— 机械层已经接住了：同名跨 kind 在
    ``upsert_node`` 里按名字复用，变体与同义词在写库前就被 ``normalize_kind``
    归一。实测 ``kind_drift`` 占全部缺陷的 59%，而落库影响为 0（库里 0 个
    变体）。为「写库时就会被改掉的东西」烧一整轮重提取，是纯浪费。
    """
    out: list[Finding] = []
    by_name: dict[str, set[str]] = {}
    for n in entities:
        by_name.setdefault(_s(n, "canonical_name"), set()).add(_s(n, "kind"))

    dup = {k: v for k, v in by_name.items() if len(v) > 1}
    if dup:
        sample = "、".join(
            f"{k}({'/'.join(sorted(v))})" for k, v in list(dup.items())[:6])
        out.append(Finding(
            "same_name_multi_kind",
            f"{len(dup)} 个名字被拆成了多个节点 —— 同名即同实体，"
            f"kind 不该参与身份判定。",
            detail=f"样本 {sample}", count=len(dup), blocking=False))

    # kind 漂移用**归一器**判，而不是本地再写一遍规则 —— 归一器知道的
    # （变体剥离 + 同义词归并）就是「这个词其实该叫什么」的唯一定义。
    drift: dict[str, str] = {}
    for n in entities:
        k = _s(n, "kind")
        fixed, note = normalize_kind(k)
        if fixed != k and note != OFF_VOCAB:
            drift[k] = fixed
    if drift:
        sample = "、".join(f"{k}→{v}" for k, v in sorted(drift.items())[:6])
        out.append(Finding(
            "kind_drift",
            f"{len(drift)} 种 kind 其实该归一到核心词表里。"
            f"「character (archetypal)」不是一种 kind，是对角色的一种看法；"
            f"「person」和「character」指的是同一类东西，而消费端只认后者。",
            detail=f"{sample}；核心词表 {sorted(CORE_KINDS)}", count=len(drift),
            blocking=False))
    return out


def _check_claims(edges, obs, chunk_text: dict[str, str] | None = None) -> list[Finding]:
    """没有出处的断言 —— 写不出原文的，就是编的。

    三层，从粗到细：
    ① 连**锚点**都没有 → 翻书复核无从下手
    ② 有锚点、没有**逐字原文** → 「这句是真的吗」在库层面问不出来
    ③ 有逐字原文、但在它自己声称的那一块里**找不到** → 编的
       （需要 chunk_text；没给就记 skipped，不假装通过）
    """
    out: list[Finding] = []
    no_anchor = [e for e in edges if not _s(e, "chunk_id")]
    if no_anchor:
        out.append(Finding(
            "claim_without_evidence",
            f"{len(no_anchor)} 条关系没有 chunk 出处。",
            detail="、".join(f"{_s(e, 'source_id')[:8]}→{_s(e, 'target_id')[:8]}"
                            f"({_s(e, 'kind')})" for e in no_anchor[:5]),
            count=len(no_anchor)))
    no_ev = [o for o in obs if not _s(o, "evidence")]
    if no_ev:
        out.append(Finding(
            "claim_without_evidence",
            f"{len(no_ev)} 条观察没有 evidence 锚点。",
            detail="、".join(f"{_s(o, 'field')}={_s(o, 'value')[:12]}"
                            for o in no_ev[:5]), count=len(no_ev)))

    for label, rows, anchor in (("关系", edges, "chunk_id"), ("观察", obs, "evidence")):
        no_text = [r for r in rows if _s(r, anchor) and not _s(r, "evidence_text")]
        if no_text:
            out.append(Finding(
                "claim_without_verbatim",
                f"{len(no_text)} 条{label}有锚点、没有**逐字原文** —— "
                f"锚点只说「在哪」，说不出「凭什么」。",
                count=len(no_text)))

    if chunk_text:
        fabricated: list[str] = []
        for label, rows, anchor in (("关系", edges, "chunk_id"),
                                    ("观察", obs, "evidence")):
            for r in rows:
                ev, cid = _s(r, "evidence_text"), _s(r, anchor)
                if not ev or cid not in chunk_text:
                    continue
                if _norm_ws(ev) not in _norm_ws(chunk_text[cid]):
                    fabricated.append(f"{label}:{ev[:16]}")
        if fabricated:
            out.append(Finding(
                "fabricated_evidence",
                f"{len(fabricated)} 条断言声称的逐字原文，在它自己锚定的那一块里"
                f"找不到。",
                detail="样本 " + "、".join(fabricated[:5]), count=len(fabricated)))
    return out


def _check_quote_anchors(quotes, chunk_text: dict[str, str] | None = None
                         ) -> list[Finding]:
    """一批里所有引文共用同一个 chunk —— 锚点是批首块，不是引文真正所在。

    批是阅读顺序上连续的，批首章号 ≤ 批内任何块 → 引文被归到**更早**的章，
    于是 ``max_chapter`` 的剧透过滤会放行后面章节的台词。这是**泄漏**。
    """
    by_batch: dict[str, list] = {}
    for q in quotes:
        by_batch.setdefault(_s(q, "batch_id"), []).append(q)
    bad = []
    for batch, qs in by_batch.items():
        anchors = {_s(q, "chunk_id") for q in qs}
        if len(qs) <= 1 or len(anchors) != 1:
            continue
        # 「多引文共用一个锚点」只是**代理判据**。一段密集对话天然会让多条
        # 引文落在同一块 —— 那是对的。真正说明锚点是假的，是**至少有一条
        # 引文并不真的在它锚定的那一块里**。没给 chunk_text 时退回代理判据
        # （宁可多报，不静默放过）。
        if chunk_text:
            body = _norm_ws(chunk_text.get(next(iter(anchors))))
            texts = [_norm_ws(_s(q, "quote_text")) for q in qs]
            texts = [t for t in texts if t]
            if texts and all(t in body for t in texts):
                continue
        bad.append((batch, len(qs)))
    if not bad:
        return []
    return [Finding(
        "quote_shared_anchor",
        f"{len(bad)} 个批次里，所有引文共用同一个 chunk 锚点 —— "
        f"说明引文没有记下自己真正在哪一块，后面章节的台词会被提前展示。",
        detail="、".join(f"{b[:18]}({n}条)" for b, n in bad[:5]),
        count=sum(n for _, n in bad))]


def _check_quotes(quotes, chunk_text: dict[str, str],
                  book_text: str) -> tuple[list[Finding], int]:
    """引文三分：**锚对了** / **锚错了块** / **全书找不到**。

    这两类必须分开，否则结论会误导：
    - 锚错了块 —— 引文是真的，只是没记下自己真正在哪一块。批首锚点 bug 让它
      成了常态（实测 940/1165）。后果是**剧透泄漏**：批首章号 ≤ 批内任何块，
      于是后面章节的台词会被 ``max_chapter`` 放行。
    - 全书找不到 —— 可能是省略号截断（``«甲……乙»``）、轻度改写，或编造。
      机械判不了，交给对抗趟，**不在这里定罪**。
    """
    mis: list[tuple[str, str]] = []
    unfound: list[tuple[str, str]] = []
    checked = ok = 0
    for q in quotes:
        text = _WS_RE.sub("", _s(q, "quote_text"))
        if not text:
            continue
        anchored = chunk_text.get(_s(q, "chunk_id"))
        if anchored is not None:
            checked += 1
            if _quote_in(text, _WS_RE.sub("", anchored)):
                ok += 1
                continue
        pair = (_s(q, "character_name"), _s(q, "quote_text")[:28])
        if book_text and _quote_in(text, book_text):
            mis.append(pair)
        else:
            unfound.append(pair)

    out: list[Finding] = []
    if mis:
        out.append(Finding(
            "quote_misanchored",
            f"{len(mis)} 条引文不在它所锚的块里，但书里别处找得到 —— "
            f"锚点记错了块。这会让后面章节的台词提前露出。",
            detail="、".join(f"{c}«{t}»" for c, t in mis[:5]), count=len(mis)))
    if unfound:
        out.append(Finding(
            "quote_unverifiable",
            f"{len(unfound)} 条引文全书都找不到（可能是省略号截断或改写，也可能是编的）"
            f"—— 机械判不了，需要裁决趟逐条看。",
            detail="、".join(f"{c}«{t}»" for c, t in unfound[:5]),
            count=len(unfound),
            # **提示项，不阻塞。** 它曾经是阻塞项，后果是每一批都带着一条
            # 永远消不掉的缺陷进回炉 —— 旧回路跑满三轮的直接原因之一。
            # 现在裁决趟已经逐条看过引文；它没删的，就是它认可过的。
            blocking=False))
    return out, {"checked": checked, "anchored_ok": ok}
