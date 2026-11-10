"""会话按页面分线 + 写作会话按作品归档（零 LLM）。

盯四件事：

1. 写作页的会话只被写作页收录：``mode=writing`` / ``exclude_mode=writing``
   在列表与搜索里都生效（搜索不能成为后门）；
2. 写作会话落在 ``sessions.work_id`` 上，列表返回作品标题；
3. 旧库（没有 work_id 列）打开时自动补列（迁移 14）；
4. 加载单条会话时带 ``work_id``，前端才能把写作台切回那一本。

跑法：``python scripts/test_session_lines.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aiohttp.test_utils import TestClient, TestServer          # noqa: E402

import api_server                                              # noqa: E402
from config import Config                                      # noqa: E402
from content import ContentBlock, ConversationMessage          # noqa: E402
from services.session_service import SessionService            # noqa: E402
from storage.db_session import _SESSION_MIGRATIONS             # noqa: E402
from services.writing_service import WritingService            # noqa: E402
from storage.db_session import SessionDB                       # noqa: E402
from storage.writing_db import WritingDB                       # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"ok   {label}")
    else:
        FAILED += 1
        print(f"FAIL {label}\n       got  = {got!r}\n       want = {want!r}")


class NoBookDB:
    def list_books(self):
        return []


def _add(ssvc, sid, *, mode, text, work_id="", project_id=""):
    ssvc.append_message(
        sid,
        ConversationMessage(role="user",
                            blocks=[ContentBlock(type="text", text=text)]),
        mode=mode, title=text, project_id=project_id, work_id=work_id,
    )


async def _run(tmp: Path) -> None:
    class TestConfig(Config):
        @property
        def writing_db_path(self) -> Path:
            return tmp / "data" / "writing.db"

    cfg = TestConfig(project_dir=ROOT)
    sdb = SessionDB(tmp / "data" / "sessions.db")
    sdb.initialize()
    ssvc = SessionService(sdb, cfg)
    wsvc = WritingService(cfg, db=WritingDB(cfg.writing_db_path),
                          api_client=None)
    w1 = wsvc.create_work("某城来信")
    w2 = wsvc.create_work("长夜行")

    _add(ssvc, "s-w1", mode="writing", text="某城的雨一直没停", work_id=w1)
    _add(ssvc, "s-w2", mode="writing", text="长夜行也下雨", work_id=w2)
    _add(ssvc, "s-empty", mode="writing", text="先不建书，聊聊开场")
    _add(ssvc, "s-read", mode="immersive", text="某城的雨落在书页上",
         project_id="b1")
    _add(ssvc, "s-home", mode="immersive", text="主页闲聊")

    # ── 服务层：分线过滤 ─────────────────────────────────────────
    writing = [s.id for s in ssvc.list(mode="writing", root_only=True)]
    check("列表：mode=writing 只要写作会话",
          set(writing), {"s-w1", "s-w2", "s-empty"})
    others = [s.id for s in ssvc.list(exclude_mode="writing", root_only=True)]
    check("列表：排除写作会话", set(others), {"s-read", "s-home"})
    only_w1 = [s.id for s in ssvc.list(work_id=w1, root_only=True)]
    check("列表：work_id 过滤", only_w1, ["s-w1"])

    hits = ssvc.search_content("雨", mode="writing")
    check("搜索：只回写作会话",
          {h["session_id"] for h in hits}, {"s-w1", "s-w2"})
    hits = ssvc.search_content("雨", exclude_mode="writing")
    check("搜索：排除写作会话",
          {h["session_id"] for h in hits}, {"s-read"})
    check("搜索：结果带 work_id",
          next(h for h in hits if h["session_id"] == "s-read")["work_id"], "")

    # ── 路由：列表带作品标题、加载带 work_id ──────────────────────
    app = api_server.create_app(NoBookDB(), config=cfg, session_service=ssvc)
    app["writing_service"] = wsvc
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/sessions/list?mode=writing&root_only=true")
        data = await resp.json()
        rows = {row["id"]: row for row in data.get("sessions") or []}
        check("API：只给写作会话", set(rows), {"s-w1", "s-w2", "s-empty"})
        check("API：带作品标题", rows["s-w1"]["work_title"], "某城来信")
        check("API：空项目会话 work_id 为空", rows["s-empty"]["work_id"], "")

        resp = await client.get(
            "/api/sessions/list?exclude_mode=writing&root_only=true")
        data = await resp.json()
        rows = {row["id"]: row for row in data.get("sessions") or []}
        check("API：其它页拿不到写作会话", set(rows), {"s-read", "s-home"})

        resp = await client.get(
            "/api/sessions/search?q=%E9%9B%A8&exclude_mode=writing")
        data = await resp.json()
        check("API：搜索排除写作会话",
              {r["session_id"] for r in data.get("results") or []}, {"s-read"})

        resp = await client.get("/api/sessions/s-w1")
        data = await resp.json()
        check("API：加载会话带 work_id", data.get("work_id"), w1)

    # ── 迁移：旧库没有 work_id，打开时补列 ───────────────────────
    conn = sdb.get_conn()
    conn.execute("DROP INDEX IF EXISTS idx_sessions_work")
    conn.execute("ALTER TABLE sessions DROP COLUMN work_id")
    conn.execute("DELETE FROM schema_version WHERE version>=14")
    conn.commit()
    sdb.close()
    sdb2 = SessionDB(tmp / "data" / "sessions.db")
    sdb2.initialize()
    cols = [r[1] for r in sdb2.get_conn().execute(
        "PRAGMA table_info(sessions)")]
    check("迁移：旧库补出 work_id", "work_id" in cols, True)
    check("迁移：写作模式列", "writing_mode" in cols, True)
    check("迁移：自动切模式授权列", "auto_mode_switch" in cols, True)
    check("迁移：版本记录到最新",
          sdb2.get_conn().execute(
              "SELECT MAX(version) FROM schema_version").fetchone()[0],
          max(ver for ver, _, _ in _SESSION_MIGRATIONS))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bc-session-lines-")) / "data"
    tmp.mkdir(parents=True)
    try:
        asyncio.run(_run(tmp))
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
