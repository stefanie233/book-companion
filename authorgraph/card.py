"""作者内核 —— 把台账投影成一段能注入 system prompt 的文本。

**纯函数**，和 ``bookgraph/formatter.format_role_kernel`` 同一个位置：
不查库、不调模型、给定输入必得同一输出。所以它能被零成本单测，
也能在 fake provider 下走完整链路。

一条硬约束（本层唯一一条「宁可不说」）：

    **``source='model'`` 的条目可以给用户看，绝不注入角色。**

理由不是省钱，是**它会伪装成事实**。扮演史铁生时喂进去「史铁生 1951 年
生于北京」，角色会说得像真的一样 —— 那正是防幻觉政策要拦的东西。
可展示与可注入是两件事：展示时旁边有「模型推测」的标签，注入时没有标签，
只有第一人称。

另一条：**位置决定哪些话能当事实用。** ``position='avatar'`` 时作者是
借角色说话，语气是他的、经历未必是 —— 卡片必须把这件事说给模型听，
否则它会自动把「我」的每一句话都当自传。

模块里只有 ``format_author_card`` 是纯的；``load_author_card`` 要查库，
是它**不纯的另一半**。分成两半不是为了好看：纯的那半能被零成本单测，
不纯的那半薄到只有一条路径，且**工具与扮演接线共用它** —— 门只算一处。
"""

from __future__ import annotations

from typing import Any

from authorgraph.gate import gate_state

#: 不可注入的来源。想放宽必须先改这里，改不动是**故意的**。
NON_INJECTABLE_SOURCES = ("model",)

_POSITION_ZH = {
    "narrator": "作者本人就是叙述者，非虚构",
    "avatar": "作者借一个角色说话（自传体小说一类）—— 语气是他的，经历未必",
    "editor": "作者是整理者/解说者，不是当事人",
    "recorder": "作者记录别人的事，自己不在场",
    "absent": "作者不在这本书里",
    "unknown": "作者的位置没判出来",
}

_SPEAKER_ZH = {
    "author": "作者本人",
    "via_character": "作者借角色之口",
    "editor": "编者/译者",
    "other": "他人",
}


def injectable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """滤掉不可注入的来源。**唯一的入口** —— 别在别处再写一遍这个判断。"""
    return [r for r in rows
            if (r.get("source") or "model") not in NON_INJECTABLE_SOURCES]


_SOURCE_ZH = {"corpus": "", "graph": "书库", "web": "网页", "model": "推测"}


def format_author_card(author: dict[str, Any], *,
                       position: str = "unknown",
                       corpus: list[dict[str, Any]] | None = None,
                       records: list[dict[str, Any]] | None = None,
                       max_records: int = 40,
                       max_corpus: int = 12,
                       verified_records: set[str] | None = None) -> str:
    """作者内核。给多少说多少，缺什么就说什么缺 —— 不补全、不圆场。"""
    name = (author.get("canonical_name") or "").strip() or "(未知作者)"
    lines: list[str] = [f"## 你要扮演的作者：{name}", ""]
    native = (author.get("name_native") or "").strip()
    if native and native != name:
        lines.insert(1, f"  原文名：{native}")

    who: list[str] = []
    if author.get("nationality"):
        who.append(str(author["nationality"]))
    by, dy = author.get("birth_year"), author.get("death_year")
    if by or dy:
        who.append(f"{by or '?'}–{dy or '?'}")
    for key, label in (("birth_place", "生于"), ("death_place", "卒于")):
        if author.get(key):
            who.append(f"{label}{author[key]}")
    if author.get("primary_language"):
        who.append(f"语言：{author['primary_language']}")
    if author.get("era_note"):
        who.append(str(author["era_note"]))
    if who:
        lines.append("  " + "，".join(who))
    status = author.get("identity_status") or "provisional"
    if status != "resolved":
        lines.append("  身份状态：**待确认**（同名作者可能有多个，"
                     "不要把别处的同名材料当成本人）")
    pos_zh = _POSITION_ZH.get(position, position)
    lines += [f"  在这本书里：{pos_zh}", ""]

    verified = verified_records or set()
    recs = injectable(records or [])[:max_records]
    if recs:
        lines += ["### 关于他的已知事实", ""]
        for r in recs:
            y = r.get("year")
            tag = f"{y} " if isinstance(y, int) else "（年份不详）"
            src = r.get("source") or "model"
            label = _SOURCE_ZH.get(src, src)
            if src == "web":
                label = "网页·引文已核" if str(r.get("id")) in verified else "网页·未核验"
            lines.append(f"  - {tag}{r.get('statement') or ''}"
                         + (f"  ［来源：{label}］" if label else ""))
        lines.append("")
    else:
        lines += ["### 关于他的已知事实", "",
                  "  （暂无。不要凭空编造他的生平 —— 不知道就说不知道。）", ""]

    corp = injectable(corpus or [])[:max_corpus]
    if corp:
        lines += ["### 他自己的话（用来抓口吻，不是事实清单）", ""]
        for c in corp:
            sp = _SPEAKER_ZH.get(c.get("speaker") or "author", "")
            lines.append(f"  - 「{(c.get('excerpt') or '').strip()}」"
                         f"  ［{sp}］")
        lines.append("")

    lines += [
        "### 说话时的约束",
        "",
        "  - 用他的语气、他的词、他关心的事说话。",
        "  - 上面没有的事实，就是**你不知道**的。说「这个我说不上来」，",
        "    比编一个像样的答案好。",
        "  - 不要评论自己这部作品的好坏（那是评论家的事，不是你的事）。",
    ]
    if position == "avatar":
        lines += [
            "  - 这本书里你是**借一个角色**在说话。你可以用那种语气，",
            "    但不要把角色的经历说成你的经历。",
        ]
    if position in ("unknown", "absent"):
        lines += [
            "  - 位置没判准，所以更要谨慎：只说你确有把握的话。",
        ]
    return "\n".join(lines)


def load_author_card(adb: Any, *, name: str = "", author_id: str = "",
                     stable_book_id: str = "",
                     reader_chapter: int | None = None,
                     max_records: int = 40,
                     max_corpus: int = 12) -> dict[str, Any]:
    """取数 → 施门 → 格式化。**全层只有这一处做这件事。**

    ``author_lookup`` 工具与扮演模式的接线共用它 —— 门算两遍就会有两个答案，
    这个项目已经在剧透门上栽过一次（四处各错一处，且每处单独看都像对的）。

    ``reader_chapter is None`` = **不设门**。这不是默认值，是「全局模式」
    那个显式选择，与图谱侧 ``max_chapter=None`` 同一个约定。
    给了章号就按 ``gate.reader_year`` 算，**算不出来一律不展示**
    （fail-closed）——「无门可用」不等于「全书放行」。

    ``not_shown`` 是**卡片里没出现的条数**（被门挡住的 + 无年的 + 超出上限的）。
    它必须报出来：不报的话，「作者卡片怎么这么空」会查不出是没采到还是被挡了。
    """
    # 先按 id，再按名字。id 是稳定主键（会话里存的就是它），
    # 名字解析会漂（去重、改名），所以顺序不能反。
    author = (adb.get_author(author_id) if author_id else None) \
        or (adb.find_author(name) if name else None)
    if not author:
        return {"found": False, "queried": name or author_id or "",
                "note": "作者层还没有这个人的台账。序跋采集还没跑过，"
                        "或这本书确实没有作者第一人称文本。"}

    aid = author["author_id"]
    row = next((b for b in adb.books_of(aid)
                if b["stable_book_id"] == stable_book_id), None)
    position = (row or {}).get("position") or "unknown"

    gated = False
    max_year: int | None = None
    gstate: dict[str, Any] = {"reason": "no_gate", "dated": 0, "ahead": 0}
    if reader_chapter is not None:
        gstate = gate_state(adb, aid, stable_book_id, reader_chapter)
        max_year = gstate["gate"]
        gated = max_year is None
    st = adb.stats(aid)
    # 哪些条目有**逐字引文**（v2 证据链）。展示层用它区分
    # 「网页·引文已核」与「网页·未核验」—— 后者不能当已知事实用。
    try:
        ev_rows = adb.evidence_for_author(aid)
        verified = {str(e.get("target_id")) for e in ev_rows
                    if e.get("target_type") == "record"
                    and e.get("relation") == "supports"
                    and (e.get("quote") or "").strip()}
    except Exception:                                     # noqa: BLE001
        verified = set()

    if gated:
        recs: list[dict[str, Any]] = []
        corp: list[dict[str, Any]] = []
    else:
        recs = adb.records_for(aid, max_year=max_year, limit=max_records)
        corp = adb.corpus_for(aid, max_era_year=max_year, limit=max_corpus)

    out: dict[str, Any] = {
        "found": True,
        "author_id": aid,
        "name": author.get("canonical_name") or name,
        "identity": {
            "name_native": author.get("name_native") or "",
            "birth_place": author.get("birth_place") or "",
            "death_place": author.get("death_place") or "",
            "primary_language": author.get("primary_language") or "",
            "identity_status": author.get("identity_status") or "provisional",
            "identifiers": adb.identifiers_of(aid),
        },
        "position": position,
        "position_note": (row or {}).get("position_note") or "",
        "gate": "no_gate" if reader_chapter is None
                else ("unmapped" if gated else max_year),
        "gate_reason": gstate.get("reason") or "",
        "gate_dated": gstate.get("dated", 0),
        "gate_ahead": gstate.get("ahead", 0),
        "card": format_author_card(author, position=position,
                                   corpus=corp, records=recs,
                                   max_records=max_records,
                                   max_corpus=max_corpus,
                                   verified_records=verified),
        "verified_records": sum(1 for r in recs
                                if str(r.get("id")) in verified),
        "evidence": st.get("evidence", 0),
        "sources": st.get("sources", 0),
        "stats": st,
        "not_shown": {
            "records": max(0, st.get("records", 0) - len(recs)),
            "corpus": max(0, st.get("corpus", 0) - len(corp)),
            "note": "含「读者还没读到的」「无年的」「超出上限的」三类",
        },
    }
    if gated:
        # **两种「空」要说清是哪一种** —— 不然「作者卡片怎么是空的」
        # 会一直查不出原因。上面那个 note 说的正是这件事。
        if gstate.get("reason") == "all_ahead":
            out["gate_note"] = (
                f"这本书的作者语料全在你还没读到的位置"
                f"（{gstate.get('ahead', 0)} 条带年的都在后面，例如卷末的代跋），"
                "所以此刻一条都不展示 —— 定位不到时间线就无法证明安全。"
                "身份与位置仍可参考。")
        else:
            out["gate_note"] = (
                "这本书的序跋里没有带年份的语料，算不出读者此刻的时间点，"
                "因此**不展示生平条目**（定位不到时间线就无法证明安全）。"
                "身份与位置仍可参考。")
    return out
