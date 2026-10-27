"""读者笔记 —— 以**读者**身份读一篇，写下当场的感触。

与 ``verify.py`` 同形：普通 LLM 调用，不是子代理类型。输入完全确定
（这一篇的正文 + 篇内名册 + 人格 + 章号上界），不需要工具，
于是能被 ``scripts/fake_provider.py`` 零 token 端到端驱动。

**这一趟不是提取。** 提取问「谁对谁做了什么」，产出图谱；
这一趟问「读到这里的一个人有什么反应」，产出笔记。
两者的消费者、验收标准、失败模式都不同，所以是两个模块。

三条边界（见 ``docs/reader-notes-design.md``）：

1. **只读这一篇。** 不知道后面 —— 这是「读者」和「评论家」的唯一分界。
2. **不给图谱结论。** 名册只用来对齐落点，不是给它看的判断。
3. **不媚用户。** 笔记是 agent 自己的；写成书评就已经退化了。

本模块**只生成，不落库** —— 表结构与召回机制见设计稿，先验证产出值不值钱。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import aiohttp

from bookgraph.invariants import _norm_ws
from bookgraph.llm_call import output_budget
from config import Config
from llm_json import loads_tolerant
from provider_store import config_for
from sub_agent.types import load_prompt

log = logging.getLogger("bookgraph")

_NOTE_FALLBACK = """你在读一本书的一篇。读完，如果某一段值得，就在旁边写一段。
你是**读者** —— 不是提取器，也不是给用户写摘要的人。这些笔记是你自己的。

值得写的是「你读到某一句，心里动了一下」。不值得写的是情节梗概、人物介绍、
主题总结 —— 那些是书评。笔记一到三句，页边写不下更多。

你不知道后面发生了什么，不要预判情节。

严格 JSON：
{"notes":[{"chunk_id":"...","quote_text":"逐字原句",
           "node_name":"冲谁去的，没有就空串","text":"你的笔记"}],
 "read":"一句话感受"}
没有值得写的就返回空数组，不要硬写。"""


def reader_note_prompt() -> str:
    return load_prompt("book_reader_note", _NOTE_FALLBACK)


# ── 给读者的那一封信 ──────────────────────────────────────────────────────

def build_reader_msg(book_title: str, scope_label: str,
                     chunk_texts: list[tuple[str, str]],
                     roster: list[str] | None = None,
                     persona: str = "") -> str:
    """这一篇的全部正文 + 篇内名册 + 人格。

    **不给全书图谱结论。** 名册只用来对齐落点（和提取器「声明名字、
    recorder 解析」同一套），不是给它看的判断。
    """
    lines: list[str] = [f"你正在读《{book_title}》。"]
    if scope_label:
        lines.append(f"这一篇是《{scope_label}》。**你只读到这一篇，"
                     "不知道后面发生了什么。**")
    lines.append("")
    if persona:
        lines += ["## 你是这样的一个人", "", persona.strip(), ""]
    if roster:
        lines += ["## 这一篇里出现过的人名地名（写 node_name 时对齐用）",
                  "  " + "、".join(roster[:60]), ""]
    lines += ["## 正文", ""]
    for cid, text in chunk_texts:
        lines += [f"=== chunk_id: {cid} ===", text, ""]
    lines.append("读完，按上面的规矩写笔记。严格 JSON。")
    return "\n".join(lines)


def check_note_quotes(notes: list[dict],
                      chunk_text: dict[str, str]) -> list[str]:
    """逐字校验：`quote_text` 必须真的在它自己锚定的那一块里。

    复用 ``invariants._norm_ws``（空白归一后比对）—— 与边/观察的
    ``fabricated_evidence`` 同一把尺子，不给笔记另立标准。
    返回不合格的样本（供打印/上报），空列表 = 全部通过。
    """
    bad: list[str] = []
    for n in notes:
        if not isinstance(n, dict):
            continue
        q = str(n.get("quote_text") or "").strip()
        cid = str(n.get("chunk_id") or "").strip()
        body = chunk_text.get(cid)
        if not q or body is None or _norm_ws(q) not in _norm_ws(body):
            bad.append(f"{cid or '?'}:{q[:20] or '(空)'}")
    return bad


def reanchor(notes: list[dict], chunk_text: dict[str, str]) -> list[str]:
    """把 `chunk_id` 纠正到**真正含有 `quote_text` 的那一块**。

    `chunk_id` 是**导出量** —— 和 ``recorder._first_mention`` 同一个道理：
    模型写的是「我以为它在这」，系统一秒能算出「它实际在哪」。
    实测《记忆与印象2》7 条里 2 条锚到了同章的兄弟块上（引文本身逐字为真，
    所以逐字校验抓不住它，只有这条规则能）。

    只在本篇内找；找不到就**保持原样**（那才是真缺陷，交给 ``check_note_quotes``
    报出来）。返回被纠正的样本（供上报），空列表 = 本来就全对。
    """
    fixed: list[str] = []
    for n in notes:
        if not isinstance(n, dict):
            continue
        q = _norm_ws(str(n.get("quote_text") or ""))
        cid = str(n.get("chunk_id") or "").strip()
        if not q or q in _norm_ws(chunk_text.get(cid, "")):
            continue
        hit = next((c for c, t in chunk_text.items() if q in _norm_ws(t)), None)
        if hit:
            fixed.append(f"{cid[:8]}→{hit[:8]}")
            n["chunk_id"] = hit
    return fixed


# ── 跑一趟 ────────────────────────────────────────────────────────────────

async def generate_notes(
    session: aiohttp.ClientSession,
    cfg: Config,
    book_title: str,
    scope_label: str,
    chunk_texts: list[tuple[str, str]],
    roster: list[str] | None = None,
    persona: str = "",
    temperature: float = 0.7,
    log_cb: Callable[[dict], None] | None = None,
) -> dict[str, Any] | None:
    """跑一趟读者笔记。返回 ``{notes, read}``，**解析不出就返回 None**。

    返回 None **不等于**「这一篇没什么可写」—— 后者是 ``{"notes": []}``，
    是诚实的回答。None 只意味着这一趟没跑成，调用方必须分辨这两者。

    ``temperature`` 默认 0.7 而非证伪趟的 0.0：这是主观任务，贪心解码会
    让每一篇的笔记长得一样。可复现性在这里让位于「像人写的」。
    """
    cfg = config_for(cfg, "bookgraph.notes")
    sys_prompt = reader_note_prompt()
    user_msg = build_reader_msg(book_title, scope_label, chunk_texts,
                                roster, persona)
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user_msg}],
        "stream": False,
        "temperature": temperature,
        "max_tokens": output_budget(cfg),
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json"}
    t0 = time.monotonic()
    try:
        async with session.post(
            f"{cfg.api_base}/chat/completions", json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                log.warning("Reader notes API %s: %s", resp.status, err[:200])
                if log_cb:
                    log_cb({"status": "api_error", "prompt": user_msg,
                            "response": "", "error_detail": err[:300],
                            "latency_ms": int((time.monotonic() - t0) * 1000)})
                return None
            data = json.loads(await resp.text())
            content = data["choices"][0]["message"].get("content") or ""
            parsed = loads_tolerant(content, expect=dict)
            if log_cb:
                log_cb({"status": "ok" if parsed else "parse_failed",
                        "prompt": user_msg, "response": content,
                        "latency_ms": int((time.monotonic() - t0) * 1000)})
            return parsed
    except Exception as exc:
        log.warning("Reader notes failed: %s", exc)
        return None


# ── 取篇：把一本书切成「情节单元」────────────────────────────────────────
#
# 情节单元 = **level==0 的正文章**。``part000X split 00Y`` 是切分续块，
# 属于它前面那一篇 —— 直接用 chapter_index 会把一篇拆成几颗
# （《我与地坛》26 个 chapter_index，真实情节只有 12 个）。

def _owner_map(books_db, book_id: str) -> list[tuple[int, int, str]]:
    """``[(chapter_index, 所属 L0 章号, 该 L0 篇名)]``。"""
    rows = books_db.get_conn().execute(
        "SELECT chapter_index, level, title FROM book_chapters "
        "WHERE book_id=? ORDER BY chapter_index", (book_id,)).fetchall()
    out: list[tuple[int, int, str]] = []
    cur, title = -1, ""
    for r in rows:
        if r["level"] == 0:
            cur, title = r["chapter_index"], r["title"] or ""
        out.append((r["chapter_index"], cur, title))
    return out


def iter_scopes(books_db, book_id: str) -> list[tuple[str, list[int]]]:
    """全书按情节单元切：``[(篇名, [chapter_index...])]``，**只含正文**。

    目录 / 版权页 / 代跋 按 ``chapter_type`` 排除 —— 它们不是情节。
    """
    types = {r["chapter_index"]: (r["chapter_type"] or "")
             for r in books_db.get_conn().execute(
                 "SELECT chapter_index, chapter_type FROM book_chapters "
                 "WHERE book_id=?", (book_id,))}
    order: list[int] = []
    by_owner: dict[int, list[int]] = {}
    titles: dict[int, str] = {}
    for ci, owner, title in _owner_map(books_db, book_id):
        by_owner.setdefault(owner, []).append(ci)
        if ci == owner:
            titles[owner] = title
            if types.get(ci) == "body":
                order.append(owner)
    return [(titles[o], by_owner[o]) for o in order]


def find_scope(books_db, book_id: str,
               needle: str) -> tuple[str, list[int]] | None:
    """按篇名关键词找一篇（试跑脚本用）。找不到返回 None。"""
    return next((s for s in iter_scopes(books_db, book_id) if needle in s[0]),
                None)


def scope_chunks(books_db, book_id: str,
                 idxs: list[int]) -> list[tuple[str, str]]:
    """这一篇的全部块，按阅读顺序。"""
    ph = ",".join("?" * len(idxs))
    rows = books_db.get_conn().execute(
        f"SELECT id, content FROM book_chunks WHERE book_id=? "
        f"AND chapter_index IN ({ph}) ORDER BY chapter_index, chunk_index",
        [book_id] + list(idxs)).fetchall()
    return [(r["id"], r["content"] or "") for r in rows]


def roster_for(graph_path, chunk_ids: list[str]) -> list[str]:
    """篇内出现过的人名地名 —— 给读者对齐 `node_name` 用。

    **不是给它看的判断。** 它是读者，名册只用来把笔记挂到正确的节点上
    （和提取器「声明名字、recorder 解析」同一套）。
    """
    import sqlite3
    if not graph_path or not Path(graph_path).exists():
        return []
    conn = sqlite3.connect(f"file:{Path(graph_path).as_posix()}?mode=ro",
                           uri=True)
    try:
        ph = ",".join("?" * len(chunk_ids))
        rows = conn.execute(
            f"SELECT DISTINCT n.canonical_name FROM edges e JOIN nodes n "
            f"ON n.id IN (e.source_id, e.target_id) "
            f"WHERE e.chunk_id IN ({ph}) AND n.kind!='chapter'",
            chunk_ids).fetchall()
        return sorted(r[0] for r in rows)
    except sqlite3.Error:
        return []
    finally:
        conn.close()
