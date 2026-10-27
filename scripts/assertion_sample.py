"""抽取一份**可裁决的断言样本**，用来量化提取质量。

为什么需要它：机械体检、证伪器、未验证率都只能回答「这批有没有被挑出
问题」，回答不了「图谱里的断言到底对不对」。而且证伪器与提取器是同一个
模型 —— 拿它当分数是循环论证。质量的分母必须是**人工裁决过的样本**。

用法：

    # 抽 40 条（分层：边 / 观察 / 引文），生成审阅表
    python scripts/assertion_sample.py --book <book_id> --n 40

    # 裁决完（把 review.json 里每条 verdict 填上）后算分
    python scripts/assertion_sample.py --score c:/tmp/assertion-review-xxxx.json

裁决取值：
    ok            断言与正文一致（含合理的概括，但不改事实）
    overstrong    有据，但把「猜测/看法/借口」写成了事实（event_type 用错）
    wrong_agent   主体或对象错位（谁对谁做了什么记错）
    unsupported   正文里找不到依据 / 编造
    merged        把两个场景、两句不同的话、两条因果并成一条
    bad_entity    关系本身没错，但端点不是一个实体（如「拉布吕耶尔的这段文字」）

指标：
    precision = ok / n
    critical  = (wrong_agent + unsupported + merged) / n   ← 会误导读者的错
    overstrong 单列：它不篡改事实，但污染「事实」这个字段。
    bad_entity 单列：图谱的噪声，不误导读者，但会让检索与角色内核变脏。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WS = re.compile(r"\s+")


def resolve_path(book_id: str) -> Path:
    """只读解析：稳定 id 文件存在就用它，否则用 book_id 命名。

    不调用 ``resolve_graph_db_path`` —— 那个函数会在需要时**改名迁移**，
    而抽样是只读操作，不该改盘上的东西。
    """
    from config import Config
    from storage.db import BookDB
    cfg = Config()
    row = BookDB(cfg.books_db_path).get_import_log_by_book(book_id)
    if row and row.stable_book_id:
        p = cfg.graph_db_path(row.stable_book_id)
        if p.exists():
            return p
    return cfg.graph_db_path(book_id)


def _norm(text: str) -> str:
    return WS.sub("", text or "")


def load(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def chunk_texts(book_id: str, ids: set[str]) -> dict[str, str]:
    db = sqlite3.connect(f"file:{(ROOT / 'data' / 'books.db').as_posix()}?mode=ro", uri=True)
    if not ids:
        return {}
    rows = db.execute(
        "SELECT id, content FROM book_chunks WHERE book_id=? AND id IN (%s)"
        % ",".join("?" * len(ids)), [book_id, *sorted(ids)]).fetchall()
    db.close()
    return {r[0]: r[1] or "" for r in rows}


def window(text: str, evidence: str, width: int = 260) -> str:
    """evidence 前后各截一段 —— 裁决要看得出「这句话放在什么语境里」。"""
    if not text:
        return ""
    ev = _norm(evidence)
    hay = _norm(text)
    if not ev:
        return text[:width * 2]
    # 原文里按空白归一化过，回不到精确下标；用未归一化的原文找原文串
    idx = text.find(evidence) if evidence else -1
    if idx < 0:
        compact = re.sub(r"\s+", "", text)
        pos = compact.find(ev)
        if pos < 0:
            return text[:width * 2]
        # 归一化坐标 ≈ 字符坐标，够用
        idx = min(len(text) - 1, pos)
    return text[max(0, idx - width): idx + len(evidence) + width]


def draw(graph: Path, book_id: str, n: int, seed: int) -> dict:
    conn = load(graph)
    items: list[dict] = []
    edge_rows = conn.execute(
        "SELECT e.id, e.source_id, e.target_id, e.kind, e.event_type, e.subject, "
        "e.chunk_id, e.evidence_text, "
        "(SELECT canonical_name FROM nodes WHERE id=e.source_id), "
        "(SELECT canonical_name FROM nodes WHERE id=e.target_id) "
        "FROM edges e WHERE e.project_id=?", (book_id,)).fetchall()
    obs_rows = conn.execute(
        "SELECT o.id, o.node_id, o.field, o.value, o.event_type, o.subject, "
        "o.evidence_text, o.anchor_chapter, "
        "(SELECT canonical_name FROM nodes WHERE id=o.node_id) "
        "FROM graph_observations o WHERE o.project_id=?", (book_id,)).fetchall()
    quote_rows = conn.execute(
        "SELECT id, character_name, quote_text, chunk_id, scene_context "
        "FROM graph_quotes WHERE project_id=?", (book_id,)).fetchall()
    conn.close()

    for r in edge_rows:
        items.append({"type": "edge", "id": r[0],
                      "claim": f"{r[8]} —{r[3]}→ {r[9]}（{r[4]}）",
                      "evidence": r[7] or "", "chunk_id": r[6] or ""})
    for r in obs_rows:
        # 观察没有 chunk_id，只有 anchor_chapter；块靠 evidence 反查
        # （见 _chunk_for_evidence），查不到就留空 —— 裁决表里会显示
        # 「无定位」，那本身也是一个信号。
        items.append({"type": "observation", "id": r[0],
                      "claim": f"{r[8]} · {r[2]} = {r[3]}（{r[4]}）",
                      "evidence": r[6] or "", "chunk_id": "",
                      "anchor_chapter": r[7]})
    for r in quote_rows:
        items.append({"type": "quote", "id": r[0],
                      "claim": f"台词：{r[1]}「{r[2]}」",
                      "evidence": r[2] or "", "chunk_id": r[3] or ""})

    rng = random.Random(seed)
    # 分层：每种类型至少 8 条（不够就全取），其余按比例填满 n。
    by_type: dict[str, list[dict]] = {}
    for it in items:
        by_type.setdefault(it["type"], []).append(it)
    picked: list[dict] = []
    for t in sorted(by_type):
        bucket = by_type[t]
        rng.shuffle(bucket)
        picked += bucket[: min(8, len(bucket))]
        bucket[:] = bucket[min(8, len(bucket)):]
    rest = [it for t in sorted(by_type) for it in by_type[t]]
    rng.shuffle(rest)
    picked += rest[: max(0, n - len(picked))]
    rng.shuffle(picked)

    # 观察靠 evidence 反查块：取证据前 20 个非空白字符做 LIKE。
    missing = [it for it in picked if not it["chunk_id"] and it["evidence"]]
    if missing:
        db = sqlite3.connect(
            f"file:{(ROOT / 'data' / 'books.db').as_posix()}?mode=ro", uri=True)
        for it in missing:
            probe = _norm(it["evidence"])[:20]
            if not probe:
                continue
            row = db.execute(
                "SELECT id FROM book_chunks WHERE book_id=? AND "
                "replace(replace(content, char(10), ''), ' ', '') LIKE ? LIMIT 1",
                (book_id, f"%{probe}%")).fetchone()
            if row:
                it["chunk_id"] = row[0]
        db.close()

    texts = chunk_texts(book_id, {it["chunk_id"] for it in picked if it["chunk_id"]})
    for it in picked:
        full = texts.get(it["chunk_id"], "")
        it["context"] = window(full, it["evidence"])
        it["evidence_in_source"] = bool(it["evidence"]) and _norm(it["evidence"]) in _norm(full)
        it["verdict"] = None

    return {"book_id": book_id, "graph": str(graph), "seed": seed,
            "n": len(picked), "items": picked}


def score(payload: dict) -> int:
    items = payload.get("items") or []
    done = [it for it in items if it.get("verdict")]
    if not done:
        print("还没有裁决结果。")
        return 1
    counts = {"ok": 0, "overstrong": 0, "wrong_agent": 0,
              "unsupported": 0, "merged": 0, "bad_entity": 0}
    by_type: dict[str, list[str]] = {}
    for it in done:
        v = it["verdict"]
        if v not in counts:
            print(f"未知 verdict: {v}")
            return 2
        counts[v] += 1
        by_type.setdefault(it["type"], []).append(v)
    n = len(done)
    critical = counts["wrong_agent"] + counts["unsupported"] + counts["merged"]
    print(f"已裁决 {n}/{len(items)} 条")
    print(f"  precision = {counts['ok'] / n:.1%}")
    print(f"  critical  = {critical / n:.1%}  "
          f"(主体错位 {counts['wrong_agent']} · 无据 {counts['unsupported']} · "
          f"场景合并 {counts['merged']})")
    print(f"  overstrong= {counts['overstrong'] / n:.1%}")
    print(f"  bad_entity= {counts['bad_entity'] / n:.1%}")
    for t, vs in sorted(by_type.items()):
        ok = sum(v == "ok" for v in vs)
        print(f"  {t}: {ok}/{len(vs)} ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", help="book_id")
    ap.add_argument("--graph", help="graph db path（默认按 book_id 解析）")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20251021)
    ap.add_argument("--out", default="")
    ap.add_argument("--score", default="")
    args = ap.parse_args()

    if args.score:
        payload = json.loads(Path(args.score).read_text("utf-8"))
        return score(payload)

    if not args.book:
        ap.error("--book 或 --score 二选一")
    graph = Path(args.graph) if args.graph else resolve_path(args.book)
    payload = draw(graph, args.book, args.n, args.seed)
    out = Path(args.out) if args.out else Path(f"c:/tmp/assertion-review-{args.book}.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"样本 {payload['n']} 条 -> {out}")
    print(f"其中 evidence 能在源块里逐字找到: "
          f"{sum(it['evidence_in_source'] for it in payload['items'])}/{payload['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
