"""Backfill llm_usage_log from bookgraph.jsonl — run:
    python scripts/backfill_usage.py [--file data/logs/bookgraph.jsonl] [--dry-run]

dedup_key formula matches bookgraph/logging.py log_llm_call exactly, so
this is idempotent (INSERT OR IGNORE) and can never double-count a call
that was also recorded live after the feature shipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from usage_log import get_store


def backfill(jsonl_path: Path, dry_run: bool) -> None:
    attempted = inserted = skipped = malformed = 0
    store = None if dry_run else get_store()

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            attempted += 1
            try:
                rec = json.loads(line)
                ts = int(datetime.fromisoformat(rec["ts"]).timestamp() * 1000)
                key = "bg:%s:%s:%s:%s:%s:%s" % (
                    rec["ts"], rec.get("run_id", ""), rec.get("batch_id", ""),
                    rec.get("batch_index", 0), rec.get("attempt", 1),
                    rec.get("phase", ""),
                )
            except (json.JSONDecodeError, KeyError, ValueError):
                malformed += 1
                continue

            if dry_run:
                inserted += 1
                continue
            ok = store.insert_usage_log(
                ts=ts,
                model=rec.get("model") or "",
                source="bookgraph",
                tokens_in=rec.get("tokens_in") or 0,
                tokens_out=rec.get("tokens_out") or 0,
                session_id=rec.get("book_id") or "",
                status=rec.get("status") or "ok",
                dedup_key=key,
            )
            if ok:
                inserted += 1
            else:
                skipped += 1

    label = "would insert" if dry_run else "inserted"
    print(f"attempted={attempted} {label}={inserted} skipped={skipped} malformed={malformed}")


if __name__ == "__main__":
    root = Path(__file__).parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, default=root / "data" / "logs" / "bookgraph.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.file.exists():
        print(f"no such file: {args.file}")
        sys.exit(1)
    backfill(args.file, args.dry_run)
