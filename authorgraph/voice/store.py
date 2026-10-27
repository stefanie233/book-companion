"""把声音 run 落进作者台账 —— **唯一写入口**，与 ``store_harvest`` 平级。

三条纪律：

1. **只写声音账。** 这个模块**没有**任何 ``add_record`` 调用，也不许加 ——
   声音管线的产物是 ``author_corpus`` / ``author_voice_traits``，一旦写
   ``author_records``，小说情节就会混进作者生平（不可逆）。
2. **语料先落、trait 后落。** 模型引用的是 ``corpus_id``，所以样本必须先
   有主键；这也让「证据指回哪一段」不依赖模型复述原文。
3. **不删旧 trait。** 新 revision 写完后旧行标 ``superseded``（库层做），
   证据链要能追溯。
"""

from __future__ import annotations

import logging
from typing import Any

from authorgraph.voice.vocab import norm_scene
from storage.authors_db import AuthorsDB

log = logging.getLogger("authorgraph")


def store_samples(adb: AuthorsDB, author_id: str, *, stable_book_id: str,
                  book_id: str = "", samples: list[dict[str, Any]],
                  speaker: str = "author",
                  narrative_layer: str = "narration"
                  ) -> dict[str, Any]:
    """落样本语料。返回 ``{corpus_ids: {chunk_id: corpus_id}, added, reused}``。

    每个样本一条语料，``excerpt`` 是**逐字窗口**（不是摘要）——
    写作透镜要用它当例证，重跑要靠它重新定位块。
    """
    src = adb.book_source_id(stable_book_id)
    ids: dict[str, int] = {}
    added = reused = 0
    conn = adb.get_conn()
    for s in samples:
        text = s.get("text") or ""
        chunk_id = s.get("chunk_id") or ""
        # 先看这一条在不在：``add_corpus_v2`` 幂等，但只返回 id，分不出
        # 「新写」与「早就有了」—— 台账要的是增量，不是累计。
        existed = conn.execute(
            "SELECT id FROM author_corpus WHERE author_id=?"
            " AND stable_book_id=? AND IFNULL(chunk_id,'')=? AND excerpt=?"
            " LIMIT 1", (author_id, stable_book_id, chunk_id, text)).fetchone()
        cid = adb.add_corpus_v2(
            author_id, text,
            source_id=src, stable_book_id=stable_book_id,
            book_id=book_id or None, chunk_id=chunk_id or None,
            chapter_index=s.get("chapter_index"),
            chunk_index=s.get("chunk_index"),
            role="author", speaker=speaker,
            # 样本可以自带层（对话窗口标 dialogue）；没有就用默认。
            narrative_layer=s.get("narrative_layer") or narrative_layer,
            text_kind="body", scene_type=norm_scene(s.get("scene_hint")),
            source="corpus")
        if cid is None:
            continue
        ids[chunk_id or str(s.get("key") or cid)] = int(cid)
        if existed:
            reused += 1
        else:
            added += 1
    return {"corpus_ids": ids, "added": added, "reused": reused}


def apply_sample_corrections(adb: AuthorsDB, corrections: list[dict[str, Any]],
                             corpus_ids: dict[str, int]) -> int:
    """把模型对样本场景/视角/时期的纠正回写到语料行。

    纠正按 ``corpus_id`` 定位；找不到的静默跳过（它可能是别的批的样本）。
    """
    by_id = {v: k for k, v in corpus_ids.items()}
    written = 0
    for c in corrections:
        cid = int(c.get("corpus_id") or 0)
        if cid not in by_id:
            continue
        adb.update_corpus_meta(
            cid, scene_type=c.get("scene_type") or "",
            pov=c.get("pov") or "", period=c.get("period") or "",
            scene_note=c.get("notes") or "")
        written += 1
    return written


def store_voice(adb: AuthorsDB, author_id: str, *,
                traits: list[dict[str, Any]],
                corrections: list[dict[str, Any]],
                corpus_ids: dict[str, int],
                revision: str) -> dict[str, Any]:
    """落 trait + 样本纠正。返回台账（供 manifest / 打印）。"""
    # 同一批样本的 corpus_id 集合 —— trait 的证据只可能落在其中。
    valid = set(corpus_ids.values())
    written = 0
    skipped = 0
    for t in traits:
        ev = tuple(int(c) for c in (t.get("evidence_ids") or [])
                   if int(c) in valid)
        if not ev:
            skipped += 1
            continue
        tid = adb.add_voice_trait(
            author_id, t["dimension"], t["value"], t["statement"],
            voice_domain=t.get("voice_domain") or "unknown",
            strength=float(t.get("strength") or 0.5),
            scope_type=t.get("scope_type") or "work",
            scope_ref=t.get("scope_ref") or "",
            transferable=bool(t.get("transferable", True)),
            confidence=float(t.get("confidence") or 0.3),
            status=t.get("status") or "provisional",
            revision=revision, evidence_ids=ev)
        if tid:
            written += 1
        else:
            skipped += 1
    fixed = apply_sample_corrections(adb, corrections, corpus_ids)
    return {"traits_written": written, "traits_skipped": skipped,
            "samples_corrected": fixed}


__all__ = ["apply_sample_corrections", "store_samples", "store_voice"]
