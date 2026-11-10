"""工序 6 的**零 LLM 端到端** —— 真提示词、真校验、真落库。

联网那两跳（搜索、抓页）被换成固定结果：这一趟要验的是
「页面正文进来之后发生了什么」，不是 Tavily/Wikidata 通不通。
模型那一跳仍然是真的 HTTP —— 打到 ``scripts/fake_provider.py``，
所以提示词里必须有什么、输出形状怎么收，都是被钉住的。

它同时钉住 v2 证据链的三条：

1. 引文能在页面正文里逐字找到 → 落 ``author_evidence``（带 quote）。
2. 引文找不到 → 整条作废（``quote_not_found``），**不降级成 web**。
3. 来源角色不支持该 target（维基不支撑接受史）→ ``role_mismatch``。

跑法：``python scripts/test_author_enrich_e2e.py``（退出码 0 = 全绿）。
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

import aiohttp                                                   # noqa: E402

from authorgraph import enrich as E                              # noqa: E402
from authorgraph.sources import extract as X                     # noqa: E402
from authorgraph.sources import pipeline as P                    # noqa: E402
from storage.authors_db import AuthorsDB                         # noqa: E402

ROOT = Path(__file__).parent.parent
FAILED = 0

logging.getLogger("authorgraph").setLevel(logging.ERROR)

NAME = "测试作者"
URL = "https://zh.wikipedia.org/wiki/测试作者"
PAGE_TEXT = ("测试作者（1951年—2010年），中国作家。\n"
             "1980 年秋，肾衰初发。")

#: 三条 claim：一条真、一条编引文、一条 target 与来源角色不匹配。
REPLY = {
    "claims": [
        {"target": "biography", "kind": "life_event", "year": 1980,
         "statement": "肾衰初发",
         "evidence_quote": "1980 年秋，肾衰初发", "confidence": "high"},
        {"target": "biography", "kind": "life_event", "year": 1999,
         "statement": "编出来的事",
         "evidence_quote": "这句话页面里没有", "confidence": "high"},
        {"target": "reception", "kind": "life_event",
         "statement": "他是重要作家",
         "evidence_quote": "中国作家", "confidence": "medium"},
    ],
    "notes": ["没找到出生地"],
}


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def _rules() -> dict:
    return {"rules": [{"contains": NAME,
                       "response": {"text": json.dumps(REPLY,
                                                       ensure_ascii=False)}}]}


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


async def _fake_search(session, cfg_, query, *, max_results=8):
    return {"hits": [{"title": "测试作者 - 维基百科", "url": URL,
                      "content": "摘要不是证据"}],
            "warnings": [], "provider": "fake"}


async def _fake_fetch(session, url, **kw):
    return {"url": url, "canonical_url": URL, "title": "测试作者",
            "publisher": "维基百科", "language": "zh",
            "main_text": PAGE_TEXT, "content_hash": "hash-1",
            "content_excerpt": PAGE_TEXT[:200], "published_at": "",
            "status": "ok", "http_status": 200}


async def _no_wikidata(session, name, **kw):
    return []


async def _drive(base: str, adb: AuthorsDB, tmp: str) -> dict:
    from config import Config
    cfg = Config()
    cfg.api_base = base
    cfg.api_key = "fake"
    cfg.model = "fake-model"
    # 临时 project_dir：否则 web_deepseek.resolve() 会翻到仓库里真实的
    # data/config/providers.json，把替身搜索/抓页顶掉。
    cfg.project_dir = Path(tmp)

    # 模型绑定会把假端点顶掉：extract_page 内部走
    # ``config_for(cfg, "authorgraph.enrich")``，而 Config.project_dir 默认
    # 是 cwd —— 从仓库根跑测试会读到真实的 providers.json。
    X.config_for = lambda config, scenario: config      # type: ignore[assignment]
    P.web_search = _fake_search                         # type: ignore[assignment]
    P.fetch_page = _fake_fetch                          # type: ignore[assignment]
    P.wikidata_candidates = _no_wikidata                # type: ignore[assignment]

    author = {"author_id": "au-e2e", "canonical_name": NAME,
              "nationality": "中国", "identity_status": "resolved",
              "_books": [{"title": "某本书"}]}
    async with aiohttp.ClientSession() as session:
        return await E.enrich_life(session, cfg, adb, author)


def _assert(out: dict, tmp: str) -> int:
    for w in (out.get("warnings") or []):
        print(f"     （warning：{w}）")
    check("e2e：抓了 1 页", out.get("fetched"), 1)
    check("e2e：抽出 3 条", out.get("claims_extracted"), 3)
    check("e2e：通过 1 条", out.get("claims_verified"), 1)
    check("e2e：编造的引文被拦", out.get("quote_not_found"), 1)
    check("e2e：角色不支持被拦", out.get("role_mismatch"), 1)
    check("e2e：新增记录 1 条", out.get("new_records"), 1)
    ok("e2e：notes 进了缺陷清单",
       any("没找到出生地" in d for d in (out.get("defects") or [])))

    adb = AuthorsDB(Path(tmp) / "authors.db")
    rows = adb.records_for("au-e2e", limit=50)
    check("e2e：记录落库", len(rows), 1)
    r = rows[0]
    check("e2e：来源是 web", r["source"], "web")
    check("e2e：旧字段带引文", r["evidence_excerpt"], "1980 年秋，肾衰初发")
    ev = adb.evidence_for("record", str(r["id"]))
    check("e2e：证据一条", len(ev), 1)
    check("e2e：证据引文逐字", ev[0]["quote"], "1980 年秋，肾衰初发")
    check("e2e：证据关系 supports", ev[0]["relation"], "supports")
    check("e2e：来源 tier=2", ev[0]["source_tier"], 2)
    check("e2e：来源 canonical 归一", ev[0]["canonical_url"], URL)
    check("e2e：bio_status=partial",
          adb.get_author("au-e2e")["bio_status"], "partial")
    check("e2e：编造引文没有落库",
          any("编出来的事" in (x["statement"] or "") for x in rows), False)
    check("e2e：重跑跳过已抓页面",
          asyncio.run(_drive(_BASE[0], adb, tmp)).get("fetched"), 0)
    check("e2e：重跑不新增", adb.stats("au-e2e")["records"], 1)
    adb.close()

    print("\n全绿" if not FAILED else f"\n**{FAILED} 项失败**")
    return 1 if FAILED else 0


_BASE = [""]


def ok(label: str, cond) -> None:
    check(label, bool(cond), True)


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    _BASE[0] = base
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

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            # 真实 enrich 管线会调 usage_log；不指到临时库就会把 fake-model
            # 写进生产 data/sessions.db（决策 147）。
            from usage_log import init_for_tests
            init_for_tests(Path(tmp) / "sessions.db")
            adb = AuthorsDB(Path(tmp) / "authors.db")
            adb.upsert_author(NAME)
            adb.get_conn().execute(
                "UPDATE authors SET author_id='au-e2e', identity_status='resolved'"
                " WHERE canonical_name=?", (NAME,))
            adb.get_conn().commit()
            try:
                out = asyncio.run(_drive(base, adb, tmp))
            finally:
                adb.close()
            return _assert(out, tmp)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:                                # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
