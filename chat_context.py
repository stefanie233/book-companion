"""Web chat 的上下文管理 —— 结构化历史 + token 预算 + 压缩视图。

四条不变量：

- **记录**：assistant 的 tool_use 与 role=tool 的 tool_result 成对进入历史，
  不再在落盘时被压成纯文本；
- **配平**：发请求前补齐缺失的 call output（aborted），剔除孤儿 output，
  保证每一对 call/output 都满足 OpenAI 协议；
- **预算**：按 token 估算在 user 回合边界裁剪，绝不从一对 call/output 中间切；
- **补位**：被裁掉的老回合压成一段结构化摘要，原始会话不落盘、不丢失。

压缩摘要的生成方式：按项目定案
（`.claude/docs/platform.md` 02）保持纯结构化，不额外调用 LLM。
"""
from __future__ import annotations

from typing import Any

# ── Budget ────────────────────────────────────────────────────────────
# 历史默认最多 24K tokens；窗口更小时按窗口的 50% 收窄。
# 注意：估算按 UTF-8 字节 / 4，
# 不用字符 / 4 —— 中文一个字 3 字节，按字符算会把预算低估三倍。
DEFAULT_HISTORY_TOKEN_BUDGET = 24_000
MIN_HISTORY_TOKEN_BUDGET = 2_000
COMPLETION_TOKEN_RESERVE = 2_048
SUMMARY_TOKEN_RESERVE = 1_000
TOOL_OUTPUT_MAX_TOKENS = 6_000
# 「读正文」类工具的输出是模型接下来要改的对象，不能按命令输出截。
# 普通章节远小于 24K tokens（≈96K 字符），实际等于不截。
LONG_OUTPUT_MAX_TOKENS = 24_000
LONG_OUTPUT_TOOLS = frozenset({
    "writing_read_chapter", "library_read", "read_file", "read_book",
    "read_book_file", "read_session",
})
SUMMARY_MAX_CHARS = 1_200


def approx_tokens(text: str) -> int:
    """UTF-8 字节 / 4 —— 中文按字节计，不会低估。"""
    if not text:
        return 0
    return (len(text.encode("utf-8")) + 3) // 4


def _flat_text(content: Any) -> str:
    """API content 可能是 str 或 content-part 列表，压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text") or c.get("content") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def message_tokens(msg: dict) -> int:
    total = approx_tokens(_flat_text(msg.get("content")))
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        total += 8  # 协议字段本身的开销
        total += approx_tokens(str(fn.get("name") or ""))
        total += approx_tokens(str(fn.get("arguments") or ""))
    return total


def estimate_tokens(messages: list[dict]) -> int:
    return sum(message_tokens(m) for m in messages)


def compute_history_budget(window: int, reserve_tokens: int = 0,
                           completion_tokens: int = COMPLETION_TOKEN_RESERVE) -> int:
    """历史可用预算。

    ``reserve_tokens`` 是 system prompt + tools schema 的估算；这些每轮都在，
    不能算进历史。窗口未知（0）时退回默认值。
    """
    if window <= 0:
        # 窗口未知时仍要扣掉 system prompt / tools，别把默认值当总额。
        return max(MIN_HISTORY_TOKEN_BUDGET,
                   DEFAULT_HISTORY_TOKEN_BUDGET - max(0, reserve_tokens))
    available = window - max(0, reserve_tokens) - max(0, completion_tokens)
    if available <= MIN_HISTORY_TOKEN_BUDGET:
        return MIN_HISTORY_TOKEN_BUDGET
    return max(
        MIN_HISTORY_TOKEN_BUDGET,
        min(DEFAULT_HISTORY_TOKEN_BUDGET, int(available * 0.5)),
    )


# ── Tool output truncation ────────────────────────────────────────────


def tool_output_budget(tool_name: str) -> int:
    """按工具选截断预算：读正文类放宽，其余按命令输出处理。"""
    return (LONG_OUTPUT_MAX_TOKENS
            if str(tool_name or "") in LONG_OUTPUT_TOOLS
            else TOOL_OUTPUT_MAX_TOKENS)


def truncate_tool_output(text: str,
                         max_tokens: int = TOOL_OUTPUT_MAX_TOKENS) -> str:
    """头尾保留、中间省略。

    头尾都留：工具结果的尾部常有关键 id / 计数 / 状态，只截尾会把它们剪掉。
    预算按 **UTF-8 字节**算（tokens × 4），不按字符——中文一个字 3 字节，
    按字符截会让实际预算膨胀三倍，和上面的 token 估算口径对不上。
    """
    if not text:
        return text or ""
    max_bytes = max(1024, max_tokens * 4)
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    head_bytes = max_bytes * 3 // 4
    tail_bytes = max_bytes - head_bytes
    # 按字节切片可能切在多字节字符中间：errors="ignore" 丢掉半个字符。
    head = raw[:head_bytes].decode("utf-8", errors="ignore")
    tail = raw[-tail_bytes:].decode("utf-8", errors="ignore")
    omitted = max(0, len(text) - len(head) - len(tail))
    return (
        head
        + f"\n...（工具输出过长，中间省略约 {omitted} 字符；"
          "如需细节请重新调用工具）...\n"
        + tail
    )


# ── Session → API messages ────────────────────────────────────────────


def session_messages_to_api(messages: list) -> list[dict]:
    """把持久化的 ConversationMessage 还原成 OpenAI messages。

    只还原协议需要的东西：
    - user：文本 + 中断注记；
    - assistant：文本 + tool_use（thinking 不回灌，模型不需要自己的推理草稿）；
    - tool：tool_result（超长输出在这里截断）；
    - system：压缩续写摘要（CLI 路径写的会话可能会有）。
    """
    out: list[dict] = []
    for m in messages or []:
        role = getattr(m, "role", "")
        blocks = getattr(m, "blocks", []) or []
        texts = [b.text for b in blocks if b.type == "text" and b.text]
        notes = [b.text for b in blocks if b.type == "interrupt" and b.text]

        if role == "user":
            text = "\n\n".join(notes + texts).strip()
            if text:
                out.append({"role": "user", "content": text})
        elif role == "assistant":
            calls = [
                {
                    "id": b.tool_use_id or "",
                    "type": "function",
                    "function": {
                        "name": b.tool_name or "",
                        "arguments": b.tool_input or "{}",
                    },
                }
                for b in blocks if b.type == "tool_use"
            ]
            text = "".join(texts).strip()
            if calls:
                out.append({
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": calls,
                })
            elif text:
                out.append({"role": "assistant", "content": text})
        elif role == "tool":
            for b in blocks:
                if b.type == "tool_result":
                    out.append({
                        "role": "tool",
                        "tool_call_id": b.tool_use_id or "",
                        "content": truncate_tool_output(
                            b.tool_output or "",
                            tool_output_budget(b.tool_name or "")),
                    })
                elif b.type == "text" and b.text:
                    # 旧格式：tool 结果写在 text 块里
                    out.append({
                        "role": "tool",
                        "tool_call_id": b.tool_use_id or "",
                        "content": truncate_tool_output(
                            b.text, tool_output_budget(b.tool_name or "")),
                    })
        elif role == "system":
            text = "".join(texts).strip()
            if text:
                out.append({"role": "system", "content": text})
    return out


def client_history_to_api(history: list[dict]) -> list[dict]:
    """浏览器快照（纯文本）→ API messages；仅在没有 DB 会话时兜底。"""
    out: list[dict] = []
    for h in history or []:
        role = "assistant" if h.get("role") == "agent" else h.get("role")
        if role not in ("user", "assistant"):
            continue
        text = str(h.get("content") or "").strip()
        if text:
            out.append({"role": role, "content": text})
    return out


def to_legacy_history(messages: list[dict]) -> list[dict]:
    """结构化历史 → 旧图提取用的 {role: user/agent, content} 形状。"""
    out: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _flat_text(m.get("content")).strip()
        if text:
            out.append({
                "role": "agent" if role == "assistant" else "user",
                "content": text,
            })
    return out


# ── Invariants ────────────────────────────────────────────────────────


def normalize_call_pairs(messages: list[dict]) -> list[dict]:
    """保证每个 tool_call 都有 output，且 output 紧跟对应的 assistant。

    - 缺失的 output 补一条 ``aborted``；
    - 没有对应 call 的 output 丢弃；
    - 多个 output 按 tool_call 顺序重排，不依赖调用方落盘顺序。
    """
    out: list[dict] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role")

        # 没有 assistant 认领的 tool 消息 = 孤儿
        if role == "tool":
            i += 1
            continue

        out.append(msg)
        i += 1

        if role != "assistant" or not msg.get("tool_calls"):
            continue

        # 收集紧随其后的 tool 消息（允许连续多条）
        pending: dict[str, dict] = {}
        while i < n and messages[i].get("role") == "tool":
            tm = messages[i]
            cid = tm.get("tool_call_id") or ""
            if cid and cid not in pending:
                pending[cid] = tm
            i += 1

        for tc in msg.get("tool_calls") or []:
            cid = tc.get("id") or ""
            out.append(pending.pop(cid, {
                "role": "tool",
                "tool_call_id": cid,
                "content": "aborted",
            }))
        # pending 里剩下的是别的 call 的孤儿 output —— 直接丢弃
    return out


# ── Budgeted window + summary ─────────────────────────────────────────


def _turn_starts(messages: list[dict], start: int) -> list[int]:
    return [i for i in range(start, len(messages))
            if messages[i].get("role") == "user"]


def select_window(messages: list[dict],
                  budget_tokens: int) -> tuple[list[dict], list[dict]]:
    """在 user 回合边界上裁剪，返回 (保留, 丢弃)。

    - 头部 system（CLI 压缩续写）永远保留；
    - 从最新回合往前找最早能放下的边界，保证当前回合完整保留；
    - 即使最后一个回合超预算也整回合保留 —— 切碎当前回合比超预算更糟。
    """
    if not messages:
        return [], []

    # 先算前缀和，边界扫描是 O(n)；每轮对话都会走这里，别做成 O(n²)。
    n = len(messages)
    prefix = [0] * (n + 1)
    for i, msg in enumerate(messages):
        prefix[i + 1] = prefix[i] + message_tokens(msg)

    head: list[dict] = []
    start = 0
    if messages[0].get("role") == "system":
        head = [messages[0]]
        start = 1
    head_tokens = prefix[start]

    if head_tokens + prefix[n] - prefix[start] <= budget_tokens:
        return messages, []

    starts = _turn_starts(messages, start)
    if not starts:
        # 没有 user 边界：从尾部按条装，装不下就停
        kept: list[dict] = []
        used = head_tokens
        for i in range(n - 1, start - 1, -1):
            t = prefix[i + 1] - prefix[i]
            if kept and used + t > budget_tokens:
                break
            kept.append(messages[i])
            used += t
        kept.reverse()
        dropped = messages[start:n - len(kept)]
        return head + normalize_call_pairs(kept), dropped

    chosen = starts[-1]
    for idx in reversed(starts):
        if head_tokens + prefix[n] - prefix[idx] <= budget_tokens:
            chosen = idx
        else:
            break

    kept = head + messages[chosen:]
    dropped = messages[start:chosen]
    return normalize_call_pairs(kept), dropped


def _truncate_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def summarize_dropped(dropped: list[dict],
                      max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """把被裁掉的老回合压成一段可核对的结构化摘要。

    只写**确定的事实**：多少条消息、用户说过什么、调用过哪些工具。
    不做语义总结，也不猜结论 —— 摘要不能比原文更自信。
    """
    user_texts: list[str] = []
    assistant_texts: list[str] = []
    tool_names: list[str] = []
    tool_errors = 0

    for m in dropped:
        role = m.get("role")
        if role == "user":
            text = _flat_text(m.get("content")).strip()
            if text:
                user_texts.append(text)
        elif role == "assistant":
            text = _flat_text(m.get("content")).strip()
            if text:
                assistant_texts.append(text)
            for tc in m.get("tool_calls") or []:
                name = (tc.get("function") or {}).get("name")
                if name and name not in tool_names:
                    tool_names.append(name)
        elif role == "tool":
            if str(m.get("content") or "") == "aborted":
                tool_errors += 1

    lines = [
        "<conversation-summary>",
        "以下是更早对话的机械摘要，不是新的用户请求。原始消息已不在上下文里；"
        "需要细节时请重新调用工具或读取会话。",
        f"- 已压缩 {len(dropped)} 条消息"
        f"（用户 {len(user_texts)} 条，助手 {len(assistant_texts)} 条）。",
    ]
    if tool_names:
        shown = "、".join(tool_names[:12])
        extra = f" 等 {len(tool_names)} 个" if len(tool_names) > 12 else ""
        lines.append(f"- 调用过的工具：{shown}{extra}。")
    if tool_errors:
        lines.append(f"- 其中 {tool_errors} 次工具调用被中断/未返回。")
    if user_texts:
        lines.append("- 用户此前的请求（按时间顺序，节选）：")
        # 取「最近被裁掉的」10 条：更早的请求通常已经被后续请求覆盖。
        for t in user_texts[-10:]:
            lines.append("  - " + _truncate_line(t, 70))
    if assistant_texts:
        lines.append("- 助手此前的回复（节选）：")
        for t in assistant_texts[-3:]:
            lines.append("  - " + _truncate_line(t, 70))
    lines.append("</conversation-summary>")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 24] + "\n…（摘要已截断）\n</conversation-summary>"
    return text


def build_prompt_history(messages: list[dict],
                         budget_tokens: int) -> tuple[list[dict], list[dict]]:
    """配平 → 按预算裁剪 → 给被裁掉的部分补一段摘要。

    返回 ``(发给模型的 messages, 被裁掉的 messages)``。摘要插在头部 system
    之后；如果整段历史都在预算内，不生成摘要。
    """
    messages = normalize_call_pairs(messages or [])
    if not messages:
        return [], []

    # 给摘要本身留出预算，避免「裁完又超」。不到 MIN 也不抬高 ——
    # 抬到 MIN 会让调用方给的预算失效；最后一个回合无论如何都会保留。
    effective = max(0, budget_tokens - SUMMARY_TOKEN_RESERVE)
    kept, dropped = select_window(messages, effective)
    if not dropped:
        return kept, []

    summary = summarize_dropped(dropped)
    ins = 1 if kept and kept[0].get("role") == "system" else 0
    kept = list(kept[:ins]) + [{"role": "user", "content": summary}] + list(kept[ins:])
    return kept, dropped


# ── Session facade ────────────────────────────────────────────────────


def load_session_history(session_service, session_id: str) -> list[dict]:
    """从持久化会话重建结构化历史（不含本轮 user 消息）。"""
    if not session_service or not session_id:
        return []
    sess = session_service.load_messages(session_id, dispatch_hooks=False)
    if not sess:
        return []
    return session_messages_to_api(sess.messages)
