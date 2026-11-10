"""认路地图：逐章 compact 的投影 + 按卷合成。

两级产物：

- **逐章行**（零成本）：``第 n 章 标题：compact``，直接从笔记投影，见
  ``reading.store.chapter_map``。80 章约 8k 字，足够当伴读前情。
- **卷级图**（每 8–10 章一次调用）：把一段章节的逐章行压成一张路标图，
  链式携带上一张图，保证「前 n 章」始终有一份定长、有结构的认路地图。

**刻意不接进逐章阅读循环**：地图是低频产物，上下文换窗仍按 35% 阈值
触发。每章都重写上下文会毁掉前缀缓存（实测代价约 2.4 倍输入），这块
资产用 projection + 低频合成就能拿到。
"""
from __future__ import annotations

import logging
from typing import Any

from reading.llm import call_chat
from reading.store import ReadingStore, chapter_map

log = logging.getLogger("reading.map")

#: 合成提示词版本；改提示词时 +1，旧图按版本重跑。
MAP_PROMPT_VERSION = "map-v1"

#: 一段卷（默认 8 章）压成的图目标长度。80 章 ≈ 10 张 × 500 字。
ARC_TARGET_CHARS = 500
#: 2k 在 deepseek-v4.1-flash 上会先撞顶一次（推理占大头），4k 一次成。
ARC_MAX_TOKENS = 4_000

MAP_SYSTEM = """你是「留白」的地图编辑。给你一段已经读过的章节的逐章摘要，
把它们压成一张路标式地图，供伴读与角色扮演回头认路。

只写会让读者/角色困惑的三类东西：
1. 推进与转折（谁做了什么，局面怎么变了）；
2. 人物关系的变化（结盟、决裂、身份揭晓），用「A → B：…」；
3. 尚未解决的线索（许诺、怀疑、未接的电话、没解释的物件）。

不写：文学评论、主题分析、原文长引文、后文信息（你不知道后文）。

输出中文，篇幅 <= 目标字数。只输出地图正文，不要标题、不要 JSON、
不要列表符号以外的格式。"""


def map_block(text: str, start: int, end: int) -> str:
    """换窗用的摘要块：同一份地图，包一层标签告诉模型它是什么。"""
    return (
        "<reading-map>\n"
        f"以下是你读过的第 {start}–{end} 章的认路地图，原文已不在上下文里；"
        "需要细节时调用 search_book / read_passage 回查。\n\n"
        + text.strip()
        + "\n</reading-map>"
    )


def arc_prompt(prev_map: str, entries: list[tuple[int, str, str]], *,
               target_chars: int = ARC_TARGET_CHARS,
               start_chapter: int | None = None) -> str:
    """``entries``: ``(chapter_index, title, compact)``，按章升序。

    输出的图是**累积的**：覆盖从全书第一章到本卷末。``start_chapter``
    是全书第一个读到的章（书可能有前言的章索引不连续）。
    """
    lines = [f"第 {idx} 章 {title}：{compact}".strip()
             for idx, title, compact in entries]
    lo, hi = entries[0][0], entries[-1][0]
    cover_from = entries[0][0] if start_chapter is None else start_chapter
    head = (f"已有的前情地图（截至第 {lo - 1} 章）：\n{prev_map.strip()}\n\n"
            if prev_map.strip() else "")
    return (
        f"{head}本卷章节（第 {lo}–{hi} 章）的逐章摘要：\n"
        + "\n".join(lines)
        + f"\n\n把「前情地图」与「本卷」压成一张新的认路地图，"
        f"覆盖第 {cover_from}–{hi} 章，<= {target_chars} 字。"
        "不要逐章复述，只留路标。"
    )


async def synthesize(cfg: Any, prev_map: str,
                     entries: list[tuple[int, str, str]], *,
                     target_chars: int = ARC_TARGET_CHARS,
                     start_chapter: int | None = None,
                     recorder: Any = None, session_id: str = "",
                     book_id: str = "") -> str:
    """把一巻逐章摘要（+ 上一张图）压成新地图；失败返回空串。"""
    if not entries:
        return ""
    try:
        from provider_store import config_for
        cfg = config_for(cfg, "reading.map")
    except Exception:
        pass
    messages = [{"role": "system", "content": MAP_SYSTEM},
                {"role": "user",
                 "content": arc_prompt(prev_map, entries,
                                       target_chars=target_chars,
                                       start_chapter=start_chapter)}]
    try:
        return await call_chat(
            cfg, messages, max_tokens=ARC_MAX_TOKENS, source="reading.map",
            mode=MAP_PROMPT_VERSION, recorder=recorder,
            session_id=session_id, turn_index=int(entries[0][0]),
            book_id=book_id)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("map synthesis failed: %s", exc)
        return ""


async def build_arc_maps(cfg: Any, store: ReadingStore, book_id: str, *,
                         arc_size: int = 8, force: bool = False,
                         prompt_version: str = MAP_PROMPT_VERSION,
                         recorder: Any = None,
                         log_cb: Any = None) -> list[dict[str, Any]]:
    """把尚未成卷的章节按 ``arc_size`` 切段并合成地图，返回新写入的图。"""
    notes = store.notes_for(book_id)
    if not notes:
        return []
    existing = {int(m["start_chapter"]): m
                for m in store.maps_for(book_id, level="arc")}
    entries = [(int(n["chapter_index"]),
                str(n.get("chapter_title") or ""),
                str(n.get("compact")
                    or (n.get("note") or {}).get("compact") or ""))
               for n in notes]
    entries = [e for e in entries if e[2].strip()]
    built: list[dict[str, Any]] = []
    # --force 是整本重做：链头必须从空开始，否则会用最后一张图（含后文）
    # 去压第一章，把后文泄进前情地图。
    prev_map = ""
    i = 0
    while i + arc_size <= len(entries):
        chunk = entries[i:i + arc_size]
        lo, hi = chunk[0][0], chunk[-1][0]
        old = existing.get(lo)
        if old is not None and not force:
            prev_map = str(old.get("text") or "")
        else:
            text = await synthesize(
                cfg, prev_map, chunk, recorder=recorder,
                start_chapter=entries[0][0],
                session_id=f"reading-map:{book_id}", book_id=book_id)
            if not text:
                # 失败就停：后面的卷依赖「前情地图截至上一卷」，跳过去
                # 会在链上留洞。重跑会从这一卷继续。
                log.warning("arc %s–%s failed; stop here", lo, hi)
                break
            store.save_map(book_id, start_chapter=lo, end_chapter=hi,
                           text=text, level="arc", note_count=len(chunk),
                           prompt_version=prompt_version)
            built.append({"start": lo, "end": hi, "chars": len(text)})
            prev_map = text
            if log_cb:
                log_cb({"type": "map", "start": lo, "end": hi,
                        "chars": len(text)})
        i += arc_size
    return built


def map_for(store: ReadingStore, book_id: str, as_of: int | None = None,
            *, max_chars: int = 4_000) -> str:
    """消费入口：拿一本书截至 ``as_of`` 的认路地图（卷图 + 剩余逐章行）。"""
    notes = store.notes_for(book_id, as_of=as_of)
    maps = store.maps_for(book_id, as_of=as_of)
    return chapter_map(notes, maps, as_of=as_of, max_chars=max_chars)
