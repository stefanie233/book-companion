"""引文分级回填：给已有笔记里的引文补 kind / tag / who。

阅读会话从 `reading-v5` 起直接产出分级字段；但 v5 之前的全本笔记
（面纱 80 章、窄门 8 章）重读一遍要 6.35M 输入，只为重新打标不值。
回填只把**已经逐字核对过的引文**送进去分级：每卷一次调用，输入不到
1k，输出带推理。

只在 `note_json` 里加字段，不建表、不改锚点——引文本身不动。
"""
from __future__ import annotations

import logging
from typing import Any

from llm_json import loads_tolerant
from reading import prompts
from reading.llm import call_chat
from reading.store import ReadingStore

log = logging.getLogger("reading.tagging")

TAG_PROMPT_VERSION = prompts.TAG_VERSION
#: 一卷 24 条引文，模型的推理要吃掉大半预算；2k 会在每条调用上撞顶一次
#: （实跑 deepseek-v4.1-flash 时 6 次调用有 5 次先截断再重试）。
TAG_MAX_TOKENS = 6_000

KINDS = ("literary", "kernel", "other")

TAG_SYSTEM = """你是引文分级员。下面每条引文都是从小说里逐字摘出的，已经核对过原文。

给每条标三样东西：

1. kind，三选一：
   - literary：值得学写法——意象、比喻、反讽、对白里的潜台词、
     叙事滑进人物内心的写法、句子的节奏。
   - kernel：暴露人物内核——欲望与行动的冲突、自欺、决断点、关系反转、
     认知落差、掩饰。
   - other：两条都不像（只是情节重要的句子大多落在这里）。
2. tag：只能从词表里选一个，不要自造：
   literary：意象 / 比喻 / 反讽 / 对白潜台词 / 自由间接引语 / 节奏
   kernel：欲望-恐惧 / 自欺 / 决断点 / 关系反转 / 认知落差 / 掩饰
   kind=other 时 tag 写 other。
3. who：kind=kernel 时必填，写这条属于或指向的角色；literary 与 other 留空。

**literary 的门槛**：技法必须从这一句本身看得出来。要靠大段上下文解释
才成立的，标 other 或 kernel。对白里普通的推诿、冷淡、客套都不算潜台词，
只有字面与真意明显错位才算。宁可漏，不要凑。

判断只看引文本身和它所在章的角色名单，不要用你的后文知识。
只输出 JSON：{"tags": [{"i": 0, "kind": "kernel", "tag": "自欺", "who": "凯蒂"}]}
每条引文都要有结果，i 是引文编号。"""


def tag_prompt(entries: list[tuple[int, int, str, str, list[str]]]) -> str:
    """``entries``: ``(i, chapter, text, why, characters_of_chapter)``。"""
    lines: list[str] = []
    for i, chapter, text, why, chars in entries:
        who = ("；本章角色：" + "、".join(chars)) if chars else ""
        lines.append(f"[{i}] 第 {chapter} 章「{text}」"
                     + (f"（原注：{why}）" if why else "") + who)
    return "\n".join(lines) + "\n\n按 system 的规则给每条分级。"


def _normalize(raw: Any, allowed: set[int]) -> dict[int, dict[str, str]]:
    obj = raw if isinstance(raw, dict) else {}
    out: dict[int, dict[str, str]] = {}
    for item in obj.get("tags") or []:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if i not in allowed:
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in KINDS:
            kind = "other"
        tag = str(item.get("tag") or "").strip()
        # 词表外的 tag 记 other，但保留 kind——词表缺口不该把这条的价值
        # 一起丢掉；other 占比高时改词表，不是改 kind。
        if kind == "other" or tag not in prompts.QUOTE_TAGS:
            tag = "other"
        who = str(item.get("who") or "").strip()
        # literary 也可以带 who（这段是谁的视角）；只有 other 不带。
        if kind == "other":
            who = ""
        out[i] = {"kind": kind, "tag": tag, "who": who}
    return out


async def tag_quotes(cfg: Any, entries: list[tuple[int, int, str, str,
                                                    list[str]]], *,
                     recorder: Any = None, session_id: str = "",
                     book_id: str = "") -> dict[int, dict[str, str]]:
    """给一批引文分级；失败返回空 dict（调用方保留原样）。"""
    if not entries:
        return {}
    try:
        from provider_store import config_for
        cfg = config_for(cfg, "reading.tag")
    except Exception:
        pass
    messages = [{"role": "system", "content": TAG_SYSTEM},
                {"role": "user", "content": tag_prompt(entries)}]
    try:
        text = await call_chat(
            cfg, messages, max_tokens=TAG_MAX_TOKENS, source="reading.tag",
            mode=TAG_PROMPT_VERSION, temperature=0.0,
            recorder=recorder, session_id=session_id,
            turn_index=int(entries[0][1]), book_id=book_id)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("tagging failed: %s", exc)
        return {}
    obj = loads_tolerant(text, expect=dict)
    return _normalize(obj, {e[0] for e in entries})


async def backfill_tags(cfg: Any, store: ReadingStore, book_id: str, *,
                        arc_size: int = 8, force: bool = False,
                        tagger: Any = None, recorder: Any = None,
                        log_cb: Any = None) -> list[dict[str, Any]]:
    """按卷给已有引文补分级；已打标（同版本）的章跳过。"""
    tagger = tagger or tag_quotes
    notes = store.notes_for(book_id)
    pending = [n for n in notes
               if force or str((n.get("note") or {}).get("quote_tags_version")
                               or "") != TAG_PROMPT_VERSION]
    done: list[dict[str, Any]] = []
    for start in range(0, len(pending), arc_size):
        chunk = pending[start:start + arc_size]
        entries: list[tuple[int, int, str, str, list[str]]] = []
        index: list[tuple[dict, int]] = []
        for note_item in chunk:
            note = note_item.get("note") or {}
            chars = [str(c.get("name")) for c in note.get("characters") or []
                     if isinstance(c, dict) and c.get("name")]
            for qi, q in enumerate(note.get("quotes") or []):
                if not isinstance(q, dict) or not q.get("text"):
                    continue
                i = len(entries)
                entries.append((i, int(note_item["chapter_index"]),
                                str(q["text"]), str(q.get("why") or ""), chars))
                index.append((note_item, qi))
        if not entries:
            continue
        tags = await tagger(cfg, entries, recorder=recorder,
                            session_id=f"reading-tag:{book_id}",
                            book_id=book_id)
        if not tags:
            log.warning("tagging returned nothing for arc %s", chunk[0].get(
                "chapter_index"))
            continue
        touched: set[str] = set()
        for i, (note_item, qi) in enumerate(index):
            got = tags.get(i)
            if not got:
                continue
            note = note_item["note"]
            note["quotes"][qi].update(got)
            touched.add(str(note_item["run_id"]))
            note["quote_tags_version"] = TAG_PROMPT_VERSION
        for note_item in chunk:
            if str(note_item["run_id"]) in touched:
                store.update_note_json(str(note_item["run_id"]),
                                       int(note_item["chapter_index"]),
                                       note_item["note"])
        done.append({"from": int(chunk[0]["chapter_index"]),
                     "to": int(chunk[-1]["chapter_index"]),
                     "quotes": len(entries), "tagged": len(tags)})
        if log_cb:
            log_cb(done[-1])
    return done
