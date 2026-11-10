"""按攻略采作者细节：序跋/指定正文 → 观察 + 钩子。

**独立进程**，与 ``run_author_harvest.py`` / ``run_voice_harvest.py`` 同理由：
服务进程里跑会冻事件循环。重跑就是恢复机制；观察与钩子都有幂等键。

跑法：
  python scripts/run_author_details.py --list
  python scripts/run_author_details.py --author 史铁生 --plan
  python scripts/run_author_details.py --author 史铁生 --extract
  python scripts/run_author_details.py --author 史铁生 --close-hooks
  python scripts/run_author_details.py --author 史铁生 --plans
  python scripts/run_author_details.py --set-plan pl-xxxx black
  python scripts/run_author_details.py --book 小团圆 --read-as autobiographical --extract
  python scripts/run_author_details.py --all --plan --extract --dry-run

``--close-hooks`` 先用一次 ``plan_queries`` 给全部开放钩子出短检索词，
再逐钩子搜索/抓页/一问一答；引文逐字命中才关闭。
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

from authorgraph.details import extract_details, store_details    # noqa: E402
from authorgraph.plan import (deterministic_plan, model_plan)     # noqa: E402
from authorgraph.sources.hook_close import (answer_hook,          # noqa: E402
                                            plan_queries)
from authorgraph.scope import (body_sample_chunks, book_identity,  # noqa: E402
                               candidate_chunks, chunks_by_title)
from config import Config, load_config                           # noqa: E402
from flight_recorder import create_recorder, session_tag          # noqa: E402
from storage.authors_db import AuthorsDB, resolve_authors_db_path  # noqa: E402
from storage.db import BookDB                                     # noqa: E402

# 数据根（用户根）：源码 = 仓库根，安装版 = %LOCALAPPDATA%\LiuBai。
# 脚本经 --run-script 在冻结产物里运行时 __file__ 在 _MEIPASS，不能拿来存数据。
from runtime_paths import user_root                                 # noqa: E402
ROOT = user_root()
BATCH_CHARS = 45_000


def _load_config() -> Config:
    """跟服务走同一条选路：控制台绑定优先，``.env`` 兜底。

    必须走 ``load_config`` 而不是裸 ``Config()``：后者只是 dataclass 默认值，
    拿不到 ``.env`` 里的 Tavily key 等联网配置，钩子闭合会静默零结果。
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
            print(f"模型：{r.model or cfg.model}")
    except Exception as exc:                             # noqa: BLE001
        print(f"（控制台连接读取失败，退回 .env：{exc}）")
    if not cfg.api_key:
        print("**没有可用的 api_key** —— 只能跑规则攻略，不能抽取。")
    return cfg


def _target_authors(adb: AuthorsDB, books_db: BookDB,
                    args) -> list[dict]:
    if args.author:
        found = adb.find_author(args.author)
        return [found] if found else []
    if args.book:
        rows = books_db.get_conn().execute(
            "SELECT id FROM books WHERE title LIKE ?",
            (f"%{args.book}%",)).fetchall()
        out: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            ident = book_identity(books_db, r["id"])
            for a in adb.authors_of_book(ident["stable_book_id"]):
                if a["author_id"] not in seen:
                    seen.add(a["author_id"])
                    out.append(dict(adb.get_author(a["author_id"]) or a))
        return out
    return [dict(r) for r in adb.get_conn().execute(
        "SELECT * FROM authors ORDER BY canonical_name").fetchall()]


def _split_batches(chunks: list[dict], limit: int = BATCH_CHARS) -> list[list[dict]]:
    out: list[list[dict]] = []
    cur: list[dict] = []
    used = 0
    for c in chunks:
        n = len(c.get("text") or "")
        if cur and used + n > limit:
            out.append(cur)
            cur, used = [], 0
        cur.append(c)
        used += n
    if cur:
        out.append(cur)
    return out


def _book_row(adb: AuthorsDB, aid: str, stable: str) -> dict:
    return next((b for b in adb.books_of(aid)
                 if b.get("stable_book_id") == stable), {})


def _chunks_for(adb: AuthorsDB, books_db: BookDB, aid: str,
                plans: list[dict],
                args) -> tuple[list[dict], list[str], str]:
    """按攻略取候选块。返回 ``(chunks, warnings, 不允许的原因)``。

    ``plans`` 是同一 target 下的整组先验：放行条件看**任意一条**，不是
    只看第一条 —— 同一本书常同时有「读序跋」「读自述」两条先验。
    """
    target = (plans[0].get("target") if plans else "") or ""
    stable, _, scope = target.partition("#")
    book = _book_row(adb, aid, stable)
    book_id = (book.get("book_id") or "").strip()
    if not book_id:
        return [], [], "这本书的 book_id 悬空（重导过），先重跑序跋采集"
    if scope == "paratext":
        chunks, warns = candidate_chunks(books_db, book_id, max_chars=100_000)
        return chunks, warns, ""
    if scope.startswith("chapter:"):
        # 模型按目录点名的章（自序/附录/年表/致辞），不受 position 门限制：
        # 读的是被点名的这一章，依据/用途仍由 details.py 的闸门逐条判。
        chunks, warns = chunks_by_title(
            books_db, book_id, scope.split(":", 1)[1])
        return chunks, warns, ""
    read_as = adb.read_as_of(aid, stable)
    bases = {p.get("expected_basis") or "" for p in plans}
    position = (book.get("position") or "unknown")
    # 正文放行三档：
    #   1. 用户标记 read_as=autobiographical（最强的投射锚）；
    #   2. 作者以 narrator 身份写非虚构（作者自述）；
    #   3. 攻略预期是投射且作者在书里以 narrator/avatar 在场 —— 只读样本、
    #      只产出投射（dossier），不升格成经历。第 3 档让模型的世界知识
    #      （「这本可能是自传体」）能触发一次有界阅读，而不是静默跳过。
    allowed = (read_as == "autobiographical"
               or ("author_statement" in bases and position == "narrator")
               or (bases & {"autobiographical_projection", "fiction_projection"}
                   and position in ("narrator", "avatar")))
    if not allowed:
        return [], [], ("正文只在用户标记 read_as=autobiographical、"
                        "作者以 narrator 写非虚构，或攻略预期是投射且"
                        "作者以 narrator/avatar 在场时才采")
    chunks, warns = body_sample_chunks(books_db, book_id)
    return chunks, warns, ""


async def _run_author(session: aiohttp.ClientSession, cfg: Config,
                      adb: AuthorsDB, books_db: BookDB, author: dict,
                      args, recorder) -> dict:
    aid = author["author_id"]
    name = author.get("canonical_name") or aid
    led: dict = {"name": name, "plans": 0, "obs": 0, "hooks": 0,
                 "closed": 0, "failed": False, "notes": []}
    author_meta = adb.get_author(aid) or author
    if args.plan:
        det = deterministic_plan(books_db, adb, aid, write=not args.dry_run)
        led["plans"] += len(det)
        if cfg.api_key and not args.dry_run:
            sid = f"authorgraph.plan:{session_tag(name)}"
            log_cb = (lambda ev, sid=sid: recorder.record_llm_event(
                ev, session_id=sid, mode="authorgraph.plan")
                ) if recorder else None
            got = await model_plan(session, cfg, adb, books_db, aid,
                                   write=True, log_cb=log_cb)
            if got is None:
                led["notes"].append("模型先验没跑成")
            else:
                led["plans"] += len(got.get("plans") or [])
                led["notes"].extend(got.get("defects") or [])
        print(f"  攻略：规则 {len(det)} 条")
        if args.dry_run:
            return led
    if not args.extract:
        if args.close_hooks:
            return await _close_hooks(session, cfg, adb, author_meta, args, led)
        return led
    plans = adb.plans_for(aid, open_only=not args.force)
    # 黑名单是**目标级**的：用户黑掉过某个 target，模型换个 hypothesis
    # 重跑也不能把它放回来。
    black_targets = {p.get("target") for p in adb.plans_for(
        aid, open_only=False) if p.get("list_type") == "black"}
    # --force 只放开「已完成」，**不放开黑名单**。
    plans = [p for p in plans
             if p.get("list_type") != "black"
             and p.get("target") not in black_targets]
    if not plans:
        print("  没有待执行的攻略 —— 先跑 --plan")
        return led
    groups: dict[str, list[dict]] = {}
    for p in plans:
        groups.setdefault(p.get("target") or "", []).append(p)
    for target, group in groups.items():
        plan = group[0]
        chunks, warns, blocked = _chunks_for(adb, books_db, aid, group, args)
        for w in warns:
            print(f"    ! {w}")
        if blocked:
            print(f"  跳过 {target}：{blocked}")
            continue
        if not chunks:
            print(f"  跳过 {target}：没有候选文本")
            continue
        stable = target.partition("#")[0]
        book = _book_row(adb, aid, stable)
        book_id = (book.get("book_id") or "").strip()
        ident = book_identity(books_db, book_id)
        ident["stable_book_id"] = stable
        author_meta = dict(author_meta, position=book.get("position") or "unknown")
        read_as = adb.read_as_of(aid, stable)
        hooks = adb.hooks_for(aid, status="open", limit=20)
        batches = _split_batches(chunks)
        print(f"  {target}：{len(chunks)} 块 / {len(batches)} 批"
              f"　{len(group)} 条先验　读法={read_as or '未标记'}")
        for i, batch in enumerate(batches, 1):
            if args.dry_run:
                print(f"    [dry-run] 第 {i} 批 {len(batch)} 块，不调模型")
                continue
            sid = (f"authorgraph.details:"
                   f"{session_tag(name + '·' + (ident.get('title') or book_id))}")
            log_cb = (lambda ev, sid=sid: recorder.record_llm_event(
                ev, session_id=sid, mode="authorgraph.details")
                ) if recorder else None
            result = await extract_details(
                session, cfg, ident, author_meta, batch, group, hooks,
                read_as=read_as, log_cb=log_cb)
            if result is None:
                print(f"    ! 第 {i} 批没跑成")
                led["failed"] = True
                continue
            out = store_details(adb, ident, aid, result, plan_id=plan["plan_id"],
                                model=cfg.model)
            # 同一 target 的多条先验共用一趟抽取：主条目记收益，其余只标完成。
            for extra in group[1:]:
                adb.mark_plan_result(extra["plan_id"], status="done")
            led["obs"] += out["observations_added"]
            led["hooks"] += out["hooks_added"]
            print(f"    第 {i} 批：观察 +{out['observations_added']}"
                  f"　钩子 +{out['hooks_added']}"
                  f"　缺陷 {len(out['defects'])}")
            for d in out["defects"][:8]:
                print(f"      ! {d}")
    if args.close_hooks:
        return await _close_hooks(session, cfg, adb, author_meta, args, led)
    return led


async def _close_hooks(session: aiohttp.ClientSession, cfg: Config,
                       adb: AuthorsDB, author: dict, args,
                       led: dict) -> dict:
    aid = author["author_id"]
    hooks = adb.hooks_for(aid, status="open", limit=50)
    print(f"  待闭合钩子：{len(hooks)}")
    if args.dry_run:
        for h in hooks:
            print(f"    [dry-run] {h.get('topic')}：{h.get('missing')}")
        return led
    # 检索词一次生成：模型只负责「去哪搜」，答案仍只认抓到的页面。
    planned = await plan_queries(session, cfg, author, hooks)
    if planned:
        print(f"  检索词：{sum(len(v) for v in planned.values())} 组"
              f"　覆盖 {len(planned)}/{len(hooks)}")
    for h in hooks:
        got = await answer_hook(session, cfg, adb, author, h,
                                queries=planned.get(h.get("hook_id")),
                                log_cb=None)
        if got.get("closed"):
            led["closed"] = led.get("closed", 0) + 1
            print(f"    闭合 {h.get('hook_id')} ← {got.get('url')}")
        else:
            print(f"    未闭合 {h.get('hook_id')}：{got.get('reason')}"
                  f"　{got.get('stats') or ''}")
    return led


async def _run(args) -> int:
    books_db = BookDB(ROOT / "data" / "books.db")
    adb = AuthorsDB(resolve_authors_db_path(ROOT))
    if args.set_plan:
        pid, ltype = args.set_plan
        if ltype not in ("grey", "white", "black"):
            print("名单只能是 grey / white / black")
            return 2
        if not adb.set_plan_list(pid, ltype):
            print(f"没找到攻略 {pid}")
            return 1
        print(f"攻略 {pid} → {ltype}")
        return 0
    if args.plans:
        targets = _target_authors(adb, books_db, args)
        for a in targets:
            aid = a["author_id"]
            name = a.get("canonical_name") or aid
            plans = adb.plans_for(aid, open_only=False)
            print(f"\n{name}　{len(plans)} 条攻略")
            for p in plans:
                print(f"  {p.get('plan_id')}　[{p.get('list_type')}]"
                      f" {p.get('status')}　{p.get('target')}")
                print(f"    {(p.get('hypothesis') or '')[:80]}")
        return 0
    if args.list:
        for a in adb.get_conn().execute(
                "SELECT author_id, canonical_name, identity_status"
                " FROM authors ORDER BY canonical_name").fetchall():
            aid = a["author_id"]
            plans = adb.plans_for(aid, open_only=True)
            obs = adb.observations_for(aid, limit=1000)
            hooks = adb.hooks_for(aid, status="open", limit=1000)
            print(f"  {a['canonical_name'] or aid}　[{a['identity_status']}]"
                  f"　书 {len(adb.books_of(aid))}　攻略 {len(plans)}"
                  f"　观察 {len(obs)}　钩子 {len(hooks)}")
        return 0
    if args.read_as:
        if not args.book:
            print("--read-as 必须配 --book：只给一本书写读法")
            return 2
        ident = None
        rows = books_db.get_conn().execute(
            "SELECT id FROM books WHERE title LIKE ?",
            (f"%{args.book}%",)).fetchall()
        for r in rows:
            ident = book_identity(books_db, r["id"])
            for a in adb.authors_of_book(ident["stable_book_id"]):
                adb.set_read_as(a["author_id"], ident["stable_book_id"],
                                args.read_as)
                print(f"标记读法：{a['canonical_name']} · "
                      f"{ident.get('title') or r['id']} = {args.read_as}")
        if not rows:
            print(f"没找到书名含 {args.book!r} 的书")
            return 1

    targets = _target_authors(adb, books_db, args)
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        print("没有匹配的作者")
        return 1
    print(f"作者库：{resolve_authors_db_path(ROOT)}")
    print(f"目标：{len(targets)} 位作者"
          f"{'（dry-run，不写库/不调模型）' if args.dry_run else ''}\n")
    cfg = _load_config()
    recorder = create_recorder(ROOT)
    total = {"obs": 0, "hooks": 0, "closed": 0, "failed": 0}
    async with aiohttp.ClientSession() as session:
        for i, a in enumerate(targets, 1):
            print(f"[{i}/{len(targets)}] {a.get('canonical_name') or a['author_id']}")
            try:
                led = await _run_author(session, cfg, adb, books_db, a, args,
                                        recorder)
                total["obs"] += led.get("obs", 0)
                total["hooks"] += led.get("hooks", 0)
                total["closed"] += led.get("closed", 0)
                total["failed"] += 1 if led.get("failed") else 0
            except Exception as exc:                     # noqa: BLE001
                print(f"  ! 崩了：{exc}")
                total["failed"] += 1
    print(f"\n{'=' * 56}")
    print(f"观察 +{total['obs']}　钩子 +{total['hooks']}"
          f"　闭合 {total['closed']}　失败 {total['failed']}")
    return 1 if total["failed"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="按攻略采作者细节")
    ap.add_argument("--author", default="", help="只跑一位（名字，模糊匹配）")
    ap.add_argument("--book", default="", help="只跑一本书（书名，模糊匹配）")
    ap.add_argument("--plan", action="store_true", help="生成/刷新攻略")
    ap.add_argument("--plans", action="store_true",
                    help="列出攻略（含 plan_id / 黑白名单）")
    ap.add_argument("--set-plan", nargs=2, default=None,
                    metavar=("PLAN_ID", "grey|white|black"),
                    help="改一条攻略的黑白名单（只有用户能改）")
    ap.add_argument("--extract", action="store_true", help="按攻略抽取")
    ap.add_argument("--close-hooks", action="store_true",
                    help="联网闭合未关闭的钩子")
    ap.add_argument("--read-as", default="", choices=["", "autobiographical"],
                    help="把书标记为按自传读（必须配 --book）")
    ap.add_argument("--all", action="store_true", help="所有作者")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几位")
    ap.add_argument("--force", action="store_true", help="连已完成的攻略也重跑")
    ap.add_argument("--dry-run", action="store_true", help="只看会做什么")
    ap.add_argument("--list", action="store_true", help="只看状态")
    args = ap.parse_args()
    if not (args.plan or args.plans or args.set_plan or args.extract
            or args.list or args.read_as or args.close_hooks):
        args.extract = True
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
