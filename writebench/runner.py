"""跑一个用例 × 配方 × 重复：装配 → 生成 → 落结果。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from config import Config
from services.writing_service import (LENGTH_PROMPT, MATERIAL_PROMPT,
                                      WritingService)
from writebench import assemble as bench_assemble
from writebench import material, metrics
from writebench.fixtures import seed_db
from writebench.llm import Chat, endpoint

ROOT = Path(__file__).resolve().parents[1]
RUNS = Path(os.environ.get("WB_RUNS", str(ROOT / "data" / "test_writebench_runs")))

PROMPTS = ROOT / "writebench" / "prompts"


async def _distill(chat: Chat, pack: Mapping[str, Any], *,
                   model: str, limit: int = 520, v2: bool = False) -> str:
    cand = material.skeleton(pack, max_chars=2400)
    if not cand:
        return ""
    template = material.DISTILL_SYSTEM_V2 if v2 else material.DISTILL_SYSTEM
    system = template.replace("{limit}", str(limit))
    return await chat.complete(endpoint(model[0], model[1]), system, cand,
                               temperature=0.3, max_tokens=900)


def _prompt_text(name: str, target_chars: int) -> str:
    text = ((ROOT / "prompts" / "writing" / "chapter.md").read_text(
                encoding="utf-8") if name == "production"
            else (PROMPTS / f"{name}.md").read_text(encoding="utf-8"))
    return (text.replace("{target_chars}", str(target_chars))
                .replace("{min_chars}", str(int(target_chars * 0.8))))


async def _chat_complete(chat: Chat, ep, system: str, user: str, *,
                         temperature: float | None, json_mode: bool = False) -> str:
    last = ""
    for attempt in range(3):
        try:
            return await chat.complete(ep, system, user, temperature=temperature,
                                       max_tokens=None, json_mode=json_mode)
        except Exception as exc:                     # noqa: BLE001
            last = str(exc)
            await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError(last)


def _strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return re.sub(r"<think>.*\Z", "", text, flags=re.S).strip()


REVISE_SYSTEM = """## 修订任务

你在修同一章的草稿。只做必要修改：

1. 逐条处理质检单；没有问题的段落不要动。
2. 不新增情节、不改变章末状态、不违反合同。
3. 字数不足时，回到已写场景补可感的细节，不写总结、不重复。
4. 只输出修改后的完整正文，不要解释。
"""


def _quality_issues(case: Mapping[str, Any], text: str,
                    target: int) -> list[str]:
    m = metrics.mechanical(case, text)
    issues: list[str] = []
    for item in m.get("deslop_top") or []:
        if item.get("tier") != "blocking":
            continue
        example = (item.get("examples") or [""])[0]
        issues.append(f"套话/句式：{item.get('message')}"
                      + (f"（如「{example}」）" if example else ""))
    if m.get("length_ratio") is not None and m["length_ratio"] < 0.8:
        issues.append(f"字数不足：现在 {m['chars']} 字，目标 {target} 字，"
                      f"至少写到 {int(target * 0.8)} 字")
    for word in m.get("forbidden_hits") or []:
        issues.append(f"违反禁令：出现了「{word}」")
    for word in m.get("leak_hits") or []:
        issues.append(f"参考素材泄漏：出现了「{word}」")
    if m.get("pov_ok") is False:
        issues.append(f"视角错误：作品要求「{m.get('pov')}」")
    if m.get("must_hit_rate") is not None and m["must_hit_rate"] < 1:
        issues.append("合同事件可能没写全，逐条核对 must_hit_now")
    return issues


async def run_case(case: Mapping[str, Any], recipe: Mapping[str, Any], *,
                   rep: int = 0, chat: Chat, tag: str) -> dict[str, Any]:
    case_id = case["id"]
    run_dir = RUNS / tag / f"{case_id}__{recipe['name']}__r{rep}"
    run_dir.mkdir(parents=True, exist_ok=True)
    db, work_id, chapter_id = seed_db(case, run_dir / "writing.db")
    service = WritingService(Config(project_dir=ROOT), db=db)
    pov = str((case.get("work") or {}).get("narrative_pov") or "")
    if recipe.get("pov") and pov:
        profile = dict((case.get("work") or {}).get("style_profile") or {})
        rules = [f"叙事视角：{pov}"] + list(profile.get("hard_rules") or [])
        profile["hard_rules"] = rules
        db.update_work_style_profile(work_id, profile)

    target = int((case.get("contract") or {}).get("target_chars") or 2000)
    terms = material.contract_terms(case)
    if recipe["reference"] == "hybrid2":
        from writebench.recall.pack import pack_for_case
        pack = await pack_for_case(case, k=int(recipe.get("ref_k") or 24))
    else:
        pack = material.collect(case, project_dir=ROOT, terms=terms)
    ref_text = ""
    if recipe["reference"] == "raw":
        ref_text = material.render_raw(pack, max_chars=recipe.get("ref_chars", 1400))
    elif recipe["reference"] in ("card", "card2", "hybrid2"):
        v2 = recipe["reference"] != "card"
        suffix_name = ("_v2_hybrid" if recipe["reference"] == "hybrid2"
                       else ("_v2" if v2 else ""))
        cache = RUNS / "_material" / f"{case_id}{suffix_name}.txt"
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            card = cache.read_text(encoding="utf-8")
        else:
            card = await _distill(chat, pack, model=recipe["distill_model"],
                                  v2=v2)
            cache.write_text(card, encoding="utf-8")
        ref_text = material.render_card(
            card, [b["label"] for b in pack.get("books") or []])
    style_ref = None
    if recipe.get("voice"):
        author = ((case.get("references") or {}).get("authors") or [{}])[0]
        if author.get("author_id"):
            style_ref = {"author_id": author["author_id"],
                         "scenario": "", "scene_type": author.get("scene_type", "")}
    assembled = bench_assemble.assemble(
        service, work_id, int(case["chapter"]["index"]),
        reference_text=ref_text, style_ref=style_ref,
        focus_refs=case.get("focus") if recipe.get("focus") else None,
        budget=recipe.get("budget", 6000))

    system = _prompt_text(recipe.get("prompt", "production"), target)
    suffix = str(recipe.get("suffix") or "")
    if recipe.get("prompt") == "production":
        # 与 stream_chapter 同序：素材说明在前，长度纪律最后。
        if ref_text:
            system += MATERIAL_PROMPT
        system += LENGTH_PROMPT.format(target=target,
                                       floor=int(target * 0.8))
    else:
        if "material2" in suffix:
            from writebench.variants import MATERIAL_SUFFIX_V2
            system += MATERIAL_SUFFIX_V2
        elif "material" in suffix:
            from writebench.variants import MATERIAL_SUFFIX
            system += MATERIAL_SUFFIX
        if "length" in suffix:
            from writebench.variants import LENGTH_SUFFIX
            system += LENGTH_SUFFIX.format(target=target,
                                           floor=int(target * 0.8))
    user = assembled["text"]
    ep = endpoint(recipe["model"][0], recipe["model"][1])
    started = time.time()
    plan = ""
    if recipe.get("pipeline") == "plan":
        plan = await _chat_complete(
            chat, ep, (PROMPTS / "plan.md").read_text(encoding="utf-8"),
            user, temperature=recipe.get("temperature"))
        user = user + "\n\n## 写作路线（只是走法，不是合同）\n" + plan.strip()
    content = _strip_think(await _chat_complete(
        chat, ep, system, user, temperature=recipe.get("temperature")))
    draft = content
    repair = None
    if recipe.get("pipeline") == "repair":
        issues = _quality_issues(case, content, target)
        if issues:
            revised = _strip_think(await _chat_complete(
                chat, ep, REVISE_SYSTEM,
                content + "\n\n## 质检单\n" + "\n".join(f"- {x}" for x in issues),
                temperature=recipe.get("temperature")))
            before = metrics.hard_score(
                metrics.mechanical(case, draft, reference=ref_text))
            after = metrics.hard_score(
                metrics.mechanical(case, revised, reference=ref_text))
            repair = {"issues": issues, "before": before, "after": after,
                      "accepted": bool(revised.strip()) and after >= before}
            if repair["accepted"]:
                content = revised
    elapsed = time.time() - started

    out = {"case": case_id, "recipe": recipe["name"], "rep": rep,
           "content": content, "draft": draft, "plan": plan, "repair": repair,
           "tokens": assembled["tokens"], "manifest": assembled["manifest"],
           "assembled": assembled["text"], "reference": ref_text,
           "style": assembled.get("style"), "elapsed_s": round(elapsed, 1)}
    (run_dir / "output.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (run_dir / "chapter.txt").write_text(content, encoding="utf-8")
    db.close()
    try:
        (run_dir / "writing.db").unlink()
    except OSError:
        pass
    return out
