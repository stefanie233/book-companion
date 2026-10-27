"""作者层的剧透门 —— **语义换了，机制照旧**。

书里那套门问的是「作者知不知道后文」。作者当然知道全书 —— 他写的。
所以那套门在作者层没有意义。

作者层的门问的是另一件事：**读者还没读到，别替他读。**

而且**生平会剧透作品**：读者刚读到《我与地坛》第一篇，你告诉他作者
「1998 年因肾病恶化而卧床」，他不光知道了生平，还知道了后面几篇的情绪底色。
所以门必须算在**年**上，不是章上。

于是需要「章号 → 编年」的映射。这个映射**不落库**，从语料里现算 ——
它是个导出量：同一本书的语料多采一条，映射就变一点。落库就会过期，
和「实体重要级不落库」是同一条。

全层**只有一处**算这个门（``reader_year``），消费端一律读它的返回值 ——
照搬书里那条「全流程唯一一处计算」（那次是因为四处各错一处，且每处
单独看都像是对的）。
"""

from __future__ import annotations

from typing import Any


def gate_year(pairs: list[tuple[Any, Any]], reader_chapter: int) -> int | None:
    """纯函数：由 ``[(chapter_index, era_year)]`` 与读者当前章算门。

    取**读者已经读过的那些块里最晚的一个年** —— 那就是他「此刻所处的
    时间点」。之后的事一律不放行。

    **无年或无语料的块不参与**，一条都没有时返回 ``None`` ——
    调用方必须把 ``None`` 当「无门可用」（fail-closed，什么都不展示），
    **不能**当「无门 = 全书放行」。书里那套门踩过这个坑：``ch is None``
    被当成「已见」放行，实测某本书 19/19 条观察无锚点 = 剧透门 100% 失效。
    """
    best: int | None = None
    for ch, yr in pairs:
        if not isinstance(ch, int) or not isinstance(yr, int):
            continue
        if ch <= reader_chapter and (best is None or yr > best):
            best = yr
    return best


def gate_state(db: Any, author_id: str, stable_book_id: str,
               reader_chapter: int) -> dict[str, Any]:
    """门的**三态 + 为什么**。``reader_year`` 只给值，说不出原因 ——
    而「卡片是空的」有两个完全不同的原因，不分开就查不出来：

    - ``no_dated``  —— 这本书的语料一条带年份的都没有（采集没抽到）
    - ``all_ahead`` —— 有带年份的语料，但**全在读者还没读到的位置**。
      实测《我与地坛》就是这一种：9 条语料**全部**在 ch25 的代跋，
      读者读到第 7 章时一条都够不着。这不是缺陷，是「序跋在卷末」
      的正确后果 —— 但不说出来就会被当成「作者层没数据」。

    ``ahead`` 是有几年语料落在读者之后，报出来才知道差多远。
    """
    try:
        rows = db.get_conn().execute(
            "SELECT chapter_index, era_year FROM author_corpus"
            " WHERE author_id=? AND stable_book_id=?"
            "   AND era_year IS NOT NULL AND chapter_index IS NOT NULL",
            (author_id, stable_book_id)).fetchall()
    except Exception:                                   # noqa: BLE001
        return {"gate": None, "reason": "unreadable", "dated": 0, "ahead": 0}
    pairs = [(r["chapter_index"], r["era_year"]) for r in rows]
    gate = gate_year(pairs, reader_chapter)
    if gate is not None:
        return {"gate": gate, "reason": "open", "dated": len(pairs), "ahead": 0}
    ahead = sum(1 for ch, _ in pairs if ch > reader_chapter)
    return {"gate": None, "reason": "all_ahead" if ahead else "no_dated",
            "dated": len(pairs), "ahead": ahead}


def reader_year(db: Any, author_id: str, stable_book_id: str,
                reader_chapter: int) -> int | None:
    """这个作者、这本书、读到这一章时，时间轴最远能到哪一年。

    ``None`` = 无门可用。**薄薄一层**，判据全在 ``gate_state`` 里 ——
    两处各算一遍就会有两个答案。
    """
    return gate_state(db, author_id, stable_book_id, reader_chapter)["gate"]
