"""自我说明层：卡片加载、匹配与组包（决策 154）。

卡片是**原子事实**，不是问答对：agent 拿走摘要 / 位置 / 常见误区，按用户
这句话现场组织。状态永远来自探针，卡片正文不许出现「当前未配置」这类值。

匹配刻意做得很轻：别名命中 + 标题/摘要分词交集 + 当前页面加分。二三十张
卡不需要向量，也不需要 BM25。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from services.self_probes import PROBES, run_probe

_ROOT = Path(__file__).resolve().parent.parent
_CARD_DIR = _ROOT / "prompts" / "self"
_INDEX = _CARD_DIR / "index.json"
_CACHE: dict[str, Any] = {"stamp": (), "cards": []}
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)
_WORD = re.compile(r"[a-z0-9]+")

#: danger → 给模型的一句话后果说明。命中就只解释、不代做。
_DANGER = {
    "delete": "删除不可恢复",
    "discard": "丢弃未存档改动不可恢复",
    "rollback": "回滚会改写工作区",
    "merge": "合并可能产生冲突并改写文件",
    "overwrite": "覆盖会替换现有内容",
    "publish": "对外发送后无法撤回",
}


def _norm(text: str) -> str:
    return _PUNCT.sub("", str(text or "").lower())


def _tokens(text: str) -> set[str]:
    text = str(text or "").strip()
    if not text:
        return set()
    try:
        import jieba
    except Exception:                                # noqa: BLE001
        return {w for w in _WORD.findall(text.lower()) if w}
    return {t.strip().lower() for t in jieba.lcut(text)
            if t.strip() and _PUNCT.sub("", t)}


def _read_card(path: Path) -> dict[str, str]:
    """按 ``## 标题`` 切段：一句话 / 展开 / 常见误区。"""
    sections: dict[str, str] = {}
    current = ""
    buf: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return sections

def _stamp() -> tuple[int, ...]:
    files = [_INDEX, *_CARD_DIR.glob("cards/*.md")]
    return tuple(sorted(p.stat().st_mtime_ns for p in files if p.exists()))


def load_cards() -> list[dict[str, Any]]:
    """加载索引与正文；按 mtime 失效（开发时改卡不用重启）。"""
    stamp = _stamp()
    if _CACHE["stamp"] == stamp and _CACHE["cards"]:
        return _CACHE["cards"]
    if not _INDEX.exists():
        return []
    data = json.loads(_INDEX.read_text(encoding="utf-8"))
    cards: list[dict[str, Any]] = []
    for row in data.get("cards") or []:
        path = _CARD_DIR / str(row.get("file") or "")
        if not path.exists():
            continue
        sections = _read_card(path)
        cards.append({
            **row,
            "summary": sections.get("一句话", ""),
            "detail": sections.get("展开", ""),
            "pitfalls": sections.get("常见误区", ""),
        })
    _CACHE.update(stamp=stamp, cards=cards)
    return cards


def _score(question: str, card: dict[str, Any], page: str) -> int:
    qn = _norm(question)
    aliases = [_norm(a) for a in card.get("aliases") or []]
    alias_hit = bool(qn and any(a and a in qn for a in aliases))
    score = 100 if alias_hit else 0
    title = _norm(card.get("title", ""))
    if title and qn and title in qn:
        score += 30
    # 别名命中已经足够强，不再触发 jieba 首次加载（约 1 秒）。
    if not alias_hit:
        qt = _tokens(question)
        if qt:
            score += 4 * len(qt & _tokens(card.get("title", "")))
            score += min(10, len(qt & _tokens(card.get("summary", ""))))
    if page and page in (card.get("pages") or []):
        score += 10
    return score


def explain(question: str = "", *, depth: str = "short", topic: str = "",
            ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回 ``{matched, cards, status?, guard?}``；永不 raise 业务错误。"""
    ctx = ctx or {}
    if str(ctx.get("mode") or "") == "roleplay":
        return {"matched": False, "disabled": True, "reason": "roleplay",
                "hint": "保持角色，不解释产品机制。"}
    cards = load_cards()
    page = str(ctx.get("page") or "")
    picked: list[dict[str, Any]] = []
    if topic:
        picked = [c for c in cards if c.get("id") == topic][:1]
    if not picked and str(question or "").strip():
        ranked = sorted(((_score(question, c, page), c) for c in cards),
                        key=lambda pair: pair[0], reverse=True)
        picked = [c for s, c in ranked if s >= 20][:3]
    if not picked:
        return {"matched": False,
                "suggestions": [c.get("title", "") for c in cards[:3]],
                "hint": "没有匹配的说明；不知道就说不知道，不要编。"}

    out_cards: list[dict[str, Any]] = []
    status: dict[str, Any] = {}
    guards: list[str] = []
    full = str(depth or "short").lower() == "full"
    for card in picked:
        item = {
            "id": card.get("id"),
            "title": card.get("title", ""),
            "where": card.get("where", ""),
            "summary": card.get("summary", ""),
            "related": card.get("related") or [],
        }
        if full and card.get("detail"):
            item["detail"] = str(card["detail"])[:1200]
        if full and card.get("pitfalls"):
            item["pitfalls"] = str(card["pitfalls"])[:600]
        out_cards.append(item)
        probe = card.get("probe")
        if probe and probe in PROBES:
            status[str(probe)] = run_probe(str(probe), ctx)
        for danger in card.get("danger") or []:
            text = _DANGER.get(str(danger))
            if text and text not in guards:
                guards.append(text)

    payload: dict[str, Any] = {"matched": True, "cards": out_cards}
    if status:
        payload["status"] = status
    if guards:
        payload["guard"] = ("涉及不可逆操作：" + "；".join(guards)
                            + "。只解释与确认，不要在同一轮执行。")
    return payload

