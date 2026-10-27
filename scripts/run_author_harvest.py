"""跑作者层采集：读书的序跋，认作者，落作者台账。

设计稿：``docs/authorgraph/README.md``。

**独立进程**。理由和 ``run_graph_extraction.py`` 一样（服务进程内跑会冻
事件循环），但更简单：作者层没有进度条、没有 SSE、没有断点续跑 ——
一个作者十来次调用、一分钟的事，**重跑就是恢复机制**。

幂等靠**按块判断**（抄 ``run_reader_notes.py``）：某本书已有语料就跳过，
``--force`` 才重跑。所以中途断了直接再跑一遍即可。

跑法：
  python scripts/run_author_harvest.py --list
  python scripts/run_author_harvest.py --book 我与地坛
  python scripts/run_author_harvest.py --stale        # 只跑没采过的
  python scripts/run_author_harvest.py --all --force  # 全部重采
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import aiohttp                                                   # noqa: E402

from authorgraph.harvest import harvest_author                    # noqa: E402
from authorgraph.scope import (book_identity, candidate_chunks,   # noqa: E402
                               toc_lines)
from authorgraph.store import store_harvest                       # noqa: E402
from config import Config                                         # noqa: E402
from flight_recorder import create_recorder, session_tag          # noqa: E402
from storage.authors_db import AuthorsDB, resolve_authors_db_path  # noqa: E402
from storage.db import BookDB                                     # noqa: E402

ROOT = Path(__file__).parent.parent


def _load_config() -> Config:
    """跟服务走同一条选路：控制台绑定优先，``.env`` 兜底。"""
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
    except Exception as exc:                             # noqa: BLE001
        print(f"（控制台连接读取失败，退回 .env：{exc}）")
    if not cfg.api_key:
        print("**没有可用的 api_key** —— 先在控制台配一个连接，或设 .env。")
    return cfg


def _targets(books_db: BookDB, adb: AuthorsDB, args) -> list[tuple[str, str]]:
    """要跑的书 ``[(book_id, title)]``。"""
    if args.book:
        rows = books_db.get_conn().execute(
            "SELECT id, title FROM books WHERE title LIKE ?",
            (f"%{args.book}%",)).fetchall()
    else:
        rows = books_db.get_conn().execute(
            "SELECT id, title FROM books ORDER BY time_created").fetchall()
    out: list[tuple[str, str]] = []
    for r in rows:
        ident = book_identity(books_db, r["id"])
        stable = ident["stable_book_id"]
        done = adb.get_conn().execute(
            "SELECT COUNT(*) FROM author_corpus WHERE stable_book_id=?",
            (stable,)).fetchone()[0]
        if done and not args.force:
            if args.book:
                print(f"  《{r['title']}》已采过 {done} 条语料（--force 可重跑）")
            continue
        out.append((r["id"], r["title"]))
    return out


async def _one(session: aiohttp.ClientSession, cfg: Config, books_db: BookDB,
               adb: AuthorsDB, book_id: str, title: str, i: int,
               total: int, force: bool = False, recorder=None) -> dict:
    """采一本书。返回一行台账。"""
    ident = book_identity(books_db, book_id)
    chunks, warns = candidate_chunks(books_db, book_id)
    if not chunks:
        print(f"  [{i}/{total}] 《{title}》—— 无序跋，跳过")
        for w in warns:
            print(f"        ! {w}")
        return {"title": title, "skipped": True, "warnings": warns}
    toc = toc_lines(books_db, book_id)
    print(f"  [{i}/{total}] 《{title}》—— {len(chunks)} 块序跋"
          f"（{sum(len(c['text']) for c in chunks)} 字）")
    if force:
        # --force = 真正的重跑：先清这本书的旧料，否则模型每轮挑的句子不同，
        # 唯一约束挡不住，语料只会越滚越多（实测两轮 72 → 122）
        gone = adb.clear_book(ident["stable_book_id"])
        if any(gone.values()):
            print(f"        清掉旧料：语料 {gone.get('author_corpus', 0)}"
                  f"　编年 {gone.get('author_records', 0)}")
    # 飞行记录仪：采集不在 ReAct 循环里，提示词/思维链/正文不在这里显式
    # 交出去就完全看不见 —— 而调提示词时最需要的正是这三样。
    log_cb = None
    if recorder is not None:
        sid = f"authorgraph.harvest:{session_tag(title or book_id)}"
        log_cb = lambda ev: recorder.record_llm_event(          # noqa: E731
            ev, session_id=sid, mode="authorgraph.harvest", book_id=book_id)
    result = await harvest_author(session, cfg, ident, toc, chunks,
                                  log_cb=log_cb)
    if result is None:
        # 与 reader_notes 同一条约定：跑不成 ≠ 没料
        print(f"        !! 这一趟没跑成（不等于「这本书没料」）")
        return {"title": title, "failed": True, "warnings": warns}
    led = store_harvest(adb, ident, result)
    co = led.get("co_authors") or []
    print(f"        作者={led.get('author_name') or '?'}"
          + (f"（合著：{'、'.join(co)}）" if co else "")
          + f"  位置={led.get('position') or '?'}"
          f"  语料+{led.get('corpus_added', 0)}"
          f"  编年+{led.get('records_added', 0)}")
    for d in (led.get("defects") or []):
        print(f"        ! {d}")
    for w in warns:
        print(f"        ! {w}")
    led.update({"title": title, "warnings": warns})
    return led


async def _run(args: argparse.Namespace) -> int:
    books_db = BookDB(ROOT / "data" / "books.db")
    adb = AuthorsDB(resolve_authors_db_path(ROOT))
    if args.list:
        for aid_name, n in _inventory(adb):
            print(f"  {aid_name:20} 语料 {n}")
        return 0
    targets = _targets(books_db, adb, args)
    if not targets:
        print("没有要跑的书。")
        return 0
    cfg = _load_config()
    if not cfg.api_key:
        return 2
    recorder = create_recorder(ROOT)
    if recorder is None:
        print("（飞行记录仪构造失败，本次不落轨迹）")
    print(f"作者库：{resolve_authors_db_path(ROOT)}\n"
          f"要跑 {len(targets)} 本\n")
    ledger: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for i, (bid, title) in enumerate(targets, 1):
            try:
                ledger.append(await _one(session, cfg, books_db, adb,
                                         bid, title, i, len(targets),
                                         force=args.force, recorder=recorder))
            except Exception as exc:                     # noqa: BLE001
                print(f"        !! 崩了：{exc}")
                ledger.append({"title": title, "failed": True})
    # 工序 1（身份归一化）：采完自动合一次。`upsert_author` 已按归一化键
    # 匹配，所以这只处理**存量**裂口（老库、或归一化规则上线前建的）。
    merged = adb.dedupe_authors()
    for m in merged:
        print(f"  合并同一人：{m['drop']} → {m['keep']}"
              f"（语料 {m['corpus']}　编年 {m['records']}）")
    return _summary(adb, ledger)


def _inventory(adb: AuthorsDB) -> list[tuple[str, int]]:
    rows = adb.get_conn().execute(
        "SELECT a.canonical_name n, COUNT(c.id) k FROM authors a"
        " LEFT JOIN author_corpus c ON c.author_id=a.author_id"
        " GROUP BY a.author_id ORDER BY k DESC").fetchall()
    return [(r["n"], r["k"]) for r in rows]


def _summary(adb: AuthorsDB, ledger: list[dict]) -> int:
    ok = sum(1 for x in ledger if x.get("ok"))
    skip = sum(1 for x in ledger if x.get("skipped"))
    fail = sum(1 for x in ledger if x.get("failed"))
    corpus = sum(int(x.get("corpus_added") or 0) for x in ledger)
    recs = sum(int(x.get("records_added") or 0) for x in ledger)
    defects = sum(len(x.get("defects") or []) for x in ledger)
    print(f"\n{'=' * 56}")
    print(f"采成 {ok} 本　跳过 {skip}　没跑成 {fail}")
    print(f"新增语料 {corpus} 条　编年 {recs} 条　缺陷 {defects} 条")
    print(f"库内：{adb.counts()}")
    if fail:
        print("**有没跑成的** —— 重跑一遍即可（幂等）。")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="作者层采集")
    ap.add_argument("--book", default="", help="书名（模糊匹配）")
    ap.add_argument("--all", action="store_true", help="全部书（默认行为）")
    ap.add_argument("--stale", action="store_true",
                    help="只跑没采过的（默认行为，保留给可读性）")
    ap.add_argument("--force", action="store_true", help="已有语料也重采")
    ap.add_argument("--list", action="store_true", help="只看台账，不跑")
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

