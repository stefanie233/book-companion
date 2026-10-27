"""一次性：给空 ``paragraph_sigs`` 的旧 chunk 补回段落排版签名。

``paragraph_sigs``（v18）是每段的体例签名（``p|calibre19``），由导入路径
（``epub_import_service.py``）写入。**先于 v18 导入的书**这一列落成默认
``'[]'``，而没有任何回填 —— 实测全库 4455 块里 476 块（10%）是空的，集中在
万历十五年 / 当下的力量 / 非暴力沟通三本。

为什么值得补：这是「排版 → 提取器」唯一还活着的通道。空的签名意味着那三本书
的 chunk 拿不到逐段形态标签，书信里的第一人称又会被归给叙述者。

做法
----
从源 EPUB 建一张 ``{段落文本: 签名}`` 表，再按**文本**去匹配每个 chunk 的段落。
按文本而非位置匹配，是为了绕开这三本未决的 spine 对齐问题（它们的
``book_chapters.spine_index`` 全为 0，``reimport_books.py --aligned`` 覆盖不到；
而重导正是历史上把它们炸成 94/101 章的操作）。

铁律：**只 UPDATE，绝不 insert/delete chunk**
--------------------------------------------
``library_service.insert_chunks`` 会**先删全书再插**，换掉 ``book_chunks.id``，
而 ``graph_chunks.chunk_id`` / ``edges.chunk_id`` / ``graph_quotes.chunk_id`` 都
挂在它上面（无 DB 级外键）—— 换主键会把图谱静默变孤儿。

一个 chunk 里有任何一段匹配不上就**整块跳过**，不半填：半填的签名列表与
``paragraph_offsets`` 错位，比空着更糟。

幂等：只处理 ``paragraph_sigs = '[]'`` 的行；中断可续跑。
默认 dry-run（只统计），``--apply`` 才写库。

用法:
  python scripts/backfill_chunk_sigs.py            # 统计
  python scripts/backfill_chunk_sigs.py --apply    # 回填
  python scripts/backfill_chunk_sigs.py 万历十五年 --apply
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from file_readers import open_book
from file_readers.epub_reader import EpubReader, _format_html_styled
from file_readers.html_skeleton import split_html_by_heading
from services.library_service import _extract_paragraphs
from storage.db import BookDB


def _book_sig_index(epub_path: str, method: str, selector: str) -> tuple[dict, int]:
    """``{段落文本: 签名}`` + 冲突数（同一段文本在不同处签名不同）。

    分段方式与导入路径一致：先 ``split_html_by_heading`` 再逐段
    ``_format_html_styled`` —— 后者带 ``seen`` 去重，按整文件跑会与按节跑结果
    不同，所以要复刻。签名冲突时取**先出现的**，只记数不报错：短行（如 ``***``）
    在不同类里出现是正常的。
    """
    r = open_book(epub_path)
    if not isinstance(r, EpubReader):
        # MOBI 走的是「转纯文本」路径，没有 HTML、没有 spine —— 它**没有**排版
        # 可补。返回空索引，让调用方报「跳过」而不是假装填了。
        if hasattr(r, "close"):
            r.close()
        return {}, 0
    index: dict[str, str] = {}
    conflicts: Counter = Counter()
    try:
        r._ensure_loaded()
        for si in range(len(r._spine_ids)):
            try:
                raw = r._read_spine_html(si)
            except Exception:
                continue
            try:
                sections = split_html_by_heading(raw, method, selector)
            except Exception:
                sections = [(None, raw, "")]
            for _heading, body, _tag in sections:
                if not (body or "").strip():
                    continue
                for p in _format_html_styled(body):
                    text, sig = p.get("text") or "", p.get("sig") or ""
                    if not text or not sig:
                        continue
                    if text in index:
                        if index[text] != sig:
                            conflicts[text] += 1
                    else:
                        index[text] = sig
    finally:
        if hasattr(r, "close"):
            r.close()
    return index, sum(conflicts.values())


def _plan(conn, book_id: str, index: dict) -> tuple[list, int, int]:
    """返回 ``(updates, filled, skipped)``；updates 是 ``(sigs_json, chunk_id)``。"""
    rows = conn.execute(
        "SELECT id, content, paragraph_count FROM book_chunks "
        "WHERE book_id=? AND (paragraph_sigs IS NULL OR paragraph_sigs='[]')",
        (book_id,),
    ).fetchall()
    updates, skipped = [], 0
    for r in rows:
        paras = _extract_paragraphs(r["content"] or "")
        if not paras or (r["paragraph_count"] and r["paragraph_count"] != len(paras)):
            skipped += 1
            continue
        sigs = [index.get(p) for p in paras]
        if any(s is None for s in sigs):     # 有一段认不出就整块跳过，不半填
            skipped += 1
            continue
        updates.append((json.dumps(sigs, ensure_ascii=False), r["id"]))
    return updates, len(updates), skipped


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    cfg = load_config()
    db = BookDB(cfg.books_db_path)
    db.initialize()
    conn = db.get_conn()
    bank = Path(cfg.books_db_path).parent / "bookbank"

    sql = ("SELECT id, title, source_path, metadata_json FROM books "
           "WHERE id IN (SELECT DISTINCT book_id FROM book_chunks "
           "             WHERE paragraph_sigs IS NULL OR paragraph_sigs='[]')")
    params: tuple = ()
    if args:
        sql = sql.replace("WHERE id IN", "WHERE title LIKE ? AND id IN")
        params = (f"%{args[0]}%",)
    books = conn.execute(sql, params).fetchall()
    if not books:
        print("没有待回填的书。")
        return 0

    total_fill = total_skip = 0
    for b in books:
        epub = Path(b["source_path"] or (bank / f"{b['id']}.epub"))
        if not epub.exists():
            print(f"  {b['title'][:22]:<24} 源文件不存在，跳过")
            continue
        try:
            meta = json.loads(b["metadata_json"] or "{}")
        except Exception:
            meta = {}
        method = meta.get("epub_import_method") or "h_tags"
        selector = meta.get("epub_import_selector") or ""
        index, conflicts = _book_sig_index(str(epub), method, selector)
        if not index:
            print(f"  {b['title'][:22]:<24} 非 EPUB（无排版可取），跳过")
            continue
        updates, filled, skipped = _plan(conn, b["id"], index)
        total_fill += filled
        total_skip += skipped
        print(f"  {b['title'][:22]:<24} 切分法={method:<12} "
              f"索引 {len(index):>6} 段（冲突 {conflicts}） "
              f"填 {filled:>4} 跳过 {skipped}")
        if apply and updates:
            conn.executemany(
                "UPDATE book_chunks SET paragraph_sigs=? WHERE id=?;", updates)
    conn.commit()

    print(f"\n{'[dry-run]' if not apply else '[apply]  '} 待回填 {total_fill} 块，"
          f"跳过 {total_skip} 块")
    if not apply:
        print("(dry-run 未写库；加 --apply 执行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
