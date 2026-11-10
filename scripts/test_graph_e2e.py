"""BookGraph 提取的**零 LLM 端到端**测试 —— 真 HTTP、真提示词、真落库。

前面几个测试各自盯一层（纯函数 / 接线 / 单条 SQL）。这一个把整条链跑通：
``GraphOrchestrator`` → 真 ``run_explorer``/``run_verifier``（走 HTTP）→ 真
``recorder`` → 真 ``graph.db``，中间只有 LLM 被 ``fake_provider`` 换掉。

用 ``/_mock/scenario`` 按**最后一条 user message** 分派：探针的 user message
长什么样，规则就照那个写。所以这个测试同时钉住了「提示词里必须有什么」。

跑法：``python scripts/test_graph_e2e.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph import GraphOrchestrator                        # noqa: E402
from storage.db import BookDB                                  # noqa: E402
from storage.graph_db import GraphDB                           # noqa: E402
from storage.models import BookChapterRow, BookChunkRow, BookRow  # noqa: E402

ROOT = Path(__file__).parent.parent
BOOK_ID = "sb-e2e"
FAILED = 0

# 第二轮**故意**留下未经验证的批次，日志会照实喊 —— 那是它的职责，
# 但测试输出里只该有断言结果。要看日志就单独跑一次。
logging.getLogger("bookgraph").setLevel(logging.ERROR)


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


# ── 一本小散文集：两篇，每篇一块正文 ────────────────────────────────────

TEXT_A = "母亲喜欢花，可自从我的腿瘫痪后，她侍弄的那些花都死了。"
TEXT_B = "我在好几篇小说中都提到过一座废弃的古园，实际上就是地坛。"


def _seed(db: BookDB) -> None:
    db.insert_book(BookRow(id=BOOK_ID, title="测试散文集", author="某人",
                           status="raw", time_created=1, time_updated=1))
    db.insert_chapters([
        BookChapterRow(id="c0", book_id=BOOK_ID, chapter_index=0, title="目录",
                       content="目录", chapter_type="toc", level=0, in_toc=1),
        BookChapterRow(id="c1", book_id=BOOK_ID, chapter_index=1,
                       title="秋天的怀念", content=TEXT_A, chapter_type="body",
                       level=0, in_toc=1),
        BookChapterRow(id="c2", book_id=BOOK_ID, chapter_index=2, title="我与地坛",
                       content=TEXT_B, chapter_type="body", level=0, in_toc=1),
    ])
    db.insert_chunks([
        BookChunkRow(id="k0", book_id=BOOK_ID, chapter_index=0, chunk_index=0,
                     content="目录", char_count=2, paragraph_count=1,
                     paragraph_offsets="[]", paragraph_sigs="[]"),
        BookChunkRow(id="k1", book_id=BOOK_ID, chapter_index=1, chunk_index=0,
                     content=TEXT_A, char_count=len(TEXT_A), paragraph_count=1,
                     paragraph_offsets="[]", paragraph_sigs="[]"),
        BookChunkRow(id="k2", book_id=BOOK_ID, chapter_index=2, chunk_index=0,
                     content=TEXT_B, char_count=len(TEXT_B), paragraph_count=1,
                     paragraph_offsets="[]", paragraph_sigs="[]"),
    ])


# ── 假 LLM 的四个回答 ────────────────────────────────────────────────────

CONTEXT_JSON = {
    "world_view_boundary": "散文集，只提取文中人物与地点",
    "genre": "nonfiction",
    "narrative_structure": "collection",
    "tone_and_themes": "追忆",
    "suggested_kinds": ["character", "location"],
    "entities_to_exclude": ["publisher"],
}

HEAD_SCAN_JSON = {"identity": [], "boundary": "toc", "preface_line_reached": False,
                  "confidence": 0.9}

def _explorer(anchor: str, name: str, kind: str, ev_node: str,
              ev_edge: str, ev_obs: str) -> dict:
    """一批的提取结果。**锚点必须落在本批那一块上** —— 写死成别的块的 id
    会被机械体检查出悬空锚点（这正是它该干的事）。"""
    return {
        "new_nodes": [
            {"canonical_name": name, "kind": kind, "why": "在正文里做了事",
             "evidence": ev_node, "first_appearance_chunk_id": anchor},
        ],
        "new_edges": [
            {"source": name, "target": name, "kind": "appears_in",
             "chunk_id": anchor, "event_type": "fact", "subject": None,
             "evidence": ev_edge, "tags": ["主题"]},
        ],
        "new_observations": [
            {"node_name": name, "field": "role_in_story", "value": "在场",
             "event_type": "fact", "subject": None,
             "evidence_chunk_id": anchor, "evidence": ev_obs, "tags": []},
        ],
        "new_aliases": [], "merge_candidates": [],
        "quotes": [
            {"character_name": name, "quote_text": ev_obs, "chunk_id": anchor,
             "scene_context": "回忆", "related_nodes": []},
        ],
        "summary": "ok",
    }


# 两批各一份，锚点各自落在本批的块上
EXPLORER_A = _explorer("k1", "母亲", "character", "母亲喜欢花",
                       "她侍弄的那些花都死了", "母亲喜欢花")
EXPLORER_B = _explorer("k2", "地坛", "location", "一座废弃的古园",
                       "实际上就是地坛", "一座废弃的古园")

# 第三份：声明了一个**没人引用**的实体 —— 机械体检会报孤儿，回炉也改不好
# （假 provider 每轮返回同一份），于是这批应当被标成「未经验证」。
EXPLORER_ORPHAN = json.loads(json.dumps(EXPLORER_A))
EXPLORER_ORPHAN["new_nodes"].append(
    {"canonical_name": "花", "kind": "item", "why": ""})

VERIFIER_JSON = {"verdict": "upheld", "challenges": [], "summary": "可信"}


def _rules(orphan: bool = False) -> dict:
    """按**最后一条 user message** 分派。规则顺序要紧：证伪趟的 message 里
    也含正文与 chunk_id 标记，必须先匹配它。"""
    return {"rules": [
        # 裁决趟：看到编号表就答「没问题」。它必须排在证伪规则**前面** ——
        # 两条消息都含正文与 chunk_id 标记，先匹配到谁就答谁的。
        {"contains": "待裁决的断言",
         "response": {"text": json.dumps(
             {"verdict": "upheld", "fixes": [], "summary": ""},
             ensure_ascii=False)}},
        {"contains": "从这批正文里提取出的断言",
         "response": {"text": json.dumps(VERIFIER_JSON, ensure_ascii=False)}},
        {"contains": "目录（顶层单元",
         "response": {"text": json.dumps(CONTEXT_JSON, ensure_ascii=False)}},
        {"contains": "前史",
         "response": {"text": json.dumps(HEAD_SCAN_JSON, ensure_ascii=False)}},
        {"contains": "母亲喜欢花",
         "response": {"text": json.dumps(
             EXPLORER_ORPHAN if orphan else EXPLORER_A, ensure_ascii=False)}},
        {"contains": "废弃的古园",
         "response": {"text": json.dumps(EXPLORER_B, ensure_ascii=False)}},
    ]}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


async def _drive(base: str, db: BookDB, gdb: GraphDB, project_dir: Path) -> dict:
    from config import Config
    cfg = Config()
    cfg.api_base = base
    cfg.api_key = "fake"
    cfg.model = "fake-model"
    # 测试绝不能写生产审计链（决策 147）：orchestrator 的 JSONL 路径、
    # usage_log 的归属都从 project_dir 推导。config.project_dir 默认是
    # cwd，而 check_all 从仓库根跑 —— 不隔离时 sb-e2e 会占掉
    # data/logs/bookgraph.jsonl 的一半，fake-model 也会混进用量统计。
    cfg.project_dir = project_dir
    import usage_log
    usage_log.init_for_tests(project_dir / "sessions.db")
    events: list[dict] = []
    orch = GraphOrchestrator(cfg, db, gdb, Path("x"))
    res = await orch.run(BOOK_ID, batch_size=5, progress_cb=events.append,
                         use_scout=False, run_skeleton_pass=False)
    return {"result": res, "events": events}


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "fake_provider.py"),
         "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(f"{base}/_mock/requests", timeout=3).read()
                break
            except Exception:
                if time.monotonic() > deadline:
                    print("fake provider 没起来")
                    return 1
                time.sleep(0.2)
        _post(f"{base}/_mock/scenario", _rules())

        # ignore_cleanup_errors：Windows 上句柄没放干净时清理会抛，把真正
        # 的断言失败盖掉 —— 那正是最需要看见的东西。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = BookDB(Path(tmp) / "books.db")
            db.initialize()
            gdb = GraphDB(Path(tmp) / "graph.db")
            _seed(db)
            try:
                out = asyncio.run(_drive(base, db, gdb, Path(tmp)))
            except Exception:
                import traceback
                traceback.print_exc()
                check("端到端跑通（异常见上）", False, True)
                db.close()
                gdb.close()
                print(f"\n{'FAILED ' + str(FAILED) if FAILED else 'ALL PASS'}")
                return 1
            conn = gdb.get_conn()

            names = {r["canonical_name"] for r in conn.execute(
                "SELECT canonical_name FROM nodes WHERE project_id=? "
                "AND kind!='chapter'", (BOOK_ID,))}
            check("两批的实体都建了", names, {"母亲", "地坛"})
            # 准入闸的保证：图里不存在「没有任何关系也没有任何观察」的节点
            check("图里没有孤儿（准入闸的构造性保证）", conn.execute(
                "SELECT COUNT(*) FROM nodes n WHERE n.project_id=? "
                "AND n.kind!='chapter' AND NOT EXISTS("
                "  SELECT 1 FROM edges e WHERE e.source_id=n.id OR e.target_id=n.id)"
                " AND NOT EXISTS(SELECT 1 FROM graph_observations o "
                "                WHERE o.node_id=n.id)", (BOOK_ID,)).fetchone()[0], 0)

            # 两批的落库顺序由异步完成顺序决定，fetchone() 不能假定 k1
            # 先到；按锚点块取，顺带把两批的逐字原文都钉住。
            edge_ev = {r[0]: r[1] for r in conn.execute(
                "SELECT chunk_id, evidence_text FROM edges WHERE project_id=?",
                (BOOK_ID,))}
            check("边的逐字原文落库",
                  (edge_ev.get("k1"), edge_ev.get("k2")),
                  ("她侍弄的那些花都死了", "实际上就是地坛"))
            obs_ev = {r[0]: r[1] for r in conn.execute(
                "SELECT evidence, evidence_text FROM graph_observations "
                "WHERE project_id=?", (BOOK_ID,))}
            check("观察的逐字原文落库",
                  (obs_ev.get("k1"), obs_ev.get("k2")),
                  ("母亲喜欢花", "一座废弃的古园"))

            # 每条引文锚在**它自己**那一块 —— 两批各一块，不能都挤到 k1
            q = conn.execute("SELECT chunk_id, batch_id FROM graph_quotes "
                             "WHERE project_id=? ORDER BY chunk_id",
                             (BOOK_ID,)).fetchall()
            check("引文各锚各的块", [r["chunk_id"] for r in q], ["k1", "k2"])
            check("引文 batch_id 是真的（不是 batch-<块id>）",
                  all(r["batch_id"].startswith("batch-")
                      and r["batch_id"] != f"batch-{r['chunk_id']}" for r in q),
                  True)

            meta = conn.execute("SELECT value FROM graph_meta WHERE key='scope_plan'"
                                ).fetchone()
            plan = json.loads(meta["value"]) if meta else {}
            check("集类书真的划了域（语义裁决被消费）",
                  (plan.get("source"), len(plan.get("scopes") or [])),
                  ("collection", 2))

            # 目录页那块被体例裁掉，绝不该进批次
            st = {r["chunk_id"]: r["extract_status"] for r in conn.execute(
                "SELECT chunk_id, extract_status FROM graph_chunks "
                "WHERE book_id=?", (BOOK_ID,))}
            check("目录块被裁、正文块已提", (st.get("k0"), st.get("k1")),
                  ("excluded", "done"))

            bad = [e for e in out["events"] if e.get("type") == "batch_unverified"]
            check("干净批次不报未经验证", len(bad), 0)
            check("提取本身没失败", out["result"].chunks_failed, 0)
            gdb.close()
            db.close()

        # ── 第二轮：声明了没人引用的实体 → 必须被标成未经验证 ───────────
        _post(f"{base}/_mock/scenario", _rules(orphan=True))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = BookDB(Path(tmp) / "books.db")
            db.initialize()
            gdb = GraphDB(Path(tmp) / "graph.db")
            _seed(db)
            out = asyncio.run(_drive(base, db, gdb, Path(tmp)))
            conn = gdb.get_conn()
            bad = [e for e in out["events"] if e.get("type") == "batch_unverified"]
            # **孤儿现在是提示项，不是阻塞项。** 落库层按构造丢弃孤儿
            # （只给被引用的名字建节点），所以「声明了没人引用的实体」是一
            # 件已经处理完的事 —— 它曾经会触发回炉，实测只是让每一批都带着
            # 一条永远消不掉的缺陷被判未经验证。真正的「沉默不是成功」由
            # test_graph_orchestrator 的「证伪/裁决趟哑了 → 未经验证」守着。
            check("孤儿不再触发未验证（提示项，准入闸已接住）", len(bad), 0)
            names = {r["canonical_name"] for r in conn.execute(
                "SELECT canonical_name FROM nodes WHERE project_id=?",
                (BOOK_ID,))}
            check("没被引用的实体最终没进图（准入闸兜住）", "花" in names, False)
            gdb.close()
            db.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{'FAILED ' + str(FAILED) if FAILED else 'ALL PASS'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
