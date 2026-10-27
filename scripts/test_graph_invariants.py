"""``bookgraph.invariants`` 的单元测试（纯函数，零 LLM、零 IO）。

跑法：``python scripts/test_graph_invariants.py``（退出码 0 = 全绿），
已接进 ``scripts/check_all.py`` 的 MODULE_TESTS。

每个用例都从**一份干净的图谱**出发，只破坏一处 —— 这样断言能精确指向
是哪条检查在报，而不是「反正是坏的」。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph.invariants import check_graph, Finding  # noqa: E402


def _clean_graph() -> dict:
    """一份没有问题的图谱：两个角色、一条有出处的边、一条逐字引文。"""
    return {
        "nodes": [
            {"id": "n1", "canonical_name": "甲", "kind": "character"},
            {"id": "n2", "canonical_name": "乙", "kind": "character"},
            {"id": "ch1", "canonical_name": "第一章", "kind": "chapter"},
        ],
        "edges": [
            {"source_id": "n1", "target_id": "n2", "kind": "loves", "chunk_id": "k1",
             "evidence_text": "甲说你好"},
        ],
        "observations": [
            {"node_id": "n1", "field": "personality", "value": "刚强",
             "evidence": "k1", "evidence_text": "然后就走了"},
        ],
        "quotes": [
            {"character_name": "甲", "quote_text": "你好", "chunk_id": "k1",
             "batch_id": "b1"},
        ],
        "chunks": [{"chunk_id": "k1"}],
        "aliases": [],
        "unresolved": [],
    }


TEXT = {"k1": "甲说你好，然后就走了。"}
# k1 之外还有正文 —— 用来把「锚错了块」和「全书都找不到」分开
BOOK = "甲说你好，然后就走了。他后来还说了一句：明天见。"
CASES: list[tuple[str, dict, str | None]] = []


def case(name: str, mutate, chunk_text: dict | None = TEXT):
    """注册一个用例：mutate 就地破坏图谱，然后跑体检。"""
    def run() -> list[Finding]:
        g = _clean_graph()
        mutate(g)
        return check_graph(g, chunk_text=chunk_text, book_text=BOOK).findings
    CASES.append((name, run, chunk_text))


case("干净图谱不报任何问题", lambda g: None)
case("孤儿：没有任何引用", lambda g: g["nodes"].append(
    {"id": "n9", "canonical_name": "孤", "kind": "concept"}))
case("被别名引用不算孤儿", lambda g: (
    g["nodes"].append({"id": "n9", "canonical_name": "孤", "kind": "concept"}),
    g["aliases"].append({"node_id": "n9", "alias_name": "阿孤"})))
case("被引文引用不算孤儿", lambda g: (
    g["nodes"].append({"id": "n9", "canonical_name": "丙", "kind": "character"}),
    g["quotes"].append({"character_name": "丙", "quote_text": "你好",
                        "chunk_id": "k1", "batch_id": "b2"})))
# 改既有节点的名字+kind（而不是新增一个没连边的），才能只破坏一处：
# n2 仍连着边，所以不会同时报 orphan
case("同名多 kind", lambda g: g["nodes"][1].update(
    canonical_name="甲", kind="location"))
case("kind 漂移（核心词挂括号）", lambda g: g["nodes"][1].update(
    kind="character (archetypal)"))
case("kind 漂移（同义词 person→character）", lambda g: g["nodes"][1].update(
    kind="person"))
case("悬空锚点", lambda g: g["edges"].append(
    {"source_id": "n1", "target_id": "n2", "kind": "x", "chunk_id": "MISSING",
     "evidence_text": "某句原文"}))
case("边无出处", lambda g: g["edges"].append(
    {"source_id": "n1", "target_id": "n2", "kind": "x", "chunk_id": ""}))
case("观察无 evidence", lambda g: g["observations"].append(
    {"node_id": "n1", "field": "motivation", "value": "?", "evidence": ""}))
case("有锚点、没有逐字原文", lambda g: g["edges"].append(
    {"source_id": "n1", "target_id": "n2", "kind": "x", "chunk_id": "k1"}))
case("逐字原文在自己锚定的块里找不到（编的）", lambda g: g["edges"].append(
    {"source_id": "n1", "target_id": "n2", "kind": "x", "chunk_id": "k1",
     "evidence_text": "我从来没说过这句话"}))
case("逐字原文跨行也能对上（空白归一）", lambda g: g["edges"].append(
    {"source_id": "n1", "target_id": "n2", "kind": "x", "chunk_id": "k1",
     "evidence_text": "甲说\n你好"}))
# 共用锚点只在**至少有一条并不真的在那块里**时才算锚点造假。密集对话段
# 天然多条引文同块 —— 那是对的（实测《秋天的怀念》843 字一块里 5 条台词）。
case("引文共用批锚点（引文并不在那块里）", lambda g: g["quotes"].append(
    {"character_name": "甲", "quote_text": "明天见", "chunk_id": "k1",
     "batch_id": "b1"}))
case("密集对话段多条引文同块（真的都在）", lambda g: g["quotes"].append(
    {"character_name": "乙", "quote_text": "然后就走了", "chunk_id": "k1",
     "batch_id": "b1"}))
case("有未解析引用（知情，不是错误）", lambda g: g["unresolved"].append(
    {"reference_name": "丙", "reference_kind": "edge"}))
case("引文锚错了块（书里别处有）", lambda g: g["quotes"].append(
    {"character_name": "甲", "quote_text": "明天见", "chunk_id": "k1",
     "batch_id": "b2"}))
case("引文全书都找不到", lambda g: g["quotes"].append(
    {"character_name": "甲", "quote_text": "我从没说过这句", "chunk_id": "k1",
     "batch_id": "b2"}))
case("没给 chunk_text 时明说跳过（不假装通过）",
     lambda g: g["quotes"].append(
         {"character_name": "甲", "quote_text": "编的", "chunk_id": "k1",
          "batch_id": "b2"}),
     chunk_text=None)


# 每个用例期望**恰好**出现这些 code（顺序无关，多重集比较）。
EXPECT: dict[str, list[str]] = {
    "干净图谱不报任何问题": [],
    "孤儿：没有任何引用": ["orphan_node"],
    "被别名引用不算孤儿": [],
    "被引文引用不算孤儿": [],
    "同名多 kind": ["same_name_multi_kind"],
    "kind 漂移（核心词挂括号）": ["kind_drift"],
    "kind 漂移（同义词 person→character）": ["kind_drift"],
    "悬空锚点": ["dangling_anchor"],
    "边无出处": ["claim_without_evidence"],
    "观察无 evidence": ["claim_without_evidence"],
    "有锚点、没有逐字原文": ["claim_without_verbatim"],
    "逐字原文在自己锚定的块里找不到（编的）": ["fabricated_evidence"],
    "逐字原文跨行也能对上（空白归一）": [],
    "有未解析引用（知情，不是错误）": ["unresolved_reference"],
    # 两条一起报是对的：锚点既「整批共用」又「锚错了块」
    "引文共用批锚点（引文并不在那块里）":
        ["quote_shared_anchor", "quote_misanchored"],
    "密集对话段多条引文同块（真的都在）": [],
    "引文锚错了块（书里别处有）": ["quote_misanchored"],
    "引文全书都找不到": ["quote_unverifiable"],
    "没给 chunk_text 时明说跳过（不假装通过）": [],
}


def _test_dangling_refs() -> int:
    """悬空引用 —— 少一条边是这套系统里最贵的缺陷。

    实测《我与地坛》某一批 28 条边只落地 3 条：提取器按提示词「索引里的实体
    不用重复声明」行事，而库里根本没有那 7 个名字（我/母亲/地坛/…），准入闸
    又不给没声明的名字建节点 —— 于是谁也不建，边整条消失，只在
    ``graph_unresolved`` 里留一行没人看的记录。
    """
    from bookgraph.verify import check_candidate
    failed = 0
    result = {
        "new_nodes": [{"canonical_name": "小姑娘", "kind": "character"}],
        "new_edges": [
            {"source": "我", "target": "小姑娘", "kind": "meets",
             "chunk_id": "k1", "evidence": "我遇见她"},
            {"source": "小姑娘", "target": "园神", "kind": "worships",
             "chunk_id": "k1", "evidence": "她信园神"},
        ],
        "new_observations": [], "new_aliases": [], "quotes": [],
        "merge_candidates": [], "summary": "",
    }
    txt = {"k1": "我遇见她。她信园神。"}

    def codes(known):
        return sorted(f.code for f in
                      check_candidate(result, ["k1"], txt,
                                      known=known).findings)

    # 「我」「园神」都被引用却没声明 —— 两条边都保不住
    if "unresolved_reference" not in codes(set()):
        print("FAIL 悬空引用没被报出来")
        failed += 1
    else:
        print("ok   悬空引用报成缺陷（不再静默丢边）")

    # 「我」在库里已经有了 → 正常引用旧实体，只剩「园神」悬空
    got = codes({"我"})
    if "unresolved_reference" not in got:
        print("FAIL 库里没有的名字没报")
        failed += 1
    else:
        print("ok   库里已有的名字不算悬空（不误报旧实体引用）")

    r = check_candidate(result, ["k1"], txt, known={"我"})
    f = [x for x in r.findings if x.code == "unresolved_reference"][0]
    if "园神" not in f.detail or "我" in f.detail:
        print(f"FAIL 悬空清单不准：{f.detail}")
        failed += 1
    else:
        print("ok   悬空清单只列真悬空的那个")
    return failed


def main() -> int:
    failed = 0
    for name, run, _ct in CASES:
        want = sorted(EXPECT[name])
        try:
            got = sorted(f.code for f in run())
        except Exception as exc:                       # 体检器自己不该抛
            print(f"FAIL {name}\n     抛异常: {exc!r}")
            failed += 1
            continue
        if got != want:
            print(f"FAIL {name}\n     期望 {want}\n     实得 {got}")
            failed += 1
        else:
            print(f"ok   {name}")

    # 跳过要明说，不能静默
    r = check_graph(_clean_graph())
    if r.skipped != ["quote 核对（没给 chunk_text，无法核对逐字）"]:
        print(f"FAIL skipped 契约：{r.skipped}")
        failed += 1
    else:
        print("ok   skipped 明说")

    # sqlite3.Row 也要能取值（调用方从库里直接拿行）
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE nodes(id TEXT, canonical_name TEXT, kind TEXT)")
    conn.execute("INSERT INTO nodes VALUES('n1','甲','character')")
    rows = conn.execute("SELECT * FROM nodes").fetchall()
    r = check_graph({"nodes": rows})
    if [f.code for f in r.findings] != ["orphan_node"]:
        print(f"FAIL sqlite3.Row 取值：{[f.code for f in r.findings]}")
        failed += 1
    else:
        print("ok   sqlite3.Row 取值")

    failed += _test_dangling_refs()
    failed += _test_new_helpers()

    print(f"\n{'FAILED ' + str(failed) if failed else 'ALL PASS'}  "
          f"({len(CASES) + 2} 项)")
    return 1 if failed else 0


def _test_new_helpers() -> int:
    """省略号引文 + 持久关系分类 —— 两条都是「静默失效」过的东西。"""
    from bookgraph.invariants import _quote_in, is_durable_relation
    bad = 0

    # 引文里的省略号是摘录标记：前后两段都在正文里出现即算命中。
    body = "我早就没了这种心态，也憎恶它，想消除它以简化我的灵魂"
    cases = [
        ("省略号引文命中", _quote_in("我早就没了这种心态……想消除它", body), True),
        ("省略号引文，后段不在正文", _quote_in("我早就没了这种心态……并不存在的句子", body), False),
        ("顺序反了不算命中", _quote_in("想消除它……我早就没了这种心态", body), False),
        # 持久关系 vs 一幕戏的动作
        ("loves 是持久关系", is_durable_relation("loves"), True),
        ("parent_of 是持久关系", is_durable_relation("parent_of"), True),
        ("asks 不是持久关系", is_durable_relation("asks"), False),
        ("gazes_at 不是持久关系", is_durable_relation("gazes_at"), False),
    ]
    for label, got, want in cases:
        if got != want:
            print(f"FAIL {label}: 期望 {want!r} 实得 {got!r}")
            bad += 1
        else:
            print(f"ok   {label}")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
