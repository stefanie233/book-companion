"""把 book_chapters.spine_index 回填成 OPF 坐标（决策 146）。

旧导入把 reader 内部的**压缩** spine 下标写进了库：`EpubReader` 会跳过
封面/目录等 item，而前端渲染器 epub.js 的 sectionIndex 是 OPF itemref 的
**原始**序号。跳项之后的章节整体偏移，/api/sync 的「结构定章」静默落空，
只能退回文字指纹（跨章/短章时会认错）。

判定方式：看章节正文开头是否出现在该 spine item 的正文里，比较「按压缩
坐标读」和「按 OPF 坐标读」两种解释，逐本投票。默认 dry-run，`--apply`
才写库；重复执行安全（已修过的书会投出 opf）。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from file_readers.epub_reader import open_book  # noqa: E402
from storage.db import TABLE_BOOK_CHAPTERS, BookDB  # noqa: E402

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub("", s or "")


def _body_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html or "")
    m = re.search(r"(?is)<body[^>]*>(.*)</body>", html)
    return _norm(re.sub(r"<[^>]+>", "", m.group(1) if m else html))


def _sample(rows: list, limit: int = 6) -> list:
    """均匀抽样：偏移是整段的，不同位置的投票比只看头尾可靠。"""
    if len(rows) <= limit:
        return rows
    step = len(rows) / float(limit)
    return [rows[int(i * step)] for i in range(limit)]


def _vote(reader, chapters: list) -> tuple[list[int], dict, list[str]]:
    """对抽样章节投票：库里存的是压缩坐标还是 OPF 坐标？"""
    mapping = list(reader._spine_opf_index)
    rev = {opf: comp for comp, opf in enumerate(mapping)}
    cache: dict[int, str] = {}

    def spine_text(comp: int | None) -> str:
        if comp is None or comp < 0 or comp >= len(mapping):
            return ""
        if comp not in cache:
            try:
                cache[comp] = _body_text(reader._read_spine_html(comp))
            except Exception:
                cache[comp] = ""
        return cache[comp]

    votes = {"compressed": 0, "opf": 0, "both": 0, "unknown": 0}
    misses: list[str] = []
    for ch in _sample(chapters):
        stored = int(ch.spine_index)
        head = _norm(ch.content)[:24]
        if len(head) < 10 or stored < 0:
            continue
        # 压缩坐标只在 mapping 范围内成立；OPF 坐标走反向表，末项也能判。
        ok_compressed = stored < len(mapping) and head in spine_text(stored)
        ok_opf = head in spine_text(rev.get(stored))
        if ok_compressed and ok_opf:
            votes["both"] += 1
        elif ok_compressed:
            votes["compressed"] += 1
        elif ok_opf:
            votes["opf"] += 1
        else:
            votes["unknown"] += 1
            misses.append(f"ch{ch.chapter_index} spine={stored} {head[:12]!r}")
    return mapping, votes, misses


def _plan(mapping: list[int], chapters: list) -> list[tuple[int, int, int]]:
    """按压缩→OPF 映射，算出需要改的 (chapter_index, old, new)。"""
    changes = []
    for ch in chapters:
        old = int(ch.spine_index)
        if 0 <= old < len(mapping):
            new = mapping[old]
            if new != old:
                changes.append((int(ch.chapter_index), old, new))
    return changes


def _infer_legacy(reader, chapters: list, mapping: list[int]) -> list[tuple[int, int, int]]:
    """旧导入的 -1 行：按正文开头反推该章来自哪个 spine item。

    只认「恰好一个 item 含这段开头」的章；多义或找不到的都留 -1，交给
    指纹兜底，不猜。返回 (chapter_index, -1, opf_index)。
    """
    texts: dict[int, str] = {}
    for comp in range(len(mapping)):
        try:
            texts[comp] = _body_text(reader._read_spine_html(comp))
        except Exception:
            texts[comp] = ""
    fills = []
    for ch in chapters:
        head = _norm(ch.content)[:24]
        if len(head) < 10:
            continue
        hits = sorted({mapping[c] for c, t in texts.items() if t and head in t})
        if len(hits) == 1:
            fills.append((int(ch.chapter_index), -1, hits[0]))
    return fills


def _apply(db, book_id: str, changes: list[tuple[int, int, int]]) -> None:
    conn = db.get_conn()
    for ch_idx, _old, new in changes:
        conn.execute(
            f"UPDATE {TABLE_BOOK_CHAPTERS} SET spine_index=? "
            "WHERE book_id=? AND chapter_index=?",
            (new, book_id, ch_idx),
        )
    conn.commit()


def _run_book(db, book, *, apply: bool) -> int:
    """处理一本书，返回实际（或 dry-run 下计划）改动行数。"""
    epub = ROOT / "data" / "bookbank" / f"{book.id}.epub"
    label = f"{book.id} 《{(book.title or '')[:16]}》"
    if not epub.exists():
        print(f"  跳过 {label}: 找不到 {epub.name}")
        return 0

    reader = open_book(epub)
    try:
        reader._ensure_loaded()
        all_chs = db.get_chapters(book.id)
        if not all_chs:
            print(f"  跳过 {label}: 没有章节")
            return 0
        mapped, legacy = [], []
        for c in all_chs:
            si = int(c.spine_index if c.spine_index is not None else -1)
            (mapped if si >= 0 else legacy).append(c)

        mapping = list(reader._spine_opf_index)
        identity = mapping == list(range(len(mapping)))
        changes: list[tuple[int, int, int]] = []

        if mapped:
            _, votes, misses = _vote(reader, mapped)
            summary = (f"compressed={votes['compressed']} opf={votes['opf']} "
                       f"both={votes['both']} unknown={votes['unknown']}")
            if identity:
                print(f"  坐标一致 {label}: {summary}")
            elif votes["opf"] and not votes["compressed"]:
                print(f"  已是 OPF 坐标 {label}: {summary}")
            elif votes["compressed"]:
                changes = _plan(mapping, mapped)
                print(f"  需回填 {label}: {summary} → {len(changes)} 行 "
                      f"(OPF spine={len(mapping)})")
            else:
                print(f"  无法判定 {label}: {summary}")
                for m in misses[:3]:
                    print(f"      未命中样本 {m}")

        if legacy:
            fills = _infer_legacy(reader, legacy, mapping)
            changes += fills
            if fills:
                print(f"  旧章补映射 {label}: {len(fills)}/{len(legacy)} 行"
                      "按正文唯一命中反推")
            else:
                print(f"  旧章仍无法反推 {label}: {len(legacy)} 行保留 -1"
                      "（走指纹兜底）")

        if not changes:
            return 0
        for ch_idx, old, new in changes[:4]:
            print(f"      ch{ch_idx}: {old} -> {new}")
        if len(changes) > 4:
            print(f"      ... 另 {len(changes) - 4} 行")
        if apply:
            _apply(db, book.id, changes)
        return len(changes)
    finally:
        reader.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="真正写库（默认只演练，不修改 books.db）")
    ap.add_argument("--book", help="只处理指定 book_id")
    ap.add_argument("--db", default=str(ROOT / "data" / "books.db"))
    args = ap.parse_args()

    db_path = Path(args.db)
    if args.apply:
        # 一次写库前备份整库；回填只改 spine_index，但书表是用户数据。
        # 库跑在 WAL 下，直接 copy .db 会漏掉最新事务——用 sqlite 的
        # backup API 取一致快照（服务在跑也能用）。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(backup))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        print(f"已备份 {db_path.name} -> {backup.name}")

    db = BookDB(db_path)
    if args.book:
        one = db.get_book(args.book)
        if one is None:
            print(f"找不到书 {args.book}")
            return 1
        books = [one]
    else:
        books = db.list_books(limit=10000)

    print(f"{'写库' if args.apply else '演练（加 --apply 才写库）'}，"
          f"共 {len(books)} 本书")
    total = 0
    for book in books:
        try:
            total += _run_book(db, book, apply=args.apply)
        except Exception as exc:  # 单本失败不阻断其余书
            print(f"  失败 {book.id}: {type(exc).__name__}: {exc}")
    verb = "已修改" if args.apply else "计划修改"
    print(f"完成：{verb} {total} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


