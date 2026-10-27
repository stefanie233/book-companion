"""给已生成的输出补打分：python -m writebench.score --tag base1"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from writebench import judge, metrics, variants          # noqa: E402
from writebench.fixtures import load_case, load_cases    # noqa: E402
from writebench.llm import Chat                          # noqa: E402

RUNS = Path(r"C:\tmp\wb_runs")
OUT = Path(r"C:\tmp\wb_results")


def _find(tag: str) -> list[tuple[str, str, int, Path]]:
    found = []
    for path in sorted((RUNS / tag).glob("*/*/output.json")):
        recipe = path.parents[1].name
        case_id, _, rep = path.parent.name.partition("__")
        rep = rep.rsplit("__r", 1)[-1]
        found.append((case_id, recipe, int(rep), path))
    return found


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    chat = Chat(concurrency=args.concurrency)
    rows = []
    for case_id, recipe_name, rep, path in _find(args.tag):
        case = load_case(case_id)
        recipe = variants.get(recipe_name)
        data = json.loads(path.read_text(encoding="utf-8"))
        draft = data["content"]
        mech = metrics.mechanical(case, draft,
                                  reference=data.get("reference", ""))
        try:
            assembled = data.get("assembled", "")
            rub = await asyncio.wait_for(judge.rubric(
                chat, case, draft, judge_model=recipe["judge"],
                with_material=recipe["reference"] != "none",
                with_style=bool(recipe["voice"]),
                reference=judge.extract_block(assembled, "reference_material"),
                voice=judge.extract_block(assembled, "author_voice")), timeout=420)
            item = {"case": case_id, "recipe": recipe_name, "rep": rep,
                    "row": data, "mech": mech, "judge": rub}
            if case.get("facts") or case.get("promises"):
                item["consistency"] = await asyncio.wait_for(
                    judge.consistency(chat, case, draft,
                                      judge_model=recipe["judge"]), timeout=300)
        except Exception as exc:                     # noqa: BLE001
            item = {"case": case_id, "recipe": recipe_name, "rep": rep,
                    "row": data, "mech": mech, "error": str(exc)}
        rows.append(item)
        print("scored", case_id, recipe_name, rep)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"tag": args.tag, "results": rows,
               "calls": chat.calls,
               "prompt_tokens": chat.prompt_tokens,
               "completion_tokens": chat.completion_tokens}
    (OUT / f"{args.tag}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(table(rows))


def table(rows: list[dict[str, Any]]) -> str:
    lines = ["case | recipe | comp | hard | craft | contract | material | style | "
             "viol | chars | ratio | pov | deslop"]
    for r in rows:
        if "judge" not in r:
            lines.append(f"{r['case']} | {r['recipe']} | ERROR {r.get('error')}")
            continue
        j, m = r["judge"], r["mech"]
        craft = sum(j["criteria"][k]["score"] for k in judge.CRAFT) / 4
        mat = j.get("material_transfer")
        sty = j.get("style_fidelity")
        lines.append(
            f"{r['case']} | {r['recipe']} | {j['composite']:.2f} | "
            f"{metrics.hard_score(m):.1f} | {craft:.2f} | "
            f"{j['criteria']['contract_fidelity']['score']:.0f} | "
            f"{mat['score'] if mat else '-'} | {sty['score'] if sty else '-'} | "
            f"{len(j['contract_violations'])} | {m['chars']} | "
            f"{m['length_ratio']} | {m['pov_ok']} | {m['deslop_blocking']}")
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
