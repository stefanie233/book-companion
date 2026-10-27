"""把一趟采集的结果落进作者台账。

**唯一写入口。** 生成侧（``harvest.py``）不认识库，库（``storage/authors_db.py``）
不认识模型输出 —— 翻译工作只在这里做一次。

落库的三条纪律：

1. **幂等。** 同一本书重跑不产生重复条目（``add_corpus``/``add_record``
   在 SQL 层用唯一约束挡）。这是「断了直接重跑」能成立的前提。
2. **不覆盖已判定的东西。** 这一趟没判出 ``position`` 就不写它 ——
   一次不带判定的采集不该把上一轮的结论抹成 ``unknown``。
3. **落完标脏。** 书级采集完成 = 作者级的活（身份归一化、编年合并、
   生平补全）该重算了。标脏不重算 —— 重算由独立进程跑。
"""

from __future__ import annotations

import logging
from typing import Any

from storage.authors_db import AuthorsDB

log = logging.getLogger("authorgraph")


def store_harvest(db: AuthorsDB, ident: dict[str, Any],
                  result: dict[str, Any]) -> dict[str, Any]:
    """落一趟采集。返回一份台账（供打印/上报），不是布尔。"""
    stable = ident.get("stable_book_id") or ident.get("book_id") or ""
    book_id = ident.get("book_id") or ""

    # 名字：优先模型从文本里认出的署名，其次书的 author 字段。
    # 后者可能带「（美）…著」这类后缀，但**有总比没有好** —— 落一个
    # 脏名字，好过整趟采集因为没有名字而丢掉。
    name = (result.get("author_name") or "").strip() \
        or (ident.get("author") or "").strip()
    if not name:
        return {"ok": False, "reason": "no_author_name",
                "note": "文本与书的署名都没给出作者名 —— 无法建台账"}

    author_id = db.upsert_author(
        name,
        nationality=result.get("nationality") or ident.get("nationality") or "",
        birth_year=result.get("birth_year"),
        death_year=result.get("death_year"),
    )
    if not author_id:
        return {"ok": False, "reason": "upsert_failed"}

    for a in (result.get("author_aliases") or []):
        db.add_alias(a, author_id, source="corpus")
    # 书的署名本身也是一条别名 —— 下次遇到同一写法能直接命中。
    signed = (ident.get("author") or "").strip()
    if signed and signed != name:
        db.add_alias(signed, author_id, source="signed")

    # position 传 None 表示「这趟没判」→ 保留旧值。
    pos = result.get("position")
    # **主作者一律记 `author`。** 模型有时把主作者的 role 填成 `co-author`
    # （因为署名里确实有两个人），而 `author_books` 的主键含 role ——
    # 于是同一个人的同一本书会**占两行**（实测张景）。主作者就是主作者，
    # 合著者另走下面的分支。
    db.link_book(
        author_id, stable, book_id=book_id, role="author",
        position=pos if pos and pos != "unknown" else None,
        position_source="corpus" if pos and pos != "unknown" else "",
        position_note=result.get("position_note") or "",
    )
    # 自愈：同一个人在同一本书上不可能既是 `author` 又是 `co-author`。
    # 早期版本会让模型填的 role 直接落库，同一个人的同一本书占了两行
    # （实测张景）。这里顺手清掉，重跑一次就干净了。
    db.get_conn().execute(
        "DELETE FROM author_books WHERE author_id=? AND stable_book_id=?"
        " AND role='co-author'", (author_id, stable))
    db.get_conn().commit()

    # 合著者：**只建名，不继承国籍生卒** —— 那是另一个人的属性。
    # 位置不判（这趟的 position 是主作者的）；role 标 co-author，
    # 于是「这本书有几个人署名」在库里查得出来。
    co_ids: list[str] = []
    co_link_ids: list[str] = []
    for ca in (result.get("co_authors") or []):
        ca_id = db.upsert_author(ca)
        if ca_id and ca_id != author_id:
            db.link_book(ca_id, stable, book_id=book_id, role="co-author")
            co_ids.append(ca)
            co_link_ids.append(ca_id)

    n_corpus = sum(
        1 for c in (result.get("corpus") or [])
        if db.add_corpus(
            author_id, stable, c.get("excerpt") or "",
            chunk_id=c.get("chunk_id"), book_id=book_id,
            chapter_index=c.get("chapter_index"),
            chunk_index=c.get("chunk_index"),
            speaker=c.get("speaker") or "author",
            text_kind=c.get("text_kind") or "",
            era_year=c.get("era_year"), era_note=c.get("era_note") or "",
            source="corpus")
    )
    # 编年条目也挂来源与逐字证据 —— 与网页那条通道用同一张 evidence 表，
    # 消费端就不用分两套证明方式（设计稿 02 §五.1）。
    book_sid = db.book_source_id(stable) if stable else ""
    n_records = 0
    for r in (result.get("records") or []):
        created = db.add_record(
            author_id, r.get("kind") or "life_event",
            r.get("statement") or "",
            year=r.get("year"), year_end=r.get("year_end"),
            evidence_excerpt=r.get("evidence_excerpt") or "",
            stable_book_id=stable, chunk_id=r.get("chunk_id"),
            source="corpus")
        rid = db.add_record_ex(
            author_id, r.get("kind") or "life_event",
            r.get("statement") or "",
            year=r.get("year"), year_end=r.get("year_end"),
            stable_book_id=stable, chunk_id=r.get("chunk_id"),
            source="corpus")
        if created:
            n_records += 1
        quote = (r.get("evidence_excerpt") or "").strip()
        if rid is not None and quote:
            db.add_evidence(
                author_id, "record", str(rid), source_id=book_sid,
                quote=quote, locator=f"chunk:{r.get('chunk_id') or ''}",
                relation="supports", confidence=0.7)
    # 合著者也要标脏 —— 他们同样挂了这本书，作品房间要长出这一条。
    # 只标主作者的话，张松辉的作品房间永远是空的（实测踩到）。
    for cid in co_link_ids:
        db.mark_dirty(cid)
    db.mark_dirty(author_id)
    log.info("author harvest stored: %s ← 《%s》 corpus+%d records+%d",
             name, ident.get("title") or book_id, n_corpus, n_records)
    return {
        "ok": True, "author_id": author_id, "author_name": name,
        "co_authors": co_ids,
        "position": pos or "", "corpus_added": n_corpus,
        "records_added": n_records,
        "defects": result.get("defects") or [],
    }
