"""导入分析编排的驱动壳：立论 → 证伪 → 机械校验 → 回炉。

与 run_book_classifier.py 的区别：那个只跑一趟 classifier，这个跑完整循环 ——
classifier 立论、verifier 拿着结论去证伪、纯代码查机械不变量，有问题就回炉。

用法:
  python scripts/run_import_analysis.py 窄门
  python scripts/run_import_analysis.py 窄门 --rounds 3
  python scripts/run_import_analysis.py 窄门 --commit        # 落库
  python scripts/run_import_analysis.py 窄门 --dump c:/tmp/x.txt

为什么必须是独立进程：见记忆 service-graph-extraction-deadlock —— 在 api_server
进程内跑 agent/orchestrator 会冻住事件循环（getaddrinfo-executor）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from run_book_classifier import commit_overrides, commit_text_forms, resolve_epub  # noqa: E402

from config import load_config  # noqa: E402
from flight_recorder import create_recorder  # noqa: E402
from runtime import OpenAIClient  # noqa: E402
from services.import_analysis_service import analyze_book  # noqa: E402
from storage.db import BookDB  # noqa: E402
from sub_agent import SubAgentRunner  # noqa: E402
from tools import create_builtin_registry  # noqa: E402


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else ""


def main() -> int:
    cfg = load_config()
    from provider_store import ProviderStore
    bound = ProviderStore(cfg.providers_path).apply_to_config(cfg)
    if bound:
        print(f"控制台连接: {bound}")
    if not cfg.api_key:
        print("!! 未检测到 api_key")
        return 2

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_commit = "--commit" in sys.argv
    rounds = int(_flag(sys.argv, "--rounds") or 2)
    arg = args[0] if args else "窄门"

    db = BookDB(cfg.books_db_path)
    db.initialize()
    book_id, title, epub = resolve_epub(db, arg)
    print(f"端点: {cfg.api_base}  模型: {cfg.model}")
    print(f"书  : {title}  id={book_id or '(未入库)'}")
    print(f"epub: {epub}   回炉上限: {rounds}\n")

    api_client = OpenAIClient(cfg)
    registry = create_builtin_registry(api_client=api_client, config=cfg, bookbank_db=db)
    runner = SubAgentRunner(api_client, registry, cfg,
                            context_recorder=create_recorder(cfg.project_dir),
                            scenario="import.analysis")

    res = asyncio.run(analyze_book(
        runner, epub, title=title, max_rounds=rounds,
        on_progress=lambda m: print(f"  · {m}", flush=True)))

    print("\n── 轮次日志 ──")
    for line in res.log:
        print("  " + line)

    print(f"\n── 末轮机械发现（{len(res.findings)} 条）──")
    for f in res.findings:
        print("  ⚠ " + f.render())

    if not res.verified:
        print("\n── 证伪趟**未完成** —— 本轮结论未经验证 ──")
        print("   （证伪趟耗尽预算或输出漂移，没吐出 JSON。别把它当成「没有反例」。）")
    else:
        v = res.verifier or {}
        print(f"\n── 证伪趟 verdict={v.get('verdict', '?')} ──")
        for c in v.get("challenges") or []:
            if isinstance(c, dict):
                print(f"  [{c.get('severity', '?')}] {c.get('claim', '')}"
                      f"   证据：{c.get('evidence', '')}")
        for m in v.get("missed") or []:
            if isinstance(m, dict):
                print(f"  [漏掉] {m.get('what', '')}   证据：{m.get('evidence', '')}")
        print(f"  已查: {v.get('checked')}")

    if res.analysis:
        print("\n── 最终结论 ──")
        print(json.dumps(res.analysis, ensure_ascii=False, indent=2)[:3000])

    dump = _flag(sys.argv, "--dump")
    if dump:
        Path(dump).write_text(
            f"===== classifier =====\n{res.raw_classifier}\n\n"
            f"===== verifier =====\n{res.raw_verifier}\n", encoding="utf-8")
        print(f"\n(原始输出已落盘: {dump})")

    if do_commit:
        print("\n── 落库 ──")
        if not res.verified:
            print("!! 结论未经过证伪 —— 拒绝落库。"
                  "加 --rounds 重跑，或确认证伪趟只是超时后再议。")
            return 1
        if res.analysis is None:
            print("!! 没有可落库的结论")
            return 1
        if not book_id:
            print("!! 这本书不在库里，无处可落")
            return 1
        if not any(c.spine_index >= 0 for c in db.get_chapters(book_id)):
            print("!! 缺 spine_index，先跑：python scripts/reimport_books.py <书名>")
            return 2
        old_ch, old_tf = db.clear_agent_layer(book_id)
        wrote, skipped = commit_overrides(db, book_id, res.analysis)
        print(f"覆盖层：清掉旧 {old_ch} 条，写入 {wrote} 条（source='agent'），"
              f"跳过 {skipped} 条越界 index")
        print(f"文本形态：清掉旧 {old_tf} 条，"
              f"写入 {commit_text_forms(db, book_id, res.analysis)} 条")
    return 0 if res.analysis else 1


if __name__ == "__main__":
    raise SystemExit(main())
