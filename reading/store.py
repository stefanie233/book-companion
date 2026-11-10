"""阅读会话的独立存储（``data/reading.db``）。

原型阶段不往 books.db 加表：阅读笔记的字段还会变，先隔离，等契约稳定
再决定是否并库。所有读取都按「最新一次 run 的每章笔记」去重。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from storage.db_base import BaseDatabase, _now_ms

_DDL = [
    """CREATE TABLE IF NOT EXISTS reading_runs (
        run_id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        window_tokens INTEGER NOT NULL DEFAULT 0,
        threshold_tokens INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running',
        chapters_done INTEGER NOT NULL DEFAULT 0,
        totals_json TEXT NOT NULL DEFAULT '{}',
        time_created INTEGER NOT NULL,
        time_updated INTEGER NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS reading_notes (
        run_id TEXT NOT NULL,
        book_id TEXT NOT NULL,
        chapter_index INTEGER NOT NULL,
        chapter_title TEXT NOT NULL DEFAULT '',
        note_json TEXT NOT NULL,
        compact TEXT NOT NULL DEFAULT '',
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_cached INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        tokens_reasoning INTEGER NOT NULL DEFAULT 0,
        time_created INTEGER NOT NULL,
        PRIMARY KEY (run_id, chapter_index)
    );""",
    """CREATE TABLE IF NOT EXISTS reading_resets (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        start_chapter INTEGER NOT NULL DEFAULT 0,
        end_chapter INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '',
        tokens_before INTEGER NOT NULL DEFAULT 0,
        tokens_after INTEGER NOT NULL DEFAULT 0,
        time_created INTEGER NOT NULL,
        PRIMARY KEY (run_id, seq)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_reading_notes_book "
    "ON reading_notes(book_id, chapter_index);",
]

#: 提示词版本：续跑只复用同版本的笔记，旧 v1/v2 小样不会混进全本。
_DDL_V2 = [
    "ALTER TABLE reading_notes ADD COLUMN prompt_version TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE reading_runs ADD COLUMN prompt_version TEXT NOT NULL DEFAULT '';",
]


#: 认路地图：逐章 compact 的纯投影（不落库），按卷合成的地图落这里。
#: 合成是低频操作（每 8–10 章一次），与上下文换窗无关——不要把它接回
#: 逐章循环，否则会毁掉前缀缓存。
_DDL_MAPS = [
    """CREATE TABLE IF NOT EXISTS reading_maps (
        book_id TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'arc',
        start_chapter INTEGER NOT NULL,
        end_chapter INTEGER NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        note_count INTEGER NOT NULL DEFAULT 0,
        prompt_version TEXT NOT NULL DEFAULT '',
        time_created INTEGER NOT NULL,
        PRIMARY KEY (book_id, level, start_chapter, end_chapter)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_reading_maps_book "
    "ON reading_maps(book_id, start_chapter);",
]


class ReadingStore(BaseDatabase):
    """reading.db 的唯一入口。方法与表一一对应，不做业务判断。"""

    _MIGRATIONS = [
        (1, "Reading session prototype", _DDL),
        (2, "Prompt version on reading runs/notes", _DDL_V2),
        (3, "Reading maps (arc synthesis)", _DDL_MAPS),
    ]

    def __init__(self, db_path: Path | str):
        super().__init__(Path(db_path))
        self.initialize()

    def start_run(self, book_id: str, *, model: str = "",
                  window_tokens: int = 0, threshold_tokens: int = 0,
                  prompt_version: str = "") -> str:
        # 毫秒时间戳在同毫秒内会撞主键（测试里连开两个 run 就复现过），
        # 加随机后缀；前缀保持时间可排序。
        run_id = f"rd-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        now = _now_ms()
        self.get_conn().execute(
            "INSERT INTO reading_runs (run_id, book_id, model, window_tokens,"
            " threshold_tokens, prompt_version, status, time_created,"
            " time_updated) VALUES (?,?,?,?,?,?,'running',?,?)",
            (run_id, book_id, model, window_tokens, threshold_tokens,
             prompt_version, now, now),
        )
        self.get_conn().commit()
        return run_id

    def finish_run(self, run_id: str, *, status: str,
                   chapters_done: int, totals: dict[str, Any]) -> None:
        self.get_conn().execute(
            "UPDATE reading_runs SET status=?, chapters_done=?, totals_json=?,"
            " time_updated=? WHERE run_id=?",
            (status, chapters_done,
             json.dumps(totals, ensure_ascii=False), _now_ms(), run_id),
        )
        self.get_conn().commit()

    def save_note(self, run_id: str, book_id: str, chapter_index: int,
                  note: dict[str, Any], *, chapter_title: str = "",
                  usage: dict[str, int] | None = None,
                  prompt_version: str = "") -> None:
        u = usage or {}
        compact = str(note.get("compact") or "").strip()
        self.get_conn().execute(
            "INSERT OR REPLACE INTO reading_notes (run_id, book_id,"
            " chapter_index, chapter_title, note_json, compact, tokens_in,"
            " tokens_cached, tokens_out, tokens_reasoning, prompt_version,"
            " time_created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, book_id, chapter_index, chapter_title,
             json.dumps(note, ensure_ascii=False), compact,
             int(u.get("prompt_tokens") or 0),
             int(u.get("cached_tokens") or 0),
             int(u.get("completion_tokens") or 0),
             int(u.get("reasoning_tokens") or 0), prompt_version, _now_ms()),
        )
        self.get_conn().commit()

    def update_note_json(self, run_id: str, chapter_index: int,
                         note: dict[str, Any]) -> None:
        """只改 note_json（引文回填分级用），不动用量与 compact 列。"""
        compact = str(note.get("compact") or "").strip()
        self.get_conn().execute(
            "UPDATE reading_notes SET note_json=?, compact=? "
            "WHERE run_id=? AND chapter_index=?",
            (json.dumps(note, ensure_ascii=False), compact,
             run_id, int(chapter_index)),
        )
        self.get_conn().commit()

    def save_reset(self, run_id: str, seq: int, *, start_chapter: int,
                   end_chapter: int, summary: str,
                   tokens_before: int, tokens_after: int) -> None:
        self.get_conn().execute(
            "INSERT OR REPLACE INTO reading_resets (run_id, seq, start_chapter,"
            " end_chapter, summary, tokens_before, tokens_after, time_created)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (run_id, seq, start_chapter, end_chapter, summary,
             tokens_before, tokens_after, _now_ms()),
        )
        self.get_conn().commit()

    def save_map(self, book_id: str, *, start_chapter: int, end_chapter: int,
                 text: str, level: str = "arc", note_count: int = 0,
                 prompt_version: str = "") -> None:
        self.get_conn().execute(
            "INSERT OR REPLACE INTO reading_maps (book_id, level,"
            " start_chapter, end_chapter, text, note_count, prompt_version,"
            " time_created) VALUES (?,?,?,?,?,?,?,?)",
            (book_id, level, int(start_chapter), int(end_chapter), text,
             int(note_count), prompt_version, _now_ms()),
        )
        self.get_conn().commit()

    def maps_for(self, book_id: str, *, as_of: int | None = None,
                 level: str | None = None) -> list[dict[str, Any]]:
        """按起始章升序返回地图；``as_of`` 只收完整落在上界内的卷。"""
        conds, params = ["book_id=?"], [book_id]
        if as_of is not None:
            conds.append("end_chapter<=?")
            params.append(as_of)
        if level is not None:
            conds.append("level=?")
            params.append(level)
        rows = self.get_conn().execute(
            "SELECT * FROM reading_maps WHERE " + " AND ".join(conds)
            + " ORDER BY start_chapter ASC", tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def notes_for(self, book_id: str, as_of: int | None = None, *,
                  prompt_version: str | None = None) -> list[dict[str, Any]]:
        """按章升序返回每章最新一条笔记。

        ``as_of`` 是闭区间上界；``prompt_version`` 用于续跑时只复用同版本
        的笔记（旧小样不会混进全本）。
        """
        conds, params = ["book_id=?"], [book_id]
        if as_of is not None:
            conds.append("chapter_index<=?")
            params.append(as_of)
        if prompt_version is not None:
            conds.append("prompt_version=?")
            params.append(prompt_version)
        sql = ("SELECT * FROM reading_notes WHERE " + " AND ".join(conds)
               + " ORDER BY chapter_index ASC, time_created ASC")
        rows = self.get_conn().execute(sql, tuple(params)).fetchall()
        by_ch: dict[int, dict[str, Any]] = {}
        for r in rows:
            item = dict(r)
            try:
                item["note"] = json.loads(item.get("note_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["note"] = {}
            by_ch[int(item["chapter_index"])] = item
        return [by_ch[k] for k in sorted(by_ch)]


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def chapter_map(notes: list[dict[str, Any]],
                maps: list[dict[str, Any]] | tuple = (), *,
                as_of: int | None = None, max_chars: int = 4_000,
                header: bool = True) -> str:
    """认路地图：逐章 compact 的纯投影，卷级合成图优先。

    这是「前 n 章读到哪了」的可消费资产，不调模型。每章 100 字，80 章
    约 5.5k tokens；超出预算时先丢最早的单章行，保留卷图和最近章节——
    越新的路标对伴读/扮演越有用。

    **卷图是累积的**（每张都覆盖从第一章到本卷末），所以只取截至
    ``as_of`` 的最新一张，再补上它之后的逐章行；把十张卷图拼起来会把
    同一段历史重复十遍。
    """
    items = [n for n in notes
             if as_of is None or int(n.get("chapter_index") or 0) <= as_of]
    if not items:
        return ""
    arc: tuple[int, int, str] | None = None
    for m in maps or []:
        lo = int(m.get("start_chapter") or 0)
        hi = int(m.get("end_chapter") or 0)
        text = " ".join(str(m.get("text") or "").split())
        if not text or hi < lo:
            continue
        if as_of is not None and hi > as_of:
            continue
        if arc is None or hi > arc[1]:
            arc = (lo, hi, text)
    covered: set[int] = set()
    arc_lines: list[tuple[int, str]] = []
    if arc is not None:
        lo, hi, text = arc
        # 卷图是累积的：它已经概括了从第一章到本卷末，所以本卷之前的
        # 单章行全部让位，只保留卷末之后的逐章行。
        first = min(int(n.get("chapter_index") or 0) for n in items)
        covered.update(range(min(first, lo), hi + 1))
        arc_lines.append((lo, f"截至第 {hi} 章：{text}"))
    chap_lines: list[tuple[int, str]] = []
    for item in items:
        idx = int(item.get("chapter_index") or 0)
        if idx in covered:
            continue
        compact = str(item.get("compact")
                      or (item.get("note") or {}).get("compact") or "")
        if not compact:
            continue
        title = str(item.get("chapter_title") or "").strip()
        head = f"第 {idx} 章" + (f" {title}" if title else "")
        chap_lines.append((idx, f"{head}：{compact}"))
    last = max(int(n.get("chapter_index") or 0) for n in items)
    head = f'<reading-map through="{last}">\n' if header else ""
    tail = "\n</reading-map>" if header else ""
    budget = max_chars - len(head) - len(tail)
    arc_len = sum(len(line) + 1 for _, line in arc_lines)
    if arc_len > budget:
        # 卷图本身就超预算：截断最早的卷图，保证消费侧拿到的是前缀。
        kept_arcs, used = [], 0
        for idx, line in arc_lines:
            if used + len(line) + 1 > budget:
                break
            kept_arcs.append((idx, line[:budget - used - 1]))
            used += len(line) + 1
        arc_lines, arc_len = kept_arcs, used
    kept: list[tuple[int, str]] = []
    used = 0
    for idx, line in reversed(chap_lines):
        if arc_len + used + len(line) + 1 > budget:
            break
        kept.append((idx, line))
        used += len(line) + 1
    lines = [line for _, line in sorted(arc_lines + kept)]
    if not lines:
        return ""
    return head + "\n".join(lines) + tail


def highlights_for(notes: list[dict[str, Any]], *, kind: str | None = None,
                   tag: str | None = None, who: str | None = None,
                   as_of: int | None = None,
                   limit: int = 0) -> list[dict[str, Any]]:
    """从笔记里检索高亮（引文）。纯投影，不调模型、不建表。

    高亮就是笔记里的引文：``kind`` 分 literary（值得学写法）/ kernel
    （暴露人物内核），``tag`` 取自受控词表，``who`` 是这条指向的角色。
    旧笔记没分级（``kind`` 为空）时不匹配任何 kind 过滤——不猜。
    """
    out: list[dict[str, Any]] = []
    for item in notes:
        idx = int(item.get("chapter_index") or 0)
        if as_of is not None and idx > as_of:
            continue
        for q in (item.get("note") or {}).get("quotes") or []:
            if not isinstance(q, dict) or not q.get("text"):
                continue
            if kind and q.get("kind") != kind:
                continue
            if tag and q.get("tag") != tag:
                continue
            if who and q.get("who") != who:
                continue
            out.append({
                "chapter": idx,
                "chapter_title": str(item.get("chapter_title") or ""),
                "chunk": int(q.get("chunk") or 0),
                "text": str(q["text"]),
                "kind": str(q.get("kind") or ""),
                "tag": str(q.get("tag") or ""),
                "who": str(q.get("who") or ""),
                "why": str(q.get("why") or ""),
            })
    out.sort(key=lambda h: (h["chapter"], h["chunk"]))
    return out[:limit] if limit else out


def render_highlights(items: list[dict[str, Any]]) -> str:
    """高亮检索结果的可读文本（检查器/CLI 用）。"""
    lines: list[str] = []
    for h in items:
        meta = " · ".join(x for x in (h.get("kind"), h.get("tag"),
                                      h.get("who")) if x)
        head = f"[{h['chapter']}:{h['chunk']}]"
        lines.append(f"{head}「{h['text']}」")
        if meta:
            lines.append(f"    {meta}")
        if h.get("why"):
            lines.append(f"    {h['why']}")
    return "\n".join(lines)


def _prepend_map(body: str, map_text: str, max_chars: int,
                 share: float) -> str:
    """把地图放在正文前面，但最多占预算的 ``share``；太挤就整块丢掉。"""
    if not map_text:
        return body
    room = min(int(max_chars * share), max_chars - len(body) - 2)
    if room < 120:
        return body
    if len(map_text) > room:
        map_text = map_text[:room - 1] + "…"
    return map_text + "\n\n" + body


def scene_view(notes: list[dict[str, Any]], chapter: int, *,
               window: int = 1, max_chars: int = 6_000,
               map_text: str = "") -> str:
    """伴读视图：只取截至 ``chapter`` 的笔记，不碰后文。"""
    lo = max(0, chapter - max(0, window))
    parts: list[str] = []
    for item in notes:
        idx = int(item.get("chapter_index") or 0)
        if idx < lo or idx > chapter:
            continue
        note = item.get("note") or {}
        title = str(item.get("chapter_title") or "")
        parts.append(f"## 第 {idx} 章 {title}".strip())
        if note.get("scene"):
            parts.append(str(note["scene"]))
        for r in note.get("relations") or []:
            if isinstance(r, dict) and r.get("from") and r.get("to"):
                parts.append(f"- 关系：{r['from']} —({r.get('kind') or '其他'})"
                             f"→ {r['to']}：{_clip(r.get('note'), 120)}")
        for fact in note.get("facts") or []:
            parts.append(f"- 事实：{_clip(fact, 300)}")
        for q in note.get("quotes") or []:
            text = _clip((q or {}).get("text"), 200)
            why = _clip((q or {}).get("why"), 120)
            if text:
                parts.append(f"- 引文：「{text}」（{why}）")
        for th in note.get("threads") or []:
            parts.append(f"- 线索：{_clip(th, 200)}")
        if note.get("reading"):
            parts.append(f"解读：{_clip(note['reading'], 400)}")
        parts.append("")
    text = "\n".join(parts).strip()
    text = _prepend_map(text, map_text, max_chars, 0.5)
    return text if len(text) <= max_chars else text[:max_chars - 1] + "…"


def roleplay_kernel(notes: list[dict[str, Any]], as_of: int, *,
                    max_chars: int = 4_000, recent_facts: int = 20,
                    map_text: str = "") -> str:
    """扮演内核：合并截至 ``as_of`` 的角色状态、事实与声音样张。"""
    chars: dict[str, dict[str, Any]] = {}
    facts: list[tuple[int, str]] = []
    quotes: list[tuple[int, str]] = []
    rels: dict[tuple[str, str, str], tuple[int, str]] = {}
    for item in notes:
        idx = int(item.get("chapter_index") or 0)
        if idx > as_of:
            continue
        note = item.get("note") or {}
        for c in note.get("characters") or []:
            if not isinstance(c, dict):
                continue
            name = _clip(c.get("name"), 60)
            if not name:
                continue
            aliases = [_clip(a, 60) for a in (c.get("aliases") or [])
                       if _clip(a, 60)]
            canonical = name
            if name not in chars:
                # 模型用旧称呼当 name：归到已有规范名。
                canonical = next(
                    (k for k, v in chars.items()
                     if name in (v.get("aliases") or [])), name)
            cur = chars.setdefault(canonical, {"chapter": idx, "aliases": []})
            for alias in aliases:
                old = chars.get(alias)
                if alias == canonical or not isinstance(old, dict):
                    continue
                # 本章揭示了规范名：把旧称呼下的状态并进来（新字段优先）。
                chars.pop(alias, None)
                for key in ("wants", "fears", "knows", "state"):
                    if old.get(key) and not cur.get(key):
                        cur[key] = old[key]
                cur["aliases"] = list(dict.fromkeys(
                    (cur.get("aliases") or []) + (old.get("aliases") or [])))
            cur["aliases"] = list(dict.fromkeys(
                (cur.get("aliases") or []) + aliases))
            for key in ("wants", "fears", "knows", "state"):
                if c.get(key):
                    cur[key] = _clip(c[key], 400)
            cur["chapter"] = idx
        for r in note.get("relations") or []:
            if not isinstance(r, dict) or not r.get("from") or not r.get("to"):
                continue
            key = (str(r["from"]), str(r["to"]),
                   str(r.get("kind") or "其他"))
            rels[key] = (idx, _clip(r.get("note"), 120))
        for fact in note.get("facts") or []:
            facts.append((idx, _clip(fact, 300)))
        for q in note.get("quotes") or []:
            text = _clip((q or {}).get("text"), 200)
            if text:
                quotes.append((idx, text))

    lines: list[str] = [f"# 扮演内核（截至第 {as_of} 章）", ""]
    for name, c in chars.items():
        alias_text = ("；别名：" + "、".join(c["aliases"])
                      if c.get("aliases") else "")
        lines.append(f"## {name}{alias_text}"
                     f"（最近状态：第 {c.get('chapter')} 章）")
        for key, label in (("wants", "想要"), ("fears", "顾虑"),
                           ("knows", "知道"), ("state", "处境")):
            if c.get(key):
                lines.append(f"- {label}：{c[key]}")
        lines.append("")
    if rels:
        def canon(n: str) -> str:
            for cname, c in chars.items():
                if n == cname or n in (c.get("aliases") or []):
                    return cname
            return n

        lines.append("## 关系（最近）")
        for (a, b, kind), (idx, note) in list(rels.items())[-12:]:
            tail = f"：{note}" if note else ""
            lines.append(f"- {canon(a)} —({kind})→ {canon(b)}{tail}"
                         f"（第 {idx} 章）")
        lines.append("")
    if facts:
        lines.append("## 已确立的事实（最近）")
        for idx, fact in facts[-recent_facts:]:
            lines.append(f"- [第 {idx} 章] {fact}")
        lines.append("")
    if quotes:
        lines.append("## 声音样张（逐字）")
        for idx, text in quotes[-8:]:
            lines.append(f"- 「{text}」—— 第 {idx} 章")
    text = "\n".join(lines).strip()
    if map_text:
        title, _, rest = text.partition("\n\n")
        rest = _prepend_map(rest.strip(), map_text,
                            max(0, max_chars - len(title) - 2), 0.4)
        text = title + ("\n\n" + rest if rest else "")
    return text if len(text) <= max_chars else text[:max_chars - 1] + "…"




