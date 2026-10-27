"""作者层采集 —— 一次普通 LLM 调用，**不是子代理类型**。

与 ``bookgraph/notes.py``、``bookgraph/verify.py`` 同形，理由也一样：
输入完全确定（书的身份 + 目录 + 序跋全文），不需要探索；给它工具去读更多
东西，恰恰破坏「上下文隔离」这个唯一的价值；普通调用才能被
``scripts/fake_provider.py`` 零 token 端到端驱动。

这个项目已经三次走到同一个结论（``notes.py`` 的注释明写「不是子代理类型」、
P4 把 ``book_extract_verifier`` 子代理换成 ``verify.py`` 的一次调用、
``incremental.py`` 从对话里抽观察也是普通调用）。判据是**这一步需不需要
去够它没拿到的东西** —— 采集不需要，它要的东西 ``scope.py`` 已经给全了。

本模块**只生成，不落库**。落库在 ``store.py``，编排在
``scripts/run_author_harvest.py``。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import aiohttp

import usage_log
from bookgraph.invariants import _norm_ws
from bookgraph.llm_call import (finish_reason, output_budget, reasoning_text,
                                usage_tokens)
from config import Config
from llm_json import loads_tolerant
from provider_store import config_for
from storage.authors_db import (BOOK_ROLES, POSITIONS, RECORD_KINDS,
                                SPEAKERS)
from sub_agent.types import load_prompt

log = logging.getLogger("authorgraph")

#: 兜底提示词。``prompts/subagent/author_harvest.md`` 缺失时用 ——
#: 最小可用，不静默降级成「什么都不做」。
_FALLBACK = """你在读一本书的序跋（作者自序/前言/后记）。
你看不到正文，也不许猜正文里的事。

判断作者在这本书里的位置（narrator/avatar/editor/recorder/absent/unknown；
判不出来写 avatar），并摘出作者本人说的话。

严格 JSON：
{"author_name":"","author_aliases":[],"nationality":"","birth_year":null,
 "death_year":null,"role":"author","position":"avatar","position_note":"",
 "corpus":[{"chunk_id":"","excerpt":"逐字原文","speaker":"author",
            "text_kind":"preface","era_year":null,"era_note":""}],
 "records":[{"kind":"life_event","year":null,"year_end":null,
             "statement":"","evidence_excerpt":"","chunk_id":""}]}
excerpt 必须逐字照抄；年份只在文本明说时填；没有就返回空数组，不要硬写。"""


def harvest_prompt() -> str:
    return load_prompt("author_harvest", _FALLBACK)


# ── 宽容三层：语法 / 形状 / 取值 ─────────────────────────────────────────
#
# 抄项目既有的三层（见 CLAUDE.md「模型输出宽容三层」）：语法交给
# ``llm_json.loads_tolerant``，形状在这里收，取值不认识的**照报**，
# 不装作没判定。

def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _year(v: Any) -> int | None:
    """年份。收得宽（"1944年" / 1944.0 / "约1944"），收不出就 None。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        y = int(v)
        return y if 1 <= y <= 2999 else None
    s = _s(v)
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 3:
        y = int(digits[:4])
        return y if 1 <= y <= 2999 else None
    return None


def _rows(v: Any) -> list[dict[str, Any]]:
    """形状宽容：模型可能给 list，也可能给 dict（单条）或 ``{"0": {...}}``。"""
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        vals = list(v.values())
        if vals and all(isinstance(x, dict) for x in vals):
            return list(vals)
        return [v]
    return []


def normalize_result(raw: Any, chunk_text: dict[str, str],
                     chunk_meta: dict[str, tuple[int, int]] | None = None,
                     ) -> tuple[dict[str, Any], list[str]]:
    """把模型的原始输出收成落库形状。返回 ``(result, defects)``。

    ``chunk_meta`` 是 ``{chunk_id: (chapter_index, chunk_index)}`` ——
    **坐标由系统补，不由模型给**。这是踩出来的：第一版没传坐标，于是
    ``author_corpus.chapter_index`` 全是 NULL，而剧透门（``gate.reader_year``）
    正是按章号算的 —— **门看起来装好了，实际永远不会亮**。
    与 ``first_appearance_chunk_id``、``graph_notes.chunk_id`` 同一条：
    导出量不该让模型猜。

    **两条不同的处置**，别混：

    - **词表外的取值**（``position`` 写了 ``"memoir"``）：收成安全默认值
      **并报进 defects** —— 照报不装作没判定。
    - **找不到出处的引文**：先按 ``excerpt`` 在候选块里**重新定位**
      （锚点是导出量，不该让模型猜 —— 与 ``notes.reanchor`` 同一条），
      重定位也找不到才**丢弃并报** ``fabricated_excerpt``。

    ``defects`` 空 = 干净。非空不等于失败，是**要给人看的账**。
    """
    defects: list[str] = []
    d = raw if isinstance(raw, dict) else {}

    pos = _s(d.get("position"))
    if pos not in POSITIONS:
        if pos:
            defects.append(f"position 词表外：{pos!r} → unknown")
        pos = "unknown"

    role = _s(d.get("role"))
    if role not in BOOK_ROLES:
        if role:
            defects.append(f"role 词表外：{role!r} → author")
        role = "author"

    aliases = [_s(x) for x in d["author_aliases"] if _s(x)] \
        if isinstance(d.get("author_aliases"), list) else []
    co_authors = [_s(x) for x in d["co_authors"] if _s(x)] \
        if isinstance(d.get("co_authors"), list) else []
    # 同一个名字不该既当合著又当别名
    co_authors = [x for x in co_authors
                  if x != _s(d.get("author_name")) and x not in aliases]

    corpus: list[dict[str, Any]] = []
    for c in _rows(d.get("corpus")):
        ex = _s(c.get("excerpt"))
        if not ex:
            continue
        cid = _s(c.get("chunk_id"))
        # 逐字校验 → 失败则重定位（导出量现算）
        if cid not in chunk_text or _norm_ws(ex) not in _norm_ws(chunk_text[cid]):
            hit = next((k for k, t in chunk_text.items()
                        if _norm_ws(ex) in _norm_ws(t)), None)
            if hit is None:
                defects.append(f"fabricated_excerpt：{ex[:24]!r} 不在任何候选块里")
                continue
            if cid and cid != hit:
                defects.append(f"reanchor {cid[:8]}→{hit[:8]}")
            cid = hit
        sp = _s(c.get("speaker"))
        if sp not in SPEAKERS:
            if sp:
                defects.append(f"speaker 词表外：{sp!r} → other")
            sp = "other"
        ci, ck = (chunk_meta or {}).get(cid, (None, None))
        corpus.append({
            "chunk_id": cid, "excerpt": ex, "speaker": sp,
            "chapter_index": ci, "chunk_index": ck,
            "text_kind": _s(c.get("text_kind")) or "other",
            "era_year": _year(c.get("era_year")),
            "era_note": _s(c.get("era_note")),
        })

    records: list[dict[str, Any]] = []
    for r in _rows(d.get("records")):
        st = _s(r.get("statement"))
        if not st:
            continue
        k = _s(r.get("kind"))
        if k not in RECORD_KINDS:
            if k:
                defects.append(f"kind 词表外：{k!r} → life_event")
            k = "life_event"
        ev = _s(r.get("evidence_excerpt"))
        cid = _s(r.get("chunk_id"))
        if ev and (cid not in chunk_text
                   or _norm_ws(ev) not in _norm_ws(chunk_text.get(cid, ""))):
            hit = next((kk for kk, t in chunk_text.items()
                        if _norm_ws(ev) in _norm_ws(t)), None)
            if hit:
                cid = hit
            else:
                defects.append(f"record 无原文依据：{st[:24]!r}")
                ev = ""
        records.append({
            "kind": k, "year": _year(r.get("year")),
            "year_end": _year(r.get("year_end")), "statement": st,
            "evidence_excerpt": ev, "chunk_id": cid or None,
        })

    # ── 机械体检：两个字段之间的自相矛盾 ─────────────────────────────
    #
    # 实测（2026-10-15 全库首跑）：6 本书被判 `absent`（作者不在书里），
    # 其中 5 本**同时**采到了 `speaker=author` 的语料 —— 作者不在书里，
    # 却又在书里说「我」。根因是我在提示词里把「论著」写成了 `absent`
    # 的例子，模型照着填。提示词已改，但**这种矛盾系统一秒能查出来**，
    # 不该只靠提示词自律。
    #
    # 照报不装作没判定：报出来，**不自动改** —— 该改哪个字段是判断，
    # 不是机械操作（与 `invariants.normalize_kind` 词表外原样保留同一条）。
    if pos == "absent" and any(c["speaker"] == "author" for c in corpus):
        defects.append(
            "自相矛盾：position=absent（作者不在书里）却采到 speaker=author "
            "的语料 —— 位置应改为 narrator 或 editor")

    return {
        "author_name": _s(d.get("author_name")),
        "co_authors": co_authors,
        "author_aliases": aliases,
        "nationality": _s(d.get("nationality")),
        "birth_year": _year(d.get("birth_year")),
        "death_year": _year(d.get("death_year")),
        "role": role, "position": pos,
        "position_note": _s(d.get("position_note")),
        "corpus": corpus, "records": records,
    }, defects


# ── 跑一趟 ────────────────────────────────────────────────────────────────

async def harvest_author(
    session: aiohttp.ClientSession,
    cfg: Config,
    ident: dict[str, Any],
    toc: list[str],
    chunks: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    log_cb: Callable[[dict], None] | None = None,
) -> dict[str, Any] | None:
    """跑一趟采集。返回**已归一**的结果；``None`` = 这一趟没跑成。

    返回 None **不等于**「这本书没料」—— 后者是 ``{"corpus": [], ...}``，
    是诚实的回答。调用方必须分辨这两者（与 ``generate_notes`` 同一条约定）。

    ``temperature`` 默认 0.2 而非读者笔记的 0.7：这是**取证**任务，不是
    创作任务。取证要可复现 —— 同一本书跑两次该得到同一份台账。
    """
    cfg = config_for(cfg, "authorgraph.harvest")
    from authorgraph.scope import build_harvest_msg

    user_msg = build_harvest_msg(ident, toc, chunks)
    system_prompt = harvest_prompt()
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_msg}],
        "stream": False,
        "temperature": temperature,
        "max_tokens": output_budget(cfg),
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json"}
    chunk_text = {c["chunk_id"]: c["text"] for c in chunks}
    chunk_meta = {c["chunk_id"]: (c["chapter_index"], c["chunk_index"])
                  for c in chunks}
    t0 = time.monotonic()

    def _emit(status: str, *, response: str = "", reasoning: str = "",
              finish: str = "", error_detail: str = "",
              tokens_in: int = 0, tokens_out: int = 0) -> None:
        """把这一趟交给调用方（脚本接飞行记录仪），并记一条用量。

        思维链**一起交出去**：采集是否把作者的「位置」判错，往往在推理里
        就看得见；只记正文等于把最该读的那段丢了。
        """
        # token 先记：它不依赖有没有接记录仪，用量页要算作者层的成本。
        usage_log.record(
            source="authorgraph", model=cfg.model,
            tokens_in=tokens_in, tokens_out=tokens_out, status=status,
            ref_id=str(ident.get("stable_book_id") or ""))
        if log_cb:
            log_cb({"status": status, "system": system_prompt,
                    "prompt": user_msg, "response": response,
                    "reasoning_content": reasoning, "finish_reason": finish,
                    "error_detail": error_detail,
                    "model": cfg.model,
                    "latency_ms": int((time.monotonic() - t0) * 1000)})

    try:
        async with session.post(
            f"{cfg.api_base}/chat/completions", json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                log.warning("Author harvest API %s: %s", resp.status, err[:200])
                _emit("api_error", error_detail=err[:300])
                return None
            data = json.loads(await resp.text())
            content = data["choices"][0]["message"].get("content") or ""
            reasoning = reasoning_text(data)
            finish = finish_reason(data)
            tokens_in, tokens_out = usage_tokens(data)
            parsed = loads_tolerant(content, expect=dict)
            if not parsed:
                _emit("parse_failed", response=content, reasoning=reasoning,
                      finish=finish, tokens_in=tokens_in,
                      tokens_out=tokens_out)
                return None
            result, defects = normalize_result(parsed, chunk_text, chunk_meta)
            result["defects"] = defects
            _emit("ok", response=content, reasoning=reasoning, finish=finish,
                  tokens_in=tokens_in, tokens_out=tokens_out)
            return result
    except Exception as exc:                            # noqa: BLE001
        log.warning("Author harvest failed: %s", exc)
        _emit("exception", error_detail=str(exc)[:300])
        return None

