"""提取输入裁剪 —— 把**已经做完的归属分类**接进提取链路。

诊断（2026-10-10 实测）：同一条链上有**三份**「这块是什么」的判断，而只有
信息最少的那一份在起作用。

| 来源 | 粒度 | 现状 |
|---|---|---|
| `book_chapters.chapter_type`（classifier 判 + 人手在标注面板改过的覆盖层） | 章 | `bookgraph/` 里**出现 0 次** |
| `head_scan.boundaries`（`copyright/promo/preface/toc/body_lead/ambiguous`） | 块 | 只被 orchestrator 拿去发进度事件 |
| Scout 自己的 `excluded_chunk_ids` | 块 | **唯一真正生效的**，而它只看 500 字截断 |

于是「人手的修正」对提取毫无影响，而提取器还得为 Calibre 拆出来的目录片段
白花 token（窄门实测 78/148 块、大棋局 43/144）。

纯函数：不碰库、不调 LLM。调用方把三份判断查出来传进来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 这些体例不是正文，不进图谱。**`body` 之外一律不提** —— 序/跋/附录/索引
# 属于「关于这本书的话」，不是书本身（评价通道是另一条线，见
# docs/graph-agent-mapping.md §二）。
NON_BODY_TYPES = frozenset({
    "cover", "copyright", "toc", "preface",
    "postscript", "appendix", "bibliography", "index", "notes",
})

# head_scan 的区域标签里，这三个明确不是正文。
# `ambiguous` 刻意**保留** —— 拿不准就不动它，裁掉的代价比留下的高。
DROP_REGIONS = frozenset({"copyright", "promo", "toc"})


@dataclass
class Selection:
    """裁剪结果。``conflicts`` 是两份判断打架的地方 —— 报告出来，不偷偷选一边。"""

    keep: list[str] = field(default_factory=list)
    drop: dict[str, str] = field(default_factory=dict)      # chunk_id -> 理由
    conflicts: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.keep) + len(self.drop)

    def summary(self) -> str:
        c = self.counts
        return (f"{len(self.keep)}/{self.total} 块可提（按体例裁 {c.get('by_type', 0)}、"
                f"按头部扫描裁 {c.get('by_region', 0)}、"
                f"Scout 裁 {c.get('by_scout', 0)}；冲突 {len(self.conflicts)}）")


def select_chunks(chunks, chapter_types: dict[int, str] | None = None,
                  boundaries: dict[str, str] | None = None,
                  scout_excluded: set[str] | None = None,
                  *, use_scout: bool = True) -> Selection:
    """决定哪些块进提取。

    *chunks* 每项要有 ``id`` / ``chapter_index`` / ``chunk_index``（Row 或对象）。
    *boundaries* 的键是 head_scan 的 ``F#<chapter_index>-<chunk_index>``。
    *use_scout* 为 False 时 Scout 的排除**不生效**（降为报告），用来对比
    「只按既有分类裁」与「再叠 Scout」的差别。
    """
    types = chapter_types or {}
    regions = boundaries or {}
    scout = scout_excluded or set()
    sel = Selection()

    for ch in chunks:
        cid = _attr(ch, "id")
        ci, ck = _attr(ch, "chapter_index"), _attr(ch, "chunk_index")
        ctype = types.get(ci, "body")
        region = regions.get(f"F#{ci}-{ck}", "")

        if ctype in NON_BODY_TYPES:
            sel.drop[cid] = f"chapter_type={ctype}"
            sel.counts["by_type"] = sel.counts.get("by_type", 0) + 1
            # 体例说不是正文，Scout 也排除了它 → 一致，不算冲突
            continue
        if region in DROP_REGIONS:
            sel.drop[cid] = f"head_scan={region}"
            sel.counts["by_region"] = sel.counts.get("by_region", 0) + 1
            continue
        if cid in scout:
            # 体例与头部扫描都认它是正文，只有 Scout 要排 —— 两份判断打架。
            sel.conflicts.append({
                "chunk_id": cid, "chapter_index": ci, "chunk_index": ck,
                "chapter_type": ctype, "head_scan": region or "(未扫到)",
                "who": "scout 排除，体例/头部扫描认它是正文",
            })
            if use_scout:
                sel.drop[cid] = "scout"
                sel.counts["by_scout"] = sel.counts.get("by_scout", 0) + 1
                continue
        sel.keep.append(cid)

    return sel


def _attr(row, key: str):
    if isinstance(row, dict):
        return row.get(key)
    if hasattr(row, "keys"):
        return row[key] if key in row.keys() else None
    return getattr(row, key, None)
