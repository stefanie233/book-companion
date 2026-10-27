"""把召回结果排成 prompt 素材块：raw（原文摘录）与 card（蒸馏卡）。

两种形态对应两种假设：
- raw：原文密度高、语气可感，但专名与设定容易泄漏；
- card：去专名、只留可迁移的写法与细节，注意力更省，但可能变空泛。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from writebench import refs


def contract_terms(case: Mapping[str, Any]) -> list[str]:
    """检索词 = 场景意图，不是本作人名。

    人名只在本书里有意义，拿去检索参考书只会搅进噪声、还招泄漏；
    所以先扣掉账本里的 subject_ref。显式 ``references.query`` 优先。
    """
    explicit = [str(x) for x in
                ((case.get("references") or {}).get("query") or []) if str(x)]
    if explicit:
        return explicit
    contract = case.get("contract") or {}
    work = case.get("work") or {}
    parts: list[str] = []
    parts += [str(contract.get("purpose") or ""),
              str(work.get("premise") or "")]
    for key in ("must_hit_now", "must_preserve", "forbidden_cross"):
        parts.extend(str(x) for x in (contract.get(key) or []))
    names = {str(f.get("subject_ref") or "") for f in case.get("facts") or []}
    names |= {str(work.get("title") or ""),
              str((case.get("chapter") or {}).get("title") or "")}
    terms = refs.query_terms("\n".join(parts))
    return [t for t in terms if t not in names]


def collect(case: Mapping[str, Any], *, project_dir: Path,
            terms: Sequence[str] | None = None
            ) -> dict[str, Any]:
    terms = list(terms or contract_terms(case))
    refs_cfg = case.get("references") or {}
    books_db = project_dir / "data" / "books.db"
    graphs = project_dir / "data" / "graphs"
    books: list[dict[str, Any]] = []
    for book in refs_cfg.get("books") or []:
        sid = str(book.get("stable_book_id") or "")
        graph = refs.recall_graph(graphs / f"graph_{sid}.db", terms)
        text = refs.recall_text(books_db, str(book.get("book_id") or ""),
                                terms, limit=3)
        books.append({"label": book.get("label") or sid, "graph": graph,
                      "text": text})
    return {"terms": terms, "books": books}


def _clean(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip("，。；、 ") + "…"


def _pick(items: Sequence[Mapping[str, Any]], n: int) -> list[Mapping[str, Any]]:
    seen: set[str] = set()
    out: list[Mapping[str, Any]] = []
    for item in items:
        key = _clean(item.get("value") or item.get("evidence_text")
                     or item.get("quote_text") or "", 200)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= n:
            break
    return out


def render_raw(pack: Mapping[str, Any], *, max_chars: int = 1400) -> str:
    lines = ["## 参考素材（另一个故事；只借写法与细节，"
             "不得搬用设定、人名、地名、作品名）"]
    for book in pack.get("books") or []:
        graph = book.get("graph") or {}
        obs = _pick(graph.get("observations") or [], 2)
        quotes = _pick(graph.get("quotes") or [], 2)
        edges = _pick(graph.get("edges") or [], 2)
        texts = _pick(book.get("text") or [], 2)
        if not (obs or quotes or edges or texts):
            continue
        lines.append(f"### 《{book['label']}》")
        for item in obs:
            who = _clean(item.get("node_name"), 12)
            value = _clean(item.get("value"), 90)
            lines.append(f"- 人物质感：{who}：{value}" if who else f"- 人物质感：{value}")
        for item in edges:
            src = item.get("source_name") or ""
            tgt = item.get("target_name") or ""
            ev = _clean(item.get("evidence_text"), 90)
            lines.append(f"- 关系/事件：{src} —{item.get('kind')}→ {tgt}｜{ev}")
        for item in quotes:
            quote = _clean(item.get("quote_text"), 100)
            ctx = _clean(item.get("scene_context"), 50)
            lines.append(f"- 对话：「{quote}」{('（' + ctx + '）') if ctx else ''}")
        for item in texts:
            body = _clean(item.get("content"), 180)
            lines.append(f"- 原文（第{item.get('chapter_index')}章）：{body}")
        if len("\n".join(lines)) > max_chars:
            break
    text = "\n".join(lines)
    return text if len(text) > 120 else ""


def skeleton(pack: Mapping[str, Any], *, max_chars: int = 1800) -> str:
    """给蒸馏调用看的候选池；比 raw 多，但仍是确定性排版。"""
    return render_raw(pack, max_chars=max_chars)


DISTILL_SYSTEM = """你在为一位中文小说作者准备「可迁移的写法素材卡」。
输入是另一本书的片段与人物观察。你要把它们变成这位作者能直接借用的
写法与细节，不能让他照搬原书。

输出纯文本，不超过 {limit} 字，固定三节：
【可借用的写法】2-3 条，讲结构/视角/节奏/信息给法，不写具体情节。
【可转化的细节】3-5 条，具体、可感的物象或动作，能放进另一个故事。
【句式参考】1-2 条，只保留句子的节奏与语气，去掉人名地名。

硬规则：
1. 不得出现原书的人名、地名、作品名；
2. 不得总结原书剧情；
3. 不写「应该」「建议」这类指导语，直接写素材本身；
4. 找不到就少写，不要编。
"""


DISTILL_SYSTEM_V2 = """你在为一位中文小说作者准备「可迁移的细节素材卡」。
输入是另一本书的片段与人物观察。你要把它们拆成这位作者能直接借用的
观察方式与细节，不能让他照搬原书，也不能让他照着写情节。

输出纯文本，不超过 {limit} 字，固定两节：

【可借用的观察】2-3 条，只讲「看什么、怎么看得见」：视角落在哪个部位、
用哪个感官锚定、在哪个动作上停住。不写情节走向，不写人物关系判断，
不写「先抑后扬」这类叙事策略，不给结局或点题的比喻。

【可转化的细节】3-5 条，具体、可感的物象或动作，能放进另一个故事。

硬规则：
1. 不得出现原书的人名、地名、作品名；
2. 不得总结原书剧情，不得出现可被照搬的完整对白；
3. 不写「应该」「建议」这类指导语，直接写素材本身；
4. 找不到就少写，不要编。
"""


def render_card(distilled: str, labels: Sequence[str]) -> str:
    body = (distilled or "").strip()
    if not body:
        return ""
    head = ("## 参考素材 · " + "、".join(f"《{x}》" for x in labels)
            + "（另一个故事；只借写法与细节，不得搬用设定与人名）")
    return head + "\n" + body
