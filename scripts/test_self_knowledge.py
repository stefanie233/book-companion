"""自我说明层（决策 154）的测试。

零外网：探针用 fake config / 临时库；卡片用仓库内 ``prompts/self``。
跑法：``python scripts/test_self_knowledge.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.self_knowledge import _score, explain, load_cards      # noqa: E402
from services.self_probes import (                                   # noqa: E402
    PROBES, probe_authors, probe_provider, probe_semantic, probe_vcs)
from storage.authors_db import AuthorsDB                             # noqa: E402
from storage.embedding_db import EmbeddingDB, resolve_embedding_db_path  # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"ok   {label}")
    else:
        FAILED += 1
        print(f"FAIL {label}\n     期望 {want!r}\n     实际 {got!r}")


def section(name: str) -> None:
    print(f"\n-- {name}")


def fake_config(tmp: str, **over) -> SimpleNamespace:
    base = dict(
        project_dir=str(tmp), providers_path=None,
        api_key="sk-test", api_base="https://example.com/v1", model="chat-m",
        embedding_api_key="", embedding_base_url="", embedding_model="",
        tts_api_key="", tts_api_base="", tts_model="",
        asr_api_key="", asr_api_base="", search_api_key="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_cards() -> None:
    section("cards")
    cards = load_cards()
    ids = [c["id"] for c in cards]
    check("6 张卡", len(cards), 6)
    check("id 唯一", len(ids), len(set(ids)))
    for card in cards:
        cid = card["id"]
        check(f"{cid} 有摘要", bool(card.get("summary")), True)
        check(f"{cid} 有位置", bool(card.get("where")), True)
        check(f"{cid} probe 合法", card.get("probe") in (None, *PROBES), True)
        check(f"{cid} related 存在",
              all(r in ids for r in card.get("related") or []), True)
        check(f"{cid} danger 合法",
              all(d in {"delete", "discard", "rollback", "merge",
                        "overwrite", "publish"}
                  for d in card.get("danger") or []), True)

def test_match() -> None:
    section("match")
    cases = [
        ("合同是啥", "writing-contract"),
        ("我改坏了能退吗", "writing-version"),
        ("作者档案怎么是空的", "author-layer"),
        ("版本控制在哪", "writing-version"),
        ("为什么语义搜索用不了", "semantic-recall"),
        ("作品记忆放什么", "writing-workspace"),
        ("模型绑定在哪", "model-binding"),
        ("为什么联网搜索用不了", "model-binding"),
        ("语音为什么用不了", "model-binding"),
        ("分支怎么合并", "writing-version"),
    ]
    for question, want in cases:
        got = explain(question)
        check(f"{question} -> {want}",
              (got.get("cards") or [{}])[0].get("id"), want)
    check("无匹配", explain("今天天气怎么样").get("matched"), False)
    check("扮演禁用",
          explain("合同", ctx={"mode": "roleplay"}).get("disabled"), True)
    check("topic 精确命中",
          explain(topic="author-layer")["cards"][0]["id"], "author-layer")
    check("不可逆动作带 guard", "guard" in explain("我改坏了能退吗"), True)
    check("合同不带 guard", "guard" in explain("合同是啥"), False)
    full = (explain("合同是啥", depth="full").get("cards") or [{}])[0]
    check("full 带展开", bool(full.get("detail")), True)
    check("full 带误区", bool(full.get("pitfalls")), True)


def test_page_boost() -> None:
    section("page boost")
    card = {"title": "测试", "aliases": [], "summary": "合同 验收",
            "pages": ["writing"]}
    diff = _score("合同怎么写", card, "writing") - _score(
        "合同怎么写", card, "home")
    check("写作页加分", diff, 10)

def test_provider() -> None:
    section("provider")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = fake_config(tmp)
        out = probe_provider({"config": cfg})
        check("chat 就绪", out["facts"]["chat"]["ready"], True)
        check("embedding 未绑", out["facts"]["embedding"]["ready"], False)
        check("partial", out["state"], "partial")
        raw = json.dumps(out, ensure_ascii=False)
        check("不泄露 key", "sk-test" in raw, False)
        check("不泄露端点", "example.com" in raw, False)
        check("只报模型名", out["facts"]["chat"]["model"], "chat-m")
        off = fake_config(tmp, api_key="", api_base="", model="",
                          search_api_key="")
        check("全关 unconfigured",
              probe_provider({"config": off})["state"], "unconfigured")
        with_emb = fake_config(
            tmp, embedding_api_key="k", embedding_base_url="https://e",
            embedding_model="em", tts_api_key="k", tts_api_base="https://t",
            search_api_key="sk")
        facts = probe_provider({"config": with_emb})["facts"]
        check("embedding 就绪", facts["embedding"]["ready"], True)
        check("tts 就绪", facts["tts"]["ready"], True)
        check("web 走 tavily", facts["web"]["mode"], "tavily")


def test_semantic() -> None:
    section("semantic")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = fake_config(tmp)
        check("未绑定", probe_semantic({"config": cfg})["state"],
              "unconfigured")
        cfg = fake_config(tmp, embedding_api_key="k",
                          embedding_base_url="https://e",
                          embedding_model="em")
        check("无索引", probe_semantic({"config": cfg})["state"], "no_index")
        path = resolve_embedding_db_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = EmbeddingDB(path)
        db.upsert_many("em", [("c1", "h1", "b1", 0, 2, b"\x00" * 8)])
        db.close()
        out = probe_semantic({"config": cfg})
        check("有索引", out["state"], "ok")
        check("chunks", out["facts"]["chunks"], 1)
        check("不泄露端点", "https://e" in json.dumps(out), False)
        other = fake_config(tmp, embedding_api_key="k",
                            embedding_base_url="https://e",
                            embedding_model="other")
        mismatch = probe_semantic({"config": other})
        check("模型不匹配", mismatch["state"], "model_mismatch")
        check("保留旧模型名", mismatch["facts"]["index_models"], ["em"])

class FakeBookDB:
    """只提供 book_identity 需要的两件事；books 查询抛错会被它自己吞掉。"""

    def get_conn(self):
        raise RuntimeError("no books conn")

    def get_import_log_by_book(self, book_id: str):
        return SimpleNamespace(stable_book_id="sb1")


def test_authors() -> None:
    section("authors")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = fake_config(tmp)
        check("没书", probe_authors({"config": cfg})["state"], "no_book")
        base = {"config": cfg, "db": FakeBookDB(), "book_id": "b1",
                "reading_chapter": 0}
        check("没作者库", probe_authors(base)["state"], "no_author")
        adb = AuthorsDB(Path(tmp) / "data" / "authors.db")
        aid = adb.upsert_author("张三")
        adb.link_book(aid, "sb1")
        adb.close()
        check("有作者没内容", probe_authors(base)["state"], "no_harvest")
        adb = AuthorsDB(Path(tmp) / "data" / "authors.db")
        adb.add_record(aid, "event", "生于某年", year=1900)
        adb.close()
        check("只有记录无年份语料", probe_authors(base)["state"], "no_dated")
        adb = AuthorsDB(Path(tmp) / "data" / "authors.db")
        adb.add_corpus(aid, "sb1", "卷末的样本", chunk_id="c10",
                       chapter_index=10, era_year=1990)
        adb.close()
        ahead = probe_authors(base)
        check("语料全在未读章节", ahead["state"], "all_ahead")
        check("报出差多远", ahead["facts"]["ahead"], 1)
        adb = AuthorsDB(Path(tmp) / "data" / "authors.db")
        adb.add_corpus(aid, "sb1", "开头的样本", chunk_id="c0",
                       chapter_index=0, era_year=1990)
        adb.close()
        check("门内可见", probe_authors(base)["state"], "ok")


def test_vcs() -> None:
    section("vcs")
    check("没作品", probe_vcs({})["state"], "no_work")
    clean = SimpleNamespace(vcs_status=lambda wid: {
        "clean": True, "branch": "main", "branches": [1], "stash_count": 0,
        "chapter_counts": {}, "file_counts": {}, "ledger_dirty": False,
        "head": {"time_created": 1, "message": "init"}})
    out = probe_vcs({"writing_ctx": {"work_id": "w1"}, "writing_svc": clean})
    check("clean", out["state"], "clean")
    dirty = SimpleNamespace(vcs_status=lambda wid: {
        "clean": False, "branch": "main", "branches": [1, 2],
        "stash_count": 1, "chapter_counts": {"changed": 2},
        "file_counts": {"added": 1}, "ledger_dirty": True,
        "head": {"time_created": 2, "message": "draft"}})
    out = probe_vcs({"writing_ctx": {"work_id": "w1"}, "writing_svc": dirty})
    check("dirty", out["state"], "dirty")
    check("未提交计数", out["facts"]["uncommitted"], 4)
    check("分支数", out["facts"]["branches"], 2)


def test_registry() -> None:
    section("registry")
    from tools.builtin import create_builtin_registry
    from tools.registry import ToolRegistry
    from writegraph.chat_tools import build_writing_registry
    reg = create_builtin_registry()
    spec = reg.get("self_explain")
    check("主 registry 有 self_explain", spec is not None, True)
    check("只读权限", spec.permission_mode.name, "READ_ONLY")
    base = ToolRegistry()
    base.register(spec)
    svc = SimpleNamespace(vcs_status=lambda wid: {
        "clean": True, "branch": "main", "branches": [1], "stash_count": 0,
        "chapter_counts": {}, "file_counts": {}, "ledger_dirty": False,
        "head": {"time_created": 1, "message": "init"}})
    wr = build_writing_registry(base, svc, {"work_id": "w1"})
    check("写作 registry 也有", wr.get("self_explain") is not None, True)
    check("写作现场挂上了", wr.reading_ctx.get("page"), "writing")
    out = json.loads(wr.execute("self_explain", '{"question": "版本在哪"}'))
    check("写作现场探针可见", out["status"]["vcs"]["state"], "clean")


def main() -> int:
    test_cards()
    test_match()
    test_page_boost()
    test_provider()
    test_semantic()
    test_authors()
    test_vcs()
    test_registry()
    print("\n==== summary ====")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())



