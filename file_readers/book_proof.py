"""版式样张与切分实测 —— 让 agent 看得见书、量得出结构。

三个层次
--------
1. :func:`render_proof` —— **看**。每段一行，前缀是它的排版身份。agent 读它
   就像人看排版稿：窄门的信件一眼可辨，不需要谁告诉它 ``calibre19`` 是信。
2. :func:`book_overview` —— **俯瞰**。全书法医报告：spine 序列 + 每文件的
   视觉类指纹 + 书自己的目录树。
3. :func:`test_boundary_rule` —— **量**。agent 提出切分假设，全书跑一遍，
   回报命中分布 + 与书目录逐条对账 + 冲突。把「我猜 h3 是章界」变成
   「h3 命中 150 处，目录 150 条，0 冲突」。

设计约定
--------
* 坐标一律用 ``(spine_index, block_index)`` —— 与 :mod:`style_resolver` 的
  ``block_index`` 同口径。**不要**和 ``probe_tag_themes`` 的 ``paragraph_index``
  混用（设计稿 7.8 的教训就是两个 index 混用导致误伤）。
* 工具返回值带 ``conflicts``：机械不变量被违反的地方。工具不只递数据，
  它**提出质疑**。
* 纯只读 —— 不碰库、不写文件、不抛异常（读不动的文件跳过并记录）。
"""

from __future__ import annotations

import re
from collections import Counter

from file_readers.style_resolver import (
    Block, VisualClass, analyze_spine, body_base, cached_body_base,
    equiv_groups, survey_spines,
)

# ── 目录对账的基础 ────────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def _loaded(reader):
    """确保 spine 列表已加载。**读 ``reader._spine_ids`` 之前必须调它。**

    spine 列表是懒加载的，未加载时它是**空列表** —— 于是 ``len()`` 得 0，
    整本书被当成空书，函数安静地返回空报告而不是报错。工具层
    （``tools._reader``）已经加载过一次，这里是给脚本直调的兜底。

    为什么要防这一手：安静的空结果正是这一轮重构要消灭的失败方式。一个
    不报错的空答案，比一个报错更难查。
    """
    try:
        reader._ensure_loaded()
    except Exception:
        pass
    return reader


def _norm_label(s: str) -> str:
    """标题归一 —— 去**全部**空白，用于跨源比对。

    与 ``epub_import_service._build_depth_index`` / ``epub_probe._toc_depth_by_label``
    同口径。刻意各写一份而不是互相 import：本模块在 file_readers 层，反向
    依赖 services 会破坏分层（epub_probe 已为同样理由写过一次）。
    """
    return _WS_RE.sub("", s or "")


def _toc_spine_of(reader, entry: dict) -> int:
    """目录条目指向的 spine 序号；解析不出返回 -1。

    NCX 的 ``src`` 是 href（可能带 ``#fragment``），要经 ``_href_to_spine``
    翻译 —— 它已经处理过 fragment 与相对路径。
    """
    src = (entry.get("src") or "").split("#")[0]
    if not src:
        return -1
    return (getattr(reader, "_href_to_spine", None) or {}).get(src, -1)


def toc_entries(reader) -> list[dict]:
    """书自己的目录（**未过滤**版），每项 ``{label, depth, spine}``。

    用 ``_toc_all`` 而非 ``_toc``：后者被 ``_filter_front_matter_toc`` 摘掉了
    扉页/版权/目录这类条目，而 agent 恰恰需要知道书**自己**声称有哪些结构。
    """
    out: list[dict] = []
    for entry in getattr(reader, "_toc_all", []) or []:
        label = entry.get("label") or ""
        if not label.strip():
            continue
        out.append({
            "label": label,
            "depth": entry.get("depth"),
            "spine": _toc_spine_of(reader, entry),
        })
    return out


# ── 1. 版式样张：让 agent 看得见 ───────────────────────────────────────────

# 单次返回的字符硬顶。取值依据（2026-10-08 实测全库 21 本）：
#
# 500k 字符 ≈ 325k token（中文约 0.65 token/字）。依据是端点**已确认**的
# 窗口：doubao-seed-2.1-pro（0915）为 1024k token。此前取 250k 是保守值 ——
# model_profiles 对未知模型回退到 1M 兜底，端点真实窗口可能只有 256k，而
# 撑爆窗口的失败方式很难看（API 报错，agent 无从补救），分页的代价却只是一
# 轮 next_spine。窗口确认后就没有理由再省。
#
# 实测：窄门整本 66k 字符；人类简史 358k 字符（301k 正文 + 每段前缀）；
# 遥远的救世主 468k 字符 —— 三者现在都能一次读完。
PROOF_CHAR_LIMIT = 500_000


def proof_legend(classes: list[VisualClass]) -> str:
    """样张开头的图例：每个视觉类是什么排版、有多少量。

    字号一律相对**本书正文**（占字最多的那一类）描述，不相对 ``<body>`` ——
    见 :meth:`EffectiveStyle.describe`。
    """
    base = body_base(classes)
    lines = ["── 版式图例（前缀里的 selector = 该段的排版身份）──"]
    for vc in classes:
        lines.append(f"  {vc.selector:<16} {vc.style.describe(base):<26} "
                     f"{vc.paragraphs:>5}段 {vc.chars:>7}字")
    return "\n".join(lines)


def render_proof(reader, spine_from: int = 0, spine_to: int | None = None,
                 *, block_from: int = 0,
                 classes: list[VisualClass] | None = None,
                 include_legend: bool = True,
                 max_chars: int = PROOF_CHAR_LIMIT) -> dict:
    """把书渲染成**版式样张**：每段一行，前缀 ``[spine|block] selector``。

    前缀里的 ``[spine|block]`` 就是**证据坐标** —— agent 引用它，:func:`zoom`
    取回它。没有坐标，agent 说「第三章是散文」就无从复核。

    截断时回报 ``next_spine`` + ``next_block``，续读时两个一起传回来。**两个
    都要**：只记 spine 的话，单个文件比一页还大时会原地打转 —— 续读会从头重发
    同一批块（实测 20000 字符上限下跑了 200 页只覆盖 43/49 个 spine）。

    无论如何至少吐一块，保证每次调用都有进展。
    """
    _loaded(reader)
    n = len(reader._spine_ids)
    lo = max(0, spine_from)
    hi = n if spine_to is None else max(lo, min(n, spine_to))

    if classes is None:
        _, classes, _ = survey_spines(reader, range(n))

    head = proof_legend(classes) if include_legend else ""
    out: list[str] = [head] if head else []
    used = len(head)
    blocks = 0
    next_spine: int | None = None
    next_block: int | None = None

    for si in range(lo, hi):
        try:
            bs = analyze_spine(reader, si)
        except Exception as exc:
            out.append(f"\n── spine[{si}] 读取失败: {exc} ──")
            continue
        # 续读时只从 block_from 起 —— 之前发过的块不再重发
        start_at = block_from if si == lo else 0
        todo = [b for b in bs if b.block_index >= start_at]
        if not todo:
            continue
        try:
            label = reader.get_chapter_label(si)
        except Exception:
            label = ""
        header = f"\n── spine[{si}]  {label}  ({len(bs)} 块) ──"
        out.append(header)
        used += len(header) + 1
        for b in todo:
            # 容器链直接写在前缀里 —— 「这一段在 blockquote 里」是结构证据，
            # 不该逼 agent 回原文才发现
            ctx = f" «{' > '.join(b.containers)}»" if b.containers else ""
            line = (f"[{b.spine_index}|{b.block_index}] {b.selector}{ctx}"
                    f" │ {b.text}")
            # blocks > 0：保证每次调用至少吐一块，否则续读会原地打转
            if used + len(line) > max_chars and blocks > 0:
                next_spine, next_block = si, b.block_index
                break
            out.append(line)
            used += len(line) + 1
            blocks += 1
        if next_spine is not None:
            break

    return {
        "text": "\n".join(out),
        "spines": [lo, next_spine if next_spine is not None else hi],
        "blocks": blocks,
        "chars": used,
        "truncated": next_spine is not None,
        "next_spine": next_spine,
        "next_block": next_block,
    }


# ── 2. 全书法医报告 ───────────────────────────────────────────────────────


def book_overview(reader, *, samples_per_class: int = 3,
                  spine_preview: int = 60) -> dict:
    """全书法医报告 —— 一眼看完这本书的骨架。

    含四块：**目录树**（书自己声称的结构）、**spine 序列**（每文件的视觉类
    指纹 + 首末段原文）、**视觉类清单**（含等价组）、**结构信号**（guide /
    landmarks）。

    空 spine（Calibre 留下的 461 字节空文件）**不进列表**，只记在
    ``empty_spines`` 里。设计稿 7.7 的教训：没有信息就不该让它做判断 ——
    递过去它只会就近猜成 cover，而提示词层面的规则压不住这种猜测。
    """
    from file_readers.epub_probe import probe_spine_list

    _loaded(reader)
    n = len(reader._spine_ids)
    blocks, classes, groups = survey_spines(reader, range(n), samples_per_class)
    body_ratio = body_base(classes)
    base = {d["index"]: d for d in probe_spine_list(reader, head_chars=spine_preview)}

    per_spine: dict[int, Counter] = {}
    first_of: dict[int, Block] = {}
    last_of: dict[int, Block] = {}
    chars_of: Counter = Counter()
    for b in blocks:
        per_spine.setdefault(b.spine_index, Counter())[b.selector] += 1
        chars_of[b.spine_index] += len(b.text)
        first_of.setdefault(b.spine_index, b)
        last_of[b.spine_index] = b

    spines: list[dict] = []
    empty: list[int] = []
    for si in range(n):
        cnt = per_spine.get(si)
        if not cnt:
            empty.append(si)
            continue
        d = base.get(si, {})
        spines.append({
            "index": si,
            "href": d.get("href", ""),
            "label": d.get("label") or reader.get_chapter_label(si),
            "chars": chars_of[si],
            "blocks": sum(cnt.values()),
            "links": d.get("links", 0),
            "top_classes": [f"{sel}×{c}" for sel, c in cnt.most_common(4)],
            "first": _brief(first_of[si]),
            "last": _brief(last_of[si]),
        })

    return {
        "spine_total": n,
        "spine_with_text": len(spines),
        "empty_spines": empty,
        "toc_total": len(toc_entries(reader)),
        "toc": _toc_tree(reader),
        "visual_classes": [_class_dict(vc, body_ratio) for vc in classes],
        "equiv_groups": groups,
        "structural_signals": {
            "guide": reader.guide_type_map(),
            "landmarks": reader.landmark_type_map(),
        },
        "spines": spines,
    }


def _brief(b: Block) -> dict:
    """块的一句话摘要（报告里只放前 80 字，全文让 agent 去 read_proof 取）。"""
    return {
        "block": b.block_index,
        "selector": b.selector,
        "text": b.text[:80],
    }


def _class_dict(vc: VisualClass, base: float = 1.0) -> dict:
    return {
        "selector": vc.selector,
        "style": vc.style.describe(base),
        "paragraphs": vc.paragraphs,
        "chars": vc.chars,
        "avg_len": vc.avg_len,
        "spine_span": vc.spine_span,
        "equiv_group": vc.equiv_group,
        "samples": vc.samples,
    }


def _toc_tree(reader, limit: int = 400) -> list[dict]:
    """书自己的目录，保留嵌套深度。超出 *limit* 截断（``toc_total`` 会说明）。"""
    return [{"label": e["label"], "depth": e["depth"], "spine": e["spine"]}
            for e in toc_entries(reader)[:limit]]


# ── 3. 切分实测：把「猜」变成「量」 ────────────────────────────────────────


def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _match_block(b: Block, rule: dict) -> bool:
    """一条规则是否命中某块。

    规则 DSL 刻意小而机械 —— agent 不该学一门语言，它该提出假设、由工具去量。
    支持 ``all_of`` / ``any_of`` / ``not`` 组合，以及下列叶子判据：

    ``tag`` / ``selector`` / ``class`` / ``container`` / ``text_regex`` /
    ``align`` / ``font_family`` / ``page_break`` / ``min_chars`` /
    ``max_chars`` / ``size_ratio_lt`` / ``size_ratio_gt``
    """
    if not isinstance(rule, dict):
        return False
    kind = str(rule.get("kind") or "").strip()
    val = rule.get("value")

    if kind == "all_of":
        subs = rule.get("rules") or []
        return bool(subs) and all(_match_block(b, r) for r in subs)
    if kind == "any_of":
        return any(_match_block(b, r) for r in (rule.get("rules") or []))
    if kind == "not":
        return not _match_block(b, rule.get("rule") or {})
    if kind == "tag":
        return b.tag == str(val or "").lower()
    if kind == "selector":
        return b.selector == str(val or "")
    if kind == "class":
        return str(val or "") in b.classes
    if kind == "container":
        return str(val or "") in b.containers
    if kind == "align":
        return b.style.align == str(val or "")
    if kind == "font_family":
        # 值是**归一化后**的族名（kaiti/songti/…），见 normalize_font_family
        return b.style.font_family == str(val or "").strip().lower()
    if kind == "page_break":
        return b.style.page_break_before
    if kind == "text_regex":
        try:
            return re.search(str(val or ""), b.text) is not None
        except re.error:
            return False
    if kind == "min_chars":
        return len(b.text) >= _as_int(val)
    if kind == "max_chars":
        return len(b.text) <= _as_int(val)
    if kind == "size_ratio_lt":
        return b.style.size_ratio < _as_float(val, 1.0)
    if kind == "size_ratio_gt":
        return b.style.size_ratio > _as_float(val, 1.0)
    return False


def _label_match(a: str, b: str) -> bool:
    """标题对账：归一后相等，或一方包含另一方。

    与 ``library_service._title_match_score`` 的「exact 或 one-contains-other」
    同精神，但**更严** —— 只认完整包含，不做字符集相似度，避免把「第一章」
    和「第二十一章」判成同一个。
    """
    x, y = _norm_label(a), _norm_label(b)
    if not x or not y:
        return False
    return x == y or x in y or y in x


def test_boundary_rule(reader, rule: dict, *, sample_hits: int = 40,
                       gap_buckets: int = 8) -> dict:
    """把切分假设**全书跑一遍**，回报命中、分布、与目录对账、冲突。

    本工具集里最重要的一件 —— 它把「我猜 h3 是章界」变成「h3 命中 150 处，
    目录 150 条，148 条对得上，对不上的 2 条列在这里」。**猜测变成了测量**。
    """
    _loaded(reader)
    n = len(reader._spine_ids)
    # 只要块序列，不要视觉类统计 —— survey_spines 一次读盘两样都出，这里只取其一
    blocks = survey_spines(reader, range(n))[0]

    hit_flags = [_match_block(b, rule) for b in blocks]
    hits = [b for b, ok in zip(blocks, hit_flags) if ok]
    hit_positions = [i for i, ok in enumerate(hit_flags) if ok]

    per_spine: Counter = Counter(b.spine_index for b in hits)

    # 间距直方图（按全书块序）—— 等距是强结构信号，忽疏忽密说明规则漏了
    gaps = [b - a for a, b in zip(hit_positions, hit_positions[1:])]
    gap_hist: dict[str, int] = {}
    if gaps:
        lo, hi = min(gaps), max(gaps)
        if lo == hi:
            gap_hist[str(lo)] = len(gaps)
        else:
            step = max(1, (hi - lo) // gap_buckets)
            for g in gaps:
                bucket = lo + ((g - lo) // step) * step
                gap_hist[f"{bucket}-{bucket + step - 1}"] = \
                    gap_hist.get(f"{bucket}-{bucket + step - 1}", 0) + 1

    # —— 与书自己的目录逐条对账 ——
    toc = toc_entries(reader)
    toc_unmatched: list[dict] = []
    for e in toc:
        if not any(_label_match(b.text, e["label"]) for b in hits):
            toc_unmatched.append({"label": e["label"], "spine": e["spine"]})
    hits_not_in_toc = [
        {"spine": b.spine_index, "block": b.block_index, "text": b.text[:60]}
        for b in hits
        if not any(_label_match(b.text, e["label"]) for e in toc)
    ]

    return {
        "rule": rule,
        "hit_count": len(hits),
        "block_total": len(blocks),
        "hits_per_spine": {str(k): v for k, v in sorted(per_spine.items())},
        "gap_histogram": gap_hist,
        "gap_median": sorted(gaps)[len(gaps) // 2] if gaps else 0,
        "gap_max": max(gaps) if gaps else 0,
        "sample_hits": [
            {"spine": b.spine_index, "block": b.block_index,
             "selector": b.selector, "text": b.text[:60]}
            for b in hits[:sample_hits]
        ],
        "hits_truncated": len(hits) > sample_hits,
        "toc": {
            "total": len(toc),
            "matched": len(toc) - len(toc_unmatched),
            "unmatched_count": len(toc_unmatched),
            "unmatched": toc_unmatched[:40],
            "hits_not_in_toc_count": len(hits_not_in_toc),
            "hits_not_in_toc": hits_not_in_toc[:40],
        },
        "conflicts": _boundary_conflicts(
            hits, per_spine, toc, gaps, hit_positions, blocks),
    }


def _boundary_conflicts(hits, per_spine, toc, gaps, hit_positions,
                        blocks) -> list[str]:
    """机械不变量被违反的地方 —— 工具不只递数据，它**提出质疑**。

    每一条都是「这个假设可能不成立」的具体理由，而不是一个分数。agent 拿
    这些质疑去决定是改规则、还是补证据说明为什么规则对。
    """
    if not hits:
        return ["规则零命中 —— 这个判据在这本书里根本不成立。"]

    out: list[str] = []
    toc_spines = {e["spine"] for e in toc if e["spine"] >= 0}
    if toc_spines:
        stray = sorted(s for s in per_spine if s not in toc_spines)
        if stray:
            out.append(
                f"spine {stray[:8]} 有命中，但书目录里没有指向它们的条目"
                f"（共 {len(stray)} 个）—— 要么目录不全，要么这些命中不是章界。")
        missing = sorted(s for s in toc_spines if s not in per_spine)
        if missing:
            out.append(
                f"spine {missing[:8]} 在目录里有条目，本规则在它们内部一处都没命中"
                f"（共 {len(missing)} 个）—— 规则漏了这些章。")

    if gaps:
        med = sorted(gaps)[len(gaps) // 2]
        if med > 0 and max(gaps) > med * 20:
            out.append(f"命中间距中位数 {med} 块、最大 {max(gaps)} 块，悬殊过大 —— "
                       f"中间可能有若干章没被命中。")

    if hit_positions and len(blocks) > 3:
        if hit_positions[-1] < len(blocks) * 0.2:
            out.append(f"全部命中都挤在全書前 20% 内（最后一条在第 "
                       f"{hit_positions[-1]}/{len(blocks)} 块）—— 这不是贯穿全书的章界规则。")

    long_hits = sum(1 for b in hits if len(b.text) > 60)
    if long_hits:
        out.append(f"{long_hits} 处命中的文本超过 60 字 —— 章标题通常很短，"
                   f"这些可能是正文段落而非标题。")
    return out


# ── 4. 区段比较与回看原文 ─────────────────────────────────────────────────


def _region_span(region: dict) -> tuple[int, int]:
    lo = max(0, _as_int(region.get("from"), 0))
    hi = max(lo + 1, _as_int(region.get("to"), lo + 1))
    return lo, hi


def _region_blocks(reader, region: dict) -> list[Block]:
    lo, hi = _region_span(region)
    out: list[Block] = []
    for si in range(lo, hi):
        try:
            out.extend(analyze_spine(reader, si))
        except Exception:
            continue
    return out


def _region_profile(reader, region: dict, blocks: list[Block]) -> dict:
    lo, hi = _region_span(region)
    cnt: Counter = Counter(b.selector for b in blocks)
    chars = sum(len(b.text) for b in blocks)
    toc_spines = {e["spine"] for e in toc_entries(reader)}
    return {
        "region": [lo, hi],
        "blocks": len(blocks),
        "chars": chars,
        "avg_block_len": chars // len(blocks) if blocks else 0,
        "top_selectors": [f"{s}×{c}" for s, c in cnt.most_common(5)],
        "labels": [reader.get_chapter_label(si) for si in range(lo, hi)][:12],
        "spines_in_toc": [si for si in range(lo, hi) if si in toc_spines],
        "samples": [
            {"spine": b.spine_index, "block": b.block_index, "text": b.text[:100]}
            for b in blocks[:3]
        ],
    }


def compare_regions(reader, region_a: dict, region_b: dict) -> dict:
    """两个 spine 区段并排比较 —— agent 判「这是正文还是体例页」的核心动作。

    区段形如 ``{"from": 9, "to": 13}``（左闭右开）。回报两侧的排版构成、段均长、
    是否在书目录里有条目，外加机械算出的**差异点** —— 让 agent 少做一次心算。
    """
    _loaded(reader)
    a_blocks = _region_blocks(reader, region_a)
    b_blocks = _region_blocks(reader, region_b)
    a = _region_profile(reader, region_a, a_blocks)
    b = _region_profile(reader, region_b, b_blocks)

    diffs: list[str] = []
    if a["avg_block_len"] and b["avg_block_len"]:
        ratio = a["avg_block_len"] / b["avg_block_len"]
        if ratio >= 1.5 or ratio <= 1 / 1.5:
            longer = "A" if ratio > 1 else "B"
            diffs.append(
                f"段均长相差 {max(ratio, 1 / ratio):.1f} 倍（{longer} 更长："
                f"A={a['avg_block_len']} B={b['avg_block_len']}）—— 长段一侧更像正文。")
    sa, sb = set(a["top_selectors"]), set(b["top_selectors"])
    if not (sa & sb):
        diffs.append("两侧的主要排版类完全不重叠 —— 它们很可能是**不同性质**的内容。")
    if bool(a["spines_in_toc"]) != bool(b["spines_in_toc"]):
        which = "A" if a["spines_in_toc"] else "B"
        diffs.append(f"只有 {which} 在书目录里有条目 —— 另一侧很可能是目录未收录的体例页。")

    return {"a": a, "b": b, "differences": diffs}


def zoom(reader, spine_index: int, block_index: int,
         context: int = 2, raw_window: int = 400) -> dict:
    """取回某一块的**全文 + 原文片段 + 前后邻块** —— 核对行内结构用。

    样张给的是折过空白文本；题词两行同属一段、或某行被 ``<span>`` 包着，
    只有回原文才看得见。``raw`` 是从原始 HTML 里截取的窗口。
    """
    _loaded(reader)
    # 字号描述要相对**本书正文**，而单块视图手里没有视觉类清单。基准走模块级
    # 缓存（见 style_resolver._BODY_BASE_CACHE）；缓存冷时现场扫一遍全书 ——
    # 一次调用换一个正确的基准，比退回 <body> 说错话划算。缓存热了就是免费。
    if cached_body_base(reader) is None:
        try:
            survey_spines(reader, range(len(reader._spine_ids)))
        except Exception:
            pass
    try:
        blocks = analyze_spine(reader, spine_index)
    except Exception as exc:
        return {"error": f"spine[{spine_index}] 读取失败: {exc}"}

    target = next((b for b in blocks if b.block_index == block_index), None)
    if target is None:
        return {"error": f"spine[{spine_index}] 没有 block {block_index}"
                         f"（该文件共 {len(blocks)} 块）"}

    raw = ""
    try:
        html = reader._read_spine_html(spine_index)
        probe = target.text[:20]
        pos = html.find(probe) if probe else -1
        if pos >= 0:
            lo = max(0, pos - raw_window // 2)
            raw = html[lo:lo + raw_window]
    except Exception:
        pass

    idx = blocks.index(target)
    return {
        "spine": spine_index,
        "block": block_index,
        "selector": target.selector,
        "classes": sorted(target.classes),
        "containers": list(target.containers),
        "tag": target.tag,
        "style": target.style.describe(cached_body_base(reader)),
        "text": target.text,
        "prev": _brief(blocks[idx - 1]) if idx > 0 else None,
        "next": _brief(blocks[idx + 1]) if idx + 1 < len(blocks) else None,
        "raw": raw,
    }
