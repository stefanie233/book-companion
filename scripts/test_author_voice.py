"""声音管线（M2）的零 token 单测 + fake-provider 端到端。

盯的七件事：

1. **可见性** —— editor/recorder/absent/unknown 的正文一律不采。
2. **确定性抽样** —— 同一本书跑两次抽到同一批窗口；窗口是逐字子串。
3. **非正文排除** —— 序跋/版权章不进声音语料。
4. **词表与证据** —— 未知维度丢弃；未知取值收成 variable；编造的
   corpus_id 剔除；单证据 strength ≤0.4。
5. **跨批冲突** —— 同维度不同值 → 一条 variable + 两条子证据，不平均。
6. **落库边界** —— 声音 run **不写 author_records**；trait 幂等、旧 revision 退役。
7. **端到端** —— 真提示词 + fake provider + 真落库；重跑命中缓存；
   ``--force`` 只清本书声音语料。

跑法：``python scripts/test_author_voice.py``（退出码 0 = 全绿）。
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

from authorgraph.voice import pipeline as P                      # noqa: E402
from authorgraph.voice import extract as X                       # noqa: E402
from authorgraph.voice.extract import split_batches              # noqa: E402
from authorgraph.voice.from_extraction import voice_candidates   # noqa: E402
from authorgraph.voice.normalize import normalize_batch          # noqa: E402
from authorgraph.voice.sample import (sample_windows, scene_hint,  # noqa: E402
                                      window_for)
from authorgraph.voice.scope import (ChunkView, eligible_chunks,  # noqa: E402
                                     looks_like_toc, visibility_for)
from authorgraph.voice.select import (eligible_traits,             # noqa: E402
                                      select_for_scene)
from authorgraph.voice.traits import merge_batches               # noqa: E402
from storage.authors_db import AuthorsDB                         # noqa: E402
from storage.db import BookDB                                    # noqa: E402
from storage.models import BookChapterRow, BookChunkRow, BookRow  # noqa: E402

ROOT = Path(__file__).parent.parent
FAILED = 0
logging.getLogger("authorgraph").setLevel(logging.ERROR)

TITLE = "测试声音书"
NAME = "声音作者"
STABLE = "sb-voice-1"


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


def ok(label: str, cond) -> None:
    check(label, bool(cond), True)


# ── 1. 可见性 ────────────────────────────────────────────────────────────

def test_visibility() -> None:
    check("可见：narrator 正文可用", visibility_for("narrator").body, True)
    check("可见：narrator 域=nonfiction",
          visibility_for("narrator").domain, "nonfiction")
    check("可见：avatar 口吻可用", visibility_for("avatar").body, True)
    check("可见：avatar speaker=via_character",
          visibility_for("avatar").speaker, "via_character")
    for pos in ("editor", "recorder", "absent", "unknown", ""):
        check(f"可见：{pos or '(空)'} 不采正文",
              visibility_for(pos).body, False)


# ── 2. 确定性抽样与窗口 ──────────────────────────────────────────────────

def _chunk(cid: str, text: str, *, ci: int = 1, ki: int = 0,
           offsets: tuple[tuple[int, int], ...] = ()) -> ChunkView:
    return ChunkView(chunk_id=cid, chapter_index=ci, chunk_index=ki,
                     chapter_title=f"第{ci}章", chapter_type="body",
                     text=text, band="middle", paragraph_offsets=offsets)


def test_sampling() -> None:
    long_text = "他走进屋子，看见桌上的灯还亮着。" * 60       # 960 字
    win = window_for(_chunk("c-long", long_text), "sb")
    ok("窗口：≤500 字", len(win["text"]) <= 500)
    check("窗口：逐字子串", win["text"], long_text[win["start"]:win["end"]])
    ok("窗口：≥200 字", len(win["text"]) >= 200)

    texts = {
        "c-dlg": "“你来了。”他说。\n“嗯。”她没抬头，手指在杯沿上转了一圈。\n"
                 "“外面下雨了。”\n“我知道。”\n" + "雨声打在窗上，密得像一层布。" * 6,
        "c-mem": "那年冬天我还在北方。记得屋里有炉子，煤球烧得通红。"
                 "后来我常想起那间屋子，想起窗外的雪。" * 3,
        "c-quiet": "院子里有一棵树。树下面放着一把旧椅子。风来的时候，"
                   "叶子落在椅子上。没有人去扫。" * 4,
        "c-act": "他推开门冲出去，雨打在脸上。他跑过两条街，抓住那人的手臂，"
                 "转身把他按在墙上。对方挣了一下，没挣开。" * 3,
        "c-land": "山从远处压过来，天很低。河面泛着灰白的光，"
                  "对岸的树一排排站着，看不见人。" * 4,
        "c-intro": "我想起她说过的话，心里忽然一沉。仿佛有什么东西"
                   "在暗处动了一下，我意识到自己一直没有明白。" * 4,
    }
    chunks = [_chunk(cid, t, ci=1 + i // 2, ki=i % 2)
              for i, (cid, t) in enumerate(texts.items())]
    s1, w1 = sample_windows(chunks, stable_book_id="sb", max_samples=6)
    s2, _ = sample_windows(chunks, stable_book_id="sb", max_samples=6)
    check("抽样：两次结果相同", [s["key"] for s in s1], [s["key"] for s in s2])
    check("抽样：抽满 6 段", len(s1), 6)
    ok("抽样：没有重复块", len({s["chunk_id"] for s in s1}) == len(s1))
    ok("抽样：每段逐字来自原块",
       all(s["text"] == next(c.text for c in chunks
                             if c.chunk_id == s["chunk_id"])[s["start"]:s["end"]]
           for s in s1))
    check("场景：对话密集 → dialogue", scene_hint(texts["c-dlg"]), "dialogue")
    check("场景：回忆标记 → memory", scene_hint(texts["c-mem"]), "memory")
    ok("场景：样本覆盖 ≥4 类",
       len({s["scene_hint"] for s in s1}) >= 4)
    ok("预算：总字数 ≤8000", sum(len(s["text"]) for s in s1) <= 8000)
    # 候选只当建议：不在候选集里的块不会被塞进来
    cand = [{"chunk_id": "c-dlg", "scene_hint": "dialogue", "why": "x"}]
    sc, _ = sample_windows(chunks, stable_book_id="sb", candidates=cand,
                           max_samples=3)
    ok("候选：真实块被采用", any(s["chunk_id"] == "c-dlg" for s in sc))
    check("批：最多 2 批", len(split_batches([{}] * 20)), 2)

    # Scout 候选的宽容收口：缺失/不在书里/被排除/词表外
    from bookgraph.scout import normalize_voice_candidates
    plan = {"voice_candidates": [
        {"chunk_id": "c-dlg", "scene_hint": "Dialogue", "why": "x"},
        {"chunk_id": "不存在", "scene_hint": "quiet"},
        {"chunk_id": "c-act", "scene_hint": "nonsense"},
    ], "excluded_chunk_ids": ["c-act"]}
    parsed, warns = normalize_voice_candidates(
        plan, chunk_ids={"c-dlg", "c-act", "c-mem"}, excluded={"c-act"})
    check("Scout：只留可用候选", len(parsed["voice_candidates"]), 1)
    check("Scout：场景小写归一",
          parsed["voice_candidates"][0]["scene_hint"], "dialogue")
    ok("Scout：不在书里/被排除都报", len(warns) == 2)
    empty, _ = normalize_voice_candidates({"batch_plan": []},
                                          chunk_ids={"x"})
    check("Scout：老计划缺字段 → 空", empty["voice_candidates"], [])
    ok("目录：目录页识别",
       looks_like_toc("目录\n" + "\n".join(f"第{i}章 标题" for i in range(1, 8))))
    ok("目录：诗不误杀", not looks_like_toc("雨落在窗上\n灯还亮着\n" * 6))


# ── 3. 归一化与证据 ──────────────────────────────────────────────────────

def test_select_dedupe() -> None:
    class _Adb:
        def voice_traits(self, *a, **kw):
            return [{"dimension": "sentence_rhythm", "confidence": 0.9},
                    {"dimension": "sentence_rhythm", "confidence": 0.8},
                    {"dimension": "closure_pattern", "confidence": 0.7}]
    check("选择器：每维度只留一条", len(eligible_traits(_Adb(), "a")), 2)
    check("选择器：可要全量",
          len(eligible_traits(_Adb(), "a", per_dimension=False)), 3)


def test_normalize() -> None:
    samples = {
        1: {"corpus_id": 1, "chunk_id": "a", "chapter_index": 0},
        2: {"corpus_id": 2, "chunk_id": "b", "chapter_index": 1},
        3: {"corpus_id": 3, "chunk_id": "c", "chapter_index": 1},
    }
    parsed = {
        "samples": [{"corpus_id": 2, "scene_type": "瞎写",
                     "narrative_layer": "dialogue", "pov": "第一人称"}],
        "traits": [
            {"dimension": "sentence_rhythm", "value": "短句",
             "voice_domain": "fiction", "statement": "以短句为主。",
             "strength": 0.8, "evidence_corpus_ids": [1, 2],
             "confidence": 0.8},
            {"dimension": "narrative_distance", "value": "非常近",
             "voice_domain": "fiction", "statement": "贴得很近。",
             "strength": 0.9, "evidence_corpus_ids": [1],
             "confidence": 0.8},
            {"dimension": "made_up", "value": "x", "voice_domain": "fiction",
             "statement": "未知维度。", "evidence_corpus_ids": [1]},
            {"dimension": "closure_pattern", "value": "open",
             "voice_domain": "fiction", "statement": "编了证据。",
             "evidence_corpus_ids": [999]},
            {"dimension": "lexical_texture", "value": "plain",
             "voice_domain": "fiction", "statement": "词汇朴素。",
             "strength": 0.8, "evidence_corpus_ids": [2, 3],
             "confidence": 0.8},
        ],
        "source_bound": [{"name": "测试地标", "type": "name",
                          "statement": "只在这本出现。",
                          "evidence_corpus_ids": [1, 999]}],
        "defects": ["样本不够"],
    }
    out = normalize_batch(parsed, samples_by_id=samples,
                          default_domain="fiction")
    check("归一：未知维度丢弃", len(out["traits"]), 3)
    check("归一：短句 → short", out["traits"][0]["value"], "short")
    check("归一：未知取值 → variable", out["traits"][1]["value"], "variable")
    check("归一：单证据 → provisional", out["traits"][1]["status"], "provisional")
    ok("归一：单证据 strength≤0.4", out["traits"][1]["strength"] <= 0.4)
    check("归一：证据同章 → provisional", out["traits"][2]["status"],
          "provisional")
    ok("归一：同章有记账",
       any("同一章" in d for d in out["defects"]))
    ok("归一：编造 corpus_id 被剔除",
       any("999" in d for d in out["defects"]))
    ok("归一：编造证据的 trait 被丢弃",
       all(t["statement"] != "编了证据。" for t in out["traits"]))
    ok("归一：source_bound 证据只留本批",
       out["source_bound"][0]["evidence_ids"] == [1])
    check("归一：场景词表外 → other", out["samples"][0]["scene_type"], "other")
    check("归一：pov 中文归一", out["samples"][0]["pov"], "first")
    ok("归一：模型自报进缺陷", any("样本不够" in d for d in out["defects"]))
    # 缺 voice_domain → 推断并报
    out2 = normalize_batch(
        {"traits": [{"dimension": "lexical_texture", "value": "plain",
                     "statement": "词汇朴素。", "evidence_corpus_ids": [1, 2]}]},
        samples_by_id=samples, default_domain="nonfiction")
    ok("归一：缺域推断", out2["traits"][0]["voice_domain"] == "nonfiction")
    ok("归一：推断有记账",
       any("voice_domain" in d for d in out2["defects"]))


# ── 4. 跨批合并 ──────────────────────────────────────────────────────────

def test_merge() -> None:
    samples = {1: {"corpus_id": 1, "chunk_id": "a", "chapter_index": 0,
                   "text": "甲"},
               2: {"corpus_id": 2, "chunk_id": "b", "chapter_index": 1,
                   "text": "乙"},
               3: {"corpus_id": 3, "chunk_id": "c", "chapter_index": 2,
                   "text": "丙"}}
    b1 = {"traits": [{"dimension": "sentence_rhythm", "value": "short",
                      "voice_domain": "fiction", "statement": "短句为主。",
                      "strength": 0.8, "transferable": True,
                      "evidence_ids": [1, 2], "status": "active",
                      "model_confidence": 0.8}],
          "source_bound": [], "defects": []}
    b2 = {"traits": [{"dimension": "sentence_rhythm", "value": "long",
                      "voice_domain": "fiction", "statement": "长句为主。",
                      "strength": 0.8, "transferable": True,
                      "evidence_ids": [3], "status": "provisional",
                      "model_confidence": 0.8}],
          "source_bound": [], "defects": []}
    out = merge_batches([b1, b2], samples_by_id=samples,
                        scope_type="work", scope_ref="sb",
                        prompt_version="v1", model="m")
    ok("合并：冲突生成 variable",
       any(t["value"] == "variable" for t in out["traits"]))
    ok("合并：两条子判断都保留",
       {t["value"] for t in out["traits"]} >= {"short", "long"})
    ok("合并：冲突被报告", any("不一致" in c for c in out["conflicts"]))
    check("合并：revision 稳定",
          out["revision"],
          merge_batches([b1, b2], samples_by_id=samples, scope_type="work",
                        scope_ref="sb", prompt_version="v1",
                        model="m")["revision"])


# ── 5. 端到端（fake provider）────────────────────────────────────────────

BODY = {
    "c-dlg": "“你来了。”他说。\n“嗯。”她没抬头，手指在杯沿上转了一圈。\n"
             "“外面下雨了。”\n“我知道。”\n雨声打在窗上，密得像一层布。" * 4,
    "c-mem": "那年冬天我还在北方。记得屋里有炉子，煤球烧得通红。"
             "后来我常想起那间屋子，想起窗外的雪。" * 3,
    "c-quiet": "院子里有一棵树。树下面放着一把旧椅子。风来的时候，"
               "叶子落在椅子上。没有人去扫。" * 4,
    "c-act": "他推开门冲出去，雨打在脸上。他跑过两条街，抓住那人的手臂，"
             "转身把他按在墙上。对方挣了一下，没挣开。" * 3,
    "c-land": "山从远处压过来，天很低。河面泛着灰白的光，"
              "对岸的树一排排站着，看不见人。" * 4,
    "c-intro": "我想起她说过的话，心里忽然一沉。仿佛有什么东西在暗处"
               "动了一下，我意识到自己一直没有明白。" * 4,
}

def _reply_for(ids: list[int]) -> dict:
    """模型回复。``ids`` 是本轮语料主键 —— force 重跑会换一批 id。"""
    return {
        "samples": [
            {"corpus_id": i, "scene_type": "introspection",
             "narrative_layer": "narration", "pov": "first", "period": ""}
            for i in ids
        ],
        "traits": [
            {"dimension": "sentence_rhythm", "voice_domain": "fiction",
             "value": "short", "statement": "以短句为主，情绪由动作承接。",
             "strength": 0.8, "transferable": True,
             "evidence_corpus_ids": [ids[0], ids[2]], "confidence": 0.8},
            {"dimension": "made_up", "voice_domain": "fiction", "value": "x",
             "statement": "未知维度。", "evidence_corpus_ids": ids[:1],
             "confidence": 0.5},
            {"dimension": "closure_pattern", "voice_domain": "fiction",
             "value": "open", "statement": "留白收束。", "strength": 0.6,
             "evidence_corpus_ids": [9999], "confidence": 0.6},
        ],
        "source_bound": [{"name": "测试地标", "type": "name",
                          "statement": "只在这本书里出现。",
                          "evidence_corpus_ids": ids[:1]}],
        "defects": [],
    }


REPLY = _reply_for([1, 2, 3, 4, 5, 6])
REPLY_FORCE = _reply_for([7, 8, 9, 10, 11, 12])
REPLY_FORCE2 = _reply_for([13, 14, 15, 16, 17, 18])


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


def _make_books(path: Path) -> None:
    db = BookDB(path)
    db.initialize()
    db.insert_book(BookRow(id="bk-1", title=TITLE, author=NAME,
                           chapter_count=4, total_chars=4000))
    db.insert_chapters([
        BookChapterRow(id="ch-0", book_id="bk-1", chapter_index=0,
                       title="序", chapter_type="preface", content="序" * 200),
        BookChapterRow(id="ch-1", book_id="bk-1", chapter_index=1,
                       title="第一章", chapter_type="body"),
        BookChapterRow(id="ch-2", book_id="bk-1", chapter_index=2,
                       title="第二章", chapter_type="body"),
        BookChapterRow(id="ch-3", book_id="bk-1", chapter_index=3,
                       title="第三章", chapter_type="body"),
    ])
    chunks = []
    for i, (cid, text) in enumerate(BODY.items()):
        chunks.append(BookChunkRow(
            id=cid, book_id="bk-1", chapter_index=1 + i // 2,
            chunk_index=i % 2, content=text, char_count=len(text)))
    # 正文混注释的块：注号 ≥3，抽样阶段就该被拦住（实测《房思琪》）。
    note = ("这一段是正文。" * 20
            + " (1) 注一。 (2) 注二。 (3) 注三。")
    chunks.append(BookChunkRow(
        id="c-note", book_id="bk-1", chapter_index=3, chunk_index=9,
        content=note, char_count=len(note)))
    # 正文块里夹着的目录页（实测《当下的力量》第一章第一块）。
    toc = "目录\n" + "\n".join(f"第{i}章 某个标题{i}" for i in range(1, 11))
    chunks.append(BookChunkRow(
        id="c-toc", book_id="bk-1", chapter_index=3, chunk_index=8,
        content=toc, char_count=len(toc)))
    db.insert_chunks(chunks)
    db.close()


def _rules() -> dict:
    return {"rules": [{"contains": TITLE,
                       "response": {"text": json.dumps(REPLY,
                                                       ensure_ascii=False)}}]}


async def _drive(base: str, books_db: BookDB, adb: AuthorsDB,
                 *, force: bool = False, dry_run: bool = False) -> dict:
    from config import Config
    cfg = Config()
    cfg.api_base = base
    cfg.api_key = "fake"
    cfg.model = "fake-model"
    # 模型绑定会把假端点顶掉（与 enrich e2e 同一条）。
    P.config_for = lambda config, scenario: config       # type: ignore
    X.config_for = lambda config, scenario: config       # type: ignore
    async with aiohttp.ClientSession() as session:
        return await P.harvest_voice(
            session, cfg, books_db, adb, author_id="au-voice",
            stable_book_id=STABLE, book_id="bk-1", title=TITLE,
            force=force, max_samples=6, dry_run=dry_run)


def _assert_e2e(out: dict, tmp: str) -> int:
    check("e2e：状态 ready/partial", out.get("status") in ("ready", "partial"),
          True)
    check("e2e：样本 6 段", out.get("samples"), 6)
    check("e2e：1 次调用", out.get("calls"), 1)
    check("e2e：trait 只落 1 条（其余被证据/维度规则拦下）",
          out.get("traits"), 1)
    check("e2e：active 1 条", out.get("active_traits"), 1)
    check("e2e：source_bound 进 manifest 不进表",
          len(out.get("source_bound") or []), 1)

    adb = AuthorsDB(Path(tmp) / "authors.db")
    # 纯净度：声音 run 一条 author_records 都不许写。
    check("e2e：不写 author_records", len(adb.records_for("au-voice")), 0)
    corpus = adb.voice_corpus_of_book("au-voice", STABLE)
    check("e2e：语料 6 条", len(corpus), 6)
    ok("e2e：语料都有来源/场景/层/字数",
       all(r["source_id"] and r["scene_type"] and r["narrative_layer"]
           and r["word_count"] > 0 for r in corpus))
    ok("e2e：语料没有 paratext", all(r["narrative_layer"] != "paratext"
                                     for r in corpus))
    check("e2e：窗口层都是 narration",
          {r["narrative_layer"] for r in corpus}, {"narration"})
    traits = adb.voice_traits("au-voice")
    check("e2e：库里 1 条 active trait", len(traits), 1)
    t = traits[0]
    check("e2e：域=fiction", t["voice_domain"], "fiction")
    check("e2e：范围=work", t["scope_type"], "work")
    check("e2e：范围指向本书", t["scope_ref"], STABLE)
    ev = adb.trait_evidence(t["trait_id"])
    check("e2e：证据 2 段", len(ev), 2)
    ok("e2e：证据逐字（excerpt 非空）", all(e["excerpt"] for e in ev))
    st = adb.voice_status_of("au-voice", STABLE)
    check("e2e：书级状态同步", st["voice_status"], out.get("status"))
    check("e2e：内容 hash 落库", st["voice_content_hash"], out.get("content_hash"))
    run = adb.last_voice_run("au-voice", STABLE)
    ok("e2e：run 可追溯", run and run["status"] in ("ready", "partial"))
    check("e2e：run 记录了样本", len(json.loads(run["sample_ids_json"])), 6)

    picked = select_for_scene(adb, "au-voice", {"pov": "first"})
    check("e2e：选择器能取到全部窗口", len(picked["samples"]), 6)
    ok("e2e：选择器只给 narration/essay",
       all(s["narrative_layer"] in ("narration", "essay")
           for s in picked["samples"]))
    check("e2e：选择器只给可迁移 trait", len(picked["traits"]), 1)

    # 重跑：同内容 hash → 命中缓存，不再调用
    out2 = asyncio.run(_drive(_BASE[0], BookDB(Path(tmp) / "books.db"), adb))
    check("e2e：重跑命中缓存", out2.get("cached"), True)
    check("e2e：重跑不新增语料",
          len(adb.voice_corpus_of_book("au-voice", STABLE)), 6)

    # 提示词/规则版本变了 → 即使内容 hash 相同也要重采（幂等键含版本），
    # 且旧样本按旧规则选的，先清本书声音语料再采（新 id 从 7 起）。
    adb.get_conn().execute(
        "UPDATE author_voice_runs SET prompt_version='old' WHERE run_id=?",
        (run["run_id"],))
    adb.get_conn().commit()
    _post(f"{_BASE[0]}/_mock/scenario",
          {"rules": [{"contains": TITLE,
                      "response": {"text": json.dumps(REPLY_FORCE,
                                                      ensure_ascii=False)}}]})
    out2b = asyncio.run(_drive(_BASE[0], BookDB(Path(tmp) / "books.db"), adb))
    check("e2e：版本变了不命中缓存", out2b.get("cached"), False)
    check("e2e：版本变了会重采", out2b.get("calls"), 1)
    check("e2e：重采不新增语料",
          len(adb.voice_corpus_of_book("au-voice", STABLE)), 6)
    check("e2e：版本变了仍 active 1 条", out2b.get("active_traits"), 1)

    # --force：只清本书声音语料，序跋不动；重采后仍 6 条 1 条 trait。
    _post(f"{_BASE[0]}/_mock/scenario",
          {"rules": [{"contains": TITLE,
                      "response": {"text": json.dumps(REPLY_FORCE2,
                                                      ensure_ascii=False)}}]})
    out3 = asyncio.run(_drive(_BASE[0], BookDB(Path(tmp) / "books.db"), adb,
                              force=True))
    check("e2e：force 后仍是 6 条语料",
          len(adb.voice_corpus_of_book("au-voice", STABLE)), 6)
    check("e2e：force 后 trait 仍 1 条", len(adb.voice_traits("au-voice")), 1)
    ok("e2e：force 后 active", out3.get("active_traits") == 1)
    adb.close()

    print("\n全绿" if not FAILED else f"\n**{FAILED} 项失败**")
    return 1 if FAILED else 0


_BASE = [""]


def main() -> int:
    test_visibility()
    test_sampling()
    test_select_dedupe()
    test_normalize()
    test_merge()

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
            # 声音采集会调 usage_log；与 enrich e2e 同一条（决策 147）。
            from usage_log import init_for_tests
            init_for_tests(Path(tmp) / "sessions.db")
            books_path = Path(tmp) / "books.db"
            _make_books(books_path)
            books_db = BookDB(books_path)
            chunks, warns = eligible_chunks(books_db, "bk-1")
            check("e2e：序跋/注释/目录块被排除", len(chunks), 6)
            check("e2e：可用块都在正文章",
                  {c.chapter_index for c in chunks}, {1, 2, 3})
            ok("e2e：注释过滤有记账",
               any("注释" in w for w in warns))
            ok("e2e：目录过滤有记账",
               any("目录" in w for w in warns))
            # head_scan 的块级判定（F#<chapter>-<chunk>）也要生效
            cut, _ = eligible_chunks(books_db, "bk-1",
                                     boundaries={"F#2-0": "copyright"})
            check("e2e：head_scan 区域被排除", len(cut), 5)

            adb = AuthorsDB(Path(tmp) / "authors.db")
            aid = adb.upsert_author(NAME)
            adb.get_conn().execute(
                "UPDATE authors SET author_id='au-voice' WHERE author_id=?",
                (aid,))
            adb.get_conn().commit()
            adb.link_book("au-voice", STABLE, book_id="bk-1",
                          position="narrator", position_source="corpus")
            try:
                out = asyncio.run(_drive(base, books_db, adb))
            finally:
                adb.close()
            return _assert_e2e(out, tmp)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:                                # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
