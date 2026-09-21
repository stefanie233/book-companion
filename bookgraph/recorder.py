"""Recorder — write Explorer results to graph.db.

Pure SQL, no LLM calls. Handles name→node_id resolution,
dedup via ON CONFLICT, and merge candidate persistence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from storage.graph_db import GraphDB

log = logging.getLogger("bookgraph")


@dataclass
class RecordStats:
    """Stats from recording one batch."""
    new_nodes: int = 0
    new_observations: int = 0
    new_edges: int = 0
    new_aliases: int = 0
    merge_candidates: int = 0
    new_quotes: int = 0
    errors: int = 0


def record_result(
    db: GraphDB,
    book_id: str,
    chapter_node_ids: dict[int, str],
    chunk_ids: list[str],
    result: dict,
) -> RecordStats:
    """Write Explorer result to graph.db. Returns stats.

    Args:
        chapter_node_ids: {chapter_index: node_id} for chapter nodes
        chunk_ids: list of chunk_ids in this batch (for marking done)
        result: parsed JSON from Explorer
    """
    stats = RecordStats()

    # name → node_id cache (seeded with chapter nodes)
    # Chapter nodes use their title as canonical_name
    name_cache: dict[str, str] = {}
    # We can't reverse chapter_index → title here, so chapter nodes
    # are looked up by name via db.name_to_node_id when needed.

    # ── Insert new nodes ──────────────────────────────────────────────────
    for nn in result.get("new_nodes", []):
        name = (nn.get("canonical_name") or "").strip()
        kind = (nn.get("kind") or "unknown").strip()
        if not name:
            continue
        desc = nn.get("description")
        first_chunk = nn.get("first_appearance_chunk_id")
        # Deep fields moved to new_observations (event model). upsert_node
        # still accepts them for backward compat but Recorder no longer passes.
        node_id = db.upsert_node(book_id, name, kind, desc, first_chunk)
        name_cache[name] = node_id
        stats.new_nodes += 1

    # ── Insert observations (event model) ─────────────────────────────────
    for ob in result.get("new_observations", []):
        node_name = (ob.get("node_name") or "").strip()
        field = (ob.get("field") or "").strip()
        value = (ob.get("value") or "").strip()
        if not node_name or not field or not value:
            continue
        node_id = name_cache.get(node_name) or db.name_to_node_id(book_id, node_name)
        if not node_id:
            stats.errors += 1
            log.debug("Observation unresolved: %s.%s", node_name, field)
            continue
        event_type = (ob.get("event_type") or "perspective").strip()
        subject = ob.get("subject")
        evidence = ob.get("evidence_chunk_id")
        # Resolve anchor chapter from evidence chunk (internal method)
        anchor_chapter = db._chapter_of_chunk(evidence) if evidence else None
        obs_tags = [t.strip() for t in (ob.get("tags") or []) if t and t.strip()]
        for t in obs_tags:
            db.register_tag(book_id, t, event_type)
        db.add_observation(
            book_id, node_id, field, value,
            event_type=event_type, subject=subject,
            anchor_chapter=anchor_chapter, evidence=evidence, tags=obs_tags,
        )
        stats.new_observations += 1

    # ── Insert aliases ────────────────────────────────────────────────────
    for al in result.get("new_aliases", []):
        alias = (al.get("alias") or "").strip()
        canon = (al.get("canonical_name") or "").strip()
        if not alias or not canon:
            continue
        node_id = name_cache.get(canon) or db.name_to_node_id(book_id, canon)
        if node_id:
            db.add_alias(book_id, alias, node_id)
            name_cache[alias] = node_id
            stats.new_aliases += 1

    # ── Insert edges ──────────────────────────────────────────────────────
    for ed in result.get("new_edges", []):
        src = (ed.get("source") or "").strip()
        tgt = (ed.get("target") or "").strip()
        kind = (ed.get("kind") or "mentions").strip()
        chunk_id = ed.get("chunk_id")
        event_type = (ed.get("event_type") or "fact").strip()
        subject = ed.get("subject")
        edge_tags = [t.strip() for t in (ed.get("tags") or []) if t and t.strip()]
        if not src or not tgt:
            continue
        src_id = name_cache.get(src) or db.name_to_node_id(book_id, src)
        tgt_id = name_cache.get(tgt) or db.name_to_node_id(book_id, tgt)
        if not src_id or not tgt_id:
            stats.errors += 1
            log.debug("Edge unresolved: %s --%s--> %s", src, kind, tgt)
            continue
        for t in edge_tags:
            db.register_tag(book_id, t, event_type)
        db.add_edge(book_id, src_id, tgt_id, kind, chunk_id,
                    event_type=event_type, subject=subject, tags=edge_tags)
        stats.new_edges += 1

    # ── Insert merge candidates ───────────────────────────────────────────
    for mc in result.get("merge_candidates", []):
        name_a = mc.get("name_a", "")
        name_b = mc.get("name_b", "")
        try:
            confidence = float(mc.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = mc.get("reason", "")
        node_id_a = name_cache.get(name_a) or db.name_to_node_id(book_id, name_a)
        db.add_merge_candidate(book_id, name_a, name_b, confidence, reason, node_id_a)
        stats.merge_candidates += 1

    # ── Insert quotes ────────────────────────────────────────────────────
    first_chunk = chunk_ids[0] if chunk_ids else ""
    batch_id = f"batch-{first_chunk[:12]}" if first_chunk else ""
    for q in result.get("quotes", []):
        char_name = (q.get("character_name") or "").strip()
        quote_text = (q.get("quote_text") or "").strip()
        scene = (q.get("scene_context") or "").strip()
        related_names = q.get("related_nodes") or []
        q_chunk = first_chunk
        if not char_name or not quote_text:
            continue
        # Resolve related node names → node_ids (same resolver as edges)
        related_ids: list[str] = []
        for rname in related_names:
            rname_clean = (rname or "").strip()
            if not rname_clean:
                continue
            nid = name_cache.get(rname_clean) or db.name_to_node_id(book_id, rname_clean)
            if nid:
                related_ids.append(nid)
        if db.add_quote(book_id, char_name, quote_text, scene,
                        q_chunk, batch_id, related_ids):
            stats.new_quotes += 1

    # ── Mark chunks done ──────────────────────────────────────────────────
    node_count = db.total_node_count(book_id)
    for chunk_id in chunk_ids:
        db.mark_chunk_done(chunk_id, node_count)

    db.get_conn().commit()
    return stats
