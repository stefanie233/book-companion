"""作者级的活 —— 工序 4（编年校验）/ 5（作品房间）/ 6（生平补全）。

**与书级采集（``harvest.py``）的分工**：采集读**一本书的序跋**，产出语料
和它顺带读到的编年；作者级的活是**跨书累积**的 —— 导入第 3 本张爱玲时，
前两本的编年也要重算。这个依赖不体现在任何一本书上，所以只能靠
``needs_refresh`` 标脏表达，由 ``scripts/refresh_author.py`` 独立进程消费。

三件事的性质完全不同，**不要混在一个函数里**：

| 工序 | 花 token？ | 输入 | 产出 |
|---|---|---|---|
| 4 编年校验 | 否（机械那半） | 已有的 records + 序跋原文 | 缺陷清单 |
| 5 作品房间 | 否 | 他挂着的书 | ``kind=work`` 行 |
| 6 生平补全 | 是（联网 + LLM） | 作者名 + 已有编年 | ``source=web`` 行 |

前两件是**纯函数**（给同样的输入必得同样的输出），所以能零成本单测，
也能在没配 key 的机器上跑。第三件单独一条路，且**必须显式开启** ——
它会把模型的世界知识写进台账，那是要用户点头的事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: 作品房间装哪些 kind。**``kind`` 落库、``room`` 现算** —— 与「证据落库、
#: 导出量现算」同一条：房间是对 kind 的看法，多一个 kind 就要改分类，
#: 而落库的列改不动。设计稿 §五 的「楼层/房间」正是这个投影。
WORK_KINDS = ("work", "publication")
LIFE_KINDS = ("life_event", "relation")


def room_of(kind: str) -> str:
    """``kind`` → 房间。**唯一一处映射**，前端与卡片都读它。"""
    return "work" if kind in WORK_KINDS else "life"


@dataclass
class Finding:
    """一条机械发现。与 ``structure_invariants.Finding`` 同形。"""

    code: str
    question: str
    detail: str = ""

    def render(self) -> str:
        return f"[{self.code}] {self.question}" + (
            f"（{self.detail}）" if self.detail else "")


# ── 工序 4：编年校验（机械那半，零 token）─────────────────────────────────

#: 年份的合理区间。**不是知识，是排错网** —— 它拦的是「模型把 1980 写成
#: 1890」和「把章号当成年份」这类走形，不拦「这个作者到底哪年生的」。
MIN_YEAR = 1000


def _fold(s: str) -> str:
    """空白归一 —— 与图谱那边 ``fabricated_evidence`` 同一把尺子。"""
    return re.sub(r"\s+", "", s or "")


def can_check(record: dict[str, Any], text_by_chunk: dict[str, str],
              fallback_texts: list[str] | None = None) -> bool:
    """这条记录**能不能**做逐字校验。

    **查不了的不算通过，算没查** —— 一个「一条都没查」的体检和一个
    「查了全干净」的体检，输出都是空清单，长得一模一样。所以调用方
    要用它把覆盖率报出来（``沉默不是成功``）。
    """
    if (record.get("source") or "") != "corpus":
        return False
    if not (record.get("evidence_excerpt") or "").strip():
        return False
    if text_by_chunk.get(record.get("chunk_id") or "") is not None:
        return True
    return bool(fallback_texts)


def audit_records(records: list[dict[str, Any]],
                  text_by_chunk: dict[str, str],
                  fallback_texts: list[str] | None = None,
                  *, now_year: int = 2100) -> list[Finding]:
    """工序 4 的机械那一半：**不花 token 能查出来的，就不要问模型。**

    ``text_by_chunk`` 是 ``chunk_id → 该块原文``；``fallback_texts`` 是
    「这条记录没给 chunk_id 时去哪儿找」的兜底正文（该书的序跋块）。
    两者都没有的条目**不报缺陷** —— 校验器不该比被校验者更脆弱，
    查不了就是查不了。

    逐字校验**只对 ``source='corpus'`` 生效**。这是设计稿 §七 那张表：
    corpus 要求逐字原文，web 要求 URL，model 什么都不要求（但也不可注入）。
    拿「网页摘要」去比对书的原文，是把两把尺子当成一把。
    """
    out: list[Finding] = []
    for r in records:
        rid = r.get("id")
        st = (r.get("statement") or "").strip()
        if not st:
            out.append(Finding("empty_statement", "有一条编年没有内容。",
                               f"id={rid}"))
            continue
        y, ye = r.get("year"), r.get("year_end")
        if isinstance(y, int) and not (MIN_YEAR <= y <= now_year):
            out.append(Finding(
                "year_out_of_range",
                f"年份 {y} 不像年份 —— 是不是把章号或别的数字填进来了？",
                f"id={rid} 《{st[:24]}》"))
        if isinstance(y, int) and isinstance(ye, int) and ye < y:
            out.append(Finding(
                "year_order", f"结束年 {ye} 早于起始年 {y}。",
                f"id={rid} 《{st[:24]}》"))
        ev = (r.get("evidence_excerpt") or "").strip()
        if (r.get("source") or "") != "corpus" or not ev:
            continue                       # web/model 的出处不是逐字原文
        hay = text_by_chunk.get(r.get("chunk_id") or "")
        if hay is None:
            if not fallback_texts:
                continue                   # 没有可比对的正文 → 不报
            hay = "\n".join(fallback_texts)
        if _fold(ev) not in _fold(hay):
            out.append(Finding(
                "evidence_not_verbatim",
                "这条编年的「原文」在它指的那块里找不到 —— "
                "要么锚错了，要么这句不是原文。",
                f"id={rid} 《{st[:24]}》"))
    return out


# ── 工序 5：作品房间（零 token）───────────────────────────────────────────

def work_room_rows(*, links: list[dict[str, Any]],
                   titles: dict[str, str],
                   existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """这个人的**作品房间** —— 他挂着的每一本书。

    与工序 3 的区别：采集只从**这本书的序跋**里读到作者提到的作品，
    所以毛姆在《面纱》里说不到《克拉多克太太》。作品房间补的正是这一块：
    **我们书库里有的书，本身就是「他写过这本」的证据** —— 来源标
    ``graph``（本地这一侧的事实），不是模型编的。

    ``existing`` 是该作者已有的 work/publication 行。**标题已经出现在
    某条陈述里的书不再补** —— 采集若已经写过「一九九六年写《我与地坛》」，
    再补一条「《我与地坛》」就是同一件事记两遍，而两遍会让门算出两个答案。

    年份一律留空：**书在库里不等于知道它哪年写的**。无年在有门的场合
    出不去，这是正确的代价（设计稿 §九）。
    """
    covered = " ".join((r.get("statement") or "") for r in existing)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for b in links:
        stable = b.get("stable_book_id") or ""
        if not stable or stable in seen:
            continue
        seen.add(stable)
        title = (titles.get(stable) or "").strip()
        if not title or title in covered:
            continue
        role = b.get("role") or "author"
        if role not in ("author", "co-author"):
            continue                       # 译者/编者写的不是「他的作品」
        out.append({
            "kind": "work",
            "year": None,
            "statement": f"《{title}》",
            "evidence_excerpt": "",
            "stable_book_id": stable,
            "source": "graph",
        })
    return out
