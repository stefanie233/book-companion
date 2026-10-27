"""``bookgraph.recorder`` 的回归测试（真写一个临时 graph.db，零 LLM、零网络）。

盯的是四组曾经静默失效的行为：

1. **解析不到的引用要落 ``graph_unresolved``** —— 以前是 ``log.debug`` 一句就丢，
   表建好了却全库 0 行。
2. **每条引文带自己的 chunk 与真实 batch_id** —— 以前整批共用批首块，
   后面章节的台词因此被标成前面章节的。
3. **准入闸：声明的实体只有被引用才真的建节点** —— 孤儿由构造消灭。
4. **kind 归一 + 同名跨 kind 复用** —— 同一实体裂成多节点（孔子×4）的根治。
5. **锚点首现块是导出量，系统自己算** —— 模型填的是 evidence 所在块。

跑法：``python scripts/test_graph_recorder.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph.recorder import record_result                # noqa: E402
from storage.graph_db import GraphDB                        # noqa: E402

BOOK = "sb-test"
CHUNKS = ["k1", "k2", "k3"]


def _result() -> dict:
    """一份刻意埋了四种「解析不到」的提取结果。"""
    return {
        "new_nodes": [
            {"canonical_name": "甲", "kind": "character", "description": "主角"},
            {"canonical_name": "乙", "kind": "character", "description": "配角"},
        ],
        "new_edges": [
            # 正常
            {"source": "甲", "target": "乙", "kind": "loves", "chunk_id": "k1"},
            # 端点「丙」不在图谱里 → 未解析
            {"source": "甲", "target": "丙", "kind": "hates", "chunk_id": "k2"},
        ],
        "new_observations": [
            {"node_name": "甲", "field": "personality", "value": "刚强",
             "evidence_chunk_id": "k1"},
            # 观察对象「丁」不在图谱里 → 未解析
            {"node_name": "丁", "field": "personality", "value": "?",
             "evidence_chunk_id": "k1"},
        ],
        "new_aliases": [
            {"alias": "阿甲", "canonical_name": "甲"},
            # canonical「戊」不在图谱里 → 未解析
            {"alias": "阿戊", "canonical_name": "戊"},
        ],
        "merge_candidates": [],
        "quotes": [
            {"character_name": "甲", "quote_text": "第一句", "chunk_id": "k2",
             "scene_context": "场景一", "related_nodes": ["乙"]},
            {"character_name": "乙", "quote_text": "第二句", "chunk_id": "k3",
             "scene_context": "场景二", "related_nodes": ["己"]},   # 「己」未解析
        ],
    }


def _admission_result() -> dict:
    """一份专门试探准入闸与 kind 归一的提取结果。"""
    return {
        "new_nodes": [
            # 被边引用 → 准入；kind 是**变体** → 归一为 character
            {"canonical_name": "甲", "kind": "character (archetypal)",
             "why": "推动主线", "evidence": "甲走了"},
            # 被观察引用 → 准入；kind 是**同义词** → 归一为 character
            {"canonical_name": "乙", "kind": "person", "evidence": "乙笑了"},
            # 没有任何东西引用它 → 不建节点，进缺陷清单
            {"canonical_name": "原子", "kind": "concept", "why": ""},
            # 词表外的 kind，但被引文角色引用 → 准入，kind **原样保留**
            {"canonical_name": "丙", "kind": "deity_of_war", "evidence": "丙降临"},
            # 只有 Scout 点名 → 无条件准入
            {"canonical_name": "主角", "kind": "character"},
        ],
        "new_edges": [
            {"source": "甲", "target": "乙", "kind": "mentors", "chunk_id": "k1",
             "evidence": "甲教乙读书"},
        ],
        "new_observations": [
            {"node_name": "乙", "field": "personality", "value": "沉静",
             "evidence_chunk_id": "k1", "evidence": "乙笑了"},
        ],
        "new_aliases": [], "merge_candidates": [],
        "quotes": [
            {"character_name": "丙", "quote_text": "我来", "chunk_id": "k2",
             "related_nodes": []},
        ],
    }


def _drift_result() -> dict:
    """第二批：把已有的「甲」换个 kind 再声明一次。"""
    return {
        "new_nodes": [{"canonical_name": "甲", "kind": "concept"}],
        "new_edges": [{"source": "甲", "target": "乙", "kind": "knows",
                       "chunk_id": "k3"}],
        "new_observations": [], "new_aliases": [], "merge_candidates": [],
        "quotes": [],
    }


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        db = GraphDB(Path(tmp) / "graph.db")
        stats = record_result(db, BOOK, {}, CHUNKS, _result(), batch_id="batch-real")
        conn = db.get_conn()

        def check(label: str, got, want) -> None:
            nonlocal failed
            if got != want:
                print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
                failed += 1
            else:
                print(f"ok   {label}")

        # 1. 被引用却没声明的名字：**建节点兜底**，边不再整条蒸发。
        #    （旧行为是丢进 graph_unresolved 然后丢掉这条边 —— 实测
        #     《我与地坛》28 条边只落地 3 条，丢的全是「我」「母亲」「地坛」。）
        check("兜底建节点：丙/丁/戊/己", set(stats.unresolved_admitted),
              {"丙", "丁", "戊", "己"})
        check("兜底建出来的 kind 是 unknown（不编假的）",
              sorted({r["kind"] for r in conn.execute(
                  "SELECT kind FROM nodes WHERE project_id=? AND canonical_name "
                  "IN ('丙','丁','戊','己')", (BOOK,))}), ["unknown"])
        # 没锚点的节点跳不回原文、算不出章号（剧透门失效）—— 兜底也要锚
        check("兜底建出来的节点有锚点",
              {r["canonical_name"]: r["first_appearance_chunk_id"]
               for r in conn.execute(
                   "SELECT canonical_name, first_appearance_chunk_id FROM nodes "
                   "WHERE project_id=? AND kind='unknown'", (BOOK,))},
              {"丙": "k2", "丁": "k1", "戊": "", "己": "k3"})
        check("没有边再因端点缺失而丢", db.count_unresolved(BOOK), 0)
        check("stats.unresolved 归零", stats.unresolved, 0)

        # 2. 引文各带自己的 chunk 与真实 batch_id
        qs = conn.execute(
            "SELECT quote_text, chunk_id, batch_id FROM graph_quotes "
            "WHERE project_id=? ORDER BY quote_text", (BOOK,)).fetchall()
        check("引文 chunk 各归各", [q["chunk_id"] for q in qs], ["k2", "k3"])
        check("引文 batch_id 是真的", {q["batch_id"] for q in qs}, {"batch-real"})

        # 3. 全部关系都落地了（旧行为这里只有 1）
        check("边数", conn.execute(
            "SELECT COUNT(*) FROM edges WHERE project_id=?", (BOOK,)).fetchone()[0], 2)
        check("观察数", conn.execute(
            "SELECT COUNT(*) FROM graph_observations WHERE project_id=?",
            (BOOK,)).fetchone()[0], 2)
        check("别名数", conn.execute(
            "SELECT COUNT(*) FROM graph_aliases WHERE project_id=?",
            (BOOK,)).fetchone()[0], 2)
        db.close()

    # ── 3/4. 准入闸 + kind 归一 + 同名跨 kind 复用 ─────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        db = GraphDB(Path(tmp) / "graph.db")
        stats = record_result(db, BOOK, {}, ["k1", "k2"], _admission_result(),
                              batch_id="b1", vocabulary={"character", "concept"},
                              key_characters=["主角", "幽灵角色"])
        conn = db.get_conn()
        names = {r["canonical_name"] for r in conn.execute(
            "SELECT canonical_name FROM nodes WHERE project_id=? AND kind!='chapter'",
            (BOOK,))}
        check("准入闸：没人引用的实体不建节点",
              names, {"甲", "乙", "丙", "主角"})
        check("孤儿进缺陷清单", [d["name"] for d in stats.orphan_dropped], ["原子"])
        check("Scout 点名的无条件准入", "主角" in names, True)
        # Scout 名单是**全书**的（实测 34 个）。兜底建节点只能看本批引用过谁，
        # 否则第一批就把全书角色全建成 degree=0 的节点 —— 那正是要消灭的孤儿。
        check("Scout 名单里本批没用到的名字不建节点",
              "幽灵角色" in names, False)

        kinds = {r["canonical_name"]: r["kind"] for r in conn.execute(
            "SELECT canonical_name, kind FROM nodes WHERE project_id=?", (BOOK,))}
        check("kind 变体归一（character (archetypal) → character）",
              kinds.get("甲"), "character")
        check("kind 同义词归并（person → character）", kinds.get("乙"), "character")
        check("词表外的 kind 原样保留（不硬改）", kinds.get("丙"), "deity_of_war")
        check("词表外的 kind 记进缺陷清单（原话，供回炉用）",
              stats.kind_off_vocab, ["deity_of_war"])

        # 模型漏写 first_appearance_chunk_id 时用「第一次引用它的块」兜底 ——
        # 没锚点的节点定位不到，剧透门对它失效（实测真实图谱里 8% 如此）。
        check("声明漏了锚点 → 用首次引用的块兜底",
              {r["canonical_name"]: r["first_appearance_chunk_id"]
               for r in conn.execute(
                   "SELECT canonical_name, first_appearance_chunk_id FROM nodes "
                   "WHERE project_id=? AND canonical_name IN ('甲','乙')",
                   (BOOK,))},
              {"甲": "k1", "乙": "k1"})

        # first_appearance_chunk_id 是**导出量**：给了 chunk_text 就逐块扫正文
        # 算，不采信模型的猜测。实测模型填的是「这条断言引的 evidence 所在
        # 块」—— 证伪器为此报了 14 条质疑（占全部质疑 10%），每次烧一整轮
        # 重提取，去改一个系统一秒就能算对的字段。
        record_result(
            db, BOOK, {}, CHUNKS,
            {"new_nodes": [{"canonical_name": "辛", "kind": "character",
                            "first_appearance_chunk_id": "k3"}],
             "new_edges": [{"source": "辛", "target": "甲", "kind": "knows",
                            "chunk_id": "k3", "evidence": "辛认识甲"}],
             "new_observations": [], "new_aliases": [],
             "merge_candidates": [], "quotes": []},
            batch_id="b3",
            chunk_text={"k1": "辛在第一章就出场了。", "k2": "无关",
                        "k3": "辛认识甲"},
        )
        check("锚点首现块由系统算（模型自报 k3，正文首现是 k1）",
              conn.execute("SELECT first_appearance_chunk_id FROM nodes "
                           "WHERE project_id=? AND canonical_name='辛'",
                           (BOOK,)).fetchone()[0], "k1")

        check("边的逐字证据落库", conn.execute(
            "SELECT evidence_text FROM edges WHERE project_id=? AND kind='mentors'",
            (BOOK,)).fetchone()[0], "甲教乙读书")
        check("观察的逐字证据落库", conn.execute(
            "SELECT evidence_text FROM graph_observations WHERE project_id=?", (BOOK,)
        ).fetchone()[0], "乙笑了")

        # 第二批：同名换 kind → 复用同一个节点，并记一条漂移
        stats2 = record_result(db, BOOK, {}, ["k3"], _drift_result(), batch_id="b2")
        check("同名跨 kind 复用（不新建节点）", conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id=? AND canonical_name='甲'",
            (BOOK,)).fetchone()[0], 1)
        check("kind 漂移被记下来",
              stats2.kind_drift, [{"name": "甲", "kept": "character",
                                   "offered": "concept"}])

        # 书名号变体：同一个 work 的两种写法必须合成一个节点。
        # 实测窄门一本里同时出现 《圣经》 与 圣经。
        a = db.upsert_node(BOOK, "《圣经》", "work")
        b = db.upsert_node(BOOK, "圣经", "work")
        check("书名号变体合并（同 kind）", a, b)
        # 但跨 kind 不合并：《窄门》是作品、窄门是概念，压成一个就错了
        c = db.upsert_node(BOOK, "《窄门》", "work")
        d = db.upsert_node(BOOK, "窄门", "concept")
        check("书名号变体不跨 kind 合并", c == d, False)
        db.close()

    # ── 5. 边去重：同一条持久关系只落一行，锚点取更早的块 ───────────────
    with tempfile.TemporaryDirectory() as tmp:
        db = GraphDB(Path(tmp) / "graph.db")
        conn = db.get_conn()
        # 三块分属两章：k1/k2 在 ch0，k3 在 ch1。
        db.register_chunk("k1", BOOK, 0, 0, "h1")
        db.register_chunk("k2", BOOK, 0, 1, "h2")
        db.register_chunk("k3", BOOK, 1, 0, "h3")
        a = db.upsert_node(BOOK, "甲", "character")
        b = db.upsert_node(BOOK, "乙", "character")

        def check2(label: str, got, want) -> None:
            nonlocal failed
            if got != want:
                print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
                failed += 1
            else:
                print(f"ok   {label}")

        check2("首次写入算新增", db.add_edge(BOOK, a, b, "loves", chunk_id="k3",
                                            event_type="perspective",
                                            evidence_text="后来的话"), True)
        # 后一章先写、前一章后写 —— 锚点必须挪到**更早**的那块（剧透门要
        # 的是「最早能在哪知道」，不是「最后一次被提到」）。
        check2("重复写入不算新增", db.add_edge(
            BOOK, a, b, "loves", chunk_id="k1", event_type="perspective",
            evidence_text="最早的话"), False)
        row = conn.execute(
            "SELECT chunk_id, evidence_text, occurrences FROM edges "
            "WHERE project_id=? AND kind='loves'", (BOOK,)).fetchone()
        check2("锚点取更早的块", row["chunk_id"], "k1")
        check2("证据跟着锚点走", row["evidence_text"], "最早的话")
        check2("出现次数累加", row["occurrences"], 2)
        check2("行数只有一行", conn.execute(
            "SELECT COUNT(*) FROM edges WHERE project_id=? AND kind='loves'",
            (BOOK,)).fetchone()[0], 1)
        # event_type 进键：「她是甲的爱人」（fact）与「乙觉得甲爱她」
        # （perspective）是两句不同的话，不能合并成一条。
        db.add_edge(BOOK, a, b, "loves", chunk_id="k2", event_type="fact")
        check2("event_type 不同的同一条关系不合并", conn.execute(
            "SELECT COUNT(*) FROM edges WHERE project_id=? AND kind='loves'",
            (BOOK,)).fetchone()[0], 2)
        # 详情页按 (方向, 对方, 关系词) 合并，occurrences 用行上的值 ——
        # 写入侧去重后这个计数还要在，否则角色卡排序退回「谁先被写」。
        detail = db.get_node_detail(BOOK, "甲")
        rel = [r for r in detail["relations"] if r["kind"] == "loves"]
        check2("详情页拿到累计次数", sum(r["occurrences"] for r in rel), 3)

        # 飞行记录仪要能取回**思考过程**，不能只留一个 token 数。
        call_id = db.record_llm_call(
            book_id=BOOK, run_id="run-x", phase="explorer", batch_index=4,
            prompt="P", response="R", reasoning_content="我在核对代词…")
        got = db.get_llm_call(call_id)
        check2("思维链正文落库可取回", got["reasoning_content"], "我在核对代词…")
        check2("调用列表仍可用", len(db.list_llm_calls("run-x")), 1)
        db.close()

    print(f"\n{'FAILED ' + str(failed) if failed else 'ALL PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
