"""导入分析编排 —— 假设 → 对抗 → 机械校验 → 收敛。

为什么不是「跑一次 agent 拿结果」
--------------------------------
单趟分类会漂（设计稿 7.5：同一本书两次跑给出不同结论）。原因不是模型不稳，
是**没有东西逼它收敛**：一次成型的判断，对错都无从检验。

这里改成三轮相互作用：

    Pass 1  立论   classifier 读全书，给出结构叙事
    Pass 2  证伪   verifier（全新上下文）拿结论去找反例，必须给反例
    Pass 3  校验   纯代码检查机械不变量，违规项变成**问题**回炉

三者交替，直到没有问题、或轮次耗尽。结构不是被设计出来的，是这三方作用后
**剩下**的那个。

关键设计：verifier 只拿**结论**、不拿**推理过程**。看得见对方的推理链，它就会
替对方辩护 —— 对抗的价值全在上下文隔离上。
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field

from llm_json import repair_unescaped_quotes
from services.structure_invariants import Finding, check_structure

@dataclass
class AnalysisResult:
    """一次完整分析的结果。"""

    analysis: dict | None = None        # classifier 的结构叙事
    verifier: dict | None = None        # verifier 的证伪结果
    findings: list[Finding] = field(default_factory=list)   # 末轮的机械发现
    rounds: int = 0
    log: list[str] = field(default_factory=list)
    raw_classifier: str = ""
    raw_verifier: str = ""
    # 末轮的结论是否**真的**经过证伪。证伪趟耗尽预算、没吐出 JSON 时为 False
    # —— 那时「没有反例」是未知，不是干净（见 analyze_book 里的注释）。
    verified: bool = False

    @property
    def clean(self) -> bool:
        """结论可用，且没有未解决的机械问题。"""
        return self.analysis is not None and not self.findings


# ── 从 agent 输出里挖 JSON ────────────────────────────────────────────────
# 落库脚本与编排都要用 —— 实现放这一份，别让它漂成两份。

DEFAULT_REQUIRED_KEYS = ("chapters", "text_forms")


def load_agent_json(text: str,
                    required_keys: tuple[str, ...] = DEFAULT_REQUIRED_KEYS) -> dict | None:
    """从 agent 的 result_text 里挖出最终 JSON。

    result_text 是所有轮次 chunk 的拼接（think / 工具标记 / 正文混在一起），
    JSON 在最后，常被 ``` 围栏包着，有时后面还补一段说明文字。

    做法：**逐个 ``{`` 试 raw_decode**，取第一个能解出完整对象、且带
    *required_keys* 里任一键的位置。

    **不能**用「先锚定键、再向前找最近的 ``{``」—— 输出契约里的 ``regions``
    是**对象数组**，那样会锚到数组里最后一个区段对象上。2026-10-08 实测：
    整个输出被解析成了一个 ``{"from":21,"to":23,...}`` 的区段片段。
    """
    s = (text or "").strip()
    if s.endswith("```"):
        s = s[:-3].rstrip()
    found = _scan_for_object(s, required_keys)
    if found is not None:
        return found
    # LLM 用 ASCII 直引号写中文引号（``把爱情与"德行"对立``）时，raw_decode 会在
    # 字符串中间提前收尾、后续整段错位。转义后重扫一遍；仍不行才落 _load_loose。
    fixed = repair_unescaped_quotes(s)
    if fixed != s:
        found = _scan_for_object(fixed, required_keys)
        if found is not None:
            return found
    return _load_loose(s, required_keys)


def _scan_for_object(s: str, required_keys: tuple[str, ...]) -> dict | None:
    """逐个 ``{`` 试 raw_decode，取第一个带 required_keys 的完整对象。"""
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            data, _ = dec.raw_decode(s[i:])
        except Exception:
            continue
        if isinstance(data, dict) and any(k in data for k in required_keys):
            return data
    return None


def _brace_spans(s: str):
    """产出每个**顶层** ``{...}`` 的 ``(start, end)``，跳过字符串里的花括号。

    单引号只在「像 Python 字符串起手」的位置才算定界符（前一个非空字符是
    ``{[,:`` ）—— 否则英文思考文本里的撇号（``don't``）会把扫描带偏。
    """
    stack: list[int] = []
    quote = ""
    prev_sig = ""
    for i, ch in enumerate(s):
        if quote:
            if ch == quote and s[i - 1:i] != "\\":
                quote = ""
            continue
        if ch in "\"'" and (ch == '"' or prev_sig in "{[,:"):
            quote = ch
            continue
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            if not stack:
                yield start, i
        if not ch.isspace():
            prev_sig = ch


def _load_loose(s: str, required_keys: tuple[str, ...]) -> dict | None:
    """兜底：模型直接吐 Python 字面量（单引号、无引号键，实测房思琪/窄门）。

    **不能**用「先找键、再往前找 ``{``」：键本身就可能是单引号写的
    （2026-10-08 实测窄门，整份输出是 ``{'genre': ...}``，双引号锚点全落空），
    而且 ``regions`` 是对象数组，往前找会锚到数组里最后一个区段对象上。

    改成与 JSON 路径同构：**逐个顶层 ``{`` 试 literal_eval**，取第一个能解出
    含 *required_keys* 的字典的位置。
    """
    for start, end in _brace_spans(s):
        try:
            data = ast.literal_eval(s[start:end + 1])
        except Exception:
            continue
        if isinstance(data, dict) and any(k in data for k in required_keys):
            return data
    return None


# ── 提示词装配 ────────────────────────────────────────────────────────────


def _classifier_prompt(epub_path: str, feedback: list[str]) -> str:
    """Pass 1 的用户消息：书的位置 +（回炉时）上一轮被指出的问题。"""
    parts = [f"EPUB 路径：{epub_path}"]
    if feedback:
        parts.append(
            "\n## 上一轮结论被指出的问题\n"
            "逐条处理：要么改结论，要么补证据说明为什么这条质疑不成立。"
            "**不要无视它们**。\n" + "\n".join(f"- {f}" for f in feedback)
        )
    return "\n".join(parts)


def _verifier_prompt(epub_path: str, analysis: dict) -> str:
    """Pass 2 的用户消息：**只有结论**，没有推理过程。

    这是对抗有效性的前提 —— 看得见对方的推理链，verifier 就会替对方辩护，
    对抗趟就退化成橡皮图章。
    """
    return (
        f"EPUB 路径：{epub_path}\n\n"
        "## 待校验的结论\n```json\n"
        + json.dumps(analysis, ensure_ascii=False, indent=2)
        + "\n```\n"
    )


def _book_facts(epub_path: str) -> tuple[int, int]:
    """``(spine_count, toc_total)`` —— 机械校验需要的基本量。"""
    from file_readers import open_book
    r = open_book(epub_path)
    try:
        r._ensure_loaded()
        return len(r._spine_ids), len(getattr(r, "_toc_all", []) or [])
    finally:
        if hasattr(r, "close"):
            r.close()


def _verifier_feedback(verifier: dict | None) -> list[str]:
    """把 verifier 的质疑整理成回炉给 classifier 的问题。"""
    if not isinstance(verifier, dict):
        return []
    out: list[str] = []
    for c in verifier.get("challenges") or []:
        if not isinstance(c, dict):
            continue
        claim = str(c.get("claim") or "").strip()
        if not claim:
            continue
        sev = str(c.get("severity") or "").strip()
        ev = str(c.get("evidence") or "").strip()
        out.append(f"[证伪·{sev or '?'}] {claim}" + (f"  证据：{ev}" if ev else ""))
    for m in verifier.get("missed") or []:
        if not isinstance(m, dict):
            continue
        what = str(m.get("what") or "").strip()
        if what:
            ev = str(m.get("evidence") or "").strip()
            out.append(f"[漏掉] {what}" + (f"  证据：{ev}" if ev else ""))
    return out


# ── 主循环 ────────────────────────────────────────────────────────────────


def _notify(cb, msg: str) -> None:
    if cb is None:
        return
    try:
        cb(msg)
    except Exception:
        pass


async def analyze_book(runner, epub_path: str, *, title: str = "",
                       max_rounds: int = 2,
                       on_progress=None) -> AnalysisResult:
    """跑完整的分析循环，返回末轮结论 + 未解决的问题。

    *runner* 是构造好的 ``SubAgentRunner``（带着 registry 与飞行记录仪）。
    *max_rounds* 是**回炉轮数**上限 —— 每轮含一次立论 + 一次证伪，所以 agent
    调用最多 ``2 × max_rounds`` 次。
    """
    res = AnalysisResult()
    try:
        spine_count, toc_total = _book_facts(epub_path)
    except Exception as exc:
        res.log.append(f"读不出书的骨架（spine/toc）：{exc}")
        return res

    desc = (f"分析《{title}》的章节体例与正文文本形态" if title
            else "分析这本书的章节体例与正文文本形态")
    feedback: list[str] = []

    for rnd in range(1, max_rounds + 1):
        res.rounds = rnd

        out = await runner.run(agent_type="book_classifier", description=desc,
                               prompt=_classifier_prompt(epub_path, feedback))
        res.raw_classifier = getattr(out, "result_text", "") or ""
        analysis = load_agent_json(res.raw_classifier)
        if analysis is None:
            feedback = ["你上一轮**没有输出 JSON**。无论查到什么程度，最后都必须"
                        "输出那个 JSON 对象（拿不准的用 best guess）。"]
            res.log.append(f"第 {rnd} 轮：classifier 未输出可解析的 JSON")
            _notify(on_progress, res.log[-1])
            continue

        res.analysis = analysis
        res.log.append(f"第 {rnd} 轮：classifier 完成（{len(res.raw_classifier)} 字符）")

        res.findings = check_structure(
            analysis, spine_count=spine_count, toc_total=toc_total)
        res.log.append(f"第 {rnd} 轮：机械校验发现 {len(res.findings)} 条问题")
        _notify(on_progress, res.log[-1])

        ver = await runner.run(agent_type="book_verifier", description=desc,
                               prompt=_verifier_prompt(epub_path, analysis))
        res.raw_verifier = getattr(ver, "result_text", "") or ""
        res.verifier = load_agent_json(res.raw_verifier, ("verdict", "challenges"))

        # 解析不出 ≠ 没有反例。证伪趟耗尽预算或输出漂移时，这一轮的结论是
        # **未经验证**的，绝不能当成收敛 —— 实测第 2 轮 verifier 用满 16 轮
        # 预算、还在发工具调用就被截断，差点被误判成「无问题、无反例」。
        # 沉默不是成功。
        if res.verifier is None:
            res.verified = False
            res.log.append(
                f"第 {rnd} 轮：证伪趟**未产出结论**（输出 {len(res.raw_verifier)} "
                f"字符、无 JSON）—— 本轮结论未经验证")
            _notify(on_progress, res.log[-1])
            break

        res.verified = True
        vfb = _verifier_feedback(res.verifier)
        res.log.append(
            f"第 {rnd} 轮：证伪趟 verdict={(res.verifier or {}).get('verdict', '?')}"
            f"，提出 {len(vfb)} 条")
        _notify(on_progress, res.log[-1])

        feedback = [f.render() for f in res.findings] + vfb
        if not feedback:
            res.log.append(f"第 {rnd} 轮：无问题、无反例 —— 收敛。")
            _notify(on_progress, res.log[-1])
            break
    else:
        # 两个来源都要报。只数机械发现会漏掉证伪趟的质疑 —— 实测窄门第 2 轮
        # 机械 0 条、证伪 2 条，日志却写成「仍有 0 条未解决」，读起来像收敛。
        res.log.append(
            f"达到回炉上限 {max_rounds} 轮：机械问题 {len(res.findings)} 条、"
            f"证伪质疑 {len(feedback) - len(res.findings)} 条仍未解决。")

    return res
