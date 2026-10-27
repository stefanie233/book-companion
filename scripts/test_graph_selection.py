"""``bookgraph.selection`` 的单元测试（纯函数，零 LLM、零 IO）。

盯的三件事：
1. **默认惰性** —— 没做过归属分类的书，一块都不能少。
2. **只裁非正文** —— 体例与头部扫描都认是正文的块，Scout 也不能一个人说了算。
3. **打架要报** —— 两份判断不一致时列进 conflicts，不偷偷选一边。

跑法：``python scripts/test_graph_selection.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph.selection import select_chunks          # noqa: E402


def _chunks(spec: list[tuple[int, int]]) -> list[dict]:
    """[(chapter_index, chunk_index)] → 块行（id 形如 c{ch}.{ck}）。"""
    return [{"id": f"c{ci}.{ck}", "chapter_index": ci, "chunk_index": ck}
            for ci, ck in spec]


# 一本小书：ch0=版权页、ch1=目录、ch2=正文三块、ch3=后记
BOOK = _chunks([(0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (3, 0)])
TYPES = {0: "copyright", 1: "toc", 2: "body", 3: "postscript"}

CASES: list[tuple[str, dict, set[str], list[str], bool]] = []


def case(name: str, *, types=None, boundaries=None, scout=(), use_scout=True):
    CASES.append((name, types if types is not None else TYPES,
                  set(scout), boundaries or {}, use_scout))


case("未分类的书一块都不少", types={})
case("只裁非正文章节", )
case("正文块全保留")
case("头部扫描标 promo 的裁掉",
     boundaries={"F#2-1": "promo"})
case("头部扫描标 ambiguous 的保留",
     boundaries={"F#2-1": "ambiguous"})
case("Scout 排除与体例打架：默认裁掉并报冲突", scout={"c2.1"})
case("关掉 scout 后不裁，但冲突照报", scout={"c2.1"}, use_scout=False)
case("Scout 排除非正文块不算冲突", scout={"c1.0"})

EXPECT = {
    "未分类的书一块都不少": (["c0.0", "c1.0", "c2.0", "c2.1", "c2.2", "c3.0"], 0),
    "只裁非正文章节": (["c2.0", "c2.1", "c2.2"], 0),
    "正文块全保留": (["c2.0", "c2.1", "c2.2"], 0),
    "头部扫描标 promo 的裁掉": (["c2.0", "c2.2"], 0),
    "头部扫描标 ambiguous 的保留": (["c2.0", "c2.1", "c2.2"], 0),
    "Scout 排除与体例打架：默认裁掉并报冲突": (["c2.0", "c2.2"], 1),
    "关掉 scout 后不裁，但冲突照报": (["c2.0", "c2.1", "c2.2"], 1),
    "Scout 排除非正文块不算冲突": (["c2.0", "c2.1", "c2.2"], 0),
}


def main() -> int:
    failed = 0
    for name, types, scout, boundaries, use_scout in CASES:
        sel = select_chunks(BOOK, types, boundaries, scout, use_scout=use_scout)
        want_keep, want_conf = EXPECT[name]
        got = (sel.keep, len(sel.conflicts))
        if got != (want_keep, want_conf):
            print(f"FAIL {name}\n     期望 保留{want_keep} 冲突{want_conf}\n"
                  f"     实得 保留{sel.keep} 冲突{len(sel.conflicts)}")
            failed += 1
        else:
            print(f"ok   {name}")

    # 裁掉的理由要能说清是谁裁的
    sel = select_chunks(BOOK, TYPES, {}, set())
    reasons = sel.drop
    if reasons.get("c0.0") != "chapter_type=copyright":
        print(f"FAIL 裁剪理由：{reasons}")
        failed += 1
    else:
        print("ok   裁剪理由带体例名")

    print(f"\n{'FAILED ' + str(failed) if failed else 'ALL PASS'}  ({len(CASES) + 1} 项)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
