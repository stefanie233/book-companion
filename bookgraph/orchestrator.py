"""Orchestrator — loop_extract: drives Scout + Explorer + Recorder + progress events.

This is the main entry point for book graph extraction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import aiohttp

from bookgraph.chunk_summary import missing_ids, summaries_from_result
from bookgraph.context_setter import run_context_setter
from bookgraph.explorer import build_explorer_prompt, run_explorer
from bookgraph.llm_call import output_budget
from bookgraph.logging import log_llm_call
from bookgraph.recorder import record_result, RecordStats
from bookgraph.invariants import CORE_KINDS
from bookgraph.scope import plan_scopes, plan_to_meta, scope_brief, top_level_units
from bookgraph.selection import select_chunks
from bookgraph.head_scan import run_head_scan
from bookgraph.scout import run_scout
from provider_store import config_for
from bookgraph.adjudicate import (
    apply_fixes, evidence_flags, mechanical_fixes, run_adjudicator,
    verdict_summary)
from bookgraph.skeleton import (
    group_chapters, normalize_skeleton, record_skeleton, run_skeleton)
from bookgraph.verify import (
    challenge_lines, check_candidate, round_rank, run_verifier)
from config import Config
from services.chunk_annotation import annotate_chunks, form_map
from storage.db import BookDB
from storage.graph_db import GraphDB

log = logging.getLogger("bookgraph")


@dataclass
class ExtractionResult:
    """Final result of a full extraction run."""
    book_id: str
    book_title: str
    total_chunks: int
    chunks_done: int
    chunks_failed: int
    final_stats: dict
    book_context: dict | None = None
    batch_stats: list[dict] = field(default_factory=list)
    scout_plan: dict | None = None


# ── Progress callback ─────────────────────────────────────────────────────

ProgressCallback = Callable[[dict], None]
"""Called with a progress event dict on each batch step.

Event shape:
    {
        "type": "batch_start" | "batch_done" | "batch_failed" | "context_set"
              | "scout_done" | "complete",
        "book_id": str,
        "batch_id": str,
        "batch_index": int,
        "total_batches": int,
        "done_chunks": int,
        "total_chunks": int,
        "step_label": str | None,
        "detail": dict | None,
    }
"""

# Scout auto-activation threshold: skip scout for books with fewer chunks
SCOUT_MIN_CHUNKS = 50

# How many Explorer LLM calls may run concurrently. Only the LLM calls are
# concurrent — all graph_db writes happen serially in the reaping loop below,
# which keeps the single SQLite connection free of interleaving.
#
# 可用环境变量压到 1–2：火山的网关有一条 **burst 增长** 保护，3 并发连着
# 打会成片 429（实测一趟 44 次，末端两个批次直接失败、丢掉 10 块正文）。
# 并发只影响墙钟，不影响产物 —— 限流严重时降并发比加大重试更省事。
BATCH_CONCURRENCY = max(1, int(os.environ.get("BC_GRAPH_CONCURRENCY", "3")))


def _split_by_scope(seq: list, by_chunk: dict, size: int | None) -> list[list]:
    """把一串连续块切成批次，**遇到叙事域边界就断开**。

    *size* 为 None 时只在域边界断开（Scout 已经定过批次大小）；
    否则域内再按 size 分批。``by_chunk`` 为空（单域/未划域）时行为与
    改动前逐字一致 —— 长篇不受影响。
    """
    out: list[list] = []
    cur: list = []
    cur_sid = None
    for ch in seq:
        sid = by_chunk.get(ch.id, "")
        if cur and (sid != cur_sid or (size and len(cur) >= size)):
            out.append(cur)
            cur = []
        cur.append(ch)
        cur_sid = sid
    if cur:
        out.append(cur)
    return out


async def _run_batch_explorer(
    sem: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    cfg: Config,
    graph_db: GraphDB,
    book_id: str,
    book_title: str,
    book_author: str,
    run_id: str,
    batch_id: str,
    batch_index: int,
    chunk_dicts: list[dict],
    total: int,
    system_prompt: str,
    jsonl_path: Path,
    extraction_focus: str = "",
    scope_brief_text: str = "",
    *,
    verify: bool = True,
    max_rounds: int = 2,
    admitted: set[str] | None = None,
    repair_mode: str = "adjudicate",
    book_text: str = "",
) -> tuple[int, dict | None, str | None, dict]:
    """跑一批的提取与修补。

    ``repair_mode="adjudicate"``（默认）：提取一次 → 机械修补 → 裁决趟对
    **带编号的断言**下判决 → 机械执行 → 再体检。**不重提**。
    ``repair_mode="rework"``：旧回炉环（提取 → 体检 + 证伪 → 带缺陷清单重提，
    保留「最好的一轮」），留作回退与 A/B。

    Returns ``(batch_index, result, error_type, verdict)``。``verdict`` 里有
    轮数、残余缺陷数、裁决/证伪结论 —— **未经验证的那一批必须看得出来**。

    信号量在整个环上持有，所以后开始的批次仍能看见前面已提交的节点
    （近似滑动窗口）。每次 LLM 调用都经 ``log_llm_call`` 落库。
    """
    async with sem:
        done_count = graph_db.done_count(book_id)
        chunk_ids = [d["chunk_id"] for d in chunk_dicts]
        chunk_text = {d["chunk_id"]: d.get("content") or "" for d in chunk_dicts}
        # 简述按块累计、跨轮保留：采纳的那一轮（可能是第 1 轮）漏写某几块时，
        # 别让整批的简述白丢。后一轮覆盖前一轮 —— 改过的通常更准。
        collected_summaries: dict[str, str] = {}

        def _with_summaries(payload: dict) -> dict:
            if collected_summaries:
                payload["chunk_summaries"] = dict(collected_summaries)
            return payload

        _phase_route = {
            "explorer": "bookgraph.explorer",
            "extract_verifier": "bookgraph.verify",
            "adjudicate": "bookgraph.adjudicate",
            "adjudicate_retry": "bookgraph.adjudicate",
        }

        def _log(phase: str):
            # Record the model that actually served this call, not the base
            # one — a route binding may point this phase elsewhere.
            model = config_for(cfg, _phase_route[phase]).model \
                if phase in _phase_route else cfg.model

            def _cb(fields: dict) -> None:
                log_llm_call(
                    graph_db, jsonl_path,
                    book_id=book_id, book_title=book_title, run_id=run_id,
                    phase=phase, batch_id=batch_id, batch_index=batch_index,
                    chunk_ids=chunk_ids, total_chunks=total, model=model,
                    scout_focus=extraction_focus, **fields,
                )
            return _cb

        # 证伪趟的名册：这个叙事域里**已有**的实体。只给名字 —— 用来判
        # 「这是不是同一个人」，不给关系也不给推理。
        # 库里已有的名字（含别名）—— 体检时用来分辨「引用了旧实体」（正常）
        # 和「真悬空」（要回炉）。少了它会把前者全误报成缺陷。
        try:
            known_names = graph_db.known_names(book_id)
        except Exception:
            log.warning("已有节点名查询失败（悬空引用判定会偏严）", exc_info=True)
            known_names = set()

        # 名册也**不按域过滤**：它的用途就是判「这是不是同一个人」，而
        # 复现实体恰恰是跨篇的 —— 把别的篇藏起来正好让它判不了。
        roster: list[str] = []
        try:
            idx = graph_db.get_node_index_two_tier(
                book_id, {d["chapter_index"] for d in chunk_dicts
                          if "chapter_index" in d},
                compact_cap=60)
            roster = [n["canonical_name"] for n in (idx.get("compact") or [])]
        except Exception:
            log.warning("证伪名册查询失败（不影响提取）", exc_info=True)

        verdict: dict = {"rounds": 0, "verified": False, "verdict":
                         "skipped" if not verify else "?",
                         "mechanical": 0, "challenges": 0, "residual": 0,
                         # 逐轮台账。没有它，「回炉到底在收敛还是空转」「采纳的
                         # 是哪一轮」都只能靠重放日志猜（而且会猜错 —— 真正的
                         # 评分含证伪质疑数，不只是机械缺陷数）。
                         "history": []}

        # ── 裁决模式：一次提取 + 一次裁决 + 机械修补，不重提 ─────────────
        # 旧回炉环实测不收敛（边逐字稳定率 19%，观察 1%），而且它是拿「重提
        # 整批」去修「某一条断言的错」，把 81% 正确的内容一起扔了。这里改成
        # 对编号下判决：能改一个字段就不删整条，能不动的绝不重写。
        if repair_mode == "adjudicate":
            result, error_type = await run_explorer(
                session, cfg, graph_db, book_id, book_title, book_author,
                chunk_dicts, total, done_count, system_prompt,
                log_cb=_log("explorer"),
                scope_brief=scope_brief_text, feedback=[],
            )
            if result is None:
                return batch_index, None, error_type, verdict
            collected_summaries.update(
                summaries_from_result(result, chunk_ids))

            # 机械能判的先判掉：别名无据、锚点出批、同批重复。
            result, auto_ledger = mechanical_fixes(
                result, chunk_ids, chunk_text, book_text)
            report = check_candidate(result, chunk_ids, chunk_text, admitted,
                                     known=known_names, book_text=book_text)

            adjudicated: dict | None = None
            ledger: list[dict] = []
            flags: dict[str, str] = {}
            if verify:
                # 机械体检的结果要**一起送过去**。逐字校验早就发现「证据
                # 不在这块正文里」，但那只是一条非阻塞的 finding —— 裁决者
                # 看不到它，就只能靠自己逐字比对五块正文，而那件事它做不好。
                flags = evidence_flags(result, chunk_text)
                adjudicated = await run_adjudicator(
                    session, cfg, book_title,
                    [(cid, chunk_text.get(cid, "")) for cid in chunk_ids],
                    result, roster, log_cb=_log("adjudicate"), flags=flags,
                )
                if adjudicated:
                    before = len(result.get("new_edges") or [])
                    result, ledger = apply_fixes(
                        result, adjudicated.get("fixes") or [], set(chunk_ids))
                    after = len(result.get("new_edges") or [])
                    if before and after < before * 0.5:
                        log.warning(
                            "Batch %d: 裁决删掉了 %d/%d 条边 —— 超过一半，"
                            "这多半是裁决失控而不是提取差",
                            batch_index, before - after, before)
                    report = check_candidate(result, chunk_ids, chunk_text,
                                             admitted, known=known_names,
                                             book_text=book_text)

                    # **被拒的修改要回灌一次。** 裁决者只有一轮时，它永远
                    # 不知道自己的修改没落地 —— 实测 14 条里 5 条因
                    # `empty_patch` 被拒，原因全是「只写了 why、没给 patch」，
                    # 而下一批它还会照原样犯。这里把驳回原因连同**当前**
                    # 断言表再送一次：编号会变（前面的条目可能已被删），
                    # 所以只给「上一条为什么被拒」，不给旧编号。
                    rejected = [e for e in ledger if not e["applied"]]
                    if rejected:
                        retry_flags = evidence_flags(result, chunk_text)
                        retry = await run_adjudicator(
                            session, cfg, book_title,
                            [(cid, chunk_text.get(cid, ""))
                             for cid in chunk_ids],
                            result, roster, log_cb=_log("adjudicate_retry"),
                            flags=retry_flags, rejected=rejected,
                        )
                        if retry:
                            fixed, ledger2 = apply_fixes(
                                result, retry.get("fixes") or [],
                                set(chunk_ids))
                            if ledger2:
                                result = fixed
                                ledger.extend(ledger2)
                                report = check_candidate(
                                    result, chunk_ids, chunk_text, admitted,
                                    known=known_names, book_text=book_text)
                            verdict["retry_fixes_applied"] = sum(
                                1 for e in ledger2 if e["applied"])
                            verdict["retry_fixes_rejected"] = [
                                e for e in ledger2 if not e["applied"]][:10]

            blocking = [f for f in report.findings if f.blocking]
            summary = verdict_summary(adjudicated, ledger)
            verdict.update({
                "rounds": 1,
                # 沉默不是成功：裁决趟没吐出可解析结论 = 未经验证。
                "verified": bool(adjudicated) and not blocking,
                "verdict": summary["verdict"] if verify else "skipped",
                "mechanical": len(blocking),
                "challenges": summary["fixes_proposed"],
                "residual": len(blocking),
                "advisory": len(report.findings) - len(blocking),
                "auto_fixes": len(auto_ledger),
                # 机械体检捞出来的可疑条数。它是**裁决的输入**，不是结论 ——
                # 记下来才能分清「这批判得干净」和「这批根本没送可疑项」。
                "machine_flags": len(flags),
                "fixes_applied": summary["fixes_applied"],
                "by_action": summary["by_action"],
                # 被拒收的修改要看得见 —— 否则「裁决提了 5 条只落了 1 条」
                # 只能靠重放日志猜（实测 4 条 rewrite 因为字段名不同被静默丢弃）。
                "fix_rejected": [e for e in ledger if not e["applied"]][:20],
            })
            verdict["history"].append({
                "round": 1, "auto_fixes": len(auto_ledger),
                "fixes_proposed": summary["fixes_proposed"],
                "fixes_applied": summary["fixes_applied"],
                "by_action": summary["by_action"],
                "mechanical": len(blocking),
                "codes": sorted({f.code for f in report.findings}),
                "nodes": len(result.get("new_nodes") or []),
                "edges": len(result.get("new_edges") or []),
                "verifier": verdict["verdict"],
            })
            return batch_index, _with_summaries(result), None, verdict

        feedback: list[str] = []
        best_result: dict | None = None
        best_rank: tuple | None = None
        best_round = 0

        for rnd in range(1, max_rounds + 1):
            result, error_type = await run_explorer(
                session, cfg, graph_db, book_id, book_title, book_author,
                chunk_dicts, total, done_count, system_prompt,
                log_cb=_log("explorer"),
                scope_brief=scope_brief_text, feedback=feedback,
            )
            if result is None:
                # 回炉轮反而崩了 → 用上一轮那份好的，别把整批丢掉
                if best_result is not None:
                    log.warning("Batch %d: 第 %d 轮回炉失败(%s)，保留上一轮结果",
                                batch_index, rnd, error_type)
                    break
                return batch_index, None, error_type, verdict

            collected_summaries.update(
                summaries_from_result(result, chunk_ids))

            report = check_candidate(result, chunk_ids, chunk_text, admitted,
                                     known=known_names, book_text=book_text)
            v = await run_verifier(
                session, cfg, book_title,
                [(cid, chunk_text.get(cid, "")) for cid in chunk_ids],
                result, roster, log_cb=_log("extract_verifier"),
            ) if verify else None
            v_lines = challenge_lines(v)
            verifier_ok = (v is not None)

            # 反馈**全给**（提示项也写进去，提取器学得到）；但**只有阻塞项**
            # 决定要不要再来一轮。为「写库时就会被归一掉的东西」烧一次完整
            # 重提取，是这套系统里最贵的一笔冤枉钱。
            blocking = [f for f in report.findings if f.blocking]
            feedback = [f.render() for f in report.findings] + v_lines
            trigger = [f.render() for f in blocking] + v_lines
            score = len(feedback)
            rank = round_rank(len(blocking), len(v_lines),
                              len(result.get("new_nodes") or []),
                              len(result.get("new_edges") or []))
            verdict.update({
                "rounds": rnd, "mechanical": len(blocking),
                "challenges": len(v_lines),
                "advisory": len(report.findings) - len(blocking),
                "verdict": (str(v.get("verdict") or "?") if v
                            else ("skipped" if not verify else "inconclusive")),
            })
            verdict["history"].append({
                "round": rnd, "score": score, "rank": list(rank),
                "mechanical": len(blocking), "challenges": len(v_lines),
                "advisory": len(report.findings) - len(blocking),
                "codes": sorted({f.code for f in report.findings}),
                "nodes": len(result.get("new_nodes") or []),
                "edges": len(result.get("new_edges") or []),
                # 逐轮记证伪结论。只记最后一轮会让台账说谎 —— 采纳的可能是
                # 第 2 轮，而最后一轮（第 3 轮）恰好 upheld，于是事件里出现
                # 「未经验证（upheld）」这种自相矛盾的行。
                "verifier": (str(v.get("verdict") or "?") if v
                             else ("skipped" if not verify else "inconclusive")),
            })
            if best_rank is None or rank < best_rank:
                best_rank, best_result, best_round = rank, result, rnd
            if not trigger:
                # 收敛 = 没有阻塞项 **且** 证伪趟确实说了话。证伪趟哑了就是
                # 未经验证，不能冒充收敛。
                verdict["verified"] = verifier_ok
                verdict["best_round"] = rnd
                return batch_index, _with_summaries(best_result), None, verdict

        verdict["residual"] = (best_rank[0] + best_rank[1]) if best_rank else 0
        verdict["best_round"] = best_round
        verdict["verified"] = False
        # 结论取**采纳那一轮**的，不取最后一轮的 —— 否则「upheld」会和
        # 「未经验证」并排出现，读的人没法判断到底信谁。
        for h in verdict["history"]:
            if h["round"] == best_round:
                verdict["verdict"] = h["verifier"]
                verdict["challenges"] = h["challenges"]
                verdict["mechanical"] = h["mechanical"]
                break
        return batch_index, _with_summaries(best_result), None, verdict


# ── Main orchestrator ─────────────────────────────────────────────────────

class GraphOrchestrator:
    """Drives the Scout + Explorer + Recorder loop for one book.

    Usage:
        orch = GraphOrchestrator(config, books_db, graph_db)
        result = await orch.run(book_id, batch_size=5, progress_cb=callback)
    """

    def __init__(
        self,
        config: Config,
        books_db: BookDB,
        graph_db: GraphDB,
        graph_db_path=None,
    ):
        self.config = config
        self.books_db = books_db
        self.graph_db = graph_db
        self.graph_db_path = graph_db_path
        self._jsonl_path = Path(config.project_dir) / "data" / "logs" / "bookgraph.jsonl"

    async def run(
        self,
        book_id: str,
        batch_size: int = 5,
        progress_cb: ProgressCallback | None = None,
        use_scout: bool = True,
        verify: bool = True,
        verify_rounds: int = 3,
        repair_mode: str = "adjudicate",
        run_skeleton_pass: bool = True,
    ) -> ExtractionResult:
        """Run full extraction loop for a book.

        Args:
            book_id: Book identifier.
            batch_size: Default batch size for mechanical fallback (ignored
                        when scout produces a batch plan).
            progress_cb: Optional callback for SSE progress events.
            use_scout: Whether to run the scout phase (Phase 0) before
                       extraction.  Auto-disabled for books with < 50 chunks.
            verify: 开裁决/证伪趟。关掉它 = 「一次提取 + 机械修补」直接落库。
            verify_rounds: 旧回炉模式的轮数上限（每轮 = 一次提取 + 一次证伪）。
                ``repair_mode="adjudicate"`` 时无意义（永远只跑一轮）。
            repair_mode: ``"adjudicate"``（默认）= 一次提取 + 对编号下判决 +
                机械修补；``"rework"`` = 旧回炉环（重提整批），保留用于
                A/B 与回退。
            run_skeleton_pass: 提取前先跑一趟「骨架」（实体 + 结构关系）。
                它是全局的：同一人跨场景才认得出，亲缘结构才拼得全。失败时
                退回逐批提取并出声。

        Resumable: skips chunks already marked done.
        """
        # ── 1. Load book metadata + chunks ────────────────────────────────
        book = self.books_db.get_book(book_id)
        if book is None:
            raise ValueError(f"Book not found: {book_id}")

        all_chunks = self.books_db.get_chunks(book_id)
        if not all_chunks:
            raise ValueError(f"Book has no chunks: {book_id}")

        # Load chapter titles
        chapters = self.books_db.get_chapters(book_id)
        chapter_titles: dict[int, str] = {
            ch.chapter_index: (ch.title or f"第{ch.chapter_index + 1}章")
            for ch in chapters
        }

        total = len(all_chunks)
        # 全书正文：别名准入要用它（「全书没这个写法」= 外部知识，不是别名）。
        book_text = "\n".join((ch.content or "") for ch in all_chunks)

        # ── 1b. Run identity ─────────────────────────────────────────────
        # A run_id scopes every progress row + LLM call log for this run.
        # The SSE endpoint uses the run_start row as its replay anchor, so
        # this must be the FIRST progress row written each run.
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.graph_db.record_event(book_id, "run_start",
            step_label=f"提取开始 · {run_id}",
            detail=json.dumps({"run_id": run_id, "total_chunks": total},
                              ensure_ascii=False),
            run_id=run_id)
        self._emit(progress_cb, {
            "type": "run_start",
            "book_id": book_id,
            "run_id": run_id,
            "total_chunks": total,
        })

        # ── 2. 锚点重连 + 登记块 ──────────────────────────────────────────
        # 重导会重新生成 book_chunks.id，而图谱的边/引文/观察/节点首现锚点都
        # 指着旧 id（实测 9 本里 8 本交集为 0）。**必须在 register_chunk 之前
        # 重连** —— 重指之后 is_chunk_done(新 id) 才为真，跨重导的续跑才真正
        # 正确；晚了就是把旧行当分母、把已提过的块再提一遍。
        current = [{
            "chunk_id": ch.id,
            "chapter_index": ch.chapter_index,
            "chunk_index": ch.chunk_index,
            "content_hash": hashlib.sha256(
                (ch.content or "").encode("utf-8")).hexdigest()[:16],
        } for ch in all_chunks]
        try:
            rl = self.graph_db.relink_chunks(book_id, current)
            if rl.relinked or rl.ambiguous or rl.unresolved or rl.status_kept:
                log.info("Relink: %s", rl.summary())
                self.graph_db.record_event(
                    book_id, "relink",
                    step_label=f"锚点重连 · {rl.resolved}/{rl.total}",
                    detail=json.dumps({
                        "resolved": rl.resolved, "total": rl.total,
                        "by_hash": rl.by_hash, "by_coord": rl.by_coord,
                        "merged": rl.merged,
                        "ambiguous": len(rl.ambiguous),
                        "unresolved": len(rl.unresolved),
                        "nodes_relinked": rl.nodes_relinked,
                        "obs_reanchored": rl.obs_reanchored,
                    }, ensure_ascii=False), run_id=run_id)
                self._emit(progress_cb, {
                    "type": "relink", "book_id": book_id, "run_id": run_id,
                    "resolved": rl.resolved, "total": rl.total,
                    "unresolved": len(rl.unresolved),
                })
        except Exception:
            log.warning("锚点重连失败（不影响本轮提取）", exc_info=True)

        for c in current:
            self.graph_db.register_chunk(
                c["chunk_id"], book_id, c["chapter_index"],
                c["chunk_index"], c["content_hash"],
            )

        # ── 3. Init chapter nodes ─────────────────────────────────────────
        chapter_list = [
            (idx, chapter_titles.get(idx, f"第{idx + 1}章"))
            for idx in sorted(chapter_titles.keys())
        ]
        chapter_node_ids = self.graph_db.init_chapter_nodes(book_id, chapter_list)

        already_done = self.graph_db.done_count(book_id)
        if already_done > 0:
            log.info("Resume: %d/%d chunks already done", already_done, total)
            self._emit(progress_cb, {
                "type": "resume",
                "book_id": book_id,
                "run_id": run_id,
                "done_chunks": already_done,
                "total_chunks": total,
            })
            self.graph_db.record_event(book_id, "resume",
                step_label=f"续接: {already_done}/{total} chunks 已完成",
                detail=json.dumps({"done_chunks": already_done, "total_chunks": total},
                                  ensure_ascii=False),
                run_id=run_id)

        # ── 4. ContextSetter (cached) ─────────────────────────────────────
        book_context = None
        cached_ctx = self.graph_db.get_meta("book_context")
        if cached_ctx:
            try:
                book_context = json.loads(cached_ctx)
                if "narrative_structure" not in (book_context or {}):
                    # 老缓存（本次改动之前落的）：它连目录都没看过，判不出
                    # 「一个世界还是几个世界」。当未命中重跑一次 —— 一次调用，
                    # 顺带把目录喂给它，它的**全部**输出都变准（原来只拿得到
                    # 书名+作者）。判回 single 时下游行为与改动前一字不差。
                    log.info("ContextSetter: 老缓存缺 narrative_structure，重跑")
                    cached_ctx, book_context = None, None
                else:
                    log.info("ContextSetter: cache hit")
            except json.JSONDecodeError:
                cached_ctx = None

        if not cached_ctx:
            ctx_cfg = config_for(self.config, "bookgraph.context_setter")
            async with aiohttp.ClientSession() as session:
                def _log_ctx(fields: dict) -> None:
                    log_llm_call(
                        self.graph_db, self._jsonl_path,
                        book_id=book_id, book_title=book.title, run_id=run_id,
                        phase="context", total_chunks=total,
                        model=ctx_cfg.model,
                        **fields,
                    )
                book_context = await run_context_setter(
                    session, ctx_cfg.api_key, ctx_cfg.api_base,
                    ctx_cfg.model, book.title, book.author or "",
                    log_cb=_log_ctx,
                    unit_titles=[u["title"] for u in top_level_units(chapters)],
                    max_tokens=output_budget(self.config),
                )
            if book_context is not None:
                self.graph_db.set_meta(
                    "book_context", json.dumps(book_context, ensure_ascii=False)
                )

        if book_context:
            self._emit(progress_cb, {
                "type": "context_set",
                "book_id": book_id,
                "run_id": run_id,
                "genre": book_context.get("genre"),
                "suggested_kinds": book_context.get("suggested_kinds", []),
            })
            self.graph_db.record_event(book_id, "context_set",
                step_label=f"世界观边界 · {book_context.get('genre', '未知')}",
                detail=json.dumps({
                    "genre": book_context.get("genre"),
                    "suggested_kinds": book_context.get("suggested_kinds", []),
                }, ensure_ascii=False),
                run_id=run_id)

        # ── 4b. Head scan: 书头版权/序边界（v0, region 落 graph meta）──────
        # 定稿:mapping 文档 §五-b。滑动窗口 + 连续两块 preface 终止;产物
        # 目前落 meta + 事件日志,消费(Scout/explorer 输入裁剪)留后续。
        head_scan = None
        cached_head = self.graph_db.get_meta("head_scan")
        if cached_head:
            try:
                head_scan = json.loads(cached_head)
                log.info("HeadScan: cache hit (verdict=%s, scanned=%d)",
                         head_scan.get("verdict"), head_scan.get("scanned", 0))
            except json.JSONDecodeError:
                cached_head = None

        if not head_scan:
            head_chunks = sorted(
                all_chunks, key=lambda c: (c.chapter_index, c.chunk_index)
            )
            async with aiohttp.ClientSession() as session:
                def _log_head(fields: dict) -> None:
                    log_llm_call(
                        self.graph_db, self._jsonl_path,
                        book_id=book_id, book_title=book.title, run_id=run_id,
                        phase="head_scan",
                        chunk_ids=[c.id for c in head_chunks
                                   if c.chapter_index <= 3],
                        total_chunks=len(head_chunks),
                        model=config_for(self.config,
                                         "bookgraph.head_scan").model,
                        **fields,
                    )
                head_scan = await run_head_scan(
                    session, self.config,
                    book.title, book.author or "",
                    head_chunks, chapter_titles,
                    log_cb=_log_head,
                )
            if head_scan and head_scan.get("verdict") != "fail":
                self.graph_db.set_meta(
                    "head_scan", json.dumps(head_scan, ensure_ascii=False)
                )

        if head_scan:
            hs = head_scan
            self._emit(progress_cb, {
                "type": "head_scan",
                "book_id": book_id,
                "run_id": run_id,
                "verdict": hs.get("verdict"),
                "scanned": hs.get("scanned", 0),
                "calls": hs.get("calls", 0),
                "preface_start": hs.get("preface_start"),
                "identity": len(hs.get("identity") or []),
                "promo": len(hs.get("promo_chunks") or []),
            })
            self.graph_db.record_event(book_id, "head_scan_done",
                step_label=f"头部扫描 · {hs.get('verdict')} · "
                           f"{hs.get('scanned', 0)} 块",
                detail=json.dumps({
                    "verdict": hs.get("verdict"),
                    "preface_start": hs.get("preface_start"),
                    "reason": hs.get("reason", ""),
                }, ensure_ascii=False),
                run_id=run_id)

        # 文本形态回流：导入期判定的形态（哪些排版承载什么性质的文本）注入提取
        # 提示词。提取器只看得到块文本、看不到排版 —— 不回流，信里第一人称的
        # 口吻就会被归给叙述者（详见 explorer._render_text_forms）。
        # 读不到就静默跳过：形态是增益，不是提取的前置条件。
        try:
            text_forms = self.books_db.list_text_forms(book_id)
        except Exception:
            text_forms = []
        if text_forms:
            self.graph_db.record_event(
                book_id, "text_forms_injected", run_id=run_id,
                step_label=f"文本形态注入 · {len(text_forms)} 条")
        explorer_prompt = build_explorer_prompt(book_context, text_forms)
        # 形态映射只进 system prompt 是不够的 —— 提取器读的块文本是扁平的，映射表
        # 无处落地。这里再算一份 {选择器: 形态名}，把**逐段**标签渲染进送给 LLM
        # 的那份 content（绝不写回库，见 services/chunk_annotation）。
        forms = form_map(text_forms)

        # kind 词表 = **保留字** ∪ 本书建议词表。保留字必须在里面：
        # incremental._get_character_names、get_node_index_two_tier 的
        # `kind='character'` 优先级、get_node_detail 的引文分支、前端
        # graph-viz.js 的 KIND_COLORS 都硬编码认它，缺一个就**静默**弄坏
        # 对话观察提取与角色内核。词表外的 kind 不硬改，只记进缺陷清单。
        kind_vocab = set(CORE_KINDS) | {
            str(k).strip()
            for k in ((book_context or {}).get("suggested_kinds") or [])
            if str(k).strip()
        }

        # ── 5. Phase 0: Scout (optional, cached) ──────────────────────────
        scout_plan: dict | None = None
        excluded_ids: set[str] = set()
        # Build a lookup early — needed by content filtering below
        chunk_by_id: dict[str, Any] = {ch.id: ch for ch in all_chunks}
        should_scout = use_scout and total >= SCOUT_MIN_CHUNKS

        if should_scout:
            # Check cache
            cached_scout = self.graph_db.get_meta("scout_plan")
            if cached_scout:
                try:
                    scout_plan = json.loads(cached_scout)
                    log.info("Scout: cache hit (%d batches)",
                             len(scout_plan.get("batch_plan", [])))
                except json.JSONDecodeError:
                    cached_scout = None

            if not cached_scout:
                chunk_dicts = [{
                    "chunk_id": ch.id,
                    "chapter_index": ch.chapter_index,
                    "chapter_title": chapter_titles.get(
                        ch.chapter_index, f"第{ch.chapter_index + 1}章"
                    ),
                    "chunk_index": ch.chunk_index,
                    "content": ch.content or "",
                    "paragraph_sigs": ch.paragraph_sigs,
                    "paragraph_offsets": ch.paragraph_offsets,
                } for ch in all_chunks]
                annotate_chunks(chunk_dicts, forms)

                async with aiohttp.ClientSession() as session:
                    def _log_scout(fields: dict) -> None:
                        log_llm_call(
                            self.graph_db, self._jsonl_path,
                            book_id=book_id, book_title=book.title, run_id=run_id,
                            phase="scout",
                            chunk_ids=[d["chunk_id"] for d in chunk_dicts],
                            total_chunks=total,
                            model=config_for(self.config,
                                             "bookgraph.scout").model,
                            **fields,
                        )
                    scout_plan = await run_scout(
                        session, self.config,
                        book.title, book.author or "",
                        chunk_dicts, total,
                        log_cb=_log_scout,
                    )

                if scout_plan and scout_plan.get("batch_plan"):
                    self.graph_db.set_meta(
                        "scout_plan", json.dumps(scout_plan, ensure_ascii=False)
                    )
                    log.info("Scout: plan cached (%d batches)",
                             len(scout_plan["batch_plan"]))

            if scout_plan and scout_plan.get("batch_plan"):
                # ── Content filtering: mark excluded chunks ────────────────
                excluded_ids = set(scout_plan.get("excluded_chunk_ids") or [])
                content_warnings = scout_plan.get("content_warnings") or []
                if excluded_ids:
                    for cid in excluded_ids:
                        ch = chunk_by_id.get(cid)
                        if ch:
                            # Find the reason from content_warnings
                            reason = ""
                            for cw in content_warnings:
                                if cid in (cw.get("chunk_ids") or []):
                                    reason = cw.get("reason", "")
                                    break
                            self.graph_db.mark_chunk_excluded(cid, reason)
                    excluded_count = len(excluded_ids)
                    log.info("Scout: excluded %d chunks (%d content warnings) — "
                             "%s", excluded_count, len(content_warnings),
                             "foreign lang" if any("外语" in cw.get("reason", "") or
                                 "语" in cw.get("reason", "")
                                 for cw in content_warnings) else "non-narrative")
                    self._emit(progress_cb, {
                        "type": "content_filtered",
                        "book_id": book_id,
                        "run_id": run_id,
                        "excluded_count": excluded_count,
                        "total_chunks": total,
                        "content_warnings": [
                            {"chunk_count": len(cw.get("chunk_ids") or []),
                             "reason": cw.get("reason", ""),
                             "suggestion": cw.get("suggestion", "skip")}
                            for cw in content_warnings
                        ],
                    })
                    self.graph_db.record_event(book_id, "content_filtered",
                        step_label=f"过滤 {excluded_count} chunks",
                        detail=json.dumps({
                            "excluded_count": excluded_count,
                            "total_chunks": total,
                            "content_warnings": [
                                {"chunk_count": len(cw.get("chunk_ids") or []),
                                 "reason": cw.get("reason", ""),
                                 "suggestion": cw.get("suggestion", "skip")}
                                for cw in content_warnings
                            ],
                        }, ensure_ascii=False),
                        run_id=run_id)

                self._emit(progress_cb, {
                    "type": "scout_done",
                    "book_id": book_id,
                    "run_id": run_id,
                    "total_chunks": total,
                    "scout_batches": len(scout_plan["batch_plan"]),
                    "narrative_units": len(scout_plan.get("narrative_units", [])),
                    "excluded_chunks": len(excluded_ids) if excluded_ids else 0,
                })
                self.graph_db.record_event(book_id, "scout_done",
                    step_label=f"Scout: {len(scout_plan['batch_plan'])} 批次"
                              f" · {len(scout_plan.get('narrative_units', []))} 叙事单元"
                              + (f" · 排除 {len(excluded_ids)}" if excluded_ids else ""),
                    detail=json.dumps({
                        "total_chunks": total,
                        "scout_batches": len(scout_plan["batch_plan"]),
                        "narrative_units": len(scout_plan.get("narrative_units", [])),
                        "excluded_chunks": len(excluded_ids) if excluded_ids else 0,
                    }, ensure_ascii=False),
                    run_id=run_id)
            else:
                # Scout produced nothing useful — fall back to mechanical
                log.info("Scout: no usable plan, falling back to mechanical batching")
                scout_plan = None

        # ── 5b. 输入裁剪：消费已完成的归属分类 ────────────────────────────
        # 同一条链上有三份「这块是什么」的判断：classifier 的 chapter_type
        # （含人手在标注面板改过的覆盖层）、head_scan 的区域标签、Scout 在
        # 500 字截断上的重判 —— 而只有信息最少的那份在起作用。这里把前两份
        # 接上，Scout 降为兜底，两份打架的地方**报出来**而不是偷偷选一边。
        # 诊断与实测见 bookgraph/selection.py。
        chapter_types: dict[int, str] = {}
        try:
            chapter_types = {c.chapter_index: c.chapter_type
                             for c in self.books_db.get_chapters(book_id)}
        except Exception:
            log.warning("读 chapter_type 失败，本轮按全书提取", exc_info=True)
        sel = select_chunks(all_chunks, chapter_types,
                            (head_scan or {}).get("boundaries") or {},
                            excluded_ids)
        dropped = set(sel.drop)
        for cid, reason in sel.drop.items():
            # Scout 那批上面已经标过，且带 content_warnings 里的具体理由，别覆盖它
            if reason != "scout":
                self.graph_db.mark_chunk_excluded(cid, reason)
        if dropped or sel.conflicts:
            detail = {"kept": len(sel.keep), "dropped": len(dropped),
                      "by_type": sel.counts.get("by_type", 0),
                      "by_region": sel.counts.get("by_region", 0),
                      "by_scout": sel.counts.get("by_scout", 0),
                      "conflicts": sel.conflicts[:20]}
            log.info("Selection: %s", sel.summary())
            self._emit(progress_cb, {
                "type": "content_filtered", "book_id": book_id,
                "run_id": run_id, "excluded_count": len(dropped),
                "total_chunks": total, "detail": detail,
            })
            self.graph_db.record_event(
                book_id, "content_filtered",
                step_label=f"输入裁剪 · {sel.summary()}",
                detail=json.dumps(detail, ensure_ascii=False), run_id=run_id)

        kept_chunks = [ch for ch in all_chunks if ch.id not in dropped]

        # ── 5c. 叙事域：集类书各篇各自成域 ────────────────────────────────
        # 用户最先提的那个病：「短篇小说集会跨章节串流，导致文本污染」。
        # 域由**两份判断共同**决定 —— 语义裁决（context_setter 拿目录判
        # narrative_structure）+ 机械守卫（顶层单元标题带不带序号）。任何一份
        # 缺席就退成全书单域，**长篇的行为一字不变**。诊断见 bookgraph/scope.py。
        scope_plan = plan_scopes(
            chapters, kept_chunks,
            narrative_structure=(book_context or {}).get("narrative_structure", ""),
        )
        try:
            self.graph_db.set_meta("scope_plan", json.dumps(
                plan_to_meta(scope_plan, kept_chunks), ensure_ascii=False))
        except Exception:
            log.warning("落库 scope_plan 失败（不影响提取）", exc_info=True)
        if scope_plan.is_split:
            log.info("Scope: %s", scope_plan.summary())
            detail = {
                "source": scope_plan.source, "reason": scope_plan.reason,
                "scopes": [{"scope_id": s.scope_id, "label": s.label,
                            "chunks": len(s.chunk_ids)} for s in scope_plan.scopes],
            }
            self.graph_db.record_event(
                book_id, "scopes_planned",
                step_label=f"叙事域 · {scope_plan.summary()}",
                detail=json.dumps(detail, ensure_ascii=False), run_id=run_id)
            self._emit(progress_cb, {
                "type": "scopes_planned", "book_id": book_id, "run_id": run_id,
                "scope_count": len(scope_plan.scopes),
                "labels": [s.label for s in scope_plan.scopes],
            })
        elif scope_plan.conflicts:
            # 「沉默不是成功」：裁决与结构打架时说清楚，不偷偷选一边。
            log.info("Scope: 拒绝划域 — %s", scope_plan.conflicts[0])
            self.graph_db.record_event(
                book_id, "scopes_planned",
                step_label=f"叙事域 · 单域（{scope_plan.reason}）",
                detail=json.dumps({"source": scope_plan.source,
                                   "reason": scope_plan.reason,
                                   "conflicts": scope_plan.conflicts},
                                  ensure_ascii=False), run_id=run_id)

        # ── 6. Build batch list ───────────────────────────────────────────
        if scout_plan and scout_plan.get("batch_plan"):
            # Scout-driven batches
            batch_plan = scout_plan["batch_plan"]
            batch_list: list[list[Any]] = []
            seen_ids: set[str] = set()
            # Pre-seed seen_ids with dropped so they aren't treated as leftover
            seen_ids.update(dropped)
            for bp in batch_plan:
                seg: list[Any] = []
                for cid in bp.get("chunk_ids", []):
                    if cid not in seen_ids and cid not in dropped:
                        ch = chunk_by_id.get(cid)
                        if ch:
                            seg.append(ch)
                            seen_ids.add(cid)
                # Scout 按情节节拍分批，可能一批横跨两篇 —— 在域边界上切开
                batch_list.extend(_split_by_scope(seg, scope_plan.by_chunk, None))
            # Append any leftover chunks not in scout plan (but not dropped)
            leftover = [ch for ch in all_chunks
                        if ch.id not in seen_ids and ch.id not in dropped]
            if leftover:
                log.info("Scout missed %d chunks — appending as final batch", len(leftover))
                batch_list.extend(
                    _split_by_scope(leftover, scope_plan.by_chunk, batch_size))
            log.info("Scout plan: %d batches (vs %d mechanical)%s",
                     len(batch_list), (total + batch_size - 1) // batch_size,
                     f" — {len(dropped)} dropped" if dropped else "")
        else:
            # Mechanical fallback —— 也要跳过被裁掉的块、也不跨域。
            # 旧代码这里直接用 all_chunks，于是 Scout 的排除在它没产出计划时
            # **静默失效**（过滤只活在 scout-plan 那条分支里）。
            batch_list = _split_by_scope(kept_chunks, scope_plan.by_chunk, batch_size)

        total_batches = len(batch_list)

        # ── 7. Loop: batch → Explorer → Recorder ──────────────────────────
        # Preparation (serial) launches every Explorer call under a concurrency
        # semaphore; reaping (also serial on the event loop) performs all DB
        # writes so SQLite's single connection is never interleaved.
        batch_stats: list[dict] = []

        # Scout 点名的关键人物**无条件准入** —— 主角可能在某一批里一条关系都
        # 没挂上（他只是在场），但那不代表他不是实体。
        key_chars = [str(c.get("name") or "").strip()
                     for c in ((scout_plan or {}).get("key_characters") or [])
                     if str(c.get("name") or "").strip()]

        async with aiohttp.ClientSession() as session:
            # ── 骨架趟：先认清成员与结构，再逐批读 ────────────────────────
            # 必须在**这个** session 里跑 —— 上面几个阶段各自开了自己的
            # ``async with ClientSession()``，名字会泄漏到外层，但连接已经关了；
            # 拿那个跑会得到 “Session is closed”（实测）。
            # 产出（规范名 / 别称 / 一句话身份 / 亲缘与社会关系）会出现在后面
            # 每个批次的名字索引里 —— 批次不再从零开始认人。
            skeleton_stats: dict | None = None
            if run_skeleton_pass and kept_chunks:
                try:
                    skeleton_stats = await self._run_skeleton_phase(
                        session, book, chapter_titles, kept_chunks, run_id,
                        progress_cb, book_context=book_context)
                except Exception:
                    # 骨架趟失败不该拖垮整次提取 —— 退回「没有骨架」的旧行为，
                    # 但要出声（沉默不是成功）。
                    log.warning("骨架趟失败，退回逐批提取", exc_info=True)
                    skeleton_stats = None

            sem = asyncio.Semaphore(BATCH_CONCURRENCY)
            pending: dict[int, asyncio.Task] = {}
            batch_by_index: dict[int, list[Any]] = {}
            batch_ids: dict[int, str] = {}
            batch_labels: dict[int, str] = {}

            for batch_index, batch_chunks_ in enumerate(batch_list, 1):
                batch_id = f"batch-{uuid.uuid4().hex[:8]}"

                # Build step label from scout plan or first chunk
                if scout_plan and scout_plan.get("batch_plan"):
                    bp = scout_plan["batch_plan"][batch_index - 1] if batch_index - 1 < len(scout_plan["batch_plan"]) else {}
                    step_label = bp.get("label") or (
                        f"第 {batch_chunks_[0].chapter_index + 1} 章「"
                        f"{chapter_titles.get(batch_chunks_[0].chapter_index, '')}」"
                    )
                else:
                    step_label = (
                        f"第 {batch_chunks_[0].chapter_index + 1} 章「"
                        f"{chapter_titles.get(batch_chunks_[0].chapter_index, '')}」"
                    )

                # Skip batch if all its chunks are already done
                if all(self.graph_db.is_chunk_done(ch.id) for ch in batch_chunks_):
                    log.info("Batch %d/%d: SKIP (all done)", batch_index, total_batches)
                    self._emit(progress_cb, {
                        "type": "batch_skipped",
                        "book_id": book_id,
                        "run_id": run_id,
                        "batch_id": batch_id,
                        "batch_index": batch_index,
                        "total_batches": total_batches,
                        "done_chunks": self.graph_db.done_count(book_id),
                        "total_chunks": total,
                    })
                    self.graph_db.record_progress(
                        book_id, batch_id, batch_index, step_label, "skipped",
                        detail=json.dumps({
                            "done_chunks": self.graph_db.done_count(book_id),
                            "total_chunks": total,
                            "total_batches": total_batches,
                        }),
                        run_id=run_id,
                    )
                    continue

                self._emit(progress_cb, {
                    "type": "batch_start",
                    "book_id": book_id,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "batch_index": batch_index,
                    "total_batches": total_batches,
                    "done_chunks": self.graph_db.done_count(book_id),
                    "total_chunks": total,
                    "step_label": step_label,
                })
                self.graph_db.record_progress(
                    book_id, batch_id, batch_index, step_label, "in_progress",
                    detail=json.dumps({"total_batches": total_batches}),
                    run_id=run_id,
                )

                # Build chunk dicts for Explorer
                chunk_dicts = [{
                    "chunk_id": ch.id,
                    "chapter_index": ch.chapter_index,
                    "chapter_title": chapter_titles.get(
                        ch.chapter_index, f"第{ch.chapter_index + 1}章"
                    ),
                    "chunk_index": ch.chunk_index,
                    "content": ch.content or "",
                    "paragraph_sigs": ch.paragraph_sigs,
                    "paragraph_offsets": ch.paragraph_offsets,
                } for ch in batch_chunks_]
                annotate_chunks(chunk_dicts, forms)

                # Augment explorer prompt with scout extraction focus
                batch_explorer_prompt = explorer_prompt
                extraction_focus = ""
                if scout_plan and scout_plan.get("batch_plan"):
                    bp = scout_plan["batch_plan"][batch_index - 1] if batch_index - 1 < len(scout_plan["batch_plan"]) else {}
                    extraction_focus = bp.get("extraction_focus", "")
                    if extraction_focus:
                        batch_explorer_prompt = explorer_prompt + f"\n\n**本批提取重点**：{extraction_focus}"

                # 叙事域：本批属于哪一篇 —— 只在 user message 里带一句域简报。
                # **实体索引不按域过滤**（见 explorer.py 的说明：过滤会让复现
                # 实体被反复重命名）。单域（长篇）时简报为空，行为不变。
                batch_scope_brief = ""
                if scope_plan.is_split:
                    sc = scope_plan.get(scope_plan.scope_of(batch_chunks_[0].id))
                    if sc is not None:
                        batch_scope_brief = scope_brief(sc, scope_plan)

                batch_ids[batch_index] = batch_id
                batch_labels[batch_index] = step_label
                batch_by_index[batch_index] = batch_chunks_
                pending[batch_index] = asyncio.create_task(
                    _run_batch_explorer(
                        sem, session, self.config, self.graph_db,
                        book_id, book.title, book.author or "",
                        run_id, batch_id, batch_index, chunk_dicts, total,
                        batch_explorer_prompt, self._jsonl_path, extraction_focus,
                        scope_brief_text=batch_scope_brief,
                        verify=verify, max_rounds=verify_rounds,
                        admitted=set(key_chars),
                        repair_mode=repair_mode,
                        book_text=book_text,
                    )
                )

            # ── Reap (serial DB writes — completions may be out of order) ──
            # asyncio.wait returns the ORIGINAL task objects (as_completed
            # wraps them, so we can't recover the batch_index from it).
            pending_by_task = {t: i for i, t in pending.items()}
            while pending:
                done, _ = await asyncio.wait(
                    pending.values(), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    batch_index = pending_by_task[task]
                    try:
                        _, result, error_type, verdict = task.result()
                    except Exception as exc:
                        # One unexpected per-batch failure shouldn't cancel the
                        # remaining pending batches — mark it failed, move on.
                        log.error("Batch %d/%d: UNEXPECTED ERROR: %s",
                                  batch_index, total_batches, exc)
                        result, error_type = None, "exhausted_retries"
                        verdict = {"verified": False, "verdict": "error",
                                   "rounds": 0, "mechanical": 0,
                                   "challenges": 0, "residual": 0}
                    batch_chunks_ = batch_by_index[batch_index]
                    batch_id = batch_ids[batch_index]
                    step_label = batch_labels[batch_index]

                    if result is None:
                        log.error("Batch %d/%d: FAILED (%s)", batch_index,
                                  total_batches, error_type)
                        for ch in batch_chunks_:
                            self.graph_db.mark_chunk_failed(ch.id, error_reason=error_type)
                        self.graph_db.record_progress(
                            book_id, batch_id, batch_index, step_label, "failed",
                            detail=json.dumps({"error_reason": error_type}),
                            run_id=run_id,
                        )
                        self._emit(progress_cb, {
                            "type": "batch_failed",
                            "book_id": book_id,
                            "run_id": run_id,
                            "batch_id": batch_id,
                            "batch_index": batch_index,
                            "total_batches": total_batches,
                            "done_chunks": self.graph_db.done_count(book_id),
                            "total_chunks": total,
                            "step_label": step_label,
                            "detail": {"error_reason": error_type},
                        })
                        batch_stats.append({
                            "batch_index": batch_index,
                            "status": "failed",
                            "error_reason": error_type,
                        })
                    else:
                        # Record result
                        chunk_ids = [ch.id for ch in batch_chunks_]
                        stats = record_result(
                            self.graph_db, book_id, chapter_node_ids, chunk_ids, result,
                            batch_id=batch_id,
                            vocabulary=kind_vocab, key_characters=key_chars,
                            # 锚点首现块由系统算（见 recorder._first_mention）——
                            # 模型填的是 evidence 所在块，证伪器为此报了 14 条质疑。
                            chunk_text={ch.id: (ch.content or "")
                                        for ch in batch_chunks_},
                        )
                        # 每块一句话 —— 与图谱同源、落在 books.db（见
                        # ``bookgraph/chunk_summary``）。漏写的块记下来，
                        # 补录脚本按空值扫，不在这里重烧一轮提取。
                        summaries = summaries_from_result(result, chunk_ids)
                        if summaries:
                            self.books_db.set_chunk_summaries(summaries)
                            miss = missing_ids(chunk_ids, summaries)
                            if miss:
                                log.warning(
                                    "Batch %d/%d 简述 %d/%d，漏 %d 块"
                                    "（可由 scripts/backfill_chunk_summaries.py 补）",
                                    batch_index, total_batches, len(summaries),
                                    len(chunk_ids), len(miss))
                        else:
                            log.warning(
                                "Batch %d/%d 没有返回 chunk_summaries —— 这 %d 块"
                                "检索时退回原文", batch_index, total_batches,
                                len(chunk_ids))
                        # 缺陷清单落进度行 —— P4 按批回炉时从这里取。
                        # 「沉默不是成功」：孤儿、漂移、词表外都要看得见。
                        defects = {
                            "orphan_dropped": stats.orphan_dropped[:50],
                            "kind_drift": stats.kind_drift[:50],
                            "kind_off_vocab": sorted(set(stats.kind_off_vocab))[:50],
                            "unresolved_admitted": stats.unresolved_admitted[:50],
                            "identity_warnings": stats.identity_warnings[:50],
                        }
                        if any(defects.values()):
                            log.info("Batch %d/%d 缺陷: 孤儿丢弃 %d · kind 漂移 %d · "
                                     "词表外 %d · 兜底建节点 %d",
                                     batch_index, total_batches,
                                     len(stats.orphan_dropped), len(stats.kind_drift),
                                     len(stats.kind_off_vocab),
                                     len(stats.unresolved_admitted))
                        self.graph_db.record_progress(
                            book_id, batch_id, batch_index, step_label, "done",
                            detail=json.dumps({
                                "new_nodes": stats.new_nodes,
                                "new_edges": stats.new_edges,
                                "new_aliases": stats.new_aliases,
                                "new_quotes": stats.new_quotes,
                                "merge_candidates": stats.merge_candidates,
                                "unresolved": stats.unresolved,
                                "defects": defects,
                                "verify": verdict,
                            }, ensure_ascii=False),
                            run_id=run_id,
                        )
                        # **沉默不是成功**：没验干净的批次必须看得见，不能混在
                        # 一堆「完成」里冒充收敛。
                        if not verdict.get("verified"):
                            log.warning(
                                "Batch %d/%d 未经验证（%s，回炉 %d 轮，残余 %d 条）",
                                batch_index, total_batches,
                                verdict.get("verdict"), verdict.get("rounds", 0),
                                verdict.get("residual", 0))
                            self.graph_db.record_event(
                                book_id, "batch_unverified",
                                step_label=f"第 {batch_index} 批未经验证 · "
                                           f"{verdict.get('verdict')}",
                                detail=json.dumps({
                                    "batch_index": batch_index,
                                    "batch_id": batch_id,
                                    "verify": verdict,
                                }, ensure_ascii=False), run_id=run_id)
                            self._emit(progress_cb, {
                                "type": "batch_unverified", "book_id": book_id,
                                "run_id": run_id, "batch_id": batch_id,
                                "batch_index": batch_index,
                                "total_batches": total_batches,
                                "verify": verdict,
                            })

                        done_now = self.graph_db.done_count(book_id)
                        log.info(
                            "Batch %d/%d: nodes=%d obs=%d edges=%d aliases=%d quotes=%d [%d/%d done]",
                            batch_index, total_batches,
                            stats.new_nodes, stats.new_observations,
                            stats.new_edges, stats.new_aliases,
                            stats.new_quotes, done_now, total,
                        )
                        self._emit(progress_cb, {
                            "type": "batch_done",
                            "book_id": book_id,
                            "run_id": run_id,
                            "batch_id": batch_id,
                            "batch_index": batch_index,
                            "total_batches": total_batches,
                            "done_chunks": done_now,
                            "total_chunks": total,
                            "step_label": step_label,
                            "detail": {
                                "new_nodes": stats.new_nodes,
                                "new_observations": stats.new_observations,
                                "new_edges": stats.new_edges,
                                "new_aliases": stats.new_aliases,
                                "new_quotes": stats.new_quotes,
                                "merge_candidates": stats.merge_candidates,
                            },
                        })
                        batch_stats.append({
                            "batch_index": batch_index,
                            "status": "done",
                            "nodes": stats.new_nodes,
                            "edges": stats.new_edges,
                            "aliases": stats.new_aliases,
                            "quotes": stats.new_quotes,
                            "merges": stats.merge_candidates,
                        })

                    del pending_by_task[task]
                    del pending[batch_index]

        # ── 8. Auto-merge high-confidence entity candidates ─────────────────
        try:
            merged = self.graph_db.auto_merge(book_id, min_confidence=0.9)
            if merged:
                log.info("[GraphOrchestrator] Auto-merged %d entity pairs for %s", merged, book_id)
        except Exception as exc:
            log.warning("[GraphOrchestrator] Auto-merge failed (non-fatal): %s", exc)

        # ── 9. Final stats ────────────────────────────────────────────────
        final_stats = self.graph_db.get_stats(book_id)
        self._emit(progress_cb, {
            "type": "complete",
            "book_id": book_id,
            "run_id": run_id,
            "done_chunks": final_stats["chunks_done"],
            "total_chunks": total,
            "final_stats": final_stats,
        })
        self.graph_db.record_event(book_id, "complete",
            step_label="提取完成",
            detail=json.dumps({
                "run_id": run_id,
                "done_chunks": final_stats["chunks_done"],
                "total_chunks": total,
                "final_stats": final_stats,
            }, ensure_ascii=False),
            run_id=run_id)

        return ExtractionResult(
            book_id=book_id,
            book_title=book.title,
            total_chunks=total,
            chunks_done=final_stats["chunks_done"],
            chunks_failed=final_stats["chunks_failed"],
            final_stats=final_stats,
            book_context=book_context,
            batch_stats=batch_stats,
            scout_plan=scout_plan,
        )

    async def _run_skeleton_phase(self, session, book, chapter_titles: dict,
                                  chunks: list, run_id: str, progress_cb,
                                  book_context: dict | None = None) -> dict:
        """跑骨架趟：按章切组、逐组提取、合并落库。

        分组而不是一次喂完整本，是为了长书也不爆上下文；组间用**已确认的
        实体名册**串起来（后一组沿前一组的叫法），与批次提取器同一条约束。
        """
        blocks: list[tuple[int, str, str]] = []
        cur_idx = None
        buf: list[str] = []
        for ch in sorted(chunks, key=lambda c: (c.chapter_index, c.chunk_index)):
            if ch.chapter_index != cur_idx:
                if cur_idx is not None:
                    blocks.append((cur_idx, chapter_titles.get(cur_idx, ""),
                                   "\n".join(buf)))
                cur_idx = ch.chapter_index
                buf = []
            buf.append(ch.content or "")
        if cur_idx is not None:
            blocks.append((cur_idx, chapter_titles.get(cur_idx, ""),
                           "\n".join(buf)))

        groups = group_chapters(blocks)
        self._emit(progress_cb, {
            "type": "skeleton_start", "book_id": book.id, "run_id": run_id,
            "groups": len(groups), "chapters": len(blocks),
        })
        self.graph_db.record_event(
            book.id, "skeleton_start",
            step_label=f"骨架趟 · {len(blocks)} 章 / {len(groups)} 组",
            run_id=run_id)

        entities: list[dict] = []
        relations: list[dict] = []
        seen: set[str] = set()
        notes: list[str] = []
        for i, group in enumerate(groups, 1):
            roster = sorted(seen)
            parsed = await run_skeleton(
                session, self.config, book.title, book.author or "", group,
                roster=roster, part=i, total_parts=len(groups),
                collection=((book_context or {}).get("narrative_structure")
                            == "collection"),
                log_cb=self._skeleton_log_cb(book, run_id, i),
            )
            if parsed is None:
                notes.append(f"第 {i} 组无结果")
                continue
            es, rs, ns = normalize_skeleton(parsed)
            notes.extend(ns)
            for e in es:
                if e["name"] not in seen:
                    seen.add(e["name"])
                    entities.append(e)
            relations.extend(rs)

        stats = record_skeleton(self.graph_db, book.id, entities, relations,
                                [{"chunk_id": c.id,
                                  "chapter_index": c.chapter_index,
                                  "content": c.content or ""}
                                 for c in chunks])
        stats["notes"] = notes[:20]
        stats["groups"] = len(groups)
        # **空骨架不是成功。** 实测一次跑里骨架调用被限流打掉，落库的是
        # 「实体 0 · 别名 0 · 关系 0」，事件台账却写 `done` —— 下游只看到
        # 「这本书没有人物」。失败要能一眼看出来。
        stats["ok"] = bool(entities or relations)
        if not stats["ok"]:
            log.error("骨架趟产出为空：%d 组全部失败（%s）—— 后续批次没有"
                      "规范名与亲缘边", len(groups), "; ".join(notes[:3]))
        if stats.get("evidence_unverified"):
            # 骨架关系不过裁决环，这里是它唯一的体检口。
            log.warning("骨架趟有 %d/%d 条关系的证据不是逐字原文（例：%s）",
                        len(stats["evidence_unverified"]), stats["relations"],
                        stats["evidence_unverified"][0])
        self.graph_db.record_event(
            book.id, "skeleton_done",
            step_label=(f"骨架{'（失败）' if not stats['ok'] else ''} · "
                        f"实体 {stats['entities']} · 别名 {stats['aliases']}"
                        f" · 关系 {stats['relations']}"),
            detail=json.dumps(stats, ensure_ascii=False), run_id=run_id)
        self._emit(progress_cb, {
            "type": "skeleton_done", "book_id": book.id, "run_id": run_id,
            "detail": {k: v for k, v in stats.items() if k != "notes"},
        })
        log.info("Skeleton: %s", stats)
        return stats

    def _skeleton_log_cb(self, book, run_id: str, part: int):
        def _cb(fields: dict) -> None:
            log_llm_call(
                self.graph_db, self._jsonl_path,
                book_id=book.id, book_title=book.title, run_id=run_id,
                phase="skeleton", batch_id=f"skeleton-{part}",
                batch_index=part, chunk_ids=[], total_chunks=0,
                model=config_for(self.config,
                                 "bookgraph.skeleton").model, **fields,
            )
        return _cb

    def _emit(self, cb: ProgressCallback | None, event: dict):
        """Safely call progress callback."""
        if cb:
            try:
                cb(event)
            except Exception as exc:
                log.warning("Progress callback error: %s", exc)
