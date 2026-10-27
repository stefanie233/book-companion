"""``bookgraph.scope`` 的单元测试（纯函数，零 LLM、零 IO）。

盯的四件事：
1. **默认惰性** —— 语义裁决缺席（老缓存/没跑过）时必须退成单域，长篇一字不变。
2. **两份判断都得点头** —— 语义说集类、但标题全带序号（分章）时以机械为准并报冲突。
3. **就近前置** —— EPUB 里被切出来的续块（level/in_toc = -1）归到它前面那一篇。
4. **完整划分** —— by_chunk 覆盖每一个块，不漏不重。

跑法：``python scripts/test_graph_scope.py``（退出码 0 = 全绿）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bookgraph.scope import (                            # noqa: E402
    plan_scopes, plan_to_meta, scope_brief, top_level_units,
)

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got != want:
        print(f"FAIL {label}\n     期望 {want!r}\n     实得 {got!r}")
        FAILED += 1
    else:
        print(f"ok   {label}")


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def chapters(spec: list[tuple[int, str, int, int, str]]) -> list[Row]:
    """[(chapter_index, title, level, in_toc, chapter_type)]"""
    return [Row(chapter_index=ci, title=t, level=lv, in_toc=toc, chapter_type=ct)
            for ci, t, lv, toc, ct in spec]


def chunks(spec: list[tuple[int, int]]) -> list[Row]:
    """[(chapter_index, chunk_index)] → id 形如 k{ci}.{ck}"""
    return [Row(id=f"k{ci}.{ck}", chapter_index=ci, chunk_index=ck)
            for ci, ck in spec]


# ── 一本散文集：目录 / 版权页（非正文）+ 3 篇 + 一段被切出来的续块 ──────────
ESSAYS = chapters([
    (0, "目录", 0, 1, "toc"),
    (1, "版权页", 0, 1, "copyright"),
    (2, "秋天的怀念", 0, 1, "body"),
    (3, "合　欢　树", 0, 1, "body"),
    (4, "我与地坛", 0, 1, "body"),
    (5, "part0003 split 001", -1, -1, "body"),   # 长 spine 文档被切出来的续块
    (6, "扶轮问路（代跋）", 0, 1, "postscript"),
])
ESSAY_CHUNKS = chunks([(1, 0),                                    # 目录页
                       (2, 0), (2, 1), (2, 2),                    # 秋天的怀念
                       (3, 0), (3, 1), (3, 2),                    # 合　欢　树
                       (4, 0), (4, 1), (4, 2),                    # 我与地坛
                       (5, 0),                                    # 切出来的续块
                       (6, 0)])                                   # 代跋

# ── 一部长篇小说：分章（标题带序号） ────────────────────────────────────
NOVEL = chapters([(i, f"第{n}章", 0, 1, "body")
                  for i, n in enumerate(["一", "二", "三", "四"], start=1)])
NOVEL_CHUNKS = chunks([(1, 0), (1, 1), (2, 0), (3, 0), (3, 1), (4, 0)])

# ── 学术书：引论 + 裸数字分节（反杜林论的真实形状） ──────────────────────
TREATISE = chapters([(0, "引论", 0, 1, "body"), (1, "一", 0, 1, "body"),
                     (2, "二", 0, 1, "body"), (3, "三", 0, 1, "body")])
TREATISE_CHUNKS = chunks([(0, 0), (1, 0), (1, 1), (2, 0), (3, 0), (3, 1), (3, 2)])

# ── 一个单元吞掉大半本（分节冒充篇） ────────────────────────────────────
DOMINANT = chapters([(0, "小记", 0, 1, "body"), (1, "正编", 0, 1, "body")])
DOMINANT_CHUNKS = chunks([(0, 0)] + [(1, i) for i in range(5)])


def main() -> int:
    # ① 惰性：没裁决 = 不划域
    p = plan_scopes(ESSAYS, ESSAY_CHUNKS)
    check("裁决缺席 → 单域", (p.is_split, len(p.scopes), p.source), (False, 1, "single"))
    check("裁决缺席 → 块一个不少",
          sorted(p.by_chunk), sorted(c.id for c in ESSAY_CHUNKS))
    check("裁决 single → 单域",
          plan_scopes(ESSAYS, ESSAY_CHUNKS, narrative_structure="single").is_split,
          False)

    # ② 两份都点头 → 划域
    p = plan_scopes(ESSAYS, ESSAY_CHUNKS, narrative_structure="collection")
    check("集类 → 三篇各自成域",
          [(s.label, len(s.chunk_ids)) for s in p.scopes],
          [("秋天的怀念", 4), ("合　欢　树", 3), ("我与地坛", 5)])
    check("目录页的块落到就近前置的篇",
          [p.scope_of("k1.0"), p.scope_of("k2.0")], ["s0", "s0"])
    check("切出来的续块跟它前面的篇",
          [p.scope_of("k5.0"), p.scope_of("k6.0")], ["s2", "s2"])
    check("by_chunk 是完整划分（不漏不重）",
          (sorted(p.by_chunk), len(set(p.by_chunk.values()))),
          (sorted(c.id for c in ESSAY_CHUNKS), 3))

    # ③ 机械守卫：语义说集类，但结构说不是
    p = plan_scopes(NOVEL, NOVEL_CHUNKS, narrative_structure="collection")
    check("标题全带序号 → 拒绝划域", (p.is_split, len(p.conflicts)), (False, 1))
    p = plan_scopes(TREATISE, TREATISE_CHUNKS, narrative_structure="collection")
    check("裸数字分节 → 拒绝划域", (p.is_split, len(p.conflicts)), (False, 1))
    p = plan_scopes(DOMINANT, DOMINANT_CHUNKS, narrative_structure="collection")
    check("单元吞掉大半本 → 拒绝划域", (p.is_split, len(p.conflicts)), (False, 1))
    check("拒绝划域时理由说得清", "吞掉" in plan_scopes(
        DOMINANT, DOMINANT_CHUNKS, narrative_structure="collection").conflicts[0],
        True)

    # ④ 结构本身不成立
    one = chapters([(0, "独篇", 0, 1, "body")])
    p = plan_scopes(one, chunks([(0, 0)]), narrative_structure="collection")
    check("顶层单元不足 2 → 单域并报冲突", (p.is_split, len(p.conflicts)), (False, 1))
    check("非正文章节不算单元",
          [u["title"] for u in top_level_units(ESSAYS)],
          ["秋天的怀念", "合　欢　树", "我与地坛"])

    # ⑤ 域简报
    p = plan_scopes(ESSAYS, ESSAY_CHUNKS, narrative_structure="collection")
    check("单域不发域简报", scope_brief(p.scopes[0], plan_scopes(ESSAYS, ESSAY_CHUNKS)), "")
    brief = scope_brief(p.scopes[1], p)
    check("集类简报含篇名与篇序",
          ("合　欢　树" in brief, "2/3" in brief, "只属于这一篇" in brief),
          (True, True, True))

    # ⑥ 落库用坐标（chunk_id 会随重导变，坐标不会）
    meta = plan_to_meta(p, ESSAY_CHUNKS)
    check("meta 用坐标而不是 chunk_id",
          meta["scopes"][0]["cells"], ["F#1-0", "F#2-0", "F#2-1", "F#2-2"])

    # ⑦ 裁决值的**说法**宽容 —— 实测换模型就静默失效
    p = plan_scopes(ESSAYS, ESSAY_CHUNKS,
                    narrative_structure="essay_collection")
    check("essay_collection 也当集类（DeepSeek 的实际说法）",
          (p.is_split, len(p.scopes)), (True, 3))
    check("认出来的变体也留下备注",
          any("essay_collection" in c for c in p.conflicts), True)
    check("empty 仍然退回单域（宁可不划）",
          plan_scopes(ESSAYS, ESSAY_CHUNKS,
                      narrative_structure="episodic").is_split, False)
    check("不认识的值要报出来，不能装作没判定",
          any("episodic" in c for c in plan_scopes(
              ESSAYS, ESSAY_CHUNKS, narrative_structure="episodic").conflicts),
          True)

    print(f"\n{'FAILED ' + str(FAILED) if FAILED else 'ALL PASS'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
