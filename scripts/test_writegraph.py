"""``writegraph`` + ``storage.writing_db`` 的单元测试（纯函数 + 临时库，零 LLM）。

盯的五件事：

1. **位置是闭区间** —— 第 12 章写的值在第 12 章可见；推翻只关区间，
   历史值永不 UPDATE。
2. **未知是一等公民** —— ``value`` / ``unknown_reason`` 二选一，
   冲突要显式标出，不许静默取最新。
3. **证据逐字** —— 引文不在正文里，整份 delta 拒绝（不是丢一条）。
4. **五类义务算得出、排得对、裁得下** —— 每类 ≤3，合计 ≤8，
   安全类（禁止提前揭示）先占位。
5. **七栏固定** —— 缺输入源直接报错；空栏写「无」，不写空串。

跑法：``python scripts/test_writegraph.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.writing_service import WritingService                   # noqa: E402
from storage.writing_db import WritingDB                              # noqa: E402
from writegraph import budget as budget_mod                          # noqa: E402
from writegraph import card, deslop, facts, lens, promises, review   # noqa: E402
from writegraph import state                                          # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def has(label: str, needle: str, hay: str) -> None:
    global FAILED
    if needle not in hay:
        print(f"FAIL {label}\n     没找到 {needle!r}\n     在 {hay[:300]!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def raises(label: str, exc_type, fn) -> None:
    global FAILED
    try:
        fn()
    except exc_type:
        print(f"ok   {label}")
        return
    except Exception as exc:                      # noqa: BLE001
        print(f"FAIL {label}\n     抛了 {type(exc).__name__}: {exc}")
        FAILED += 1
        return
    print(f"FAIL {label}\n     没有抛错")
    FAILED += 1


def fact(subject: str, field: str, value, start: int,
         until: int | None = None, reason: str | None = None) -> dict:
    return {"id": f"{subject}-{field}-{start}", "subject_ref": subject,
            "field": field, "value": value, "unknown_reason": reason,
            "valid_from_chapter": start, "valid_until_chapter": until}


def test_position() -> None:
    rows = [
        fact("n1", "location", "码头", 0, 5),
        fact("n1", "location", "城门", 6),
        fact("n1", "core_desire", None, 0, reason="unclaimed"),
    ]
    at5 = facts.current(rows, 5)
    check("位置：第5章仍是码头", [(r["field"], r["value"]) for r in at5
                                if r["field"] == "location"], [("location", "码头")])
    at6 = facts.current(rows, 6)
    check("位置：第6章换成城门", [r["value"] for r in at6
                                if r["field"] == "location"], ["城门"])
    check("位置：第0章看不到第6章的城门",
          [r["value"] for r in facts.current(rows, 0)
           if r["field"] == "location"], ["码头"])

    unknown = [r for r in at5 if r["field"] == "core_desire"][0]
    check("未知：状态标 unknown", unknown["status"], "unknown")
    has("未知：带原因", "作者尚未设定", facts.describe(unknown))

    conflict_rows = [fact("n1", "goal", "复仇", 3), fact("n1", "goal", "活着", 3)]
    conflicted = facts.current(conflict_rows, 3)
    check("冲突：两条都返回", len(conflicted), 2)
    check("冲突：都标 conflict",
          [r["status"] for r in conflicted], ["conflict", "conflict"])
    has("冲突：渲染显式", "冲突", facts.describe(conflicted[0]))


def promise(pid: str, **kw) -> dict:
    base = {"id": pid, "title": pid, "status": "planted", "weight": 5,
            "planned_payoff_chapter": None, "planned_plant_chapter": None,
            "earliest_reveal_chapter": None}
    base.update(kw)
    return base


def test_promises() -> None:
    rows = [
        promise("p_early", planned_payoff_chapter=12, earliest_reveal_chapter=10),
        promise("p_now", planned_payoff_chapter=5, weight=9),
        promise("p_late", planned_payoff_chapter=3, weight=7),
        promise("p_up", planned_payoff_chapter=7, weight=6),
        promise("p_plant", status="planned", planned_plant_chapter=5, weight=8),
    ]
    groups = promises.obligations(rows, 5, horizon=3)
    check("义务：禁止提前揭示", [p["id"] for p in groups["prohibited_early"]],
          ["p_early"])
    check("义务：本章必须兑现", [p["id"] for p in groups["must_resolve"]],
          ["p_now"])
    check("义务：逾期", [p["id"] for p in groups["overdue"]], ["p_late"])
    check("义务：临近", [p["id"] for p in groups["upcoming"]], ["p_up"])
    check("义务：本章必须埋", [p["id"] for p in groups["to_plant"]],
          ["p_plant"])

    many = [promise(f"a{i}", planned_payoff_chapter=5, weight=i)
            for i in range(1, 6)]
    many += [promise(f"b{i}", planned_payoff_chapter=1, weight=i)
             for i in range(1, 6)]
    many += [promise(f"c{i}", planned_payoff_chapter=7, weight=i)
             for i in range(1, 6)]
    capped = promises.top(promises.obligations(many, 5))
    check("裁剪：合计不超过 8",
          sum(len(v) for v in capped.values()), 8)
    check("裁剪：每类不超过 3",
          max(len(v) for v in capped.values()) <= 3, True)
    check("裁剪：同类保权重高的",
          [p["id"] for p in capped["must_resolve"]], ["a5", "a4", "a3"])


TEXT = "沈砚走进码头，左手已经废了。他抬头看见老周。"


def base_delta() -> dict:
    return {
        "chapter_summary": "沈砚到码头见老周",
        "fact_ops": [
            {"op": "set", "subject_ref": "n1", "field": "location",
             "value": "码头", "since_chapter": 1,
             "evidence_text": "沈砚走进码头"},
            {"op": "unknown", "subject_ref": "n1", "field": "core_desire",
             "unknown_reason": "unclaimed", "since_chapter": 1,
             "evidence_text": "他抬头看见老周"},
        ],
        "promise_ops": [
            {"op": "plant", "id": "tmp1", "title": "老周的身份",
             "kind": "foreshadow", "weight": 8,
             "evidence_text": "他抬头看见老周"},
        ],
    }


def test_validate() -> None:
    good = state.validate_delta(base_delta(), chapter_index=1,
                                chapter_text=TEXT, known_refs=["n1"])
    check("校验：合法 delta 通过", (good.ok, good.errors), (True, []))
    check("校验：归一化保留章节摘要", good.delta["chapter_summary"],
          "沈砚到码头见老周")

    bad = base_delta()
    bad["fact_ops"][0]["evidence_text"] = "这句话不在正文里"
    r = state.validate_delta(bad, chapter_index=1, chapter_text=TEXT,
                             known_refs=["n1"])
    check("校验：证据不逐字 → 整份拒绝", r.ok, False)
    has("校验：报出证据问题", "逐字", "；".join(r.errors))

    bad = base_delta()
    bad["fact_ops"][0]["subject_ref"] = "n404"
    r = state.validate_delta(bad, chapter_index=1, chapter_text=TEXT,
                             known_refs=["n1"])
    check("校验：未知节点被拒", r.ok, False)

    bad = base_delta()
    bad["fact_ops"][0]["unknown_reason"] = "unclaimed"
    r = state.validate_delta(bad, chapter_index=1, chapter_text=TEXT,
                             known_refs=["n1"])
    check("校验：value 与 unknown 同时给被拒", r.ok, False)

    bad = base_delta()
    bad["fact_ops"].append(dict(bad["fact_ops"][0]))
    r = state.validate_delta(bad, chapter_index=1, chapter_text=TEXT,
                             known_refs=["n1"])
    check("校验：同位置重复定义被拒", r.ok, False)

    bad = base_delta()
    bad["fact_ops"][1]["unknown_reason"] = "不知道"
    r = state.validate_delta(bad, chapter_index=1, chapter_text=TEXT,
                             known_refs=["n1"])
    check("校验：未知原因词表外被拒", r.ok, False)

    bad = base_delta()
    bad["promise_ops"].append({"op": "advance", "id": "pm-nope"})
    r = state.validate_delta(bad, chapter_index=1, chapter_text=TEXT,
                             known_refs=["n1"])
    check("校验：引用未知承诺被拒", r.ok, False)

    good = base_delta()
    good["promise_ops"].append({"op": "resolve", "id": "pm1"})
    r = state.validate_delta(good, chapter_index=1, chapter_text=TEXT,
                             known_refs=["n1"],
                             existing_promises=[{"id": "pm1"}])
    check("校验：引用已有承诺通过", r.ok, True)

    bad = base_delta()
    bad["promise_ops"][0].pop("title")
    r = state.validate_delta(bad, chapter_index=1, chapter_text=TEXT,
                             known_refs=["n1"])
    check("校验：plant 缺 title 被拒", r.ok, False)

    check("校验：非对象 delta 被拒",
          state.validate_delta("x", chapter_index=1,
                               chapter_text=TEXT).ok, False)


def test_apply() -> None:
    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    w = db.create_work("测试作品")
    ch1 = db.add_chapter(w, 1)
    ch2 = db.add_chapter(w, 2)

    r = state.validate_delta(base_delta(), chapter_index=1,
                             chapter_text=TEXT, known_refs=["n1"])
    out = db.apply_delta(w, ch1, 1, delta=r.delta, content_hash="h1")
    check("落库：事实两条、承诺一条", out["written"],
          {"facts": 2, "promises": 1})
    pid = out["promise_ids"]["tmp1"]
    check("落库：宿主分配承诺 id", pid.startswith("pm-"), True)

    at1 = facts.current(db.get_facts(w), 1)
    check("落库：位置1读得到码头",
          [x["value"] for x in at1 if x["field"] == "location"], ["码头"])
    check("落库：未知事实保留原因",
          [x["unknown_reason"] for x in at1
           if x["field"] == "core_desire"], ["unclaimed"])

    text2 = "沈砚离开码头，回到城门。"
    d2 = {"fact_ops": [{"op": "set", "subject_ref": "n1",
                        "field": "location", "value": "城门",
                        "since_chapter": 2, "evidence_text": "回到城门"}],
          "promise_ops": [{"op": "mention", "id": pid}]}
    r2 = state.validate_delta(d2, chapter_index=2, chapter_text=text2,
                              known_refs=["n1"],
                              existing_promises=db.get_promises(w))
    check("落库：第二条 delta 合法", r2.ok, True)
    db.apply_delta(w, ch2, 2, delta=r2.delta, content_hash="h2")

    rows = db.get_facts(w)
    at1 = facts.current(rows, 1)
    at2 = facts.current(rows, 2)
    check("区间：第1章仍是码头",
          [x["value"] for x in at1 if x["field"] == "location"], ["码头"])
    check("区间：第2章是城门",
          [x["value"] for x in at2 if x["field"] == "location"], ["城门"])
    check("区间：旧值没被 UPDATE",
          [x["value"] for x in rows
           if x["field"] == "location" and x["valid_from_chapter"] == 1],
          ["码头"])
    check("承诺：mention 不推进",
          db.get_promise(pid)["last_touched_chapter"], 1)

    def settle(chapter: int, op: dict, text: str) -> None:
        cid = db.add_chapter(w, chapter)
        delta = {"fact_ops": [], "promise_ops": [op]}
        r = state.validate_delta(delta, chapter_index=chapter,
                                 chapter_text=text, known_refs=["n1"],
                                 existing_promises=db.get_promises(w))
        assert r.ok, r.errors
        db.apply_delta(w, cid, chapter, delta=r.delta)

    settle(3, {"op": "defer", "id": pid}, "沈砚按下此事。")
    p = db.get_promise(pid)
    check("承诺：defer 改状态", p["status"], "deferred")
    check("承诺：defer 不推进", p["last_touched_chapter"], 1)

    settle(4, {"op": "advance", "id": pid}, "沈砚重新追查。")
    p = db.get_promise(pid)
    check("承诺：advance 回到已埋", p["status"], "planted")
    check("承诺：advance 推进 touched", p["last_touched_chapter"], 4)

    settle(5, {"op": "resolve", "id": pid}, "真相揭开。")
    p = db.get_promise(pid)
    check("承诺：resolve 记回收章", (p["status"], p["payoff_chapter"]),
          ("resolved", 5))
    settle(6, {"op": "resolve", "id": pid}, "已经是结局。")
    p = db.get_promise(pid)
    check("承诺：resolved 不可重开", (p["status"], p["payoff_chapter"]),
          ("resolved", 5))

    recent = db.recent_summaries(w, 2)
    check("摘要：第一章的结算在了",
          [x["summary"] for x in recent], ["沈砚到码头见老周"])


def test_card() -> None:
    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    w = db.create_work("长夜", style_profile={
        "hard_rules": ["能力不能复活死者"], "taboos": ["不写穿越"]})
    ch1 = db.add_chapter(w, 1)
    ch2 = db.add_chapter(w, 2, title="渡口")
    db.set_contract(ch2, purpose="过河", must_hit_now=["沈砚上船"],
                    story_time="深秋", forbidden_cross=["提前揭示船夫身份"])

    rows = [fact("n1", "location", "码头", 0, 1),
            fact("n1", "location", "城门", 2),
            fact("n1", "core_desire", None, 0, reason="unclaimed")]
    prs = [promise("pm1", title="船夫的身份", planned_payoff_chapter=2,
                   weight=9)]

    c = card.build_card(work=db.get_work(w), chapter=db.get_chapter(w, 2),
                        contract=db.get_contract(ch2), facts_rows=rows,
                        promise_rows=prs, name_of={"n1": "沈砚"})
    check("卡片：七栏齐全", [s["key"] for s in c["sections"]],
          list(card.SECTION_KEYS))
    check("卡片：位置默认取章前", c["position"], 1)
    check("卡片：章前读码头",
          [s for s in c["sections"] if s["key"] == "characters"][0]["lines"],
          ["- 沈砚 —— location：码头；goal：未知；"
           "state：未知；knowledge：未知"])
    check("卡片：承诺进入活跃栏",
          [p["id"] for p in c["obligations"]["must_resolve"]], ["pm1"])
    has("卡片：长期约束在", "能力不能复活死者", c["text"])
    has("卡片：风险带来源", "[合同 forbidden_cross]", c["text"])
    has("卡片：渲染七个小节", "## 7. 连贯性风险", c["text"])

    raises("卡片：缺合同直接报错", card.CardIncomplete,
           lambda: card.build_card(work=db.get_work(w),
                                   chapter=db.get_chapter(w, 2),
                                   contract=None, facts_rows=[], promise_rows=[]))
    broken = {"sections": c["sections"][:-1]}
    raises("卡片：缺栏渲染报错", card.CardIncomplete,
           lambda: card.render_card(broken))


def test_db() -> None:
    path = os.path.join(tempfile.mkdtemp(), "w.db")
    db = WritingDB(path)
    w = db.create_work("库测试")
    ch = db.add_chapter(w, 1)
    conn = db.get_conn()

    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
    wanted = {"works", "chapters", "chapter_contracts", "facts", "promises",
              "promise_edges", "snapshots", "review_findings", "revisions",
              "chapter_chunks", "writing_chunks_fts", "writing_runs",
              "writing_chunks_ai", "writing_chunks_ad", "writing_chunks_au"}
    check("建库：表与触发器齐全", sorted(wanted - names), [])

    conn.execute(
        "INSERT INTO chapter_chunks (id, chapter_id, chunk_index, content)"
        " VALUES ('ck1', ?, 0, '沈砚走进城南码头')", (ch,))
    conn.commit()
    # trigram 分词器只索引长度 >= 3 的片段 —— 两字查询永远搜不到，
    # 这是选型自带的下界，不是 bug（查询侧见 04-lens.md）。
    hit = conn.execute(
        "SELECT id FROM writing_chunks_fts WHERE writing_chunks_fts MATCH ?",
        ("城南码",)).fetchall()
    check("FTS：插入即可搜到", [r[0] for r in hit], ["ck1"])

    conn.execute("UPDATE chapter_chunks SET content='沈砚离开渡口'"
                 " WHERE id='ck1'")
    conn.commit()
    old = conn.execute(
        "SELECT id FROM writing_chunks_fts WHERE writing_chunks_fts MATCH ?",
        ("城南码",)).fetchall()
    new = conn.execute(
        "SELECT id FROM writing_chunks_fts WHERE writing_chunks_fts MATCH ?",
        ("离开渡",)).fetchall()
    check("FTS：更新触发器同步", ([r[0] for r in old], [r[0] for r in new]),
          ([], ["ck1"]))

    db.close()
    again = WritingDB(path)
    check("重开：数据还在", again.get_work(w)["title"], "库测试")
    again.close()


def test_rename() -> None:
    from services.writing_service import WritingError

    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    svc = WritingService(
        SimpleNamespace(project_dir=Path(__file__).resolve().parent.parent),
        db=db)
    w = svc.create_work("旧书名")
    svc.create_chapter(w, title="旧章名")

    svc.rename_work(w, "  新书名  ")
    check("重命名：书名落库并去空白", db.get_work(w)["title"], "新书名")
    svc.rename_chapter(w, 1, "新章名")
    check("重命名：章名落库", db.get_chapter(w, 1)["title"], "新章名")
    svc.rename_chapter(w, 1, "")
    check("重命名：章名可为空", db.get_chapter(w, 1)["title"], "")

    raises("重命名：书名不许空",
           WritingError, lambda: svc.rename_work(w, "   "))
    raises("重命名：作品不存在",
           WritingError, lambda: svc.rename_work("wk-missing", "x"))
    raises("重命名：章不存在",
           WritingError, lambda: svc.rename_chapter(w, 9, "x"))
    db.close()


def test_budget() -> None:
    blocks = {
        "chapter_contract": "甲" * 100,
        "facts_current": "乙" * 100,
        "prev_tail": "",
        "context_card": "丙" * 100,
        "reference_material": "丁" * 4000,
    }
    result = budget_mod.compile_blocks(blocks, budget=1000)
    dropped = [b["id"] for b in result["manifest"]["blocks"] if b["dropped"]]
    check("预算：参考素材第一个丢", dropped, ["reference_material"])

    raises("预算：必需块溢出直接报错",
           budget_mod.ProtectedContextOverflow,
           lambda: budget_mod.compile_blocks(
               {**blocks, "context_card": "丙" * 4000}, budget=500))

    raises("预算：缺必需块报错", budget_mod.ProtectedContextOverflow,
           lambda: budget_mod.compile_blocks(
               {"chapter_contract": "", "facts_current": "",
                "context_card": ""}, budget=1000))

    raises("预算：未登记块报错", ValueError,
           lambda: budget_mod.compile_blocks(
               {**blocks, "mystery": "x"}, budget=1000))

    # 回归：priority 越高越重要（旧实现按升序保留，语义正好相反）。
    tight = {
        "chapter_contract": "甲" * 100,
        "facts_current": "乙" * 100,
        "prev_tail": "",
        "context_card": "丙" * 100,
        "author_voice": "戊" * 1000,
        "reference_material": "丁" * 1000,
    }
    result = budget_mod.compile_blocks(tight, budget=700)
    kept = [b["id"] for b in result["manifest"]["blocks"] if not b["dropped"]]
    dropped = [b["id"] for b in result["manifest"]["blocks"] if b["dropped"]]
    check("预算：只够一个可选块时留 priority 高的",
          ("author_voice" in kept, "reference_material" in dropped),
          (True, True))


def test_lens() -> None:
    query = lens.fts_query("码头 复仇 a 沈砚走进城南码头")
    check("查询：短词被滤掉", query, '"沈砚走进城南码头"')
    check("查询：空文本给空串", lens.fts_query("码头 的"), "")

    rows = [
        {"id": "old", "rank": -2.0, "chapter_index": 1, "chunk_index": 0,
         "content": "沈砚"},
        {"id": "near", "rank": -2.0, "chapter_index": 8, "chunk_index": 0,
         "content": "沈砚"},
    ]
    ranked = lens.rank(rows, chapter=9, entity_names=["沈砚"])
    check("排序：章距近的在前", [r["id"] for r in ranked], ["near", "old"])


def test_deslop() -> None:
    kinds = {i["kind"] for i in deslop.detect("他不是害怕，而是兴奋。")["issues"]}
    check("去AI味：不是A而是B 命中", "not-is" in kinds, True)

    kinds = {i["kind"] for i in deslop.detect("他知道——没有人会来。")["issues"]}
    check("去AI味：破折号命中", "em-dash" in kinds, True)

    result = deslop.detect("这一章的细纲写得很清楚。")
    check("去AI味：工程词是 blocking", result["blocking"] >= 1, True)

    clean = "沈砚推开门，风灌进来。他把灯放在桌上，坐了一会儿。"
    check("去AI味：干净文本零 blocking",
          deslop.detect(clean)["blocking"], 0)

    kinds = {i["kind"] for i in deslop.detect("他走了")["issues"]}
    check("去AI味：结尾无标点命中", "no-terminal-punct" in kinds, True)


def _all_criteria(evidence: str, points: float = 5.0) -> list[dict]:
    return [{"criterion": c, "verdict": "pass", "earned": points,
             "evidence": evidence} for c in review.CRITERIA]


def test_review() -> None:
    content = "他抬头看见老周。" + "没有风。" * 80
    payload = {"criteria": _all_criteria("他抬头看见老周。"),
               "findings": []}
    result = review.normalize_review(payload, content)
    check("审稿：满分全评估", (result["quality_score"], result["coverage"]),
          (100.0, 1.0))
    check("审稿：通过", result["delivery_status"], "pass")

    no_evidence = [{"criterion": c, "verdict": "pass", "earned": 4,
                    "evidence": ""} for c in review.CRITERIA]
    result = review.normalize_review({"criteria": no_evidence}, content)
    check("审稿：无证据不得分",
          (result["quality_score"], result["delivery_status"]),
          (None, "inconclusive"))

    scored = review.score({"time_scene": {"verdict": "pass", "earned": 5,
                                          "evidence": "x"}})
    check("审稿：覆盖不足不算满分", scored["coverage"] < 0.8, True)

    good = {"criteria": _all_criteria("他抬头看见老周。"), "findings": [
        {"severity": "S2", "category": "prose", "evidence": "他抬头看见老周。",
         "issue": "开场太直"}]}
    result = review.normalize_review(good, content)
    kept = [f for f in result["findings"]
            if f["evidence"] == "他抬头看见老周。"]
    check("审稿：问题不扣分", result["quality_score"], 100.0)
    check("审稿：问题带偏移", kept[0]["location"], "0-8")

    bad = {"criteria": _all_criteria("他抬头看见老周。"), "findings": [
        {"severity": "S1", "category": "canon", "evidence": "不存在的句子",
         "issue": "矛盾"}]}
    result = review.normalize_review(bad, content)
    check("审稿：编造的引文被作废", result["findings"], [])
    check("审稿：作废有记录", len(result["rejected"]), 1)


def test_revision() -> None:
    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    w = db.create_work("修订")
    ch = db.add_chapter(w, 1)
    text = "沈砚走进码头。" + "夜色很沉。" * 20
    db.save_draft(w, ch, text)

    rev = db.create_revision(ch, run_id=None,
                             anchor_quote="沈砚走进码头",
                             replacement="沈砚走进城门")
    check("修订：提案待确认", rev["status"], "proposed")
    raises("修订：锚点不唯一被拒", ValueError,
           lambda: db.create_revision(ch, run_id=None,
                                      anchor_quote="夜色很沉",
                                      replacement="x"))
    raises("修订：乐观锁拒绝过期应用", ValueError,
           lambda: db.apply_revision(rev["id"], expected_hash="wrong"))

    out = db.apply_revision(rev["id"])
    check("修订：应用后正文替换", "沈砚走进城门" in out["content"], True)
    check("修订：版本递增", out["version"], 2)
    check("修订：状态 applied", db.get_revision(rev["id"])["status"],
          "applied")
    raises("修订：不能重复应用", ValueError,
           lambda: db.apply_revision(rev["id"]))


def test_map_tension() -> None:
    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    w = db.create_work("地图")
    volume = db.add_volume(w, 1, title="上卷")
    chapters = [db.add_chapter(w, i, title=f"章{i}", volume_id=volume)
                for i in range(1, 5)]

    db.set_tension(w, chapters[0], 20)
    db.set_tension(w, chapters[1], 21)
    db.set_tension(w, chapters[2], 22)
    db.set_tension(w, chapters[3], 90, source="ai")
    skipped = db.set_tension(w, chapters[0], 80, source="ai")
    check("紧张度：AI 不覆盖作者锚点",
          (skipped["skipped"], skipped["value"]), (True, 20))

    db.get_conn().execute(
        "INSERT INTO promises (id, work_id, title, weight, status,"
        " planted_chapter, planned_payoff_chapter, time_created, time_updated)"
        " VALUES ('pm-x', ?, '旧伤', 7, 'planted', 1, 4, 0, 0)", (w,))
    db.get_conn().commit()

    svc = WritingService(SimpleNamespace(project_dir=Path(".")), db=db)
    curve = svc.tension_curve(w)
    flags = {p["chapter_index"]: p["flags"] for p in curve["points"]}
    check("曲线：连续三点平坝被标出",
          all("flat" in flags[i] for i in (1, 2, 3)), True)
    check("曲线：提示非空", len(curve["hints"]) >= 1, True)
    check("曲线：AI 值也进点", curve["points"][3]["value"], 90)

    data = svc.map_data(w)
    kinds = {(e["kind"], e["from"].split(":")[0], e["to"].split(":")[0])
             for e in data["edges"]}
    check("地图：卷包含章", ("contains", "vol", "ch") in kinds, True)
    check("地图：本章须兑现", ("must_payoff", "ch", "pm") in kinds, True)
    check("地图：承诺埋设于", ("planted_in", "pm", "ch") in kinds, True)
    check("地图：节点齐全",
          {n["kind"] for n in data["nodes"]},
          {"work", "volume", "chapter", "promise"})

    svc.set_tension(w, 2, 55)
    check("紧张度：作者改点", svc.tension_curve(w)["points"][1]["source"],
          "user")


def test_reference_diag() -> None:
    import json as _json
    root = Path(tempfile.mkdtemp())
    svc = WritingService(SimpleNamespace(
        project_dir=root, writing_db_path=root / "w.db"), db=None)
    svc._log_reference_diag(
        {"id": "wk-1"}, {"chapter_index": 3},
        {"enabled": True, "chars": 120,
         "relevance": {"ok": True, "hits": ["渡口"]}})
    path = root / "data" / "writing" / "reference_cards" / "diagnostics.jsonl"
    rec = _json.loads(path.read_text(encoding="utf-8").strip())
    check("召回诊断：落盘",
          (rec["work_id"], rec["chapter"], rec["chars"]),
          ("wk-1", 3, 120))
    check("召回诊断：保留门", rec["relevance"]["ok"], True)


def test_chat_tools() -> None:
    import json as _json
    from tools import ToolRegistry
    from writegraph.chat_tools import build_writing_registry

    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    svc = WritingService(SimpleNamespace(
        project_dir=Path(__file__).resolve().parent.parent), db=db)
    ctx: dict = {"work_id": "", "chapter_index": 0}
    reg = build_writing_registry(ToolRegistry(), svc, ctx)

    names = reg.get_tool_names()
    check("对话工具：十三个已注册", len(names), 13)
    check("对话工具：含 git 与反馈",
          {"writing_git", "writing_git_change", "writing_report",
           "writing_references"} <= set(names), True)
    out = _json.loads(reg.execute("writing_create_work",
                                  _json.dumps({"title": "长夜"})))
    check("对话工具：建作品", out["created"], True)
    check("对话工具：现场记住作品", bool(ctx["work_id"]), True)

    out = _json.loads(reg.execute("writing_create_chapter", _json.dumps({
        "title": "渡口", "purpose": "过河", "must_hit_now": "上船\n见老周"})))
    check("对话工具：建章自动带合同", out["contract"]["must_hit_now"],
          ["上船", "见老周"])
    check("对话工具：现场记住章", ctx["chapter_index"], 1)
    chapter_id = out["contract"]["chapter_id"]

    db.save_draft(ctx["work_id"], chapter_id, "沈砚上了船。风很冷。")
    out = _json.loads(reg.execute("writing_replace_text", _json.dumps({
        "anchor_quote": "风很冷", "replacement": "风从船头灌进来"})))
    check("对话工具：唯一锚点替换", out["word_count"], 14)
    bad = _json.loads(reg.execute("writing_replace_text", _json.dumps(
        {"anchor_quote": "没有这句话", "replacement": "x"})))
    check("对话工具：锚点不唯一如实报错", bad["ok"], False)

    out = _json.loads(reg.execute("writing_read_chapter", _json.dumps(
        {"chapter_index": 1})))
    has("对话工具：读得到改后正文", "风从船头", out["content"])

    out = _json.loads(asyncio.run(
        reg.execute_async("writing_draft_chapter", "{}")))
    check("对话工具：没配模型如实报错", out["ok"], False)
    has("对话工具：错误可读", "模型", out["error"])

    class FakeClient:
        config = SimpleNamespace(model="fake-model")

        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario=""):
            for part in ("沈砚上了船。", "风从船头灌进来。"):
                yield part, [], None

    svc.api_client = FakeClient()
    reg.execute("writing_create_chapter", _json.dumps(
        {"title": "第二夜", "purpose": "上船"}))
    out = _json.loads(asyncio.run(
        reg.execute_async("writing_draft_chapter",
                          _json.dumps({"chapter_index": 2}))))
    check("对话工具：能写出一章", out["ok"], True)
    check("对话工具：草稿落库", out["word_count"], 14)
    chapter2 = svc.chapter(ctx["work_id"], 2)
    has("对话工具：正文写进库", "风从船头", chapter2["content"])

    prompt = svc.agent_prompt(ctx["work_id"], 1)
    has("写作提示词：带现场书名", "长夜", prompt)
    has("写作提示词：带当前章", "第1章", prompt)
    has("写作提示词：禁止贴全文", "不要", prompt)

    out = _json.loads(reg.execute("writing_status", "{}"))
    check("对话工具：现状列出章节", out["current"]["chapters"][0]["title"],
          "渡口")


def test_flight_recorder() -> None:
    from flight_recorder.recorder import ContextRecorder
    from services.writing_service import ThinkStreamFilter, strip_think

    check("思维链：整块剥离", strip_think("<think>想</think>正文"), "正文")
    check("思维链：未闭合整段丢", strip_think("正文<think>想一半"), "正文")

    f = ThinkStreamFilter()
    check("思维链：跨块标签不泄漏",
          (f.feed("<thi"), f.feed("nk>想</thi"), f.feed("nk>正文"), f.flush()),
          ("", "", "正文", ""))

    rec = ContextRecorder(Path(tempfile.mkdtemp()) / "ctx.db")
    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    svc = WritingService(
        SimpleNamespace(project_dir=Path(__file__).resolve().parent.parent),
        db=db, recorder=rec, recorder_session="sess-1")
    work = svc.create_work("录制")
    chapter = svc.create_chapter(work, title="一")
    svc.set_contract(chapter["id"], purpose="推进")

    class ThinkClient:
        config = SimpleNamespace(model="fake-model")

        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario=""):
            yield "<think>先想好再写</think>", [], None
            yield "沈砚上了船。", [], None

    svc.api_client = ThinkClient()
    # 这条用例钉的是「一次草稿只记一轮」；自动修订会合法地多一轮。
    db.update_work_style_profile(work, {"auto_repair": False})

    async def run():
        return [e async for e in svc.stream_chapter(work, 1)]

    events = asyncio.run(run())
    deltas = "".join(e["data"]["text"] for e in events
                     if e["event"] == "delta")
    check("录音：流式不泄漏思维链", deltas, "沈砚上了船。")
    saved = svc.chapter(work, 1)["content"]
    has("录音：落库正文不含思维链", "沈砚上了船", saved)
    check("录音：落库正文不含 think", "<think>" in saved, False)

    rounds = rec.list_rounds("sess-1")
    check("录音：整章只记一轮", len(rounds), 1)
    check("录音：标出调用点", rounds[0]["mode"], "writing.draft")
    full = rec.get_round(rounds[0]["id"])
    has("录音：思维链进了记录仪", "先想好再写", full["response_text"])
    has("录音：系统提示已存", "长度纪律", full["system_prompt"])
    has("录音：长度纪律排在最后", "不要用总结、抒情或重复来凑字数。",
        full["system_prompt"].rstrip()[-40:])

    # ── 自动修订：字数不足时补一轮，且只补一轮 ──
    db2 = WritingDB(os.path.join(tempfile.mkdtemp(), "w2.db"))
    svc2 = WritingService(
        SimpleNamespace(project_dir=Path(__file__).resolve().parent.parent),
        db=db2)
    work2 = svc2.create_work("修订")
    chapter2 = svc2.create_chapter(work2, title="一")
    svc2.set_contract(chapter2["id"], purpose="推进", target_chars=100)
    db2.update_work_style_profile(work2, {"auto_repair": True})

    class RepairClient:
        config = SimpleNamespace(model="fake-model")

        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario=""):
            if "修订任务" in system:
                yield "他把门推开，冷风灌进来。" * 12, [], None
            else:
                yield "短。", [], None

    svc2.api_client = RepairClient()

    async def run2():
        return [e async for e in svc2.stream_chapter(work2, 1)]

    events2 = asyncio.run(run2())
    check("自动修订：触发一次",
          sum(1 for e in events2 if e.get("data", {}).get("stage") == "revising"),
          1)
    check("自动修订：字数补到下限",
          len(svc2.chapter(work2, 1)["content"]) >= 80, True)

    async def settle_like() -> str:
        return await svc._complete("sys", "user", kind="settle",
                                   work_id=work, chapter_index=1)

    svc.api_client = ThinkClient()
    out = asyncio.run(settle_like())
    check("录音：结构化调用也剥思维链", out, "沈砚上了船。")


def main() -> int:
    test_position()
    test_promises()
    test_validate()
    test_apply()
    test_card()
    test_db()
    test_rename()
    test_budget()
    test_lens()
    test_deslop()
    test_review()
    test_revision()
    test_map_tension()
    test_reference_diag()
    test_chat_tools()
    test_flight_recorder()
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
