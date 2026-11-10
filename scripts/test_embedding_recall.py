"""语义召回（决策 148）的测试：端点、向量库、排序、检索、写作工具。

零外网：embedding 调用用假端点/假向量替换；真实联调另跑
``python scripts/build_embedding_index.py`` 与 ``writing_research``。

跑法：``python scripts/test_embedding_recall.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from provider_store import ProviderStore, ResolvedEndpoint            # noqa: E402
from services import embedding_service as embed_mod                  # noqa: E402
from services.writing_service import WritingService                  # noqa: E402
from storage.embedding_db import (                                   # noqa: E402
    EmbeddingDB,
    content_hash,
    resolve_embedding_db_path,
)
from storage.writing_db import WritingDB                             # noqa: E402
from tools import ToolRegistry                                       # noqa: E402
from writegraph import semantic                                      # noqa: E402
from writegraph.chat_tools import build_writing_registry             # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"ok   {label}")
    else:
        FAILED += 1
        print(f"FAIL {label}\n     期望 {want!r}\n     实际 {got!r}")


def raises(label: str, exc, fn) -> None:
    global FAILED
    try:
        fn()
    except exc:
        print(f"ok   {label}")
        return
    except Exception as other:                       # noqa: BLE001
        FAILED += 1
        print(f"FAIL {label}\n     抛的是 {type(other).__name__}: {other}")
        return
    FAILED += 1
    print(f"FAIL {label}\n     没有抛 {exc.__name__}")


def _books_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE book_chapters (book_id TEXT, chapter_index INTEGER,
                                    title TEXT);
        CREATE TABLE book_chunks (id TEXT PRIMARY KEY, book_id TEXT,
                                  chapter_index INTEGER, chunk_index INTEGER,
                                  content TEXT, char_count INTEGER);
    """)
    conn.execute("INSERT INTO books VALUES ('b1','窄门')")
    conn.execute("INSERT INTO books VALUES ('b2','茶花女')")
    conn.execute("INSERT INTO book_chapters VALUES ('b1',20,'第八章')")
    conn.execute("INSERT INTO book_chapters VALUES ('b1',21,'第九章')")
    conn.execute("INSERT INTO book_chapters VALUES ('b2',38,'第十四章')")
    conn.execute("INSERT INTO book_chunks VALUES"
                 " ('c1','b1',20,0,'我把感情压在心里，像守着一件圣物。',200)")
    conn.execute("INSERT INTO book_chunks VALUES"
                 " ('c2','b1',21,0,'她越克制，越显得虔敬。',200)")
    conn.execute("INSERT INTO book_chunks VALUES"
                 " ('c3','b2',38,0,'爱情与牺牲在同一个夜晚结束。',200)")
    # 命中点放在长块后半段：语义卡也要取命中窗口，不能只截块头。
    long_c1 = ("雨落在瓦上。" * 40) + (
        "我把感情压在心里，像守着一件圣物。她越克制，越显得虔敬。")
    conn.execute("UPDATE book_chunks SET content=?, char_count=? WHERE id='c1'",
                 (long_c1, len(long_c1)))
    conn.commit()
    conn.close()


def _vec(*values: float) -> bytes:
    from array import array
    return array("f", values).tobytes()


def test_endpoint(tmp: Path) -> None:
    providers = tmp / "providers.json"
    store = ProviderStore(providers)
    provider = store.upsert_provider({
        "name": "fake", "base_url": "https://embed.example/v1",
        "api_key": "k",
        "models": [{"id": "fake-embed", "kind": "embedding"}]})
    store.set_slot("embedding", provider["id"], "fake-embed")
    cfg = SimpleNamespace(project_dir=tmp, providers_path=providers,
                          embedding_api_key="", embedding_base_url="",
                          embedding_model="")
    ep = embed_mod.resolve_endpoint(cfg)
    check("端点：控制台绑定", (ep.base_url, ep.model, ep.key),
          ("https://embed.example/v1", "fake-embed", "k"))
    store.set_slot("embedding", "")
    cfg.embedding_api_key = "env-key"
    cfg.embedding_base_url = "https://env.example/v1"
    cfg.embedding_model = "env-embed"
    ep = embed_mod.resolve_endpoint(cfg)
    check("端点：退回环境字段", (ep.base_url, ep.model, ep.source),
          ("https://env.example/v1", "env-embed", "env"))
    cfg.embedding_api_key = ""
    raises("端点：没绑定就报错", embed_mod.EmbeddingError,
           lambda: embed_mod.resolve_endpoint(cfg))

    text = "开头" + "中" * 3000 + "结尾"
    clipped = embed_mod.clip_for_embedding(text)
    check("裁剪：不超上限", len(clipped) <= embed_mod.TEXT_LIMIT, True)
    check("裁剪：留头尾", (clipped.startswith("开头"),
                           clipped.endswith("结尾")), (True, True))
    vec = embed_mod.normalize([3.0, 4.0])
    check("归一化", [round(x, 4) for x in vec], [0.6, 0.8])


def test_store(tmp: Path) -> None:
    path = tmp / "data" / "e.db"
    check("路径：默认落 data/embeddings.db",
          resolve_embedding_db_path(None, root=tmp),
          tmp / "data" / "embeddings.db")
    db = EmbeddingDB(path)
    model = "fake"
    db.upsert_many(model, [
        ("c1", content_hash("甲"), "b1", 1, 2, _vec(1.0, 0.0)),
        ("c2", content_hash("乙"), "b1", 2, 2, _vec(0.0, 1.0)),
        ("c3", content_hash("丙"), "b2", 3, 2, _vec(0.5, 0.5)),
    ])
    check("向量：指纹", db.fingerprints(model),
          {"c1": content_hash("甲"), "c2": content_hash("乙"),
           "c3": content_hash("丙")})
    check("向量：按书过滤", [r["chunk_id"] for r in db.vectors(model, ["b1"])],
          ["c1", "c2"])
    check("向量：统计", db.stats(model),
          {"chunks": 3, "books": 2, "model": model})
    db.delete_model(model)
    check("向量：清模型", db.stats(model)["chunks"], 0)


def test_rank() -> None:
    rows = [
        {"chunk_id": "a", "dim": 2, "vec": _vec(1.0, 0.0)},
        {"chunk_id": "b", "dim": 2, "vec": _vec(0.0, 1.0)},
        {"chunk_id": "c", "dim": 2, "vec": _vec(-1.0, 0.0)},
        {"chunk_id": "d", "dim": 3, "vec": _vec(1.0, 1.0, 1.0)},
    ]
    hits = semantic.rank(rows, [1.0, 0.0], limit=4)
    check("排序：余弦高于阈值", [cid for cid, _ in hits], ["a"])
    check("排序：阈值过滤", all(score >= semantic.MIN_SCORE for _, score in hits),
          True)
    check("排序：丢弃维度不符", [cid for cid, _ in hits], ["a"])
    check("排序：limit", len(semantic.rank(rows, [1.0, 0.0], limit=1)), 1)


def test_search(tmp: Path) -> None:
    books = tmp / "data" / "books.db"
    emb = tmp / "data" / "embeddings.db"
    _books_db(books)
    db = EmbeddingDB(emb)
    model = "fake-embed"
    db.upsert_many(model, [
        ("c1", content_hash("甲"), "b1", 20, 2, _vec(1.0, 0.0)),
        ("c2", content_hash("乙"), "b1", 21, 2, _vec(0.95, 0.31)),
        ("c3", content_hash("丙"), "b2", 38, 2, _vec(0.8, 0.6)),
        ("gone", content_hash("丁"), "b9", 1, 2, _vec(1.0, 0.0)),
    ])
    cfg = SimpleNamespace(project_dir=tmp)
    real_resolve, real_one = (embed_mod.resolve_endpoint, embed_mod.embed_one)
    embed_mod.resolve_endpoint = lambda _cfg: ResolvedEndpoint(
        key="k", base_url="https://x", model=model, source="console")

    async def fake_one(_cfg, _text, **_kw):
        return [1.0, 0.0]

    embed_mod.embed_one = fake_one
    try:
        result = asyncio.run(semantic.search(
            cfg, "克制的爱", limit=2, books_db=books, embedding_db=emb))
        check("检索：来源仍是书库", result["source"], "library")
        check("检索：召回方式是语义", result["recall"], "semantic")
        check("检索：有卡", result["count"], 2)
        check("检索：按分数降序",
              [c["score"] for c in result["cards"]],
              sorted([c["score"] for c in result["cards"]], reverse=True))
        check("检索：孤儿向量被跳过",
              [c["chunk_id"] for c in result["cards"]], ["c1", "c2"])
        check("检索：卡带出处", result["cards"][0]["book_title"], "窄门")
        check("检索：片段围绕命中词",
              [c["chunk_id"] for c in result["cards"]
               if "克制" in str(c.get("excerpt") or "")], ["c1", "c2"])
        check("检索：回传命中词",
              result["cards"][0].get("matched_terms"), ["克制"])
        limited = asyncio.run(semantic.search(
            cfg, "克制的爱", limit=3, books_db=books, embedding_db=emb))
        check("检索：每本最多两条",
              [c["book_title"] for c in limited["cards"]],
              ["窄门", "窄门", "茶花女"])

        embed_mod.resolve_endpoint = lambda _cfg: ResolvedEndpoint(
            key="k", base_url="https://x", model="other", source="console")
        mismatch = asyncio.run(semantic.search(
            cfg, "克制的爱", books_db=books, embedding_db=emb))
        check("检索：模型不匹配不凑数", mismatch["count"], 0)
        check("检索：模型不匹配有说明",
              "索引里没有 other" in mismatch["note"], True)
    finally:
        embed_mod.resolve_endpoint, embed_mod.embed_one = real_resolve, real_one
    check("检索：没有索引时给建库指引",
          "build_embedding_index" in asyncio.run(semantic.search(
              cfg, "x", books_db=books,
              embedding_db=tmp / "data" / "none.db"))["note"], True)


def test_service_tool(tmp: Path) -> None:
    project = tmp / "svc"
    project.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "prompts", project / "prompts",
                    dirs_exist_ok=True)
    books = project / "data" / "books.db"
    emb = project / "data" / "embeddings.db"
    _books_db(books)
    db = EmbeddingDB(emb)
    model = "fake-embed"
    db.upsert_many(model, [
        ("c1", content_hash("甲"), "b1", 20, 2, _vec(1.0, 0.0)),
    ])
    cfg = SimpleNamespace(project_dir=project, books_db_path=books)
    svc = WritingService(cfg, db=WritingDB(project / "w.db"))
    work_id = svc.create_work("窄门来信")
    ctx = {"work_id": work_id, "chapter_index": 0}
    reg = build_writing_registry(ToolRegistry(), svc, ctx)

    real_resolve, real_one = (embed_mod.resolve_endpoint, embed_mod.embed_one)
    embed_mod.resolve_endpoint = lambda _cfg: ResolvedEndpoint(
        key="k", base_url="https://x", model=model, source="console")

    async def fake_one(_cfg, _text, **_kw):
        return [1.0, 0.0]

    embed_mod.embed_one = fake_one
    try:
        result = json.loads(asyncio.run(reg.execute_async(
            "writing_research", json.dumps({
                "query": "克制的爱", "source": "semantic"}))))
        check("工具：semantic 可调", result.get("ok"), True)
        check("工具：带 recall", result.get("recall"), "semantic")
        check("工具：带候选", result.get("count"), 1)
        saved = json.loads(asyncio.run(reg.execute_async(
            "writing_research", json.dumps({
                "query": "克制的爱", "source": "semantic", "save": True}))))
        check("工具：semantic 不落网络素材", saved.get("ok"), False)
    finally:
        embed_mod.resolve_endpoint, embed_mod.embed_one = real_resolve, real_one

    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        test_endpoint(tmp)
        test_store(tmp)
        test_rank()
        test_search(tmp)
        test_service_tool(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILED:
        print(f"**{FAILED} 项失败**")
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
