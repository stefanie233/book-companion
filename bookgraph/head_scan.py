"""HeadScanner — book-head region scan (v0).

Scans from the very start of a book until the preface start line, disposing of
copyright identity data, isolating promo/ad noise, and marking the boundary
where the pipeline hands off to the next agent.

Architecture (定稿 v0, see docs/graph-agent-mapping.md §五-b):
  - Sliding window, one new block per call (slide-by-1); the first call covers
    chunks 0..FIRST_WINDOW-1 so it can't start blind.
  - Stop predicate lives in the ORCHESTRATOR (accumulated regions), not the
    model: two consecutive "preface" region verdicts terminate the scan.
  - Overlapping blocks are NOT re-sent in full — prior two regions travel as a
    short conclusion line (key:region) to keep each round small.
  - Identity merges across windows; the longest value wins per field.
  - Tool `get_chunk` is NOT wired in v0 — the copyright-block full-text refine
    round (IDENTITY_REFINE_PROMPT) covers the ISBN-in-the-middle case that the
    head/tail preview truncates. Degraded judges fall back to ambiguous.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Coroutine

import aiohttp

from config import Config

log = logging.getLogger("bookgraph")

# ── Tuning ────────────────────────────────────────────────────────────────────

# 滑动指针上限:处理到第 MAX_HEAD_BLOCKS 块仍未连续两次 preface 即降级退出。
MAX_HEAD_BLOCKS = 24
# 连续两块 preface 判定为到达 preface 起始线。
PREFACE_CONSECUTIVE = 2
# 预览分界:≤ 150 字全量注入;> 150 字取首尾各 75 字。
PREVIEW_FULL_LEN = 150
PREVIEW_SIDE = 75
# 首轮窗口大小。
FIRST_WINDOW = 3
# 连续判定失败次数上限,超过则当作 degraded 提前终止。
MAX_CONSECUTIVE_FAILS = 2

# 版权块身份补全提示词:预览"首尾各75字"可能截掉中段字段(如 ISBN/出版年份),
# 对判为 copyright 且超过预览上限的块,拿全文再做一轮身份补全。
IDENTITY_REFINE_PROMPT = """以下是一本书"版权页/丛书页"的完整全文。它是版权身份数据。
提取其中属于 书名/作者/译者/出版社/ISBN/出版年份/丛书名/版次 的字段。
规则:
- 只能填原文出现的,原文没有的字段不输出。
- evidence 逐字摘录自原文。
- 宣传语、推荐语、联系方式(微博/公众号/邮箱/电话)、营销文案一律不提取。
直接输出 JSON:{"identity": [{"field":"出版社","value":"...","evidence":"..."}]}"""


def _build_identity_refine_msg(ch: Any, chapter_title: str | None) -> str:
    head, body = format_chunk_preview(ch, chapter_title)
    # 全量注入:覆盖 preview 截断的字段。
    return f"{head} (全文如下)\n\n{ch.content or ''}"


# ── System prompt(定稿,与 mapping 文档 §五-b 一致)────────────────────────────

HEAD_SCANNER_SYSTEM_PROMPT = """你是这本书"头部区块处置员"。你只负责从书头到"序/preface"起始线之间的区域:
处置完版权页、隔离广告噪音、划出序的起始线,然后把后续交付给流水线下一个代理。

【分块逻辑——你必须知道的事实】
1. 正文以 chunk 存储。chunk 由若干完整段落组成——切分是贪心算法按段落累计
   (~800字/块),段落是原子的,chunk 不会从段落中间断开。
2. 全书每 chunk 平均 900~1000 字。版权页/广告页/标题页常整页只有 1 块,
   字数远小于均值(几十~几百字)。
3. 章节标题来自程序解析 EPUB 时的产物:标题未必准确,仅供参考,不是判决依据。
4. 你收到的是"预览块":≤150 字给全量,>150 字给首尾各 75 字。
   预览不足以判断时,把该块标为 ambiguous(系统有降级处理),不要乱猜。

【本次任务目标(本轮)】
对当前的新块:
1. identity:提取纯净版权身份信息。字段白名单:书名/作者/译者/出版社/ISBN/
   出版年份/丛书名/版次。只填原文出现的,原文没有的留 null,严禁编造;
   每条必须带 evidence(原文摘录)。已被前史覆盖的字段不重复输出。
2. boundary:给新块打区域标:
   copyright | promo(营销噪音) | preface(序/导读/编者语) | toc | body_lead(已进正文) | ambiguous
3. 若新块判为 preface,且前史上一块也判为 preface,报告 preface_line_reached=true。

【声噪与序类情景(重要分层)】
正文之外的块要区分三类,别混淆:
1. promo(营销噪音,唯一该"剔除")——以导流为目的:整页为"加小编QQ / 关注公众号 /
   获取免费电子书 / 网站地址 / 扫码下载",引导去站外。此类标 promo。
2. preface(序/导读/编者语)——包括**:推荐序、译者序、作者序、编者按、导读,以及
   名人赞誉/推荐语页**(多位名人引述对本书的评价)。它们都是"他人或作者对本书的观点",
   是评价通道素材,标 preface,不进正文图谱、也不进 copyright identity。
3. copyright(版权身份)——仅当块内包含 书名/作者/译者/出版社/ISBN/出版年份/丛书名/版次
   这样的版权字段时才标 copyright,并提取 identity。

【identity 提取规则(严格)】
- 仅当当前块判为 copyright 时,才输出 identity 字段。
- evidence 必须**逐字摘录自当前块原文**,不得从赞誉、序言、导读等处搬运信息。
- 原文没有的字段留 null,严禁编造。

【示例】
例1 · 纯净版权页(→ copyright,字段全提): COPYRIGHT / 书名 / 作者 / 出版社 / 出版时间 / ISBN。
例2 · 广告引流页(→ promo,整块剔除): 加小编QQ/关注公众号/访问网站。(真实ID均已脱敏)
例3 · 出版社联系页(→ copyright,仅提身份字段,联系方式不进 identity): 书名/作者/译者。
例4 · 丛书页(→ copyright,含 ISBN 提取;丛书宣传语只作 evidence 上下文,不单独成字段): 丛书名大字 + ISBN。
例5 · 名人赞誉页(→ preface,评价通道素材): 多位名人的推荐语与署名。
例6 · 推荐序/译者序/作者序(→ preface): 通常有标题与署名,正文是对书的评价或创作缘起。
以上是常见形态。也可能存在你未见过的促销、跳转引导等——凡不属于白名单字段的内容,一律不进 identity。

【输出格式】严格 JSON,无额外文本:
{
  "identity": [{"field":"出版社","value":"北京联合出版公司·磨铁","evidence":"出版社:..."}],
  "boundary": "copyright",
  "preface_line_reached": false,
  "confidence": 0.9
}
"""

# ── Preview formatting ────────────────────────────────────────────────────────


def format_chunk_preview(chunk: Any, chapter_title: str | None) -> tuple[str, str]:
    """Return (header_line, body_preview) for one chunk."""
    content = chunk.content or ""
    n = len(content)
    title = chapter_title or f"第{chunk.chapter_index + 1}章"
    head = f"[F#{chunk.chapter_index}-{chunk.chunk_index}] " \
           f"章节={title}·程序产物 len={n}"
    if n <= PREVIEW_FULL_LEN:
        return head, content
    body = content[:PREVIEW_SIDE] + "\n……(中段省略)……\n" + content[-PREVIEW_SIDE:]
    return head, body


def _chunk_key(ch: Any) -> str:
    return f"F#{ch.chapter_index}-{ch.chunk_index}"


def _build_first_msg(book_title: str, book_author: str,
                     first_blocks: list[Any],
                     chapter_titles: dict[int, str]) -> str:
    """First round: window of three full previews, no prior history."""
    lines = [f"书名:《{book_title}》", f"作者:{book_author}",
             "注:这是预览块。≤150 字为全量;>150 字为首尾各 75 字。",
             "前史:无(本轮为窗口第一天,三块全送)。",
             "本轮共 3 块。请输出 JSON 对象:boundaries 含这 3 个块的 key 与各自区域;",
             "identity 仅在出现身份信息时输出;如已有连续两块 preface,置 preface_line_reached=true。",
             ""]
    for ch in first_blocks:
        head, body = format_chunk_preview(
            ch, chapter_titles.get(ch.chapter_index))
        lines += [head, body, ""]
    return "\n".join(lines)


def _build_round_msg(book_title: str, book_author: str,
                     new_block: Any, chapter_titles: dict[int, str],
                     prior: list[tuple[str, str]]) -> str:
    """Rounds after the first: one new block + prior-two conclusions."""
    lines = [f"书名:《{book_title}》", f"作者:{book_author}",
             "注:预览块,≤150字为全量,>150字为首尾各75字。",
             "前史(已处置结论,不重发全文):"
             + ("; ".join(f"{k}:{r}" for k, r in prior) or "无"),
             ""]
    head, body = format_chunk_preview(new_block, chapter_titles.get(new_block.chapter_index))
    lines += [head, body]
    return "\n".join(lines)


# ── Accumulation helpers ──────────────────────────────────────────────────────


def _merge_identity(identity: dict[str, dict], items: list[dict]) -> None:
    """Fold round identity into the accumulator. Longest value wins per field."""
    for it in items or []:
        field = (it.get("field") or "").strip()
        if not field:
            continue
        val = it.get("value") or ""
        ev = it.get("evidence") or ""
        cur = identity.get(field)
        if cur is None or len(val) > len(cur.get("value", "")):
            identity[field] = {"value": val, "evidence": ev}


def _preface_line_reached(boundaries: dict[str, str]) -> bool:
    """Stop predicate: last <=PREFACE_CONSECUTIVE regions all 'preface'."""
    regs = list(boundaries.values())
    if len(regs) < PREFACE_CONSECUTIVE:
        return False
    return all(r == "preface" for r in regs[-PREFACE_CONSECUTIVE:])


def _preface_start(boundaries: dict[str, str]) -> str | None:
    for k, r in boundaries.items():
        if r == "preface":
            return k
    return None


# ── Single (stateless) LLM call ───────────────────────────────────────────────


async def _call_once(session: aiohttp.ClientSession, config: Config,
                     sys_prompt: str, user_msg: str, chunk_key: str | None,
                     timeout: int = 90,
                     log_cb: Callable[[dict], None] | None = None,
                     ) -> tuple[dict | None, bool]:
    """One stateless round. Returns (parsed_json, ok). `ok` true iff the HTTP
    call succeeded AND the response parsed as JSON. chunk_key is stamped into
    the parsed dict by the orchestrator for round attribution."""
    body = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 16000,
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.monotonic()

    def _emit(status: str, response: str, *, err: str | None = None) -> None:
        if log_cb is None:
            return
        try:
            log_cb({
                "status": status,
                "prompt": f"[system]\n{sys_prompt}\n\n[user]\n{user_msg}",
                "response": response,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "attempt": 1, "max_attempts": 1,
                "error_detail": err,
            })
        except Exception:
            log.warning("HeadScan log_cb failed", exc_info=True)

    try:
        async with session.post(
            f"{config.api_base}/chat/completions",
            json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                _emit("api_error", "", err=f"API {resp.status}: {err[:200]}")
                return None, False
            data = json.loads(await resp.text())
            content = data["choices"][0]["message"].get("content") or ""
            parsed = _parse_json(content)
            if parsed is None:
                _emit("parse_failed", content)
                return None, False
            if chunk_key:
                parsed["chunk_key"] = chunk_key
            _emit("ok", content)
            return parsed, True
    except Exception as exc:
        _emit("api_error", "", err=str(exc))
        return None, False


# ── Public entry ──────────────────────────────────────────────────────────────


async def run_head_scan(
    session: aiohttp.ClientSession,
    config: Config,
    book_title: str,
    book_author: str,
    chunks: list[Any],
    chapter_titles: dict[int, str],
    log_cb: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Run the full head scan over linearly-ordered chunks.

    Returns a verdict record dict:
      {verdict, preface_start, boundaries, identity, promo_chunks,
       confidence, calls, scanned, reason}
    """
    boundaries: dict[str, str] = {}
    identity: dict[str, dict] = {}
    fail_streak = 0
    total_failures = 0
    calls = 0
    n = len(chunks)
    reason = ""

    # ── First round: window [0, FIRST_WINDOW) ────────────────────────────
    first = chunks[:FIRST_WINDOW]
    msg = _build_first_msg(book_title, book_author, first, chapter_titles)
    res, ok = await _call_once(session, config, HEAD_SCANNER_SYSTEM_PROMPT,
                               msg, chunk_key=None, log_cb=log_cb)
    calls += 1
    if ok and isinstance(res, dict):
        for key, region in (res.get("boundaries") or {}).items():
            boundaries[key] = region
        _merge_identity(identity, res.get("identity") or [])
    else:
        log.warning("HeadScan: first round unusable (%r)", type(res).__name__)
        total_failures += 1
        fail_streak += 1

    # ── Sliding rounds: one new block per call ───────────────────────────
    i = FIRST_WINDOW
    while i < n and i < MAX_HEAD_BLOCKS and not _preface_line_reached(boundaries):
        if fail_streak >= MAX_CONSECUTIVE_FAILS:
            reason = f"连续 {fail_streak} 轮失败,提前终止"
            break
        new_block = chunks[i]
        prior = list(boundaries.items())[-PREFACE_CONSECUTIVE:]
        msg = _build_round_msg(book_title, book_author, new_block,
                               chapter_titles, prior)
        res, ok = await _call_once(
            session, config, HEAD_SCANNER_SYSTEM_PROMPT, msg,
            chunk_key=_chunk_key(new_block), log_cb=log_cb)
        calls += 1
        if ok and isinstance(res, dict):
            fail_streak = 0
            b = (res.get("boundary") or "ambiguous").strip()
            boundaries[_chunk_key(new_block)] = b
            _merge_identity(identity, res.get("identity") or [])
        else:
            log.warning("HeadScan: round for %s unusable (%r)",
                        _chunk_key(new_block), type(res).__name__)
            total_failures += 1
            fail_streak += 1
        i += 1

    # ── Copyright 块身份补全(预览截断兜底)────────────────────────────────
    # 预览"首尾各75字"可能截掉版权页中段字段(ISBN/出版年份)。对判为
    # copyright 且超过预览上限的块,注入全文再做一轮身份补全——这是 v0
    # 中 get_chunk 工具(按需拉全文)的等价实现,不引入 tool-calling 基建。
    chunk_by_key = {_chunk_key(c): c for c in chunks}
    for key in list(boundaries):
        if boundaries.get(key) != "copyright":
            continue
        ch = chunk_by_key.get(key)
        if ch is None or len(ch.content or "") <= PREVIEW_FULL_LEN:
            continue
        msg = _build_identity_refine_msg(ch, chapter_titles.get(ch.chapter_index))
        res2, ok2 = await _call_once(
            session, config, IDENTITY_REFINE_PROMPT, msg,
            chunk_key=None, log_cb=log_cb)
        calls += 1
        if ok2 and isinstance(res2, dict):
            _merge_identity(identity, res2.get("identity") or [])
        else:
            total_failures += 1

    # ── Verdict ──────────────────────────────────────────────────────────
    reached = _preface_line_reached(boundaries)
    has_id = bool(identity)
    confidence = max(0.0, 1.0 - total_failures / calls) if calls else 0.0

    if calls and total_failures >= max(2, calls // 2):
        verdict, reason = "fail", reason or "模型多数轮次失败"
    elif reached and has_id:
        verdict = "succeed"
        reason = reason or (f"连续{PREFACE_CONSECUTIVE}块 preface 终止,身份齐全,"
                            f"共扫描 {len(boundaries)} 块")
    elif reached and not has_id:
        verdict = "degraded"
        reason = reason or "边界达成但未提取身份字段(该书可能无版权页)"
    elif not reached:
        verdict = "degraded"
        reason = reason or (f"扫描 {len(boundaries)} 块仍未连续"
                            f"{PREFACE_CONSECUTIVE}块 preface(到上限或全块耗尽)")
    else:
        verdict = "degraded"
        reason = reason or "非正常终态"

    return {
        "verdict": verdict,
        "preface_start": _preface_start(boundaries) if reached else None,
        "boundaries": boundaries,
        # 稳定排序:字段名固定顺序输出。
        "identity": [{"field": f, **v} for f, v in sorted(identity.items())],
        "promo_chunks": [k for k, r in boundaries.items() if r == "promo"],
        "confidence": round(confidence, 2),
        "calls": calls,
        "scanned": len(boundaries),
        "reason": reason,
    }


def _parse_json(content: str) -> dict | None:
    """Tolerant JSON parse: direct → code fence → first { to last }."""
    if not content:
        return None
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None