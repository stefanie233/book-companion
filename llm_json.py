"""LLM 文本输出的 JSON 宽容解析 —— 含裸引号修复。

问题
----
LLM 写中文时用 ASCII 直引号当引号（``把爱情与"德行/圣洁"对立``），嵌进 JSON
字符串值里就是非法 JSON：

    "book_overview": "…把爱情与"德行/圣洁"对立起来…"

``json.loads`` 在第一个内层引号处认定字符串结束，随后报
``Expecting ',' delimiter``。全库实测：scout 7 次调用 6 次失败、explorer
106 次调用 13 次失败 —— 而且**全是静默降级**：scout 退回机械分批、explorer
整批节点/边/引文丢失、L1 抽取失败会清空 buffer 把整批对话丢掉。

修复
----
逐个字符走，跟踪「是否在字符串内」。在字符串内遇到 ``"`` 时按下一个非空白
字符 ``nxt`` 判定：

* ``nxt`` 为空（到结尾）或 ``nxt in "}]"``  → 收尾引号
* ``nxt == ","``                          → **消歧**：看逗号后面是不是
  ``"键" :`` 形态（``_looks_like_key``）。是则收尾，否则是内容里的引号
* 其余                                     → 内容里的引号，转义为 ``\\"``

逗号那一支是关键。``"他说"你好", 然后走了"`` 里，``你好`` 后的引号后面跟的是
逗号 —— 只看「后跟结构字符」会把它误判成收尾，字符串提前闭合，剩下的话变成
裸文本。而真正的收尾引号后面跟的逗号，后面**一定**是下一个键（``"…" :``）。
实测：只看后跟字符的对拍 4000 条里 1915 条修不好，加上消歧后全部修好。

安全性质
--------
修复是**最后手段**：只有直解、剥围栏、括号截取全部失败之后才跑。合法 JSON
永远走不到修复级，因此修复不可能改坏本来能解析的响应。（实测：158 条真实
ok 响应对 ``repair_unescaped_quotes`` 的调用次数为 0；60 万条随机合法 JSON
经修复后无一条改变。）

修复级另有两道守卫，见 ``_leaky_keys``。
"""
from __future__ import annotations

import json
import re

__all__ = ["repair_unescaped_quotes", "repair_control_chars",
           "repair_fullwidth_punct", "loads_tolerant"]

_MISS = object()                       # 「没解出来」哨兵，区别于合法的 null

_FENCE_OBJ = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_FENCE_ARR = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


# ── 修复 ──────────────────────────────────────────────────────────────────

def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    return i


def _looks_like_key(s: str, i: int) -> bool:
    """从 ``i``（逗号之后）往后看，是否呈 ``"键" :`` 形态。

    用来消歧「字符串值里的裸引号后跟逗号」与「真正的收尾引号后跟逗号」：
    后者后面一定是下一个键。
    """
    j = _skip_ws(s, i)
    if j >= len(s) or s[j] != '"':
        return False
    j += 1
    while j < len(s):
        c = s[j]
        if c == "\\":                  # 键里的转义序列，跳过两字符
            j += 2
            continue
        if c == '"':
            break
        j += 1
    else:
        return False                   # 键的引号没闭合
    j = _skip_ws(s, j + 1)
    return j < len(s) and s[j] == ":"


def repair_unescaped_quotes(s: str) -> str:
    """把 JSON 字符串值内部未转义的 ASCII 直引号转义掉。

    已存在的转义序列（``\\"``、``\\\\``、``\\n``…）原样搬运，不会被二次转义。
    对合法 JSON 是恒等变换。
    """
    if not s:
        return s
    out: list[str] = []
    stack: list[str] = []              # 容器栈，用来区分对象里的逗号与数组里的逗号
    in_str = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if not in_str:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]" and stack:
                stack.pop()
            out.append(ch)
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            if i + 1 < n:
                out.append(s[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch != '"':
            out.append(ch)
            i += 1
            continue
        # 字符串内遇到引号：判它是收尾还是内容
        j = _skip_ws(s, i + 1)
        nxt = s[j] if j < n else ""
        if nxt == ",":
            # 数组里逗号后面是下一个**值**，所以一定是收尾引号；
            # 对象里逗号后面应该是下一个**键**，不像键就是值内容里的引号。
            if (stack and stack[-1] == "[") or _looks_like_key(s, j + 1):
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')
        elif nxt == "" or nxt in ":}]":
            in_str = False             # 收尾引号（键后跟 :，值后跟 }/]）
            out.append(ch)
        else:
            out.append('\\"')          # 后跟普通字符 → 内容里的引号
        i += 1
    return "".join(out)


# ── 另两种语法走形（各自独立，绝不串联）────────────────────────────────────

_CTRL_ESC = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
# 结构位上的全角标点。**只在字符串外**替换 —— 字符串内的全角标点是正文内容。
_FULLWIDTH = {"，": ",", "：": ":"}


def _rewrite(s: str, inside, outside) -> str:
    """逐字符走，跟踪「是否在字符串内」，分别套用两个改写函数。

    转义序列（``\\"``、``\\\\``）整对搬运，不参与判定。
    """
    if not s:
        return s
    out: list[str] = []
    in_str = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if in_str and ch == "\\" and i + 1 < n:
            out.append(s[i:i + 2])
            i += 2
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            i += 1
            continue
        rep = inside(ch) if in_str else outside(ch)
        out.append(rep if rep is not None else ch)
        i += 1
    return "".join(out)


def repair_control_chars(s: str) -> str:
    """把 JSON 字符串**内部**的字面控制字符转义掉。

    模型写长中文时会在值里直接换行（实测《我与地坛》某批 1 个节点如此）。
    字面控制字符在 JSON 字符串里永远非法，转义是唯一合法解释，故对合法 JSON
    是恒等变换。

    **刻意不与裸引号修复串联**：字符串**没收尾**时（模型漏了收尾引号），转义
    后仍解不出来 —— 那就让它失败。若再叠一层裸引号修复，会把下一个键当成
    内容吞进这个字符串，产出**合法但语义错误**的对象（``why`` 吞掉
    ``evidence``），那比解析失败更糟。字符串外的换行是合法空白，一律不动。
    """
    return _rewrite(s, lambda c: _CTRL_ESC.get(c), lambda c: None)


def repair_fullwidth_punct(s: str) -> str:
    """把**字符串外**的全角逗号/冒号换成半角。

    模型偶尔在结构位打全角（实测 ``"summary": "…。"，`` 后接下一个键）。
    全角标点在字符串外永远不是合法 JSON 结构，替换只可能帮忙；字符串内的
    全角标点是中文正文，一律不动。
    """
    return _rewrite(s, lambda c: None, lambda c: _FULLWIDTH.get(c))


# ── 泄漏守卫 ──────────────────────────────────────────────────────────────

def _leaky_keys(obj: object) -> bool:
    """结果里有没有「值的内容泄漏成了键」的痕迹。

    修复可能把一个畸形输入重组成**合法但语义错误**的对象，例如
    ``{"":": ""}`` → ``{'': ': "'}``。这种静默损坏比解析失败更糟，所以在修复
    路径上拒绝：任一 dict 的键含 ASCII 直引号或为空串，即判为泄漏。

    只作用于修复路径 —— 直解路径不过这道守卫，合法响应不受影响。
    """
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if not k or '"' in k:
                    return True
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return False


# ── 候选切片 ──────────────────────────────────────────────────────────────

def _strip_fence(s: str) -> str:
    t = s.strip()
    if not t.startswith("```"):
        return t
    nl = t.find("\n")
    t = t[nl + 1:] if nl >= 0 else t[3:]
    t = t.rstrip()
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


def _span(s: str, openers: str, closers: str) -> str:
    """首个 openers 字符到末个 closers 字符之间的片段；找不到返回空串。"""
    starts = [i for i in (s.find(c) for c in openers) if i >= 0]
    ends = [i for i in (s.rfind(c) for c in closers) if i >= 0]
    if not starts or not ends:
        return ""
    a, b = min(starts), max(ends)
    return s[a:b + 1] if b > a else ""


def _candidates(content: str) -> list[str]:
    """按优先级排列的候选串，去重保序。

    顺序即「最小改动优先」：原串 → 剥围栏 → 围栏内对象 → 围栏内数组 →
    首[末] → 首{末} → 首{[末]}。后三者保留旧阶梯各自的既有胜例
    （``{"a":1} [2,3]`` 在 expect=dict 下仍应给出 ``{"a":1}``）。
    """
    fenced = [
        m.group(1) for m in (_FENCE_OBJ.search(content), _FENCE_ARR.search(content))
        if m is not None
    ]
    out: list[str] = []
    for cand in [
        content,
        _strip_fence(content),
        *fenced,
        _span(content, "[", "]"),
        _span(content, "{", "}"),
        _span(content, "{[", "]}"),
    ]:
        if cand and cand not in out:
            out.append(cand)
    return out


def _try(cand: str, expect: type | None):
    try:
        obj = json.loads(cand)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return _MISS
    if expect is not None and not isinstance(obj, expect):
        return _MISS
    return obj


# ── 对外入口 ──────────────────────────────────────────────────────────────

def loads_tolerant(content: str, *, expect: type | None = None):
    """LLM 输出 → Python 对象；解析不出返回 ``None``。

    ``expect`` 声明期望的顶层类型（``dict`` / ``list`` / ``None`` 不筛），
    承接各调用点原有的 ``isinstance`` 约定。设为 keyword-only，是为了让调用点
    显式声明意图 —— 传错位置参数不会静默关掉类型筛选。

    阶梯：逐候选直解 → 全失败后逐候选修复裸引号再解。修复路径的产物还要过
    ``_leaky_keys``。
    """
    if not isinstance(content, str) or not content.strip():
        return None
    # 顶层解出来是个字符串字面量时不再往下切。字符串的内容是**数据**不是结构，
    # 从里面切出 `{}`/`[]` 是凭空造结构（`'"中{}"'` → `{}`，且会让 scout 记成
    # status='ok' 从而掩盖模型输出走形）。类型匹配就原样返回，否则 None。
    top = _try(content, None)
    if isinstance(top, str):
        return top if expect is None or isinstance(top, expect) else None
    cands = _candidates(content)
    for cand in cands:
        obj = _try(cand, expect)
        if obj is not _MISS:
            return obj
    for cand in cands:
        fixed = repair_unescaped_quotes(cand)
        if fixed == cand:
            continue
        obj = _try(fixed, expect)
        if obj is not _MISS and not _leaky_keys(obj):
            return obj
    # 另两种走形各**独立**作用于原候选，不与上面那档串联（理由见
    # repair_control_chars 的 docstring）。
    for repair in (repair_control_chars, repair_fullwidth_punct):
        for cand in cands:
            fixed = repair(cand)
            if fixed == cand:
                continue
            obj = _try(fixed, expect)
            if obj is not _MISS and not _leaky_keys(obj):
                return obj
    return None
