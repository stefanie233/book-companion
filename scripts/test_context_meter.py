"""Smoke tests for context_meter — run:
    python scripts/test_context_meter.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from content import TokenUsage
from context_meter import (
    BLOCK_KEYS,
    ContextUsage,
    format_bill,
    measure,
)


def main() -> None:
    # Real usage: blocks aligned proportionally, sum == input_tokens exactly
    u = measure(
        system_prompt="s" * 400,
        tools_schema=[{"name": "t", "desc": "d" * 100}],
        history_messages=[{"role": "user", "content": "h" * 200}],
        injected_context="c" * 100,
        user_input="q" * 100,
        usage=TokenUsage(input_tokens=1000, output_tokens=50),
        window=10_000,
        model="m",
    )
    assert not u.estimated and u.total_input == 1000 and u.total_output == 50
    assert sum(u.blocks.values()) == 1000, u.blocks
    assert set(u.blocks) <= set(BLOCK_KEYS)
    assert 10.0 <= u.percent <= 10.6  # 1050/10000
    assert not u.alert(0.35)
    print("  real-usage proportional alignment: ok")

    # Proportions track raw sizes: system(400ch) ≈ 4× input(100ch) etc.
    assert abs(u.blocks["system"] - 4 * u.blocks["input"]) <= 3 * len(u.blocks), u.blocks
    assert abs(u.blocks["conversation"] - 2 * u.blocks["input"]) <= 3 * len(u.blocks), u.blocks
    assert u.blocks["input"] > 0
    print("  block proportions track raw sizes: ok")

    # No usage → local estimate, blocks = raw counts
    u2 = measure(system_prompt="abcd" * 4, user_input="xy")  # 16+2 chars → 4+0
    assert u2.estimated and u2.total_input == 4, u2.total_input
    assert u2.blocks["system"] == 4 and u2.blocks["input"] == 0
    print("  estimated fallback (chars//4): ok")

    # Alert line
    u3 = ContextUsage(total_input=400, total_output=0, window=1000)
    assert u3.alert(0.35) and not u3.alert(0.5)
    assert abs(u3.percent - 40.0) < 1e-9
    print("  alert threshold + percent: ok")

    # Empty usage object (API returned nothing) → estimated path
    u4 = measure(system_prompt="hello", usage=TokenUsage(), window=100)
    assert u4.estimated and u4.total_input > 0
    print("  zero-usage treated as estimate: ok")

    # to_dict shape (frontend contract)
    d = u.to_dict(alert_ratio=0.35)
    for key in ("total", "total_input", "total_output", "window", "percent",
                "alert", "alert_ratio", "estimated", "tokenizer_estimated",
                "model", "session_id", "blocks"):
        assert key in d, key
    assert all(set(b) == {"key", "tokens"} for b in d["blocks"])
    print("  to_dict contract: ok")

    # format_bill renders without raising and marks estimates
    bill = format_bill(u2)
    assert "[est]" in bill and "context bill:" in bill
    print("  format_bill: ok")

    # _message_text flattens tool_calls / tool role
    from context_meter import _message_text
    m = {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]}
    assert "f" in _message_text(m) and "{}" in _message_text(m)
    assert _message_text({"role": "tool", "content": "result"}) == "result"
    print("  message text flattening: ok")

    print("all context_meter tests passed")


if __name__ == "__main__":
    main()
