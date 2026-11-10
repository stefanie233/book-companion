"""写作 git 工具 + 自测清单的测试（零 LLM）。

盯三件事：

1. agent 真的够得着这些工具：三个工具都注册了，action 覆盖指令表；
2. 工具壳没有把服务语义弄丢：提交、分支、合并冲突、撤销、回滚、暂存、标签；
3. 自测清单可执行：有步骤、有通过标准，selftest_seed 能建出固定种子；
   问题日志能写、能列、能关单。

跑法：``python scripts/test_writing_git_tools.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.writing_service import WritingError, WritingService  # noqa: E402
from storage.writing_db import WritingDB                       # noqa: E402
from tools import ToolRegistry, ToolSpec                       # noqa: E402
from writegraph.chat_tools import build_writing_registry       # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def _setup():
    db = WritingDB(os.path.join(tempfile.mkdtemp(prefix="wgtool_"), "w.db"))
    svc = WritingService(SimpleNamespace(project_dir=ROOT), db=db)
    ctx: dict = {}
    registry = build_writing_registry(ToolRegistry(), svc, ctx)
    return svc, registry, ctx


def _call(registry, tool: str, **args):
    return json.loads(registry.execute(tool, json.dumps(args)))


def test_registry_surface() -> None:
    svc, registry, ctx = _setup()
    for name in ("writing_git", "writing_git_change", "writing_report"):
        spec = registry.get(name)
        check(f"注册：{name}", spec is not None, True)
        if spec is not None:
            check(f"注册：{name} 属于写作工具集",
                  spec.toolset, "writing")
    git_props = registry.get("writing_git").parameters["properties"]
    git_actions = set(git_props["action"]["enum"])
    check("只读工具覆盖查询动作",
          {"status", "log", "diff", "reflog", "branches", "tags",
           "stash_list", "selftest_plan", "selftest_seed"} <= git_actions,
          True)
    check("清单支持按阶段/步骤取", {"phase", "step"} <= set(git_props), True)
    change_actions = set(registry.get("writing_git_change").parameters[
        "properties"]["action"]["enum"])
    check("变更工具覆盖指令表",
          {"commit", "amend", "branch_create", "branch_switch",
           "branch_rename", "branch_delete", "merge", "revert",
           "cherry_pick", "restore", "discard_chapter", "tag_create",
           "tag_delete", "stash_push", "stash_pop", "stash_drop"}
          <= change_actions, True)
    check("未知 action 返回错误而不是抛异常",
          _call(registry, "writing_git", action="nope").get("ok"), False)

    # 写作模式不再携带平台全量工具：只留写作现场的十几个 schema。
    base = ToolRegistry()
    base.register(ToolSpec(
        name="library_list", description="", parameters={},
        execute=lambda **_kw: "{}", toolset="library_query"))
    filtered = build_writing_registry(base, svc, ctx)
    check("写作注册表不带阅读工具", filtered.get("library_list"), None)
    check("写作注册表仍带写作工具",
          filtered.get("writing_git") is not None, True)


def test_selftest_plan_and_seed() -> None:
    svc, registry, ctx = _setup()
    plan = _call(registry, "writing_git", action="selftest_plan")
    check("清单：返回 ok", plan.get("ok"), True)
    steps = plan.get("plan", {}).get("steps") or []
    check("清单：步骤够用", len(steps) >= 15, True)
    check("清单：每步都有 do 和 pass",
          all(step.get("do") and step.get("pass") for step in steps), True)
    check("清单：覆盖四个阶段",
          sorted({step["id"][0] for step in steps}), ["A", "B", "C", "D"])
    d3 = next(step for step in steps if step["id"] == "D3")
    calls = [(d["tool"], (d.get("args") or {}).get("action"))
             for d in d3["do"]]
    conns = [
        i for i, (tool, action) in enumerate(calls)
        if (tool, action) in (("writing_git_change", "branch_rename"),
                              ("writing_git", "branches"),
                              ("writing_git_change", "branch_delete"))
    ]
    check("清单：D3 改名→查 branches→删除，顺序自洽",
          len(conns) >= 3
          and calls[conns[0]][1] == "branch_rename"
          and calls[conns[1]][1] == "branches"
          and calls[conns[2]][1] == "branch_delete", True)

    seed = _call(registry, "writing_git", action="selftest_seed")
    check("种子：建了作品", bool(seed.get("work_id")), True)
    check("种子：三章", len(seed.get("chapters") or []), 3)
    check("种子：固定锚点", seed.get("anchors", {}).get("甲"), "风从码头来")
    check("种子：上下文记住了作品", ctx.get("work_id"), seed.get("work_id"))
    detail = svc.work_detail(seed["work_id"])
    check("详情：带卷目录", isinstance(detail.get("volumes"), list), True)
    check("详情：每章带场景层",
          all("scenes" in c for c in detail.get("chapters") or []), True)
    first_chapter = (detail.get("chapters") or [{}])[0]
    scene_id = "sc-tree-check"
    svc.db.get_conn().execute(
        "INSERT INTO scenes (id, chapter_id, scene_index, title, status)"
        " VALUES (?,?,?,?,?)",
        (scene_id, first_chapter["id"], 1, "码头开场", "planned"))
    svc.db.get_conn().commit()
    detail = svc.work_detail(seed["work_id"])
    scenes = (detail.get("chapters") or [{}])[0].get("scenes") or []
    check("详情：场景挂在所属章下",
          [s.get("title") for s in scenes], ["码头开场"])
    status = _call(registry, "writing_git", action="status")
    check("种子：基线干净", status.get("clean"), True)
    log = _call(registry, "writing_git", action="log")
    check("种子：基线一条提交", len(log.get("commits") or []), 1)

    cleaned = _call(registry, "writing_git", action="selftest_cleanup")
    check("清理：删除自测作品", cleaned.get("ok"), True)
    check("清理：上下文清空", ctx.get("work_id"), None)
    check("清理：作品不在列表",
          any(w["id"] == seed["work_id"] for w in svc.list_works()), False)

    normal = svc.create_work("正常作品")
    ctx["work_id"] = normal
    refused = _call(registry, "writing_git", action="selftest_cleanup")
    check("清理：非自测作品拒绝", refused.get("ok"), False)
    check("清理：正常作品还在", svc.db.get_work(normal) is not None, True)


def test_commit_log_amend_via_tools() -> None:
    svc, registry, ctx = _setup()
    seed = _call(registry, "writing_git", action="selftest_seed")
    work = seed["work_id"]

    rep = _call(registry, "writing_replace_text", chapter_index=1,
                anchor_quote="风从码头来", replacement="风从码头来（改）")
    check("改稿：工具成功", rep.get("ok"), True)
    dirty = _call(registry, "writing_git", action="status")
    check("改稿：状态变脏", dirty.get("clean"), False)
    check("瘦身：HEAD 不带树/账本哈希",
          "tree_hash" not in (dirty.get("head") or {}), True)
    changed = (dirty.get("changes") or {}).get("changed") or [{}]
    check("瘦身：改动不带完整前后快照",
          "before" not in changed[0] and "after" not in changed[0], True)
    log_now = _call(registry, "writing_git", action="log")
    check("瘦身：历史提交不带哈希",
          all("tree_hash" not in c and "ledger_hash" not in c
              for c in log_now.get("commits") or []), True)

    committed = _call(registry, "writing_git_change", action="commit",
                      work_id=work, message="改第一章开头")
    check("提交：成功", committed.get("ok"), True)
    check("提交：说明正确", committed["commit"]["message"], "改第一章开头")

    hist = _call(registry, "writing_git", action="log", chapter_index=1)
    check("单章历史：两条", len(hist.get("commits") or []), 2)
    other = _call(registry, "writing_git", action="log", chapter_index=3)
    check("单章历史：没改的章只有基线", len(other.get("commits") or []), 1)

    amended = _call(registry, "writing_git_change", action="amend",
                    message="改第一章开头（补）")
    check("修正：成功", amended.get("ok"), True)
    hist2 = _call(registry, "writing_git", action="log")
    check("修正：历史仍是两条", len(hist2.get("commits") or []), 2)
    reflog = _call(registry, "writing_git", action="reflog")
    check("修正：reflog 有 amend",
          any(e["reason"] == "amend" for e in reflog.get("entries") or []),
          True)


def test_branch_diff_and_conflict() -> None:
    svc, registry, ctx = _setup()
    seed = _call(registry, "writing_git", action="selftest_seed")
    work = seed["work_id"]

    created = _call(registry, "writing_git_change", action="branch_create",
                    name="岔路A", switch=True)
    check("分支：建好就切换", created.get("ok"), True)
    _created_status = created.get("status") or {}
    check("分支：建好就切换后 head.branch 一致",
          (_created_status.get("head") or {}).get("branch"),
          _created_status.get("branch"))
    status = _call(registry, "writing_git", action="status")
    check("分支：当前是岔路A", status.get("branch"), "岔路A")

    _call(registry, "writing_replace_text", chapter_index=1,
          anchor_quote="风从码头来", replacement="岔路版")
    _call(registry, "writing_git_change", action="commit", message="岔路改")
    switched = _call(registry, "writing_git_change", action="branch_switch",
                     name="main")
    check("分支：切回 main 后 head.branch 一致",
          (switched.get("head") or {}).get("branch"),
          switched.get("branch"))
    chapter = _call(registry, "writing_read_chapter", chapter_index=1)
    check("分支：切回主线正文回退", "岔路版" not in chapter.get("content", ""),
          True)

    diff = _call(registry, "writing_git", action="diff",
                 from_ref="main...岔路A")
    check("三点差异：一章", (diff.get("chapter_counts") or {}).get("changed"), 1)
    _preview = (diff.get("previews") or [{}])[0]
    check("三点差异：带目标侧正文预览",
          "岔路版" in (_preview.get("head") or ""), True)

    _call(registry, "writing_replace_text", chapter_index=1,
          anchor_quote="风从码头来", replacement="主线版")
    _call(registry, "writing_git_change", action="commit", message="主线改")
    conflict = _call(registry, "writing_git_change", action="merge",
                     branch="岔路A")
    check("合并：返回需要裁决", conflict.get("needs_resolution"), True)
    check("合并：冲突定位到章",
          (conflict.get("conflicts") or [{}])[0].get("kind"), "chapter")
    check("合并：工作区没被改脏",
          (conflict.get("status") or {}).get("clean"), True)
    key = conflict["conflicts"][0]["key"]
    resolved = _call(registry, "writing_git_change", action="merge",
                     branch="岔路A", resolutions={key: "theirs"})
    check("合并：裁决后成功", bool(resolved.get("commit")), True)
    check("合并：预览取到岔路版本",
          any("岔路版" in (c.get("head") or "")
              for c in resolved.get("chapters") or []), True)


def test_cherry_pick_revert_restore_via_tools() -> None:
    svc, registry, ctx = _setup()
    seed = _call(registry, "writing_git", action="selftest_seed")

    _call(registry, "writing_git_change", action="branch_create",
          name="岔路A", switch=True)
    _call(registry, "writing_replace_text", chapter_index=1,
          anchor_quote="风从码头来", replacement="岔路甲")
    _call(registry, "writing_git_change", action="commit", message="岔路一改")
    all_log = _call(registry, "writing_git", action="log", all_branches=True)
    picked = next(c for c in all_log["commits"] if c["message"] == "岔路一改")
    _call(registry, "writing_git_change", action="branch_switch", name="main")

    picked_res = _call(registry, "writing_git_change", action="cherry_pick",
                       commit_id=picked["id"])
    check("摘取：成功", picked_res.get("ok"), True)
    check("摘取：类型正确", picked_res["commit"]["kind"], "cherry-pick")
    check("摘取：预览正文过来",
          any("岔路甲" in (c.get("head") or "")
              for c in picked_res.get("chapters") or []), True)
    again = _call(registry, "writing_git_change", action="cherry_pick",
                  commit_id=picked["id"])
    check("摘取：重复被拦住", again.get("ok"), False)

    _call(registry, "writing_replace_text", chapter_index=2,
          anchor_quote="配角没有抬头", replacement="要撤销的戊")
    _call(registry, "writing_git_change", action="commit", message="要撤销")
    head = _call(registry, "writing_git", action="log", limit=1)["commits"][0]
    reverted = _call(registry, "writing_git_change", action="revert",
                     commit_id=head["id"])
    check("撤销：成功", reverted.get("ok"), True)
    check("撤销：类型正确", reverted["commit"]["kind"], "revert")
    check("撤销：带改动章预览", bool(reverted.get("chapters")), True)
    check("撤销：预览正文回退",
          all("要撤销的戊" not in ((c.get("head") or "")
                                   + (c.get("tail") or ""))
              for c in reverted.get("chapters") or []), True)
    twice = _call(registry, "writing_git_change", action="revert",
                  commit_id=head["id"])
    check("撤销：重复被拦住", twice.get("ok"), False)

    current = _call(registry, "writing_git", action="log", limit=1)["commits"][0]
    _call(registry, "writing_replace_text", chapter_index=3,
          anchor_quote="雨落在篷布上", replacement="雨落在篷布上（坏）")
    _call(registry, "writing_git_change", action="commit", message="写坏了")
    restored = _call(registry, "writing_git_change", action="restore",
                     commit_id=current["id"])
    check("回滚：成功", restored.get("ok"), True)
    check("回滚：带改动章预览", bool(restored.get("chapters")), True)
    check("回滚：预览正文回到目标",
          all("（坏）" not in ((c.get("head") or "") + (c.get("tail") or ""))
              for c in restored.get("chapters") or []), True)
    twice = _call(registry, "writing_git_change", action="restore",
                  commit_id=current["id"])
    check("回滚：已在目标上被拦住", twice.get("ok"), False)


def test_discard_stash_tag_via_tools() -> None:
    svc, registry, ctx = _setup()
    _call(registry, "writing_git", action="selftest_seed")

    _call(registry, "writing_replace_text", chapter_index=3,
          anchor_quote="他把信折好", replacement="他把信折好（乱改）")
    dirty = _call(registry, "writing_git", action="status")
    check("丢弃前：脏", dirty.get("clean"), False)
    discarded = _call(registry, "writing_git_change", action="discard_chapter",
                      chapter_index=3)
    check("丢弃：成功", discarded.get("ok"), True)
    preview = discarded.get("chapter") or {}
    check("丢弃：返回章节预览", "乱改" not in (preview.get("head") or ""), True)
    check("丢弃：预览是回退后的正文",
          "他把信折好" in (preview.get("head") or ""), True)

    _call(registry, "writing_replace_text", chapter_index=2,
          anchor_quote="船期写在木牌上", replacement="船期写在木牌上（暂存）")
    stashed = _call(registry, "writing_git_change", action="stash_push",
                    message="自测暂存")
    check("暂存：成功", stashed.get("ok"), True)
    check("暂存：返回干净现场",
          (stashed.get("status") or {}).get("clean"), True)
    stashes = _call(registry, "writing_git", action="stash_list")["stashes"]
    check("暂存：列表一条", len(stashes), 1)
    name = stashes[0]["name"]

    _call(registry, "writing_replace_text", chapter_index=2,
          anchor_quote="配角没有抬头", replacement="暂存之后改的")
    _call(registry, "writing_git_change", action="commit", message="暂存后又改")
    popped = _call(registry, "writing_git_change", action="stash_pop",
                   name=stashes[0]["label"])
    check("暂存恢复：成功", popped.get("ok"), True)
    text = "".join((c.get("head") or "") + (c.get("tail") or "")
                   for c in popped.get("chapters") or [])
    check("暂存恢复：预览里有补丁", "（暂存）" in text, True)
    check("暂存恢复：预览里没吃掉后来的提交",
          "暂存之后改的" in text, True)

    tagged = _call(registry, "writing_git_change", action="tag_create",
                   name="v1自测")
    check("标签：成功", tagged.get("ok"), True)
    tags = _call(registry, "writing_git", action="tags")["tags"]
    check("标签：列表一条", len(tags), 1)
    _call(registry, "writing_git_change", action="tag_delete", name="v1自测")
    check("标签：删除后为空",
          _call(registry, "writing_git", action="tags")["tags"], [])


def test_checkpoint_branch_delete_guard() -> None:
    """切换分支的自动检查点不能随分支 force 删除静默消失。"""
    svc, registry, ctx = _setup()
    _call(registry, "writing_git", action="selftest_seed")
    _call(registry, "writing_git_change", action="branch_create",
          name="检查点路", switch=True)
    # 先让分支和 main 分开：否则切回 main 时两边指向同一提交，checkout
    # 不会触发自动检查点，测不到这次要守的路径。
    _call(registry, "writing_replace_text", chapter_index=3,
          anchor_quote="他把信折好", replacement="检查点路先提交")
    _call(registry, "writing_git_change", action="commit",
          message="检查点路先提交")
    _call(registry, "writing_replace_text", chapter_index=1,
          anchor_quote="风从码头来", replacement="未保存的改动")
    switched = _call(registry, "writing_git_change", action="branch_switch",
                     name="main")
    check("检查点：切回 main 成功", switched.get("ok"), True)
    deleted = _call(registry, "writing_git_change", action="branch_delete",
                    name="检查点路", force=True)
    check("检查点：force 删除成功", deleted.get("ok"), True)
    check("检查点：保留 lost 标签", deleted.get("recovered_tag"),
          "lost-检查点路")
    tags = _call(registry, "writing_git", action="tags")["tags"]
    lost = [t for t in tags if t.get("name") == "lost-检查点路"]
    check("检查点：标签在列表里", len(lost), 1)
    if lost:
        restored = _call(registry, "writing_git_change", action="restore",
                         commit_id=lost[0]["commit_id"])
        text = "".join((c.get("head") or "") + (c.get("tail") or "")
                       for c in restored.get("chapters") or [])
        check("检查点：标签可恢复未保存改动", "未保存的改动" in text, True)


def test_partial_commit_via_tools() -> None:
    """agent 只提交自己改过的章：作者手改的章留在工作区，不混进这次提交。"""
    svc, registry, ctx = _setup()
    seed = _call(registry, "writing_git", action="selftest_seed")
    work = seed["work_id"]

    _call(registry, "writing_replace_text", chapter_index=1,
          anchor_quote="风从码头来", replacement="风从码头来（作者手改）")
    _call(registry, "writing_replace_text", chapter_index=2,
          anchor_quote="船期写在木牌上", replacement="船期写在木牌上（agent 改）")

    out = _call(registry, "writing_git_change", action="commit",
                work_id=work, message="只提交 agent 改的那章", chapters=[2])
    check("部分提交：成功", out.get("ok"), True)
    check("部分提交：返回剩余状态", bool(out.get("status")), True)
    check("部分提交：只剩一章没提交",
          (out.get("status") or {}).get("chapter_counts", {}).get("changed"), 1)
    still = _call(registry, "writing_git", action="status")
    changed = (still.get("changes") or {}).get("changed") or [{}]
    check("部分提交：留下的是作者那一章",
          changed[0].get("chapter_index"), 1)

    bad = _call(registry, "writing_git_change", action="commit",
                work_id=work, message="没改的章", chapters=[3])
    check("部分提交：选中的章没改动被拒", bad.get("ok"), False)


def test_feedback_log() -> None:
    svc, registry, ctx = _setup()
    _call(registry, "writing_git", action="selftest_seed")

    added = _call(registry, "writing_report", action="add", kind="issue",
                  area="git", severity="high", title="暂存恢复后 diff 不刷新",
                  body="D1 恢复后 status 仍显示旧差异；期望 clean=true")
    check("反馈：写入成功", added.get("ok"), True)
    check("反馈：返回 id", bool(added["feedback"]["id"]), True)
    check("反馈：open 计数", added["open"]["open"], 1)
    check("反馈：高优先级计数", added["open"]["high"], 1)

    listed = _call(registry, "writing_report", action="list", status="open")
    check("反馈：能列出", len(listed.get("items") or []), 1)
    feedback_id = listed["items"][0]["id"]
    resolved = _call(registry, "writing_report", action="resolve",
                     feedback_id=feedback_id, status="fixed")
    check("反馈：能关单", resolved["feedback"]["status"], "fixed")
    check("反馈：关单后 open 为空",
          _call(registry, "writing_report", action="list",
                status="open").get("items"), [])

    bad = _call(registry, "writing_report", action="add", title="")
    check("反馈：空标题被拒", bad.get("ok"), False)
    bad2 = _call(registry, "writing_report", action="resolve",
                 feedback_id="fb-nope", status="fixed")
    check("反馈：不存在的 id 被拒", bad2.get("ok"), False)


def test_phase_plan_and_action_state() -> None:
    """阶段清单 + 变更操作自带的现场/预览（省掉一轮 status/读章）。"""
    svc, registry, ctx = _setup()

    phase = _call(registry, "writing_git", action="selftest_plan", phase="C")
    ids = [s["id"] for s in phase.get("plan", {}).get("steps") or []]
    check("清单：phase 只取 C", ids, ["C1", "C2", "C3", "C4", "C5"])
    check("清单：带阶段名", phase["plan"].get("phase"), "C 撤销与恢复")
    short = _call(registry, "writing_git", action="selftest_plan",
                  phase="基础")
    check("清单：阶段简称也能取",
          [s["id"] for s in short["plan"]["steps"]],
          ["A1", "A2", "A3", "A4", "A5"])
    one = _call(registry, "writing_git", action="selftest_plan", step="C4")
    check("清单：step 只取一步",
          [s["id"] for s in one["plan"]["steps"]], ["C4"])
    bad = _call(registry, "writing_git", action="selftest_plan", phase="Z")
    check("清单：未知阶段报错", bad.get("ok"), False)
    full = _call(registry, "writing_git", action="selftest_plan")
    check("清单：全量仍可取", len(full["plan"]["steps"]), 22)

    seed = _call(registry, "writing_git", action="selftest_seed")
    wid = seed["work_id"]
    _call(registry, "writing_replace_text", chapter_index=1,
          anchor_quote="风从码头来", replacement="风从码头来（改）")
    first = _call(registry, "writing_git_change", action="commit",
                  work_id=wid, message="改第一章开头")
    check("commit：带精简 status", (first.get("status") or {}).get("clean"),
          True)
    chapters = first.get("chapters") or [{}]
    check("commit：带改动章预览", chapters[0].get("chapter_index"), 1)
    check("commit：预览能看到改动",
          "风从码头来（改）" in (chapters[0].get("head") or ""), True)

    _call(registry, "writing_replace_text", chapter_index=1,
          anchor_quote="风从码头来（改）", replacement="风从码头来（再改）")
    second = _call(registry, "writing_git_change", action="commit",
                   work_id=wid, message="再改一次")
    check("commit：parent_id 指向上一次 HEAD",
          second["commit"]["parent_id"], first["commit"]["id"])

    branch = _call(registry, "writing_git_change", action="branch_create",
                   work_id=wid, name="岔路A", switch=True)
    check("branch_create：切过去后带 status",
          (branch.get("status") or {}).get("branch"), "岔路A")

    amended = _call(registry, "writing_git_change", action="amend",
                    work_id=wid, message="改提交说明")
    check("amend：带 status", (amended.get("status") or {}).get("clean"), True)
    check("amend：带改动章预览", bool(amended.get("chapters")), True)

    reflog = _call(registry, "writing_git", action="reflog", work_id=wid)
    entry = (reflog.get("entries") or [{}])[0]
    check("reflog：不再带完整 from_id", "from_id" in entry, False)
    check("reflog：保留原因", bool(entry.get("reason")), True)

    _call(registry, "writing_git_change", action="branch_switch",
          work_id=wid, name="main")
    _call(registry, "writing_git_change", action="branch_create",
          work_id=wid, name="快进路", switch=True)
    _call(registry, "writing_replace_text", chapter_index=2,
          anchor_quote="配角没有抬头", replacement="快进戊")
    _call(registry, "writing_git_change", action="commit",
          work_id=wid, message="快进路改第二章")
    _call(registry, "writing_git_change", action="branch_switch",
          work_id=wid, name="main")
    merged = _call(registry, "writing_git_change", action="merge",
                   work_id=wid, branch="快进路")
    check("merge：快进合并", merged.get("fast_forward"), True)
    check("merge：快进也带改动预览",
          any(c.get("chapter_index") == 2
              for c in merged.get("chapters") or []), True)

    prompt = svc.agent_prompt(wid, 1)
    check("现场：带版本现场块", "的版本现场" in prompt, True)
    check("现场：带分支与 HEAD", "- 分支 " in prompt and "HEAD " in prompt,
          True)
    check("现场：带最近存档", "- 最近存档：" in prompt, True)


def test_work_pin_and_delete() -> None:
    svc, _registry, _ctx = _setup()
    first = svc.create_work("先写的")
    second = svc.create_work("后写的")
    svc.create_chapter(first, title="第一章")
    svc.save_content(first, 1, "风从码头来", reason="manual")
    chapter = svc.chapter(first, 1)

    svc.set_work_pinned(second, True)
    works = svc.list_works()
    check("置顶：排在最前", works[0]["id"], second)
    check("置顶：pinned=1", int(works[0]["pinned"]), 1)
    svc.set_work_pinned(second, False)
    row = next(w for w in svc.list_works() if w["id"] == second)
    check("取消置顶：pinned=0", int(row["pinned"]), 0)

    try:
        svc.delete_work(first, confirm_title="写错的名字")
    except WritingError as exc:
        check("删除：书名不匹配被拒", "确认" in str(exc), True)
    else:
        check("删除：书名不匹配被拒", False, True)
    check("删除：拒绝后作品还在", svc.db.get_work(first) is not None, True)

    result = svc.delete_work(first, confirm_title="先写的")
    check("删除：返回标题", result["title"], "先写的")
    check("删除：作品不在列表",
          any(w["id"] == first for w in svc.list_works()), False)
    check("删除：章节级联清掉", svc.db.get_chapter(first, 1), None)
    check("删除：版本级联清掉",
          svc.db.latest_saved_version(chapter["id"]), None)


def main() -> int:
    test_registry_surface()
    test_selftest_plan_and_seed()
    test_work_pin_and_delete()
    test_commit_log_amend_via_tools()
    test_branch_diff_and_conflict()
    test_cherry_pick_revert_restore_via_tools()
    test_discard_stash_tag_via_tools()
    test_checkpoint_branch_delete_guard()
    test_partial_commit_via_tools()
    test_feedback_log()
    test_phase_plan_and_action_state()
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
