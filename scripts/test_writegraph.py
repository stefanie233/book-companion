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
from writegraph import card, deslop, facts, goals, lens, promises, review   # noqa: E402
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


TEXT = "人物A走进码头，左手已经废了。他抬头看见配角。"


def base_delta() -> dict:
    return {
        "chapter_summary": "人物A到码头见配角",
        "fact_ops": [
            {"op": "set", "subject_ref": "n1", "field": "location",
             "value": "码头", "since_chapter": 1,
             "evidence_text": "人物A走进码头"},
            {"op": "unknown", "subject_ref": "n1", "field": "core_desire",
             "unknown_reason": "unclaimed", "since_chapter": 1,
             "evidence_text": "他抬头看见配角"},
        ],
        "promise_ops": [
            {"op": "plant", "id": "tmp1", "title": "配角的身份",
             "kind": "foreshadow", "weight": 8,
             "evidence_text": "他抬头看见配角"},
        ],
    }


def test_validate() -> None:
    good = state.validate_delta(base_delta(), chapter_index=1,
                                chapter_text=TEXT, known_refs=["n1"])
    check("校验：合法 delta 通过", (good.ok, good.errors), (True, []))
    check("校验：归一化保留章节摘要", good.delta["chapter_summary"],
          "人物A到码头见配角")

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
    joined = "；".join(r.errors)
    has("校验：拒绝时给出可用 id", "可用的 subject_ref：n1", joined)
    has("校验：拒绝时给出处方", "entity_ops", joined)

    # 空账本：没有可用 id 时必须明说「先放进 entity_ops」。
    r = state.validate_delta(base_delta(), chapter_index=1, chapter_text=TEXT,
                             known_refs=[])
    check("校验：空账本也给处方",
          "entity_ops" in "；".join(r.errors), True)

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

    # 来源权威（决策 180）：结算不得覆盖作者/导入来源的开放事实。
    author_fact = {"subject_ref": "n1", "field": "location",
                   "value": "码头", "source_kind": "author",
                   "valid_from_chapter": 1, "valid_until_chapter": None}
    over = {"fact_ops": [{"op": "set", "subject_ref": "n1",
                          "field": "location", "value": "城门",
                          "since_chapter": 2,
                          "evidence_text": "离开码头"}]}
    r = state.validate_delta(over, chapter_index=2,
                             chapter_text="人物A离开码头。",
                             known_refs=["n1"],
                             existing_facts=[author_fact])
    check("权威：结算不能覆盖作者事实", r.ok, False)
    has("权威：拒绝说清来源", "作者来源", "；".join(r.errors))

    r = state.validate_delta(over, chapter_index=2,
                             chapter_text="人物A离开码头。",
                             known_refs=["n1"],
                             existing_facts=[author_fact],
                             allow_author=True)
    check("权威：allow_author 不放宽低来源", r.ok, False)

    claim = {"fact_ops": [{"op": "set", "subject_ref": "n1",
                           "field": "mood", "value": "平静",
                           "since_chapter": 2,
                           "source_kind": "author",
                           "evidence_text": "离开码头"}]}
    r = state.validate_delta(claim, chapter_index=2,
                             chapter_text="人物A离开码头。",
                             known_refs=["n1"])
    check("权威：结算不能自称 author", r.ok, False)
    has("权威：自称 author 被点名",
        "不能声明 source_kind='author'", "；".join(r.errors))
    r = state.validate_delta(claim, chapter_index=2,
                             chapter_text="人物A离开码头。",
                             known_refs=["n1"], allow_author=True)
    check("权威：作者侧入口可写 author", r.ok, True)

    close = {"fact_ops": [{"op": "close", "subject_ref": "n1",
                           "field": "location", "until_chapter": 2}]}
    r = state.validate_delta(close, chapter_index=2,
                             chapter_text="人物A离开码头。",
                             known_refs=["n1"],
                             existing_facts=[author_fact])
    check("权威：结算不能关闭作者事实", r.ok, False)

    delta_fact = {**author_fact, "source_kind": "delta"}
    r = state.validate_delta(over, chapter_index=2,
                             chapter_text="人物A离开码头。",
                             known_refs=["n1"],
                             existing_facts=[delta_fact])
    check("权威：同级来源可以正常更新", r.ok, True)


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

    text2 = "人物A离开码头，回到城门。"
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

    settle(3, {"op": "defer", "id": pid}, "人物A按下此事。")
    p = db.get_promise(pid)
    check("承诺：defer 改状态", p["status"], "deferred")
    check("承诺：defer 不推进", p["last_touched_chapter"], 1)

    settle(4, {"op": "advance", "id": pid}, "人物A重新追查。")
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
          [x["summary"] for x in recent], ["人物A到码头见配角"])

    # 同章重结算：改稿后再结算允许覆盖本章上一版的事实；旧行标
    # superseded（保留可追溯，不参与求值），跨章同位置仍然拒绝。
    redo = {"fact_ops": [{"op": "set", "subject_ref": "n1",
                          "field": "location", "value": "山道",
                          "since_chapter": 2,
                          "evidence_text": "回到城门"}]}
    r = state.validate_delta(redo, chapter_index=2, chapter_text=text2,
                             known_refs=["n1"],
                             existing_facts=db.get_facts(w))
    check("重结算：同章允许覆盖", r.ok, True)
    check("重结算：标记 resettle", r.delta["fact_ops"][0].get("resettle"), True)
    db.apply_delta(w, ch2, 2, delta=r.delta)
    now2 = facts.current(db.get_facts(w), 2)
    check("重结算：读到新值",
          [x["value"] for x in now2 if x["field"] == "location"], ["山道"])
    old = [x for x in db.get_facts(w) if x["field"] == "location"
           and x["valid_from_chapter"] == 2 and x["value"] == "城门"]
    check("重结算：旧行标 superseded", old[0]["superseded"], 1)
    check("重结算：旧行不再可见",
          [x["value"] for x in facts.visible(db.get_facts(w), 2)
           if x["field"] == "location"], ["山道"])


def test_card() -> None:
    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    w = db.create_work("长夜", style_profile={
        "hard_rules": ["能力不能复活死者"], "taboos": ["不写穿越"]})
    ch1 = db.add_chapter(w, 1)
    ch2 = db.add_chapter(w, 2, title="渡口")
    db.set_contract(ch2, purpose="过河", must_hit_now=["人物A上船"],
                    story_time="深秋", forbidden_cross=["提前揭示船夫身份"],
                    withhold=["配角与人物A父亲的关系"])
    saved_contract = db.get_contract(ch2)
    check("合同：withhold 落库", saved_contract.get("withhold"),
          ["配角与人物A父亲的关系"])
    check("合同：withhold 不混进禁止项",
          saved_contract.get("forbidden_cross"), ["提前揭示船夫身份"])

    rows = [fact("n1", "location", "码头", 0, 1),
            fact("n1", "location", "城门", 2),
            fact("n1", "core_desire", None, 0, reason="unclaimed")]
    prs = [promise("pm1", title="船夫的身份", planned_payoff_chapter=2,
                   weight=9)]

    c = card.build_card(work=db.get_work(w), chapter=db.get_chapter(w, 2),
                        contract=db.get_contract(ch2), facts_rows=rows,
                        promise_rows=prs, name_of={"n1": "人物A"})
    check("卡片：七栏齐全", [s["key"] for s in c["sections"]],
          list(card.SECTION_KEYS))
    check("卡片：位置默认取章前", c["position"], 1)
    check("卡片：章前读码头",
          [s for s in c["sections"] if s["key"] == "characters"][0]["lines"],
          ["- 人物A —— location：码头；goal：未知；"
           "state：未知；knowledge：未知"])
    check("卡片：承诺进入活跃栏",
          [p["id"] for p in c["obligations"]["must_resolve"]], ["pm1"])
    has("卡片：长期约束在", "能力不能复活死者", c["text"])
    has("卡片：风险带来源", "[合同 forbidden_cross]", c["text"])
    has("卡片：不点破进风险", "[合同 withhold]", c["text"])
    has("卡片：渲染七个小节", "## 7. 连贯性风险", c["text"])

    authored = card.build_card(
        work=db.get_work(w), chapter=db.get_chapter(w, 2),
        contract=db.get_contract(ch2),
        facts_rows=[dict(fact("n1", "location", "码头", 0, 1),
                         source_kind="author")],
        promise_rows=[], name_of={"n1": "人物A"})
    has("卡片：作者来源带标记", "〔作者〕", authored["text"])

    dormant = card.build_card(
        work=db.get_work(w), chapter=db.get_chapter(w, 2),
        contract=db.get_contract(ch2), facts_rows=rows,
        promise_rows=[*prs, promise("pm2", title="旧信物",
                                    last_touched_chapter=0)],
        name_of={"n1": "人物A"}, position=7)
    has("卡片：休眠线索只在风险栏提示", "已 7 章未推进",
        dormant["text"])
    check("卡片：休眠不进五类义务",
          [p["id"] for p in dormant["obligations"]["must_resolve"]], ["pm1"])

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
        " VALUES ('ck1', ?, 0, '人物A走进城南码头')", (ch,))
    conn.commit()
    # trigram 分词器只索引长度 >= 3 的片段 —— 两字查询永远搜不到，
    # 这是选型自带的下界，不是 bug（查询侧见 04-lens.md）。
    hit = conn.execute(
        "SELECT id FROM writing_chunks_fts WHERE writing_chunks_fts MATCH ?",
        ("城南码",)).fetchall()
    check("FTS：插入即可搜到", [r[0] for r in hit], ["ck1"])

    conn.execute("UPDATE chapter_chunks SET content='人物A离开渡口'"
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

    # 装配收据：必须能答「哪些块被拒、为什么、必需是否覆盖完整」，
    # 但收据里不许出现正文。
    decision = result["decision"]
    check("收据：必需覆盖完整",
          decision["coverage"], {"required": 4, "included": 4, "complete": True})
    check("收据：参考素材被记入丢弃", "reference_material" in decision["dropped"], True)
    check("收据：不含块正文",
          any("丁" in str(b) for b in decision["blocks"]), False)


def test_lens() -> None:
    query = lens.fts_query("码头 复仇 a 人物A走进城南码头")
    check("查询：短词被滤掉", query, '"走进城南码头"')
    check("查询：空文本给空串", lens.fts_query("码头 的"), "")

    rows = [
        {"id": "old", "rank": -2.0, "chapter_index": 1, "chunk_index": 0,
         "content": "人物A"},
        {"id": "near", "rank": -2.0, "chapter_index": 8, "chunk_index": 0,
         "content": "人物A"},
    ]
    ranked = lens.rank(rows, chapter=9, entity_names=["人物A"])
    check("排序：章距近的在前", [r["id"] for r in ranked], ["near", "old"])

    content = "开场白。\n\n沈砚推开门，风灌进来。\n\n屋里没人。\n\n他走到桌前。\n\n桌上有一封信。"
    passages, matched = lens.evidence_passages(content, ["推开门"])
    check("证据窗口：命中段带前后各一段", matched, 1)
    check("证据窗口：取到相邻三段",
          "开场白" in passages[0] and "屋里没人" in passages[0], True)
    check("证据窗口：无关段不进来", "桌上有一封信" in passages[0], False)

    dup = lens.dedupe_contained(["甲段", "甲段\n\n乙段"])
    check("证据窗口：被包含片段去重", dup, ["甲段\n\n乙段"])

    report = lens.material_evidence(
        [{"chapter_index": 3, "content": content},
         {"chapter_index": 4, "content": "完全无关的一段话。"}],
        ["推开门"])
    check("素材收据：命中章被纳入", [i["chapter"] for i in report["included"]], [3])
    check("素材收据：无关章明确省略",
          [(i["chapter"], i["reason"]) for i in report["omitted"]],
          [(4, "no-relevant-passage")])


def test_deslop() -> None:
    kinds = {i["kind"] for i in deslop.detect("他不是害怕，而是兴奋。")["issues"]}
    check("去AI味：不是A而是B 命中", "not-is" in kinds, True)

    kinds = {i["kind"] for i in deslop.detect("他知道——没有人会来。")["issues"]}
    check("去AI味：破折号命中", "em-dash" in kinds, True)

    result = deslop.detect("这一章的细纲写得很清楚。")
    check("去AI味：工程词是 blocking", result["blocking"] >= 1, True)

    clean = "人物A推开门，风灌进来。他把灯放在桌上，坐了一会儿。"
    check("去AI味：干净文本零 blocking",
          deslop.detect(clean)["blocking"], 0)

    kinds = {i["kind"] for i in deslop.detect("他走了")["issues"]}
    check("去AI味：结尾无标点命中", "no-terminal-punct" in kinds, True)


def _all_criteria(evidence: str, points: float = 5.0) -> list[dict]:
    return [{"criterion": c, "verdict": "pass", "earned": points,
             "evidence": evidence} for c in review.CRITERIA]


def test_review() -> None:
    content = "他抬头看见配角。" + "没有风。" * 80
    payload = {"criteria": _all_criteria("他抬头看见配角。"),
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

    good = {"criteria": _all_criteria("他抬头看见配角。"), "findings": [
        {"severity": "S2", "category": "prose", "evidence": "他抬头看见配角。",
         "issue": "开场太直"}]}
    result = review.normalize_review(good, content)
    kept = [f for f in result["findings"]
            if f["evidence"] == "他抬头看见配角。"]
    check("审稿：问题不扣分", result["quality_score"], 100.0)
    check("审稿：问题带偏移", kept[0]["location"], "0-8")

    bad = {"criteria": _all_criteria("他抬头看见配角。"), "findings": [
        {"severity": "S1", "category": "canon", "evidence": "不存在的句子",
         "issue": "矛盾"}]}
    result = review.normalize_review(bad, content)
    check("审稿：编造的引文被作废", result["findings"], [])
    check("审稿：作废有记录", len(result["rejected"]), 1)


def test_goals() -> None:
    contract = {"must_hit_now": ["把信交给沈砚"],
                "must_preserve": ["沈砚不知道身世"]}
    sources = ["【第3章必现】雨停之前离开码头",
               "【第2章必现】这一条不属于本章"]
    frozen = goals.freeze_goals(contract, 3, author_sources=sources)
    check("目标清单：三项来源齐全",
          [i["source"] for i in frozen["items"]],
          ["contract.must_hit_now", "contract.must_preserve", "author.marker"])
    check("目标清单：必现标记按章过滤",
          frozen["items"][2]["text"], "雨停之前离开码头")
    check("目标清单：coverage 完整", frozen["coverage"], "complete")
    has("目标清单：提示词带三态合同", "goalReviews",
        goals.render_prompt_block(frozen))
    empty = goals.freeze_goals({}, 3)
    check("目标清单：没有条目是 not_configured",
          (empty["coverage"], goals.render_prompt_block(empty)),
          ("not_configured", ""))

    content = "他走到桌前。\n\n他把信交给沈砚。\n\n沈砚没有问。"
    raw = [
        {"id": "ch3:must_hit:1", "status": "completed",
         "description": "本章交付", "evidence": [{"quote": "他把信交给沈砚。"}]},
        {"id": "ch3:preserve:1", "status": "completed",
         "description": "没有说破", "evidence": [{"quote": "沈砚没有问。"}]},
        {"id": "ch3:mustShow:1", "status": "unknown",
         "description": "未提及雨", "evidence": []},
    ]
    result = goals.normalize_goal_reviews(raw, frozen, content)
    check("目标核对：unknown 压过 completed",
          result["overall"], "unknown")
    check("目标核对：证据偏移落位",
          result["items"][0]["evidence"][0]["start"], 8)
    has("目标核对：摘要一行", "完成 2 / 未达成 0",
        goals.summary_line(result))

    # unmet 压过 unknown。
    raw[2] = {"id": "ch3:mustShow:1", "status": "unmet",
              "description": "雨停之前没走", "evidence": [{"quote": "沈砚没有问。"}]}
    result = goals.normalize_goal_reviews(raw, frozen, content)
    check("目标核对：unmet 压过 unknown", result["overall"], "unmet")

    # 证据不在正文 → 降级 unknown，且 coverage 标 unknown。
    bad = [dict(raw[0]), dict(raw[1]),
           {"id": "ch3:mustShow:1", "status": "completed",
            "description": "雨停之前没走", "evidence": [{"quote": "雨停了。"}]}]
    result = goals.normalize_goal_reviews(bad, frozen, content)
    check("目标核对：无效证据降级 unknown",
          result["items"][2]["status"], "unknown")
    check("目标核对：coverage 标记不完整", result["coverage"], "unknown")

    # 引文出现多次 → 不算唯一证据。
    twice = "他走到桌前。\n\n他走到桌前。"
    dup = goals.normalize_goal_reviews(
        [{"id": "ch3:must_hit:1", "status": "completed",
          "description": "重复", "evidence": [{"quote": "他走到桌前。"}]}],
        frozen, twice)
    check("目标核对：非唯一引文不算证据",
          dup["items"][0]["status"], "unknown")


def test_plan_first() -> None:
    """短细纲：先计划后正文，计划块标明「不是事实」，空细纲整轮失败。"""
    from writegraph import outline
    has("细纲：计划块带身份声明", "计划，不是事实依据",
        outline.render_block("1. 交信"))
    raises("细纲：空细纲报错", ValueError,
           lambda: outline.render_block("   "))

    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    base = Path(__file__).resolve().parent.parent
    svc = WritingService(SimpleNamespace(project_dir=base), db=db)
    work = db.create_work("细纲书")
    chapter_id = db.add_chapter(work, 1)
    db.set_contract(chapter_id, purpose="交信", target_chars=300)

    calls: list[tuple[str, str]] = []

    class Fake:
        config = SimpleNamespace(model="fake")

        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario="", max_tokens=None):
            calls.append((scenario, messages[0]["content"]))
            if scenario == "writing.outline":
                yield "1. 目标：把信交给沈砚。\n2. 结尾：沈砚收下信。", [], None
            else:
                yield "他把信交给沈砚。" * 20, [], None

    svc.api_client = Fake()
    events = []

    async def run() -> None:
        async for event in svc.stream_chapter(work, 1, plan_first=True):
            events.append(event)

    asyncio.run(run())
    check("细纲：先计划再正文",
          [c[0] for c in calls], ["writing.outline", "writing.draft"])
    has("细纲：正文请求带计划块",
        "【生成短细纲：计划，不是事实依据】", calls[1][1])
    stages = [e["data"].get("stage") for e in events if e["event"] == "stage"]
    check("细纲：事件流报出 outlined", "outlined" in stages, True)

    class EmptyOutline(Fake):
        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario="", max_tokens=None):
            if scenario == "writing.outline":
                yield "   ", [], None
            else:
                yield "正文。", [], None

    svc.api_client = EmptyOutline()
    raises("细纲：空返回整轮失败", RuntimeError,
           lambda: asyncio.run(_drain(svc, work)))


async def _drain(svc, work) -> None:
    async for _event in svc.stream_chapter(work, 1, plan_first=True):
        pass


def test_review_goal_wiring() -> None:
    """审稿链路真的把冻结清单送进提示词，并把三态结果带回工具。"""
    import json as _json
    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    base = Path(__file__).resolve().parent.parent
    svc = WritingService(SimpleNamespace(project_dir=base), db=db)
    work = db.create_work("目标书")
    chapter_id = db.add_chapter(work, 1)
    db.set_contract(chapter_id, purpose="交信",
                    must_hit_now=["把信交给沈砚"])
    svc.save_content(work, 1,
                     "他走到桌前。\n\n他把信交给沈砚。\n\n沈砚没有问。")
    svc.docs_create(work, "设定/人物.md", content="【第1章必现】雨停之前离开码头")

    captured: dict = {}

    class FakeReview:
        config = SimpleNamespace(model="fake")

        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario="", max_tokens=None):
            captured["user"] = messages[0]["content"]
            yield _json.dumps({
                "criteria": [], "findings": [],
                "goalReviews": [
                    {"id": "ch1:must_hit:1", "status": "completed",
                     "description": "本章交付",
                     "evidence": [{"quote": "他把信交给沈砚。"}]},
                    {"id": "ch1:mustShow:1", "status": "unknown",
                     "description": "没提到雨", "evidence": []},
                ],
            }, ensure_ascii=False), [], None

    svc.api_client = FakeReview()
    out = asyncio.run(svc.review_chapter(work, 1))
    has("审稿接线：清单进提示词", "本章目标逐项核对", captured.get("user", ""))
    has("审稿接线：必现标记进清单", "雨停之前离开码头", captured.get("user", ""))
    check("审稿接线：三态结果带回",
          out["goal_review"]["overall"], "unknown")
    check("审稿接线：摘要一行", out["goal_summary"],
          "目标核对：完成 1 / 未达成 0 / 待核实 1")


def test_revision() -> None:
    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    w = db.create_work("修订")
    ch = db.add_chapter(w, 1)
    text = "人物A走进码头。" + "夜色很沉。" * 20
    db.save_draft(w, ch, text)

    rev = db.create_revision(ch, run_id=None,
                             anchor_quote="人物A走进码头",
                             replacement="人物A走进城门")
    check("修订：提案待确认", rev["status"], "proposed")
    raises("修订：锚点不唯一被拒", ValueError,
           lambda: db.create_revision(ch, run_id=None,
                                      anchor_quote="夜色很沉",
                                      replacement="x"))
    raises("修订：乐观锁拒绝过期应用", ValueError,
           lambda: db.apply_revision(rev["id"], expected_hash="wrong"))

    out = db.apply_revision(rev["id"])
    check("修订：应用后正文替换", "人物A走进城门" in out["content"], True)
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
    check("对话工具：二十个已注册（含 self_explain / writing_materials / Agent / 模式）",
          len(names), 20)
    check("对话工具：含素材侦察子代理", "Agent" in names, True)
    check("对话工具：含模式切换", "writing_set_mode" in names, True)
    check("对话工具：含素材库", "writing_materials" in names, True)
    check("对话工具：含 git 与反馈",
          {"writing_git", "writing_git_change", "writing_report",
           "writing_references"} <= set(names), True)
    check("对话工具：含自我说明", "self_explain" in names, True)
    out = _json.loads(reg.execute("writing_create_work",
                                  _json.dumps({"title": "长夜"})))
    check("对话工具：建作品", out["created"], True)
    check("对话工具：现场记住作品", bool(ctx["work_id"]), True)

    out = _json.loads(reg.execute("writing_create_chapter", _json.dumps({
        "title": "渡口", "purpose": "过河", "must_hit_now": "上船\n见配角"})))
    check("对话工具：建章自动带合同", out["contract"]["must_hit_now"],
          ["上船", "见配角"])
    check("对话工具：现场记住章", ctx["chapter_index"], 1)
    chapter_id = out["contract"]["chapter_id"]

    db.save_draft(ctx["work_id"], chapter_id, "人物A上了船。风很冷。")
    out = _json.loads(reg.execute("writing_replace_text", _json.dumps({
        "anchor_quote": "风很冷", "replacement": "风从船头灌进来"})))
    check("对话工具：唯一锚点替换", out["word_count"], 15)
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
                         scenario="", max_tokens=None):
            for part in ("人物A上了船。", "风从船头灌进来。"):
                yield part, [], None

    svc.api_client = FakeClient()
    reg.execute("writing_create_chapter", _json.dumps(
        {"title": "第二夜", "purpose": "上船"}))
    out = _json.loads(asyncio.run(
        reg.execute_async("writing_draft_chapter",
                          _json.dumps({"chapter_index": 2}))))
    check("对话工具：能写出一章", out["ok"], True)
    check("对话工具：草稿落库", out["word_count"], 15)
    chapter2 = svc.chapter(ctx["work_id"], 2)
    has("对话工具：正文写进库", "风从船头", chapter2["content"])

    prompt = svc.agent_prompt(ctx["work_id"], 1)
    has("写作提示词：带现场书名", "长夜", prompt)
    has("写作提示词：带当前章", "第1章", prompt)
    has("写作提示词：禁止贴全文", "不要", prompt)

    out = _json.loads(reg.execute("writing_status", "{}"))
    check("对话工具：现状列出章节", out["current"]["chapters"][0]["title"],
          "渡口")


def test_draft_budget_retry() -> None:
    """撞 max_tokens 时按预算翻倍重来，而不是把半截正文当成品。"""
    import json as _json
    from runtime import StreamTruncated
    from tools import ToolRegistry
    from writegraph.chat_tools import build_writing_registry

    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    svc = WritingService(SimpleNamespace(
        project_dir=Path(__file__).resolve().parent.parent), db=db)
    ctx: dict = {"work_id": "", "chapter_index": 0}
    reg = build_writing_registry(ToolRegistry(), svc, ctx)
    reg.execute("writing_create_work", _json.dumps({"title": "预算"}))
    reg.execute("writing_create_chapter", _json.dumps(
        {"title": "第一章", "purpose": "试预算", "target_chars": 2500}))

    class TruncatingClient:
        config = SimpleNamespace(model="fake-model")

        def __init__(self) -> None:
            self.budgets: list = []

        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario="", max_tokens=None):
            self.budgets.append(max_tokens)
            if len(self.budgets) == 1:
                yield "半截正文。" * 3, [], None
                raise StreamTruncated(scenario=scenario, chars=15)
            yield "完整正文。" * 3, [], None

    client = TruncatingClient()
    svc.api_client = client
    out = _json.loads(asyncio.run(reg.execute_async(
        "writing_draft_chapter", _json.dumps({"chapter_index": 1}))))
    check("截断：重试一次", len(client.budgets), 2)
    check("截断：第一次带预算", (client.budgets[0] or 0) >= 4096, True)
    check("截断：重试抬高预算",
          (client.budgets[1] or 0) > (client.budgets[0] or 0), True)
    check("截断：留下的是完整稿", out["ok"], True)
    chapter = svc.chapter(ctx["work_id"], 1)
    has("截断：半截正文没有落库", "完整正文", chapter["content"])
    check("截断：半截正文被丢弃", "半截" in chapter["content"], False)


def test_settle_context() -> None:
    """结算上下文必须带「核心角色状态」——settle.md 要求 subject_ref 只能
    填这里的 id，以前这段根本没装配，模型只能自造（18:55 的 tmp-char-1）。"""
    import json as _json
    from tools import ToolRegistry
    from writegraph.chat_tools import build_writing_registry

    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    svc = WritingService(SimpleNamespace(
        project_dir=Path(__file__).resolve().parent.parent), db=db)
    ctx: dict = {"work_id": "", "chapter_index": 0}
    seen: list[dict] = []

    async def progress(event: dict) -> None:
        seen.append(event)

    reg = build_writing_registry(ToolRegistry(), svc, ctx, progress=progress)
    reg.execute("writing_create_work", _json.dumps({"title": "结算"}))
    reg.execute("writing_create_chapter", _json.dumps(
        {"title": "第一章", "purpose": "推进"}))
    chapter = svc.chapter(ctx["work_id"], 1)
    db.save_draft(ctx["work_id"], chapter["id"], "人物A到了码头。", kind="save")

    empty_delta = _json.dumps({
        "chapter_summary": "人物A到码头", "key_events": [], "fact_ops": [],
        "promise_ops": [], "entity_ops": [], "unknown_claims": [],
        "tension": 50, "tension_note": "日常"})

    class Client:
        config = SimpleNamespace(model="fake")

        def __init__(self, payload: str) -> None:
            self.payload = payload
            self.users: list[str] = []

        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario="", max_tokens=None):
            self.users.append(str(messages[-1].get("content") or ""))
            yield self.payload, [], None

    client = Client(empty_delta)
    svc.api_client = client
    out = _json.loads(asyncio.run(reg.execute_async(
        "writing_settle_chapter", "{}")))
    check("结算：空账本也能结算", out["ok"], True)
    has("结算：上下文带核心角色状态", "## 核心角色状态", client.users[-1])
    has("结算：空账本明说 entity_ops", "还没有任何主体", client.users[-1])
    check("结算：发了 settling 进度",
          any(e.get("data", {}).get("stage") == "settling" for e in seen), True)
    check("结算：进度标明是结算工具",
          any(e.get("tool") == "writing_settle_chapter" for e in seen), True)

    # 账本里已经有主体：上下文要把它列成可用 id。
    db.apply_delta(ctx["work_id"], chapter["id"], 1, delta={
        "fact_ops": [{"op": "set", "subject_type": "character",
                      "subject_ref": "人物A", "field": "location",
                      "value": "码头", "since_chapter": 1,
                      "evidence_text": "人物A到了码头"}]})
    client2 = Client(_json.dumps({
        "chapter_summary": "人物A在码头等", "key_events": ["到码头"],
        "fact_ops": [{"op": "set", "subject_type": "character",
                      "subject_ref": "人物A", "field": "goal",
                      "value": "找到配角", "since_chapter": 1,
                      "evidence_text": "人物A到了码头"}],
        "promise_ops": [], "entity_ops": [], "unknown_claims": [],
        "tension": 40, "tension_note": "等待"}))
    svc.api_client = client2
    out = _json.loads(asyncio.run(reg.execute_async(
        "writing_settle_chapter", "{}")))
    check("结算：有账本时正常入账", out["ok"], True)
    has("结算：列出可用 id", "人物A", client2.users[-1])
    has("结算：带当前字段值", "location：码头", client2.users[-1])

    # 版本习惯（决策 182）：默认每章结算后自动提交；改成 manual 后不再提交，
    # 工作区保持脏，结算结果明确告诉调用方「没提交」。
    check("习惯：默认为每章自动", svc.git_habit(ctx["work_id"]), "chapter")
    before = len(svc.vcs_log(ctx["work_id"]))
    db.save_draft(ctx["work_id"], chapter["id"], "人物A还在码头。", kind="save")
    svc.api_client = Client(empty_delta)
    out = _json.loads(asyncio.run(reg.execute_async(
        "writing_settle_chapter", "{}")))
    check("习惯：默认结算会提交", out["git"]["committed"], True)
    check("习惯：默认提交数增加", len(svc.vcs_log(ctx["work_id"])), before + 1)

    bad = reg.execute("writing_git_change", _json.dumps(
        {"action": "set_habit", "habit": "weird"}))
    has("习惯：非法值被拒", "未知版本习惯", bad)
    habit = _json.loads(asyncio.run(reg.execute_async(
        "writing_git_change", _json.dumps(
            {"action": "set_habit", "habit": "manual"}))))
    check("习惯：工具改 manual", habit["git_habit"], "manual")
    check("习惯：工具返回中文行", habit["label"], "只在作者要求时存档")
    has("习惯：提示词带一行", "版本习惯：只在作者要求时存档",
        svc.agent_prompt(ctx["work_id"], 1))

    before = len(svc.vcs_log(ctx["work_id"]))
    db.save_draft(ctx["work_id"], chapter["id"], "人物A仍留在码头。", kind="save")
    svc.api_client = Client(empty_delta)
    out = _json.loads(asyncio.run(reg.execute_async(
        "writing_settle_chapter", "{}")))
    check("习惯：manual 结算不提交", out["git"]["committed"], False)
    check("习惯：manual 提交数不变", len(svc.vcs_log(ctx["work_id"])), before)
    check("习惯：manual 工作区仍脏", svc.vcs_status(ctx["work_id"])["clean"],
          False)


def test_contract_lifecycle() -> None:
    """合同是验收单：写之前全文进提示词，结算后结清、只留一行。"""
    import json as _json
    from tools import ToolRegistry
    from writegraph.chat_tools import build_writing_registry

    db = WritingDB(os.path.join(tempfile.mkdtemp(), "w.db"))
    svc = WritingService(SimpleNamespace(
        project_dir=Path(__file__).resolve().parent.parent), db=db)
    ctx: dict = {"work_id": "", "chapter_index": 0}
    reg = build_writing_registry(ToolRegistry(), svc, ctx)
    reg.execute("writing_create_work", _json.dumps({"title": "验收"}))
    out = _json.loads(reg.execute("writing_create_chapter", _json.dumps({
        "title": "码头", "purpose": "找到配角",
        "must_hit_now": "抵达码头\n与配角见面"})))
    check("合同：新建即在写", out["contract"]["status"], "active")
    chapter = svc.chapter(ctx["work_id"], 1)
    prompt = svc.agent_prompt(ctx["work_id"], 1)
    has("合同：在写时全文进提示词", "必须兑现：抵达码头；与配角见面", prompt)

    db.save_draft(ctx["work_id"], chapter["id"],
                  "人物A把车停在码头。他四处张望，没有看到配角。", kind="save")

    class Client:
        config = SimpleNamespace(model="fake")

        async def stream(self, system, messages, tools, cancel_event=None,
                         scenario="", max_tokens=None):
            yield _json.dumps({
                "chapter_summary": "到码头没见到配角",
                "key_events": ["到码头"], "fact_ops": [], "promise_ops": [],
                "entity_ops": [], "unknown_claims": [], "tension": 40,
                "tension_note": "落空"}), [], None

    svc.api_client = Client()
    out = _json.loads(asyncio.run(reg.execute_async(
        "writing_settle_chapter", _json.dumps({
            "outcomes": "hit|抵达码头|人物A把车停在码头\n"
                        "miss|与配角见面|这一章没见到配角",
            "note": "配角推到下一章"}))))
    check("合同：结算成功", out["ok"], True)
    check("合同：结算后结清", out["contract"]["status"], "closed")
    check("合同：兑现盘点", out["contract"]["stats"]["hit"], 1)
    check("合同：未兑现计数", out["contract"]["stats"]["miss"], 1)

    prompt = svc.agent_prompt(ctx["work_id"], 1)
    has("合同：结清后只留一行", "第1章合同已结清", prompt)
    check("合同：结清后不再全文注入", "必须兑现：抵达码头" in prompt, False)

    rows = svc.list_contracts(ctx["work_id"])
    check("合同：收录带状态", rows[0]["status"], "closed")
    check("合同：收录带兑现行", rows[0]["outcomes"][0]["verdict"], "hit")

    # 下一章的现场要提示上一章没兑现的那条。
    reg.execute("writing_create_chapter", _json.dumps(
        {"title": "再访", "purpose": "见到配角"}))
    prompt = svc.agent_prompt(ctx["work_id"], 2)
    has("合同：下一章看到上一章欠账", "上一章合同未兑现：与配角见面", prompt)

    # 改合同 = 重新立约：状态回到在写，旧兑现记录作废。
    svc.set_contract(chapter["id"], purpose="改成在码头遇见配角")
    row = svc.contract(chapter["id"])
    check("合同：改约回到在写", row["status"], "active")
    check("合同：改约清掉旧兑现", row["outcomes"], [])


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
                         scenario="", max_tokens=None):
            yield "<think>先想好再写</think>", [], None
            yield "人物A上了船。", [], None

    svc.api_client = ThinkClient()
    # 这条用例钉的是「一次草稿只记一轮」；自动修订会合法地多一轮。
    db.update_work_style_profile(work, {"auto_repair": False})

    async def run():
        return [e async for e in svc.stream_chapter(work, 1)]

    events = asyncio.run(run())
    deltas = "".join(e["data"]["text"] for e in events
                     if e["event"] == "delta")
    check("录音：流式不泄漏思维链", deltas, "人物A上了船。")
    saved = svc.chapter(work, 1)["content"]
    has("录音：落库正文不含思维链", "人物A上了船", saved)
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
                         scenario="", max_tokens=None):
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
    check("录音：结构化调用也剥思维链", out, "人物A上了船。")


def test_withhold_migration() -> None:
    """v11 旧库补 ``withhold`` 列：ALTER 之后已有合同行必须还在。"""
    path = os.path.join(tempfile.mkdtemp(), "w.db")
    db = WritingDB(path)
    work = db.create_work("旧库")
    chapter = db.add_chapter(work, 1)
    db.set_contract(chapter, purpose="推进", forbidden_cross=["提前揭示"])
    conn = db.get_conn()
    conn.execute("ALTER TABLE chapter_contracts DROP COLUMN withhold")
    # 迁移引擎按 MAX(version) 推进：删掉最新一条及以后，才会重放 v12。
    conn.execute("DELETE FROM schema_version WHERE version>=12")
    conn.commit()
    db.close()

    db2 = WritingDB(path)
    contract = db2.get_contract(chapter)
    check("迁移 v12：旧合同还在", contract.get("purpose"), "推进")
    check("迁移 v12：withhold 默认空", contract.get("withhold"), [])
    db2.set_contract(chapter, withhold=["不点破身世"])
    check("迁移 v12：可写 withhold",
          db2.get_contract(chapter).get("withhold"), ["不点破身世"])


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
    test_goals()
    test_plan_first()
    test_review_goal_wiring()
    test_revision()
    test_map_tension()
    test_reference_diag()
    test_chat_tools()
    test_draft_budget_retry()
    test_settle_context()
    test_contract_lifecycle()
    test_withhold_migration()
    test_flight_recorder()
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
