# -*- coding: utf-8 -*-
"""批量探测 method=None 旧书的正确切分 method（零 LLM token）。

对每本 EPUB，实测三种切法（h_tags / css_class 候选 / font_size 候选），
用「切出章数与 TOC 标签的匹配率」打分，选最优：
  h_tags      → selector=''
  css_class   → 候选 = 所有出现≥2次的非噪声类名
  font_size   → 候选 = 源 HTML 中实际出现的 <font size=N> />

打分 = 匹配到 TOC 的章数 / 总章数；章数过碎(<2)或全不匹配则低分。

用法: python scripts/detect_split_method.py             # 仅 method=None 的书
      python scripts/detect_split_method.py --book 窄门  # 单本
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from file_readers.epub_reader import EpubReader
from file_readers.html_skeleton import split_html_by_heading, _clean_inline_tags
from services.library_service import _title_match_score
from storage.db import BookDB

_NOISE_CLASS_RE = re.compile(
    r"^(?:calibre\d*|sgc-\d+|sgc-toc-\d+|block_\d+|tapuz_.*|"
    r"filepos\d*|fs-\d+|id\d+|index\d+|list\d+|item\d+|"
    r"para\d+|sec\d+|sect\d+|sub\d+|txt\d+)$",
    re.IGNORECASE,
)


def read_spine(reader: EpubReader, idx: int) -> str | None:
    try:
        return reader._read_spine_html(idx)
    except (KeyError, FileNotFoundError):
        return None


def all_spines(reader: EpubReader) -> list[str]:
    out = []
    for i in range(len(reader._spine_ids)):
        h = read_spine(reader, i)
        if h:
            out.append(h)
    return out


def _tag_inner(m: re.Match) -> str:
    return _clean_inline_tags(m.group(2))


def score(reader: EpubReader, method: str, selector: str, toc: list[dict]) -> dict:
    """对 (method, selector) 实测切分，返回匹配统计。"""
    labels = {e.get("label", "") for e in toc if e.get("label")}
    total = 0
    matched = 0
    n_spines_cut = 0
    for h in all_spines(reader):
        sections = split_html_by_heading(h, method, selector)
        if sections and sections != [("", h, "")]:
            n_spines_cut += 1
        for heading_text, _body, _tag in sections:
            if not heading_text:
                continue
            if len(heading_text) > 40:  # 标题过长不像章节标题
                continue
            total += 1
            if any(_title_match_score(heading_text, lab) >= 2
                   for lab in labels if lab):
                matched += 1
    if total == 0:
        return {"method": method, "selector": selector, "total": 0,
                "matched": 0, "rate": 0.0, "spines_cut": n_spines_cut}
    return {"method": method, "selector": selector, "total": total,
            "matched": matched, "rate": matched / total, "spines_cut": n_spines_cut}


def candidate_css_classes(reader: EpubReader) -> list[str]:
    """收集源 HTML 中出现在短块上的 class（出现≥2次、非噪声）。"""
    cnt: Counter = Counter()
    for h in all_spines(reader):
        for m in re.finditer(r'<(\w+)\b[^>]*class="([^"]+)"[^>]*>(.*?)</\1>',
                             h, re.DOTALL | re.IGNORECASE):
            inner = re.sub(r"<[^>]+>", "", m.group(3)).strip()
            if not inner or len(inner) > 40:
                continue
            for cls in m.group(2).split():
                if not _NOISE_CLASS_RE.match(cls.lower()) and len(cls) <= 40:
                    cnt[cls] += 1
    return [c for c, n in cnt.most_common() if n >= 2]


def candidate_font_sizes(reader: EpubReader) -> list[str]:
    cnt: Counter = Counter()
    for h in all_spines(reader):
        for m in re.finditer(r'<font\b[^>]*size\s*=\s*["\']?(\d+)',
                             h, re.IGNORECASE):
            cnt[m.group(1)] += 1
    return [s for s, _ in cnt.most_common() if int(s) >= 3 and int(s) <= 7]


def best_method(reader: EpubReader, toc: list[dict]) -> dict:
    cands: list[dict] = []

    hs = score(reader, "h_tags", "", toc)
    cands.append(hs)

    for cls in candidate_css_classes(reader)[:40]:
        cands.append(score(reader, "css_class", cls, toc))

    for fs in candidate_font_sizes(reader)[:8]:
        cands.append(score(reader, "font_size", fs, toc))

    # 打分: 匹配率优先，其次切出的章数多且 spine 被切的比例高
    def key(c: dict) -> tuple:
        return (c["matched"], round(c["rate"], 3), c["total"])
    best = max(cands, key=key) if cands else {"method": "?", "total": 0}
    return best, cands


def sniff(reader: EpubReader, book_id: str) -> None:
    toc = reader._toc
    n_spine = len(reader._spine_ids)
    best, cands = best_method(reader, toc)

    print(f"  spine={n_spine}  最优: {best.get('method','?')}"
          f"@{best.get('selector','')}  切出章 {best.get('total',0)} "
          f"匹配{best.get('matched',0)} ({best.get('rate',0):.0%})")
    if best.get("matched", 0) < 3:
        # 候选不理想，列出前 6 个候选换人工判断
        for c in sorted(cands, key=lambda x: (x["matched"], x["total"]),
                        reverse=True)[:6]:
            print(f"    └ 候选 {c['method']}@{c['selector']!r}: "
                  f"章{c['total']} 匹配{c['matched']} "
                  f"({c['rate']:.0%}) spine被切{c['spines_cut']}/{n_spine}")
    return best


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default=None, help="书名子串（默认全部 method=None 的 EPUB）")
    args = ap.parse_args()

    db = BookDB(Path("data/books.db"))
    conn = db.get_conn()

    if args.book:
        rows = conn.execute(
            "SELECT id, title, source_path FROM books WHERE title LIKE ?",
            (f"%{args.book}%",)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, source_path FROM books WHERE "
            "coalesce(json_extract(metadata_json,'$.epub_import_method'),'')='' "
            "AND source_path LIKE '%.epub'").fetchall()

    if not rows:
        print("!! 没有待探测的书")
        return 1

    for bid, title, path in rows:
        if not (path and Path(path).exists()):
            print(f"\n{bid} {title[:18]}  !! 源文件缺失: {path}")
            continue
        print(f"\n{bid} {title[:22]}")
        try:
            r = EpubReader(path)
            r._ensure_loaded()
        except Exception as exc:
            print(f"  !! 打开失败: {exc}")
            continue
        try:
            sniff(r, bid)
        except Exception as exc:
            print(f"  !! 探测异常: {exc!r}")
        finally:
            r.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())