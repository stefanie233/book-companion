"""Prompt budget — 开发期膨胀治理：上下文固定开销按来源计量。

    python scripts/prompt_budget.py

按源文件列出 system prompt 的构成（SOUL / prompts / skills / 冻结记忆）
+ 工具 schema 逐工具 tokens，回答"谁在膨胀"。tokenizer 与窗口口径同
model_profiles 配置，与运行时 context_meter 一致。纯只读，不发请求。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import load_config


def _fmt(n: int) -> str:
    return f"{n:,}"


def main() -> None:
    from context_meter import _count, resolve_profile

    config = load_config()
    # Console 绑定的主模型优先（.env model 只是回退值）
    model = config.model
    try:
        from provider_store import ProviderStore, SLOT_MAIN_LLM
        r = ProviderStore(config.providers_path).resolve(SLOT_MAIN_LLM, config)
        if r.model:
            model = r.model
    except Exception:
        pass
    prof = resolve_profile(model, config)
    tok = prof["tokenizer_path"]
    rows: list[tuple[str, int, int]] = []  # (source, chars, tokens)

    def add(label: str, text: str) -> None:
        if not text:
            return
        tokens, est = _count(text, tok)
        rows.append((label + (" [est]" if est else ""), len(text), tokens))

    def add_file(label: str, path: Path) -> None:
        path = Path(path)
        if path.exists():
            add(label, path.read_text(encoding="utf-8", errors="replace"))

    # ── System prompt sources ──────────────────────────────────────
    from runtime import _PROMPTS_DIR
    add_file("SOUL.md", config.soul_path)
    if _PROMPTS_DIR.exists():
        for md in sorted(_PROMPTS_DIR.glob("*.md")):
            add_file(f"prompts/{md.name}", md)
        for md in sorted(_PROMPTS_DIR.glob("modes/*.md")):
            add_file(f"prompts/modes/{md.name}", md)
    if config.skills_dir.exists():
        for skill_md in sorted(config.skills_dir.glob("*/SKILL.md")):
            add_file(f"skill/{skill_md.parent.name}", skill_md)
    for name in ("MEMORY.md", "USER.md", "persona.md"):
        add_file(f"memory/{name}", config.memory_dir / name)

    # ── Tool schemas (per-tool: the usual suspect when tools grow) ──
    from tools import create_builtin_registry
    try:
        registry = create_builtin_registry(config=config)
        schemas = registry.get_schemas()
    except Exception as exc:
        print(f"tool registry unavailable ({exc}); skipping tool section")
        schemas = []
    tool_rows: list[tuple[str, int, int]] = []
    for s in schemas:
        body = json.dumps(s, ensure_ascii=False)
        tokens, est = _count(body, tok)
        tool_rows.append((f"tool:{s.get('function', {}).get('name', '?')}"
                          + (" [est]" if est else ""),
                          len(body), tokens))
    rows.extend(sorted(tool_rows, key=lambda r: -r[2]))

    # ── Report ─────────────────────────────────────────────────────
    total_chars = sum(r[1] for r in rows)
    total_tokens = sum(r[2] for r in rows)
    width = max((len(r[0]) for r in rows), default=20)
    print(f"\nmodel: {model}   window: {_fmt(prof['window'])} tokens"
          f"   alert line: {prof['window'] * config.context_alert_ratio:,.0f}"
          f" ({config.context_alert_ratio:.0%})")
    print(f"\n{'source':<{width}}  {'chars':>9}  {'tokens':>8}  {'share':>6}")
    print("-" * (width + 32))
    for label, chars, tokens in rows:
        share = f"{tokens / total_tokens:.1%}" if total_tokens else "-"
        print(f"{label:<{width}}  {_fmt(chars):>9}  {_fmt(tokens):>8}  {share:>6}")
    print("-" * (width + 32))
    print(f"{'TOTAL (static overhead)':<{width}}  {_fmt(total_chars):>9}"
          f"  {_fmt(total_tokens):>8}"
          f"  {total_tokens / prof['window']:.1%} of window")
    n_tools = len(schemas)
    print(f"\nhint: {n_tools} tools exposed; 'tool:' rows are the tools param"
          " (separate from system prompt).")


if __name__ == "__main__":
    main()
