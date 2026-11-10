"""写作 git 的单元测试（临时库，零 LLM）。

盯四件事：

1. **提交是项目级清单** —— HEAD 只能是提交；工作区改动按章列出。
2. **账本一起走** —— 合同/事实/承诺随提交回滚，绝不只回正文。
3. **分支能切回去** —— 切分支恢复正文与账本；脏工作区先自动检查点。
4. **回滚不删历史** —— restore 产生一条新提交，旧提交仍在。

跑法：``python scripts/test_writegraph_vcs.py``（退出码 0 = 全绿）。
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
        print(f"FAIL {label}\n     期望 {exc_type.__name__}，实得 {type(exc).__name__}: {exc}")
        FAILED += 1
    else:
        print(f"FAIL {label}\n     期望抛 {exc_type.__name__}，实际没抛")
        FAILED += 1


def _setup():
    db = WritingDB(os.path.join(tempfile.mkdtemp(prefix="wvcs_"), "w.db"))
    svc = WritingService(SimpleNamespace(project_dir=ROOT), db=db)
    work = svc.create_work("测试作品")
    svc.create_chapter(work, title="第一章")
    svc.create_chapter(work, title="第二章")
    svc.save_content(work, 1, "第一稿\n第二行")
    svc.save_content(work, 2, "第二章初稿")
    return db, svc, work


def test_commit_and_status() -> None:
    db, svc, work = _setup()
    st = svc.vcs_status(work)
    check("状态：无 HEAD 时两章都算新增", st["chapter_counts"]["added"], 2)
    check("状态：初始不干净", st["clean"], False)

    c1 = svc.vcs_commit(work, "初稿")
    check("提交：根提交没有 parent", c1["parent_id"], "")
    check("提交：落在 main", c1["branch"], "main")
    check("状态：提交后干净", svc.vcs_status(work)["clean"], True)

    svc.save_content(work, 1, "第二稿\n第二行")
    st = svc.vcs_status(work)
    check("状态：正文改动被列出", st["chapter_counts"]["changed"], 1)
    check("状态：账本没动", st["ledger_dirty"], False)

    ch1 = svc.chapter(work, 1)
    svc.set_contract(ch1["id"], purpose="推进主线")
    check("状态：账本改动被列出", svc.vcs_status(work)["ledger_dirty"], True)

    c2 = svc.vcs_commit(work, "改稿")
    check("提交：第二个提交指向第一个", c2["parent_id"], c1["id"])
    check("提交：提交后再次干净", svc.vcs_status(work)["clean"], True)

    log = svc.vcs_log(work)
    check("日志：两条提交", len(log), 2)
    check("日志：HEAD 是最新一条", log[0]["is_head"], True)
    check("日志：带短 id", len(log[0]["short"]), 8)

    diff = svc.vcs_diff(work, from_ref=c1["id"], to_ref=c2["id"],
                        chapter_id=ch1["id"], text=True)
    check("差异：一章正文变了", diff["chapter_counts"]["changed"], 1)
    check("差异：文本里有新增行", "+第二稿" in diff["text_diff"], True)
    check("差异：合同变更被列出",
          len(diff["ledger"]["contracts"]["added"]), 1)
    raises("提交：干净时拒绝空提交",
           WritingError, lambda: svc.vcs_commit(work, "空的"))


def test_branch() -> None:
    db, svc, work = _setup()
    ch1 = svc.chapter(work, 1)
    c1 = svc.vcs_commit(work, "初稿")

    svc.save_content(work, 1, "主线的第二稿")
    svc.set_contract(ch1["id"], purpose="主线推进")
    c2 = svc.vcs_commit(work, "主线改稿")

    branch = svc.vcs_create_branch(work, "岔路A", from_ref=c1["id"])
    check("分支：从旧提交创建", branch["commit_id"], c1["id"])
    raises("分支：重名拒绝", WritingError,
           lambda: svc.vcs_create_branch(work, "岔路A"))
    raises("分支：非法名拒绝", WritingError,
           lambda: svc.vcs_create_branch(work, "no space"))

    svc.vcs_checkout(work, "岔路A")
    check("切分支：正文回到旧提交",
          svc.chapter(work, 1)["content"], "第一稿\n第二行")
    check("切分支：账本一起回", db.get_contract(ch1["id"]), None)

    svc.save_content(work, 1, "岔路稿")
    c3 = svc.vcs_commit(work, "岔路改稿")
    check("切分支：在新分支上提交", c3["branch"], "岔路A")

    svc.vcs_checkout(work, "main")
    check("切回主线：正文回来", svc.chapter(work, 1)["content"], "主线的第二稿")
    check("切回主线：合同回来",
          (db.get_contract(ch1["id"]) or {}).get("purpose"), "主线推进")

    # 脏工作区切分支：先自动检查点，不能丢改动。
    svc.save_content(work, 1, "未提交的主线改动")
    before = len(svc.vcs_log(work, branch="main"))
    svc.vcs_checkout(work, "岔路A")
    main_log = svc.vcs_log(work, branch="main")
    check("脏切换：自动检查点", len(main_log), before + 1)
    check("脏切换：检查点类型", main_log[0]["kind"], "checkpoint")
    svc.vcs_checkout(work, "main")
    check("脏切换：改动没丢", svc.chapter(work, 1)["content"],
          "未提交的主线改动")


def test_restore() -> None:
    db, svc, work = _setup()
    ch1 = svc.chapter(work, 1)
    c1 = svc.vcs_commit(work, "初稿")

    svc.set_contract(ch1["id"], purpose="后来加的合同")
    svc.save_content(work, 1, "第二稿")
    svc.vcs_commit(work, "改稿")
    log_before = len(svc.vcs_log(work))

    result = svc.vcs_restore(work, c1["short"])  # 短号也应能回滚
    check("回滚：正文回到目标", svc.chapter(work, 1)["content"],
          "第一稿\n第二行")
    check("回滚：账本回到目标", db.get_contract(ch1["id"]), None)
    check("回滚：产生新提交", (result["commit"] or {}).get("kind"), "restore")
    check("回滚：历史没被删除", len(svc.vcs_log(work)), log_before + 1)
    check("回滚：工作区干净", svc.vcs_status(work)["clean"], True)
    raises("回滚：已在目标上拒绝", WritingError,
           lambda: svc.vcs_restore(work, c1["id"]))

    # 脏工作区先检查点，再回滚。
    svc.save_content(work, 1, "未提交的改动")
    before = len(svc.vcs_log(work))
    svc.vcs_restore(work, c1["id"])
    check("回滚：脏状态先检查点", len(svc.vcs_log(work)), before + 2)
    check("回滚：检查点保住未提交内容",
          any(c["kind"] == "checkpoint" for c in svc.vcs_log(work)), True)


def test_save_idempotent() -> None:
    """自动保存会反复提交同一份正文：内容没变就不该新增版本。"""
    db, svc, work = _setup()
    chapter = svc.chapter(work, 1)
    chapter_id = chapter["id"]

    def versions() -> int:
        return db.get_conn().execute(
            "SELECT COUNT(*) FROM chapter_versions WHERE chapter_id=?",
            (chapter_id,)).fetchone()[0]

    before = versions()
    same = svc.save_content(work, 1, chapter["content"], reason="autosave")
    check("幂等保存：同内容不新增版本", versions(), before)
    check("幂等保存：标记 unchanged", same.get("unchanged"), True)
    latest = db.get_conn().execute(
        "SELECT MAX(version) FROM chapter_versions WHERE chapter_id=?",
        (chapter_id,)).fetchone()[0]
    check("幂等保存：返回当前版本", same.get("version"), latest)

    changed = svc.save_content(work, 1, chapter["content"] + "。",
                               reason="autosave")
    check("幂等保存：改内容仍新增", versions(), before + 1)
    check("幂等保存：标记 changed", changed.get("unchanged"), False)


def test_draft_vs_save() -> None:
    """自动保存只算暂存稿；按了保存才有保存点。"""
    db, svc, work = _setup()
    chapter = svc.chapter(work, 1)

    def kinds() -> list[str]:
        return [r[0] for r in db.get_conn().execute(
            "SELECT kind FROM chapter_versions WHERE chapter_id=?"
            " ORDER BY version", (chapter["id"],)).fetchall()]

    check("旧版本默认算保存点", kinds(), ["save"])
    check("保存点：初始存在", bool(svc.save_point(chapter)), True)

    svc.save_content(work, 1, chapter["content"] + "\n暂存一行", reason="autosave")
    check("暂存：新版本标记 draft", kinds()[-1], "draft")
    saved = svc.save_point(svc.chapter(work, 1))
    check("暂存：保存点仍是旧版本", saved["version"], 1)
    current = svc.chapter(work, 1)
    check("暂存：正文已落库", current["content"].endswith("暂存一行"), True)
    check("暂存：正文哈希与保存点不同",
          current["content_hash"] != saved["content_hash"], True)

    # 内容没变，但这次作者按的是「保存」：就地升成保存点，不新增版本。
    before = len(kinds())
    promoted = svc.save_content(work, 1, current["content"], reason="manual")
    check("保存：同内容不新增版本", len(kinds()), before)
    check("保存：暂存稿被升成保存点", promoted.get("kind"), "save")
    check("保存：保存点前移到最新版",
          svc.save_point(svc.chapter(work, 1))["version"], promoted["version"])

    svc.save_content(work, 1, "另一版正文", reason="manual")
    check("保存：新内容产生 save 版本", kinds()[-1], "save")
    restored = svc.saved_content(work, 1)
    check("回到保存点：拿到最新保存点正文", restored["content"], "另一版正文")


def test_partial_commit() -> None:
    """只提交某几章：别的章的改动留在工作区，不混进这次提交。"""
    db, svc, work = _setup()
    svc.vcs_commit(work, "初稿")
    svc.save_content(work, 1, "作者手改第一章")
    svc.save_content(work, 2, "agent 改第二章")

    picked = svc.vcs_commit(work, "只提交第二章", chapter_indexes=[2])
    tree = {e["chapter_index"]: e for e in
            json.loads(db.get_commit(picked["id"])["tree_json"])}
    check("部分提交：选中章进了提交",
          tree[2]["content_hash"] == svc.chapter(work, 2)["content_hash"], True)
    check("部分提交：未选中章沿用 HEAD 的版本",
          tree[1]["content_hash"] != svc.chapter(work, 1)["content_hash"], True)

    st = svc.vcs_status(work)
    check("部分提交：未提交的章仍然是脏的", st["chapter_counts"]["changed"], 1)
    svc.vcs_commit(work, "再提交第一章")
    check("部分提交：补完就干净了", svc.vcs_status(work)["clean"], True)

    raises("部分提交：选中的章没改动就拒绝", WritingError,
           lambda: svc.vcs_commit(work, "空", chapter_indexes=[2]))
    raises("部分提交：章不存在就拒绝", WritingError,
           lambda: svc.vcs_commit(work, "空", chapter_indexes=[9]))


def main() -> int:
    test_commit_and_status()
    test_save_idempotent()
    test_branch()
    test_restore()
    test_draft_vs_save()
    test_partial_commit()
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
