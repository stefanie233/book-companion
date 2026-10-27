"""成对比较：python -m writebench.pairwise --tag round1 --a base_pov --b ref_card"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from writebench import judge, variants               # noqa: E402
from writebench.fixtures import load_case             # noqa: E402
from writebench.llm import Chat                       # noqa: E402

RUNS = Path(r"C:\tmp\wb_runs")
OUT = Path(r"C:\tmp\wb_results")


def _output(tag: str, recipe: str, case_id: str,
            rep: int) -> tuple[str, str] | None:
    base = RUNS / tag / recipe / f"{case_id}__{recipe}__r{rep}"
    chapter = base / "chapter.txt"
    if not chapter.exists():
        return None
    data = json.loads((base / "output.json").read_text(encoding="utf-8"))
    return chapter.read_text(encoding="utf-8"), data.get("assembled", "")


async def compare(chat: Chat, tag: str, a: str, b: str) -> dict[str, Any]:
    cases = sorted({p.parent.name.split("__")[0]
                    for p in (RUNS / tag).glob(f"{a}/*/output.json")})

    async def one(case_id: str, rep: int) -> dict[str, Any] | None:
        pa, pb = _output(tag, a, case_id, rep), _output(tag, b, case_id, rep)
        if not pa or not pb:
            return None
        (ta, aa), (tb, ab) = pa, pb
        case = load_case(case_id)
        model = variants.PAIR_JUDGE
        ref = (judge.extract_block(ab, "reference_material")
               or judge.extract_block(aa, "reference_material"))
        fwd, rev = await asyncio.wait_for(asyncio.gather(
            judge.pairwise(chat, case, ta, tb, judge_model=model, reference=ref),
            judge.pairwise(chat, case, tb, ta, judge_model=model, reference=ref)),
            timeout=300)
        votes = [{"A": a, "B": b}.get(fwd["winner"], "tie"),
                 {"B": a, "A": b}.get(rev["winner"], "tie")]
        win = Counter(votes).most_common(1)[0][0]
        return {"case": case_id, "rep": rep, "winner": win,
                "fwd": fwd, "rev": rev}

    jobs = []
    for case_id in cases:
        reps = sorted(int(p.parent.name.rsplit("__r", 1)[-1])
                      for p in (RUNS / tag).glob(
                          f"{a}/{case_id}__{a}__r*/output.json"))
        jobs.extend(one(case_id, rep) for rep in reps)
    rows = [r for r in await asyncio.gather(*jobs, return_exceptions=True)
            if r and not isinstance(r, Exception)]
    tally = Counter(r["winner"] for r in rows)
    return {"tag": tag, "a": a, "b": b, "rows": rows, "tally": dict(tally)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    args = ap.parse_args()
    chat = Chat(concurrency=6)
    result = await compare(chat, args.tag, args.a, args.b)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{args.tag}__{args.a}_vs_{args.b}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"{args.a} vs {args.b}: {result['tally']}")
    for row in result["rows"]:
        print(f"  {row['case']} r{row['rep']}: {row['winner']} "
              f"| {row['fwd']['reason'][:60]}")


if __name__ == "__main__":
    asyncio.run(main())
