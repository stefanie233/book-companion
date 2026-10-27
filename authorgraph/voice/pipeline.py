"""声音采集编排 —— 可见性 → 抽样 → 落语料 → 1–2 次小调用 → 合并 → 落 trait。

与事实管线（``authorgraph/sources/pipeline.py``）的两条硬边界：

1. **不写 ``author_records``。** 这里的产物只有语料与文风特征。
2. **不联网。** 声音只从书里来（访谈/演讲留 M5 的 public 入口）。
   事实管线那套搜索/抓页/逐字校验在这里一概没有 —— 不是省事，是
   两种输入本来就不该共用一条调用链（设计稿 08 §一）。

状态机（``author_books.voice_status``）：``none → running → ready/partial``，
失败 ``failed``，按 position 不可采 ``skipped``，内容变了 ``stale``。
**任何失败都不影响图谱提取** —— 它是一条旁路。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import aiohttp

from provider_store import config_for

from authorgraph.voice.extract import (BATCH_SIZE, MAX_CALLS, extract_batch,
                                       split_batches)
from authorgraph.voice.from_extraction import (extraction_run_id,
                                               head_scan_meta, plan_hash,
                                               scout_plan, voice_candidates)
from authorgraph.voice.sample import MAX_SAMPLES, sample_windows
from authorgraph.voice.scope import (content_hash, eligible_chunks,
                                     visibility_for)
from authorgraph.voice.store import store_samples, store_voice
from authorgraph.voice.traits import merge_batches
from authorgraph.voice.vocab import PROMPT_VERSION
from storage.authors_db import AuthorsDB

log = logging.getLogger("authorgraph")

#: 书本语料进 ``author_corpus.narrative_layer``。窗口里对话多就标 dialogue。
_LAYER = "narration"


def _new_manifest(author_id: str, stable: str, book_id: str,
                  title: str = "") -> dict[str, Any]:
    return {
        "author_id": author_id, "stable_book_id": stable, "book_id": book_id,
        "title": title, "status": "", "error_code": "", "cached": False,
        "position": "", "domain": "", "content_hash": "",
        "run_id": "", "revision": "", "scout_plan_hash": "",
        "model": "", "samples": 0, "batches": 0, "calls": 0,
        "corpus_added": 0, "corpus_reused": 0, "traits": 0,
        "active_traits": 0, "scenes": 0, "source_bound": [],
        "conflicts": [], "defects": [], "warnings": [], "failures": [],
        "sample_preview": [],
    }


def _book_link(adb: AuthorsDB, author_id: str,
               stable_book_id: str) -> dict[str, Any] | None:
    """作者与这本书的关系行。优先主作者 —— 译者/编者的 position 不是声音。"""
    rows = [r for r in adb.books_of(author_id)
            if r.get("stable_book_id") == stable_book_id]
    if not rows:
        return None
    rows.sort(key=lambda r: 0 if r.get("role") == "author" else 1)
    return rows[0]


async def harvest_voice(session: aiohttp.ClientSession | None, cfg: Any,
                        books_db: Any, adb: AuthorsDB, *,
                        author_id: str, stable_book_id: str,
                        book_id: str = "", title: str = "",
                        graph_db: Any = None, force: bool = False,
                        max_samples: int = MAX_SAMPLES,
                        dry_run: bool = False,
                        log_cb: Callable[[dict], None] | None = None
                        ) -> dict[str, Any]:
    """跑一本书的声音采集。返回 manifest；**不 raise**。"""
    m = _new_manifest(author_id, stable_book_id, book_id, title)
    vcfg = config_for(cfg, "authorgraph.voice") if cfg is not None else cfg
    model = str(getattr(vcfg, "model", "") or "")
    m["model"] = model
    author = adb.get_author(author_id)
    if not author:
        m["status"] = "failed"
        m["error_code"] = "author_missing"
        return m
    link = _book_link(adb, author_id, stable_book_id)
    if link is None:
        m["status"] = "failed"
        m["error_code"] = "book_not_linked"
        m["warnings"].append("作者与这本书还没建立关系 —— 先跑 run_author_harvest")
        return m
    m["position"] = link.get("position") or "unknown"
    vis = visibility_for(m["position"])
    m["domain"] = vis.domain

    if not vis.body:
        m["status"] = "skipped"
        m["error_code"] = "no_position_voice"
        m["warnings"].append(f"position={m['position']}：{vis.reason}")
        adb.set_voice_status(author_id, stable_book_id, "skipped")
        return m

    plan = scout_plan(graph_db)
    m["scout_plan_hash"] = plan_hash(plan)
    excluded = set((plan.get("excluded_chunk_ids") or []) if plan else [])
    boundaries = (head_scan_meta(graph_db).get("boundaries") or {})
    try:
        chunks, warns = eligible_chunks(books_db, book_id,
                                        boundaries=boundaries,
                                        scout_excluded=excluded)
    except Exception as exc:                            # noqa: BLE001
        m["status"] = "failed"
        m["error_code"] = "no_candidates"
        m["failures"].append(f"读正文失败：{exc}")
        return m
    m["warnings"].extend(warns)
    if not chunks:
        m["status"] = "skipped"
        m["error_code"] = "no_candidates"
        adb.set_voice_status(author_id, stable_book_id, "skipped")
        return m

    h = content_hash(chunks, stable_book_id)
    m["content_hash"] = h
    prior = adb.voice_status_of(author_id, stable_book_id)
    last = adb.last_voice_run(author_id, stable_book_id)
    same_proc = bool(
        last
        and (last.get("prompt_version") or "") == PROMPT_VERSION
        and (last.get("model") or "") == model)
    if (prior.get("voice_status") in ("ready", "partial")
            and prior.get("voice_content_hash") == h
            and same_proc and not force):
        m["status"] = prior["voice_status"]
        m["cached"] = True
        m["warnings"].append("内容 hash 未变 —— 命中已有声音数据，未重复调用")
        return m
    if (prior.get("voice_status") in ("ready", "partial")
            and prior.get("voice_content_hash") == h and not same_proc):
        m["warnings"].append(
            "提示词/模型已变 —— 同一内容也要重采（幂等键含 prompt_version+model）")
    if (prior.get("voice_status") in ("ready", "partial")
            and prior.get("voice_content_hash") not in ("", h)):
        m["warnings"].append("书籍内容已变化：本次重采，旧 trait 会被标 superseded")

    valid = {c.chunk_id for c in chunks}
    cands, cw = voice_candidates(plan, valid_ids=valid,
                                 excluded_ids=excluded)
    m["warnings"].extend(cw)
    samples, sw = sample_windows(chunks, stable_book_id=stable_book_id,
                                 candidates=cands, max_samples=max_samples)
    m["warnings"].extend(sw)
    # **窗口一律 narration。** 对话密集的窗口仍然是作者写下的叙述文字，
    # 用 ``scene_type='dialogue'`` 标记就够；``narrative_layer='dialogue'``
    # 留给「只摘角色对白」的样本（M2 不产）。若按窗口拆层，写作透镜
    # （只取 narration/essay）会把对话场景整批丢掉 —— 实测林奕含笑对话
    # 场景时样本为 0。
    m["samples"] = len(samples)
    m["scenes"] = len({s["scene_hint"] for s in samples})
    if dry_run:
        m["status"] = "dry_run"
        m["sample_preview"] = [
            {"chunk_id": s["chunk_id"], "chapter_index": s["chapter_index"],
             "scene_hint": s["scene_hint"], "start": s["start"],
             "end": s["end"], "chars": len(s["text"]),
             "head": " ".join(s["text"][:80].split())}
            for s in samples]
        return m
    if not samples:
        m["status"] = "failed"
        m["error_code"] = "no_candidates"
        adb.set_voice_status(author_id, stable_book_id, "failed")
        return m

    run_id = adb.start_voice_run(
        author_id, book_id=book_id, stable_book_id=stable_book_id,
        extraction_run_id=extraction_run_id(graph_db, book_id),
        scout_plan_hash=m["scout_plan_hash"],
        sample_ids=[], model=model, prompt_version=PROMPT_VERSION)
    m["run_id"] = run_id
    # 先落 running：进程在「清完旧料、还没写新料」时被杀，下一次不能被
    # 误判成缓存命中（那会让 ready 指向一个空账本）。
    adb.set_voice_status(author_id, stable_book_id, "running")
    # 规则/模型换了就等于「换了尺子」：旧样本是按旧规则选的，留着会和
    # 新样本混成两套账。清掉本书声音语料（序跋/事实不动），再按新规则采。
    reset = force or (last is not None and not same_proc)
    if reset:
        gone = adb.clear_book_voice_corpus(stable_book_id, author_id=author_id)
        if gone.get("corpus"):
            why = "--force" if force else "规则/模型已变"
            m["warnings"].append(
                f"{why}：清掉本书声音语料 {gone['corpus']} 条后重采")

    stored = store_samples(adb, author_id, stable_book_id=stable_book_id,
                           book_id=book_id, samples=samples,
                           speaker=vis.speaker, narrative_layer=_LAYER)
    corpus_ids: dict[str, int] = stored["corpus_ids"]
    m["corpus_added"] = stored["added"]
    m["corpus_reused"] = stored["reused"]
    if not corpus_ids:
        adb.set_voice_status(author_id, stable_book_id, "failed",
                             content_hash=h)
        adb.finish_voice_run(run_id, "failed", error_code="store_failed",
                             manifest=m)
        m["status"] = "failed"
        m["error_code"] = "store_failed"
        return m
    adb.set_voice_run_samples(run_id, list(corpus_ids.values()))

    # corpus_id → 样本（trait 的证据与 revision 都按它算）。
    by_chunk = {s["chunk_id"]: s for s in samples}
    samples_by_id: dict[int, dict[str, Any]] = {}
    for chunk_id, cid in corpus_ids.items():
        s = by_chunk.get(chunk_id)
        if s:
            samples_by_id[int(cid)] = dict(s, corpus_id=int(cid))

    book = {"title": title, "position": m["position"],
            "position_note": link.get("position_note") or "",
            "domain": vis.domain}
    batches: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for batch in split_batches(samples, BATCH_SIZE):
        if m["calls"] >= MAX_CALLS:
            break
        ids = [corpus_ids.get(s["chunk_id"]) for s in batch]
        ids = [int(i) for i in ids if i]
        if not ids:
            continue
        # 提示词里的 corpus_id 就是库里的主键 —— 模型引用它才有意义。
        batch = [dict(s, corpus_id=corpus_ids.get(s["chunk_id"]))
                 for s in batch if corpus_ids.get(s["chunk_id"])]
        m["calls"] += 1
        m["batches"] += 1
        out = await extract_batch(
            session, cfg, author, book, batch,
            samples_by_id={i: samples_by_id[i] for i in ids
                           if i in samples_by_id},
            default_domain=vis.domain, prompt_version=PROMPT_VERSION,
            log_cb=log_cb)
        m["failures"].extend(out.get("defects") or [])
        if out.get("batch") is None:
            continue
        batches.append(out["batch"])
        corrections.extend(out["batch"].get("samples") or [])

    if not batches:
        adb.set_voice_status(author_id, stable_book_id, "failed",
                             content_hash=h)
        adb.finish_voice_run(run_id, "failed", error_code="provider_error",
                             corpus_count=len(corpus_ids), manifest=m)
        m["status"] = "failed"
        m["error_code"] = "provider_error"
        return m

    try:
        merged = merge_batches(
            batches, samples_by_id=samples_by_id, scope_type="work",
            scope_ref=stable_book_id, prompt_version=PROMPT_VERSION,
            model=model)
        m["revision"] = merged["revision"]
        m["conflicts"].extend(merged["conflicts"])
        m["source_bound"] = merged["source_bound"]
        # 模型纠正过的场景以纠正为准（样本 tag 只是启发式）。
        fixed_scene = {int(c.get("corpus_id") or 0): c.get("scene_type")
                       for c in corrections if c.get("scene_type")}
        m["scenes"] = len({fixed_scene.get(int(s["corpus_id"]),
                                           s["scene_hint"])
                           for s in samples_by_id.values()}) or m["scenes"]

        ledger = store_voice(adb, author_id, traits=merged["traits"],
                             corrections=corrections, corpus_ids=corpus_ids,
                             revision=merged["revision"])
        m["traits"] = ledger["traits_written"]
        m["active_traits"] = sum(1 for t in merged["traits"]
                                 if t.get("status") == "active")
    except Exception as exc:                            # noqa: BLE001
        log.warning("Voice merge/store failed: %s", exc, exc_info=True)
        adb.set_voice_status(author_id, stable_book_id, "failed",
                             content_hash=h)
        adb.finish_voice_run(run_id, "failed", error_code="store_failed",
                             corpus_count=len(corpus_ids), manifest=m)
        m["status"] = "failed"
        m["error_code"] = "store_failed"
        m["failures"].append(f"合并/落库失败：{exc}")
        return m

    # 完成度按「本次这本书」算，不按作者全库 —— partial 是书级状态。
    n = len(corpus_ids)
    status = "ready" if (n >= 6 and m["scenes"] >= 3
                         and m["active_traits"] >= 1) else "partial"
    m["status"] = status
    adb.set_voice_status(author_id, stable_book_id, status, content_hash=h)
    adb.finish_voice_run(run_id, status, corpus_count=n, manifest=m)
    return m


__all__ = ["harvest_voice"]
