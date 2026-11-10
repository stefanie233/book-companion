"""作者攻略接口与后台任务的零 token 单测。

盯四件事：
1. 命令拼装：动作 → 独立进程参数；``--force`` 只对抽取生效。
2. 任务管线：起进程、收 stdout、解析摘要、串行约束、日志落盘。
3. 攻略接口：列表带书名 / 黑白灰计数 / 只有用户能改名单。
4. 路由注册：新增的 plans/jobs 路径都在。

跑法：``python scripts/test_author_jobs.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import api.author_jobs as jobs_mod                                # noqa: E402
from api.author_jobs import AuthorJobRunner, build_command        # noqa: E402
from storage.authors_db import AuthorsDB                          # noqa: E402
from storage.db import BookDB                                     # noqa: E402
from storage.models import BookRow                                # noqa: E402

FAILED = 0


def ok(name: str, cond: bool, extra: str = "") -> None:
    global FAILED
    if cond:
        print(f"  PASS {name} {extra}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {extra}")


def test_command(root: Path) -> None:
    plan = build_command(root, "某作者", "plan")
    ok("命令：plan", plan[-1] == "--plan" and "run_author_details.py" in plan[1])
    extract = build_command(root, "某作者", "extract", force=True)
    ok("命令：extract --force", extract[-2:] == ["--extract", "--force"])
    close = build_command(root, "某作者", "close_hooks")
    ok("命令：闭合钩子", close[-1] == "--close-hooks")
    ok("命令：force 不作用于闭合",
       "--force" not in build_command(root, "某作者", "close_hooks", True))
    try:
        build_command(root, "某作者", "nope")
        ok("命令：拒绝未知动作", False)
    except ValueError:
        ok("命令：拒绝未知动作", True)


def test_plan_slot(adb: AuthorsDB, aid: str) -> None:
    before = len(adb.plans_for(aid, open_only=False))
    adb.upsert_plan(aid, target="sb-1#paratext",
                    hypothesis="换个说法：序跋里可能有写作习惯",
                    stable_book_id="sb-1", book_id="bk-1", scope="paratext",
                    expected_basis="paratext", expected_value="medium",
                    expected_cost=2, list_type="grey", created_by="model_prior")
    after = len(adb.plans_for(aid, open_only=False))
    ok("攻略：同一 target 只有一个坑", after == before,
       f"{before} → {after}")
    row = adb.get_conn().execute(
        "SELECT hypothesis FROM author_exploration_plan WHERE author_id=?"
        " AND target='sb-1#paratext'", (aid,)).fetchone()
    ok("攻略：换措辞只更新假设", "换个说法" in row["hypothesis"])


def build_fixture(tmp: Path) -> tuple[AuthorsDB, BookDB, str]:
    (tmp / "data").mkdir(exist_ok=True)
    bdb = BookDB(tmp / "data" / "books.db")
    bdb.initialize()
    bdb.insert_book(BookRow(id="bk-1", title="测试之书", author="测试作者"))
    adb = AuthorsDB(tmp / "data" / "authors.db")
    aid = adb.upsert_author("测试作者")
    adb.upsert_plan(aid, target="sb-1#paratext",
                    hypothesis="序跋可能含作者生活细节",
                    stable_book_id="sb-1", book_id="bk-1", scope="paratext",
                    expected_basis="paratext", expected_value="high",
                    expected_cost=1, list_type="grey", created_by="rule")
    adb.upsert_plan(aid, target="sb-1#body",
                    hypothesis="正文可能是自述",
                    stable_book_id="sb-1", book_id="bk-1", scope="body",
                    expected_basis="author_statement", expected_value="low",
                    expected_cost=2, list_type="grey", created_by="model_prior")
    return adb, bdb, aid


async def test_routes(tmp: Path, adb: AuthorsDB, bdb: BookDB,
                      aid: str) -> None:
    from aiohttp.test_utils import TestClient, TestServer
    from api_server import create_app
    from config import Config

    cfg = Config()
    cfg.project_dir = tmp
    app = create_app(bdb, config=cfg)
    plans = adb.plans_for(aid, open_only=False)
    pid = plans[0]["plan_id"]
    async with TestClient(TestServer(app)) as client:
        r = await client.get(f"/api/authors/{aid}/plans")
        data = await r.json()
        ok("接口：GET plans 200", r.status == 200)
        ok("接口：攻略条数", len(data.get("items") or []) == len(plans),
           f"{len(data.get('items') or [])}/{len(plans)}")
        ok("接口：带书名", data["items"][0]["book_title"] == "测试之书")
        counts = data.get("counts") or {}
        ok("接口：黑白灰计数", counts.get("grey") == len(plans)
           and counts.get("black") == 0 and counts.get("pending") == len(plans))
        r = await client.patch(f"/api/authors/{aid}/plans/{pid}",
                               json={"list_type": "white"})
        ok("接口：改白名单 200", r.status == 200)
        r = await client.get(f"/api/authors/{aid}/plans")
        got = next(x for x in (await r.json())["items"]
                   if x["plan_id"] == pid)
        ok("接口：白名单已落库", got["list_type"] == "white")
        await client.patch(f"/api/authors/{aid}/plans/{pid}",
                           json={"list_type": "grey"})
        r = await client.patch(f"/api/authors/{aid}/plans/nope",
                               json={"list_type": "black"})
        ok("接口：未知攻略 404", r.status == 404)
        r = await client.patch(f"/api/authors/{aid}/plans/{pid}",
                               json={"list_type": "purple"})
        ok("接口：非法名单 400", r.status == 400)
        r = await client.get(f"/api/authors/{aid}/jobs")
        ok("接口：任务列表为空", r.status == 200
           and (await r.json()).get("items") == [])
        r = await client.get("/api/authors/jobs/nope")
        ok("接口：未知任务 404", r.status == 404)
        r = await client.post(f"/api/authors/{aid}/jobs", json={"action": "x"})
        ok("接口：非法动作 400", r.status == 400)
        r = await client.post("/api/authors/au-nope/jobs",
                              json={"action": "plan"})
        ok("接口：未知作者 404", r.status == 404)
    # 路由注册（TestServer 已证明可达；这里只钉路径形状）
    paths = {r.resource.canonical for r in app.router.routes()}
    for p in ("/api/authors/{author_id}/plans",
              "/api/authors/{author_id}/plans/{plan_id}",
              "/api/authors/{author_id}/jobs",
              "/api/authors/jobs/{job_id}",
              "/api/authors/jobs/{job_id}/cancel"):
        ok(f"路由：{p}", p in paths)


async def test_runner(tmp: Path) -> None:
    orig = jobs_mod.build_command
    jobs_mod.build_command = lambda root, name, action, force=False: [
        sys.executable, "-c",
        "import time; time.sleep(0.3);"
        "print('观察 +7\\u3000钩子 +2\\u3000闭合 1\\u3000失败 0')"]
    try:
        runner = AuthorJobRunner(tmp)
        job, busy = await runner.start(author_id="au-x", author_name="X",
                                       action="plan")
        ok("任务：启动", bool(job) and busy is None)
        snap = runner.get(job["job_id"])
        for _ in range(100):
            await asyncio.sleep(0.05)
            snap = runner.get(job["job_id"])
            if snap["status"] != "running":
                break
        ok("任务：结束", snap["status"] == "done", snap["status"])
        ok("任务：摘要解析",
           snap["summary"] == {"observations": 7, "hooks": 2,
                               "closed": 1, "failed": 0},
           str(snap["summary"]))
        ok("任务：日志落盘", Path(snap["log_path"]).exists())
        ok("任务：按作者过滤", len(runner.list_for("au-x")) == 1
           and runner.list_for("au-y") == [])
        first = await runner.start(author_id="au-y", author_name="Y",
                                   action="plan")
        second, busy2 = await runner.start(author_id="au-z",
                                           author_name="Z", action="plan")
        ok("任务：串行约束", first[0] is not None and second is None
           and busy2 is not None)
        jid = first[0]["job_id"]
        for _ in range(100):
            await asyncio.sleep(0.05)
            if runner.get(jid)["status"] != "running":
                break
        await runner._jobs[jid]["task"]
    finally:
        jobs_mod.build_command = orig


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        adb, bdb, aid = build_fixture(tmp)
        print("命令")
        test_command(tmp)
        print("攻略槽位")
        test_plan_slot(adb, aid)
        print("接口")
        await test_routes(tmp, adb, bdb, aid)
        adb.close()
        bdb.close()
        print("任务")
        await test_runner(tmp)
    print()
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
