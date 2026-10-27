"""CLI：python -m writebench.recall build|eval|query|stats"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import INDEX_DIR


def _build(args: argparse.Namespace) -> None:
    from .index import build
    from .units import case_books
    books = set()
    if args.case_books:
        books = set(case_books())
    if args.books:
        books |= {b.strip() for b in args.books.split(",") if b.strip()}
    meta = build(limit=args.limit, force=args.force,
                 concurrency=args.concurrency, books=books or None,
                 model=args.model)
    print(json.dumps(meta, ensure_ascii=False, indent=1))


def _eval(args: argparse.Namespace) -> None:
    from .evaluate import run
    result = run(n=args.n, seed=args.seed, regenerate=args.regenerate,
                 report_dir=Path(args.report_dir) if args.report_dir else None,
                 judge=not args.no_judge)
    print(f"units={result['index']['units']} n={result['synthetic_n']}")
    for row in result["synthetic"]:
        print(f"{row['method']:8s} R@1={row['r1']:.2f} "
              f"R@5={row['r5']:.2f} R@10={row['r10']:.2f} "
              f"MRR={row['mrr']:.2f}")
    print(f"cos={result['cos']}")
    for row in result["cases"]:
        print(f"{row['case']:18s} lex={row['lexical_gate']} "
              f"vec={row['vector_gate']} hyb={row['hybrid_gate']} "
              f"top1={row['top1_cos']:.3f} overlap={row['overlap']:.2f}")
    for row in result.get("case_judge") or []:
        cells = " ".join(
            f"{m}={row[m]['mean']:.2f}/{row[m]['usable']:.2f}"
            for m in ("lexical", "vector", "hybrid"))
        print(f"judge {row['case']:18s} {cells}")
    print(f"report -> {Path(args.report_dir or INDEX_DIR) / 'report.md'}")


def _query(args: argparse.Namespace) -> None:
    from .index import encode_query, load
    ix = load()
    vec = encode_query(args.text, model=ix.model)
    if args.method == "vector":
        hits = ix.vector(vec, args.k, args.book)
    elif args.method == "lexical":
        hits = ix.lexical(args.text, args.k, args.book)
    else:
        hits = ix.hybrid(args.text, vec, args.k, args.book)
    for rank, (idx, score) in enumerate(hits, 1):
        unit = ix.units[idx]
        print(f"{rank}. [{unit['kind']}] {unit['book']} ch{unit['chapter']} "
              f"score={score:.4f}")
        print(f"   {unit['text'][:160]}")


def _stats(_: argparse.Namespace) -> None:
    from .index import load
    ix = load()
    counts: dict[str, int] = {}
    for unit in ix.units:
        counts[unit["kind"]] = counts.get(unit["kind"], 0) + 1
    print(json.dumps({"units": len(ix.units), "dims": int(ix.mat.shape[1]),
                      "kinds": counts, "model": ix.model},
                     ensure_ascii=False, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(prog="writebench.recall")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--case-books", action="store_true")
    p.add_argument("--books", help="comma separated stable_book_ids")
    p.add_argument("--model", default="doubao-embedding-vision")
    p.set_defaults(func=_build)

    p = sub.add_parser("eval")
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--regenerate", action="store_true")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--report-dir")
    p.set_defaults(func=_eval)

    p = sub.add_parser("query")
    p.add_argument("text")
    p.add_argument("--book")
    p.add_argument("--method", choices=("vector", "lexical", "hybrid"),
                   default="hybrid")
    p.add_argument("-k", type=int, default=5)
    p.set_defaults(func=_query)

    p = sub.add_parser("stats")
    p.set_defaults(func=_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
