"""事件式会话压缩 —— 压缩是**事件**，不是每轮重算。

替换历史的形状：

* 压缩后的替换历史 = ``最近消息 + 摘要``；
* 摘要作为**持久化 checkpoint** 保存，两次压缩之间逐字不变——前缀缓存
  从下一次请求起就能原样复用（DeepSeek 保留历史多前缀，命中即续命）；
* 摘要由一次 LLM 调用生成（``chat.compact`` 路由）；失败时退回
  :func:`chat_context.summarize_dropped` 的机械摘要，一轮对话不会因为
  压缩失败而中断；
* 最近消息保持原文；只压更早的部分；checkpoint 记录它覆盖了多少条
  append-only 历史，后续轮次直接复用，不重新推导。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import chat_context

log = logging.getLogger("compaction")

SUMMARY_OPEN = "<conversation-summary>"
SUMMARY_CLOSE = "</conversation-summary>"
SUMMARY_PREAMBLE = (
    "以下是更早对话的摘要，不是新的用户请求。原始消息已不在上下文里；"
    "需要细节时请重新调用工具或读取会话。"
)

#: 交接摘要骨架：进展 / 决定 / 约束 / 待办 / 关键数据。
SUMMARY_PROMPT = """你正在做一次上下文检查点压缩。请为另一个 LLM 写一份交接摘要，
使它能够无缝继续这段对话。必须包含：

- 当前进展与已经做出的关键决定；
- 重要上下文、约束、用户偏好；
- 尚未完成的事（明确的下一步）；
- 继续工作所需的关键数据、示例、引用。

只写确定的事实；不要猜；不要编造；不要重复原文。用中文，结构化，尽量短。"""

#: 摘要预算按**总** token 算（推理 + 正文）。推理模型单次思维链可能
#: 吃掉一万多 token；4k 会让它回空 content（bookgraph 踩过同样的坑）。
SUMMARY_MAX_TOKENS = 16_000
SUMMARY_MAX_TOKENS_CAP = 64_000
#: 摘要进上下文后要占的预算。它比 chat_context 的 1k 预留更保守——
#: 一份 2k 的摘要把 24k 预算挤到 25k 不值得。
SUMMARY_RESERVE_TOKENS = 2_000
#: 摘要调用的输入上限（字符）。正常 chat 历史远小于它；这是防爆护栏。
SUMMARY_INPUT_MAX_CHARS = 240_000
#: 压缩时保留的最近消息上限（token）。
RECENT_TOKEN_BUDGET = 20_000
#: 摘要生成失败/截断时的重试次数（不含首次）。
SUMMARY_RETRIES = 2


@dataclass
class CompactionCheckpoint:
    """一次压缩的产物。``raw_count`` = 它覆盖的原始历史条数。"""

    seq: int
    raw_count: int
    summary: str
    dropped_count: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    trigger: str = "auto"


@dataclass
class CompactionOutcome:
    """一次压缩/复用的结果：发给模型的消息 + 被压掉的消息。"""

    messages: list[dict]
    dropped: list[dict]
    checkpoint: CompactionCheckpoint | None = None
    generated: bool = False


SummarizeFn = Callable[[list[dict], Any], Awaitable[str]]


# ── 消息形状 ──────────────────────────────────────────────────────────


def summary_message(summary: str) -> dict:
    """把摘要包成一条 user 消息。保留旧的 ``<conversation-summary>`` 前缀，
    现有测试与前端对它的识别不变。"""
    body = (summary or "").strip()
    return {
        "role": "user",
        "content": (f"{SUMMARY_OPEN}\n{SUMMARY_PREAMBLE}\n\n"
                    f"{body}\n{SUMMARY_CLOSE}"),
    }


def is_summary_message(msg: dict) -> bool:
    return str(msg.get("content") or "").lstrip().startswith(SUMMARY_OPEN)


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _format_transcript(messages: list[dict]) -> str:
    """把待压缩消息压成摘要调用的输入。

    工具结果只留头尾——摘要需要知道「调用过什么、成没成」，不需要整段
    输出；整段塞进去反而会把摘要预算挤爆。
    """
    lines: list[str] = []
    for m in messages or []:
        role = str(m.get("role") or "")
        if is_summary_message(m):
            lines.append("[此前摘要] " + _clip(
                str(m.get("content") or "").replace(SUMMARY_OPEN, "")
                .replace(SUMMARY_CLOSE, ""), 1_200))
            continue
        text = _clip(chat_context._flat_text(m.get("content")), 1_500)
        if role == "user":
            lines.append("[用户] " + text)
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                names = ", ".join(
                    str((c.get("function") or {}).get("name") or "?")
                    for c in calls[:8])
                lines.append("[助手·调用工具] " + names)
            if text:
                lines.append("[助手] " + text)
        elif role == "tool":
            lines.append("[工具结果] " + _clip(text, 400))
        elif role == "system":
            lines.append("[系统] " + text)
    out = "\n".join(lines)
    return out[-SUMMARY_INPUT_MAX_CHARS:]


def checkpoint_from_row(row: Any) -> CompactionCheckpoint | None:
    """DB row → checkpoint；形状不对返回 None（调用方当作没有 checkpoint）。"""
    if not row:
        return None
    try:
        get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k])
        return CompactionCheckpoint(
            seq=int(get("seq", 0) or 0),
            raw_count=int(get("raw_count", 0) or 0),
            summary=str(get("summary") or ""),
            dropped_count=int(get("dropped_count", 0) or 0),
            tokens_before=int(get("tokens_before", 0) or 0),
            tokens_after=int(get("tokens_after", 0) or 0),
            trigger=str(get("trigger") or "auto"),
        )
    except Exception:
        return None


# ── 摘要生成 ──────────────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1]
        if value.endswith("```"):
            value = value[:-3]
    return value.strip()


async def llm_summary(messages: list[dict], config: Any) -> str:
    """一次 LLM 交接摘要。失败返回空串，由调用方退回机械摘要。

    走 ``chat.compact`` 路由：控制台可以把压缩绑到便宜模型，不绑就继承
    主模型（决策 83 的调用点路由）。
    """
    if config is None:
        return ""
    try:
        from provider_store import config_for
        cfg = config_for(config, "chat.compact")
    except Exception:
        cfg = config
    api_key = getattr(cfg, "api_key", "")
    api_base = getattr(cfg, "api_base", "") or getattr(cfg, "api_base_url", "")
    model = getattr(cfg, "model", "")
    if not api_key or not api_base:
        return ""

    transcript = _format_transcript(messages)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": transcript},
        ],
        "temperature": 0.0,
        "max_tokens": SUMMARY_MAX_TOKENS,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    import aiohttp
    partial = ""
    budget = SUMMARY_MAX_TOKENS
    for attempt in range(SUMMARY_RETRIES + 1):
        body["max_tokens"] = budget
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{api_base}/chat/completions", json=body,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=180)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"API {resp.status}")
                    data = await resp.json()
            choice = (data.get("choices") or [{}])[0]
            finish = str(choice.get("finish_reason") or "")
            content = _strip_fences(
                (choice.get("message") or {}).get("content") or "")
            usage = data.get("usage") or {}
            try:
                import usage_log
                usage_log.record(
                    model=model,
                    tokens_in=int(usage.get("prompt_tokens") or 0),
                    tokens_out=int(usage.get("completion_tokens") or 0),
                    source="chat.compact",
                )
            except Exception:
                pass
            if content and finish != "length":
                return content
            if content:
                # 有正文但被截断：留着兜底，翻倍再试一次完整的。
                partial = content
            budget = min(budget * 2, SUMMARY_MAX_TOKENS_CAP)
            log.warning("compaction summary incomplete "
                        "(finish=%s, content=%d chars); retry with %d tokens",
                        finish or "?", len(content), budget)
        except Exception as exc:                             # noqa: BLE001
            log.warning("compaction summary attempt %d failed: %s",
                        attempt + 1, exc)
            if attempt < SUMMARY_RETRIES:
                await asyncio.sleep(2)
    return partial


# ── 压缩主流程 ────────────────────────────────────────────────────────


def _effective(raw: list[dict],
               ck: CompactionCheckpoint | None) -> list[dict]:
    if ck and 0 <= ck.raw_count <= len(raw):
        return [summary_message(ck.summary)] + list(raw[ck.raw_count:])
    return list(raw)


async def compact_history(
    raw: list[dict],
    budget_tokens: int,
    *,
    session_id: str = "",
    load_checkpoint: Callable[[str], Any] | None = None,
    save_checkpoint: Callable[[str, CompactionCheckpoint], None] | None = None,
    config: Any = None,
    summarize: SummarizeFn | None = None,
) -> CompactionOutcome:
    """按换窗模型压缩一次历史。

    ``raw`` 是 append-only 的完整历史；``budget_tokens`` 是发给模型的
    历史预算。有 checkpoint 且未超预算时**原样复用**——这是缓存命中率
    的关键：两次压缩之间，摘要那条消息逐字不变。
    """
    ck: CompactionCheckpoint | None = None
    if session_id and load_checkpoint is not None:
        try:
            ck = checkpoint_from_row(load_checkpoint(session_id))
        except Exception:
            log.warning("load compaction checkpoint failed", exc_info=True)
            ck = None
    if ck is not None and not (0 <= ck.raw_count <= len(raw)):
        # 会话被 fork / 截断后 raw_count 会越界。丢掉它，否则下面的
        # ``new_count <= prior_count`` 会误判成“没有新消息”而永不压缩。
        log.warning("discard stale compaction checkpoint "
                    "(raw_count=%d, history=%d)", ck.raw_count, len(raw))
        ck = None
    messages = _effective(raw, ck)
    before = chat_context.estimate_tokens(messages)
    if before <= budget_tokens:
        return CompactionOutcome(messages=messages, dropped=[], checkpoint=ck)

    # 摘要预留不能超过预算的四分之一：小预算（测试 / 短窗口）下把
    # 2000 当固定值会把 keep_budget 压到 0，等于把历史全扔掉。
    reserve = min(SUMMARY_RESERVE_TOKENS, max(0, budget_tokens // 4))
    keep_budget = max(0, budget_tokens - reserve)
    kept, dropped = chat_context.select_window(messages, keep_budget)
    if not dropped:
        # 最后一回合单独就超预算：切碎比超预算更糟，原样发。
        return CompactionOutcome(messages=messages, dropped=[], checkpoint=ck)

    has_prior = bool(kept and is_summary_message(kept[0]))
    raw_kept = len(kept) - (1 if has_prior else 0)
    new_count = len(raw) - raw_kept
    prior_count = ck.raw_count if ck else 0
    if new_count <= prior_count:
        return CompactionOutcome(messages=messages, dropped=[], checkpoint=ck)

    newly = [m for m in dropped if not is_summary_message(m)]
    summary_input = ([summary_message(ck.summary)] if ck and ck.summary
                     else []) + newly
    fn = summarize or llm_summary
    text = ""
    try:
        text = (await fn(summary_input, config) or "").strip()
    except Exception:
        log.warning("summarize callback failed", exc_info=True)
    if not text:
        # 失败也要能继续：机械摘要比中断好。
        text = chat_context.summarize_dropped(summary_input)

    new_ck = CompactionCheckpoint(
        seq=(ck.seq + 1) if ck else 1,
        raw_count=new_count,
        summary=text,
        dropped_count=len(dropped),
        tokens_before=before,
        tokens_after=chat_context.estimate_tokens(
            [summary_message(text)] + list(raw[new_count:])),
    )
    out = [summary_message(text)] + list(raw[new_count:])
    if new_ck.seq >= 3:
        # 反复压缩时告警：摘要的摘要会持续丢信息。
        log.warning("session %s compacted %d times; "
                    "summary-of-summary drift is likely", session_id or "-",
                    new_ck.seq)
    if session_id and save_checkpoint is not None:
        try:
            save_checkpoint(session_id, new_ck)
        except Exception:
            log.warning("save compaction checkpoint failed", exc_info=True)
    return CompactionOutcome(
        messages=out, dropped=dropped, checkpoint=new_ck, generated=True)
