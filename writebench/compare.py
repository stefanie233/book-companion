"""跨 tag 成对比较：两个配方各自从不同批次取输出。

用法：
    python -m writebench.compare --a base_pov --a-tag r1b,cont1 \
        --b ref_card_v2 --b-tag r2v2
"""

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


def _find(tags: list[str], recipe: str, case_id: str,
          rep: int) -> tuple[str, str] | None:
    for tag in tags:
        base = RUNS / tag / recipe / f"{case_id}__{recipe}__r{rep}"
        chapter = base / "chapter.txt"
        if not chapter.exists():
            continue
        data = json.loads((base / "output.json").read_text(encoding="utf-8"))
        return chapter.read_text(encoding="utf-8"), data.get("assembled", "")
    return None


async def compare(chat: Chat, a: str, b: str, a_tags: list[str],
                  b_tags: list[str]) -> dict[str, Any]:
    cases = sorted({p.parent.name.split("__")[0]
                    for tag in b_tags for p in (RUNS / tag / b).glob(
                        "*/output.json")})

    async def one(case_id: str) -> dict[str, Any] | None:
        pa, pb = _find(a_tags, a, case_id, 0), _find(b_tags, b, case_id, 0)
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
        return {"case": case_id, "winner": win, "fwd": fwd, "rev": rev}

    rows = [r for r in await asyncio.gather(
        *[one(c) for c in cases], return_exceptions=True)
        if r and not isinstance(r, Exception)]
    return {"a": a, "a_tags": a_tags, "b": b, "b_tags": b_tags,
            "rows": rows, "tally": dict(Counter(r["winner"] for r in rows))}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--a-tag", required=True)
    ap.add_argument("--b-tag", required=True)
    args = ap.parse_args()
    chat = Chat(concurrency=6)
    result = await compare(chat, args.a, args.b, args.a_tag.split(","),
                           args.b_tag.split(","))
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"cmp__{args.a}_vs_{args.b}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"{args.a} vs {args.b}: {result['tally']}")
    for row in result["rows"]:
        print(f"  {row['case']}: {row['winner']} | {row['fwd']['reason'][:70]}")


if __name__ == "__main__":
    asyncio.run(main())
