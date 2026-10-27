"""读者笔记试跑 —— 只跑一篇，只打印，**不落库**。

设计稿：``docs/reader-notes-design.md``。表结构、召回、前端都还没做，
因为要先回答一个问题：**这些笔记是不是人话。**
机器指标量不了这个 —— 只能你自己读。

跑法：
  python scripts/reader_notes_trial.py 秋天的怀念
  python scripts/reader_notes_trial.py 秋天的怀念 --no-persona   # 对照：不带人格
  python scripts/reader_notes_trial.py 秋天的怀念 --temp 0.3

不建表、不写库、不碰服务 —— 跑完只往屏幕上打。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import aiohttp                                                  # noqa: E402

from bookgraph.notes import (check_note_quotes, find_scope,     # noqa: E402
                             generate_notes, roster_for, scope_chunks)
from config import Config                                       # noqa: E402
from persona import load_persona                                # noqa: E402
from storage.db import BookDB                                   # noqa: E402

ROOT = Path(__file__).parent.parent
BOOK = "我与地坛"


def _load_config() -> Config:
    """跟服务走同一条选路：控制台绑定优先，`.env` 兜底。

    光 ``Config()`` 拿不到控制台配的连接（那些在
    ``data/config/providers.json`` 里），直接跑会因为没 api_key 而失败。
    """
    cfg = Config()
    try:
        from provider_store import SLOT_MAIN_LLM, ProviderStore
        store = ProviderStore(ROOT / "data" / "config" / "providers.json")
        r = store.resolve(SLOT_MAIN_LLM, cfg)
        if r.key:
            cfg.api_key, cfg.api_base = r.key, r.base_url
            if r.model:
                cfg.model = r.model
        print(f"模型：{r.model or cfg.model}"
              + (f"（{r.provider_name}）" if r.provider_name else ""))
    except Exception as exc:
        print(f"（控制台连接读取失败，退回 .env：{exc}）")
    if not cfg.api_key:
        print("**没有可用的 api_key** —— 先在控制台配一个连接，或设 .env。")
    return cfg


def _pick_book(books_db: BookDB):
    row = books_db.get_conn().execute(
        "SELECT id, title FROM books WHERE title LIKE ?",
        (f"%{BOOK}%",)).fetchone()
    if not row:
        print(f"书库里没有《{BOOK}》")
        return None
    return row["id"], row["title"]


def _report(parsed: dict | None, chunk_text: dict[str, str]) -> None:
    """把这一趟的结果打在屏幕上 —— 这是试跑唯一的产出。"""
    if parsed is None:
        print("!! 这一趟**没跑成**（解析失败或 API 出错）。")
        print("   注意：这不等于「没什么可写」—— 后者是空数组，是诚实的回答。")
        return
    notes = [n for n in (parsed.get("notes") or []) if isinstance(n, dict)]
    read = str(parsed.get("read") or "").strip()
    if read:
        print(f"读完的感受：{read}\n")
    if not notes:
        print("这一篇没有值得写的。（空数组 —— 诚实的回答，不是失败）")
        return
    bad = check_note_quotes(notes, chunk_text)
    print(f"共 {len(notes)} 条，逐字校验 "
          f"{'全部通过' if not bad else f'**{len(bad)} 条不合格**'}")
    if bad:
        print("  不合格：" + "、".join(bad))
    print()
    for i, n in enumerate(notes, 1):
        who = str(n.get("node_name") or "").strip() or "（不属于任何人）"
        print(f"[{i}] 贴：{str(n.get('quote_text') or '').strip()}")
        print(f"    冲：{who}")
        print(f"    笔记：{str(n.get('text') or '').strip()}")
        print()


async def _main(args: argparse.Namespace) -> int:
    cfg = _load_config()
    if not cfg.api_key:
        return 2
    books_db = BookDB(ROOT / "data" / "books.db")
    picked = _pick_book(books_db)
    if not picked:
        return 2
    book_id, book_title = picked
    found = find_scope(books_db, book_id, args.needle)
    if not found:
        print(f"《{book_title}》里找不到「{args.needle}」")
        return 2
    title, idxs = found
    chunks = scope_chunks(books_db, book_id, idxs)
    chars = sum(len(t) for _c, t in chunks)
    print(f"书：《{book_title}》　篇：《{title}》　{len(chunks)} 块　{chars} 字")

    from storage.graph_db import resolve_graph_db_path
    roster = roster_for(resolve_graph_db_path(cfg, books_db, book_id),
                        [c for c, _t in chunks])
    print(f"篇内名册 {len(roster)} 个：{'、'.join(roster)}")

    persona = "" if args.no_persona else load_persona(cfg)
    print(f"人格：{'不带（对照）' if args.no_persona else f'{len(persona)} 字'}"
          f"　温度 {args.temp}\n")

    async with aiohttp.ClientSession() as session:
        parsed = await generate_notes(
            session, cfg, book_title, title, chunks, roster, persona,
            temperature=args.temp)
    _report(parsed, {c: t for c, t in chunks})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="读者笔记试跑（只打印，不落库）")
    ap.add_argument("needle", nargs="?", default="秋天的怀念",
                    help="篇名关键词，默认《秋天的怀念》")
    ap.add_argument("--no-persona", action="store_true",
                    help="对照实验：不注入人格")
    ap.add_argument("--temp", type=float, default=0.7, help="采样温度")
    return asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
