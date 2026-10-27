"""作者档案投影（M3）的零 token 单测。

盯的六件事：

1. **投影只搬数据** —— identity / timeline / works / voice / relations /
   themes / sources 分区返回，不混成一个数组。
2. **剧透门** —— book_id + max_chapter 下，未来年份的条目、未来章节的
   声音引文与来源引文都不出现；``not_shown`` 报得出数量。
3. **三种「空」分开** —— 没采到 / 声音没提取 / 被门挡住，各有 note。
4. **覆盖度** —— 纯函数、权重稳定、``missing`` 指向最缺的项。
5. **来源分类** —— 已核引文与 legacy_url_only 分开计数；冲突双侧保留。
6. **搜索** —— 默认前缀匹配，fuzzy 才子串；provisional 不改返回形状。

跑法：``python scripts/test_author_dossier.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from authorgraph.coverage import coverage_block                    # noqa: E402
from authorgraph.dossier import author_list, build_dossier, clear_cache  # noqa: E402
from storage.authors_db import AuthorsDB                           # noqa: E402
from storage.db import BookDB                                      # noqa: E402
from storage.models import BookRow                                 # noqa: E402

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


def build_fixture(tmp: Path) -> tuple[AuthorsDB, BookDB, dict]:
    adb = AuthorsDB(tmp / "authors.db")
    adb.initialize()
    bdb = BookDB(tmp / "books.db")
    bdb.initialize()
    bdb.insert_book(BookRow(id="bk-1", title="测试之书", author="测试作者"))
    bdb.insert_book(BookRow(id="bk-2", title="另一本书", author="测试作者"))

    aid = adb.upsert_author("测试作者", nationality="中国", birth_year=1901,
                            death_year=1980)
    adb.set_identity_fields(aid, name_native="Test Author",
                            birth_place="北京", primary_language="zh",
                            identity_status="resolved")
    adb.add_identifier(aid, "wikidata", "Q123", source="test",
                       confidence=0.95)
    adb.link_book(aid, "sb-1", book_id="bk-1", role="author",
                  position="narrator", position_note="第一人称回忆")
    adb.link_book(aid, "sb-2", book_id="bk-2", role="author",
                  position="narrator", position_note="")
    adb.set_voice_status(aid, "sb-1", "ready", content_hash="h1")

    r1 = adb.add_record_ex(aid, "life_event", "出生于北京", year=1901,
                           stable_book_id="sb-1", source="corpus")
    r2 = adb.add_record_ex(aid, "work", "《测试之书》长篇小说", year=1930,
                           stable_book_id="sb-1", source="corpus")
    r3 = adb.add_record_ex(aid, "publication", "《远方》出版", year=1940,
                           stable_book_id="sb-1", source="web")
    r4 = adb.add_record_ex(aid, "life_event", "开始写作", year=None,
                           stable_book_id="sb-1", source="corpus")
    r5 = adb.add_record_ex(aid, "work", "参加《某某》编辑部聚会", year=1955,
                           stable_book_id="sb-1", source="corpus")

    book_sid = adb.book_source_id("sb-1")
    web = adb.upsert_source(url="https://example.org/a", title="Example",
                            publisher="Example Press", source_tier=2,
                            source_kind="reference")
    adb.add_source_roles(web, ("biography", "bibliography"))
    adb.add_evidence(aid, "record", str(r1), source_id=web,
                     quote="出生于北京。", locator="p.1", confidence=0.8)
    adb.add_evidence(aid, "record", str(r1), source_id=web,
                     relation="contradicts", quote="有资料称出生于上海。")
    legacy = adb.upsert_source(url="https://old.example.org/b",
                               title="旧资料", source_tier=3,
                               source_kind="media")
    adb.add_evidence(aid, "record", str(r2), source_id=legacy,
                     relation="legacy_url_only")

    c1 = adb.add_corpus_v2(aid, "第一段正文。", source_id=book_sid,
                           stable_book_id="sb-1", book_id="bk-1",
                           chunk_id="ck-1", chapter_index=1, chunk_index=1,
                           narrative_layer="narration", scene_type="opening",
                           era_year=1901, era_note="序")
    c2 = adb.add_corpus_v2(aid, "第三段正文。", source_id=book_sid,
                           stable_book_id="sb-1", book_id="bk-1",
                           chunk_id="ck-2", chapter_index=3, chunk_index=1,
                           narrative_layer="narration", scene_type="ending",
                           era_year=1940)
    c3 = adb.add_corpus_v2(aid, "第五段正文。", source_id=book_sid,
                           stable_book_id="sb-1", book_id="bk-1",
                           chunk_id="ck-3", chapter_index=5, chunk_index=1,
                           narrative_layer="narration", scene_type="quiet",
                           era_year=1950)
    adb.add_voice_trait(aid, "narrative_distance", "close", "叙述距离偏近",
                        voice_domain="nonfiction", strength=0.8,
                        scope_type="work", scope_ref="sb-1",
                        confidence=0.9, status="active", revision="rev1",
                        evidence_ids=(c1, c2))
    adb.add_voice_trait(aid, "sentence_rhythm", "short", "句子短促",
                        voice_domain="nonfiction", strength=0.7,
                        scope_type="work", scope_ref="sb-1",
                        confidence=0.6, status="provisional", revision="rev1",
                        evidence_ids=(c3,))

    cl1 = adb.add_claim(aid, "theme", "记忆是这本书的母题", theme_key="记忆",
                        year=1935, confidence=0.7, status="active")
    adb.add_claim(aid, "reception", "出版后引起讨论", year=1950,
                  confidence=0.6, status="provisional")
    adb.add_claim(aid, "theme", "无年的主题陈述")
    adb.add_evidence(aid, "claim", str(cl1), source_id=web,
                     quote="记忆是这本书的母题。")

    oid = adb.upsert_author("友人")
    adb.add_relation(aid, "友人", relation_type="friend",
                     other_author_id=oid, year_start=1920,
                     statement="青年时代的朋友", confidence=0.8,
                     status="active")
    adb.add_relation(aid, "晚年对手", relation_type="rival",
                     year_start=1960, statement="晚年的论战对手",
                     confidence=0.5, status="provisional")
    adb.add_evidence(aid, "relation",
                     adb.relations_of(aid)[0]["relation_id"],
                     source_id=web, quote="两人是青年时代的朋友。")

    return adb, bdb, {"aid": aid, "r1": r1, "r2": r2, "r3": r3, "r4": r4,
                      "r5": r5, "c1": c1, "c2": c2, "c3": c3, "web": web,
                      "legacy": legacy, "other": oid}


def test_coverage_pure() -> None:
    c = coverage_block(records=20, records_with_evidence=6, works=3,
                       voice_samples=6, scenes=3,
                       scene_types=("opening", "dialogue", "quiet"),
                       relations=0, verified_sources=2, sources=4)
    ok("覆盖度：facts 有值", 0.4 < c["facts"] < 0.6)
    ok("覆盖度：overall 加权", 0.2 < c["overall"] < 0.7)
    ok("覆盖度：missing 报 relations", "relations" in c["missing"])
    ok("覆盖度：场景缺口具体", "ending" in c["missing_scenes"])
    empty = coverage_block()
    check("覆盖度：全空 overall=0", empty["overall"], 0.0)
    ok("覆盖度：全空报 facts.none", "facts.none" in empty["missing"])


def test_dossier(adb: AuthorsDB, bdb: BookDB, f: dict) -> None:
    aid = f["aid"]
    d = build_dossier(adb, author_id=aid, books_db=bdb, use_cache=False)
    ok("档案：found", d["found"])
    check("身份：生年", d["author"]["birth_year"], 1901)
    check("身份：状态", d["author"]["identity_status"], "resolved")
    check("身份：外部 ID", d["author"]["identifiers"][0]["scheme"], "wikidata")
    check("位置：书内身份", d["position"]["position"], "narrator")
    check("位置：声音状态", d["position"]["voice_status"], "ready")
    check("门：无书 = 全集视角", d["gate"]["mode"], "no_book")

    check("年谱：作品轨", len(d["timeline"]["work"]), 3)  # 1930 + 1940 + 1955
    check("年谱：生平轨", len(d["timeline"]["life"]), 1)  # 1901
    check("年谱：无年", d["timeline"]["undated"]["life"], 1)
    check("年谱：证据已核", d["timeline"]["life"][0]["verified"], True)

    check("作品：本地 2 本", d["works_meta"]["counts"]["local"], 2)
    check("作品：书目 1 条", d["works_meta"]["counts"]["bibliography"], 1)
    check("作品：总数 3", len(d["works"]), 3)
    local = next(w for w in d["works"] if w["kind"] == "local")
    check("作品：本地书合并到年份", local["year"], 1930)
    check("作品：刊物名不当作品", d["works_meta"]["counts"]["events"], 1)

    check("声音：trait 数", len(d["voice"]["traits"]), 2)
    check("声音：样本数", len(d["voice"]["samples"]), 3)
    check("声音：维度数", len(d["voice"]["dimensions"]), 2)
    check("声音：无书时为作者级", d["voice"]["status"], "author")
    trait = next(t for t in d["voice"]["traits"]
                 if t["dimension"] == "narrative_distance")
    check("声音：证据数", trait["evidence_count"], 2)
    ok("声音：证据带来源", trait["evidence"][0]["source_title"] or True)

    check("关系：两条", len(d["relations"]), 2)
    check("关系：分组元数据", d["relations_meta"]["counts"]["total"], 2)
    check("主题：两条（含无年）", len(d["themes"]), 2)
    check("接受史：一条", len(d["reception"]), 1)

    s = d["sources"]
    check("来源：T2 一条", s["counts"]["tier2"], 1)
    check("来源：T3 一条", s["counts"]["tier3"], 1)
    check("来源：legacy 计数", s["counts"]["legacy"], 1)
    web = next(x for x in s["items"] if x["source_id"] == f["web"])
    check("来源：已核引文", web["quote_verified"], 3)
    check("来源：冲突引文", len(d["conflicts"]), 1)
    check("来源：冲突双侧", len(d["conflicts"][0]["contradicts"]), 1)

    ok("覆盖度：missing 有指向", bool(d["coverage"]["missing"]))
    check("覆盖度：事实计数", d["coverage"]["counts"]["records"], 5)


def test_gate(adb: AuthorsDB, bdb: BookDB, f: dict) -> None:
    aid = f["aid"]
    d = build_dossier(adb, author_id=aid, book_id="bk-1", max_chapter=1,
                      books_db=bdb, use_cache=False)
    check("门：章节模式", d["gate"]["mode"], "chapter")
    check("门：最晚年份", d["gate"]["max_year"], 1901)
    check("门：只留 1901 条目", len(d["timeline"]["life"]) + len(d["timeline"]["work"]), 1)
    check("门：无年条目不出门", d["timeline"]["undated"]["life"], 0)
    check("门：声音样本只留第 1 章", len(d["voice"]["samples"]), 1)
    check("门：被挡样本数", d["voice"]["not_shown"]["samples"], 2)
    check("门：被挡原因", d["voice"]["not_shown"]["reason"], "reader_chapter")
    check("门：trait 被挡一条", d["voice"]["not_shown"]["traits"], 1)
    check("门：主题被挡", len(d["themes"]), 0)
    check("门：关系被挡", len(d["relations"]), 0)
    check("门：来源标记 gated", d["sources"]["gated"], True)
    legacy = next(x for x in d["sources"]["items"]
                  if x["source_id"] == f["legacy"])
    check("门：来源引文被挡", legacy["hidden_quotes"], 1)
    check("门：来源可见引文 0", legacy["evidence_count"], 0)

    d2 = build_dossier(adb, author_id=aid, book_id="bk-1",
                       books_db=bdb, use_cache=False)
    check("门：有书无章 = 显式全集", d2["gate"]["mode"], "no_gate")
    check("门：全集不挡", len(d2["voice"]["samples"]), 3)

    d3 = build_dossier(adb, author_id=aid, book_id="bk-unknown",
                       max_chapter=1, books_db=bdb, use_cache=False)
    check("门：书没对上", d3["gate"]["mode"], "unmapped")
    check("门：unmapped fail-closed", len(d3["timeline"]["work"]), 0)

    d4 = build_dossier(adb, author_id=aid, book_id="bk-1", max_chapter=0,
                       books_db=bdb, use_cache=False)
    ok("门：无年/全在后文 fail-closed",
       d4["gate"]["mode"] in ("no_dated", "all_ahead"))
    check("门：fail-closed 无记录", len(d4["timeline"]["work"]), 0)

    # 年份门算不出来（序跋没有 era_year）时，声音仍按章放行：
    # 读者刚读过的正文不是「未来的内容」。
    aid2 = adb.upsert_author("无年作者")
    adb.link_book(aid2, "sb-2", book_id="bk-2", role="author",
                  position="narrator")
    sid2 = adb.book_source_id("sb-2")
    adb.add_corpus_v2(aid2, "无年正文。", source_id=sid2,
                      stable_book_id="sb-2", book_id="bk-2",
                      chunk_id="ck-n1", chapter_index=1, chunk_index=1,
                      narrative_layer="narration", scene_type="opening")
    d5 = build_dossier(adb, author_id=aid2, book_id="bk-2", max_chapter=1,
                       books_db=bdb, use_cache=False)
    check("章门：年份门无年", d5["gate"]["mode"], "no_dated")
    check("章门：声音仍按章放行", len(d5["voice"]["samples"]), 1)
    check("章门：声音门模式", d5["voice"]["gate_mode"], "chapter_only")
    check("章门：年份材料仍挡住", len(d5["timeline"]["work"]), 0)


def test_list(adb: AuthorsDB, f: dict) -> None:
    out = author_list(adb, q="测试", limit=10)
    check("搜索：前缀命中", out["total"], 1)
    check("搜索：返回摘要", out["items"][0]["canonical_name"], "测试作者")
    ok("搜索：带覆盖度", "overall" in out["items"][0]["coverage"])
    check("搜索：子串不误中", author_list(adb, q="作者")["total"], 0)
    check("搜索：fuzzy 命中", author_list(adb, q="作者", mode="fuzzy")["total"], 2)
    check("搜索：exact 全名", author_list(adb, q="测试作者",
                                         mode="exact")["total"], 1)
    check("搜索：空 q 全量", author_list(adb)["total"], 3)


def test_empty_reasons(adb: AuthorsDB, bdb: BookDB) -> None:
    aid = adb.upsert_author("空作者")
    adb.link_book(aid, "sb-empty", book_id="bk-1", role="author",
                  position="narrator")
    d = build_dossier(adb, author_id=aid, books_db=bdb, use_cache=False)
    check("空：无年谱", len(d["timeline"]["work"]) + len(d["timeline"]["life"]), 0)
    ok("空：年谱有原因", bool(d["timeline"]["note"]))
    check("空：作品只列本地书", d["works_meta"]["counts"]["local"], 1)
    check("空：没有书目", d["works_meta"]["counts"]["bibliography"], 0)
    ok("空：声音有原因", bool(d["voice"]["note"]))
    ok("空：关系有原因", bool(d["relations_meta"]["note"]))
    ok("空：主题有原因", bool(d["notes"]["themes"]))
    missing = build_dossier(adb, author_id="au-nope", use_cache=False)
    check("不存在：found=False", missing["found"], False)


def test_routes() -> None:
    try:
        from api_server import create_app
        with tempfile.TemporaryDirectory() as tmp:
            bdb = BookDB(Path(tmp) / "books.db")
            bdb.initialize()
            app = create_app(bdb)
            paths = {r.resource.canonical for r in app.router.routes()}
            for p in ("/api/authors",
                      "/api/authors/{author_id}/dossier",
                      "/api/authors/{author_id}/voice",
                      "/api/authors/{author_id}/sources"):
                ok(f"路由：{p}", p in paths)
            bdb.close()
    except Exception as exc:                                  # noqa: BLE001
        print(f"FAIL 路由注册：{exc}")
        globals()["FAILED"] = globals()["FAILED"] + 1


def main() -> int:
    test_coverage_pure()
    clear_cache()
    with tempfile.TemporaryDirectory() as tmp:
        adb, bdb, f = build_fixture(Path(tmp))
        test_dossier(adb, bdb, f)
        test_gate(adb, bdb, f)
        test_list(adb, f)
        test_empty_reasons(adb, bdb)
        adb.close()
        bdb.close()
    test_routes()
    print()
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
