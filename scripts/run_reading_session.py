"""独立进程跑一次逐章阅读会话（原型）。

跑法：
  python scripts/run_reading_session.py 面纱 --limit 3
  python scripts/run_reading_session.py 面纱                 # 全本
  python scripts/run_reading_session.py 面纱 --from 10 --to 20
  python scripts/run_reading_session.py 面纱 --limit 8 --threshold 8000
  python scripts/run_reading_session.py 面纱 --kernel 20     # 看扮演内核
  python scripts/run_reading_session.py --list

阈值默认取模型窗口的 35%（`Config.context_alert_ratio`）。`--threshold`
直接给绝对 token 数，用来在小书上验证换窗路径。
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
from model_profiles import ModelProfileStore                   # noqa: E402
from reading import prompts                                    # noqa: E402
from reading.session import ReadingSession                     # noqa: E402
from reading.store import (                                    # noqa: E402
    ReadingStore, chapter_map, roleplay_kernel, scene_view,
)
from storage.db import BookDB                                  # noqa: E402


def _pick(books_db: BookDB, needle: str):
    rows = books_db.get_conn().execute(
        "SELECT id, title FROM books ORDER BY title").fetchall()
    hits = [r for r in rows if needle in (r["title"] or "") or r["id"] == needle]
    if not hits:
        print(f"没有匹配「{needle}」的书")
        return None
    if len(hits) > 1:
        print(f"「{needle}」匹配到 {len(hits)} 本，请给更具体的名字：")
        for r in hits:
            print(f"  {r['title'][:40]}  ({r['id']})")
        return None
    return hits[0]


def _load_config() -> Config:
    """控制台绑定优先，.env 兜底（与 run_graph_extraction 同一条选路）。"""
    cfg = Config()
    try:
        from provider_store import SLOT_MAIN_LLM, ProviderStore
        store = ProviderStore(ROOT / "data" / "config" / "providers.json")
        r = store.resolve(SLOT_MAIN_LLM, cfg)
        if r.key:
            cfg.api_key, cfg.api_base = r.key, r.base_url
            if r.model:
                cfg.model = r.model
        print(f"模型：{r.model or cfg.model}（来源 {r.source}"
              + (f" · {r.provider_name}" if r.provider_name else "") + "）")
    except Exception as exc:                                  # noqa: BLE001
        print(f"（控制台连接读取失败，退回 .env：{exc}）")
    if not cfg.api_key:
        print("**没有可用的 api_key** —— 先在控制台配一个连接，或设 .env。")
    return cfg


def _window_tokens(cfg: Config) -> int:
    try:
        store = ModelProfileStore(ROOT / "data" / "config"
                                  / "model_profiles.json")
        return int(store.resolve(str(cfg.model)).get("context_window") or 0)
    except Exception:
        return 0


def _on_event(e: dict, *, verbose: bool = False) -> None:
    if e.get("type") == "resume":
        print(f"  -- 续跑：已完成 {e.get('done')} 章，"
              f"待读 {e.get('pending')} 章")
    elif e.get("type") == "llm_call" and verbose:
        print(f"    · call {e.get('call')} 第 {e.get('chapter')} 章 "
              f"in={e.get('prompt')} cached={e.get('cached')} "
              f"out={e.get('out')} reason={e.get('reason')} "
              f"tools_offered={e.get('tools_offered')} "
              f"tool_calls={e.get('tool_calls')}")
    elif e.get("type") == "chapter_done":
        u = e.get("usage") or {}
        pin = int(u.get("prompt_tokens") or 0) or 1
        hit = int(u.get("cached_tokens") or 0) / pin
        compact = (e.get("compact") or "")[:38].replace("\n", " ")
        print(f"[{e.get('ordinal')}/{e.get('total')}] 第 {e.get('chapter')} 章 "
              f"{e.get('seconds')}s  in={pin} cached={u.get('cached_tokens', 0)}"
              f" ({hit:.0%})  out={u.get('completion_tokens', 0)}"
              f" (reason {u.get('reasoning_tokens', 0)})  {e.get('status')}"
              f"  {compact}")
    elif e.get("type") == "reset":
        lo, hi = (e.get("chapters") or [0, 0])[:2]
        print(f"  -- 换窗：第 {lo}–{hi} 章压成摘要，"
              f"{e.get('tokens_before')} → {e.get('tokens_after')} tokens")


def _run(args: argparse.Namespace, hit: dict) -> int:
    cfg = _load_config()
    if not cfg.api_key:
        return 2
    window = args.window or _window_tokens(cfg) or 128_000
    ratio = (args.threshold / window) if args.threshold else args.ratio
    books_db = BookDB(ROOT / "data" / "books.db")
    store = ReadingStore(ROOT / "data" / "reading.db")
    recorder = None if args.no_record else create_recorder(ROOT)
    session = ReadingSession(
        cfg, books_db, store, hit["id"], window_tokens=window,
        threshold_ratio=ratio, recent_chapters=args.recent,
        max_tool_rounds=args.max_tool_rounds,
        context_mode=args.context_mode, resume=args.resume,
        recorder=recorder,
        log_cb=lambda e: _on_event(e, verbose=args.verbose),
        record_usage=not args.no_usage_log)
    print(f"书：{hit['title']}  ({hit['id']})")
    print(f"窗口 {window:,} · 阈值 {session.threshold:,} tokens"
          f"（{session.threshold / max(1, window):.0%}）· "
          f"保留最近 {args.recent} 章 · 工具轮上限 {args.max_tool_rounds}"
          f" · 上下文 {args.context_mode}"
          + (" · 续跑" if args.resume else ""))
    print(f"run：{session.run_id}")
    if recorder is not None:
        print(f"飞行记录仪：reading:{session.run_id}"
              f"（data/context_log.db · {prompts.READING_PROMPT_VERSION}）")
    print()
    try:
        result = asyncio.run(session.run(
            limit=args.limit, chapter_from=args.chapter_from,
            chapter_to=args.chapter_to,
            include_front_matter=args.include_front_matter,
            min_chars=args.min_chars))
    except KeyboardInterrupt:
        store.finish_run(session.run_id, status="interrupted",
                         chapters_done=len(session.notes),
                         totals=session._totals())
        print("\n已中断；已读章节的笔记已落库。")
        result = session.summary()
    finally:
        books_db.close()
    _print_report(result, args, store, hit["id"])
    store.close()
    if recorder is not None:
        recorder.close()
    return 1 if result.get("failures") else 0


def _print_report(r: dict, args: argparse.Namespace,
                  store: ReadingStore, book_id: str) -> None:
    prompt = int(r.get("prompt_tokens") or 0)
    miss = int(r.get("miss_tokens") or 0)
    hit = float(r.get("hit_ratio") or 0)
    out = int(r.get("completion_tokens") or 0)
    print("\n" + "=" * 66)
    print(f"完成 {r.get('chapters_done')} 章 · 调用 {r.get('calls')} 次 · "
          f"换窗 {r.get('resets')} 次")
    print(f"输入 {prompt:,} · 命中 {int(r.get('cached_tokens') or 0):,}"
          f"（{hit:.1%}）· 未命中 {miss:,}")
    print(f"输出 {out:,}（推理 {int(r.get('reasoning_tokens') or 0):,}）")
    if r.get("failures"):
        print(f"失败 {len(r['failures'])} 章："
              + "、".join(str(f["chapter"]) for f in r["failures"]))
    if r.get("chapters_done", 0) >= 20:
        for label, ok in (("缓存命中 >=90%", hit >= 0.90),
                          ("未命中 <=100k", miss <= 100_000),
                          ("输出 <=110k", out <= 110_000)):
            print(f"  {'PASS' if ok else '注意'}  {label}")
    else:
        print("  （小样本：命中率/未命中只作参考，看趋势）")
    if args.map:
        notes = store.notes_for(book_id)
        print("\n" + "-" * 66 + "\n"
              + chapter_map(notes, store.maps_for(book_id), max_chars=8_000))
    if args.kernel:
        notes = store.notes_for(book_id, as_of=args.kernel)
        print("\n" + "-" * 66 + "\n"
              + roleplay_kernel(
                  notes, args.kernel,
                  map_text=chapter_map(notes, store.maps_for(book_id),
                                       as_of=args.kernel)))
    if args.scene:
        notes = store.notes_for(book_id, as_of=args.scene)
        print("\n" + "-" * 66 + "\n"
              + scene_view(notes, args.scene, window=args.scene_window,
                           map_text=chapter_map(notes, store.maps_for(book_id),
                                                as_of=args.scene)))


def main() -> int:
    p = argparse.ArgumentParser(description="逐章阅读会话（原型）")
    p.add_argument("book", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--from", dest="chapter_from", type=int)
    p.add_argument("--to", dest="chapter_to", type=int)
    p.add_argument("--window", type=int, default=0)
    p.add_argument("--threshold", type=int, default=0)
    p.add_argument("--ratio", type=float, default=0.35)
    p.add_argument("--recent", type=int, default=3)
    p.add_argument("--max-tool-rounds", type=int, default=4)
    p.add_argument("--context-mode", choices=("full", "compact"),
                   default="full")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--include-front-matter", action="store_true")
    p.add_argument("--min-chars", type=int, default=50)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--no-usage-log", action="store_true")
    p.add_argument("--no-record", action="store_true")
    p.add_argument("--map", action="store_true", help="打印认路地图")
    p.add_argument("--kernel", type=int, default=0)
    p.add_argument("--scene", type=int, default=0)
    p.add_argument("--scene-window", type=int, default=1)
    args = p.parse_args()

    books_db = BookDB(ROOT / "data" / "books.db")
    if args.list or not args.book:
        for r in books_db.get_conn().execute(
                "SELECT id, title FROM books ORDER BY title"):
            n = books_db.get_conn().execute(
                "SELECT COUNT(*) FROM book_chunks WHERE book_id=?",
                (r["id"],)).fetchone()[0]
            print(f"  {(r['title'] or '')[:44]:<46} {n:>4} 块  {r['id']}")
        books_db.close()
        return 0
    hit = _pick(books_db, args.book)
    books_db.close()
    if hit is None:
        return 2
    return _run(args, {"id": hit["id"], "title": hit["title"] or ""})


if __name__ == "__main__":
    raise SystemExit(main())





