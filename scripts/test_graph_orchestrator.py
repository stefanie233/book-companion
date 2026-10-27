"""``GraphOrchestrator`` 的集成测试 —— 不联网、零 LLM、零 token。

做法：把 orchestrator 模块里的四个外部调用（context_setter / head_scan /
scout / explorer）换成桩，其余全走真代码（真 GraphDB、真批次构造、真落库）。
这样能断言的是**接线**，不是逻辑 —— 逻辑由各纯函数的单测覆盖。

盯的是 P2a 那一刀：**被归属分类裁掉的块，绝不能出现在送给提取器的批次里**。
以前只有 Scout 的排除生效，而它没产出计划时连它自己也静默失效。

跑法：``python scripts/test_graph_orchestrator.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bookgraph.orchestrator as O                       # noqa: E402
from storage.graph_db import GraphDB                     # noqa: E402

BOOK_ID = "sb-test"


class Ch:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _chapters() -> list[Ch]:
    """ch0 版权页 / ch1 目录 / ch2 正文两块 / ch3 后记。"""
    return [
        Ch(chapter_index=0, title="版权页", chapter_type="copyright", content="x"),
        Ch(chapter_index=1, title="目录", chapter_type="toc", content="x"),
        Ch(chapter_index=2, title="第一章", chapter_type="body", content="x"),
        Ch(chapter_index=3, title="后记", chapter_type="postscript", content="x"),
    ]


def _chunks() -> list[Ch]:
    return [
        Ch(id="k0", chapter_index=0, chunk_index=0, content="版权信息正文",
           paragraph_sigs="[]", paragraph_offsets="[]"),
        Ch(id="k1", chapter_index=1, chunk_index=0, content="目录正文",
           paragraph_sigs="[]", paragraph_offsets="[]"),
        Ch(id="k2", chapter_index=2, chunk_index=0, content="正文第一块",
           paragraph_sigs="[]", paragraph_offsets="[]"),
        Ch(id="k3", chapter_index=2, chunk_index=1, content="正文第二块",
           paragraph_sigs="[]", paragraph_offsets="[]"),
        Ch(id="k4", chapter_index=3, chunk_index=0, content="后记正文",
           paragraph_sigs="[]", paragraph_offsets="[]"),
    ]


def _essay_chapters() -> list[Ch]:
    """ci0 目录 / ci1 秋天的怀念 / ci2 合欢树 / ci3 我与地坛（后两块）。"""
    return [
        Ch(chapter_index=0, title="目录", chapter_type="toc", content="x",
           level=0, in_toc=1),
        Ch(chapter_index=1, title="秋天的怀念", chapter_type="body", content="x",
           level=0, in_toc=1),
        Ch(chapter_index=2, title="合　欢　树", chapter_type="body", content="x",
           level=0, in_toc=1),
        Ch(chapter_index=3, title="我与地坛", chapter_type="body", content="x",
           level=0, in_toc=1),
    ]


def _essay_chunks() -> list[Ch]:
    return [
        Ch(id="k0", chapter_index=0, chunk_index=0, content="目录正文",
           paragraph_sigs="[]", paragraph_offsets="[]"),
        Ch(id="k1", chapter_index=1, chunk_index=0, content="秋天的怀念正文",
           paragraph_sigs="[]", paragraph_offsets="[]"),
        Ch(id="k2", chapter_index=2, chunk_index=0, content="合欢树正文",
           paragraph_sigs="[]", paragraph_offsets="[]"),
        Ch(id="k3", chapter_index=3, chunk_index=0, content="我与地坛正文一",
           paragraph_sigs="[]", paragraph_offsets="[]"),
        Ch(id="k4", chapter_index=3, chunk_index=1, content="我与地坛正文二",
           paragraph_sigs="[]", paragraph_offsets="[]"),
    ]


# 期望的域划分（由 bookgraph.scope 决定，这里写死以便断言接线是否正确）
ESSAY_SCOPE = {"k1": "s0", "k2": "s1", "k3": "s2", "k4": "s2"}


class FakeBooksDB:
    def __init__(self, chapters=None, chunks=None):
        self._ch = chapters if chapters is not None else _chapters()
        self._ck = chunks if chunks is not None else _chunks()

    def get_book(self, book_id):
        return Ch(id=book_id, title="测试书", author="某人")

    def get_chapters(self, book_id):
        return self._ch

    def get_chunks(self, book_id):
        return self._ck

    def list_text_forms(self, book_id):
        return []


class FakeConfig:
    api_key = "k"
    api_base = "http://127.0.0.1:1/v1"
    model = "fake"
    project_dir = "."


SEEN: list[dict] = []
CONTEXT: dict = {"genre": "小说", "world_view_boundary": "x"}


async def _fake_context_setter(*a, **k):
    return dict(CONTEXT)


async def _fake_head_scan(*a, **k):
    return {"verdict": "succeed", "scanned": 0, "calls": 0,
            "boundaries": {}, "identity": [], "promo_chunks": []}


async def _fake_scout(*a, **k):
    return None


async def _fake_explorer(session, cfg, db, book_id, title, author,
                         chunks, total, done, prompt, log_cb=None,
                         scope_brief="", feedback=None):
    SEEN.append({"chunks": [c["chunk_id"] for c in chunks],
                 "brief": scope_brief,
                 "feedback": list(feedback or [])})
    if EXPLORER_SCRIPT:
        return EXPLORER_SCRIPT(len(SEEN)), None
    return {"new_nodes": [], "new_edges": [], "new_observations": [],
            "new_aliases": [], "merge_candidates": [], "quotes": []}, None


# 回炉环的剧本：给一个「第 n 次调用返回什么」的函数；空 = 每次都返回干净结果。
EXPLORER_SCRIPT = None
VERIFIER_SCRIPT = None
VERIFIER_CALLS: list[dict] = []


async def _fake_verifier(session, cfg, title, chunk_texts, result,
                         roster=None, log_cb=None):
    VERIFIER_CALLS.append({"chunks": [c[0] for c in chunk_texts],
                           "roster": list(roster or []),
                           "claims": result})
    return VERIFIER_SCRIPT(len(VERIFIER_CALLS)) if VERIFIER_SCRIPT else {
        "verdict": "upheld", "challenges": []}


def _defective_result() -> dict:
    """第 1 轮：一条**没有逐字原文**的边 —— 机械体检会抓它。"""
    return {"new_nodes": [{"canonical_name": "坏", "kind": "character"}],
            "new_edges": [{"source": "坏", "target": "坏", "kind": "x",
                           "chunk_id": "k2"}],
            "new_observations": [], "new_aliases": [],
            "merge_candidates": [], "quotes": []}


def _good_result() -> dict:
    """第 2 轮：补齐逐字原文。"""
    return {"new_nodes": [{"canonical_name": "好", "kind": "character"}],
            "new_edges": [{"source": "好", "target": "好", "kind": "x",
                           "chunk_id": "k2", "evidence": "正文第一块"}],
            "new_observations": [], "new_aliases": [],
            "merge_candidates": [], "quotes": []}


ADJUDICATOR_SCRIPT = None
ADJUDICATOR_CALLS: list[dict] = []


async def _fake_adjudicator(session, cfg, title, chunk_texts, result,
                            roster=None, log_cb=None, flags=None,
                            rejected=None):
    ADJUDICATOR_CALLS.append({"chunks": [c[0] for c in chunk_texts],
                              "result": result, "flags": flags or {},
                              "rejected": rejected or []})
    if ADJUDICATOR_SCRIPT:
        return ADJUDICATOR_SCRIPT(len(ADJUDICATOR_CALLS))
    return {"verdict": "upheld", "fixes": [], "summary": ""}


def _patch() -> None:
    O.run_context_setter = _fake_context_setter
    O.run_head_scan = _fake_head_scan
    O.run_scout = _fake_scout
    O.run_explorer = _fake_explorer
    O.run_verifier = _fake_verifier
    O.run_adjudicator = _fake_adjudicator


def _test_round_rank() -> int:
    """选轮规则 —— 用《我与地坛》三轮真实数据钉住。

    旧规则（机械缺陷 + 质疑数 等权相加）在这三批上**全部选错**，回炉环空转。
    """
    from bookgraph.verify import round_rank
    bad = 0

    # (机械缺陷, 质疑, 节点, 边) —— 实测数字，不是编的
    REAL = {
        "ea165408": [(1, 1, 7, 13), (0, 3, 10, 23), (2, 1, 5, 21)],
        "71a5b321": [(2, 2, 21, 26), (0, 4, 14, 17)],
        "182c45b9": [(3, 1, 16, 18), (1, 3, 22, 29)],
    }
    for bid, rounds in REAL.items():
        ranks = [round_rank(*r) for r in rounds]
        pick = ranks.index(min(ranks))
        old = [r[0] + r[1] for r in rounds]          # 旧规则：等权相加
        old_pick = old.index(min(old))
        if pick != 1:
            print(f"FAIL {bid}: 新规则选了第 {pick + 1} 轮，应当是第 2 轮")
            bad += 1
        elif old_pick == pick:
            print(f"FAIL {bid}: 旧规则也选第 2 轮，这个用例没钉住 bug")
            bad += 1
        else:
            print(f"ok   {bid}: 旧规则选第 {old_pick + 1} 轮（错），"
                  f"新规则选第 2 轮")

    # 机械缺陷（已证实）必须压过质疑数（存疑）
    a = round_rank(0, 9, 5, 5)      # 机械干净、质疑一堆
    b = round_rank(1, 0, 5, 5)      # 有 1 个机械缺陷、零质疑
    if not a < b:
        print(f"FAIL 存疑压过了证实：{a} vs {b}")
        bad += 1
    else:
        print("ok   机械缺陷（已证实）优先于证伪质疑（存疑）")

    # 同分时偏好内容多的 —— 否则「说得少」会赢，等于奖励删东西
    if not round_rank(0, 1, 20, 30) < round_rank(0, 1, 5, 5):
        print("FAIL 同分时没偏好内容更多的那一轮")
        bad += 1
    else:
        print("ok   同分时偏好内容更多的一轮（不奖励删东西）")
    return bad


def _test_boundary_shapes() -> int:
    """head_scan 的 boundaries：**形状**宽容，不是语法宽容。

    实测换模型就崩：火山引擎回 ``{"F#0-0": "toc"}``，DeepSeek 官方回
    ``[{"key": "F#0-0", "boundary": "toc"}]``。旧代码只认前者
    （``.items()`` 直接 AttributeError），而且崩在提取**之前** —— 整本书白跑。
    """
    from bookgraph.head_scan import _coerce_boundaries as coerce
    bad = 0
    want = {"F#0-0": "toc", "F#1-0": "copyright"}
    cases = {
        "字典": {"F#0-0": "toc", "F#1-0": "copyright"},
        "对象列表": [{"key": "F#0-0", "boundary": "toc"},
                     {"key": "F#1-0", "boundary": "copyright"}],
        "变体键名": [{"chunk_key": "F#0-0", "region": "toc"},
                     {"chunk_id": "F#1-0", "type": "copyright"}],
        "二元组": [["F#0-0", "toc"], ["F#1-0", "copyright"]],
    }
    for label, raw in cases.items():
        if coerce(raw) != want:
            print(f"FAIL boundaries/{label}: {coerce(raw)}")
            bad += 1
        else:
            print(f"ok   boundaries/{label}")

    # 认不出来就返回空，绝不抛 —— 体检可以退化，整本书不能白跑
    for junk in (None, "nope", 42, [1, 2], [{"key": "x"}], {"a": None}):
        try:
            coerce(junk)
        except Exception as exc:
            print(f"FAIL boundaries 垃圾输入抛异常 {junk!r}: {exc!r}")
            bad += 1
    print("ok   boundaries 垃圾输入返回空，不抛")
    return bad


def _test_evidence_flags() -> int:
    """机械体检：证据不在原文里的断言要被标出来，并送到裁决者面前。

    钉住的是真实事故：模型把「**他的确**疯狂地爱着她」抄成「她的确疯狂地
    爱着她」（那段通篇主语是「她」）。边的方向是对的，错一个代词；逐字校验
    发现了它，但当时**没有任何东西把它送给裁决者**，于是错误留在了库里。
    """
    from bookgraph.adjudicate import evidence_flags
    bad = 0

    def expect(label: str, got, want) -> None:
        nonlocal bad
        if got != want:
            print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
            bad += 1
        else:
            print(f"ok   {label}")

    chunk_text = {
        "c1": "然而，他的确疯狂地爱着她，愿意做任何事情来取悦她。",
        "c2": "她到香港后没多久就发现了这一点。",
    }
    result = {
        "new_edges": [
            # 代词抄错：关系对，证据不是原文
            {"source": "沃尔特", "target": "凯蒂", "kind": "loves",
             "chunk_id": "c1", "evidence": "她的确疯狂地爱着她"},
            # 完全正常
            {"source": "沃尔特", "target": "凯蒂", "kind": "spouse_of",
             "chunk_id": "c1", "evidence": "他的确疯狂地爱着她"},
            # 证据在别块 —— 这类机械能自己修，不该留给裁决
            {"source": "凯蒂", "target": "香港", "kind": "lives_in",
             "chunk_id": "c1", "evidence": "她到香港后没多久就发现了这一点"},
            # 与上面共用同一句证据，但端点不同
            {"source": "沃尔特", "target": "香港", "kind": "lives_in",
             "chunk_id": "c2", "evidence": "她到香港后没多久就发现了这一点"},
        ],
        "new_observations": [],
        "quotes": [
            {"character_name": "凯蒂", "quote_text": "我没说过这句话",
             "chunk_id": "c1"},
        ],
        "new_nodes": [],
        "new_aliases": [],
        "merge_candidates": [],
    }
    flags = evidence_flags(result, chunk_text)
    expect("抄错代词的边被标出", "E1" in flags, True)
    expect("正常的边不标", "E2" in flags, False)
    expect("证据在别块 → 指出正确的块", "c2" in flags.get("E3", ""), True)
    expect("共用同一句证据 → 后一条被标",
           "E3" in flags.get("E4", ""), True)
    expect("台词逐字找不到被标", "Q1" in flags, True)
    return bad


def main() -> int:
    global EXPLORER_SCRIPT, VERIFIER_SCRIPT, ADJUDICATOR_SCRIPT
    failed = _test_evidence_flags()
    _patch()
    with tempfile.TemporaryDirectory() as tmp:
        gdb = GraphDB(Path(tmp) / "graph.db")
        orch = O.GraphOrchestrator(FakeConfig(), FakeBooksDB(), gdb)
        O.run_context_setter = _fake_context_setter
        O.run_head_scan = _fake_head_scan
        O.run_scout = _fake_scout
        O.run_explorer = _fake_explorer
        O.run_verifier = _fake_verifier
        asyncio.run(orch.run(BOOK_ID, batch_size=2, run_skeleton_pass=False))
        conn = gdb.get_conn()

        def check(label, got, want):
            nonlocal failed
            if got != want:
                print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
                failed += 1
            else:
                print(f"ok   {label}")

        sent = sorted({cid for b in SEEN for cid in b["chunks"]})
        check("只把正文块送进提取", sent, ["k2", "k3"])
        check("被裁的块标成 excluded",
              sorted(r["chunk_id"] for r in conn.execute(
                  "SELECT chunk_id FROM graph_chunks "
                  "WHERE book_id=? AND extract_status='excluded'", (BOOK_ID,))),
              ["k0", "k1", "k4"])
        check("全部块都登记了（没从表里消失）",
              conn.execute("SELECT COUNT(*) FROM graph_chunks WHERE book_id=?",
                           (BOOK_ID,)).fetchone()[0], 5)
        check("裁掉的理由带体例名",
              {r["value"] for r in conn.execute(
                  "SELECT value FROM graph_meta WHERE key LIKE 'excluded_reason_%'")},
              {"chapter_type=copyright", "chapter_type=toc",
               "chapter_type=postscript"})
        gdb.close()

    # ── 场景二：散文集 —— 批次不得跨篇 ─────────────────────────────────
    # 用户最先提的病：「短篇小说集会跨章节串流，导致文本污染」。
    SEEN.clear()
    CONTEXT.clear()
    CONTEXT.update({"genre": "散文集", "narrative_structure": "collection"})
    with tempfile.TemporaryDirectory() as tmp:
        gdb = GraphDB(Path(tmp) / "graph.db")
        orch = O.GraphOrchestrator(
            FakeConfig(), FakeBooksDB(_essay_chapters(), _essay_chunks()), gdb)
        asyncio.run(orch.run(BOOK_ID, batch_size=3, run_skeleton_pass=False))
        conn = gdb.get_conn()

        check("批次不跨篇",
              all(len({ESSAY_SCOPE[c] for c in b["chunks"]}) == 1 for b in SEEN),
              True)
        check("每一篇都送到了提取器",
              sorted({ESSAY_SCOPE[c] for b in SEEN for c in b["chunks"]}),
              ["s0", "s1", "s2"])
        check("集类：域简报进 user message",
              all("只属于这一篇" in b["brief"] for b in SEEN), True)
        check("集类：实体索引**不**按域过滤（藏起来会让复现实体分裂）",
              "scope_chunk_ids" in inspect.signature(O.run_explorer).parameters,
              False)
        meta = conn.execute(
            "SELECT value FROM graph_meta WHERE key='scope_plan'").fetchone()
        check("域划分落库（用坐标，不用 chunk_id）",
              bool(meta) and '"F#3-0"' in meta["value"] and "k3" not in meta["value"],
              True)
        gdb.close()

    # ── 场景三：回炉环 —— 第 1 轮有缺陷，第 2 轮改好 ───────────────────
    SEEN.clear()
    VERIFIER_CALLS.clear()
    EXPLORER_SCRIPT = lambda n: _defective_result() if n == 1 else _good_result()
    with tempfile.TemporaryDirectory() as tmp:
        gdb = GraphDB(Path(tmp) / "graph.db")
        orch = O.GraphOrchestrator(FakeConfig(), FakeBooksDB(), gdb)
        asyncio.run(orch.run(BOOK_ID, batch_size=2, use_scout=False,
                             run_skeleton_pass=False, repair_mode="rework"))
        conn = gdb.get_conn()
        check("有缺陷 → 回炉重提一次", len(SEEN), 2)
        check("缺陷清单带回了提取器", bool(SEEN[1]["feedback"]), True)
        check("缺陷说的是逐字原文缺失",
              any("逐字原文" in f for f in SEEN[1]["feedback"]), True)
        check("证伪趟拿到了本批正文",
              VERIFIER_CALLS[0]["chunks"], ["k2", "k3"])
        names = {r["canonical_name"] for r in conn.execute(
            "SELECT canonical_name FROM nodes WHERE project_id=?", (BOOK_ID,))}
        check("落库的是改好的那一轮", ("好" in names, "坏" in names), (True, False))
        gdb.close()

    # ── 场景四：证伪趟哑了 → 未经验证，不许冒充干净 ─────────────────────
    SEEN.clear()
    VERIFIER_CALLS.clear()
    EXPLORER_SCRIPT = None
    VERIFIER_SCRIPT = lambda n: None
    EVENTS: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        gdb = GraphDB(Path(tmp) / "graph.db")
        orch = O.GraphOrchestrator(FakeConfig(), FakeBooksDB(), gdb)
        asyncio.run(orch.run(BOOK_ID, batch_size=2, use_scout=False,
                             progress_cb=EVENTS.append,
                             run_skeleton_pass=False, repair_mode="rework"))
        unverified = [e for e in EVENTS if e.get("type") == "batch_unverified"]
        check("证伪趟没结论 → 发未经验证事件", len(unverified), 1)
        check("未经验证的理由是 inconclusive",
              unverified[0]["verify"]["verdict"] if unverified else None,
              "inconclusive")
        check("未经验证的批次不重试（机械本身干净）", len(SEEN), 1)
        gdb.close()
    VERIFIER_SCRIPT = None

    # ── 场景五：裁决环 —— 只改被判错的那几条，**不重提整批** ────────────
    # 钉住三件事：提取器只被调用一次；reverse/drop 真的落到了候选上；
    # 全书都没有的别名被机械拒收（实测提取器会带来「热罗姆」这种外部写法）。
    SEEN.clear()
    ADJUDICATOR_CALLS.clear()

    def _adjudicated_extract(n: int) -> dict:
        return {
            "new_nodes": [{"canonical_name": "甲", "kind": "character"},
                          {"canonical_name": "乙", "kind": "character"}],
            "new_edges": [{"source": "甲", "target": "乙", "kind": "hates",
                           "chunk_id": "k2", "evidence": "正文第一块"}],
            "new_observations": [{"node_name": "甲", "field": "personality",
                                  "value": "暴戾", "evidence_chunk_id": "k2",
                                  "evidence": "正文第一块"}],
            "new_aliases": [{"alias": "不存在的写法", "canonical_name": "甲"},
                            {"alias": "正文", "canonical_name": "甲"}],
            "merge_candidates": [], "quotes": [],
        }

    def _adj_script(n: int) -> dict:
        return {"verdict": "challenged", "summary": "", "fixes": [
            {"id": "E1", "action": "reverse", "why": "方向反了",
             "evidence": "正文第一块"},
            {"id": "O1", "action": "drop", "why": "正文没有这个性格判断",
             "evidence": ""},
        ]}

    EXPLORER_SCRIPT = _adjudicated_extract
    ADJUDICATOR_SCRIPT = _adj_script
    with tempfile.TemporaryDirectory() as tmp:
        gdb = GraphDB(Path(tmp) / "graph.db")
        orch = O.GraphOrchestrator(FakeConfig(), FakeBooksDB(), gdb)
        asyncio.run(orch.run(BOOK_ID, batch_size=2, use_scout=False,
                             run_skeleton_pass=False))
        conn = gdb.get_conn()
        check("裁决环不重提（提取器只调一次）", len(SEEN), 1)
        check("裁决趟拿到了本批正文", ADJUDICATOR_CALLS[0]["chunks"], ["k2", "k3"])
        name_by_id = {r["id"]: r["canonical_name"] for r in conn.execute(
            "SELECT id, canonical_name FROM nodes WHERE project_id=?", (BOOK_ID,))}
        edge = conn.execute(
            "SELECT source_id, target_id FROM edges WHERE project_id=?",
            (BOOK_ID,)).fetchone()
        check("reverse 落库（乙 --hates--> 甲）",
              (name_by_id.get(edge["source_id"]),
               name_by_id.get(edge["target_id"])) if edge else None, ("乙", "甲"))
        check("drop 落库（那条观察没有写进去）",
              conn.execute("SELECT COUNT(*) FROM graph_observations "
                           "WHERE project_id=?", (BOOK_ID,)).fetchone()[0], 0)
        al = {r["alias_name"] for r in conn.execute(
            "SELECT alias_name FROM graph_aliases WHERE project_id=?", (BOOK_ID,))}
        check("全书没有的别名被机械拒收", "不存在的写法" in al, False)
        check("真出现过的别名照常保留", "正文" in al, True)
        gdb.close()
    EXPLORER_SCRIPT = None
    ADJUDICATOR_SCRIPT = None

    # ── 被拒的修改要回灌一次（第二轮裁决）─────────────────────────────
    # 实测 14 条修改里 5 条因 `empty_patch` 被拒，原因全是「只写 why、不给
    # patch」；只有一轮时裁决者永远不知道，下一批照犯。
    def _adj_reject_then_fix(n: int) -> dict:
        if n == 1:
            return {"verdict": "challenged", "summary": "", "fixes": [
                # 只给 why，不给 patch —— 系统执行不了
                {"id": "O1", "action": "rewrite", "why": "这条观察不该这么写"},
            ]}
        return {"verdict": "challenged", "summary": "", "fixes": [
            {"id": "O1", "action": "drop", "why": "拿不出新值，撤回"},
        ]}

    ADJUDICATOR_CALLS.clear()
    ADJUDICATOR_SCRIPT = _adj_reject_then_fix
    EXPLORER_SCRIPT = _adjudicated_extract
    with tempfile.TemporaryDirectory() as tmp:
        gdb = GraphDB(Path(tmp) / "graph.db")
        orch = O.GraphOrchestrator(FakeConfig(), FakeBooksDB(), gdb)
        asyncio.run(orch.run(BOOK_ID, batch_size=2, use_scout=False,
                             run_skeleton_pass=False))
        conn = gdb.get_conn()
        check("被拒一次 → 裁决者被再问一次", len(ADJUDICATOR_CALLS), 2)
        rej = (ADJUDICATOR_CALLS[1].get("rejected") or [])
        check("第二轮拿到了驳回原因",
              [r.get("reason") for r in rej], ["empty_patch"])
        check("第二轮确实落地了（观察被删）",
              conn.execute("SELECT COUNT(*) FROM graph_observations "
                           "WHERE project_id=?", (BOOK_ID,)).fetchone()[0], 0)
        gdb.close()
    EXPLORER_SCRIPT = None
    ADJUDICATOR_SCRIPT = None

    failed += _test_round_rank()
    failed += _test_boundary_shapes()

    print(f"\n{'FAILED ' + str(failed) if failed else 'ALL PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
