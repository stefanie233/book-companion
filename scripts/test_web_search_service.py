"""Smoke tests for services/web_search_service — run:
    python scripts/test_web_search_service.py

不联网、不花 token。覆盖三件事：
① 原生搜索端点解析（只有 DeepSeek 官方 + Anthropic 兼容口才可用）；
② 兜底顺序（原生主路失败 → Tavily；全挂 → 报原因）；
③ 安全边界：模型自己在正文里拼的 URL 不能进结果，只能给服务端返回的条目补摘要。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from services import web_search_service as W


def check(label: str, got, want) -> None:
    assert got == want, f"FAIL {label}\n  got={got!r}\n want={want!r}"
    print(f"ok   {label}")


def test_native_endpoint() -> None:
    ds = Config(api_base="https://api.deepseek.com", api_key="k",
                model="deepseek-flash[1m]")
    check("deepseek 端点自动推导（含 [1m] 后缀剥离）",
          W.native_endpoint(ds),
          ("https://api.deepseek.com/anthropic", "k", "deepseek-flash"))

    ark = Config(api_base="https://ark.cn-beijing.volces.com/api/coding/v3",
                 api_key="k", model="deepseek-v4-flash")
    check("非 DeepSeek 端点不启用原生搜索", W.native_endpoint(ark), None)

    explicit = Config(api_base="https://ark.example.com/v1", api_key="k",
                      model="deepseek-flash",
                      native_search_base="https://api.deepseek.com/anthropic/")
    check("显式 native_search_base 优先",
          W.native_endpoint(explicit),
          ("https://api.deepseek.com/anthropic", "k", "deepseek-flash"))

    check("缺 key 不可用", W.native_endpoint(Config(api_base="https://api.deepseek.com")), None)


def test_hit_mapping() -> None:
    blocks = [
        {"type": "server_tool_use", "name": "web_search"},
        {"type": "web_search_tool_result", "content": [
            {"type": "web_search_result", "title": "真页面",
             "url": "https://real.example/a/", "encrypted_content": "xxx"},
        ]},
        {"type": "text", "text": '{"results": ['
                                 '{"url": "https://real.example/a", "summary": "服务端页面的摘要"},'
                                 '{"url": "https://made-up.example/x", "summary": "模型编的"}]}'},
    ]
    hits = W._hits_from_blocks(blocks, 5)
    check("只采信服务端返回的 URL", [h["url"] for h in hits],
          ["https://real.example/a/"])
    check("尾斜杠变体仍能补上摘要", hits[0]["content"], "服务端页面的摘要")

    many = [{"type": "web_search_tool_result", "content": [
        {"type": "web_search_result", "title": f"t{i}", "url": f"https://e{i}.example/"}
        for i in range(12)]}]
    check("结果按 max_results 截断", len(W._hits_from_blocks(many, 3)), 3)


def test_fallback_order() -> None:
    cfg = Config(api_base="https://api.deepseek.com", api_key="k",
                 model="deepseek-flash", search_api_key="tavily-key")
    calls: list[str] = []

    async def tavily_ok(*a, **k):
        calls.append("tavily")
        return {"hits": [{"title": "t", "url": "https://a/", "content": "c"}],
                "answer": "ans"}

    async def native_ok(*a, **k):
        calls.append("native")
        return [{"title": "n", "url": "https://b/", "content": ""}]

    orig_t, orig_n = W._tavily, W._native
    try:
        W._tavily, W._native = tavily_ok, native_ok
        res = asyncio.run(W.search(None, cfg, "q"))
        check("原生主路成功不开兜底", (res["provider"], calls), ("native", ["native"]))

        calls.clear()
        async def native_bad(*a, **k):
            calls.append("native")
            raise RuntimeError("boom")
        W._native = native_bad
        res = asyncio.run(W.search(None, cfg, "q"))
        check("原生失败切 Tavily", (res["provider"], calls), ("tavily", ["native", "tavily"]))
        check("降级原因被报出", any("原生搜索失败" in w for w in res["warnings"]), True)

        calls.clear()
        no_key = Config(api_base="https://api.deepseek.com", api_key="k",
                        model="deepseek-flash")
        W._tavily = orig_t
        res = asyncio.run(W.search(None, no_key, "q"))
        check("没配 Tavily 时只有原生一条路",
              (res["hits"], len([w for w in res["warnings"] if w])), ([], 2))
    finally:
        W._tavily, W._native = orig_t, orig_n


def main() -> int:
    test_native_endpoint()
    test_hit_mapping()
    test_fallback_order()
    print("\n全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
