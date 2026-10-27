"""M4 写作风格透镜 —— 纯函数 + 服务装配的回归测试。

钉住四件事：

1. 预算排序语义：priority 高的可选块先保留。
2. 只有 ``transferable=1`` + ``fiction/nonfiction`` 的 trait 进写作。
3. 序跋/访谈语料不进写作；合同禁忌能排除样本。
4. 不选作者时上下文与旧版一致；作者库缺失不阻塞写作。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from authorgraph.voice.select import select_for_scene                 # noqa: E402
from storage.authors_db import AuthorsDB                              # noqa: E402
from storage.writing_db import WritingDB                              # noqa: E402
from services.writing_service import WritingService                   # noqa: E402
from writegraph import budget as budget_mod                           # noqa: E402
from writegraph import voice_card, voice_check, voice_lens            # noqa: E402

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


SAMPLE_A = ("两腿初废时，我曾暗下决心：这辈子就在屋里看书，哪儿也不去了。"
            "母亲把书摞在窗台上，一本一本擦干净。她说，先别想那么远。")
SAMPLE_B = ("是呀，这五十七年我都干了些什么？扶轮问路，扶轮问路啊。"
            "院子里那棵老树还在，树皮裂开，像有人反复写过又擦掉的字。")
SAMPLE_C = ("雨落在铁皮棚上，声音很密。老周没有抬头，只把烟按灭在鞋底。"
            "他说船期改到后天，说完就走进雨里，再没回头。")
PARATEXT = ("序跋里我常被问起为什么要写这些。其实答案很简单："
            "活着这件事，本身就需要一个说法。")


def build_author_db(tmp: Path) -> tuple[AuthorsDB, str, str]:
    adb = AuthorsDB(tmp / "data" / "authors.db")
    aid = adb.upsert_author("透镜作者", nationality="中国")
    adb.link_book(aid, "sb-lens", book_id="bk-lens", role="author",
                  position="narrator")
    adb.set_voice_status(aid, "sb-lens", "ready", content_hash="vh")
    source_id = adb.book_source_id("sb-lens")
    rows = [
        (SAMPLE_A, 1, "introspection"),
        (SAMPLE_B, 2, "introspection"),
        (SAMPLE_C, 3, "action"),
    ]
    corpus_ids = []
    for i, (text, chapter, scene) in enumerate(rows, start=1):
        cid = adb.add_corpus_v2(
            aid, text, source_id=source_id, stable_book_id="sb-lens",
            book_id="bk-lens", chunk_id=f"ck-{i}", chapter_index=chapter,
            chunk_index=1, narrative_layer="narration", scene_type=scene,
            era_year=1990)
        corpus_ids.append(cid)
    adb.add_corpus_v2(aid, PARATEXT, source_id=source_id,
                      stable_book_id="sb-lens", book_id="bk-lens",
                      chunk_id="ck-p", chapter_index=0, chunk_index=1,
                      narrative_layer="paratext", scene_type="other")
    adb.add_voice_trait(
        aid, "narrative_distance", "close", "叙述距离偏近",
        voice_domain="fiction", strength=0.8, scope_type="work",
        scope_ref="sb-lens", confidence=0.9, status="active",
        revision="r1", evidence_ids=(corpus_ids[0], corpus_ids[1]))
    adb.add_voice_trait(
        aid, "sentence_rhythm", "short", "句子短促",
        voice_domain="fiction", strength=0.7, scope_type="work",
        scope_ref="sb-lens", confidence=0.6, status="active",
        revision="r1", evidence_ids=(corpus_ids[2],))
    adb.add_voice_trait(
        aid, "emotional_expression", "withheld", "情绪留白",
        voice_domain="paratext", strength=0.9, scope_type="work",
        scope_ref="sb-lens", confidence=0.95, status="active",
        revision="r1", evidence_ids=(corpus_ids[0],))
    return adb, aid, source_id


def test_lens() -> None:
    print("透镜：规则与样本")
    traits = [
        {"dimension": "narrative_distance", "value": "close",
         "confidence": 0.5, "transferable": 1, "voice_domain": "fiction"},
        {"dimension": "narrative_distance", "value": "distant",
         "confidence": 0.9, "transferable": 1, "voice_domain": "fiction"},
        {"dimension": "sentence_rhythm", "value": "short",
         "confidence": 0.6, "transferable": 1, "voice_domain": "paratext"},
        {"dimension": "closure_pattern", "value": "variable",
         "confidence": 0.9, "transferable": 1, "voice_domain": "fiction"},
        {"dimension": "lexical_texture", "value": "plain",
         "confidence": 0.9, "transferable": 0, "voice_domain": "fiction"},
    ]
    rules, warnings = voice_lens.rank_traits(traits)
    check("每维度只留置信度高的一条",
          [(r["dimension"], r["value"]) for r in rules],
          [("narrative_distance", "distant")])
    ok("序跋、不可迁移、variable 都被挡下", any("未采用" in w for w in warnings))

    samples = [
        {"corpus_id": 1, "excerpt": SAMPLE_A, "scene_type": "introspection",
         "chapter_index": 1, "stable_book_id": "sb", "content_hash": "h1",
         "source_tier": 1},
        {"corpus_id": 2, "excerpt": SAMPLE_B, "scene_type": "action",
         "chapter_index": 2, "stable_book_id": "sb", "content_hash": "h2",
         "source_tier": 2},
        {"corpus_id": 3, "excerpt": SAMPLE_B, "scene_type": "introspection",
         "chapter_index": 1, "stable_book_id": "sb", "content_hash": "h3",
         "source_tier": 1},
        {"corpus_id": 4, "excerpt": SAMPLE_B, "scene_type": "action",
         "chapter_index": 6, "stable_book_id": "sb", "content_hash": "h1",
         "source_tier": 1},
        {"corpus_id": 5, "excerpt": SAMPLE_C, "scene_type": "action",
         "chapter_index": 7, "stable_book_id": "sb", "content_hash": "h5",
         "source_tier": 1},
    ]
    picked, warns = voice_lens.rank_samples(
        samples, {"scene_type": "introspection"},
        contract={"forbidden_cross": ["雨落在铁皮棚"]})
    ids = [s["corpus_id"] for s in picked]
    check("同章/同原文只留最高分", ids, [1, 2])
    ok("合同禁忌排除样本", 5 not in ids)
    check("样本保留原文", picked[0]["excerpt"], SAMPLE_A)
    ok("排除有说明", any("禁忌" in w for w in warns))

    card = voice_card.render("透镜作者", {"rules": rules, "samples": picked})
    ok("卡片有分区标题", "参考风格：透镜作者" in card)
    ok("卡片不写第一人称扮演", "你是" not in card)
    ok("卡片带正典优先声明", "本书正典为准" in card)
    ok("卡片不空", len(card) > 40)
    ok("卡片不超预算", len(card) <= voice_card.DEFAULT_MAX_CHARS + 40)
    check("计数按实际排版", voice_card.counts(card),
          {"rules": len(rules), "samples": len(picked)})
    check("空透镜整块省略",
          voice_card.render("透镜作者", {"rules": [], "samples": []}), "")

    long_samples = [{"excerpt": "长" * 300, "scene_type": "quiet"}
                    for _ in range(10)]
    big = voice_card.render("透镜作者",
                            {"rules": rules, "samples": long_samples})
    ok("超预算时保留正典脚注", "本书正典为准" in big)
    ok("超预算时标出省略", "已省略" in big)
    ok("超预算不越界", len(big) <= voice_card.DEFAULT_MAX_CHARS)


def test_check() -> None:
    print("生成后检查")
    text = ("他坐下来。其实他早就知道了。\n"
            "他坐下来。总之事情就是这样。\n"
            "他坐下来。他心里难过，也很害怕。\n"
            "他坐下来。\n" * 2)
    m = voice_check.metrics(text)
    ok("指标：句数与平均句长", m["sentences"] > 0 and m["avg_sentence_len"] > 0)
    ok("指标：总结句比例", m["summary_sentence_ratio"] > 0.03)
    ok("指标：情绪直说密度", m["emotion_density"] > 0)
    ok("指标：重复开头", m["repeated_opening_ratio"] > 0)

    result = voice_check.check(
        text, traits=[{"dimension": "sentence_rhythm", "value": "short",
                       "transferable": 1, "confidence": 0.8}],
        author_id="au-x")
    codes = [f["code"] for f in result["findings"]]
    ok("总结句告警", "summary_sentence_density" in codes)
    ok("重复开头告警", "repeated_openings" in codes)
    ok("检查启用", result["enabled"])
    ok("只说 info/warning",
       all(f["severity"] in ("info", "warning") for f in result["findings"]))
    ok("没有阻断字段", all("block" not in f for f in result["findings"]))

    empty = voice_check.check("")
    check("空正文：不启用且不报错", (empty["enabled"], empty["findings"]),
          (False, []))


def _block(text: str, block_id: str) -> str:
    marker = f"## [{block_id}]"
    if marker not in text:
        return ""
    tail = text.split(marker, 1)[1]
    return tail.split("\n\n## [", 1)[0]


def test_service(tmp: Path) -> None:
    print("服务：装配与降级")
    os.makedirs(tmp / "data", exist_ok=True)
    adb, aid, _sid = build_author_db(tmp)
    db = WritingDB(tmp / "data" / "writing.db")
    svc = WritingService(SimpleNamespace(project_dir=tmp), db=db)
    w = svc.create_work("透镜之书", narrative_pov="first")
    ch = svc.create_chapter(w, title="雨夜")
    svc.set_contract(ch["id"], purpose="雨中告别，老周离开",
                     must_hit_now=["老周离开"],
                     forbidden_cross=["雨落在铁皮棚"])

    relaxed = select_for_scene(adb, aid, {"scene_type": "landscape"},
                               limit=10)
    ok("场景无对应时放宽取候选", len(relaxed["samples"]) > 0)
    ok("放宽有说明", any("场景" in w for w in relaxed["warnings"]))

    base = svc.assemble(w, 1)
    base_ids = [b["id"] for b in base["manifest"]["blocks"]]
    ok("不选作者：没有 author_voice 块", "author_voice" not in base_ids)
    ok("不选作者：没有 style 诊断", "style" not in base)

    out = svc.assemble(w, 1, style_ref={
        "author_id": aid, "scene_type": "introspection"})
    block = _block(out["text"], "author_voice")
    ok("选作者：块出现", bool(block))
    check("选作者：诊断无错误码", out["style"]["code"], "")
    check("选作者：作者名", out["style"]["author_name"], "透镜作者")
    ok("序跋不进口吻", "序跋里我常被问起" not in block)
    ok("合同禁忌样本被排除", "雨落在铁皮棚" not in block)
    ok("paratext trait 不进规则", "情绪留白" not in block)
    ok("块内有场景例证标记", "[内省 | 书里]" in block)
    tokens = next(b["tokens"] for b in out["manifest"]["blocks"]
                  if b["id"] == "author_voice")
    ok("块预算不超过 700 tokens", tokens <= 700)

    svc.set_style_ref(w, aid, scene_type="introspection")
    prof = svc.get_style_profile(w)
    check("默认选择落库", prof["author_refs"][0]["author_id"], aid)
    out2 = svc.assemble(w, 1)
    ok("默认选择生效", bool(_block(out2["text"], "author_voice")))
    svc.set_style_ref(w, "")
    out3 = svc.assemble(w, 1)
    ok("清空后回到旧上下文",
       "author_voice" not in [b["id"] for b in out3["manifest"]["blocks"]])

    adb.set_voice_status(aid, "sb-lens", "partial")
    out4 = svc.assemble(w, 1, style_ref={"author_id": aid})
    ok("partial 标轻参考", "样本不足" in _block(out4["text"], "author_voice"))
    adb.set_voice_status(aid, "sb-lens", "ready")

    silent = adb.upsert_author("无声作者")
    out5 = svc.assemble(w, 1, style_ref={"author_id": silent})
    check("无声音：不注入并报原因", out5["style"]["code"], "no_voice_samples")
    ok("无声音：没有块",
       "author_voice" not in [b["id"] for b in out5["manifest"]["blocks"]])

    svc.save_content(w, 1, "他坐下来。其实他早就知道了。\n"
                           "他坐下来。总之事情就是这样。", reason="manual")
    check_out = svc.style_check(w, 1, style_ref={"author_id": aid})
    ok("风格检查：有指标", check_out["metrics"]["chars"] > 0)
    ok("风格检查：只报告", all(f["severity"] in ("info", "warning")
                              for f in check_out["findings"]))
    db.close()
    adb.close()

    # 作者库不存在 → 不建空库、不阻塞。
    empty_root = Path(tempfile.mkdtemp())
    db2 = WritingDB(empty_root / "data" / "writing.db")
    svc2 = WritingService(SimpleNamespace(project_dir=empty_root), db=db2)
    w2 = svc2.create_work("没有作者库")
    ch2 = svc2.create_chapter(w2, title="第一章")
    svc2.set_contract(ch2["id"], purpose="过河")
    out6 = svc2.assemble(w2, 1, style_ref={"author_id": "au-none"})
    check("作者库缺失：报不可用", out6["style"]["code"],
          "author_db_unavailable")
    ok("作者库缺失：不创建文件",
       not (empty_root / "data" / "authors.db").exists())
    ok("作者库缺失：写作继续", bool(out6["text"]))
    db2.close()


def test_budget_order() -> None:
    print("预算：可选块排序")
    blocks = {
        "chapter_contract": "甲" * 100,
        "facts_current": "乙" * 100,
        "prev_tail": "",
        "context_card": "丙" * 100,
        "author_voice": "戊" * 1000,
        "reference_material": "丁" * 1000,
    }
    result = budget_mod.compile_blocks(blocks, budget=700)
    kept = [b["id"] for b in result["manifest"]["blocks"] if not b["dropped"]]
    dropped = [b["id"] for b in result["manifest"]["blocks"] if b["dropped"]]
    ok("高 priority 先留", "author_voice" in kept)
    ok("参考素材先丢", "reference_material" in dropped)


async def _http_smoke(tmp: Path) -> None:
    """真 aiohttp 请求：PUT 默认选择 → 装配带块 → 风格检查。"""
    from aiohttp.test_utils import TestClient, TestServer

    from api_server import create_app
    from storage.db import BookDB

    os.makedirs(tmp, exist_ok=True)
    adb, aid, _sid = build_author_db(tmp)
    db = WritingDB(tmp / "data" / "writing.db")
    svc = WritingService(SimpleNamespace(project_dir=tmp), db=db)
    w = svc.create_work("HTTP 透镜", narrative_pov="first")
    ch = svc.create_chapter(w, title="第一章")
    svc.set_contract(ch["id"], purpose="雨中告别")
    svc.save_content(w, 1, "他坐下来。其实他早就知道了。", reason="manual")
    bdb = BookDB(tmp / "books.db")
    bdb.initialize()
    app = create_app(bdb, config=SimpleNamespace(project_dir=tmp))
    app["writing_service"] = svc
    try:
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                f"/api/writing/works/{w}/style",
                json={"author_id": aid, "scene_type": "introspection"})
            data = await resp.json()
            check("HTTP：设置默认作者", resp.status, 200)
            check("HTTP：返回并保存",
                  data.get("author_refs", [{}])[0].get("author_id"), aid)

            resp = await client.get(
                f"/api/writing/works/{w}/chapters/1/context"
                f"?style_author_id={aid}")
            data = await resp.json()
            check("HTTP：装配成功", resp.status, 200)
            check("HTTP：带作者诊断",
                  data.get("style", {}).get("code"), "")
            ok("HTTP：上下文含参考风格块",
               "## [author_voice]" in (data.get("text") or ""))

            resp = await client.get(
                f"/api/writing/works/{w}/chapters/1/style-check"
                f"?style_author_id={aid}")
            data = await resp.json()
            check("HTTP：风格检查", resp.status, 200)
            ok("HTTP：检查有指标", data.get("metrics", {}).get("chars", 0) > 0)
    finally:
        bdb.close()
        db.close()
        adb.close()


def test_routes(tmp: Path) -> None:
    print("路由：风格设置与对照检查")
    try:
        from api_server import create_app
        from storage.db import BookDB
        os.makedirs(tmp, exist_ok=True)
        bdb = BookDB(tmp / "books.db")
        bdb.initialize()
        app = create_app(bdb)
        paths = {r.resource.canonical for r in app.router.routes()}
        for path in ("/api/writing/works/{work_id}/style",
                     "/api/writing/works/{work_id}/chapters/{index}/style-check"):
            ok(f"路由：{path}", path in paths)
        bdb.close()
    except Exception as exc:                                  # noqa: BLE001
        print(f"FAIL 路由注册：{exc}")
        globals()["FAILED"] = globals()["FAILED"] + 1


def main() -> int:
    test_budget_order()
    test_lens()
    test_check()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        test_service(Path(raw))
        test_routes(Path(raw) / "routes")
        print("HTTP：请求链路")
        asyncio.run(_http_smoke(Path(raw) / "http"))
    print()
    if FAILED:
        print(f"FAILED {FAILED}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

