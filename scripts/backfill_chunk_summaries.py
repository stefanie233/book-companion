"""补录 chunk 简述 —— 给「简述上线之前就已提取」的书写「这一块在讲什么」。

    python scripts/backfill_chunk_summaries.py 道德经
    python scripts/backfill_chunk_summaries.py --book-id sb-xxxx --all

只补**空简述**的块（``book_chunks.summary=''``），已有值不动：图谱提取
产出的简述与上下文同源，通常比一次重读更准。正文按批送模型（默认 8 块/批），
输出直接写 books.db。

为什么单独一趟而不是重跑图谱：重跑是「再读一遍全书 + 重建关系」，成本高一个
数量级；这里只需要模型看正文说一句话，不复用任何图谱中间状态。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp

from bookgraph.chunk_summary import normalize_chunk_summaries
from bookgraph.llm_call import MAX_OUTPUT_BUDGET, is_truncated, output_budget
from config import load_config
from llm_json import loads_tolerant
from provider_store import config_for
from storage.db import BookDB
import usage_log

SYSTEM = """你在给一本书的正文块写「一句话简述」。

对每一块，用 20–60 字写清：这一块里谁做了什么、或作者在讲哪件事。
- 只写这一块的内容，不要把后面块的情节提前写进来。
- 具体到人和事；「本段描写了人物心理」是废话。
- 复述，不评价。纯目录 / 版权 / 标题页如实写。
- 本批每一块都要有一个键，一个都不能漏。
- 键必须**原样照抄** `=== chunk_id: xxx ===` 里的那一串，不要加前缀，
  不要改写成 chunk_001 / 1 / 2 这种序号。

直接输出 JSON，无其他文本：
{"<原样照抄这一块前面的 chunk_id>": "……"}"""


def build_user_message(chunks: list) -> str:
    """chunks: BookChunkRow 列表。每块前标注 chunk_id（与正文格式一致）。"""
    parts = [f"本批共 {len(chunks)} 块。", ""]
    for ck in chunks:
        parts.append(f"=== chunk_id: {ck.id} ===")
        parts.append(f"第 {ck.chapter_index + 1} 章 · chunk {ck.chunk_index}")
        parts.append(ck.content or "")
        parts.append("")
    return "\n".join(parts)


async def call_summaries(session, cfg, chunks: list) -> dict[str, str]:
    cfg = config_for(cfg, "bookgraph.explorer")  # same operation as Explorer
    ids = [ck.id for ck in chunks]
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": build_user_message(chunks)},
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": output_budget(cfg),
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2 ** attempt)
        try:
            async with session.post(
                f"{cfg.api_base}/chat/completions", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=600),
            ) as resp:
                if resp.status != 200:
                    print(f"  ! API {resp.status}: {(await resp.text())[:200]}")
                    continue
                data = json.loads(await resp.text())
                usage = data.get("usage") or {}
                usage_log.record(
                    model=cfg.model,
                    tokens_in=usage.get("prompt_tokens") or 0,
                    tokens_out=usage.get("completion_tokens") or 0,
                    source="chunk_summary_backfill",
                )
                if is_truncated(data):
                    body["max_tokens"] = min(body["max_tokens"] * 2,
                                             MAX_OUTPUT_BUDGET)
                    print(f"  ! 撞上限，预算抬到 {body['max_tokens']} 重试")
                    continue
                content = data["choices"][0]["message"].get("content") or ""
                parsed = loads_tolerant(content, expect=dict)
                if parsed is None:
                    print(f"  ! 无法解析: {content[:120]}")
                    continue
                got = normalize_chunk_summaries(parsed, ids)
                if not got:
                    print(f"  ! 解析出 {len(parsed)} 个键，但都不在本批 "
                          f"chunk_id 里：{list(parsed)[:3]} / 本批 {ids[:3]}")
                return got
        except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError) as exc:
            print(f"  ! {type(exc).__name__}: {exc}")
    return {}


async def run_book(db: BookDB, cfg, book, batch_size: int,
                   limit: int | None) -> None:
    rows = db.get_chunks_missing_summary(book.id, limit=limit)
    if not rows:
        print(f"《{book.title}》没有待补的块")
        return
    print(f"《{book.title}》待补 {len(rows)} 块，每批 {batch_size}")
    written = 0
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            got = await call_summaries(session, cfg, batch)
            if got:
                written += db.set_chunk_summaries(got)
            done = min(i + batch_size, len(rows))
            print(f"  {done}/{len(rows)} 本批 {len(got)} 条")
    have, total = db.count_chunks_with_summary(book.id)
    print(f"完成：写入 {written} 条，覆盖率 {have}/{total}")


def main() -> int:
    ap = argparse.ArgumentParser(description="补录 chunk 简述")
    ap.add_argument("title", nargs="?", default="", help="书名（子串匹配）")
    ap.add_argument("--book-id", default="")
    ap.add_argument("--all", action="store_true", help="所有缺少简述的书")
    ap.add_argument("--batch-size", type=int, default=12,
                    help="每批送几块（默认 12；调大省调用次数，调小省单次延迟）")
    ap.add_argument("--limit", type=int, default=0,
                    help="每本书最多补多少块（0 = 全部）")
    args = ap.parse_args()

    cfg = load_config()
    # 控制台的连接绑定优先 —— .env 里的端点可能早已过期（实测 volcengine
    # 的 CodingPlan 订阅到期后，脚本会连续三次 400 而服务本体一切正常）。
    try:
        from provider_store import ProviderStore
        changed = ProviderStore(cfg.providers_path).apply_to_config(cfg)
        if changed:
            print("endpoint:", changed)
    except Exception:
        pass
    db = BookDB(cfg.books_db_path)
    db.initialize()

    if args.book_id:
        books = [db.get_book(args.book_id)]
    elif args.all:
        books = db.list_books(limit=1000)
    elif args.title:
        books = [b for b in db.list_books(limit=1000)
                 if args.title in (b.title or "")]
    else:
        ap.error("给书名、--book-id，或用 --all")
    books = [b for b in books if b]
    if not books:
        print("没有找到书")
        return 1

    limit = args.limit or None
    for book in books:
        asyncio.run(run_book(db, cfg, book, args.batch_size, limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
