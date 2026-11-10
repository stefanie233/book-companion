"""逐章阅读会话（原型）。

上下文模型：
  [system(稳定)] + 每章 [user(正文+任务), assistant(JSON 笔记)] + 工具轮消息
只追加；超过阈值才换窗 = [system, 摘要块, 最近 N 章原文]。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from hashlib import sha1
from dataclasses import dataclass, field
from typing import Any

import aiohttp

import chat_context
from bookgraph.llm_call import (
    MAX_OUTPUT_BUDGET, cached_tokens, is_throttle, is_truncated, output_budget,
    reasoning_text, reasoning_tokens, retry_delay, usage_tokens,
)
from llm_json import loads_tolerant
from reading import map as map_mod
from reading import prompts
from reading.store import ReadingStore
from reading.tools import ReadingTools
from storage.db import BookDB
from storage.models import BookChapterRow

log = logging.getLogger("reading")


class _Truncated(RuntimeError):
    """输出被预算截断；加预算已无效，必须让上层看见。"""

#: 默认只读正文。前言/后记要显式打开——它们常是作者生平或他人序，
#: 不是小说正文，混进来会污染场景笔记与扮演内核。
BODY_TYPES = ("body",)
FRONT_MATTER = ("preface", "postscript")

#: 阅读单次输出预算（推理 + 正文）。bookgraph 默认 64k 是给整批提取的；
#: 一章笔记只需 1–3k 正文，64k 会放任长推理把单次调用拖到十几分钟。
#: 撞顶仍会翻倍（见 ``_call``），正常阅读用不到。
READING_MAX_TOKENS = 16_000


@dataclass
class TurnUsage:
    calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, data: dict) -> None:
        pin, pout = usage_tokens(data)
        self.calls += 1
        self.prompt_tokens += pin
        self.cached_tokens += cached_tokens(data)
        self.completion_tokens += pout
        self.reasoning_tokens += reasoning_tokens(data)

    def as_dict(self) -> dict[str, int]:
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "cached_tokens": self.cached_tokens,
                "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens}


class ReadingSession:
    """一次逐章阅读 run。构造即开 run，``run()`` 负责收尾。"""

    def __init__(self, cfg: Any, books_db: BookDB, store: ReadingStore,
                 book_id: str, *, window_tokens: int,
                 threshold_ratio: float = 0.35, recent_chapters: int = 3,
                 max_tool_rounds: int = 4, context_mode: str = "full",
                 resume: bool = False, recorder: Any = None,
                 log_cb: Any = None, record_usage: bool = True):
        try:
            from provider_store import config_for
            cfg = config_for(cfg, "reading.session")
        except Exception:
            pass
        self.cfg = cfg
        self.books_db = books_db
        self.store = store
        self.book_id = book_id
        book = books_db.get_book(book_id)
        self.book_title = book.title if book else ""
        self.chapters: list[BookChapterRow] = books_db.get_chapters(book_id)
        parts: dict[int, list[str]] = {}
        for ck in books_db.get_chunks(book_id):
            parts.setdefault(int(ck.chapter_index), []).append(ck.content)
        self.texts = {k: "\n".join(v) for k, v in parts.items()}
        self.window_tokens = max(1, int(window_tokens))
        self.threshold = int(self.window_tokens * threshold_ratio)
        self.recent_chapters = max(1, recent_chapters)
        self.max_tool_rounds = max(0, max_tool_rounds)
        # full：完整 JSON 笔记进上下文（默认，连续性最好，miss 也最高）；
        # compact：只把 compact 进上下文，完整笔记只落库（省 ~700 tokens/章）。
        self.context_mode = ("compact" if str(context_mode).lower()
                             == "compact" else "full")
        self.resume = bool(resume)
        self._resumed_done: set[int] = set()
        self.log_cb = log_cb
        self.record_usage = record_usage
        # 飞行记录仪：把每次调用的 system + messages 原样落库，提示词
        # 调优和工作流复盘都靠它。recorder 为空时静默跳过。
        self.recorder = recorder
        # 换窗前的地图合成；测试可注入假实现，避免真打网络。
        self.map_synth = map_mod.synthesize

        self.messages: list[dict] = [{"role": "system",
                                      "content": prompts.READING_SYSTEM}]
        self.notes: dict[int, dict] = {}
        self.chapter_messages: dict[int, list[dict]] = {}
        self.selected: list[BookChapterRow] = []
        self.totals = TurnUsage()
        self.failures: list[dict] = []
        self.resets = 0
        self._last_prompt_tokens = 0
        self._call_no = 0
        self.tools = ReadingTools(
            db=books_db, book_id=book_id,
            chapters={int(c.chapter_index): c for c in self.chapters},
            coverage_fn=self._coverage)
        self.run_id = store.start_run(
            book_id, model=str(getattr(cfg, "model", "")),
            window_tokens=self.window_tokens,
            threshold_tokens=self.threshold,
            prompt_version=prompts.READING_PROMPT_VERSION)

    # ── 对外 ──────────────────────────────────────────────────────────────

    async def run(self, *, limit: int = 0,
                  chapter_from: int | None = None,
                  chapter_to: int | None = None,
                  include_front_matter: bool = False,
                  min_chars: int = 50) -> dict[str, Any]:
        allowed = BODY_TYPES + (FRONT_MATTER if include_front_matter else ())
        selected = [c for c in self.chapters
                    if c.chapter_type in allowed
                    and len(self.texts.get(int(c.chapter_index), ""))
                    >= max(0, min_chars)]
        if chapter_from is not None:
            selected = [c for c in selected
                        if int(c.chapter_index) >= chapter_from]
        if chapter_to is not None:
            selected = [c for c in selected
                        if int(c.chapter_index) <= chapter_to]
        if limit:
            selected = selected[:limit]
        self.selected = selected
        done: dict[int, dict] = {}
        if self.resume:
            rows = self.store.notes_for(
                self.book_id,
                prompt_version=prompts.READING_PROMPT_VERSION)
            wanted = {int(c.chapter_index) for c in selected}
            for r in rows:
                idx = int(r.get("chapter_index") or 0)
                if idx in wanted and (r.get("compact") or r.get("note")):
                    done[idx] = r
            if done:
                self.notes = {idx: (r.get("note") or {})
                              for idx, r in done.items()}
                entries = [(idx, r.get("compact") or "")
                           for idx, r in sorted(done.items())]
                summary = prompts.summary_block(entries)
                self.messages = [
                    self.messages[0],
                    {"role": "user", "content": summary},
                    {"role": "assistant",
                     "content": "已读摘要已载入，从中断处继续。"},
                ]
                self._last_prompt_tokens = chat_context.estimate_tokens(
                    self.messages)
                self._resumed_done = set(done)
                self._emit({"type": "resume", "done": len(done),
                            "pending": len(selected) - len(done)})
        # 非流式（stream=False）时服务端在生成完之前一个字节都不发，
        # sock_read=150 会把「长章节 + 长推理」误杀（窄门 7k 字章节实测）。
        # 只留总预算：480s，足够 15k 字章节；3 次尝试后单章失败继续。
        timeout = aiohttp.ClientTimeout(total=480, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            self._http = http
            for pos, ch in enumerate(selected, start=1):
                if int(ch.chapter_index) in done:
                    continue
                await self._read_one(ch, pos, len(selected))
        self.store.finish_run(
            self.run_id,
            status="ok" if not self.failures else "partial",
            chapters_done=len(self.notes), totals=self._totals())
        return self.summary()

    async def _read_one(self, ch: BookChapterRow, pos: int,
                        total: int) -> None:
        idx = int(ch.chapter_index)
        self.tools.current_chapter = idx
        task = prompts.chapter_task(
            idx, ch.title or "", self.texts.get(idx, ""),
            read_before=pos - 1, total=total)
        est = chat_context.estimate_tokens([{"role": "user", "content": task}])
        if (self._last_prompt_tokens
                and self._last_prompt_tokens + est > self.threshold
                and len(self.chapter_messages) > self.recent_chapters):
            await self._reset_context(next_ordinal=pos)

        start = len(self.messages)
        self.messages.append({"role": "user", "content": task})
        usage = TurnUsage()
        t0 = time.monotonic()
        try:
            note, content = await self._turn(usage)
            assistant_text = content
            if self.context_mode == "compact":
                assistant_text = (f"第 {idx} 章笔记："
                                  f"{note.get('compact') or ''}")
            self.messages.append({"role": "assistant",
                                  "content": assistant_text})
            self.chapter_messages[idx] = self.messages[start:]
            self.notes[idx] = note
            self.store.save_note(
                self.run_id, self.book_id, idx, note,
                chapter_title=ch.title or "", usage=usage.as_dict(),
                prompt_version=prompts.READING_PROMPT_VERSION)
            status = "ok"
        except Exception as exc:                              # noqa: BLE001
            log.warning("chapter %s failed: %s", idx, exc)
            self.messages.append({
                "role": "assistant",
                "content": f"（本章读取失败：{type(exc).__name__}，稍后重读。）"})
            self.failures.append({"chapter": idx,
                                  "error": f"{type(exc).__name__}: {exc}"})
            status = "failed"
        self._merge(usage)
        self._emit({
            "type": "chapter_done", "status": status,
            "chapter": idx, "ordinal": pos, "total": total,
            "seconds": round(time.monotonic() - t0, 1),
            "usage": usage.as_dict(),
            "compact": (self.notes.get(idx) or {}).get("compact", ""),
            "last_prompt_tokens": self._last_prompt_tokens,
        })

    def _merge(self, usage: TurnUsage) -> None:
        self.totals.calls += usage.calls
        self.totals.prompt_tokens += usage.prompt_tokens
        self.totals.cached_tokens += usage.cached_tokens
        self.totals.completion_tokens += usage.completion_tokens
        self.totals.reasoning_tokens += usage.reasoning_tokens

    def _totals(self) -> dict[str, Any]:
        d = self.totals.as_dict()
        prompt = max(1, d["prompt_tokens"])
        d.update({
            "run_id": self.run_id,
            "book_id": self.book_id,
            "book_title": self.book_title,
            "chapters_done": len(self.notes),
            "resets": self.resets,
            "failures": list(self.failures),
            "hit_ratio": round(d["cached_tokens"] / prompt, 4),
            "miss_tokens": max(0, d["prompt_tokens"] - d["cached_tokens"]),
        })
        return d

    def summary(self) -> dict[str, Any]:
        return self._totals()

    def _emit(self, event: dict[str, Any]) -> None:
        if self.log_cb:
            try:
                self.log_cb(event)
            except Exception:
                pass

    def _coverage(self) -> dict[str, Any]:
        noted = sorted(self.notes)
        chars = sorted({str(c.get("name")) for n in self.notes.values()
                        for c in (n.get("characters") or [])
                        if isinstance(c, dict) and c.get("name")})
        return {"read_through": self.tools.current_chapter,
                "chapters_total": len(self.selected), "noted": noted,
                "characters": chars}

    # ── 一轮对话（含工具）────────────────────────────────────────────────

    async def _turn(self, usage: TurnUsage) -> tuple[dict, str]:
        # +2：最后一轮禁用工具后，若模型仍硬调工具，还要再给它一次
        # 「只输出 JSON」的机会；否则工具结果刚回来就被判死。
        for round_no in range(self.max_tool_rounds + 2):
            use_tools = round_no < self.max_tool_rounds
            data = await self._call(
                self.messages, usage,
                tools=self.tools.schemas() if use_tools else None)
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            content = str(msg.get("content") or "").strip()
            calls = msg.get("tool_calls") or []
            if calls:
                self.messages.append({
                    "role": "assistant", "content": content or None,
                    "tool_calls": calls})
                for tc in calls:
                    fn = tc.get("function") or {}
                    name = str(fn.get("name") or "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": str(tc.get("id") or ""),
                        "content": self.tools.execute(name, args)})
                if not use_tools:
                    self.messages.append({
                        "role": "user",
                        "content": "工具轮次已用完，不要再调用工具，直接输出 JSON。"})
                continue
            if content:
                note = self._parse_note(content)
                if note is not None:
                    return note, content
                self.messages.append({"role": "assistant",
                                      "content": content})
                self.messages.append({
                    "role": "user",
                    "content": prompts.REPAIR_PROMPT.format(
                        error="JSON 解析失败")})
                data = await self._call(self.messages, usage, tools=None)
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                content = str(msg.get("content") or "").strip()
                note = self._parse_note(content)
                if note is not None:
                    return note, content
                raise ValueError("模型两次都没有输出合法 JSON")
            self.messages.append({"role": "assistant", "content": ""})
            self.messages.append({"role": "user",
                                  "content": "请按 system 的字段输出一个 JSON 对象。"})
        raise ValueError("工具轮次耗尽仍未产出笔记")

    def _parse_note(self, content: str) -> dict | None:
        obj = loads_tolerant(content, expect=dict)
        if not isinstance(obj, dict):
            return None
        note = dict(obj)
        note["scene"] = str(note.get("scene") or "")
        note["reading"] = str(note.get("reading") or "")
        chars: list[dict] = []
        for c in note.get("characters") or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            item = {k: str(c.get(k) or "")
                    for k in ("name", "wants", "fears", "knows", "state")}
            item["aliases"] = [str(a) for a in (c.get("aliases") or [])
                               if str(a).strip()]
            chars.append(item)
        note["characters"] = chars
        rels: list[dict] = []
        for r in note.get("relations") or []:
            if not isinstance(r, dict) or not r.get("from") or not r.get("to"):
                continue
            rels.append({"from": str(r["from"]), "to": str(r["to"]),
                         "kind": str(r.get("kind") or "其他"),
                         "note": str(r.get("note") or "")})
        note["relations"] = rels
        for key in ("facts", "threads"):
            note[key] = [str(x) for x in (note.get(key) or [])
                         if str(x).strip()]
        quotes, dropped = [], 0
        seen: set[str] = set()
        for q in note.get("quotes") or []:
            q = {"text": q} if isinstance(q, str) else (q or {})
            text = str(q.get("text") or "").strip()
            if not text:
                continue
            # 同一句在一章里出现两次（模型重复列举）只留第一条。
            key = sha1(" ".join(text.split()).encode()).hexdigest()[:8]
            if key in seen:
                continue
            seen.add(key)
            kind = str(q.get("kind") or "").strip().lower()
            if kind not in ("literary", "kernel"):
                kind = ""
            tag = str(q.get("tag") or "").strip()
            if tag and tag not in prompts.QUOTE_TAGS:
                tag = "other"
            who = str(q.get("who") or "").strip()
            res = self.tools.verify_text(text)
            hits = res.get("hits") or []
            if res.get("found") and hits:
                # 同一句在多章出现时取最早锚点：剧透门按章增量判断，
                # 用最晚的会把「读者早就知道的话」误判成后文。
                hit = min(hits, key=lambda h: (int(h.get("chapter") or 0),
                                               int(h.get("chunk") or 0)))
                quotes.append({"text": text,
                               "why": str(q.get("why") or ""),
                               "kind": kind, "tag": tag, "who": who,
                               "chapter": int(hit.get("chapter") or 0),
                               "chunk": int(hit.get("chunk") or 0)})
            else:
                dropped += 1
        note["quotes"] = quotes
        note["unverified_quotes"] = dropped
        note["quote_tags_version"] = prompts.TAG_VERSION
        compact = str(note.get("compact") or "").strip()
        if not compact:
            compact = note["scene"] or note["reading"]
        note["compact"] = compact[:160]
        return note

    async def _reset_context(self, *, next_ordinal: int) -> None:
        order = list(self.chapter_messages)
        if len(order) <= self.recent_chapters:
            return
        keep = order[-self.recent_chapters:]
        # 续跑时已完成章的原文不在内存里，它们只以 compact 进摘要；
        # 第一次换窗后清空，之后按本轮 chapter_messages 走。
        old = sorted(set(self._resumed_done)
                     | set(order[:-self.recent_chapters]))
        entries = [(idx, (self.notes.get(idx) or {}).get("compact", ""))
                   for idx in old]
        # 换窗前缀：能合成地图就用地图（定长、有结构），失败退回 compact
        # 拼接（机械兜底，保证换窗绝不因合成失败而失败）。
        summary = prompts.summary_block(entries)
        synthesized = await self.map_synth(
            self.cfg, "", [(idx, "", compact) for idx, compact in entries],
            target_chars=1200, recorder=self.recorder,
            session_id=f"reading-map:{self.book_id}", book_id=self.book_id)
        if synthesized:
            summary = map_mod.map_block(synthesized, int(old[0]), int(old[-1]))
            self.store.save_map(
                self.book_id, start_chapter=int(old[0]),
                end_chapter=int(old[-1]), text=synthesized, level="reset",
                note_count=len(entries),
                prompt_version=map_mod.MAP_PROMPT_VERSION)
        new = [self.messages[0],
               {"role": "user", "content": summary},
               {"role": "assistant", "content": "已读摘要已载入，继续。"}]
        for idx in keep:
            new.extend(self.chapter_messages[idx])
        before = self._last_prompt_tokens or chat_context.estimate_tokens(
            self.messages)
        after = chat_context.estimate_tokens(new)
        self.resets += 1
        self.store.save_reset(
            self.run_id, self.resets,
            start_chapter=int(old[0]), end_chapter=int(old[-1]),
            summary=summary, tokens_before=before, tokens_after=after)
        self.messages = new
        self._resumed_done = set()
        # 下一次换窗判断必须用压缩后的尺寸；否则旧的大数值会让它每章
        # 都误判超预算、连续换窗。
        self._last_prompt_tokens = after
        self._emit({"type": "reset", "seq": self.resets,
                    "chapters": [int(old[0]), int(old[-1])],
                    "next_ordinal": next_ordinal,
                    "tokens_before": before, "tokens_after": after})

    # ── LLM 调用 ─────────────────────────────────────────────────────────

    async def _call(self, messages: list[dict], usage: TurnUsage, *,
                    tools: list[dict] | None = None) -> dict:
        cfg = self.cfg
        base = str(getattr(cfg, "api_base", "")
                   or getattr(cfg, "api_base_url", "")).rstrip("/")
        key = str(getattr(cfg, "api_key", ""))
        model = str(getattr(cfg, "model", ""))
        if not base or not key:
            raise RuntimeError("缺少 api_base / api_key")
        budget = min(output_budget(cfg), READING_MAX_TOKENS)
        body: dict[str, Any] = {
            "model": model, "messages": messages, "temperature": 0.15,
            "stream": False, "max_tokens": budget}
        if tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {key}",
                   "Content-Type": "application/json"}
        last: Exception | None = None
        throttled, bumps = 0, 0
        for attempt in range(3):
            t0 = time.monotonic()
            try:
                async with self._http.post(
                        f"{base}/chat/completions", json=body,
                        headers=headers) as resp:
                    text = await resp.text()
                    if resp.status == 429:
                        throttled += 1
                        raise RuntimeError(f"429: {text[:200]}")
                    if resp.status != 200:
                        raise RuntimeError(
                            f"API {resp.status}: {text[:200]}")
                    data = json.loads(text)
                usage.add(data)
                self._call_no += 1
                pin, pout = usage_tokens(data)
                self._last_prompt_tokens = pin
                self._record_usage(data, model)
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                self._emit({"type": "llm_call", "call": self._call_no,
                            "chapter": self.tools.current_chapter,
                            "prompt": pin, "cached": cached_tokens(data),
                            "out": pout,
                            "reason": reasoning_tokens(data),
                            "tools_offered": bool(tools),
                            "tool_calls": len(msg.get("tool_calls") or [])})
                self._record_call(messages, data, model, t0, tools)
                if is_truncated(data):
                    bumps += 1
                    if bumps <= 2:
                        budget = min(budget * 2, MAX_OUTPUT_BUDGET)
                        body["max_tokens"] = budget
                        log.warning("reading output truncated; "
                                    "budget -> %d", budget)
                        continue
                    raise _Truncated("输出被截断（已两次抬预算）")
                return data
            except _Truncated:
                raise
            except Exception as exc:                          # noqa: BLE001
                last = exc
                if attempt < 2:
                    delay = retry_delay(attempt, throttled)
                    log.warning("reading call %d failed: %s; sleep %.1fs",
                                attempt + 1, exc, delay)
                    await asyncio.sleep(delay)
        raise RuntimeError(f"LLM 调用失败：{last}")

    def _record_usage(self, data: dict, model: str) -> None:
        if not self.record_usage:
            return
        try:
            import usage_log
            pin, pout = usage_tokens(data)
            usage_log.record(model=model, tokens_in=pin,
                             tokens_out=pout, source="reading.session")
        except Exception:
            pass

    def _record_call(self, messages: list[dict], data: dict, model: str,
                     t0: float, tools: list[dict] | None) -> None:
        if self.recorder is None:
            return
        try:
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            content = str(msg.get("content") or "")
            reasoning = reasoning_text(data)
            response = (f"<think>\n{reasoning}\n</think>\n{content}"
                        if reasoning else content)
            has_system = bool(messages and messages[0].get("role") == "system")
            system = str(messages[0].get("content") or "") if has_system else ""
            rest = messages[1:] if has_system else messages
            self.recorder.record_round(
                session_id=f"reading:{self.run_id}",
                turn_index=self.tools.current_chapter,
                react_round=self._call_no,
                system_prompt=system,
                messages=rest,
                tools_count=len(tools or []),
                mode=prompts.READING_PROMPT_VERSION,
                book_id=self.book_id,
                response_text=response,
                tool_calls=msg.get("tool_calls") or None,
                model=model,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception:
            # 记录器绝不能影响阅读本身。
            log.warning("record reading call failed", exc_info=True)







