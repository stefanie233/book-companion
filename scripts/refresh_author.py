"""跑作者级的活：工序 4（编年校验）/ 5（作品房间）/ 6（生平补全）。

设计稿：``docs/authorgraph/README.md`` §七。

**为什么必须单独一个进程、一套机制**：书级采集（``run_author_harvest.py``）
读的是一本书的序跋，挂在那本书上；作者级的活是**跨书累积**的 ——
导入第 3 本张爱玲时，前两本的编年也要重算。这个依赖不体现在任何一本书上，
所以只能靠 ``needs_refresh`` 标脏表达（书级采集落完自动标），由这里消费。

**不跑这个脚本，``mark_dirty`` 就是个没人接的线头** —— 台账会一直
停在「采过语料但没整理过」的状态。

跑法：
  python scripts/refresh_author.py --list
  python scripts/refresh_author.py                 # 只跑标了脏的
  python scripts/refresh_author.py --all
  python scripts/refresh_author.py --author 史铁生
  python scripts/refresh_author.py --dry-run       # 只看会做什么，不写库
  python scripts/refresh_author.py --web           # 额外跑工序 6（联网，要 key）
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

from authorgraph.refresh import (Finding, audit_records,          # noqa: E402
                                 can_check, work_room_rows)
from authorgraph.scope import (book_identity,                     # noqa: E402
                               evidence_chunks)
from config import Config, load_config                            # noqa: E402
from flight_recorder import create_recorder, session_tag          # noqa: E402
from storage.authors_db import AuthorsDB, resolve_authors_db_path  # noqa: E402
from storage.db import BookDB                                     # noqa: E402

ROOT = Path(__file__).parent.parent


def _load_config() -> Config:
    """跟服务走同一条选路：控制台绑定优先，``.env`` 兜底。

    **必须走 ``load_config()``，不能裸构造 ``Config()``。** 后者不读
    ``.env`` —— 于是 ``search_api_key`` 永远是空的，工序 6 会一路报
    「没配搜索 key」，而 key 明明就在那儿。这个坑我踩过：用裸 ``Config()``
    去验 key 存不存在，验出个假阴性，还据此写进了结论。
    """
    cfg = load_config(ROOT)
    try:
        from provider_store import SLOT_MAIN_LLM, ProviderStore
        store = ProviderStore(ROOT / "data" / "config" / "providers.json")
        r = store.resolve(SLOT_MAIN_LLM, cfg)
        if r.key:
            cfg.api_key, cfg.api_base = r.key, r.base_url
            if r.model:
                cfg.model = r.model
    except Exception as exc:                             # noqa: BLE001
        print(f"（控制台连接读取失败，退回 .env：{exc}）")
    return cfg


def _titles(books_db: BookDB, links: list[dict]) -> dict[str, str]:
    """``stable_book_id → 书名``。``author_books.book_id`` 是软引用（可悬空），
    取不到就走 ``import_log`` 兜底 —— 与 ``book_identity`` 同一条路。"""
    out: dict[str, str] = {}
    for b in links:
        stable = b.get("stable_book_id") or ""
        if not stable or stable in out:
            continue
        bid = b.get("book_id") or ""
        title = ""
        if bid:
            row = books_db.get_conn().execute(
                "SELECT title FROM books WHERE id=?", (bid,)).fetchone()
            title = (row["title"] if row else "") or ""
        if not title:
            try:
                row = books_db.get_latest_import_for_stable(stable)
                bid = (row.book_id if row else "") or ""
                if bid:
                    r2 = books_db.get_conn().execute(
                        "SELECT title FROM books WHERE id=?", (bid,)).fetchone()
                    title = (r2["title"] if r2 else "") or ""
            except Exception:                            # noqa: BLE001
                pass
        if title:
            out[stable] = title
    return out


def _prefaces(books_db: BookDB, links: list[dict],
              titles: dict[str, str]) -> dict[str, dict]:
    """作者挂着的书的序跋正文 —— 逐字校验的比对源。

    只取序跋块，不取全书：编年是从序跋里抽的，拿整本书去比对会把
    「这句在正文里」也算过，而正文不是它的出处。

    **用 ``evidence_chunks`` 而不是 ``candidate_chunks``** —— 后者带
    「太短就不值得跑」的门槛，那是**采集时**的政策。拿它当比对源，
    实测会让《霍乱时期的爱情》那条编年查不到原文，而输出是空清单，
    和「全干净」长得一模一样。
    """
    out: dict[str, dict] = {}
    for stable in titles:
        link = next((b for b in links if b.get("stable_book_id") == stable), None)
        bid = (link or {}).get("book_id") or ""
        if not bid:
            try:
                row = books_db.get_latest_import_for_stable(stable)
                bid = (row.book_id if row else "") or ""
            except Exception:                            # noqa: BLE001
                bid = ""
        if not bid:
            continue
        try:
            by_chunk = evidence_chunks(books_db, bid)
        except Exception:                                # noqa: BLE001
            continue
        if by_chunk:
            out[stable] = {"by_chunk": by_chunk,
                           "all": list(by_chunk.values())}
    return out


def _audit_one(adb: AuthorsDB, author: dict, links: list[dict],
               titles: dict[str, str],
               prefaces: dict[str, dict]) -> tuple[list[Finding], int, int]:
    """工序 4：校验这个作者的编年。**只查 ``source='corpus'`` 的逐字。**

    返回 ``(缺陷, 查了几条, 该查几条)``。分母是**该查的**（``source='corpus'``），
    不是全部 —— 作品房间那批 ``source='graph'`` 的行本来就不带逐字原文，
    算进分母会让每个作者都显示「差一条」，把真问题淹掉。
    """
    recs = adb.get_conn().execute(
        "SELECT * FROM author_records WHERE author_id=?", (author["author_id"],)
    ).fetchall()
    rows = [dict(r) for r in recs]
    by_chunk: dict[str, str] = {}
    fallback: list[str] = []
    for r in rows:
        pf = prefaces.get(r.get("stable_book_id") or "")
        if not pf:
            continue
        by_chunk.update(pf["by_chunk"])
        fallback.extend(pf["all"])
    checkable = [r for r in rows if (r.get("source") or "") == "corpus"]
    checked = sum(1 for r in checkable if can_check(r, by_chunk, fallback))
    return audit_records(rows, by_chunk, fallback), checked, len(checkable)


def _one(adb: AuthorsDB, author: dict, links: list[dict],
         titles: dict[str, str], prefaces: dict[str, dict],
         *, dry_run: bool = False) -> dict:
    """工序 5 + 工序 4。返回一行台账。**零 token。**"""
    aid = author["author_id"]
    name = author.get("canonical_name") or aid
    existing = [dict(r) for r in adb.get_conn().execute(
        "SELECT * FROM author_records WHERE author_id=? AND kind IN"
        " ('work','publication')", (aid,)).fetchall()]
    new_works = work_room_rows(links=links, titles=titles, existing=existing)
    added = 0
    if not dry_run:
        for w in new_works:
            if adb.add_record(aid, w["kind"], w["statement"], year=w["year"],
                              stable_book_id=w["stable_book_id"],
                              source=w["source"]):
                added += 1
    findings, checked, total = _audit_one(adb, author, links, titles, prefaces)
    return {"author_id": aid, "name": name, "works": len(new_works),
            "works_added": added, "findings": findings,
            "checked": checked, "audited": total, "links": len(links)}


def _select(adb: AuthorsDB, args) -> list[dict]:
    if args.author:
        a = adb.find_author(args.author)
        return [a] if a else []
    if args.all:
        return [dict(r) for r in adb.get_conn().execute(
            "SELECT * FROM authors ORDER BY canonical_name").fetchall()]
    return adb.dirty_authors(limit=args.limit)


async def _run(args) -> int:
    books_db = BookDB(ROOT / "data" / "books.db")
    adb = AuthorsDB(resolve_authors_db_path(ROOT))
    if args.list:
        print(f"库内：{adb.counts()}")
        for a in adb.get_conn().execute(
                "SELECT canonical_name, needs_refresh, last_refresh_at"
                " FROM authors ORDER BY canonical_name").fetchall():
            flag = "脏" if a["needs_refresh"] else "  "
            print(f"  [{flag}] {a['canonical_name']}")
        return 0
    targets = _select(adb, args)
    if not targets:
        print("没有要整理的作者（没有标脏的）。--all 可以强制全跑。")
        return 0
    print(f"作者库：{resolve_authors_db_path(ROOT)}")
    print(f"要整理 {len(targets)} 位作者{'（dry-run，不写库）' if args.dry_run else ''}\n")
    ledger: list[dict] = []
    for i, a in enumerate(targets, 1):
        name = a.get("canonical_name") or a["author_id"]
        links = adb.books_of(a["author_id"])
        titles = _titles(books_db, links)
        prefaces = _prefaces(books_db, links, titles)
        try:
            led = _one(adb, a, links, titles, prefaces, dry_run=args.dry_run)
        except Exception as exc:                         # noqa: BLE001
            print(f"  [{i}/{len(targets)}] {name} —— 崩了：{exc}")
            ledger.append({"name": name, "failed": True})
            continue
        print(f"  [{i}/{len(targets)}] {name} —— {led['links']} 本书"
              f"　作品房间 +{led['works_added']}/{led['works']}"
              f"　编年校验 {led['checked']}/{led['audited']}")
        if led["links"] == 0:
            # 一个人挂 0 本书 = **幽灵作者**：`upsert_author` 建了行，但没有任何
            # 东西连着他（链接被清掉、或采集在那本书上没走到 link 那步）。
            # 只报告不删 —— 该补链接还是该删行是判断，不是机械操作。
            print("        ! 这个人没挂任何书（幽灵作者）—— 他占着一行，"
                  "但语料/编年/作品房间全是空的。")
        if led["checked"] < led["audited"]:
            print(f"        ! 有 {led['audited'] - led['checked']} 条逐字"
                  "没查成 —— **这不等于干净**，是没查（序跋块取不到？）。")
        for f in led["findings"]:
            print(f"        ! {f.render()}")
        ledger.append(led)
        if not args.dry_run:
            adb.clear_dirty(a["author_id"])
    if args.web:
        await _web_pass(adb, books_db, ledger, args)
    return _summary(adb, ledger, dry_run=args.dry_run)


async def _web_pass(adb: AuthorsDB, books_db: BookDB,
                    ledger: list[dict], args) -> None:
    """工序 6：联网补生平。**默认不跑** —— 它会把模型的世界知识写进台账。"""
    from authorgraph.enrich import enrich_life
    cfg = _load_config()
    if not cfg.api_key:
        print("\n工序 6 跳过：没有可用的 api_key。")
        return
    print(f"\n── 工序 6：联网补生平（{cfg.model}）──")
    # 飞行记录仪：丰化是裸 LLM 调用，不在 ReAct 循环里。提示词里带的是
    # 搜索原文，最容易出的错是「模型无视搜索结果自己背生平」—— 那件事
    # 在思维链里看得最清楚。
    recorder = create_recorder(ROOT)
    if recorder is None:
        print("（飞行记录仪构造失败，本次不落轨迹）")
    async with aiohttp.ClientSession() as session:
        for led in ledger:
            if led.get("failed"):
                continue
            author = adb.get_author(led["author_id"])
            if not author:
                continue
            # 把书名挂上去 —— 补全调用要知道「我们知道他写过什么」，
            # 否则它会去搜作品列表，把我们已经有的东西重搜一遍。
            links = adb.books_of(led["author_id"])
            titles = _titles(books_db, links)
            author["_books"] = [{"title": t} for t in titles.values()]
            log_cb = None
            if recorder is not None:
                sid = ("authorgraph.enrich:"
                       f"{session_tag(author.get('canonical_name') or led['author_id'])}")
                log_cb = lambda ev, sid=sid: recorder.record_llm_event(  # noqa: E731
                    ev, session_id=sid, mode="authorgraph.enrich")
            try:
                r = await enrich_life(session, cfg, adb, author, log_cb=log_cb)
            except Exception as exc:                     # noqa: BLE001
                print(f"  {led['name']} —— 联网失败：{exc}")
                continue
            led["web"] = r
            print(f"  {led['name']} —— 搜到 {r.get('hits', 0)} 条"
                  f"　写入 +{r.get('added', 0)}"
                  + (f"　跳过 {r.get('skipped', 0)}" if r.get("skipped") else ""))
            # 缺陷必须打出来：`fabricated_source` 是这一趟最该看见的东西，
            # 只报 warnings 的话降级会**静默**发生（写进去的是 model，
            # 不报就只会觉得「怎么这条进不了扮演」）。
            for d in (r.get("defects") or []):
                print(f"        ! {d}")
            for c in (r.get("conflicts") or []):
                print(f"        ~ 冲突：{c}")
            for w in (r.get("warnings") or []):
                print(f"        ! {w}")


def _summary(adb: AuthorsDB, ledger: list[dict], *, dry_run: bool) -> int:
    ok = sum(1 for x in ledger if not x.get("failed"))
    fail = sum(1 for x in ledger if x.get("failed"))
    works = sum(int(x.get("works_added") or 0) for x in ledger)
    finds = sum(len(x.get("findings") or []) for x in ledger)
    web = sum(int((x.get("web") or {}).get("added") or 0) for x in ledger)
    print(f"\n{'=' * 56}")
    print(f"整理 {ok} 位　失败 {fail}　作品房间 +{works} 条")
    print(f"编年缺陷 {finds} 条" + (f"　联网补 {web} 条" if web else ""))
    if dry_run:
        print("（dry-run：什么都没写，脏标也没清）")
    print(f"库内：{adb.counts()}")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="作者级刷新（工序 4/5/6）")
    ap.add_argument("--author", default="", help="只整理一位（名字，模糊匹配）")
    ap.add_argument("--all", action="store_true", help="所有作者，不看脏标")
    ap.add_argument("--limit", type=int, default=50, help="一次最多几位")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    ap.add_argument("--web", action="store_true",
                    help="额外跑工序 6（联网补生平；Tavily 或 DeepSeek 原生搜索）")
    ap.add_argument("--list", action="store_true", help="只看谁脏着")
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
