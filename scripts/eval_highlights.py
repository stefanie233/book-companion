"""高亮抽取的 gold set：出候选、算精度/漏检率。

用法：
  python scripts/eval_highlights.py 面纱 --make
  python scripts/eval_highlights.py 面纱 --score

``--make`` 写 `data/eval/<书名>_gold.md`：一半是模型选中的高亮，一半是
同书随机句子（负样本）。人工在文件里把「值得」勾成 ``[x]``，再 ``--score``。

说明：随机样本只能量「漏检率」，量不出严格召回（不知道全书好句子总数）。
所以报告里写「精度」与「随机样本中值得的比例」，别把它叫召回。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from reading.store import ReadingStore, highlights_for             # noqa: E402
from storage.db import BookDB                                     # noqa: E402

EVAL_DIR = ROOT / "data" / "eval"
SENT_RE = re.compile(r"[^。！？!?…]{15,}[。！？!?…]?")
ENTRY_RE = re.compile(r"^## ([FR]\d+) · 第 (\d+) 章 · (model|random)"
                      r"(?:\(([^)]*)\))?$")
MARK_RE = re.compile(r"\[([ xX])\]")


def _pick(books_db: BookDB, needle: str):
    rows = books_db.get_conn().execute(
        "SELECT id, title FROM books ORDER BY title").fetchall()
    hits = [r for r in rows if needle in (r["title"] or "") or r["id"] == needle]
    if len(hits) != 1:
        print(f"「{needle}」匹配到 {len(hits)} 本")
        return None
    return hits[0]


def _sentences(books_db: BookDB, book_id: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for ck in books_db.get_chunks(book_id):
        for s in SENT_RE.findall(ck.content or ""):
            # 从对话中间切出来的残句不能当负样本：去首尾标点，引号必须成对。
            s = " ".join(s.split()).strip("”’\"'「」『』… ")
            if not (15 <= len(s) <= 140):
                continue
            if s.count("「") != s.count("」") or s.count("“") != s.count("”"):
                continue
            out.append((int(ck.chapter_index), s))
    return out


def make(books_db: BookDB, store: ReadingStore, hit: dict, *, n: int) -> int:
    notes = store.notes_for(hit["id"])
    picks = [h for h in highlights_for(notes)
             if h["kind"] in ("literary", "kernel")]
    random.seed(20261030)
    random.shuffle(picks)
    chosen = picks[:max(1, n // 2 + 1)]
    chosen_text = {" ".join(h["text"].split()) for h in chosen}
    pool = [(ch, s) for ch, s in _sentences(books_db, hit["id"])
            if " ".join(s.split()) not in chosen_text]
    randoms = random.sample(pool, min(len(pool), n - len(chosen)))
    entries = [("F", h["chapter"], "model",
                f"{h['kind']}/{h['tag']}" + (f"/{h['who']}" if h["who"] else ""),
                h["text"], h["why"]) for h in chosen]
    entries += [("R", ch, "random", "", text, "") for ch, text in randoms]

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    path = EVAL_DIR / f"{hit['title'].strip()}_gold.md"
    lines = [f"# {hit['title']} 高亮 gold set（{len(entries)} 条）", "",
             "给每条判断：**这段值得用于写作学习或角色分析吗？**",
             "在最后一行把 `[ ]` 改成 `[x]` 表示「值得」。",
             "不要看来源（F/R），不要互相比较，只按单条价值判断。", ""]
    for i, (prefix, chapter, source, meta, text, why) in enumerate(entries, 1):
        lines.append(f"## {prefix}{i:02d} · 第 {chapter} 章 · {source}"
                     + (f"({meta})" if meta else ""))
        lines.append(f"> 「{text}」")
        if why:
            lines.append(f"模型理由：{why}")
        lines.append("值得？ [ ]")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"写出 {path}（model {len(chosen)} / random {len(randoms)}）")
    return 0


def score(store: ReadingStore, hit: dict, filename: str) -> int:
    path = EVAL_DIR / f"{filename}_gold.md"
    if not path.exists():
        print(f"没有 {path}，先跑 --make 再人工标注")
        return 2
    entries: list[dict] = []
    cur: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = ENTRY_RE.match(line.strip())
        if m:
            cur = {"id": m.group(1), "chapter": int(m.group(2)),
                   "source": m.group(3), "meta": m.group(4) or "",
                   "worth": None}
            entries.append(cur)
            continue
        if cur is not None and cur["worth"] is None:
            mk = MARK_RE.search(line)
            if mk and "值得" in line:
                cur["worth"] = mk.group(1).lower() == "x"
    labeled = [e for e in entries if e["worth"] is not None]
    model = [e for e in labeled if e["source"] == "model"]
    rand = [e for e in labeled if e["source"] == "random"]
    print(f"已标注 {len(labeled)}/{len(entries)} 条")
    if not labeled:
        print("还没有标注，把 [ ] 改成 [x] 再跑")
        return 1
    if model:
        good = sum(1 for e in model if e["worth"])
        print(f"精度：模型选中 {len(model)} 条，值得 {good} 条 → "
              f"{good / len(model):.0%}")
    if rand:
        good_r = sum(1 for e in rand if e["worth"])
        print(f"随机样本：{len(rand)} 条，值得 {good_r} 条 → "
              f"{good_r / len(rand):.0%}（越高说明模型漏得越多）")
        if model:
            print(f"漏检代理：随机样本里值得的比例 = {good_r / len(rand):.0%}")
    notes = store.notes_for(hit["id"])
    dist: dict[str, int] = {}
    for h in highlights_for(notes):
        key = f"{h['kind']}/{h['tag']}"
        dist[key] = dist.get(key, 0) + 1
    print("\n全量分布：")
    for k, v in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="高亮 gold set")
    p.add_argument("book")
    p.add_argument("--make", action="store_true")
    p.add_argument("--score", action="store_true")
    p.add_argument("--n", type=int, default=25)
    args = p.parse_args()
    books_db = BookDB(ROOT / "data" / "books.db")
    hit = _pick(books_db, args.book)
    if hit is None:
        books_db.close()
        return 2
    store = ReadingStore(ROOT / "data" / "reading.db")
    try:
        if args.make:
            return make(books_db, store, hit, n=args.n)
        return score(store, hit, hit["title"].strip())
    finally:
        store.close()
        books_db.close()


if __name__ == "__main__":
    raise SystemExit(main())
