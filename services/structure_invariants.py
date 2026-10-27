"""结构不变量 —— 纯代码校验 agent 给出的结构叙事。

这不是「打分器」，是**提问器**。每条发现都是一个具体的问题：「你说 cover
只该有一个，但你标了四个，哪几个其实不是？」—— 问题回给 agent，由它决定
改结论还是补证据说明为什么不改。

为什么要有这一层：agent 的结论会漂（设计稿 7.5 实测同一本书两次跑结论不同）。
机械不变量不含观点、不会漂，正好用来把漂移逼到台面上。

纯函数模块：不碰库、不调 LLM、不抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass

# 与 file_readers/epub_semantics.py 的 10 值枚举同源。
FRONT_TYPES = frozenset({"cover", "copyright", "toc", "preface"})
BACK_TYPES = frozenset({"postscript", "appendix", "bibliography", "index", "notes"})
ALL_TYPES = FRONT_TYPES | BACK_TYPES | {"body"}

# 这些类型在整本书里通常只出现一两次 —— 多出来就值得问一句。
#
# **`toc` 刻意不在此列**：Calibre 常把一页目录拆成 N 个文件（实测窄门 10 个），
# 数量多完全正常。曾经把它设成上限 3，结果对正确结论误报（2026-10-08）。
RARE_TYPES = {"cover": 2, "copyright": 3}


@dataclass
class Finding:
    """一条机械发现。``question`` 是回给 agent 的原话。"""

    code: str
    question: str
    detail: str = ""

    def render(self) -> str:
        return f"[{self.code}] {self.question}" + (f"（{self.detail}）" if self.detail else "")


def check_structure(analysis: dict, *, spine_count: int,
                    toc_total: int = 0) -> list[Finding]:
    """校验一份结构叙事，返回**问题清单**（空 = 没发现问题）。

    *analysis* 是 classifier 输出的 JSON（``genre`` / ``regions`` /
    ``chapters`` / ``boundary_rule`` / ``text_forms``）。字段缺失不报错，只是
    少查几项 —— 校验器不该比被校验者更脆弱。
    """
    if not isinstance(analysis, dict):
        return [Finding("empty", "没有拿到结构叙事。")]

    chapters = _int_keyed(analysis.get("chapters"))
    regions = _regions(analysis.get("regions"))

    out: list[Finding] = []
    out += _check_chapters(chapters, spine_count)
    out += _check_order(chapters, spine_count)
    out += _check_regions(regions, spine_count)
    out += _check_genre(analysis.get("genre"))
    out += _check_forms(analysis.get("text_forms"))
    out += _check_boundary(analysis.get("boundary_rule"), toc_total)
    return out


def _int_keyed(raw) -> dict[int, str]:
    """``{"12": "preface"}`` → ``{12: "preface"}``；非法键值直接丢掉。"""
    out: dict[int, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            out[int(k)] = str(v).strip()
        except (TypeError, ValueError):
            continue
    return out


def _regions(raw) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for r in raw:
        if not isinstance(r, dict):
            continue
        try:
            lo, hi = int(r.get("from")), int(r.get("to"))
        except (TypeError, ValueError):
            continue
        out.append({"from": lo, "to": hi, "role": str(r.get("role") or ""),
                    "what": str(r.get("what") or "")})
    out.sort(key=lambda r: r["from"])
    return out


def _check_chapters(chapters: dict[int, str], spine_count: int) -> list[Finding]:
    out: list[Finding] = []
    bad_range = sorted(si for si in chapters if not (0 <= si < spine_count))
    if bad_range:
        out.append(Finding(
            "out_of_range",
            f"这些 spine 序号超出本书范围（0-{spine_count - 1}）：{bad_range[:10]}。"
            f"是不是把不存在的文件也标了？",
        ))
    unknown = sorted({t for t in chapters.values() if t not in ALL_TYPES})
    if unknown:
        out.append(Finding(
            "unknown_type", f"这些体例值不在 10 个枚举里：{unknown}。请改用枚举值。"))
    for t, cap in RARE_TYPES.items():
        n = sum(1 for v in chapters.values() if v == t)
        if n > cap:
            out.append(Finding(
                "rare_type_flood",
                f"你标了 {n} 个 {t}，而一本书通常不超过 {cap} 个。"
                f"多出来的那些，是什么让你认为它们属于 {t}？",
            ))
    return out


def _check_order(chapters: dict[int, str], spine_count: int) -> list[Finding]:
    """顺序合法性：前置页不该排在正文之后，后置页不该排在正文结束之前。

    正文 = **没被列进** ``chapters`` 的那些 spine（输出契约：只列非 body）。
    """
    body = [si for si in range(spine_count) if si not in chapters]
    if not body:
        return [Finding("no_body", "你一个 body 都没留 —— 整本书都是体例页吗？")]
    first_body, last_body = min(body), max(body)
    out: list[Finding] = []
    late_front = sorted(si for si, t in chapters.items()
                        if t in FRONT_TYPES and si > first_body)
    if late_front:
        out.append(Finding(
            "front_after_body",
            f"这些前置页排在正文开始之后：{late_front[:10]}。书的顺序是"
            f"「前置 → 正文 → 后置」，是不是判错了？",
            detail=f"正文从 spine[{first_body}] 开始",
        ))
    early_back = sorted(si for si, t in chapters.items()
                        if t in BACK_TYPES and si < last_body)
    if early_back:
        out.append(Finding(
            "back_before_body",
            f"这些后置页排在正文结束之前：{early_back[:10]}。除非这本书确实在"
            f"正文中间插了附录，否则值得复核。",
            detail=f"正文到 spine[{last_body}] 结束",
        ))
    return out


def _check_regions(regions: list[dict], spine_count: int) -> list[Finding]:
    if not regions:
        return [Finding("no_regions",
            "没有给出 regions —— spine 序列没有切成区段。")]
    out: list[Finding] = []
    for a, b in zip(regions, regions[1:]):
        if b["from"] < a["to"]:
            out.append(Finding("region_overlap",
                f"区段 [{a['from']},{a['to']}) 与 [{b['from']},{b['to']}) 重叠。"))
    covered: set[int] = set()
    for r in regions:
        covered.update(range(max(0, r["from"]), max(0, r["to"])))
    missing = sorted(set(range(spine_count)) - covered)
    if missing:
        out.append(Finding("region_gap",
            f"这些 spine 不在任何区段里：{missing[:12]}（共 {len(missing)} 个）。",
            detail="区段是左闭右开，to 不含 —— 只含一个 spine 的区段应写 "
                   "from: N, to: N+1"))
    order = {"front": 0, "body": 1, "back": 2}
    seq = [order[r["role"]] for r in regions if r["role"] in order]
    if seq != sorted(seq):
        out.append(Finding("region_order",
            f"区段的 role 顺序是 {[r['role'] for r in regions]}，"
            f"但书的顺序应当是 front → body → back。"))
    return out


def _check_genre(genre) -> list[Finding]:
    if not isinstance(genre, dict) or not str(genre.get("kind") or "").strip():
        return [Finding("no_genre",
            "没有给出体裁判断。体裁决定切分判据，缺了它后面所有结论都没有依据。")]
    if not str(genre.get("evidence") or "").strip():
        return [Finding("genre_no_evidence",
            f"体裁判为「{genre.get('kind')}」但没给证据。依据是什么？")]
    return []


def _check_forms(forms) -> list[Finding]:
    if not isinstance(forms, list) or not forms:
        return [Finding("no_forms",
            "没有给出 text_forms —— 正文内部的文本形态一个都没识别。")]
    out: list[Finding] = []
    for f in forms:
        if not isinstance(f, dict):
            continue
        if not str(f.get("selector") or "").strip():
            out.append(Finding("form_no_selector", "有条 text_form 没写 selector。"))
        elif not str(f.get("evidence") or "").strip():
            out.append(Finding("form_no_evidence",
                f"{f.get('selector')} 判为「{f.get('name')}」但没给证据。"))
    return out


def _check_boundary(rule, toc_total: int) -> list[Finding]:
    if rule is None:
        return []          # 明确允许 null —— 有些书确实定不下来
    if not isinstance(rule, dict) or not isinstance(rule.get("rule"), dict):
        return [Finding("bad_boundary",
            "boundary_rule 给了，但里面的 rule 不是合法判据对象。")]
    out: list[Finding] = []
    if not str(rule.get("summary") or "").strip():
        out.append(Finding("boundary_no_summary",
            "boundary_rule 没写 summary —— 这个判据全书命中多少处、与目录对账"
            "几条、conflicts 是什么？"))
    if toc_total <= 0:
        out.append(Finding("boundary_no_ground_truth",
            "这本书没有可用目录，章界判据没有外部依据可对账。你的依据是什么"
            "（间距均匀性？分页符？），写进 summary。"))
    return out


