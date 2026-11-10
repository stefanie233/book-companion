"""作者层的**环境上下文** —— 给采集调用看什么，由这里决定。

这是全层最重要的一道闸：**采集调用只看得到序跋，看不到正文。**

不是靠提示词叮嘱「别把小说当生平」，而是让它**物理上看不见** ——
抄 ``run_reader_notes`` 那句「生成门是结构性的，看不见就是看不见」。
reader_notes 用它防「预判情节」，这里用它防「把虚构写进作者台账」，
后者是**不可逆**的（一旦落库，作者生平里就混进了小说情节）。

结构信号已经有了，不需要新算：``book_chapters.chapter_type`` 把
``自序/代序/译者序/编者序/作者序`` **一律压成 ``preface``** ——
结构只回答「这在哪」，不回答「这是谁在说」。后者才是 agent 的活
（见 ``harvest.py`` 的 ``speaker`` 判定）。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("authorgraph")

#: 候选章类型 —— 作者第一人称正文最可能出现的地方。
#: ``appendix`` 故意**不收**：附录多半是他人写的资料或作者的技术性补充，
#: 收进来噪声大于收益（实测 9 章附录，体裁很杂）。
CANDIDATE_TYPES = ("preface", "postscript")

#: 序跋总字数低于这个数就不跑 —— **不值得为 18 个字烧一次调用**。
#: 实测全库首跑：3 本书的「序跋」是 18 / 36 / 79 字，多半是书名页或版权页
#: 被分类器顺手标成了 preface。它们跑出来的位置全是 `unknown`、语料全空，
#: 既浪费一次调用，又给台账添了一行没有信息量的 `unknown`。
#: 不静默跳过 —— 报出来，让上游知道这本书**为什么**没采。
MIN_CANDIDATE_CHARS = 200

#: 标题里带这些词的章不是「正文叙事」，即使导入分析把它们标成了 body。
#: 模型攻略可以用 ``#chapter:<标题原词>`` 单独点名，正文抽样则跳过，
#: 免得把自序/年表当成小说正文均匀抽样。
_MATTER_TITLES = ("自序", "代序", "译者序", "编者序", "序言", "前言",
                  "后记", "附录", "年表", "致辞", "译后", "编者",
                  "出版说明", "目录", "献词", "题记")
#: 单独一个「序/跋」也算前置/后置材料；「序章」「序曲」是正文，不能误伤。
_MATTER_EXACT = ("序", "跋")


def book_identity(db: Any, book_id: str) -> dict[str, Any]:
    """这本书是谁署名的 —— 采集的输入之一。

    ``stable_book_id`` 走 ``import_log``（``BookRow`` 没有这个字段），
    取不到就退回 ``book_id`` 本身 —— 与 ``api_server`` 的批注查询同一条
    兜底（那里注释写着「直接退回 book_id 会导致 annotations 查空」）。
    """
    out: dict[str, Any] = {
        "book_id": book_id, "stable_book_id": book_id,
        "title": "", "author": "", "nationality": "",
    }
    try:
        row = db.get_conn().execute(
            "SELECT title, author, author_nationality FROM books WHERE id=?",
            (book_id,)).fetchone()
        if row:
            out["title"] = row["title"] or ""
            out["author"] = row["author"] or ""
            out["nationality"] = row["author_nationality"] or ""
    except Exception as exc:                            # noqa: BLE001
        log.warning("book_identity 读 books 失败 (%s): %s", book_id, exc)
    try:
        slog = db.get_import_log_by_book(book_id)
        if slog and slog.stable_book_id:
            out["stable_book_id"] = slog.stable_book_id
    except Exception:                                   # noqa: BLE001
        pass
    return out


def candidate_chunks(db: Any, book_id: str, *,
                     max_chars: int = 60_000) -> tuple[list[dict[str, Any]], list[str]]:
    """这本书里**作者第一人称正文**的候选块。返回 ``(chunks, warnings)``。

    每块是 ``{chunk_id, chapter_index, chunk_index, chapter_title,
    chapter_type, text}`` —— 直接就是采集提示词要喂的形状。

    ``warnings`` 报的是**看不见的东西**（沉默不是成功）：这本书一个序跋章
    都没有时要说出来，而不是安静地返回空列表让上游以为「采集成功但没料」。
    """
    warnings: list[str] = []
    conn = db.get_conn()
    ph = ",".join("?" * len(CANDIDATE_TYPES))
    try:
        chapters = conn.execute(
            f"SELECT chapter_index, title, chapter_type FROM book_chapters"
            f" WHERE book_id=? AND chapter_type IN ({ph})"
            f" ORDER BY chapter_index", (book_id, *CANDIDATE_TYPES)).fetchall()
    except Exception as exc:                            # noqa: BLE001
        return [], [f"读 book_chapters 失败：{exc}"]
    if not chapters:
        return [], ["这本书没有 preface/postscript 章 —— 作者层无文本可采"]
    out: list[dict[str, Any]] = []
    used = 0
    for ch in chapters:
        ci = ch["chapter_index"]
        rows = conn.execute(
            "SELECT id, chunk_index, content FROM book_chunks"
            " WHERE book_id=? AND chapter_index=? ORDER BY chunk_index",
            (book_id, ci)).fetchall()
        if not rows:
            warnings.append(f"ch{ci}《{ch['title']}》分类为 {ch['chapter_type']}"
                            f" 但没有正文块")
            continue
        for r in rows:
            text = r["content"] or ""
            if used + len(text) > max_chars:
                warnings.append(
                    f"预算 {max_chars} 字用尽，ch{ci} 之后的序跋未纳入本次采集")
                return out, warnings
            used += len(text)
            out.append({
                "chunk_id": r["id"], "chapter_index": ci,
                "chunk_index": r["chunk_index"],
                "chapter_title": ch["title"] or "",
                "chapter_type": ch["chapter_type"], "text": text,
            })
    if used < MIN_CANDIDATE_CHARS:
        return [], warnings + [
            f"序跋合计只有 {used} 字（< {MIN_CANDIDATE_CHARS}），"
            f"多半是书名页/版权页被误分类 —— 不值得跑一次调用"]
    return out, warnings


def body_sample_chunks(db: Any, book_id: str, *, max_chars: int = 60_000,
                       max_chapters: int = 8) -> tuple[list[dict[str, Any]],
                                                       list[str]]:
    """正文的**确定性抽样**（给自传体投射用）。

    不扫整本：一本书几十万字，逐章找「现实投影」是针尖上找针，误报还多。
    只跨章取样本，每章取第一块足够长的正文；**有没有料由模型判，取哪些
    由结构定**（与声音抽样的纪律同一条）。

    调用方必须先确认用户标记了 ``read_as=autobiographical`` 或身份/攻略
    允许 —— 本函数只负责取样，不负责放行。
    """
    warnings: list[str] = []
    conn = db.get_conn()
    try:
        chapters = conn.execute(
            "SELECT chapter_index, title, chapter_type FROM book_chapters"
            " WHERE book_id=? AND chapter_type NOT IN ('preface','postscript')"
            " ORDER BY chapter_index", (book_id,)).fetchall()
    except Exception as exc:                            # noqa: BLE001
        return [], [f"读 book_chapters 失败：{exc}"]

    def _is_matter(title: str) -> bool:
        t = (title or "").strip()
        return (t in _MATTER_EXACT
                or any(k in t for k in _MATTER_TITLES))

    chapters = [c for c in chapters if not _is_matter(c["title"])]
    if not chapters:
        return [], ["这本书没有可抽样的正文章"]
    step = max(1, len(chapters) // max(1, max_chapters))
    picked = list(chapters)[::step][:max_chapters]
    out: list[dict[str, Any]] = []
    used = 0
    for ch in picked:
        rows = conn.execute(
            "SELECT id, chunk_index, content FROM book_chunks"
            " WHERE book_id=? AND chapter_index=? ORDER BY chunk_index",
            (book_id, ch["chapter_index"])).fetchall()
        row = next((r for r in rows if len(r["content"] or "") >= 200),
                   rows[0] if rows else None)
        if row is None:
            continue
        text = row["content"] or ""
        if used + len(text) > max_chars:
            warnings.append(f"正文预算 {max_chars} 字用尽，后面的章未纳入")
            break
        used += len(text)
        out.append({
            "chunk_id": row["id"], "chapter_index": ch["chapter_index"],
            "chunk_index": row["chunk_index"],
            "chapter_title": ch["title"] or "",
            "chapter_type": ch["chapter_type"] or "body", "text": text,
        })
    if not out:
        warnings.append("正文抽样没有取到块")
    return out, warnings


def chunks_by_title(db: Any, book_id: str, keyword: str, *,
                    max_chars: int = 60_000,
                    max_chapters: int = 3) -> tuple[list[dict[str, Any]],
                                                    list[str]]:
    """按章标题取块 —— 给模型攻略点名的「美国版自序/附录/年表」。

    为什么需要它：导入分析只按结构判 ``preface/body``，误分类很常见
    （实测《局外人》的美国版自序、诺奖致辞都落在 ``body``）。模型看目录
    能认出「这一章值得读」，但 ``#body`` 只会均匀抽样，正好跳过它。

    只认标题里出现的原词；关键字为空或没有匹配就返回空 + 原因。
    """
    kw = (keyword or "").strip()
    if not kw:
        return [], ["chapter 目标缺标题关键词"]
    esc = (kw.replace("\\", "\\\\").replace("%", "\\%")
           .replace("_", "\\_"))
    conn = db.get_conn()
    try:
        chapters = conn.execute(
            "SELECT chapter_index, title, chapter_type FROM book_chapters"
            " WHERE book_id=? AND title LIKE ? ESCAPE '\\'"
            " ORDER BY chapter_index", (book_id, f"%{esc}%")).fetchall()
    except Exception as exc:                            # noqa: BLE001
        return [], [f"按标题读 chapters 失败：{exc}"]
    if not chapters:
        return [], [f"没有标题含 {kw!r} 的章"]
    out: list[dict[str, Any]] = []
    used = 0
    for ch in chapters[:max(1, max_chapters)]:
        rows = conn.execute(
            "SELECT id, chunk_index, content FROM book_chunks"
            " WHERE book_id=? AND chapter_index=? ORDER BY chunk_index",
            (book_id, ch["chapter_index"])).fetchall()
        for row in rows:
            text = row["content"] or ""
            if not text:
                continue
            if used + len(text) > max_chars:
                return out, [f"标题匹配章的字数超过预算 {max_chars}，后面截断"]
            used += len(text)
            out.append({
                "chunk_id": row["id"], "chapter_index": ch["chapter_index"],
                "chunk_index": row["chunk_index"],
                "chapter_title": ch["title"] or "",
                "chapter_type": ch["chapter_type"] or "body", "text": text,
            })
    if not out:
        return [], [f"标题含 {kw!r} 的章没有正文块"]
    return out, []


def evidence_chunks(db: Any, book_id: str) -> dict[str, str]:
    """这本书的序跋块 ``{chunk_id: 正文}`` —— **不带任何尺寸门槛**。

    与 ``candidate_chunks`` 的区别是**问的问题不同**，不是松紧不同：

    - ``candidate_chunks`` 答「**值不值得花一次调用**采」→ 太短就不值
    - ``evidence_chunks``  答「**原文在不在这里**」→ 有多短都得算数

    混用会漏检：实测《霍乱时期的爱情》的序跋是 79 字的献词+题词，
    ``candidate_chunks`` 按门槛拒收，于是**已经采到的编年一条都查不到
    比对源** —— 校验输出为空，看着像「全干净」，其实是「没查」。

    不设门槛还有一层：这条通道只读、只在 refresh 里跑，没有成本。
    """
    out: dict[str, str] = {}
    ph = ",".join("?" * len(CANDIDATE_TYPES))
    try:
        rows = db.get_conn().execute(
            f"SELECT c.id, c.content FROM book_chunks c"
            f" JOIN book_chapters ch ON ch.book_id=c.book_id"
            f"   AND ch.chapter_index=c.chapter_index"
            f" WHERE c.book_id=? AND ch.chapter_type IN ({ph})",
            (book_id, *CANDIDATE_TYPES)).fetchall()
    except Exception as exc:                            # noqa: BLE001
        log.warning("evidence_chunks 读 %s 失败：%s", book_id, exc)
        return out
    for r in rows:
        out[r["id"]] = r["content"] or ""
    return out


def toc_lines(db: Any, book_id: str, limit: int = 60) -> list[str]:
    """顶层章标题 —— 让采集调用看见**这本书的形状**。

    它需要目录来判断「正文是一个世界还是几个世界」「是小说还是散文集」，
    但这些判断**不需要读到正文**。给目录不给正文，正是「够用但不过界」。
    """
    try:
        rows = db.get_conn().execute(
            "SELECT chapter_index, title, chapter_type FROM book_chapters"
            " WHERE book_id=? AND level=0 ORDER BY chapter_index LIMIT ?",
            (book_id, limit)).fetchall()
    except Exception:                                   # noqa: BLE001
        return []
    out: list[str] = []
    for r in rows:
        t = (r["title"] or "").strip() or "(无题)"
        tag = r["chapter_type"] or ""
        out.append(f"  ch{r['chapter_index']:>3}  {t}" + (f"   [{tag}]" if tag else ""))
    return out


def build_harvest_msg(ident: dict[str, Any], toc: list[str],
                      chunks: list[dict[str, Any]]) -> str:
    """给采集调用的那一封信。**只有书的身份 + 目录 + 序跋正文。**

    没有正文块、没有图谱结论、没有历史台账 —— 三个「不给」各有理由：
    正文不给是防虚构入账（结构性）；图谱结论不给是因为那是别人对这本书的
    解读，不是作者说的话；历史台账不给是防它顺着上一轮的结论编。
    """
    lines: list[str] = [
        f"## 这本书",
        f"  书名：{ident.get('title') or '(未知)'}",
        f"  署名：{ident.get('author') or '(未知)'}",
    ]
    if ident.get("nationality"):
        lines.append(f"  署名国籍：{ident['nationality']}")
    lines.append("")
    if toc:
        lines += ["## 目录（顶层）", *toc, ""]
    lines += ["## 作者第一人称的候选文本（序跋全文）", ""]
    for c in chunks:
        head = (f"=== chunk_id: {c['chunk_id']} ===\n"
                f"[ch{c['chapter_index']} 《{c['chapter_title']}》"
                f" 分类={c['chapter_type']}]")
        lines += [head, c["text"], ""]
    return "\n".join(lines)

