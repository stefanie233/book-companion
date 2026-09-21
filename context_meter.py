"""Context meter — 上下文水位计量（前端圆环 + 未来 compact 的数据源）。

计门口径（讨论定案）:
- 总量 = 最后一轮 API usage 的 prompt_tokens + completion_tokens（权威值）;
- 各块 = 本地 tokenizer 原始计数 → 按占比对齐到真实 input_tokens，
  五块之和恒等于总量，避免"明细加起来 ≠ 总量"的尴尬;
- 无真实 usage 时（空会话基线 / 老会话回查）整体回退本地估算，estimated=True。
- 预警线默认 0.35（注意力衰减早于窗口上限），config.context_alert_ratio 可配。

调用约定: tokenizer encode 可能达百 ms 级，async 调用方须用
``asyncio.to_thread(measure, ...)`` 包裹，勿在事件循环内直接调用。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from content import TokenUsage

logger = logging.getLogger(__name__)

# 五个标准块 key — 前端按 key 映射图标与译名，勿改名
BLOCK_KEYS = ("system", "tools", "conversation", "context", "input")

ALERT_RATIO_DEFAULT = 0.35
# 窗口兜底值统一走 model_profiles.DEFAULT_CONTEXT_WINDOW（现为 1M），
# 避免两处各写一个魔数再次跑偏。


@dataclass
class ContextUsage:
    """One turn's context snapshot — totals real, breakdown proportional."""

    total_input: int = 0
    total_output: int = 0
    window: int = 0
    model: str = ""
    session_id: str = ""
    blocks: dict[str, int] = field(default_factory=dict)
    estimated: bool = True            # True: 总量为本地估算（无真实 usage）
    tokenizer_estimated: bool = True  # True: 占比来自 chars//4（tokenizer 不可用）

    @property
    def total(self) -> int:
        return self.total_input + self.total_output

    @property
    def percent(self) -> float:
        if self.window <= 0:
            return 0.0
        return self.total / self.window * 100

    def alert(self, ratio: float = ALERT_RATIO_DEFAULT) -> bool:
        return self.window > 0 and self.total >= self.window * ratio

    def to_dict(self, *, alert_ratio: float = ALERT_RATIO_DEFAULT) -> dict:
        return {
            "total_input": self.total_input,
            "total_output": self.total_output,
            "total": self.total,
            "window": self.window,
            "percent": round(self.percent, 2),
            "alert": self.alert(alert_ratio),
            "alert_ratio": alert_ratio,
            "estimated": self.estimated,
            "tokenizer_estimated": self.tokenizer_estimated,
            "model": self.model,
            "session_id": self.session_id,
            "blocks": [{"key": k, "tokens": v} for k, v in self.blocks.items()],
        }


def _count(text: str, tok_path: str) -> tuple[int, bool]:
    """(tokens, estimated) for one block; empty path → chars//4 fallback."""
    if not text:
        return 0, False
    if tok_path:
        from tokenizer_service import count_tokens
        return count_tokens(text, tok_path)
    return len(text) // 4, True


def _message_text(m: dict) -> str:
    """Flatten one API-format message to text for proportional counting."""
    parts: list[str] = []
    content = m.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text") or c.get("content") or ""))
    for tc in m.get("tool_calls") or []:
        func = tc.get("function", {}) if isinstance(tc, dict) else {}
        parts.append(str(func.get("name") or ""))
        parts.append(str(func.get("arguments") or ""))
    return "\n".join(p for p in parts if p)


def measure(
    *,
    system_prompt: str = "",
    tools_schema: list[dict] | None = None,
    history_messages: list[dict] | None = None,
    injected_context: str = "",
    user_input: str = "",
    usage: TokenUsage | None = None,
    window: int = 0,
    model: str = "",
    tokenizer_path: str = "",
    session_id: str = "",
) -> ContextUsage:
    """Measure one turn's context composition.

    ``usage`` (the FINAL ReAct round's TokenUsage) makes totals real and
    aligns blocks proportionally; without it everything is a local estimate.
    """
    raws: dict[str, int] = {}
    any_est = False
    texts = {
        "system": system_prompt or "",
        "tools": json.dumps(tools_schema or [], ensure_ascii=False),
        "conversation": "\n".join(
            _message_text(m) for m in (history_messages or [])
        ),
        "context": injected_context or "",
        "input": user_input or "",
    }
    for key, text in texts.items():
        tokens, est = _count(text, tokenizer_path)
        raws[key] = tokens
        any_est = any_est or est

    result = ContextUsage(
        window=int(window or 0), model=model, session_id=session_id,
        tokenizer_estimated=any_est,
    )

    real_in = usage.input_tokens if usage else 0
    if real_in > 0 and sum(raws.values()) > 0:
        # 真实总量 × 本地占比 —— 明细之和恒等于 input_tokens
        scale = real_in / sum(raws.values())
        allocated = 0
        keys_with_raw = [k for k in BLOCK_KEYS if raws[k] > 0]
        for key in keys_with_raw[:-1]:
            result.blocks[key] = round(raws[key] * scale)
            allocated += result.blocks[key]
        result.blocks[keys_with_raw[-1]] = real_in - allocated
        result.total_input = real_in
        result.total_output = usage.output_tokens if usage else 0
        result.estimated = False
    else:
        result.blocks = dict(raws)
        result.total_input = sum(raws.values())
        result.estimated = True
    return result


def resolve_profile(model: str, config: object = None) -> dict:
    """{window, tokenizer_path} from model_profiles (exact → '*' fallback).

    分词器按模型选路（显式覆盖 → 厂商别名 → 全局兜底），见
    ``ModelProfileStore.resolve_tokenizer``。
    """
    from model_profiles import DEFAULT_CONTEXT_WINDOW, get_profiles_store
    store = get_profiles_store(config)
    prof = store.resolve(model)
    return {
        "window": int(prof.get("context_window") or DEFAULT_CONTEXT_WINDOW),
        "tokenizer_path": store.resolve_tokenizer(model),
    }


def format_bill(u: ContextUsage, *, alert_ratio: float = ALERT_RATIO_DEFAULT) -> str:
    """One-line DEBUG bill, e.g.
    ``context bill: 52.3K/1000.0K (5.2%) in=51.2K out=1.0K | system 9.1K
    tools 24.4K conversation 12.1K context 3.2K input 2.4K [est]``"""
    def k(n: int) -> str:
        return f"{n / 1000:.1f}K" if n >= 10_000 else str(n)

    parts = " ".join(f"{key} {k(u.blocks.get(key, 0))}" for key in BLOCK_KEYS)
    flag = " [est]" if u.estimated or u.tokenizer_estimated else ""
    mark = " ⚠ALERT" if u.alert(alert_ratio) else ""
    return (
        f"context bill: {k(u.total)}/{k(u.window)} ({u.percent:.1f}%) "
        f"in={k(u.total_input)} out={k(u.total_output)} | {parts}{flag}{mark}"
    )
