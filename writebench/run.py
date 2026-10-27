"""CLI：python -m writebench.run --cases c01,c02 --recipes base,ref_raw --reps 1"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from writebench import judge, metrics, runner, variants  # noqa: E402
from writebench.fixtures import load_cases                     # noqa: E402
from writebench.llm import Chat                                # noqa: E402

OUT = Path(r"C:\tmp\wb_results")


def _parse(value: str) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


async def _score(chat: Chat, case: dict[str, Any], row: dict[str, Any],
                 recipe: dict[str, Any]) -> dict[str, Any]:
    draft = row["content"]
    mech = metrics.mechanical(case, draft, reference=row.get("reference", ""))
    assembled = row.get("assembled", "")
    rub = await judge.rubric(
        chat, case, draft, judge_model=recipe["judge"],
        with_material=recipe["reference"] != "none",
        with_style=bool(recipe["voice"]),
        reference=judge.extract_block(assembled, "reference_material"),
        voice=judge.extract_block(assembled, "author_voice"))
    result = {"case": case["id"], "recipe": recipe["name"],
              "rep": row["rep"], "row": row, "mech": mech, "judge": rub}
    if case.get("facts") or case.get("promises"):
        con = await judge.consistency(
            chat, case, draft, judge_model=recipe["judge"])
        result["consistency"] = con
    return result


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="")
    ap.add_argument("--recipes", default="base")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--split", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()

    wanted = _parse(args.cases)
    cases = [c for c in load_cases(args.split or None)
             if not wanted or c["id"] in wanted]
    recipes = [variants.get(x) for x in _parse(args.recipes)]
    tag = args.tag or time.strftime("%m%d-%H%M%S")
    chat = Chat(concurrency=args.concurrency)

    jobs = []
    for case in cases:
        for recipe in recipes:
            for rep in range(args.reps):
                jobs.append(runner.run_case(
                    case, recipe, rep=rep, chat=chat,
                    tag=f"{tag}/{recipe['name']}"))
    rows = await asyncio.gather(*jobs, return_exceptions=True)

    scored: list[dict[str, Any]] = []
    by_key = {(c["id"], r["name"]): c for c in cases for r in recipes}
    index = 0
    for case in cases:
        for recipe in recipes:
            for rep in range(args.reps):
                row = rows[index]
                index += 1
                if isinstance(row, Exception):
                    scored.append({"case": case["id"], "recipe": recipe["name"],
                                   "rep": rep, "error": str(row)})
                    continue
                if args.no_judge:
                    scored.append({"case": case["id"], "recipe": recipe["name"],
                                   "rep": rep, "row": row})
                    continue
                try:
                    scored.append(await _score(chat, case, row, recipe))
                except Exception as exc:             # noqa: BLE001
                    scored.append({"case": case["id"], "recipe": recipe["name"],
                                   "rep": rep, "error": f"judge: {exc}"})

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{tag}.json"
    payload = {"tag": tag, "cases": [c["id"] for c in cases],
               "recipes": [r["name"] for r in recipes],
               "reps": args.reps, "calls": chat.calls,
               "prompt_tokens": chat.prompt_tokens,
               "completion_tokens": chat.completion_tokens,
               "results": scored}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")

    agg: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in scored:
        if "judge" not in item:
            continue
        agg.setdefault((item["case"], item["recipe"]), []).append(item)
    lines = ["case | recipe | n | composite | craft | contract | material | style | violations | chars | deslop"]
    for (cid, rid), items in sorted(agg.items()):
        comp = sum(i["judge"]["composite"] for i in items) / len(items)
        craft = sum(sum(i["judge"]["criteria"][k]["score"] for k in judge.CRAFT)
                    for i in items) / (len(items) * 4)
        contract = sum(i["judge"]["criteria"]["contract_fidelity"]["score"]
                       for i in items) / len(items)
        mat = [i["judge"]["material_transfer"]["score"] for i in items
               if i["judge"].get("material_transfer")]
        sty = [i["judge"]["style_fidelity"]["score"] for i in items
               if i["judge"].get("style_fidelity")]
        viol = sum(len(i["judge"]["contract_violations"]) for i in items)
        chars = sum(i["mech"]["chars"] for i in items) / len(items)
        des = sum(i["mech"]["deslop_blocking"] for i in items) / len(items)
        mat_s = f"{sum(mat) / len(mat):.1f}" if mat else "-"
        sty_s = f"{sum(sty) / len(sty):.1f}" if sty else "-"
        lines.append(
            f"{cid} | {rid} | {len(items)} | {comp:.2f} | {craft:.2f} | "
            f"{contract:.1f} | {mat_s} | {sty_s} | {viol} | "
            f"{chars:.0f} | {des:.1f}")
    (OUT / f"{tag}.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
