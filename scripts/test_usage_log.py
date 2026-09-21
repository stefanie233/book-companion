"""Smoke tests for usage_log — run:
    python scripts/test_usage_log.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import usage_log


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "sessions.db"
        usage_log.init_for_tests(db_path)

        # ContextVar attribution: default chat, scoped override restores
        assert usage_log._SOURCE.get() == "chat"
        with usage_log.source_scope("memory"):
            usage_log.record(model="m", tokens_in=10, tokens_out=5)
            assert usage_log._SOURCE.get() == "memory"
            with usage_log.source_scope("sub_agent"):
                usage_log.record(model="m", tokens_in=1, tokens_out=1)
            assert usage_log._SOURCE.get() == "memory"
        usage_log.record(model="m", tokens_in=7, tokens_out=3)
        st = usage_log.get_store().usage_totals_since(0, 9_999_999_999_999)
        by_src = {r["source"]: r for r in st["by_source"]}
        assert by_src["memory"]["tokens_in"] == 10, by_src
        assert by_src["sub_agent"]["tokens_in"] == 1
        assert by_src["chat"]["tokens_in"] == 7
        assert by_src["chat"]["tokens_out"] == 3
        print("  contextvar attribution + scope restore: ok")

        # dedup_key idempotency
        for _ in range(2):
            usage_log.record(model="bg", source="bookgraph", tokens_in=9,
                             dedup_key="bg:fixed")
        st = usage_log.get_store().usage_totals_since(0, 9_999_999_999_999)
        assert {r["source"]: r["calls"] for r in st["by_source"]}["bookgraph"] == 1
        print("  dedup idempotency: ok")

        # daily × model bucketing (local day)
        store = usage_log.get_store()
        store.insert_usage_log(ts=1_700_000_000_000, source="chat",
                               tokens_in=100, tokens_out=1)
        rows = store.usage_daily_by_model(0, 9_999_999_999_999)
        assert any(r["calls"] >= 3 for r in rows), rows  # 同日同 model 分组后仍为 3
        print("  daily×model bucketing: ok")

        # fake-model 测试数据被统计侧过滤（行保留，不入图表/汇总）
        store.insert_usage_log(ts=1_700_000_000_000, source="chat",
                               model="fake-model", tokens_in=1, tokens_out=1)
        rows = store.usage_daily_by_model(0, 9_999_999_999_999)
        tot = store.usage_totals_since(0, 9_999_999_999_999)
        # 真实行: 今日 4 (3×m + 1×bg) + 老日 1 (tokens_in=100 chat)；fake 行被滤掉
        assert sum(r["calls"] for r in rows) == 5, rows
        by_src = {r["source"]: r["calls"] for r in tot["by_source"]}
        assert by_src["chat"] == 2, by_src  # 今日 chat 1 + 老日 chat 1，不含 fake
        print("  fake-model filtered from aggregates: ok")

        # record never raises even with a broken store
        store_ref = usage_log.get_store()
        usage_log._store = object()  # type: ignore[assignment]
        usage_log.record(model="x")  # must not raise
        usage_log._store = store_ref
        print("  failure swallowing: ok")

        # release the sqlite handle so Windows can clean the temp dir
        store_ref.close()

    print("all usage_log tests passed")


if __name__ == "__main__":
    main()
