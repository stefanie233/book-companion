"""检索评测：合成查询（有真值）+ 真实用例查询（看门与样例）。

合成查询的做法：从参考书里抽一段，让模型反写一句「场景检索请求」，
再看原文段能不能被自己的请求召回。这样有确定的真值，不依赖人工标注。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from writebench.llm import Chat, endpoint
from writegraph.reference import query_terms

from . import INDEX_DIR
from . import embed as embed_mod
from .index import Index
from .units import CASE_DIR, ROOT

CACHE = Path(INDEX_DIR) / "queries.json"
GEN_MODEL = ("qwen", "qwen3.8-flash")

GEN_SYSTEM = """你在为小说写作助手生成检索请求。读一段参考书摘录，\
写一句 20-40 字的中文检索请求，描述你想找的写法、氛围或细节，\
例如「克制地写一个人等另一个人开口，不直接说想念」。

硬规则：
- 不要出现人名、地名、书名、朝代等专有名词；
- 不要照抄原句，不要总结情节；
- 只输出这一句，不要解释。"""


def case_queries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(CASE_DIR.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        books = (case.get("references") or {}).get("books") or []
        if not books:
            continue
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
        scene = "；".join(p for p in parts if p)
        for name in sorted((n for n in names if n), key=len, reverse=True):
            scene = scene.replace(name, "")
        out.append({
            "case": case["id"],
            "book": str(books[0].get("stable_book_id") or ""),
            "label": str(books[0].get("label") or ""),
            "query": scene,
            "terms": query_terms(scene, exclude=names),
        })
    return out


def sample_units(ix: Index, books: set[str], *, n: int = 30,
                 seed: int = 42) -> list[int]:
    """按类型均衡抽样：观察和引文短，不能拿 chunk 的长度门槛筛。"""
    picks: list[int] = []
    per_kind = max(1, n // 3)
    for kind in ("chunk", "observation", "quote"):
        floor = 120 if kind == "chunk" else 40
        pool = [i for i, unit in enumerate(ix.units)
                if unit["book"] in books and unit["kind"] == kind
                and len(unit["text"]) >= floor]
        pool.sort(key=lambda i: hashlib.sha1(
            f"{seed}:{ix.units[i]['uid']}".encode()).hexdigest())
        picks.extend(pool[:per_kind])
    return picks[:n]


async def generate_queries(ix: Index, idxs: Sequence[int], *,
                           force: bool = False) -> dict[str, str]:
    cache: dict[str, str] = {}
    if CACHE.exists() and not force:
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    todo = [i for i in idxs if ix.units[i]["uid"] not in cache]
    if todo:
        chat = Chat(concurrency=4)

        async def one(i: int) -> tuple[str, str]:
            unit = ix.units[i]
            text = await chat.complete(
                endpoint(*GEN_MODEL), GEN_SYSTEM, unit["text"][:700],
                temperature=0.7, max_tokens=120)
            return unit["uid"], " ".join(text.split())[:160]

        for uid, query in await asyncio.gather(*(one(i) for i in todo)):
            if query:
                cache[uid] = query
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return {ix.units[i]["uid"]: cache[ix.units[i]["uid"]]
            for i in idxs if ix.units[i]["uid"] in cache}


KS = (1, 3, 5, 10)


def _ranks(ix: Index, query: str, qvec: Sequence[float], book: str,
           method: str, k: int = 10) -> list[int]:
    if method == "vector":
        return [i for i, _ in ix.vector(qvec, k, book)]
    terms = query_terms(query)
    lexical_text = " ".join(terms) if terms else query
    if method == "lexical":
        return [i for i, _ in ix.lexical(lexical_text, k, book)]
    return [i for i, _ in ix.hybrid(query, qvec, k, book,
                                    bm_text=lexical_text)]


def eval_synthetic(ix: Index, idxs: Sequence[int],
                   queries: dict[str, str],
                   qvecs: dict[str, list[float]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for method in ("lexical", "vector", "hybrid"):
        for kind in ("all", "chunk", "observation"):
            subset = [i for i in idxs
                      if kind == "all" or ix.units[i]["kind"] == kind]
            hits = {k: 0 for k in KS}
            mrr = 0.0
            for idx in subset:
                unit = ix.units[idx]
                query = queries.get(unit["uid"])
                vec = qvecs.get(unit["uid"])
                if not query or vec is None:
                    continue
                ranked = _ranks(ix, query, vec, unit["book"], method,
                                max(KS))
                rank = ranked.index(idx) + 1 if idx in ranked else None
                if rank:
                    mrr += 1.0 / rank
                for k in KS:
                    if rank and rank <= k:
                        hits[k] += 1
            total = max(1, len(subset))
            rows.append({"method": method, "kind": kind, "n": len(subset),
                         **{f"r{k}": round(hits[k] / total, 3) for k in KS},
                         "mrr": round(mrr / total, 3)})
    return rows


def _gate(ix: Index, idxs: Sequence[int], terms: Sequence[str]) -> bool:
    if len(terms) < 2:
        return False
    blob = " ".join(ix.units[i]["text"] for i in idxs)
    return sum(1 for term in terms if term in blob) >= 2


def eval_cases(ix: Index, cases: Sequence[dict[str, Any]],
               qvecs: Sequence[list[float]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case, vec in zip(cases, qvecs):
        book = case["book"]
        if not book:
            continue
        out: dict[str, Any] = {"case": case["case"], "label": case["label"],
                               "query": case["query"], "terms": case["terms"]}
        tops: dict[str, list[int]] = {}
        for method in ("lexical", "vector", "hybrid"):
            ranked = _ranks(ix, case["query"], vec, book, method, 5)
            tops[method] = ranked
            out[f"{method}_gate"] = _gate(ix, ranked, case["terms"])
        out["top1_cos"] = round(float(ix.mat[tops["vector"][0]] @ vec), 3) \
            if tops["vector"] else 0.0
        a, b = set(tops["lexical"]), set(tops["vector"])
        out["overlap"] = round(len(a & b) / max(1, len(a | b)), 3)
        out["top3"] = [
            {"uid": ix.units[i]["uid"], "kind": ix.units[i]["kind"],
             "chapter": ix.units[i]["chapter"],
             "text": ix.units[i]["text"][:110]}
            for i in tops["vector"][:3]]
        out["top3_lexical"] = [
            {"uid": ix.units[i]["uid"], "kind": ix.units[i]["kind"],
             "chapter": ix.units[i]["chapter"],
             "text": ix.units[i]["text"][:110]}
            for i in tops["lexical"][:3]]
        rows.append(out)
    return rows


JUDGE_SYSTEM = """你是小说写作的素材审稿人。下面给出一个待写场景和一段从参考书里召回的材料。
判断这段材料对写这个场景有没有用：
2 = 有可直接转化的观察、动作或物件细节；
1 = 只能借氛围、写法或句式，没有具体细节；
0 = 与场景无关，或只有设定/情节，没有可借的东西。
只输出一个数字：0、1 或 2。"""


async def judge_cases(ix: Index, cases: Sequence[dict[str, Any]],
                      qvecs: Sequence[list[float]], *, k: int = 5,
                      reps: int = 3,
                      methods: Sequence[str] = ("lexical", "vector",
                                                "hybrid")
                      ) -> list[dict[str, Any]]:
    """让模型按「可借用程度」给 top-k 材料打分，补上合成查询看不到的质量。

    单次打分抖动明显（同一材料两次能差一级），同一候选打 ``reps`` 次取均值。
    """
    import re
    chat = Chat(concurrency=6)
    tasks: list[tuple[int, str, int]] = []
    for ci, (case, vec) in enumerate(zip(cases, qvecs)):
        if not case["book"]:
            continue
        for method in methods:
            for idx in _ranks(ix, case["query"], vec, case["book"], method, k):
                tasks.append((ci, method, idx))

    async def one(ci: int, method: str, idx: int) -> tuple[int, str, int, int]:
        prompt = (f"场景：{cases[ci]['query']}\n\n"
                  f"材料：{ix.units[idx]['text'][:360]}")
        raw = await chat.complete(endpoint(*GEN_MODEL), JUDGE_SYSTEM, prompt,
                                  temperature=0.0, max_tokens=8)
        match = re.search(r"[012]", raw or "")
        return ci, method, idx, int(match.group(0)) if match else 0

    calls = [one(*task) for task in tasks for _ in range(reps)]
    results = await asyncio.gather(*calls)
    per_candidate: dict[tuple[int, str, int], list[float]] = {}
    for ci, method, idx, score in results:
        per_candidate.setdefault((ci, method, idx), []).append(score)
    buckets: dict[tuple[int, str], list[float]] = {}
    for (ci, method, _idx), scores in per_candidate.items():
        buckets.setdefault((ci, method), []).append(
            sum(scores) / len(scores))
    out: list[dict[str, Any]] = []
    for ci, case in enumerate(cases):
        if not case["book"]:
            continue
        row: dict[str, Any] = {"case": case["case"]}
        for method in methods:
            scores = buckets.get((ci, method), [])
            row[method] = {
                "mean": round(sum(scores) / max(1, len(scores)), 2),
                "usable": round(sum(1 for s in scores if s >= 1)
                                / max(1, len(scores)), 2),
                "direct": round(sum(1 for s in scores if s >= 2)
                                / max(1, len(scores)), 2),
            }
        out.append(row)
    return out


def _cos_stats(ix: Index, idxs: Sequence[int], queries: dict[str, str],
               qvecs: dict[str, list[float]]) -> dict[str, float]:
    import numpy as np
    pos = []
    for idx in idxs:
        unit = ix.units[idx]
        vec = qvecs.get(unit["uid"])
        if vec is None:
            continue
        pos.append(float(ix.mat[idx] @ np.asarray(vec, dtype=np.float32)))
    return {"pos_top1_mean": round(float(np.mean(pos)), 3),
            "pos_p10": round(float(np.percentile(pos, 10)), 3),
            "pos_min": round(float(np.min(pos)), 3)} if pos else {}


def run(*, n: int = 30, seed: int = 42, regenerate: bool = False,
        report_dir: Path | None = None, judge: bool = True) -> dict[str, Any]:
    from .index import load
    ix = load()
    books = {c["book"] for c in case_queries() if c["book"]}
    idxs = sample_units(ix, books, n=n, seed=seed)
    queries = asyncio.run(generate_queries(ix, idxs, force=regenerate))
    idxs = [i for i in idxs if ix.units[i]["uid"] in queries]
    qvec_list = asyncio.run(embed_mod.embed_texts(
        [queries[ix.units[i]["uid"]] for i in idxs], model=ix.model))
    qvecs = {ix.units[i]["uid"]: vec
             for i, vec in zip(idxs, qvec_list)}
    synthetic = eval_synthetic(ix, idxs, queries, qvecs)
    cases = case_queries()
    cvecs = asyncio.run(embed_mod.embed_texts(
        [c["query"] for c in cases], model=ix.model))
    case_rows = eval_cases(ix, cases, cvecs)
    judge_rows = (asyncio.run(judge_cases(ix, cases, cvecs))
                  if judge else [])
    result = {"index": {"units": len(ix.units), "model": ix.model},
              "synthetic_n": len(idxs),
              "synthetic": synthetic,
              "cos": _cos_stats(ix, idxs, queries, qvecs),
              "cases": case_rows,
              "case_judge": judge_rows}
    out_dir = Path(report_dir or INDEX_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "report.md").write_text(
        _markdown(result, queries, ix, idxs), encoding="utf-8")
    return result


def _markdown(result: dict[str, Any], queries: dict[str, str], ix: Index,
              idxs: Sequence[int]) -> str:
    lines = ["# 参考素材召回评测", "",
             f"索引：{result['index']['units']} 单元，"
             f"{result['index']['model']}", "",
             f"## 合成查询（n={result['synthetic_n']}）", "",
             "| 方法 | 单元 | n | R@1 | R@3 | R@5 | R@10 | MRR |",
             "|---|---|---|---|---|---|---|---|"]
    for row in result["synthetic"]:
        lines.append(f"| {row['method']} | {row['kind']} | {row['n']} | "
                     f"{row['r1']} | {row['r3']} | {row['r5']} | "
                     f"{row['r10']} | {row['mrr']} |")
    lines += ["", f"正样本 top1 余弦：{result['cos']}", "",
              "## 真实用例", "",
              "| 用例 | 参考书 | lexical 门 | vector 门 | hybrid 门 | "
              "top1 cos | 词/向量重合 |",
              "|---|---|---|---|---|---|---|"]
    for row in result["cases"]:
        lines.append(
            f"| {row['case']} | {row['label']} | {row['lexical_gate']} | "
            f"{row['vector_gate']} | {row['hybrid_gate']} | "
            f"{row['top1_cos']} | {row['overlap']} |")
    if result.get("case_judge"):
        lines += ["", "## 真实用例材料质量（LLM 0–2）", "",
                  "| 用例 | 词法 mean/可用/直接 | 向量 mean/可用/直接 | "
                  "混合 mean/可用/直接 |",
                  "|---|---|---|---|"]
        for row in result["case_judge"]:
            cells = []
            for method in ("lexical", "vector", "hybrid"):
                item = row[method]
                cells.append(f"{item['mean']}/{item['usable']}/"
                             f"{item['direct']}")
            lines.append(f"| {row['case']} | " + " | ".join(cells) + " |")
    lines += ["", "## 样例（词法 vs 向量 top3）", ""]
    for row in result["cases"]:
        lines.append(f"### {row['case']} — {row['query'][:60]}")
        lines.append("词法：")
        for item in row.get("top3_lexical") or []:
            lines.append(f"- [{item['kind']} 第{item['chapter']}章] "
                         f"{item['text']}")
        lines.append("向量：")
        for item in row["top3"]:
            lines.append(f"- [{item['kind']} 第{item['chapter']}章] "
                         f"{item['text']}")
        lines.append("")
    lines += ["## 合成查询样例", ""]
    for idx in idxs[:12]:
        unit = ix.units[idx]
        lines.append(f"- 原段[{unit['kind']}] {unit['text'][:70]}")
        lines.append(f"  查询：{queries.get(unit['uid'], '')}")
    return "\n".join(lines) + "\n"
