"""自我说明层的本机状态探针（决策 154）。

探针只读本地配置与数据库：不联网、不写库、不 raise。每个探针返回
``{"state": ..., "facts": {...}, "remedy": "..."}``。``state`` 必须来自
各自的枚举；``unknown`` 表示探针本身失败，模型不得据此推断原因。

脱敏纪律：facts 只报能力名、是否就绪、模型名；不报 key、base_url、
绝对路径。``remedy`` 是给用户看的下一步，不是内部实现说明。
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

log = logging.getLogger("self_knowledge")

ProbeContext = dict[str, Any]


def _env_endpoint(slot: str, config: Any) -> SimpleNamespace:
    """console 未注入时的 .env 视图；与 ProviderStore.resolve 的兜底同形。"""
    from provider_store import (SLOT_ASR, SLOT_EMBEDDING, SLOT_MAIN_LLM,
                                SLOT_TTS)
    if slot == SLOT_EMBEDDING:
        return SimpleNamespace(key=getattr(config, "embedding_api_key", ""),
                               base_url=getattr(config, "embedding_base_url", ""),
                               model=getattr(config, "embedding_model", ""))
    if slot == SLOT_TTS:
        return SimpleNamespace(key=getattr(config, "tts_api_key", ""),
                               base_url=getattr(config, "tts_api_base", ""),
                               model=getattr(config, "tts_model", ""))
    if slot == SLOT_ASR:
        return SimpleNamespace(key=getattr(config, "asr_api_key", ""),
                               base_url=getattr(config, "asr_api_base", ""),
                               model="")
    return SimpleNamespace(key=getattr(config, "api_key", ""),
                           base_url=getattr(config, "api_base", ""),
                           model=getattr(config, "model", ""))


def _endpoint(store: Any, slot: str, config: Any) -> Any:
    if store is not None:
        try:
            return store.resolve(slot, config)
        except Exception as exc:                     # noqa: BLE001
            log.warning("self provider probe resolve %s failed: %s", slot, exc)
    return _env_endpoint(slot, config)


def _has(*values: Any) -> bool:
    return all(str(v or "").strip() for v in values)

def probe_provider(ctx: ProbeContext) -> dict[str, Any]:
    """哪些能力现在是关着的。只报绑定状态与模型名。"""
    from provider_store import (SLOT_ASR, SLOT_EMBEDDING, SLOT_MAIN_LLM,
                                SLOT_TTS, get_active_store)
    config = ctx.get("config")
    if config is None:
        return {"state": "unknown", "facts": {}, "remedy": ""}
    store = get_active_store(config)
    eps = {slot: _endpoint(store, slot, config) for slot in
           (SLOT_MAIN_LLM, SLOT_EMBEDDING, SLOT_TTS, SLOT_ASR)}
    chat = _has(eps[SLOT_MAIN_LLM].key, eps[SLOT_MAIN_LLM].base_url,
                eps[SLOT_MAIN_LLM].model)
    embedding = _has(eps[SLOT_EMBEDDING].key, eps[SLOT_EMBEDDING].base_url,
                     eps[SLOT_EMBEDDING].model)
    tts = _has(eps[SLOT_TTS].key, eps[SLOT_TTS].base_url)
    asr = _has(eps[SLOT_ASR].key, eps[SLOT_ASR].base_url,
               eps[SLOT_ASR].model)
    tavily = bool(str(getattr(config, "search_api_key", "") or "").strip())
    native = "deepseek" in str(eps[SLOT_MAIN_LLM].base_url or "").lower()
    facts = {
        "chat": {"ready": chat, "model": eps[SLOT_MAIN_LLM].model or ""},
        "embedding": {"ready": embedding,
                      "model": eps[SLOT_EMBEDDING].model or ""},
        "tts": {"ready": tts, "model": eps[SLOT_TTS].model or ""},
        "asr": {"ready": asr, "model": eps[SLOT_ASR].model or ""},
        "web": {"ready": tavily or native,
                "mode": "tavily" if tavily else
                        ("native" if native else "none")},
    }
    missing = [k for k in ("chat", "embedding", "tts", "asr", "web")
               if not facts[k]["ready"]]
    if not chat:
        state = "unconfigured"
    elif missing:
        state = "partial"
    else:
        state = "ok"
    remedy = ("在控制台「模型绑定」里给缺的能力选一条连接。"
              if missing else "")
    return {"state": state, "facts": facts, "missing": missing,
            "remedy": remedy}


def probe_semantic(ctx: ProbeContext) -> dict[str, Any]:
    """embedding 绑定 + 向量索引，四种状态分开。"""
    config = ctx.get("config")
    if config is None:
        return {"state": "unknown", "facts": {}, "remedy": ""}
    from services import embedding_service as embed_mod
    from storage.embedding_db import EmbeddingDB, resolve_embedding_db_path
    try:
        ep = embed_mod.resolve_endpoint(config)
    except embed_mod.EmbeddingError as exc:
        return {"state": "unconfigured", "facts": {}, "remedy": str(exc)}
    path = Path(resolve_embedding_db_path(config))
    if not path.exists():
        return {"state": "no_index", "facts": {"model": ep.model},
                "remedy": "还没有语义索引：先跑 "
                          "scripts/build_embedding_index.py。"}
    db = None
    try:
        db = EmbeddingDB(path)
        models = [str(r[0]) for r in db.get_conn().execute(
            "SELECT DISTINCT model FROM chunk_vectors").fetchall()]
        stats = db.stats(ep.model)
    finally:
        if db is not None:
            db.close()
    facts = {"model": ep.model, "chunks": stats["chunks"],
             "books": stats["books"], "index_models": models[:3]}
    if stats["chunks"] > 0:
        return {"state": "ok", "facts": facts, "remedy": ""}
    if models:
        return {"state": "model_mismatch", "facts": facts,
                "remedy": "索引是用别的模型建的；换绑模型后要重跑 "
                          "scripts/build_embedding_index.py。"}
    return {"state": "no_index", "facts": facts,
            "remedy": "索引库是空的：跑 scripts/build_embedding_index.py。"}

def probe_authors(ctx: ProbeContext) -> dict[str, Any]:
    """作者档案的状态。``no_dated`` 与 ``all_ahead`` 必须分开：

    前者是采集没抽到年份，后者是语料全在读者还没读到的章节——后者不是故障。
    """
    config = ctx.get("config")
    db = ctx.get("db")
    book_id = str(ctx.get("book_id") or "")
    if not book_id:
        return {"state": "no_book", "facts": {},
                "remedy": "先打开一本书；作者档案跟着书走。"}
    from authorgraph.gate import gate_state
    from authorgraph.scope import book_identity
    from storage.authors_db import AuthorsDB, resolve_authors_db_path
    ident = (book_identity(db, book_id) if db is not None
             else {"stable_book_id": book_id, "author": ""})
    stable = str(ident.get("stable_book_id") or book_id)
    path = Path(resolve_authors_db_path(config))
    if not path.exists():
        return {"state": "no_author",
                "facts": {"author": ident.get("author", "")},
                "remedy": "这本书还没有作者档案：在作者页先跑「采集序跋」。"}
    adb = None
    try:
        adb = AuthorsDB(path)
        authors = adb.authors_of_book(stable)
        if not authors:
            return {"state": "no_author",
                    "facts": {"author": ident.get("author", "")},
                    "remedy": "这本书还没建作者档案：在作者页先跑「采集序跋」。"}
        row = next((a for a in authors if a.get("role") == "author"),
                   authors[0])
        author_id = str(row.get("author_id") or "")
        stats = adb.stats(author_id)
        vcov = adb.voice_coverage(author_id)
        reader_chapter = int(ctx.get("reading_chapter")
                             or ctx.get("chapter_index") or 0)
        gate = gate_state(adb, author_id, stable, reader_chapter)
        facts = {
            "author": row.get("canonical_name") or ident.get("author") or "",
            "records": stats.get("records", 0),
            "corpus": stats.get("corpus", 0),
            "undated": stats.get("undated", 0),
            "evidence": stats.get("evidence", 0),
            "voice_samples": vcov.get("voice_corpus", 0),
            "scenes": vcov.get("scenes", 0),
            "gate": gate.get("reason"),
            "ahead": gate.get("ahead", 0),
        }
        if facts["records"] == 0 and facts["corpus"] == 0:
            return {"state": "no_harvest", "facts": facts,
                    "remedy": "采集还没跑，或没从序跋里抽到可用内容；"
                              "在作者页重跑「采集序跋」。"}
        if gate.get("reason") == "unreadable":
            return {"state": "unknown", "facts": facts,
                    "remedy": "作者库读不出来；这不是缺数据。"}
        if gate.get("reason") == "no_dated":
            return {"state": "no_dated", "facts": facts,
                    "remedy": "档案里有内容，但没有一条带年份；时间轴门"
                              "无法放行。补采网页资料通常能补上年份。"}
        if gate.get("reason") == "all_ahead":
            return {"state": "all_ahead", "facts": facts,
                    "remedy": f"不是没有数据：{facts['ahead']} 条带年份的"
                              "语料都在你还没读到的章节；读过去就会出现。"}
        return {"state": "ok", "facts": facts, "remedy": ""}
    finally:
        if adb is not None:
            adb.close()


def probe_vcs(ctx: ProbeContext) -> dict[str, Any]:
    """写作版本现场。只读 vcs_status，不 commit / stash / checkout。"""
    wctx = ctx.get("writing_ctx") or {}
    work_id = str(wctx.get("work_id") or "")
    svc = ctx.get("writing_svc")
    if not work_id:
        return {"state": "no_work", "facts": {},
                "remedy": "写作页还没绑定作品；先选一本或新建作品。"}
    if svc is None:
        return {"state": "unavailable", "facts": {"work_id": work_id},
                "remedy": "当前会话拿不到写作现场；在写作页提问可看到版本状态。"}
    status = svc.vcs_status(work_id)
    counts = status.get("chapter_counts") or {}
    fcounts = status.get("file_counts") or {}
    uncommitted = (sum(int(v or 0) for v in counts.values())
                   + sum(int(v or 0) for v in fcounts.values())
                   + (1 if status.get("ledger_dirty") else 0))
    head = status.get("head") or {}
    facts = {
        "work_id": work_id,
        "branch": status.get("branch") or "main",
        "uncommitted": uncommitted,
        "branches": len(status.get("branches") or []),
        "stashes": int(status.get("stash_count") or 0),
        "last_commit": head.get("time_created") or 0,
        "last_message": head.get("message") or "",
    }
    clean = bool(status.get("clean"))
    return {"state": "clean" if clean else "dirty", "facts": facts,
            "remedy": "" if clean else
            "版本控制栏会把未存档改动标成已修改/未跟踪；存档一次就留一版。"}


PROBES: dict[str, Callable[[ProbeContext], dict[str, Any]]] = {
    "provider": probe_provider,
    "semantic": probe_semantic,
    "authors": probe_authors,
    "vcs": probe_vcs,
}


def run_probe(key: str, ctx: ProbeContext) -> dict[str, Any]:
    """跑一个探针；失败只报类型名，不带异常正文（可能含路径/key）。"""
    fn = PROBES.get(str(key or ""))
    if fn is None:
        return {"state": "unknown", "facts": {}, "remedy": ""}
    try:
        return fn(ctx or {})
    except Exception as exc:                         # noqa: BLE001
        log.warning("self probe %s failed: %s", key, exc, exc_info=True)
        return {"state": "unknown", "facts": {},
                "error": type(exc).__name__}


