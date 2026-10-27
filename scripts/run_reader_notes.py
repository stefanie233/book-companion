"""全篇跑一遍读者笔记，落库。

设计稿：``docs/reader-notes-design.md``。试跑壳（``reader_notes_trial.py``）
只打印不落库；这个脚本写 ``graph_notes``。

**生成门是结构性的**：每一篇只把那篇的正文交给它，它物理上看不到后面的篇 ——
不需要额外的章号参数，看不见就是看不见。

跑法：
  python scripts/run_reader_notes.py 我与地坛
  python scripts/run_reader_notes.py 我与地坛 --reset      # 先清空再跑
  python scripts/run_reader_notes.py 我与地坛 --no-persona

已经跑过的篇默认跳过（按块判断），所以中途断了直接重跑即可。
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

from bookgraph.notes import (check_note_quotes, generate_notes,  # noqa: E402
                             iter_scopes, reanchor, roster_for,
                             scope_chunks)
from config import Config                                       # noqa: E402
from persona import load_persona                                # noqa: E402
from storage.db import BookDB                                   # noqa: E402
from storage.graph_db import GraphDB, resolve_graph_db_path      # noqa: E402

ROOT = Path(__file__).parent.parent


def _load_config() -> Config:
    """跟服务走同一条选路：控制台绑定优先，`.env` 兜底。"""
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


async def _run(args: argparse.Namespace) -> int:
    cfg = _load_config()
    if not cfg.api_key:
        return 2
    books_db = BookDB(ROOT / "data" / "books.db")
    row = books_db.get_conn().execute(
        "SELECT id, title FROM books WHERE title LIKE ?",
        (f"%{args.book}%",)).fetchone()
    if not row:
        print(f"书库里没有《{args.book}》")
        return 2
    book_id, book_title = row["id"], row["title"]
    gdb_path = resolve_graph_db_path(cfg, books_db, book_id)
    gdb = GraphDB(Path(gdb_path))
    print(f"书：《{book_title}》\n图谱：{gdb_path}")

    if args.reset:
        conn = gdb.get_conn()
        conn.execute("DELETE FROM graph_notes WHERE project_id=?", (book_id,))
        conn.commit()
        print("已清空旧笔记")
    if args.reanchor:
        return _repair(gdb, books_db, book_id)
    have = {n["chunk_id"] for n in gdb.get_notes(book_id)}
    persona = "" if args.no_persona else load_persona(cfg)
    print(f"人格：{'不带（对照）' if args.no_persona else f'{len(persona)} 字'}"
          f"　温度 {args.temp}　已有笔记 {len(have)} 条\n")
    return await _sweep(args, cfg, books_db, gdb, book_id, book_title,
                        gdb_path, persona, have)


async def _sweep(args, cfg, books_db, gdb, book_id, book_title,
                 gdb_path, persona, have) -> int:
    scopes = iter_scopes(books_db, book_id)
    stat = {"notes": 0, "bad": 0, "unresolved": 0, "failed": 0, "skipped": 0,
            "reanchored": 0}
    async with aiohttp.ClientSession() as session:
        for i, (title, idxs) in enumerate(scopes, 1):
            chunks = scope_chunks(books_db, book_id, idxs)
            if any(c in have for c, _t in chunks):
                stat["skipped"] += 1
                print(f"  [{i}/{len(scopes)}] {title} —— 已有笔记，跳过")
                continue
            roster = roster_for(gdb_path, [c for c, _t in chunks])
            parsed = await generate_notes(
                session, cfg, book_title, title, chunks, roster, persona,
                temperature=args.temp)
            _absorb(gdb, book_id, title, i, len(scopes), parsed, chunks, stat)
    return _summary(book_id, gdb, stat, len(scopes))


def _absorb(gdb, book_id: str, title: str, i: int, total: int,
            parsed: dict | None, chunks: list[tuple[str, str]],
            stat: dict) -> None:
    """落一篇的笔记。**解析不出 ≠ 没什么可写** —— 两者分开计数。"""
    if parsed is None:
        stat["failed"] += 1
        print(f"  [{i}/{total}] {title} —— !! 这一趟没跑成"
              "（注意：这不等于「没什么可写」）")
        return
    chunk_text = {c: t for c, t in chunks}
    notes = [n for n in (parsed.get("notes") or []) if isinstance(n, dict)]
    # 先纠锚，再校验 —— chunk_id 是导出量，不该让模型猜（见 notes.reanchor）
    stat["reanchored"] += len(reanchor(notes, chunk_text))
    bad = check_note_quotes(notes, chunk_text)
    stat["bad"] += len(bad)
    for n in notes:
        name = str(n.get("node_name") or "").strip()
        nid = gdb.name_to_node_id(book_id, name) if name else None
        if name and not nid:
            stat["unresolved"] += 1
        gdb.add_note(book_id, str(n.get("chunk_id") or "").strip(),
                     str(n.get("quote_text") or "").strip(),
                     str(n.get("text") or "").strip(), nid or "")
    stat["notes"] += len(notes)
    gdb.get_conn().commit()
    print(f"\n  [{i}/{total}] 《{title}》 {len(notes)} 条"
          + (f"　**逐字不合格 {len(bad)}**" if bad else ""))
    for n in notes:
        who = str(n.get("node_name") or "").strip() or "—"
        print(f"    · [{who}] {str(n.get('text') or '').strip()}")


def _summary(book_id: str, gdb, stat: dict, scopes: int) -> int:
    print(f"\n{'=' * 60}")
    print(f"共 {scopes} 篇：新写 {stat['notes']} 条，跳过 {stat['skipped']} 篇，"
          f"没跑成 {stat['failed']} 篇")
    print(f"  纠锚 {stat['reanchored']} 条　逐字不合格 {stat['bad']} 条　"
          f"node_name 解析不到 {stat['unresolved']} 条")
    print(f"  库里合计 {gdb.count_notes(book_id)} 条")
    return 0


def _repair(gdb, books_db, book_id: str) -> int:
    """只纠锚，不重新生成 —— 把已有笔记的 `chunk_id` 修正到引文真正所在的块。

    给存量用：改完 `reanchor` 不必烧一遍 LLM 重跑。
    """
    by_chunk = {r["id"]: (r["content"] or "") for r in books_db.get_conn().execute(
        "SELECT id, content FROM book_chunks WHERE book_id=?", (book_id,))}
    moved = 0
    for n in gdb.get_notes(book_id):
        fake = [{"quote_text": n["quote_text"], "chunk_id": n["chunk_id"]}]
        if not reanchor(fake, by_chunk):
            continue
        gdb.get_conn().execute(
            "UPDATE graph_notes SET chunk_id=? WHERE id=?",
            (fake[0]["chunk_id"], n["id"]))
        moved += 1
        print(f"  纠锚 {n['chunk_id'][:8]}→{fake[0]['chunk_id'][:8]}"
              f"　{n['quote_text'][:24]}")
    gdb.get_conn().commit()
    print(f"共纠锚 {moved} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="全篇跑读者笔记并落库")
    ap.add_argument("book", nargs="?", default="我与地坛", help="书名关键词")
    ap.add_argument("--reset", action="store_true", help="先清空这本书的笔记")
    ap.add_argument("--reanchor", action="store_true",
                    help="只纠锚不重跑（把 chunk_id 修正到引文真正所在的块）")
    ap.add_argument("--no-persona", action="store_true", help="对照：不注入人格")
    ap.add_argument("--temp", type=float, default=0.7, help="采样温度")
    return asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
