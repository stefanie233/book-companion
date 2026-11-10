"""看一次阅读 run 的账与模型实际在跑什么。

用法：
  python scripts/inspect_reading_run.py 面纱
  python scripts/inspect_reading_run.py 面纱 --chapter 7
  python scripts/inspect_reading_run.py 面纱 --chapter 7 --calls
  python scripts/inspect_reading_run.py 窄门 --stats

数据来源：`data/reading.db`（笔记/账）+ `data/context_log.db`（飞行记录仪：
模型收到的 system、任务、工具调用、思维链与回复）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from flight_recorder.recorder import ContextRecorder           # noqa: E402
from reading.store import ReadingStore                         # noqa: E402
from storage.db import BookDB                                  # noqa: E402


def _pick(books_db: BookDB, needle: str):
    rows = books_db.get_conn().execute(
        "SELECT id, title FROM books ORDER BY title").fetchall()
    hits = [r for r in rows if needle in (r["title"] or "") or r["id"] == needle]
    if not hits:
        print(f"没有匹配「{needle}」的书")
        return None
    if len(hits) > 1:
        print(f"「{needle}」匹配到 {len(hits)} 本，请给更具体名字")
        return None
    return hits[0]


def _latest_run(store: ReadingStore, book_id: str) -> dict | None:
    """最近一次**有笔记**的 run。

    被 Ctrl+C / TaskStop 打断的 run 会永久停在 status=running、章节 0，
    如果只看 time_created 最新的一条，检查器会指着一个空壳。优先取有
    笔记的那条，全都为空时才退回最新一条。
    """
    rows = store.get_conn().execute(
        "SELECT * FROM reading_runs WHERE book_id=? "
        "ORDER BY time_created DESC", (book_id,)).fetchall()
    if not rows:
        return None
    for row in rows:
        if int(row["chapters_done"] or 0) > 0:
            return dict(row)
    return dict(rows[0])


def _stats(store: ReadingStore, run: dict) -> None:
    rows = store.get_conn().execute(
        "SELECT * FROM reading_notes WHERE run_id=? ORDER BY chapter_index",
        (run["run_id"],)).fetchall()
    tin = sum(int(r["tokens_in"] or 0) for r in rows)
    cached = sum(int(r["tokens_cached"] or 0) for r in rows)
    tout = sum(int(r["tokens_out"] or 0) for r in rows)
    reason = sum(int(r["tokens_reasoning"] or 0) for r in rows)
    unverified = quotes = chars = rels = 0
    for r in rows:
        try:
            n = json.loads(r["note_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            n = {}
        quotes += len(n.get("quotes") or [])
        unverified += int(n.get("unverified_quotes") or 0)
        chars += len(n.get("characters") or [])
        rels += len(n.get("relations") or [])
    hit = cached / max(1, tin)
    print(f"run  {run['run_id']}  status={run['status']}  "
          f"prompt={run.get('prompt_version') or '-'}")
    print(f"章节 {len(rows)} · 输入 {tin:,} · 命中 {cached:,} ({hit:.1%}) · "
          f"未命中 {tin - cached:,}")
    print(f"输出 {tout:,}（推理 {reason:,}）")
    print(f"引文 {quotes} 条 · 未核 {unverified} 条 · "
          f"角色 {chars} 人次 · 关系 {rels} 条")
    try:
        tot = json.loads(run.get("totals_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        tot = {}
    if tot.get("failures"):
        print("失败章:", "、".join(str(f.get("chapter")) for f in
                                   tot["failures"]))
    if tot.get("resets"):
        print("换窗次数:", tot["resets"])
    for r in rows:
        pin = int(r["tokens_in"] or 0)
        ch = pin and int(r["tokens_cached"] or 0) / pin
        print(f"  第 {r['chapter_index']:>3} 章  in={pin:>6} "
              f"hit={ch:>5.0%}  out={r['tokens_out']:>5} "
              f"(r {r['tokens_reasoning']:>4})  "
              f"{(r['compact'] or '')[:36]}")


def _print_note(note: dict) -> None:
    print("scene:", note.get("scene") or "")
    for c in note.get("characters") or []:
        alias = "（别名：" + "、".join(c.get("aliases") or []) + "）" \
            if c.get("aliases") else ""
        print(f"角色 {c.get('name')}{alias}")
        for key, label in (("wants", "想要"), ("fears", "顾虑"),
                           ("knows", "知道"), ("state", "处境")):
            if c.get(key):
                print(f"  {label}：{c[key]}")
    for r in note.get("relations") or []:
        print(f"关系 {r.get('from')} —({r.get('kind')})→ {r.get('to')}："
              f"{r.get('note') or ''}")
    for f in note.get("facts") or []:
        print("事实:", f)
    for q in note.get("quotes") or []:
        print(f"引文 [{q.get('chapter')}:{q.get('chunk')}]「{q.get('text')}」"
              f" —— {q.get('why') or ''}")
    for t in note.get("threads") or []:
        print("线索:", t)
    print("解读:", note.get("reading") or "")
    print("compact:", note.get("compact") or "")


def _chapter(store: ReadingStore, rec: ContextRecorder, run: dict,
             chapter: int, show_calls: bool) -> None:
    row = store.get_conn().execute(
        "SELECT * FROM reading_notes WHERE run_id=? AND chapter_index=?",
        (run["run_id"], chapter)).fetchone()
    if not row:
        print(f"第 {chapter} 章没有笔记")
        return
    print(f"===== 第 {chapter} 章 {row['chapter_title']} =====")
    _print_note(json.loads(row["note_json"] or "{}"))
    if not show_calls:
        return
    sid = f"reading:{run['run_id']}"
    rounds = [r for r in rec.list_rounds(sid)
              if int(r.get("turn_index") or 0) == chapter]
    for r in rounds:
        full = rec.get_round(r["id"]) or {}
        msgs = json.loads(full.get("messages_json") or "[]")
        last_user = next((m.get("content") or "" for m in reversed(msgs)
                          if m.get("role") == "user"), "")
        print(f"\n--- call {r['react_round']}  {r.get('latency_ms')}ms ---")
        print("system 长度:", len(full.get("system_prompt") or ""))
        print("任务:", (last_user or "")[:600].replace("\n", " "))
        if full.get("tool_calls_json"):
            print("工具:", full["tool_calls_json"][:600])
        resp = (full.get("response_text") or "")
        print("模型回复:", resp[:1_200].replace("\n", " "))


def main() -> int:
    p = argparse.ArgumentParser(description="检查阅读 run 的账与模型轨迹")
    p.add_argument("book")
    p.add_argument("--chapter", type=int, default=0)
    p.add_argument("--calls", action="store_true",
                   help="打印该章的飞行记录仪调用（任务/工具/思维链）")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args()

    books_db = BookDB(ROOT / "data" / "books.db")
    hit = _pick(books_db, args.book)
    books_db.close()
    if hit is None:
        return 2
    store = ReadingStore(ROOT / "data" / "reading.db")
    run = _latest_run(store, hit["id"])
    if not run:
        print("这个书还没有阅读 run")
        store.close()
        return 1
    if args.stats or not args.chapter:
        _stats(store, run)
    if args.chapter:
        rec = ContextRecorder(ROOT / "data" / "context_log.db")
        _chapter(store, rec, run, args.chapter, args.calls)
        rec.close()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


