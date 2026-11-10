"""写作 git 的扩展指令测试（临时库，零 LLM）。

覆盖「作者真会用到的 git 场景」——不是复刻 git 语法，而是确认每条指令
在写作存储上语义正确：

- amend：改提交说明 / 带上漏改，旧提交退到 reflog；
- revert：撤销某一次改稿，留反向提交，不抹历史；
- cherry-pick：把岔路的一次修改单独摘过来；
- merge：快进 / 三路 / 冲突裁决，合并提交有两个父节点；
- tag：定稿里程碑；
- stash：收起手头改动，工作区回到 HEAD；
- discard：只丢弃某一章的未提交改动；
- branch -m / -d / switch -c；
- log --all / -- <章>；reflog。

跑法：``python scripts/test_writegraph_git_ops.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.writing_service import WritingError, WritingService   # noqa: E402
from storage.writing_db import WritingDB                            # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def raises(label: str, exc_type, fn) -> None:
    global FAILED
    try:
        fn()
    except exc_type:
        print(f"ok   {label}")
    except Exception as exc:                      # noqa: BLE001
        print(f"FAIL {label}\n     期望 {exc_type.__name__}，"
              f"实得 {type(exc).__name__}: {exc}")
        FAILED += 1
    else:
        print(f"FAIL {label}\n     期望抛 {exc_type.__name__}，实际没抛")
        FAILED += 1


def _setup():
    db = WritingDB(os.path.join(tempfile.mkdtemp(prefix="wgit_"), "w.db"))
    svc = WritingService(SimpleNamespace(project_dir=ROOT), db=db)
    work = svc.create_work("测试作品")
    svc.create_chapter(work, title="第一章")
    svc.create_chapter(work, title="第二章")
    svc.save_content(work, 1, "第一行\n第二行\n第三行")
    svc.save_content(work, 2, "第二章初稿")
    base = svc.vcs_commit(work, "初稿")
    return db, svc, work, base


def test_amend() -> None:
    db, svc, work, base = _setup()
    svc.save_content(work, 1, "第一行（补）\n第二行\n第三行")
    amended = svc.vcs_amend(work, "初稿（补第一章开头）")
    log = svc.vcs_log(work)
    check("amend：历史只剩一条", len(log), 1)
    check("amend：HEAD 换了提交", amended["id"] != base["id"], True)
    check("amend：父节点沿用原提交的父", amended["parent_id"], "")
    check("amend：说明已更新", log[0]["message"], "初稿（补第一章开头）")
    check("amend：带上漏改，工作区干净",
          svc.vcs_status(work)["clean"], True)
    check("amend：正文是补过的",
          svc.chapter(work, 1)["content"].startswith("第一行（补）"), True)
    reflog = svc.vcs_reflog(work)
    check("amend：reflog 记下旧提交",
          any(e["reason"] == "amend" and e["from_id"] == base["id"]
              for e in reflog), True)

    # 新作品未提交时 amend 要拦住
    db2 = WritingDB(os.path.join(tempfile.mkdtemp(prefix="wgit_"), "w.db"))
    svc3 = WritingService(SimpleNamespace(project_dir=ROOT), db=db2)
    work3 = svc3.create_work("空作品")
    svc3.create_chapter(work3, title="章")
    raises("amend：没有提交时拒绝",
           WritingError, lambda: svc3.vcs_amend(work3, "x"))


def test_revert() -> None:
    db, svc, work, base = _setup()
    svc.save_content(work, 1, "改坏的版本\n第二行\n第三行")
    bad = svc.vcs_commit(work, "改坏的一版")
    svc.save_content(work, 2, "第二章后来改的")     # 后面还有别的改动
    later = svc.vcs_commit(work, "第二章再改")

    result = svc.vcs_revert(work, bad["short"])  # 短号也应能撤销
    check("revert：产生了一条新提交", bool(result["commit"]), True)
    check("revert：提交类型是 revert", result["commit"]["kind"], "revert")
    check("revert：父节点是当前 HEAD", result["commit"]["parent_id"], later["id"])
    check("revert：正文回到改坏前",
          svc.chapter(work, 1)["content"].startswith("第一行"), True)
    check("revert：没碰后面那次的改动",
          svc.chapter(work, 2)["content"], "第二章后来改的")
    check("revert：历史保留三条 + 反向提交",
          len(svc.vcs_log(work)), 4)
    check("revert：工作区干净", svc.vcs_status(work)["clean"], True)
    raises("revert：重复撤销要拦住",
           WritingError, lambda: svc.vcs_revert(work, bad["id"]))
    raises("revert：根提交没有父状态",
           WritingError, lambda: svc.vcs_revert(work, base["id"]))


def test_cherry_pick() -> None:
    db, svc, work, base = _setup()
    svc.vcs_create_branch(work, "岔路A")
    svc.vcs_checkout(work, "岔路A")
    svc.save_content(work, 1, "岔路里的改法\n第二行\n第三行")
    picked = svc.vcs_commit(work, "岔路的实验")
    svc.vcs_checkout(work, "main")
    check("cherry-pick：回主线后是原稿",
          svc.chapter(work, 1)["content"].startswith("第一行"), True)

    result = svc.vcs_cherry_pick(work, picked["short"])  # 短号也应能摘取
    check("cherry-pick：类型正确", result["commit"]["kind"], "cherry-pick")
    check("cherry-pick：正文摘过来了",
          svc.chapter(work, 1)["content"].startswith("岔路里的改法"), True)
    check("cherry-pick：主线日志多一条",
          len(svc.vcs_log(work)), 2)
    raises("cherry-pick：重复摘取要拦住",
           WritingError, lambda: svc.vcs_cherry_pick(work, picked["id"]))


def test_merge_fast_forward() -> None:
    db, svc, work, base = _setup()
    svc.vcs_create_branch(work, "岔路A")
    svc.vcs_checkout(work, "岔路A")
    svc.save_content(work, 1, "岔路改法\n第二行\n第三行")
    tip = svc.vcs_commit(work, "岔路一改")
    svc.vcs_checkout(work, "main")

    result = svc.vcs_merge(work, "岔路A")
    check("快进合并：标记 fast_forward", result["fast_forward"], True)
    check("快进合并：HEAD 指向岔路提交", result["status"]["head"]["id"], tip["id"])
    check("快进合并：正文已更新",
          svc.chapter(work, 1)["content"].startswith("岔路改法"), True)
    check("快进合并：工作区干净", result["status"]["clean"], True)
    branches = {b["name"]: b for b in result["status"]["branches"]}
    check("快进合并：main 引用前进",
          branches["main"]["commit_id"], tip["id"])
    raises("快进合并：再合一次提示已最新",
           WritingError, lambda: svc.vcs_merge(work, "岔路A"))


def test_merge_three_way() -> None:
    db, svc, work, base = _setup()
    svc.vcs_create_branch(work, "岔路A")
    svc.vcs_checkout(work, "岔路A")
    svc.save_content(work, 1, "岔路改第一行\n第二行\n第三行")
    side = svc.vcs_commit(work, "岔路改第一章")
    svc.vcs_checkout(work, "main")
    svc.save_content(work, 2, "主线改第二章")
    svc.vcs_commit(work, "主线改第二章")

    result = svc.vcs_merge(work, "岔路A")
    check("三路合并：不是快进", result["fast_forward"], False)
    check("三路合并：两个父节点", result["commit"]["parent2_id"], side["id"])
    check("三路合并：类型是 merge", result["commit"]["kind"], "merge")
    check("三路合并：第一章取岔路",
          svc.chapter(work, 1)["content"].startswith("岔路改第一行"), True)
    check("三路合并：第二章取主线",
          svc.chapter(work, 2)["content"], "主线改第二章")
    check("三路合并：工作区干净", result["status"]["clean"], True)
    check("三路合并：历史含双方提交",
          len(svc.vcs_log(work)), 4)


def test_merge_text_auto() -> None:
    """两侧改同一章但行区间不重叠——应当自动合，不算冲突。"""
    db, svc, work, base = _setup()
    svc.vcs_create_branch(work, "岔路A")
    svc.vcs_checkout(work, "岔路A")
    svc.save_content(work, 1, "第一行（岔路）\n第二行\n第三行")
    svc.vcs_commit(work, "岔路改行首")
    svc.vcs_checkout(work, "main")
    svc.save_content(work, 1, "第一行\n第二行\n第三行（主线）")
    svc.vcs_commit(work, "主线改行尾")

    result = svc.vcs_merge(work, "岔路A")
    check("正文自动合并：没有冲突", "conflicts" not in result, True)
    content = svc.chapter(work, 1)["content"]
    check("正文自动合并：保留岔路的行首", "第一行（岔路）" in content, True)
    check("正文自动合并：保留主线的行尾", "第三行（主线）" in content, True)


def test_merge_conflict() -> None:
    db, svc, work, base = _setup()
    svc.vcs_create_branch(work, "岔路A")
    svc.vcs_checkout(work, "岔路A")
    svc.save_content(work, 1, "岔路版本\n第二行\n第三行")
    svc.vcs_commit(work, "岔路改同一行")
    svc.vcs_checkout(work, "main")
    svc.save_content(work, 1, "主线版本\n第二行\n第三行")
    svc.vcs_commit(work, "主线改同一行")

    result = svc.vcs_merge(work, "岔路A")
    check("冲突：返回冲突清单", len(result.get("conflicts") or []), 1)
    conflict = (result.get("conflicts") or [{}])[0]
    check("冲突：定位到章", conflict.get("kind"), "chapter")
    check("冲突：给出 ours / theirs",
          bool(conflict.get("ours")) and bool(conflict.get("theirs")), True)
    check("冲突：工作区没被改脏",
          svc.chapter(work, 1)["content"].startswith("主线版本"), True)
    check("冲突：状态仍干净", svc.vcs_status(work)["clean"], True)

    resolved = svc.vcs_merge(work, "岔路A",
                             resolutions={conflict["key"]: "theirs"})
    check("冲突裁决：产生合并提交", bool(resolved.get("commit")), True)
    check("冲突裁决：取岔路版本",
          svc.chapter(work, 1)["content"].startswith("岔路版本"), True)
    check("冲突裁决：工作区干净", svc.vcs_status(work)["clean"], True)


def test_merge_ledger_conflict() -> None:
    db, svc, work, base = _setup()
    ch = svc.chapter(work, 1)["id"]
    svc.vcs_create_branch(work, "岔路A")
    svc.vcs_checkout(work, "岔路A")
    svc.set_contract(ch, purpose="岔路的意图")
    svc.vcs_commit(work, "岔路改合同")
    svc.vcs_checkout(work, "main")
    svc.set_contract(ch, purpose="主线的意图")
    svc.vcs_commit(work, "主线改合同")

    result = svc.vcs_merge(work, "岔路A")
    ledger_conflicts = [c for c in result.get("conflicts") or []
                        if c.get("kind") == "ledger"]
    check("账本冲突：报出合同", len(ledger_conflicts), 1)
    key = ledger_conflicts[0]["key"]
    svc.vcs_merge(work, "岔路A", resolutions={key: "theirs"})
    check("账本冲突：裁决后取岔路",
          svc.db.get_contract(ch)["purpose"], "岔路的意图")


def test_tag() -> None:
    db, svc, work, base = _setup()
    tag = svc.vcs_create_tag(work, "v1定稿")
    check("标签：指向当前 HEAD", tag["commit_id"], base["id"])
    check("标签：列表里有一条", len(svc.vcs_tags(work)), 1)
    log = svc.vcs_log(work)
    names = sorted(r["name"] for r in log[0]["refs"])
    check("标签：历史里带装饰", names, ["main", "v1定稿"])
    raises("标签：重名拒绝",
           WritingError, lambda: svc.vcs_create_tag(work, "v1定稿"))
    raises("标签：名字带空格拒绝",
           WritingError, lambda: svc.vcs_create_tag(work, "v1 定稿"))
    svc.vcs_delete_tag(work, "v1定稿")
    check("标签：删除后为空", len(svc.vcs_tags(work)), 0)


def test_stash() -> None:
    db, svc, work, base = _setup()
    svc.save_content(work, 1, "暂存里的改法\n第二行\n第三行")
    svc.vcs_stash_push(work, "先收起来")
    check("暂存：工作区回到 HEAD", svc.vcs_status(work)["clean"], True)
    check("暂存：正文回到原稿",
          svc.chapter(work, 1)["content"].startswith("第一行"), True)
    stashes = svc.vcs_stashes(work)
    check("暂存：列表一条", len(stashes), 1)
    check("暂存：显示名是 stash@{0}", stashes[0]["label"], "stash@{0}")

    svc.save_content(work, 2, "暂存之后提交的改动")
    raises("暂存：工作区脏时拒绝恢复",
           WritingError,
           lambda: svc.vcs_stash_pop(work, stashes[0]["label"]))
    svc.vcs_commit(work, "暂存之后又写了一章")

    svc.vcs_stash_pop(work, stashes[0]["label"])
    check("暂存恢复：改法回来了",
          svc.chapter(work, 1)["content"].startswith("暂存里的改法"), True)
    check("暂存恢复：不退回暂存之后提交的章",
          svc.chapter(work, 2)["content"], "暂存之后提交的改动")
    check("暂存恢复：列表清空", len(svc.vcs_stashes(work)), 0)

    # 再暂存一次，用短号 drop——引用解析不能只认内部 ref 名
    svc.save_content(work, 1, "再改一次\n第二行\n第三行")
    svc.vcs_stash_push(work, "第二条暂存")
    second = svc.vcs_stashes(work)[0]
    svc.vcs_stash_drop(work, second["short"])
    check("暂存：短号也能 drop", len(svc.vcs_stashes(work)), 0)


def test_discard_chapter() -> None:
    db, svc, work, base = _setup()
    ch1 = svc.chapter(work, 1)["id"]
    svc.save_content(work, 1, "乱改的第一章\n第二行\n第三行")
    svc.save_content(work, 2, "乱改的第二章")
    check("丢弃：两章都脏", svc.vcs_status(work)["chapter_counts"]["changed"], 2)
    svc.vcs_discard_chapter(work, ch1)
    check("丢弃：第一章回到提交",
          svc.chapter(work, 1)["content"].startswith("第一行"), True)
    check("丢弃：第二章的改动还在",
          svc.chapter(work, 2)["content"], "乱改的第二章")
    check("丢弃：只剩一章脏", svc.vcs_status(work)["chapter_counts"]["changed"], 1)
    raises("丢弃：没有改动时拒绝",
           WritingError, lambda: svc.vcs_discard_chapter(work, ch1))


def test_branch_ops() -> None:
    db, svc, work, base = _setup()
    svc.vcs_switch(work, "岔路B", create=True)
    check("switch -c：建好就切过去",
          svc.vcs_status(work)["branch"], "岔路B")
    svc.save_content(work, 1, "岔路B改法\n第二行\n第三行")
    side = svc.vcs_commit(work, "岔路B一改")
    svc.vcs_switch(work, "main")
    raises("branch -d：未合并分支拒绝删除",
           WritingError, lambda: svc.vcs_delete_branch(work, "岔路B"))
    raises("branch -m：改名撞车拒绝",
           WritingError, lambda: svc.vcs_rename_branch(work, "岔路B", "main"))
    svc.vcs_rename_branch(work, "岔路B", "岔路C")
    names = [b["name"] for b in svc.vcs_branches(work)]
    check("branch -m：改名成功", sorted(names), ["main", "岔路C"])
    check("branch -m：提交跟着改名",
          svc.vcs_log(work, branch="岔路C")[0]["id"], side["id"])
    raises("branch -d：不能删当前分支",
           WritingError, lambda: svc.vcs_delete_branch(work, "main"))
    svc.vcs_delete_branch(work, "岔路C", force=True)
    check("branch -D：强制删除成功",
          sorted(b["name"] for b in svc.vcs_branches(work)), ["main"])


def test_log_filters() -> None:
    db, svc, work, base = _setup()
    ch1 = svc.chapter(work, 1)["id"]
    svc.vcs_create_branch(work, "岔路A")
    svc.vcs_checkout(work, "岔路A")
    svc.save_content(work, 1, "岔路改第一章\n第二行\n第三行")
    svc.vcs_commit(work, "岔路改第一章")
    svc.vcs_checkout(work, "main")
    svc.save_content(work, 2, "主线改第二章")
    svc.vcs_commit(work, "主线改第二章")

    check("log：默认只看当前分支", len(svc.vcs_log(work)), 2)
    check("log --all：含岔路提交", len(svc.vcs_log(work, all_branches=True)), 3)
    only_ch1 = svc.vcs_log(work, all_branches=True, chapter_id=ch1)
    check("log -- <章>：只列改过这章的提交", len(only_ch1), 2)
    check("log -- <章>：最新一条是岔路",
          only_ch1[0]["message"], "岔路改第一章")


def test_reflog_and_merge_base() -> None:
    db, svc, work, base = _setup()
    svc.vcs_create_branch(work, "岔路A")
    svc.vcs_checkout(work, "岔路A")
    svc.save_content(work, 1, "岔路改法\n第二行\n第三行")
    tip = svc.vcs_commit(work, "岔路一改")
    svc.vcs_checkout(work, "main")

    reflog = svc.vcs_reflog(work)
    check("reflog：记下切分支",
          any(e["reason"] == "checkout" and e["commit_id"] == base["id"]
              for e in reflog), True)
    check("reflog：记下提交",
          any(e["reason"] == "manual" and e["commit_id"] == tip["id"]
              for e in reflog), True)

    info = svc.vcs_merge_base(work, "main", "岔路A")
    check("merge-base：分叉点是初稿", info["base"]["id"], base["id"])
    check("merge-base：岔路领先 1", info["ahead"], 1)
    check("merge-base：主线没有额外提交", info["behind"], 0)
    diff = svc.vcs_diff(work, from_ref="main...岔路A", to_ref="岔路A")
    check("三点差异：从分叉点算起", diff["chapter_counts"]["changed"], 1)


def main() -> int:
    test_amend()
    test_revert()
    test_cherry_pick()
    test_merge_fast_forward()
    test_merge_three_way()
    test_merge_text_auto()
    test_merge_conflict()
    test_merge_ledger_conflict()
    test_tag()
    test_stash()
    test_discard_chapter()
    test_branch_ops()
    test_log_filters()
    test_reflog_and_merge_base()
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
