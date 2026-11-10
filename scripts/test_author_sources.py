"""来源管线的零 token 单测 + v1→v3 迁移演练。

盯的六件事：

1. **URL 归一与来源分级** —— 尾斜杠/UTM/www 变体必须落到同一个 canonical。
2. **逐字引文校验** —— 空白与全半角可归一；编的引文必须拦下。
3. **合并与冲突** —— 同键合并证据，同句不同年只报告不覆盖。
4. **查询计划** —— 身份不明时先只跑身份（同名串账是不可逆的）。
5. **身份判定** —— 只有名字相同 → provisional；两个高分候选 → conflict。
6. **迁移** —— 旧库重建后行数/id 不变，旧 web 记录落 legacy_url_only。

跑法：``python scripts/test_author_sources.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from authorgraph.identity import Candidate, decide, score_candidate   # noqa: E402
from authorgraph.sources import pipeline as P                          # noqa: E402
from authorgraph.sources.merge import (classify, index_existing,       # noqa: E402
                                        merge_claims, norm_statement)
from authorgraph.sources.query_plan import plan_queries               # noqa: E402
from authorgraph.sources.registry import (canonical_url, roles_of,     # noqa: E402
                                          source_kind_of, tier_of)
from authorgraph.sources.verify import quote_located, verify_claim     # noqa: E402
from storage.authors_db import (AUTHORS_DDL_V1, AuthorsDB,             # noqa: E402
                                _now_ms)

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def ok(label: str, cond) -> None:
    check(label, bool(cond), True)


# ── 1. 来源分级 ──────────────────────────────────────────────────────────

def test_registry() -> None:
    base = "https://zh.wikipedia.org/wiki/史铁生"
    check("URL：去 fragment", canonical_url(base + "#生平"),
          "https://zh.wikipedia.org/wiki/史铁生")
    check("URL：去 UTM",
          canonical_url(base + "?utm_source=x&page=2"),
          "https://zh.wikipedia.org/wiki/史铁生?page=2")
    check("URL：www 与尾斜杠归一",
          canonical_url("https://www.example.com/a/"),
          canonical_url("https://example.com/a"))
    check("URL：host 大小写",
          canonical_url("https://EXAMPLE.com/a"),
          canonical_url("https://example.com/a"))
    check("tier：维基=2", tier_of("https://zh.wikipedia.org/wiki/x"), 2)
    check("tier：百科=4", tier_of("https://baike.baidu.com/item/x"), 4)
    check("tier：语录站=5", tier_of("https://www.mingyan.com/x"), 5)
    check("kind：访谈", source_kind_of("https://www.theparisreview.org/i/x"),
          "author_interview")
    ok("roles：tier4 为空", roles_of("reference", 4) == ())
    ok("roles：访谈含 voice", "voice" in roles_of("author_interview", 3))


# ── 2. 逐字引文校验 ──────────────────────────────────────────────────────

PAGE = {"main_text": "史铁生（1951年—2010年），中国作家。\n1980 年秋，肾衰初发。",
        "url": "https://zh.wikipedia.org/wiki/史铁生"}


def test_verify() -> None:
    ok("引文：空白归一后命中", quote_located("1980年秋，肾衰初发。", PAGE["main_text"]))
    ok("引文：全半角归一", quote_located(
        "史铁生（1951年—2010年），中国作家。",
        "史铁生(1951年—2010年),中国作家。"))
    v = verify_claim({"target": "biography", "kind": "life_event", "year": 1980,
                      "statement": "肾衰初发",
                      "evidence_quote": "1980 年秋，肾衰初发"}, PAGE,
                     roles=("biography",), tier=2)
    check("引文：通过", v["ok"], True)
    v = verify_claim({"target": "biography", "statement": "他 1960 年去了西藏",
                      "evidence_quote": "1960 年去了西藏"}, PAGE,
                     roles=("biography",), tier=2)
    check("引文：编的引文被拦", (v["ok"], v["code"]), (False, "quote_not_found"))
    v = verify_claim({"target": "biography", "statement": "肾衰",
                      "evidence_quote": "1980 年秋，肾衰初发", "year": 1999},
                     PAGE, roles=("biography",), tier=2)
    check("引文：年份不在引文里 → 清空", v["claim"]["year"], None)
    v = verify_claim({"target": "voice", "statement": "x", "evidence_quote": "肾衰初发"},
                     PAGE, roles=("biography",), tier=2)
    check("引文：角色不支持 → 拒绝", (v["ok"], v["code"]), (False, "role_mismatch"))
    v = verify_claim({"target": "biography", "statement": "x",
                      "evidence_quote": "肾衰初发"}, PAGE,
                     roles=("biography",), tier=4)
    check("引文：低 tier → 拒绝", (v["ok"], v["code"]), (False, "tier_too_low"))
    v = verify_claim({"target": "biography", "statement": "x", "evidence_quote": ""},
                     PAGE, roles=("biography",), tier=2)
    check("引文：空引文 → 拒绝", (v["ok"], v["code"]), (False, "quote_empty"))


# ── 3. 合并与冲突 ────────────────────────────────────────────────────────

def test_merge() -> None:
    a = {"kind": "life_event", "year": 1980, "statement": "肾衰初发。",
         "evidence": [{"source_id": "s1", "quote": "q1"}]}
    b = {"kind": "life_event", "year": 1980, "statement": "肾衰初发",
         "evidence": [{"source_id": "s2", "quote": "q2"}]}
    got = merge_claims([a, b])
    check("合并：同键只留一条", len(got), 1)
    check("合并：证据合并", len(got[0]["evidence"]), 2)
    existing = index_existing([{"kind": "life_event", "year": 1979,
                                "statement": "肾衰初发"}])
    check("合并：同句不同年 → conflict",
          classify({"kind": "life_event", "year": 1980,
                    "statement": "肾衰初发。"}, existing), "conflict")
    check("合并：归一后相同 → duplicate",
          classify({"kind": "life_event", "year": 1979,
                    "statement": "肾衰初发。"}, existing), "duplicate")
    check("合并：归一化去尾标点", norm_statement(" A  B。 "), "AB")


# ── 4. 查询计划 ──────────────────────────────────────────────────────────

def test_plan() -> None:
    author = {"canonical_name": "某人"}
    cov = {"missing": ["biography", "bibliography"], "identity_status": "provisional",
           "coverage": {"identity": 0.2}, "counts": {}}
    qs = plan_queries(author, cov, max_queries=6)
    check("计划：身份不明时只跑身份", [q["facet"] for q in qs], ["identity"])
    cov2 = {"missing": ["bibliography", "biography"], "identity_status": "resolved",
            "coverage": {"identity": 1.0}, "counts": {"bibliography": 0}}
    qs2 = plan_queries(author, cov2, max_queries=6)
    check("计划：书目先于生平", [q["facet"] for q in qs2][:2],
          ["bibliography", "biography"])
    qs3 = plan_queries(author, cov, max_queries=3, strict_identity=False)
    check("计划：放行后身份仍排第一", qs3[0]["facet"], "identity")
    ok("计划：放行后不再只跑身份", len(qs3) > 1)


# ── 5. 身份判定 ──────────────────────────────────────────────────────────

def test_identity() -> None:
    c1 = Candidate(value="Q1", label="史铁生", birth_year=1951, death_year=2010)
    s = score_candidate(c1, name="史铁生", birth_year=1951, books=["我与地坛"])
    ok("身份：名字+生年高分", s >= 0.55)
    d = decide([(c1, 0.85)])
    check("身份：唯一高分 → resolved", d.status, "resolved")
    c2 = Candidate(value="Q2", label="史铁生")
    d = decide([(c1, 0.85), (c2, 0.80)])
    check("身份：两个高分打平 → conflict", d.status, "conflict")
    d = decide([(Candidate(value="Q3", label="别人"), 0.3)])
    check("身份：弱证据 → provisional", d.status, "provisional")
    d = decide([(Candidate(value="Q9", label="某人"), 0.3)], unique_exact=True)
    check("身份：唯一精确同名 → resolved（打破死锁）", d.status, "resolved")
    check("身份：外部 ID 直接命中",
          score_candidate(c1, name="x", known_identifier="Q1"), 1.0)


# ── 6. v1 → v3 迁移演练 ──────────────────────────────────────────────────

def _make_v1(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    for stmt in AUTHORS_DDL_V1:
        conn.execute(stmt)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY,"
                 " applied_at INTEGER NOT NULL, description TEXT)")
    conn.execute("INSERT INTO schema_version VALUES (1, ?, 'v1')", (_now_ms(),))
    now = _now_ms()
    conn.execute(
        "INSERT INTO authors (author_id, canonical_name, nationality, birth_year,"
        " death_year, era_note, bio_status, needs_refresh, last_refresh_at,"
        " time_created, time_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("au-1", "测试作者", "中国", None, None, "", "none", 0, None, now, now))
    conn.execute(
        "INSERT INTO author_books (author_id, stable_book_id, book_id, role,"
        " position, position_source, position_note, time_updated)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("au-1", "sb-1", "bk-1", "author", "narrator", "corpus", "", now))
    conn.execute(
        "INSERT INTO author_corpus (id, author_id, stable_book_id, book_id,"
        " chunk_id, chapter_index, chunk_index, excerpt, role, speaker,"
        " text_kind, era_year, era_note, source, trust, time_created)"
        " VALUES (7,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("au-1", "sb-1", "bk-1", "ch-1", 0, 0, "逐字原文一段", "author",
         "author", "preface", None, "", "corpus", 0.5, now))
    conn.execute(
        "INSERT INTO author_records (id, author_id, kind, year, year_end,"
        " statement, evidence_excerpt, stable_book_id, chunk_id, source,"
        " trust, time_created) VALUES (11,?,?,?,?,?,?,?,?,?,?,?)",
        ("au-1", "life_event", 1980, None, "某件事",
         "https://zh.wikipedia.org/wiki/X?utm_source=a", None, None,
         "web", 0.4, now))
    conn.commit()
    conn.close()


def test_migration() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "authors.db"
        _make_v1(path)
        adb = AuthorsDB(path)
        c = adb.get_conn()
        check("迁移：版本=5", c.execute(
            "SELECT MAX(version) FROM schema_version").fetchone()[0], 5)
        ok("迁移：攻略表已建", c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='author_exploration_plan'").fetchone() is not None)
        ok("迁移：观察表已建", c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='author_observations'").fetchone() is not None)
        ok("迁移：钩子表已建", c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='author_hooks'").fetchone() is not None)
        check("迁移：语料行数不变", c.execute(
            "SELECT COUNT(*) FROM author_corpus").fetchone()[0], 1)
        row = c.execute("SELECT * FROM author_corpus WHERE id=7").fetchone()
        check("迁移：语料 id 不变", row["id"], 7)
        ok("迁移：语料接上来源", bool(row["source_id"]))
        check("迁移：word_count", row["word_count"], len("逐字原文一段"))
        check("迁移：layer=paratext", row["narrative_layer"], "paratext")
        check("迁移：来源两条（书+网页）", c.execute(
            "SELECT COUNT(*) FROM author_sources").fetchone()[0], 2)
        ev = c.execute("SELECT * FROM author_evidence WHERE target_id='11'"
                       ).fetchone()
        check("迁移：旧 web 证据标 legacy", ev["relation"], "legacy_url_only")
        check("迁移：旧 web 证据无引文", ev["quote"], "")
        check("迁移：备份表保留旧行", c.execute(
            "SELECT COUNT(*) FROM author_corpus_v1_backup").fetchone()[0], 1)
        # 幂等：再打开一次不重复迁移、不追加行
        adb.close()
        adb = AuthorsDB(path)
        check("迁移：重开仍为 1 条语料", adb.get_conn().execute(
            "SELECT COUNT(*) FROM author_corpus").fetchone()[0], 1)
        check("迁移：重开仍为 2 条来源", adb.get_conn().execute(
            "SELECT COUNT(*) FROM author_sources").fetchone()[0], 2)
        # 旧写入路径的幂等键还在
        check("迁移：add_corpus 仍幂等", adb.add_corpus(
            "au-1", "sb-1", "逐字原文一段", chunk_id="ch-1"), False)
        cid = adb.add_claim("au-1", "theme", "研究者认为他反复写记忆")
        ok("claims：写入可取回",
           cid and adb.claims_for("au-1")[0]["statement"] == "研究者认为他反复写记忆")
        adb.close()


# ── 7. 管线端到端（全替身，零网络零 token）────────────────────────────────

def test_pipeline() -> None:
    from config import Config
    cfg = Config()

    async def fake_search(session, cfg_, query, *, max_results=8):
        return {"hits": [{"title": "t", "url": HIT_URL, "content": "摘要不是证据"}],
                "warnings": [], "provider": "fake"}

    async def fake_fetch(session, url, **kw):
        return {"url": url, "canonical_url": HIT_URL, "title": "史铁生",
                "publisher": "维基", "language": "zh",
                "main_text": "史铁生（1951年—2010年），中国作家。\n1980 年秋，肾衰初发。",
                "content_hash": "h1", "content_excerpt": "史铁生…",
                "published_at": "", "status": "ok", "http_status": 200}

    async def fake_extract(session, cfg_, page, author, *,
                           existing=None, log_cb=None):
        return {"status": "ok", "defects": [], "notes": [], "claims": [
            {"target": "biography", "kind": "life_event", "year": 1980,
             "statement": "肾衰初发", "evidence_quote": "1980 年秋，肾衰初发",
             "locator": "", "confidence": 0.85},
            {"target": "biography", "kind": "life_event",
             "statement": "编出来的事", "evidence_quote": "这句页面里没有",
             "locator": "", "confidence": 0.8},
            {"target": "reception", "kind": "life_event",
             "statement": "他是中国作家", "evidence_quote": "中国作家",
             "locator": "", "confidence": 0.6},
        ]}

    real = (P.web_search, P.fetch_page, P.extract_page, P.wikidata_candidates)
    P.web_search, P.fetch_page, P.extract_page = fake_search, fake_fetch, fake_extract
    P.wikidata_candidates = _no_candidates
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            adb = AuthorsDB(Path(tmp) / "authors.db")
            aid = adb.upsert_author("史铁生")
            author = {"author_id": aid, "canonical_name": "史铁生",
                      "identity_status": "resolved"}
            out = asyncio.run(P.enrich_author(None, cfg, adb, author))
            check("管线：抓了 1 页", out["fetched"], 1)
            check("管线：抽出 3 条", out["claims_extracted"], 3)
            check("管线：通过 1 条", out["claims_verified"], 1)
            check("管线：编的引文被拦", out["quote_not_found"], 1)
            # 维基这类 reference 来源不支撑接受史 —— 角色校验拒绝，不是漏掉
            check("管线：角色不支持被拦", out["role_mismatch"], 1)
            check("管线：新增记录 1 条", out["new_records"], 1)
            rows = adb.records_for(aid, limit=20)
            check("管线：记录写入了", len(rows), 1)
            ev = adb.evidence_for("record", str(rows[0]["id"]))
            ok("管线：证据带逐字引文", ev and ev[0]["quote"] == "1980 年秋，肾衰初发")
            check("管线：bio_status=partial", adb.get_author(aid)["bio_status"],
                  "partial")
            # 第二趟：同一页已抓过 → 跳过，不重复落
            out2 = asyncio.run(P.enrich_author(None, cfg, adb, author))
            check("管线：已抓页面跳过", out2["fetched"], 0)
            check("管线：重跑不新增", out2["new_records"], 0)
            check("管线：来源仍只有 1 条", adb.get_conn().execute(
                "SELECT COUNT(*) FROM author_sources").fetchone()[0], 1)
            adb.close()
    finally:
        (P.web_search, P.fetch_page, P.extract_page,
         P.wikidata_candidates) = real


HIT_URL = "https://zh.wikipedia.org/wiki/史铁生"


def test_identity_apply() -> None:
    """身份判定为 resolved 时，要落外部 ID 并回填生卒。"""
    async def fake_cands(session, name, **kw):
        return [Candidate(value="Q-T1", label="测试作者", birth_year=1951,
                          death_year=2010, nationality="中国",
                          works=["测试作品"])]

    real = P.wikidata_candidates
    P.wikidata_candidates = fake_cands                    # type: ignore
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            adb = AuthorsDB(Path(tmp) / "authors.db")
            # 生年已在库里（评分用它），身份判定后才能确认是这个人
            aid = adb.upsert_author("测试作者", birth_year=1951)
            author = dict(adb.get_author(aid), _book_titles=["测试作品"])
            out = asyncio.run(P._resolve_identity(None, adb, author))
            check("身份：判定 resolved", out["status"], "resolved")
            row = adb.get_author(aid)
            check("身份：状态回写", row["identity_status"], "resolved")
            check("身份：生年保留", row["birth_year"], 1951)
            ids = adb.identifiers_of(aid)
            check("身份：外部 ID 落库", ids[0]["value"], "Q-T1")
            adb.close()
    finally:
        P.wikidata_candidates = real                      # type: ignore


async def _no_candidates(session, name, **kw):
    return []


def main() -> int:
    test_registry()
    test_verify()
    test_merge()
    test_plan()
    test_identity()
    test_identity_apply()
    test_migration()
    test_pipeline()
    print("\n全绿" if not FAILED else f"\n**{FAILED} 项失败**")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
