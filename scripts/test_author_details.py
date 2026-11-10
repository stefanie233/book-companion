"""作者细节采集的零 token 单测。

盯五件事：
1. 攻略：序跋必进灰名单；身份未定不生成具体作品先验。
2. 依据闸门：译者序里的「作者自述」降级 paratext；投射无锚降级。
3. 用途闸门：投射不能进 biography / roleplay_texture。
4. 钩子：引文必须命中；钩子不升格成事实。
5. DeepSeek Markdown 洗净后，逐字引文仍能对上。

跑法：``python scripts/test_author_details.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from authorgraph.details import (normalize_details, speaker_hint,  # noqa: E402
                                 store_details)
from authorgraph.plan import (deterministic_plan, normalize_plan)  # noqa: E402
from authorgraph.scope import (body_sample_chunks, candidate_chunks,  # noqa: E402
                               chunks_by_title)
from authorgraph.sources.hook_close import answer_hook              # noqa: E402
from authorgraph.sources.verify import quote_located               # noqa: E402
from authorgraph.sources.web_deepseek import markdown_to_text       # noqa: E402
from storage.authors_db import AuthorsDB                           # noqa: E402
from storage.db import BookDB                                      # noqa: E402
from storage.models import (BookChapterRow, BookChunkRow,           # noqa: E402
                            BookRow)

FAILED = 0
PREFACE = ("我写这本书时，每天清晨四点起床，泡一壶浓茶。"
           "这本书写的是我自己的经历，不是虚构。"
           "窗外那棵槐树陪了我很多年，叶子落了又长。"
           "我习惯把稿纸摊在窗台上，写到太阳升起来才停。"
           "这段文字用于测试候选长度，重复几次，不承载额外信息。"
           "这段文字用于测试候选长度，重复几次，不承载额外信息。"
           "这段文字用于测试候选长度，重复几次，不承载额外信息。")
BODY = ("第一章的正文。" * 40)
TRANSLATOR = ("我翻译这本书时，常想起作者晚年住在海边的日子。"
              "他总在信里说，海风会把句子吹短。"
              "译本保留了一些原来的停顿，没有全改成中文的节奏。")
APPENDIX = ("美国版自序：我写这本书的时候，住在阿尔及尔。"
            "每天下午去海边，回来再改稿。")


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def build(tmp: Path) -> tuple[AuthorsDB, BookDB, str]:
    adb = AuthorsDB(tmp / "authors.db")
    bdb = BookDB(tmp / "books.db")
    bdb.initialize()
    bdb.insert_book(BookRow(id="bk-1", title="测试之书", author="测试作者"))
    bdb.insert_chapters([
        BookChapterRow(id="ch-0", book_id="bk-1", chapter_index=0,
                       title="自序", chapter_type="preface", content=PREFACE),
        BookChapterRow(id="ch-1", book_id="bk-1", chapter_index=1,
                       title="第一章", chapter_type="body", content=BODY),
        BookChapterRow(id="ch-2", book_id="bk-1", chapter_index=2,
                       title="美国版自序", chapter_type="body",
                       content=APPENDIX),
    ])
    bdb.insert_chunks([
        BookChunkRow(id="ck-p1", book_id="bk-1", chapter_index=0,
                     chunk_index=0, content=PREFACE),
        BookChunkRow(id="ck-t1", book_id="bk-1", chapter_index=0,
                     chunk_index=1, content=TRANSLATOR),
        BookChunkRow(id="ck-b1", book_id="bk-1", chapter_index=1,
                     chunk_index=0, content=BODY),
        BookChunkRow(id="ck-a1", book_id="bk-1", chapter_index=2,
                     chunk_index=0, content=APPENDIX),
    ])
    aid = adb.upsert_author("测试作者")
    adb.set_identity_fields(aid, identity_status="resolved")
    adb.link_book(aid, "sb-1", book_id="bk-1", role="author",
                  position="avatar")
    return adb, bdb, aid


def test_plan(adb: AuthorsDB, bdb: BookDB, aid: str) -> None:
    plans = deterministic_plan(bdb, adb, aid)
    targets = {p["target"] for p in plans}
    check("规则攻略：序跋进攻略", "sb-1#paratext" in targets, True)
    check("规则攻略：自传体正文进攻略", "sb-1#body" in targets, True)
    row = adb.get_conn().execute(
        "SELECT expected_basis, list_type FROM author_exploration_plan"
        " WHERE author_id=? AND target='sb-1#body'", (aid,)).fetchone()
    check("规则攻略：投射先验", row["expected_basis"],
          "autobiographical_projection")
    check("模型/规则先验一律 grey", row["list_type"], "grey")
    adb.set_identity_fields(aid, identity_status="provisional")
    before = len(adb.plans_for(aid, open_only=False))
    deterministic_plan(bdb, adb, aid)
    after = len(adb.plans_for(aid, open_only=False))
    check("身份未定不新增具体作品先验", after, before)
    adb.set_identity_fields(aid, identity_status="resolved")


def test_plan_blacklist(adb: AuthorsDB, bdb: BookDB, aid: str) -> None:
    """黑名单是目标级：模型换措辞重开也不能绕过。"""
    p = next(p for p in adb.plans_for(aid, open_only=False)
             if p["target"] == "sb-1#paratext")
    adb.set_plan_list(p["plan_id"], "black")
    deterministic_plan(bdb, adb, aid)
    left = adb.get_conn().execute(
        "SELECT COUNT(*) FROM author_exploration_plan"
        " WHERE author_id=? AND target='sb-1#paratext'"
        " AND list_type<>'black'", (aid,)).fetchone()[0]
    check("黑名单：按 target 拦住重开的先验", left, 0)
    adb.set_plan_list(p["plan_id"], "grey")


def test_scope(bdb: BookDB) -> None:
    par, _ = candidate_chunks(bdb, "bk-1")
    check("候选：只取序跋", [c["chunk_id"] for c in par],
          ["ck-p1", "ck-t1"])
    body, _ = body_sample_chunks(bdb, "bk-1")
    check("抽样：取正文（跳过误分类的自序）",
          [c["chunk_id"] for c in body], ["ck-b1"])
    named, warns = chunks_by_title(bdb, "bk-1", "美国版自序")
    check("按标题取块：命中", [c["chunk_id"] for c in named], ["ck-a1"])
    check("按标题取块：无警告", warns, [])
    miss, warns2 = chunks_by_title(bdb, "bk-1", "不存在的章")
    check("按标题取块：无匹配", miss, [])
    check("按标题取块：报原因", bool(warns2), True)


def test_plan_chapter_target() -> None:
    raw = {"plans": [{
        "target": "sb-1#chapter:美国版自序",
        "hypothesis": "美国版自序可能含写作缘起",
        "expected_basis": "author_statement",
        "expected_value": "high", "expected_cost": 1,
    }]}
    got, defects = normalize_plan(raw, known={"sb-1": "bk-1"},
                                  identity="resolved")
    check("攻略：可按标题点名", got[0]["scope"], "chapter:美国版自序")
    check("攻略：点名无缺陷", defects, [])
    raw2 = {"plans": [{
        "target": "sb-1#chapter:", "hypothesis": "空关键词",
        "expected_basis": "author_statement", "expected_cost": 1,
    }]}
    got2, defects2 = normalize_plan(raw2, known={"sb-1": "bk-1"},
                                    identity="resolved")
    check("攻略：空标题关键词被拒", got2, [])
    check("攻略：报空关键词", any("缺标题关键词" in d for d in defects2), True)


def test_speaker_hint() -> None:
    check("标题提示：译序", speaker_hint("译者序"), "translator")
    check("标题提示：自序", speaker_hint("自序"), "author")
    check("标题提示：编者", speaker_hint("编者的话"), "editor")
    check("标题提示：年表", speaker_hint("加缪年表"), "editor")
    check("标题提示：未知", speaker_hint("第一章"), "unknown")


def test_observations() -> None:
    text = {"ck-p1": PREFACE, "ck-t1": TRANSLATOR}
    meta = {
        "ck-p1": {"chapter_index": 0, "chunk_index": 0,
                  "speaker_hint": "author"},
        "ck-t1": {"chapter_index": 0, "chunk_index": 1,
                  "speaker_hint": "translator"},
    }
    raw = {"observations": [
        {"topic": "habit", "statement": "清晨四点起床写作",
         "quote": "每天清晨四点起床，泡一壶浓茶。", "chunk_id": "ck-p1",
         "basis": "author_statement", "speaker": "author",
         "use": "roleplay_texture"},
        {"topic": "dwelling", "statement": "作者晚年住在海边",
         "quote": "作者晚年住在海边的日子", "chunk_id": "ck-t1",
         "basis": "author_statement", "speaker": "author",
         "use": "biography"},
        {"topic": "motif", "statement": "文本自称是经历",
         "quote": "这本书写的是我自己的经历", "chunk_id": "ck-p1",
         "basis": "autobiographical_projection", "speaker": "author",
         "use": "biography"},
        {"topic": "habit", "statement": "编出来的",
         "quote": "这句话不在文本里", "chunk_id": "ck-p1",
         "basis": "author_statement", "speaker": "author",
         "use": "dossier"},
    ], "hooks": [
        {"topic": "relation", "quote": "作者晚年住在海边的日子",
         "chunk_id": "ck-t1", "known": "晚年住海边",
         "missing": "具体是哪一年、和谁住", "why": "可能连到晚年经历",
         "suggested_action": "web_search"},
    ]}
    out, defects = normalize_details(raw, text, meta)
    obs = out["observations"]
    check("观察：保留三条", len(obs), 3)
    check("作者自述：高置信", obs[0]["confidence"], "high")
    check("译者序里的自述降级 paratext", obs[1]["basis"], "paratext")
    check("译者序 speak 归位", obs[1]["speaker"], "translator")
    check("无锚投射降级 fiction", obs[2]["basis"], "fiction_projection")
    check("投射不能当经历", obs[2]["use"], "dossier")
    check("伪造引文被丢弃",
          any("fabricated_quote" in d for d in defects), True)
    check("钩子一条", len(out["hooks"]), 1)
    check("钩子保留原话", out["hooks"][0]["quote"],
          "作者晚年住在海边的日子")


def test_read_as_anchor() -> None:
    text = {"ck-p1": PREFACE}
    meta = {"ck-p1": {"chapter_index": 0, "chunk_index": 0,
                      "speaker_hint": "author"}}
    raw = {"observations": [{
        "topic": "motif", "statement": "文本自称是经历",
        "quote": "这本书写的是我自己的经历", "chunk_id": "ck-p1",
        "basis": "autobiographical_projection", "speaker": "author",
        "use": "dossier"}]}
    out, _ = normalize_details(raw, text, meta, read_as="autobiographical")
    check("用户标记后投射保留", out["observations"][0]["basis"],
          "autobiographical_projection")
    check("有锚投射为中置信", out["observations"][0]["confidence"], "medium")


def test_store(adb: AuthorsDB, aid: str) -> None:
    pid = adb.upsert_plan(aid, target="sb-1#paratext",
                          hypothesis="序跋可能含细节",
                          stable_book_id="sb-1", book_id="bk-1")
    result = {
        "observations": [{
            "chunk_id": "ck-p1", "chapter_index": 0, "chunk_index": 0,
            "topic": "habit", "statement": "清晨四点起床写作",
            "quote": "每天清晨四点起床", "basis": "author_statement",
            "basis_quote": "", "speaker": "author", "use": "biography",
            "confidence": "high", "content_hash": "h1"}],
        "hooks": [{
            "chunk_id": "ck-p1", "topic": "other",
            "quote": "泡一壶浓茶", "known": "喝浓茶",
            "missing": "为什么只喝浓茶", "why": "写作习惯",
            "suggested_action": "web_search", "priority": "medium"}],
        "defects": [], "notes": [],
    }
    ident = {"book_id": "bk-1", "stable_book_id": "sb-1",
             "title": "测试之书", "author": "测试作者"}
    out = store_details(adb, ident, aid, result, plan_id=pid, model="fake")
    check("落观察", out["observations_added"], 1)
    check("落钩子", out["hooks_added"], 1)
    check("观察可读", len(adb.observations_for(aid)), 1)
    check("钩子可读", len(adb.hooks_for(aid)), 1)
    ev = adb.evidence_for("observation",
                          adb.observations_for(aid)[0]["observation_id"])
    check("观察带逐字证据", len(ev) >= 1, True)
    plan = adb.get_conn().execute(
        "SELECT status FROM author_exploration_plan WHERE plan_id=?",
        (pid,)).fetchone()
    check("攻略标记完成", plan["status"], "done")


def test_hook_close(adb: AuthorsDB, aid: str) -> None:
    hid = adb.add_hook(aid, stable_book_id="sb-1", quote="晚年住海边",
                       missing="具体是哪一年、和谁住", known="晚年住海边",
                       why="可能连到晚年经历")
    page_text = "一九七五年，他搬到北京，与母亲同住。"

    async def fake_search(session, cfg, query, **kw):
        return {"hits": [{"url": "https://example.org/a"}]}

    async def fake_fetch(session, url):
        return {"url": url, "canonical_url": url, "title": "资料",
                "publisher": "example.org", "language": "zh",
                "main_text": page_text, "content_hash": "h1",
                "content_excerpt": page_text, "published_at": "",
                "status": "ok", "http_status": 200}

    async def fake_ask(session, cfg, author, hook, page, **kw):
        return {"answered": True, "statement": "一九七五年搬到北京与母亲同住",
                "evidence_quote": "一九七五年，他搬到北京，与母亲同住。",
                "kind": "life_event", "year": 1975, "note": ""}

    author = {"author_id": aid, "canonical_name": "测试作者"}
    picked = next(h for h in adb.hooks_for(aid) if h["hook_id"] == hid)
    out = asyncio.run(answer_hook(None, None, adb, author, picked,
                                  search_fn=fake_search, fetch_fn=fake_fetch,
                                  ask_fn=fake_ask))
    check("钩子：命中引文后关闭", out.get("closed"), True)
    check("钩子：状态 closed",
          adb.hooks_for(aid, status="closed")[0]["hook_id"], hid)
    check("钩子：写入一条记录", len(adb.records_for(aid)), 1)
    ev = adb.evidence_for("hook", hid)
    check("钩子：带逐字证据", ev and ev[0]["quote"], page_text)

    # 引文对不上 → 不关闭
    hid2 = adb.add_hook(aid, stable_book_id="sb-1", quote="另一句",
                        missing="另一件没说清的事")

    async def fake_ask_bad(session, cfg, author, hook, page, **kw):
        return {"answered": True, "statement": "编的",
                "evidence_quote": "这一页没有这句话", "kind": "life_event"}

    picked2 = next(h for h in adb.hooks_for(aid) if h["hook_id"] == hid2)
    out2 = asyncio.run(answer_hook(None, None, adb, author, picked2,
                                   search_fn=fake_search, fetch_fn=fake_fetch,
                                   ask_fn=fake_ask_bad))
    check("钩子：引文对不上不关闭", out2.get("closed"), False)
    still = [h["hook_id"] for h in adb.hooks_for(aid)]
    check("钩子：仍是 open", hid2 in still, True)


def test_query_clean() -> None:
    from authorgraph.sources.hook_close import _clean_queries

    got = _clean_queries(["柏大夫", "史铁生 柏大夫", "长" * 61, ""], "史铁生")
    check("检索词：补作者名并去重", got, ["史铁生 柏大夫"])


def test_multiquery(adb: AuthorsDB, aid: str) -> None:
    """两组检索词命中同一页：都去搜，但只抓一次、只问一次。"""
    hid = adb.add_hook(aid, stable_book_id="sb-1", quote="住在海边",
                       missing="哪一年搬去", known="住在海边")
    page_text = "一九八二年，他搬去海边。"
    calls = {"search": 0, "fetch": 0, "ask": 0}

    async def fake_search(session, cfg, query, **kw):
        calls["search"] += 1
        return {"hits": [{"url": "https://example.org/b"}]}

    async def fake_fetch(session, url):
        calls["fetch"] += 1
        return {"url": url, "canonical_url": url, "title": "资料",
                "publisher": "example.org", "language": "zh",
                "main_text": page_text, "content_hash": "h2",
                "content_excerpt": page_text, "published_at": "",
                "status": "ok", "http_status": 200}

    async def fake_ask(session, cfg, author, hook, page, **kw):
        calls["ask"] += 1
        return {"answered": True, "statement": "一九八二年搬去海边",
                "evidence_quote": "一九八二年，他搬去海边。",
                "kind": "life_event", "year": 1982}

    author = {"author_id": aid, "canonical_name": "测试作者"}
    picked = next(h for h in adb.hooks_for(aid) if h["hook_id"] == hid)
    out = asyncio.run(answer_hook(
        None, None, adb, author, picked, queries=["海边 哪一年", "1982 海边"],
        search_fn=fake_search, fetch_fn=fake_fetch, ask_fn=fake_ask))
    check("多检索词：命中后关闭", out.get("closed"), True)
    check("多检索词：都去搜", calls["search"] >= 2, True)
    check("多检索词：同页只抓一次", calls["fetch"], 1)
    check("多检索词：同页只问一次", calls["ask"], 1)


def test_markdown() -> None:
    md = ("# 标题\n\n**加粗**的[链接](https://example.com)和`代码`。\n\n"
          "> 引用：他于一九五一年出生。")
    text = markdown_to_text(md)
    check("Markdown 去标题", text.startswith("标题"), True)
    check("Markdown 去强调", "加粗的链接和代码" in text, True)
    check("洗净后引文可逐字命中",
          quote_located("他于一九五一年出生。", text), True)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        adb, bdb, aid = build(tmp)
        test_plan(adb, bdb, aid)
        test_plan_blacklist(adb, bdb, aid)
        test_scope(bdb)
        test_plan_chapter_target()
        test_speaker_hint()
        test_observations()
        test_read_as_anchor()
        test_store(adb, aid)
        test_hook_close(adb, aid)
        test_query_clean()
        test_multiquery(adb, aid)
        test_markdown()
        adb.close()
        bdb.close()
    print(f"\n{'=' * 40}\n失败 {FAILED} 项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

