"""给已有阅读笔记里的引文补分级（kind / tag / who），独立进程。

跑法：
  python scripts/tag_reading_quotes.py 面纱
  python scripts/tag_reading_quotes.py 面纱 --arc 10 --force
  python scripts/tag_reading_quotes.py 面纱 --show

只送已逐字核对过的引文，不重读全书；每卷一次调用（默认 8 章）。
打标结果写回 note_json，并记 quote_tags_version，重复跑会跳过。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from config import Config                                      # noqa: E402
from flight_recorder import create_recorder                    # noqa: E402
from reading import tagging                                    # noqa: E402
from reading.store import (                                    # noqa: E402
    ReadingStore, highlights_for, render_highlights,
)
from storage.db import BookDB                                  # noqa: E402


def _pick(books_db: BookDB, needle: str):
    rows = books_db.get_conn().execute(
        "SELECT id, title FROM books ORDER BY title").fetchall()
    hits = [r for r in rows if needle in (r["title"] or "") or r["id"] == needle]
    if len(hits) != 1:
        print(f"「{needle}」匹配到 {len(hits)} 本，请给更具体的名字")
        return None
    return hits[0]


def _load_config() -> Config:
    cfg = Config()
    try:
        from provider_store import SLOT_MAIN_LLM, ProviderStore
        store = ProviderStore(ROOT / "data" / "config" / "providers.json")
        r = store.resolve(SLOT_MAIN_LLM, cfg)
        if r.key:
            cfg.api_key, cfg.api_base = r.key, r.base_url
            if r.model:
                cfg.model = r.model
        print(f"模型：{r.model or cfg.model}（来源 {r.source}）")
    except Exception as exc:                                   # noqa: BLE001
        print(f"（控制台连接读取失败，退回 .env：{exc}）")
    return cfg


async def _run(args: argparse.Namespace, book: dict) -> int:
    cfg = _load_config()
    if not cfg.api_key:
        print("**没有可用的 api_key**")
        return 2
    store = ReadingStore(ROOT / "data" / "reading.db")
    recorder = None if args.no_record else create_recorder(ROOT)
    try:
        done = await tagging.backfill_tags(
            cfg, store, book["id"], arc_size=args.arc, force=args.force,
            recorder=recorder,
            log_cb=lambda e: print(f"  第 {e['from']}–{e['to']} 章："
                                   f"{e['tagged']}/{e['quotes']} 条已分级"))
        print(f"回填 {len(done)} 卷")
        notes = store.notes_for(book["id"])
        if args.show or args.kind:
            for kind in ("literary", "kernel", "other"):
                items = highlights_for(notes, kind=kind)
                tags: dict[str, int] = {}
                for h in items:
                    tags[h["tag"]] = tags.get(h["tag"], 0) + 1
                print(f"\n== {kind} {len(items)} 条 ==")
                print("  " + "、".join(f"{k}:{v}" for k, v in
                                       sorted(tags.items(), key=lambda x: -x[1])))
        if args.kind:
            print("\n" + "-" * 66)
            print(render_highlights(
                highlights_for(notes, kind=args.kind, limit=args.limit)))
    finally:
        store.close()
        if recorder is not None:
            recorder.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="引文分级回填")
    p.add_argument("book")
    p.add_argument("--arc", type=int, default=8)
    p.add_argument("--force", action="store_true", help="已打标的章也重做")
    p.add_argument("--show", action="store_true", help="打印分布")
    p.add_argument("--kind", default="", help="打印该 kind 的高亮")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--no-record", action="store_true")
    args = p.parse_args()
    books_db = BookDB(ROOT / "data" / "books.db")
    hit = _pick(books_db, args.book)
    books_db.close()
    if hit is None:
        return 2
    return asyncio.run(_run(args, {"id": hit["id"],
                                   "title": hit["title"] or ""}))


if __name__ == "__main__":
    raise SystemExit(main())
