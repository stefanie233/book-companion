"""作品工作区（作品记忆 / 设定 / 计划稿 / 素材）的测试（零 LLM）。

盯五件事：

1. **路径只认工作区里的 .md**：``..``、绝对路径、盘符、隐藏文件、别的顶层名
   一律拒绝，解析后的真实路径不能逃出作品目录；
2. **注入口径**：作品记忆全文进提示词与写作上下文，设定只带索引，
   计划稿与素材不自动带；
3. 索引分区、未列出计数、``search`` 三条路径都能走通（决策 129）；
4. 写作工具能建 / 读 / 搜 / 改 / 删，跨库只读且只认 ``素材/``（决策 130）；
5. 删作品时工作区目录一起走，不留孤儿。

跑法：``python scripts/test_writing_docs.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.writing_service import WritingService            # noqa: E402
from storage.writing_db import WritingDB                       # noqa: E402
from tools import ToolRegistry                                 # noqa: E402
from writegraph import docs as docs_mod                        # noqa: E402
from writegraph.chat_tools import build_writing_registry       # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"ok   {label}")
    else:
        FAILED += 1
        print(f"FAIL {label}\n     期望 {want!r}\n     实际 {got!r}")


def raises(label: str, exc, fn) -> None:
    global FAILED
    try:
        fn()
    except exc:
        print(f"ok   {label}")
        return
    except Exception as other:                       # noqa: BLE001
        FAILED += 1
        print(f"FAIL {label}\n     抛的是 {type(other).__name__}: {other}")
        return
    FAILED += 1
    print(f"FAIL {label}\n     没有抛 {exc.__name__}")


def test_paths() -> None:
    allowed = ["作品记忆.md", "设定/人物/人物A.md", "计划稿/结局备选.md",
               "素材/观察/雨.md", "设定/a/b/c/d.md"]
    for path in allowed:
        check(f"路径：接受 {path}", docs_mod.normalize(path), path)
    for bad in ("", "/etc/passwd", "C:/x.md", "设定/../x.md", "../x.md",
                "other/a.md", "设定", "设定/a.txt", "作品记忆.md/x.md",
                ".hidden.md", "设定/.secret.md", "设定/人物/.x.md",
                "设定/" + "深" * 61 + ".md",
                # 旧目录名不再接受：磁盘上由 ensure_workspace 迁移。
                "docs/a.md", "plans/a.md"):
        raises(f"路径：拒绝 {bad!r}", docs_mod.DocError,
               lambda b=bad: docs_mod.normalize(b))


def test_files() -> None:
    tmp = Path(tempfile.mkdtemp())
    root = docs_mod.workspace_root(tmp, "wk_demo")
    check("目录：路径", root, tmp / "data" / "writing" / "works" / "wk_demo")
    docs_mod.ensure_workspace(root, "某城来信")
    check("目录：作品记忆按骨架种下", (root / "作品记忆.md").is_file(), True)
    check("目录：设定/计划稿/素材 都在",
          ((root / "设定").is_dir(), (root / "计划稿").is_dir(),
           (root / "素材").is_dir()), (True, True, True))
    check("目录：空目录也列出来占位",
          [d["path"] for d in docs_mod.list_dirs(root)],
          ["设定", "素材", "计划稿"])

    docs_mod.write_file(root, "设定/人物/人物A.md", "# 人物A\n\n码头邮局的人。\n")
    docs_mod.write_file(root, "计划稿/结局备选.md", "A：他留在港口。\n")
    docs_mod.write_file(root, "素材/观察/雨.md", "雨落在铁皮上。\n")
    check("文件：清单",
          [f["path"] for f in docs_mod.list_files(root)],
          ["作品记忆.md", "设定/人物/人物A.md", "素材/观察/雨.md",
           "计划稿/结局备选.md"])
    check("文件：读回", "码头邮局" in
          docs_mod.read_file(root, "设定/人物/人物A.md")["content"], True)
    check("显示名：不带后缀",
          docs_mod.label("设定/人物/人物A.md"), "设定/人物/人物A")
    docs_mod.write_file(root, "设定/人物/人物A.md", "再加一句。\n", append=True)
    check("文件：追加有换行", docs_mod.read_file(
        root, "设定/人物/人物A.md")["content"].endswith("再加一句。\n"), True)

    index = docs_mod.index_lines(root)
    check("索引：只列设定", len(index), 1)
    check("索引：带首行", "人物A" in index[0], True)
    block = docs_mod.render_block(root)
    check("上下文块：有作品记忆", "作品记忆" in block, True)
    check("上下文块：有设定正文", "码头邮局" in block, True)
    check("上下文块：标题不带后缀", "## 设定：人物/人物A" in block, True)
    check("上下文块：不含计划稿", "结局备选" not in block, True)
    check("上下文块：不含素材", "雨落在铁皮上" not in block, True)

    joined = "\n".join(docs_mod.overview(root))
    check("索引：分区齐全",
          all(f"#### {name}" in joined
              for name in ("设定", "素材", "计划稿")), True)
    check("索引：素材带首行", "雨落在铁皮上" in joined, True)

    found = docs_mod.search(root, "码头")
    check("检索：命中设定", [h["path"] for h in found["hits"]],
          ["设定/人物/人物A.md"])
    found = docs_mod.search(root, "雨", scope="素材")
    check("检索：scope 限定素材", [h["path"] for h in found["hits"]],
          ["素材/观察/雨.md"])
    check("检索：跨分区", {h["path"] for h in
                       docs_mod.search(root, "港")["hits"]},
          {"计划稿/结局备选.md"})
    raises("检索：scope 非法", docs_mod.DocError,
           lambda: docs_mod.search(root, "雨", scope="别的"))

    for i in range(3):
        docs_mod.write_file(root, f"素材/观察/雨{i}.md", f"第{i}条。\n")
    tight = "\n".join(docs_mod.overview(root, material_limit=1))
    check("索引：未列出要显式报数", "还有 3 份未列出" in tight, True)

    raises("删除：作品记忆受保护", docs_mod.DocError,
           lambda: docs_mod.delete_file(root, "作品记忆.md"))
    docs_mod.delete_file(root, "设定/人物/人物A.md")
    check("删除：空目录被收掉", (root / "设定" / "人物").exists(), False)

    # 左栏右键：新建 / 改名 / 删文件夹
    docs_mod.create_dir(root, "设定/地点")
    check("新建：空文件夹列出来",
          any(d["path"] == "设定/地点" for d in docs_mod.list_dirs(root)), True)
    docs_mod.create_file(root, "设定/地点/码头.md", "# 码头\n")
    check("新建：文件已落盘",
          (root / "设定" / "地点" / "码头.md").is_file(), True)
    raises("新建：同名拒绝", docs_mod.DocError,
           lambda: docs_mod.create_file(root, "设定/地点/码头.md"))
    check("改名：文件补 .md",
          docs_mod.rename_entry(root, "设定/地点/码头.md", "港口")["path"],
          "设定/地点/港口.md")
    check("改名：文件夹",
          docs_mod.rename_entry(root, "设定/地点", "港区", kind="dir")["path"],
          "设定/港区")
    raises("改名：分区根受保护", docs_mod.DocError,
           lambda: docs_mod.rename_entry(root, "设定", "x", kind="dir"))
    raises("删除：分区根受保护", docs_mod.DocError,
           lambda: docs_mod.delete_entry(root, "设定", kind="dir"))
    check("删除：文件夹递归",
          docs_mod.delete_entry(root, "设定/港区", kind="dir")["deleted"],
          "设定/港区")
    check("删除：文件夹已不在", (root / "设定" / "港区").exists(), False)
    shutil.rmtree(tmp, ignore_errors=True)


def test_selection() -> None:
    tmp = Path(tempfile.mkdtemp())
    root = docs_mod.workspace_root(tmp, "wk_select")
    docs_mod.ensure_workspace(root, "某城")
    docs_mod.write_file(root, "设定/地点/港口.md", "# 港口\n\n雾大，船期会提前。\n")
    docs_mod.write_file(root, "设定/人物/人物A.md", "# 人物A\n\n说话短。\n")
    text, manifest = docs_mod.select_for_chapter(
        root, context_text="港口 雾大 船期", limit=1)
    check("选择：只选命中项", manifest["selected"], ["设定/地点/港口.md"])
    check("选择：带上正文", "船期会提前" in text, True)
    check("选择：不混入别的设定", "说话短" not in text, True)
    check("选择：不给作品记忆", "作品记忆" not in text, True)
    shutil.rmtree(tmp, ignore_errors=True)


def test_world_facets() -> None:
    tmp = Path(tempfile.mkdtemp())
    root = docs_mod.workspace_root(tmp, "wk_world")
    docs_mod.ensure_workspace(root, "某城来信")
    check("世界层：没有文件返回空", docs_mod.world_facets(root), [])
    docs_mod.write_file(root, "设定/世界.md", "\n".join([
        "# 世界",
        "## 时代与地理      年代、地点、技术水平",
        "状态：定",
        "可见：读者已见",
        "南方港城，蒸汽船时代。",
        "## 社会结构",
        "状态：悬",
        "可见：仅作者",
        "## 经济运转",
        "状态：定",
        "可见：仅作者",
        "码头工钱按日结。",
        "## 故事窄口",
        "状态：定",
        "可见：仅作者",
        "只写码头与灯塔街。",
        "## 航海术语",
        "状态：定",
        "可见：读者已见",
        "压舱、吃水、引水。",
    ]))
    facets = docs_mod.world_facets(root)
    by_name = {f["name"]: f for f in facets}
    check("世界层：标题带说明也认得出",
          "时代与地理" in by_name, True)
    check("世界层：状态", by_name["社会结构"]["status"], "悬")
    check("世界层：可见性", by_name["经济运转"]["visibility"], "仅作者")
    check("世界层：只有状态行不算内容",
          by_name["社会结构"]["has_content"], False)
    check("世界层：额外 facet 保留", "航海术语" in by_name, True)
    check("世界层：未写的标准 facet",
          [n for n in docs_mod.WORLD_FACETS
           if n not in by_name
           or (not by_name[n]["has_content"]
               and not by_name[n]["status"])],
          ["文化与日常", "时代情绪"])


def test_migrate_legacy() -> None:
    tmp = Path(tempfile.mkdtemp())
    root = tmp / "work"
    (root / "docs" / "人物").mkdir(parents=True)
    (root / "docs" / "人物" / "人物A.md").write_text("# 人物A", encoding="utf-8")
    (root / "plans").mkdir()
    (root / "plans" / "结局备选.md").write_text("A", encoding="utf-8")
    (root / "试错稿").mkdir()
    (root / "试错稿" / "章节设计.md").write_text("B", encoding="utf-8")
    docs_mod.ensure_workspace(root, "某城来信")
    check("迁移：旧目录收掉",
          ((root / "docs").exists(), (root / "plans").exists(),
           (root / "试错稿").exists()), (False, False, False))
    check("迁移：文件没丢",
          (root / "设定" / "人物" / "人物A.md").is_file()
          and (root / "计划稿" / "结局备选.md").is_file()
          and (root / "计划稿" / "章节设计.md").is_file(), True)
    shutil.rmtree(tmp, ignore_errors=True)


def test_vcs_files() -> None:
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "prompts", tmp / "prompts")
    db = WritingDB(tmp / "w.db")
    svc = WritingService(SimpleNamespace(project_dir=tmp), db=db)
    work_id = svc.create_work("某城来信")
    svc.create_chapter(work_id, title="第一章")
    svc.docs_write(work_id, "作品记忆.md", "# 作品记忆\n\n旧版\n")
    svc.docs_write(work_id, "设定/人物/人物A.md", "# 人物A\n")
    untracked = svc.vcs_status(work_id)
    check("状态：没提交过时文件全未跟踪",
          {f.get("status") for f in untracked["file_changes"]["added"]},
          {"U"})
    check("状态：未跟踪带说明",
          untracked["file_changes"]["added"][0].get("status_label"), "未跟踪")
    first = svc.vcs_commit(work_id, "基线")
    check("提交：工作区文件已跟踪", svc.vcs_status(work_id)["clean"], True)

    svc.docs_write(work_id, "作品记忆.md", "# 作品记忆\n\n新版\n")
    status = svc.vcs_status(work_id)
    check("状态：文件改动看得见",
          [f["path"] for f in status["file_changes"]["changed"]],
          ["作品记忆.md"])
    check("状态：改过的文件是 M",
          status["file_changes"]["changed"][0].get("status"), "M")
    check("状态：文件改动不算干净", status["clean"], False)

    svc.vcs_restore(work_id, first["id"])
    check("回滚：文件内容回来",
          "旧版" in svc.docs_read(work_id, "作品记忆.md")["content"], True)
    check("回滚：工作区恢复干净", svc.vcs_status(work_id)["clean"], True)

    svc.docs_delete(work_id, "设定/人物/人物A.md")
    status = svc.vcs_status(work_id)
    check("状态：删除看得见",
          [f["path"] for f in status["file_changes"]["removed"]],
          ["设定/人物/人物A.md"])
    check("状态：删掉的文件是 D",
          status["file_changes"]["removed"][0].get("status"), "D")
    svc.vcs_commit(work_id, "删掉人物A")
    check("提交：删除后干净", svc.vcs_status(work_id)["clean"], True)

    svc.docs_write(work_id, "计划稿/结局备选.md", "A：他留在港口。\n")
    svc.vcs_commit(work_id, "加一份计划稿")
    svc.vcs_create_branch(work_id, "岔路")
    svc.vcs_checkout(work_id, "岔路")
    svc.docs_write(work_id, "作品记忆.md", "# 作品记忆\n\n岔路版\n")
    svc.docs_write(work_id, "设定/港口.md", "# 港口\n")
    svc.vcs_commit(work_id, "岔路改设定")
    svc.vcs_switch(work_id, "main")
    check("切分支：设定跟着切回来",
          "岔路版" not in svc.docs_read(work_id, "作品记忆.md")["content"], True)
    check("切分支：别的分支的文件不在这",
          not any(f["path"] == "设定/港口.md"
                  for f in svc.docs_list(work_id)["files"]), True)
    svc.vcs_merge(work_id, "岔路")
    check("合并：设定合进来",
          "岔路版" in svc.docs_read(work_id, "作品记忆.md")["content"], True)
    check("合并：新设定文件也在",
          any(f["path"] == "设定/港口.md"
              for f in svc.docs_list(work_id)["files"]), True)
    check("合并：工作区干净", svc.vcs_status(work_id)["clean"], True)

    # 时间线：一份文件的 git 历史 + 单文件逐行差异
    svc.docs_write(work_id, "作品记忆.md", "# 作品记忆\n\n未提交\n")
    timeline = svc.vcs_file_timeline(work_id, "作品记忆.md")
    check("时间线：未提交改动置顶",
          (timeline["entries"][0]["is_work"], timeline["entries"][0]["op"]),
          (True, "changed"))
    diff = svc.vcs_diff(work_id, from_ref="HEAD", to_ref="WORK",
                        file_path="作品记忆.md")
    check("文件差异：标记路径", diff["file_path"], "作品记忆.md")
    check("文件差异：有逐行改动",
          ("-岔路版" in diff["text_diff"]
           and "+未提交" in diff["text_diff"]), True)
    svc.vcs_commit(work_id, "再改作品记忆")
    timeline = svc.vcs_file_timeline(work_id, "作品记忆.md")
    check("时间线：提交后工作区条消失",
          any(e.get("is_work") for e in timeline["entries"]), False)
    check("时间线：最新提交触到该文件",
          timeline["entries"][0]["message"], "再改作品记忆")
    check("时间线：只列碰过这份文件的提交",
          all(e.get("op") in ("added", "changed", "removed")
              for e in timeline["entries"]), True)
    shutil.rmtree(tmp, ignore_errors=True)


def test_service_and_tool() -> None:
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "prompts", tmp / "prompts")
    db = WritingDB(tmp / "w.db")
    svc = WritingService(SimpleNamespace(project_dir=tmp), db=db)
    work_id = svc.create_work("某城来信")
    ctx = {"work_id": work_id, "chapter_index": 0}
    reg = build_writing_registry(ToolRegistry(), svc, ctx)

    check("工具：已注册", "writing_docs" in reg.get_tool_names(), True)
    listing = svc.docs_list(work_id)
    check("接口：目录占位",
          [d["path"] for d in listing.get("dirs") or []],
          ["设定", "素材", "计划稿"])
    out = json.loads(reg.execute("writing_docs", json.dumps({"action": "list"})))
    check("工具：list", out.get("ok"), True)
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "write", "path": "设定/世界观/港口.md",
        "content": "# 港口\n\n雾大，船期会提前。\n"})))
    check("工具：write", out.get("ok"), True)
    out = json.loads(reg.execute("writing_docs", json.dumps({"action": "list"})))
    row = next((f for f in out.get("files") or []
                if f.get("path") == "设定/世界观/港口.md"), {})
    check("工具：list 带 git 状态", row.get("status"), "U")
    out = json.loads(reg.execute("writing_git", json.dumps({
        "action": "diff", "file_path": "设定/世界观/港口.md"})))
    check("工具：diff 只看一份文件",
          (out.get("ok"), out.get("file_path"), bool(out.get("text_diff"))),
          (True, "设定/世界观/港口.md", True))
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "read", "path": "设定/世界观/港口.md"})))
    check("工具：read", "雾大" in out.get("content", ""), True)
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "search", "query": "雾大"})))
    check("工具：search", [h["path"] for h in out.get("hits") or []],
          ["设定/世界观/港口.md"])
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "write", "path": "../evil.md", "content": "x"})))
    check("工具：越界路径回错误", out.get("ok"), False)

    other = svc.create_work("另一本")
    svc.docs_write(other, "素材/参考/潮汐.md", "潮水每天退两次。\n")
    svc.docs_write(other, "设定/人物/林.md", "林是灯塔看守。\n")
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "read", "work_id": other, "path": "素材/参考/潮汐.md"})))
    check("跨库：读素材可以", "潮水" in out.get("content", ""), True)
    check("跨库：不改变当前现场", ctx["work_id"], work_id)
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "read", "work_id": other, "path": "设定/人物/林.md"})))
    check("跨库：读设定被拒", out.get("ok"), False)
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "write", "work_id": other,
        "path": "素材/参考/潮汐.md", "content": "x"})))
    check("跨库：写入被拒", out.get("ok"), False)
    out = json.loads(reg.execute("writing_docs", json.dumps({
        "action": "search", "work_id": other, "query": "林"})))
    check("跨库：检索只认素材",
          [h["path"] for h in out.get("hits") or []], [])

    prompt = svc.agent_prompt(work_id, 1)
    check("提示词：带工作区", "#### 作品记忆" in prompt, True)
    check("提示词：带设定索引", "设定/世界观/港口.md" in prompt, True)
    check("提示词：带「现在在哪」", "### 现在在哪" in prompt, True)
    check("提示词：诊断缺第1章合同", "第1章合同" in prompt, True)

    check("工具：research 已注册",
          "writing_research" in reg.get_tool_names(), True)
    books_db = tmp / "data" / "books.db"
    books_db.parent.mkdir(parents=True, exist_ok=True)
    bconn = sqlite3.connect(books_db)
    bconn.executescript("""
        CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE book_chapters (book_id TEXT, chapter_index INTEGER,
                                    title TEXT);
        CREATE TABLE book_chunks (id TEXT PRIMARY KEY, book_id TEXT,
                                  chapter_index INTEGER, chunk_index INTEGER,
                                  content TEXT, char_count INTEGER);
    """)
    bconn.execute("INSERT INTO books VALUES ('b1','某城手记')")
    bconn.execute("INSERT INTO book_chapters VALUES ('b1',3,'夜码头')")
    bconn.execute(
        "INSERT INTO book_chunks VALUES ('c1','b1',3,0,?,200)",
        ("码头夜班的工钱按日结，搬一船盐给三个铜板；"
         "工头把工钱压在账房。",))
    bconn.commit()
    bconn.close()
    research = json.loads(asyncio.run(reg.execute_async(
        "writing_research", json.dumps({"query": "码头夜班的工钱"}))))
    check("工具：research 有候选", research.get("count"), 1)
    check("工具：research 带出处",
          (research.get("cards") or [{}])[0].get("book_title"), "某城手记")
    check("工具：research 拒绝未授权的联网",
          json.loads(asyncio.run(reg.execute_async(
              "writing_research", json.dumps({
                  "query": "码头", "source": "web"})))).get("ok"), False)
    check("工具：research 空项目也能查",
          asyncio.run(svc.research_material(
              "", "码头夜班的工钱")).get("count"), 1)
    check("联网：开关默认关", svc.web_research_enabled(work_id), False)
    check("联网：开关落库",
          svc.set_web_research(work_id, True).get("web_research_enabled"), True)
    check("联网：开关读回", svc.web_research_enabled(work_id), True)

    # 联网用假 provider：只验归一、落盘与轮次上限，不碰真实网络。
    from services import web_research_service as web_mod
    calls: list[tuple[str, int]] = []

    async def fake_search(cfg, query, *, count=5, need_content=True):
        calls.append((query, count))
        return {
            "provider": "deepseek", "source": "web", "query": query,
            "cards": [{"title": "码头夜班", "url": "https://example.com/a",
                       "site_name": "地方志", "date": "1901", "authority": 2,
                       "score": 0.9, "excerpt": "搬一船盐给三个铜板。",
                       "exact": True, "kind": "网页正文", "source": "web",
                       "caution": "外部来源"}],
            "count": 1, "note": "外部来源。",
        }

    real_search = web_mod.search
    web_mod.search = fake_search
    try:
        web = json.loads(asyncio.run(reg.execute_async(
            "writing_research", json.dumps({
                "query": "码头夜班的工钱", "source": "web",
                "enable_web": True, "save": True}))))
        check("联网：有候选", web.get("count"), 1)
        check("联网：带出处", (web.get("cards") or [{}])[0].get("url"),
              "https://example.com/a")
        saved = str(web.get("saved") or "")
        check("联网：落进素材", saved.startswith("素材/网络/"), True)
        saved_file = svc.docs_root(work_id) / saved
        check("联网：素材文件存在", saved_file.is_file(), True)
        check("联网：素材带 URL",
              "https://example.com/a" in saved_file.read_text(encoding="utf-8"),
              True)
        auto_mine = json.loads(asyncio.run(reg.execute_async(
            "writing_research", json.dumps({
                "query": "码头夜班的工钱", "source": "auto",
                "enable_web": True}))))
        check("联网：auto 先查素材库", auto_mine.get("source"), "materials")
        auto = json.loads(asyncio.run(reg.execute_async(
            "writing_research", json.dumps({
                "query": "账房", "source": "auto",
                "enable_web": True}))))
        check("联网：auto 素材库没有就读书库", auto.get("source"), "library")
        check("联网：auto 不白跑网络", len(calls), 1)
        remembered = svc.create_work("联网本")
        check("联网：remember 前开关关",
              svc.web_research_enabled(remembered), False)
        json.loads(asyncio.run(reg.execute_async(
            "writing_research", json.dumps({
                "query": "码头", "work_id": remembered, "source": "web",
                "enable_web": True, "remember_web": True}))))
        check("联网：remember 落开关",
              svc.web_research_enabled(remembered), True)
        ctx["_web_research_calls"] = 2
        capped = json.loads(asyncio.run(reg.execute_async(
            "writing_research", json.dumps({
                "query": "码头", "source": "web", "enable_web": True}))))
        check("联网：每轮上限 2 次", capped.get("ok"), False)
        ctx.pop("_web_research_calls", None)
    finally:
        web_mod.search = real_search

    svc.create_chapter(work_id, title="第一章")
    chapter = svc.chapter(work_id, 1)
    svc.set_contract(chapter["id"], purpose="到港", must_hit_now=["上岸"],
                     arc_ref="计划稿/第二卷/第1-3章.md")
    saved = svc.db.get_contract(chapter["id"])
    check("合同：弧线指针落库",
          saved.get("arc_ref"), "计划稿/第二卷/第1-3章.md")
    built = svc.assemble(work_id, 1)
    ids = [b["id"] for b in built["manifest"]["blocks"]]
    check("上下文：work_docs 进了预算", "work_docs" in ids, True)
    check("上下文：设定正文在块里", "雾大" in built["text"], True)
    check("上下文：选中设定有账", built["work_docs"]["selected"],
          ["设定/世界观/港口.md"])
    check("合同：弧线进上下文", "弧线：计划稿/第二卷" in built["text"], True)

    svc.delete_work(work_id, confirm_title="某城来信")
    check("删除作品：工作区一起走",
          (tmp / "data" / "writing" / "works" / work_id).exists(), False)
    shutil.rmtree(tmp, ignore_errors=True)


def test_workspace_selection_block() -> None:
    """「添加到聊天」的引用块：文件要求先 read，文件夹要求先 list。"""
    from api.text_helpers import writing_selection_block
    file_block = writing_selection_block("设定/人物/人物A.md", "workspace")
    check("引用：工作区文件", "工作区文件" in file_block, True)
    check("引用：文件要求先 read", "writing_docs(action=\"read\"" in file_block, True)
    folder_block = writing_selection_block("设定/人物/", "workspace")
    check("引用：工作区文件夹", "工作区文件夹" in folder_block, True)
    check("引用：文件夹要求先 list", "writing_docs(action=\"list\"" in folder_block, True)


def test_empty_project() -> None:
    """空项目：不绑作品时，提示词不代入任何一本的现场。

    以前 ``agent_prompt`` 对空 work_id 回退到 ``works[0]``——作者想拿到
    干净上下文，只能新建一本书。空项目块出现时，章节/工作区/版本现场
    一律不注入；作品目录也不展开（只报数量），工具在没绑定时给「建书或
    选书」的指引，作品清单要模型自己调 writing_status。
    """
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "prompts", tmp / "prompts")
    svc = WritingService(SimpleNamespace(project_dir=tmp),
                         db=WritingDB(tmp / "w.db"))
    wid = svc.create_work("某城来信")
    svc.docs_write(wid, "设定/人物/人物A.md", "# 人物A\n\n左眉有疤。\n")
    svc.create_chapter(wid, title="码头")

    prompt = svc.agent_prompt("", 0)
    check("空项目：明说没绑定作品", "### 空项目" in prompt, True)
    check("空项目：不注入章节现场", "《某城来信》的章节" in prompt, False)
    check("空项目：不注入工作区", "#### 作品记忆" in prompt, False)
    check("空项目：不注入版本现场", "的版本现场" in prompt, False)
    check("空项目：不注入作品书名", "某城来信" not in prompt, True)
    check("空项目：给 writing_status 指路",
          "writing_status" in prompt, True)

    other = svc.create_work("另一本")
    bound = svc.agent_prompt(wid, 1)
    check("绑定作品：现场照旧", "《某城来信》的章节" in bound, True)
    check("绑定作品：没有空项目块", "### 空项目" in bound, False)
    check("绑定作品：不注入别的作品书名", "另一本" not in bound, True)
    check("绑定作品：目录不展开 id", other not in bound, True)

    reg = build_writing_registry(ToolRegistry(), svc,
                                 {"work_id": "", "chapter_index": 0})
    out = json.loads(reg.execute("writing_docs", json.dumps(
        {"action": "list"})))
    check("空项目：文件工具拒绝", out.get("ok"), False)
    check("空项目：错误提示带指引",
          "writing_create_work" in str(out.get("error") or ""), True)

    shutil.rmtree(tmp, ignore_errors=True)


def test_web_config_lookup() -> None:
    """主模型不是 DeepSeek 时，联网仍从控制台连接里取 DeepSeek。"""
    from provider_store import ProviderStore
    from services import web_research_service as web_mod

    tmp = Path(tempfile.mkdtemp())
    providers = tmp / "data" / "config" / "providers.json"
    providers.parent.mkdir(parents=True, exist_ok=True)
    store = ProviderStore(providers)
    store.upsert_provider({
        "name": "DeepSeek", "base_url": "https://api.deepseek.com",
        "api_key": "sk-test",
        "models": [{"id": "deepseek-chat", "kind": "llm"}]})
    svc = WritingService(
        SimpleNamespace(project_dir=tmp, providers_path=providers),
        db=WritingDB(tmp / "w.db"))
    svc.api_client = SimpleNamespace(config=SimpleNamespace(
        api_base="https://api.openai.com/v1", api_key="other"))
    check("联网：从控制台找 DeepSeek",
          web_mod.deepseek_endpoint(svc._web_config()),
          ("https://api.deepseek.com", "sk-test"))
    svc.api_client.config.api_base = "https://api.deepseek.com/v1"
    svc.api_client.config.api_key = "main"
    check("联网：主模型是 DeepSeek 就直接用",
          web_mod.deepseek_endpoint(svc._web_config()),
          ("https://api.deepseek.com", "main"))
    shutil.rmtree(tmp, ignore_errors=True)


def test_web_research() -> None:
    """DeepSeek 联网的纯函数层：端点、归一、抓页边界、素材落盘格式。"""
    from services import web_research_service as web_mod

    cfg = SimpleNamespace(api_base="https://api.deepseek.com/v1", api_key="k")
    check("联网：DeepSeek 根地址", web_mod.deepseek_endpoint(cfg),
          ("https://api.deepseek.com", "k"))
    check("联网：子域也认",
          web_mod.deepseek_endpoint(SimpleNamespace(
              api_base="https://search.deepseek.com/v1", api_key="k")),
          ("https://search.deepseek.com", "k"))
    check("联网：别的端点不冒充",
          web_mod.deepseek_endpoint(SimpleNamespace(
              api_base="https://api.openai.com/v1", api_key="k")), None)
    check("联网：缺 key 不可用",
          web_mod.deepseek_endpoint(SimpleNamespace(
              api_base="https://api.deepseek.com", api_key="")), None)

    cards = web_mod.cards_from_results([
        {"title": "摘要页", "url": "https://example.com/s",
         "snippet": "只有摘要", "rerank_score": 0.4},
        {"title": "正文页", "url": "https://example.com/f",
         "content": "正文  多空格", "summary": "摘要",
         "rerank_score": 0.9, "authority": 3, "site_name": "例",
         "date": "1900"},
        {"title": "坏地址", "url": "javascript:alert(1)", "snippet": "x"},
        {"title": "无内容", "url": "https://example.com/e"},
    ], limit=6)
    check("联网：只留真实结果", len(cards), 2)
    check("联网：按分数排序", cards[0]["title"], "正文页")
    check("联网：正文与摘要分档",
          (cards[0]["exact"], cards[0]["kind"], cards[1]["exact"]),
          (True, "网页正文", False))
    check("联网：空白归一", cards[0]["excerpt"], "正文 多空格")

    raises("联网：抓页只认 http(s)", RuntimeError,
           lambda: asyncio.run(web_mod.fetch(cfg, "file:///x")))
    text = web_mod.cards_markdown("码头夜班", cards[:1])
    check("联网：素材带 URL", "https://example.com/f" in text, True)
    check("联网：素材标引文类型", "引文类型：网页正文" in text, True)


def main() -> int:
    test_paths()
    test_files()
    test_selection()
    test_world_facets()
    test_migrate_legacy()
    test_service_and_tool()
    test_vcs_files()
    test_workspace_selection_block()
    test_empty_project()
    test_web_config_lookup()
    test_web_research()
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
