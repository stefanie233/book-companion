"""跑声音采集：读作者写的正文，提炼可迁移的文风特征。

**独立进程**，与 ``run_author_harvest.py`` / ``run_graph_extraction.py`` 同理由：
服务进程里跑会冻事件循环。声音是**旁路**，失败不影响图谱提取 —— 重跑就是恢复。

它读两样东西：

- ``books.db`` 的正文块（按 ``author_books.position`` 判可见性）
- ``graph.db`` 里缓存的 ``scout_plan.voice_candidates``（有就用，没有就走
  确定性抽样 —— **不为此重跑 Scout**）

跑法：
  python scripts/run_voice_harvest.py --list
  python scripts/run_voice_harvest.py --book 我与地坛 --dry-run
  python scripts/run_voice_harvest.py --book 我与地坛
  python scripts/run_voice_harvest.py --author 史铁生 --stale
  python scripts/run_voice_harvest.py --all --force
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

from authorgraph.scope import book_identity                      # noqa: E402
from authorgraph.voice.pipeline import harvest_voice             # noqa: E402
from authorgraph.voice.sample import MAX_SAMPLES                 # noqa: E402
from authorgraph.voice.scope import visibility_for               # noqa: E402
from config import Config                                        # noqa: E402
from flight_recorder import create_recorder, session_tag         # noqa: E402
from storage.authors_db import AuthorsDB, resolve_authors_db_path  # noqa: E402
from storage.db import BookDB                                    # noqa: E402
from storage.graph_db import GraphDB, resolve_graph_db_path      # noqa: E402

ROOT = Path(__file__).parent.parent

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
            print(f"模型：{r.model or cfg.model}"
                  + (f"（{r.provider_name}）" if r.provider_name else ""))
    except Exception as exc:                             # noqa: BLE001
        print(f"（控制台连接读取失败，退回 .env：{exc}）")
    if not cfg.api_key:
        print("**没有可用的 api_key** —— 先在控制台配一个连接，或设 .env。")
    return cfg


def _targets(books_db: BookDB, adb: AuthorsDB, args) -> list[dict]:
    """要跑的书 ``[{author_id, stable_book_id, book_id, title, author,
    position, voice_status}]``。"""
    sql = "SELECT id, title FROM books"
    params: list[str] = []
    if args.book:
        sql += " WHERE title LIKE ?"
        params.append(f"%{args.book}%")
    sql += " ORDER BY time_created"
    want_aid = ""
    if args.author:
        found = adb.find_author(args.author)
        if found:
            want_aid = found["author_id"]
        else:
            print(f"  作者库里没有「{args.author}」")
            return []
    out: list[dict] = []
    for r in books_db.get_conn().execute(sql, params).fetchall():
        ident = book_identity(books_db, r["id"])
        stable = ident["stable_book_id"]
        owners = adb.authors_of_book(stable)
        if not owners:
            if args.book:
                print(f"  《{r['title']}》没有作者关系 —— 先跑 "
                      f"run_author_harvest.py")
            continue
        owners.sort(key=lambda x: 0 if x.get("role") == "author" else 1)
        owner = owners[0]
        if want_aid and owner["author_id"] != want_aid:
            continue
        st = adb.voice_status_of(owner["author_id"], stable)
        status = st.get("voice_status") or "none"
        pos = owner.get("position") or "unknown"
        if args.stale:
            # 没有导入钩子，stale 只能在 pipeline 里判定（内容 hash +
            # prompt_version + model）。这里只做**廉价的入选判断**：
            # position 变过就重新给机会；其余交给 pipeline 缓存命中。
            if status == "skipped":
                if not visibility_for(pos).body:
                    continue
                status = "none"
            elif status not in ("none", "stale", "failed", "pending",
                                "ready", "partial", "running"):
                continue
        out.append({
            "author_id": owner["author_id"],
            "author": owner.get("canonical_name") or "",
            "stable_book_id": stable, "book_id": r["id"], "title": r["title"],
            "position": pos,
            "voice_status": status,
        })
    return out


def _open_graph(cfg: Config, books_db: BookDB, book_id: str) -> GraphDB | None:
    """只在文件已存在时打开 —— ``GraphDB()`` 构造即建文件。"""
    try:
        path = resolve_graph_db_path(cfg, books_db, book_id)
        if not path.exists():
            return None
        return GraphDB(path)
    except Exception as exc:                             # noqa: BLE001
        print(f"        ! graph.db 打不开（走确定性抽样）：{exc}")
        return None


def _print_inventory(adb: AuthorsDB, books_db: BookDB) -> None:
    rows = adb.get_conn().execute(
        "SELECT a.canonical_name, b.author_id, b.stable_book_id, b.position,"
        " b.voice_status FROM author_books b"
        " JOIN authors a ON a.author_id=b.author_id"
        " ORDER BY a.canonical_name, b.time_updated DESC").fetchall()
    if not rows:
        print("作者库里还没有书。先跑 run_author_harvest.py。")
        return
    conn = adb.get_conn()
    for r in rows:
        # 台账按**书**显示（作者级数字会把一本书的语料挂到另一行上）。
        n = conn.execute(
            "SELECT COUNT(*) FROM author_corpus WHERE author_id=?"
            " AND stable_book_id=? AND narrative_layer<>'paratext'",
            (r["author_id"], r["stable_book_id"])).fetchone()[0]
        scenes = conn.execute(
            "SELECT COUNT(DISTINCT scene_type) FROM author_corpus"
            " WHERE author_id=? AND stable_book_id=?"
            " AND narrative_layer<>'paratext' AND scene_type<>''",
            (r["author_id"], r["stable_book_id"])).fetchone()[0]
        traits = conn.execute(
            "SELECT COUNT(*) FROM author_voice_traits WHERE author_id=?"
            " AND scope_ref=? AND status='active'",
            (r["author_id"], r["stable_book_id"])).fetchone()[0]
        print(f"  {r['canonical_name'] or '?':14} {r['stable_book_id']:18}"
              f" position={r['position'] or '?':10}"
              f" voice={r['voice_status']:8}"
              f" 语料={n:3} 场景={scenes} traits={traits}")


async def _one(session: aiohttp.ClientSession, cfg: Config,
               books_db: BookDB, adb: AuthorsDB, t: dict, i: int, n: int,
               args, recorder) -> dict:
    print(f"  [{i}/{n}] 《{t['title']}》 — {t['author'] or '?'}"
          f"　position={t['position']}　voice={t['voice_status']}")
    graph_db = _open_graph(cfg, books_db, t["book_id"])
    log_cb = None
    if recorder is not None:
        sid = f"authorgraph.voice:{session_tag(t['title'] or t['book_id'])}"
        log_cb = lambda ev: recorder.record_llm_event(          # noqa: E731
            ev, session_id=sid, mode="authorgraph.voice",
            book_id=t["book_id"])
    try:
        out = await harvest_voice(
            session, cfg, books_db, adb,
            author_id=t["author_id"], stable_book_id=t["stable_book_id"],
            book_id=t["book_id"], title=t["title"], graph_db=graph_db,
            force=args.force, max_samples=args.samples,
            dry_run=args.dry_run, log_cb=log_cb)
    except Exception as exc:                             # noqa: BLE001
        print(f"        !! 崩了：{exc}")
        return {"title": t["title"], "status": "failed", "error": str(exc)}
    finally:
        if graph_db is not None:
            graph_db.close()

    if args.dry_run and out.get("sample_preview"):
        for s in out["sample_preview"]:
            print(f"        · ch{s['chapter_index']} {s['scene_hint']:14}"
                  f" {s['chars']:3}字 {s['head']}")
    print(f"        状态={out.get('status')}"
          + (f"（{out.get('error_code')}）" if out.get("error_code") else "")
          + f"  样本={out.get('samples')} 批次={out.get('batches')}"
          f" 语料+{out.get('corpus_added')}(复用{out.get('corpus_reused')})"
          f" traits={out.get('traits')}（active {out.get('active_traits')}）"
          f" 场景={out.get('scenes')}")
    if out.get("conflicts"):
        for c in out["conflicts"]:
            print(f"        ~ {c}")
    for w in (out.get("warnings") or [])[:8]:
        print(f"        ! {w}")
    for f in (out.get("failures") or [])[:6]:
        print(f"        × {f}")
    return out


async def _run(args: argparse.Namespace) -> int:
    books_db = BookDB(ROOT / "data" / "books.db")
    adb = AuthorsDB(resolve_authors_db_path(ROOT))
    if args.list:
        _print_inventory(adb, books_db)
        return 0
    targets = _targets(books_db, adb, args)
    if not targets:
        print("没有要跑的书。")
        return 0
    print(f"作者库：{resolve_authors_db_path(ROOT)}\n要跑 {len(targets)} 本\n")
    cfg = _load_config()
    if not cfg.api_key and not args.dry_run:
        return 2
    recorder = create_recorder(ROOT)
    if recorder is None:
        print("（飞行记录仪构造失败，本次不落轨迹）")
    ledger: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for i, t in enumerate(targets, 1):
            ledger.append(await _one(session, cfg, books_db, adb, t, i,
                                     len(targets), args, recorder))
    return _summary(adb, ledger)


def _summary(adb: AuthorsDB, ledger: list[dict]) -> int:
    ok = sum(1 for x in ledger if x.get("status") in ("ready", "partial"))
    skip = sum(1 for x in ledger if x.get("status") == "skipped")
    fail = sum(1 for x in ledger if x.get("status") == "failed")
    dry = sum(1 for x in ledger if x.get("status") == "dry_run")
    corpus = sum(int(x.get("corpus_added") or 0) for x in ledger)
    traits = sum(int(x.get("traits") or 0) for x in ledger)
    print(f"\n{'=' * 56}")
    print(f"采成 {ok} 本　跳过 {skip}　失败 {fail}"
          + (f"　演练 {dry}" if dry else ""))
    print(f"新增声音语料 {corpus} 条　trait {traits} 条")
    for x in ledger:
        if x.get("status") == "failed":
            print(f"  ✗ 《{x.get('title')}》 {x.get('error_code') or ''}"
                  f" {x.get('error') or ''}")
    print("**失败不影响图谱提取**；重跑即可（内容 hash 命中会跳过）。")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="作者声音采集（文风特征）")
    ap.add_argument("--book", default="", help="书名（模糊匹配）")
    ap.add_argument("--author", default="", help="作者名（模糊匹配）")
    ap.add_argument("--all", action="store_true", help="全部（默认行为）")
    ap.add_argument("--stale", action="store_true",
                    help="只跑没采过/失败/内容变了的书")
    ap.add_argument("--force", action="store_true", help="清掉本书声音语料后重采")
    ap.add_argument("--dry-run", action="store_true",
                    help="只抽样并打印，不调模型、不落库")
    ap.add_argument("--samples", type=int, default=MAX_SAMPLES,
                    help=f"本次抽多少段（上限 {MAX_SAMPLES}）")
    ap.add_argument("--list", action="store_true", help="只看台账，不跑")
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
