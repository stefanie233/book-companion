"""用例加载与临时 WritingDB 播种。零 LLM。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CASE_DIR = Path(__file__).with_name("cases")
ROOT = Path(__file__).resolve().parents[1]


def _expand(case: dict[str, Any]) -> dict[str, Any]:
    for prior in case.get("prior") or []:
        name = prior.pop("content_file", "")
        if name:
            prior["content"] = (ROOT / "writebench" / name).read_text(
                encoding="utf-8").strip()
    return case


def load_cases(split: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(CASE_DIR.glob("*.json")):
        case = _expand(json.loads(path.read_text(encoding="utf-8")))
        if split and case.get("split") != split:
            continue
        out.append(case)
    return out


def load_case(case_id: str) -> dict[str, Any]:
    for case in load_cases():
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


def seed_db(case: dict[str, Any], db_path: Path):
    """按用例建一个干净的 WritingDB：作品、正文、合同、事实、承诺。"""
    from storage.writing_db import WritingDB

    if db_path.exists():
        db_path.unlink()
    db = WritingDB(db_path)
    work = case["work"]
    work_id = db.create_work(
        work["title"], genre=work.get("genre", ""),
        premise=work.get("premise", ""),
        narrative_pov=work.get("narrative_pov", ""),
        target_chars=int(work.get("target_chars") or 0),
        style_profile=work.get("style_profile") or {})
    conn = db.get_conn()
    for prior in case.get("prior") or []:
        chapter_id = db.add_chapter(
            work_id, int(prior["index"]), title=prior.get("title", ""))
        db.set_contract(chapter_id, purpose=prior.get("summary", ""),
                        ending_state=prior.get("ending_state", ""))
        db.save_draft(work_id, chapter_id, prior["content"], kind="save")
        if prior.get("summary"):
            conn.execute(
                "INSERT INTO chapter_summaries (chapter_id, summary, time_created)"
                " VALUES (?,?,?)", (chapter_id, prior["summary"], 0))
    target = case["chapter"]
    chapter_id = db.add_chapter(
        work_id, int(target["index"]), title=target.get("title", ""))
    contract = dict(case.get("contract") or {})
    db.set_contract(
        chapter_id,
        purpose=contract.get("purpose", ""),
        exclusive_event=contract.get("exclusive_event", ""),
        ending_state=contract.get("ending_state", ""),
        next_entry_state=contract.get("next_entry_state", ""),
        story_time=contract.get("story_time", ""),
        target_chars=int(contract.get("target_chars") or 0),
        must_hit_now=list(contract.get("must_hit_now") or []),
        must_preserve=list(contract.get("must_preserve") or []),
        forbidden_cross=list(contract.get("forbidden_cross") or []))
    _seed_ledger(conn, work_id, case)
    conn.commit()
    return db, work_id, chapter_id


def _seed_ledger(conn, work_id: str, case: dict[str, Any]) -> None:
    for fact in case.get("facts") or []:
        conn.execute(
            "INSERT INTO facts (id, work_id, subject_type, subject_ref, field,"
            " value, unknown_reason, valid_from_chapter, valid_until_chapter,"
            " source_chapter, source_kind, evidence_text, confidence,"
            " time_created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"ft-{fact['subject_ref']}-{fact['field']}-{fact['from']}",
             work_id, fact.get("subject_type", "character"),
             fact["subject_ref"], fact["field"], fact.get("value"),
             fact.get("unknown_reason"), int(fact["from"]),
             fact.get("until"), int(fact.get("source", fact["from"])),
             fact.get("source_kind", "author"),
             fact.get("evidence_text", ""), 1.0, 0))
    for promise in case.get("promises") or []:
        conn.execute(
            "INSERT INTO promises (id, work_id, title, summary, kind, weight,"
            " status, planned_plant_chapter, planted_chapter,"
            " earliest_reveal_chapter, planned_payoff_chapter,"
            " time_created, time_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (promise["id"], work_id, promise["title"],
             promise.get("summary", ""), promise.get("kind", "foreshadow"),
             int(promise.get("weight", 5)), promise.get("status", "planted"),
             promise.get("planned_plant_chapter"),
             promise.get("planted_chapter"),
             promise.get("earliest_reveal_chapter"),
             promise.get("planned_payoff_chapter"), 0, 0))
