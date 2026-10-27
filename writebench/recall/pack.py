"""把混合召回结果转成 writebench 的 material pack 结构。

只做格式转换：召回、排序、去重都在 index 里完成；这里保持与
``writebench.material`` 同样的 ``books/graph/text`` 形状，蒸馏模板不用改。
"""

from __future__ import annotations

from typing import Any, Mapping

from . import embed
from .index import load


def scene_text(case: Mapping[str, Any]) -> str:
    work = case.get("work") or {}
    contract = case.get("contract") or {}
    names = {str(f.get("subject_ref") or "")
             for f in case.get("facts") or []}
    names |= {str(work.get("title") or ""),
              str((case.get("chapter") or {}).get("title") or ""),
              *(str(x) for x in case.get("focus") or [])}
    parts = [str(contract.get("purpose") or ""),
             str(work.get("premise") or "")]
    parts += [str(x) for x in contract.get("must_hit_now") or []]
    text = "；".join(p for p in parts if p)
    for name in sorted((n for n in names if n), key=len, reverse=True):
        text = text.replace(name, "")
    return text


async def pack_for_case(case: Mapping[str, Any], *,
                        k: int = 24) -> dict[str, Any]:
    books = (case.get("references") or {}).get("books") or []
    if not books:
        return {"terms": [], "books": []}
    book = str(books[0].get("stable_book_id") or "")
    label = str(books[0].get("label") or book)
    ix = load()
    query = scene_text(case)
    vec = (await embed.embed_texts([query], model=ix.model))[0]
    hits = ix.hybrid(query, vec, k=k, book=book)
    graph: dict[str, list[dict[str, Any]]] = {
        "observations": [], "quotes": [], "edges": []}
    texts: list[dict[str, Any]] = []
    for idx, _score in hits:
        unit = ix.units[idx]
        if unit["kind"] == "observation":
            graph["observations"].append(
                {"value": unit["text"], "evidence_text": ""})
        elif unit["kind"] == "quote":
            graph["quotes"].append(
                {"quote_text": unit["text"], "scene_context": ""})
        else:
            texts.append({"content": unit["text"],
                          "chapter_index": unit["chapter"]})
    return {"terms": query, "books": [
        {"label": label, "graph": graph, "text": texts}]}
