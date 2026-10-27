"""实验版上下文装配：与生产 ``WritingService.assemble`` 同构，
只多两个可切换入口（外部参考素材、作者声音），用于 A/B。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from services.writing_service import _contract_text, _facts_text
from writegraph import budget as budget_mod
from writegraph import card as card_mod
from writegraph import facts as facts_mod
from writegraph import lens as lens_mod


def assemble(service, work_id: str, chapter_index: int, *,
             reference_text: str = "",
             style_ref: Mapping[str, Any] | None = None,
             focus_refs: Sequence[str] | None = None,
             budget: int | None = None) -> dict[str, Any]:
    db = service.db
    work = db.get_work(work_id) or {}
    chapter = service.chapter(work_id, chapter_index)
    contract = db.get_contract(chapter["id"]) or {}
    prev = db.get_chapter(work_id, chapter_index - 1)
    prev_tail = (prev or {}).get("content", "")[-700:]
    prev_contract = (db.get_contract(prev["id"]) if prev else None)
    prev_ending = (prev_contract or {}).get("ending_state", "")
    position = chapter_index - 1

    facts_rows = db.get_facts(work_id, up_to_chapter=position)
    current = facts_mod.current(facts_rows, position)
    promise_rows = db.get_promises(work_id)
    summaries = db.recent_summaries(work_id, chapter_index)

    card = card_mod.build_card(
        work=work, chapter=chapter, contract=contract,
        facts_rows=facts_rows, promise_rows=promise_rows,
        summaries=summaries, focus_refs=focus_refs,
        prev_ending_state=prev_ending, position=position)

    query = lens_mod.build_query(contract=contract, prev_tail=prev_tail,
                                 entities=focus_refs or ())
    raw: list[dict[str, Any]] = []
    fts = lens_mod.fts_query(query)
    if fts:
        raw = db.search_chunks(work_id, fts,
                               before_chapter=chapter_index - 3, limit=60)
    ranked = lens_mod.rank(raw, chapter=chapter_index,
                           entity_names=tuple(focus_refs or ()))

    blocks = {
        "chapter_contract": _contract_text(contract, chapter_index),
        "facts_current": _facts_text(current),
        "prev_tail": prev_tail,
        "context_card": card["text"],
        "reference_material": reference_text or lens_mod.render_material(ranked),
    }
    style_info = None
    if style_ref:
        voice_text, style_info = service._build_author_voice(
            work, contract, style_ref)
        if voice_text:
            blocks["author_voice"] = voice_text
    compiled = budget_mod.compile_blocks(
        blocks, budget=budget or budget_mod.DEFAULT_TOTAL_BUDGET)
    return {"chapter_index": chapter_index, "text": compiled["text"],
            "tokens": compiled["tokens"], "manifest": compiled["manifest"],
            "card": card, "references": ranked[:10], "style": style_info}
