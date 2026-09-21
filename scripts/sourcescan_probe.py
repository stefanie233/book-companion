"""来源层结构摸底探针（v0 · 纯机械 · 零 LLM token）。

用法:
  python scripts/sourcescan_probe.py                # 默认《窄门》
  python scripts/sourcescan_probe.py --book <子串>
  python scripts/sourcescan_probe.py --all          # 全库扫描（不强制）

对目标书输出四节摸底:
  ① 体例段聚类    —— 逐 spine 块样式签名分组，看"视觉断点"是否机械可提取、跨章是否连贯
  ② TOC 树形      —— 解析 navPoint/nav 嵌套深度，对比扁平 _toc，看集类(篇/章)层次在不在
  ③ 段落重构校验  —— 源 HTML 段落流 vs 导入后 chapter.content 重剖段落流，标失配
  ④ 锚点/注释密度 —— epub:type 分布、noteref→id target 引用对，看锚图值得不值得建

产出是数据，不是判决。设计决策（体例字段/TOC 树/锚点）基于此探针的真实输出。
"""
from __future__ import annotations

import collections
import difflib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.db import BookDB
from storage.db_base import _new_id
from file_readers.epub_reader import EpubReader, _format_html
from services.library_service import _extract_paragraphs, _build_chunks_for_chapter

BLOCK_TAGS = ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote")


# ── 0. 找书 ────────────────────────────────────────────────────────────────


def find_book(db: BookDB, sub: str):
    cur = db.get_conn().cursor()
    cur.execute("SELECT id, title, author, source_path FROM books WHERE title LIKE ? LIMIT 5",
                (f"%{sub}%",))
    rows = cur.fetchall()
    # 命中多本时优先取源文件可读的
    for r in rows:
        if r[3] and Path(r[3]).exists():
            return r
    return rows[0] if rows else None


# ── ① 体例段聚类 ───────────────────────────────────────────────────────────


def read_spine(reader: EpubReader, idx: int) -> str | None:
    """读 spine HTML；zip 引用缺失/损坏时容错跳过（真实脏书常见）。"""
    try:
        return reader._read_spine_html(idx)
    except (KeyError, FileNotFoundError):
        return None


def block_signature(m: re.Match) -> str:
    """块的样式签名：tag|class|style —— 同一签名的连续块 = 视觉一致的段。"""
    attrs = m.group(2) or ""
    cls = re.search(r'class="([^"]+)"', attrs)
    style = re.search(r'style="([^"]+)"', attrs)
    return f"{m.group(1)}|{cls.group(1) if cls else ''}|{style.group(1) if style else ''}"


def collect_blocks(html: str) -> list[tuple[str, str]]:
    """逐块(tag,class|style)收集，连同锚点 id 和 epub:type。返回 (sig, snippet)。"""
    out: list[tuple[str, str]] = []
    for tag in BLOCK_TAGS:
        for m in re.finditer(
            rf"<{tag}\b([^>]*)>(.*?)</{tag}>", html, re.DOTALL | re.IGNORECASE
        ):
            inner = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if not inner:
                continue
            out.append((block_signature(m), inner[:28].replace("\n", " ")))
    return out


def compact_css(rules: dict[str, str], sig: str) -> str:
    """合并一个块的所有 class 的 CSS 关键视觉属性（text-align/font-*）。"""
    classes = [c for c in sig.split("|")[1].split() if c]
    bits: list[str] = []
    seen: set[str] = set()
    for key in ("text-align", "font-size", "font-weight", "font-family"):
        for cls in classes:
            mm = re.search(rf"{key}\s*:\s*([^;]+)", rules.get(cls, ""))
            if mm and mm.group(1).strip() not in seen:
                bits.append(f"{key}={mm.group(1).strip()}")
                seen.add(mm.group(1).strip())
    return "; ".join(bits)


def probe_style_clusters(reader: EpubReader) -> None:
    print("\n── ① 体例段聚类 ────────────────────────────────")
    sig_spines: dict[str, list[int]] = defaultdict(list)
    sig_count: dict[str, int] = collections.Counter()
    sig_sample: dict[str, str] = {}
    css_rules: dict[str, str] = {}

    # 收集 CSS（内联 style 块 + 外部 .css）
    for name in reader._zip.namelist():
        if name.endswith(".css"):
            css = reader._zip.read(name).decode("utf-8", "ignore")
            for cm in re.finditer(r"\.([\w-]+)\s*\{([^}]*)\}", css):
                css_rules[cm.group(1)] = cm.group(2)

    non_p_sigs: set[str] = set()
    for idx in range(len(reader._spine_ids)):
        html = read_spine(reader, idx)
        if html is None:
            continue
        for sig, snippet in collect_blocks(html):
            sig_spines[sig].append(idx)
            sig_count[sig] += 1
            if sig not in sig_sample and sig_count[sig] == 1:
                sig_sample[sig] = snippet
            if not sig.startswith("p|"):
                non_p_sigs.add(sig)

    total = sum(sig_count.values())
    print(f"spine {len(reader._spine_ids)} 块，样式签名 {len(sig_count)} 种，覆盖 {total} 段")
    print(f"{'签名':<44} {'段数':>6}  {'spine跨度':>8} | 样本 ｜ CSS 关键属性")
    for sig, cnt in sig_count.most_common(12):
        spin_gen = (max(sig_spines[sig]) - min(sig_spines[sig])) + 1
        span = f"{min(sig_spines[sig])}-{max(sig_spines[sig])}"
        css = compact_css(css_rules, sig)
        print(f"  {sig[:42]:<44} {cnt:>6}  {span:>8} | "
              f"{sig_sample[sig][:22]!r} ｜ {css[:40]}")
    if len(sig_count) > 12:
        print(f"  … 其余 {len(sig_count) - 12} 种更小众签名省略")
    if non_p_sigs:
        print(f"\n  非 <p> 块签名 {len(non_p_sigs)} 种（div/h/blockquote…）——影响段落原子性")
        for s in sorted(non_p_sigs)[:6]:
            print(f"    {s[:60]}")

    # 最重的签名里，哪些跨 spine 连续（= 全书性体例，如窄门 calibre19 信件）
    heavy = [(s, n) for s, n in sig_count.most_common(4)]
    print("\n  重签名跨章连贯性（体重>全书 5%）：")
    for sig, n in heavy:
        if n < total * 0.05:
            continue
        spines = sorted(set(sig_spines[sig]))
        chunks = " ".join(f"[{a}-{b}]" for a, b in _runs(spines))
        print(f"    {sig.split('|')[1] or sig.split('|')[0]!r}: {n}段 {chunks}")


def _runs(seq: list[int]) -> list[tuple[int, int]]:
    """连续整数归组为 [首,尾] 区间列表。"""
    if not seq:
        return []
    out: list[tuple[int, int]] = []
    start = prev = seq[0]
    for x in seq[1:]:
        if x == prev + 1:
            prev = x
            continue
        out.append((start, prev))
        start = prev = x
    out.append((start, prev))
    return out


# ── ② TOC 树形 ────────────────────────────────────────────────────────────


def probe_toc_tree(reader: EpubReader) -> None:
    print("\n── ② TOC 树形 ────────────────────────────────────")
    flat = reader._toc
    print(f"现有扁平 _toc：{len(flat)} 条" +
          (f"，样label: {[e['label'][:12] for e in flat[:6]]}" if flat else ""))

    # NCX navPoint 嵌套深度（stack 计数，不依赖解析器）
    max_depth = 0
    nodes: list[tuple[int, str]] = []
    for name in reader._zip.namelist():
        if name.endswith((".ncx")):
            x = reader._zip.read(name).decode("utf-8", "ignore")
            stack = []
            for tm in re.finditer(r"<(/?)\s*navPoint\b", x, re.I):
                if tm.group(1) == "/":
                    if stack:
                        stack.pop()
                else:
                    stack.append(1)
                    depth = len(stack)
                    max_depth = max(max_depth, depth)
                    # 取其标题
                    seg = x[tm.start(): tm.start() + 1200]
                    lm = re.search(r"<text>(.*?)</text>", seg, re.I)
                    if lm:
                        nodes.append((depth, re.sub(r"<[^>]+>", "", lm.group(1))[:20]))
            break
    for name in reader._zip.namelist():
        if name.endswith((".xhtml", ".html")) and "nav" in name.lower():
            x = reader._zip.read(name).decode("utf-8", "ignore")
            tocsec = re.search(r'<nav[^>]*toc[^>]*>(.*?)</nav>', x, re.DOTALL | re.I)
            if tocsec:
                depth, ol = _ol_depth(tocsec.group(1))
                max_depth = max(max_depth, depth)
                nodes = nodes or [(d, l) for d, l in _ol_nodes(tocsec.group(1))]
            break

    if nodes:
        print(f"原始嵌套最大深度：{max_depth} 级，顶层 {sum(1 for d, _ in nodes if d == 1)} 条")
        for d, label in nodes[:10]:
            print(f"  {'  ' * (d - 1)}L{d} {label!r}")
        if len(nodes) > 10:
            print(f"  … 共 {len(nodes)} 节点")
    else:
        print("  未找到 navPoint/nav 树（无层级信息可用）")


def _ol_depth(html: str) -> tuple[int, list[range]]:
    stack = 0
    maxd = 0
    for m in re.finditer(r"<(/?)\s*ol\b|</?li\b", html, re.I):
        if m.group(0).startswith("<ol"):
            stack += 1
            maxd = max(maxd, stack)
        elif m.group(0).startswith("</ol"):
            stack = max(0, stack - 1)
    return maxd, []


def _ol_nodes(html: str) -> list[tuple[int, str]]:
    # 简化：顶层 <li> 的 label
    out = []
    for m in re.finditer(r"<li\b[^>]*>(.*?)</li>", html, re.DOTALL | re.I):
        inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if inner:
            out.append((1, inner[:20]))
    return out


# ── ③ 段落重构校验 ─────────────────────────────────────────────────────────


def probe_paragraph_rebuild(reader: EpubReader, db: BookDB, book_id: str) -> None:
    print("\n── ③ 段落重构校验 ────────────────────────────────")
    # A: 源 HTML → 段落流（_format_html 是导入路径的"洗文本"）
    html_paras: list[tuple[int, str]] = []
    for idx in range(len(reader._spine_ids)):
        html = read_spine(reader, idx)
        if html is None:
            continue
        paras = _format_html(html, as_paragraphs=True)
        for p in paras:
            html_paras.append((idx, p))
    a_text = [p for _, p in html_paras]

    # B: 导入后章 content → 重剖段落流
    chapters = sorted(db.get_chapters(book_id), key=lambda c: c.chapter_index)
    b_text: list[str] = []
    for ch in chapters:
        b_text.extend(_extract_paragraphs(ch.content))

    print(f"源 HTML 段落流：{len(a_text)} 段 ｜ 重剖段落流：{len(b_text)} 段")

    # 段内含单换行（<br> 折叠）的段落个数（原子性告警）
    with_nl = sum(1 for p in a_text if "\n" in p)
    print(f"源段内含换行 <br> 折叠：{with_nl} 段"
          "（不受 \\n\\n 原子性保护的段落）")

    if a_text == b_text:
        print("[一致] 源段落流 == 重剖段落流 —— 段落号快照可从纯文本无损重建 ✓")
    else:
        sm = difflib.SequenceMatcher(a=a_text, b=b_text, autojunk=False)
        print("[失配] 源段落流 ≠ 重剖段落流 —— 找首个差异:")
        # 只取 ops 前几个，避免 O(n²) 全diff
        opcodes = sm.get_opcodes()
        shown = 0
        for op, i1, i2, j1, j2 in opcodes:
            if op == "equal":
                continue
            print(f"  {op} 源[{i1}:{i2}] 重剖[{j1}:{j2}]:")
            for x in a_text[i1:i1 + 2]:
                print(f"    源  {x[:48]!r}")
            for x in b_text[j1:j1 + 2]:
                print(f"    剖  {x[:48]!r}")
            shown += 1
            if shown >= 4:
                print("  … 后续差异省略")
                break
        ratio = sm.ratio()
        print(f"  序列相似度 {ratio:.3f} —— {'高相似(可能只是标题注入/个别拼接)'
              if ratio > 0.9 else '低相似(段落切法系统性不同!)'}")

    # 新切分路径演示：任一章 content 重新 _build_chunks_for_chapter 会带上段落身份
    if chapters:
        demo = chapters[0]
        rows = _build_chunks_for_chapter(book_id, demo.chapter_index, demo.content)
        print(f"\n  新切分路径演示（第{len(rows)}章 → {len(rows)} chunk，均已带段落身份）:")
        for r in rows[:2]:
            print(f"    chunk{r.chunk_index}: {r.paragraph_count}段 "
                  f"offsets={r.paragraph_offsets[:46]}…")


# ── ④ 锚点/注释密度 ────────────────────────────────────────────────────────


def probe_anchors(reader: EpubReader) -> None:
    print("\n── ④ 锚点/注释密度 ───────────────────────────────")
    etypes: collections.Counter = collections.Counter()
    noterefs = 0
    id_targets = 0
    noteref_samples: list[str] = []
    href_targets = collections.Counter()
    for idx in range(len(reader._spine_ids)):
        html = read_spine(reader, idx)
        if html is None:
            continue
        etypes.update(re.findall(r'epub:type="([^"]+)"', html))
        # noteref 权威探测：epub:type="noteref"（不猜短 body）
        for m in re.finditer(r'<a\b([^>]*epub:type="[^"]*noteref[^"]*"[^>]*)>(.*?)</a>',
                             html, re.DOTALL):
            attrs = m.group(1)
            href_m = re.search(r'href="([^"]+)"', attrs)
            body = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if href_m:
                noterefs += 1
                if len(noteref_samples) < 5:
                    noteref_samples.append(f"{href_m.group(1)} {body!r}")
            href_targets[href_m.group(1) if href_m else "?"] += 1
        id_targets += len(re.findall(r'\sid="([^"]+)"', html))

    note_etype = sum(v for k, v in etypes.items() if "note" in k)
    print(f"epub:type 出现: {dict(etypes) or '(无)'}")
    print(f"noteref（epub:type 权威标记）: {noterefs} ｜ 注释类 epub:type: {note_etype} ｜ "
          f"id 目标: {id_targets}")
    if noteref_samples:
        print("  noteref 样本:", noteref_samples)
    total_links = sum(v for k, v in href_targets.items() if True)
    if total_links:
        print(f"  noteref href 总数: {total_links}（如 part0004.html#fn1 跨文件锚）")
    if note_etype:
        print("  → 存在真实注释跳转，锚图组建有依据（noteref→footnote 引用对可提取）")
    else:
        print("  → 无 noteref；锚点仅导航用")


# ── main ───────────────────────────────────────────────────────────────────


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="来源层结构摸底探针（机械，零 token）")
    ap.add_argument("--book", default="窄门", help="书名子串")
    ap.add_argument("--all", action="store_true", help="扫描全部库内可读书")
    args = ap.parse_args()

    db = BookDB(Path("data/books.db"))
    books_to_scan = []
    if args.all:
        cur = db.get_conn().cursor()
        cur.execute("SELECT id, title, author, source_path FROM books")
        books_to_scan = [r for r in cur.fetchall() if r[3] and Path(r[3]).exists()]
        if not books_to_scan:
            print("!! 库内没有源文件可读的书（source_path 不存在）")
            return 1
        print(f"扫描 {len(books_to_scan)} 本可读书")
    else:
        hit = find_book(db, args.book)
        if not hit:
            print(f"!! 无书匹配 {args.book!r}")
            return 1
        books_to_scan = [hit]

    for book_id, title, author, source_path in books_to_scan:
        print(f"\n{'=' * 60}\n书: {title}｜{author}  ({book_id})")
        try:
            reader = EpubReader(source_path)
            reader._ensure_loaded()
        except Exception as exc:
            print(f"  !! 打开失败: {exc}")
            continue
        try:
            probe_style_clusters(reader)
            probe_toc_tree(reader)
            probe_paragraph_rebuild(reader, db, book_id)
            probe_anchors(reader)
        finally:
            if hasattr(reader, "close"):
                reader.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())