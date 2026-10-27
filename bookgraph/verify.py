"""提取结论的**回炉环** —— 机械体检 + 对抗证伪 → 带缺陷清单重提该批。

导入侧早就跑通了「立论 → 证伪 → 机械校验 → 回炉」（``services/import_analysis_service``），
提取侧一直停在它之前的状态：一次调用、产出 JSON、直接落库。本模块把那个环搬过来。

三件事必须成立，缺一个这个环就是装饰：

1. **机械体检跑在候选结果上，不是落库后的图谱上。** 缺陷要在**写之前**发现，
   否则回炉等于事后补锅。``_graph_view`` 把一批提取结果投影成 ``invariants``
   认的形状，体检器不需要知道数据是从哪来的。
2. **证伪趟的上下文是隔离的。** 只给「结论 + 该批原文 + 本域名册」，**不给推理
   过程** —— 看得见推理链它就会替对方辩护。这是导入侧验证过的原则。
3. **沉默不是成功。** 证伪趟没吐出可解析的结论时，这批记 ``inconclusive``
   （未经验证），**绝不冒充收敛**。导入侧 8.5.9 的教训：verifier 用满预算被
   截断，差点被读成「无问题、无反例」。

纯编排 + 一次 LLM 调用；可被 ``scripts/fake_provider.py`` 的 ``/_mock/scenario``
零 token 端到端驱动。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import aiohttp

from bookgraph.invariants import Report, check_graph, unresolved_refs
from bookgraph.llm_call import (
    cached_tokens, output_budget, reasoning_text, reasoning_tokens)
from config import Config
from llm_json import loads_tolerant
from provider_store import config_for
from sub_agent.types import load_prompt

log = logging.getLogger("bookgraph")

_VERIFIER_FALLBACK = """你会收到一批正文和一批从其中提取的断言。你的任务是**证伪**：
找出哪些断言在正文里站不住（无据 / 张冠李戴 / 视角冒充事实）。
找不到反例就诚实地说 upheld，不要硬凑。严格 JSON：
{"verdict":"upheld|challenged|inconclusive",
 "challenges":[{"claim":"...","severity":"high|medium|low","why":"...","evidence":"..."}],
 "summary":"..."}"""


def verifier_prompt() -> str:
    return load_prompt("book_extract_verifier", _VERIFIER_FALLBACK)


# ── 候选结果 → 体检器认的形状 ─────────────────────────────────────────────

def graph_view(result: dict, chunk_ids: list[str],
               known: set[str] | None = None) -> dict:
    """把一批提取结果投影成 ``check_graph`` 认的图。

    节点用 canonical_name 当 id —— 这一批还没落库，没有真 id，而边/观察引用的
    本来就是名字。**没有声明的名字不进 nodes**：它们是图谱里已有的实体，本来
    就不是这批的产物，不该被算成孤儿。

    *known* 是**库里已有的**节点名（含别名）。没给的话，凡是被引用却没声明的
    名字都会被算成悬空 —— 那会把「引用了旧实体」误报成缺陷。
    """
    declared = {(n.get("canonical_name") or "").strip()
                for n in (result.get("new_nodes") or [])}
    declared.discard("")
    return {
        "nodes": [{"id": name, "canonical_name": name,
                   "kind": (n.get("kind") or "")}
                  for n in (result.get("new_nodes") or [])
                  if (name := (n.get("canonical_name") or "").strip())],
        "edges": [{"source_id": (e.get("source") or "").strip(),
                   "target_id": (e.get("target") or "").strip(),
                   "kind": e.get("kind"), "chunk_id": e.get("chunk_id"),
                   "evidence_text": e.get("evidence")}
                  for e in (result.get("new_edges") or [])],
        "observations": [{"node_id": (o.get("node_name") or "").strip(),
                          "field": o.get("field"), "value": o.get("value"),
                          "evidence": o.get("evidence_chunk_id"),
                          "evidence_text": o.get("evidence")}
                         for o in (result.get("new_observations") or [])],
        "quotes": result.get("quotes") or [],
        "chunks": [{"chunk_id": c} for c in chunk_ids],
        "aliases": [{"node_id": (a.get("canonical_name") or "").strip(),
                     "alias_name": a.get("alias")}
                    for a in (result.get("new_aliases") or [])],
        # 悬空引用必须进体检 —— 少一条边是这套系统里最贵的缺陷，
        # 而它以前是硬编码的空列表，等于永远不报。
        "unresolved": unresolved_refs(result, known),
    }


def check_candidate(result: dict, chunk_ids: list[str],
                    chunk_text: dict[str, str] | None = None,
                    admitted: set[str] | None = None,
                    known: set[str] | None = None,
                    book_text: str = "") -> Report:
    """体检一批**还没落库**的候选结果。

    *admitted* 是无条件准入的名字（Scout 点名的关键人物）—— 它们没被本批
    引用不算孤儿，得从发现里摘掉，否则回炉会去追一个不存在的问题。

    *known* 是库里已有的节点名 —— 悬空引用判定要用（见 ``graph_view``）。

    *book_text* 给了，引文核对才能区分「锚错块」和「全书根本没有这句」。
    不给时 ``_check_quotes`` 只能报 ``quote_unverifiable``，而那是**阻塞项**
    —— 实测这会让每一批都带着一条永远消不掉的缺陷进回炉，是旧回路跑满三轮
    的直接原因之一。
    """
    return check_graph(graph_view(result, chunk_ids, known), admitted=admitted,
                       chunk_text=chunk_text, book_text=book_text or None)


# ── 证伪趟 ────────────────────────────────────────────────────────────────

def build_verifier_msg(book_title: str, chunk_texts: list[tuple[str, str]],
                       result: dict, roster: list[str]) -> str:
    """**只给结论，不给推理** —— 隔离是这一趟的全部价值所在。"""
    lines = [f"来源：《{book_title}》", ""]
    if roster:
        lines += ["这个世界已有的实体名册（供你判断「是不是同一个人」）：",
                  "  " + "、".join(roster[:80]), ""]
    lines += ["## 本批正文（逐字）", ""]
    for cid, text in chunk_texts:
        lines += [f"=== chunk_id: {cid} ===", text, ""]
    # **chunk_summaries 不是断言，不进证伪趟。** 它是「这一块讲了什么」的
    # 复述，没有 evidence 字段 —— 而证伪器的任务是找「无据 / 张冠李戴」。
    # 实测带着它跑，每批都会被挑出几条摘要措辞问题，回炉 3 轮仍不收敛，
    # 未验证率从偶发变成几乎每批（窄门 12/12、救世主 9/12）。简述本身
    # 另有一层校验（完整性 + 长度），不需要也不应该走对抗证伪。
    candidate = {k: v for k, v in result.items()
                 if k not in ("chunk_summaries", "summaries")}
    lines += ["## 从这批正文里提取出的断言", "",
              json.dumps(candidate, ensure_ascii=False, indent=1)]
    return "\n".join(lines)


async def run_verifier(
    session: aiohttp.ClientSession,
    cfg: Config,
    book_title: str,
    chunk_texts: list[tuple[str, str]],
    result: dict,
    roster: list[str] | None = None,
    log_cb: Callable[[dict], None] | None = None,
) -> dict[str, Any] | None:
    """跑一趟证伪。返回 ``{verdict, challenges, summary}``，**解析不出就返回 None**。

    返回 None 不等于「没问题」—— 调用方必须把它记成 ``inconclusive``。
    """
    cfg = config_for(cfg, "bookgraph.verify")
    sys_prompt = verifier_prompt()
    user_msg = build_verifier_msg(book_title, chunk_texts, result, roster or [])
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user_msg}],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": output_budget(cfg),
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json"}
    t0 = time.monotonic()
    try:
        async with session.post(
            f"{cfg.api_base}/chat/completions", json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                log.warning("Verifier API %s: %s", resp.status, err[:200])
                if log_cb:
                    log_cb({"status": "api_error", "prompt": user_msg,
                            "response": "", "error_detail": err[:300],
                            "latency_ms": int((time.monotonic() - t0) * 1000)})
                return None
            data = json.loads(await resp.text())
            content = data["choices"][0]["message"].get("content") or ""
            parsed = loads_tolerant(content, expect=dict)
            if log_cb:
                # 证伪趟的用量不在 ``usage`` 之外另算 —— 它和提取趟一样贵，
                # 过去这一行不记 token，成本账里一半的钱是看不见的。
                usage = data.get("usage") or {}
                log_cb({"status": "ok" if parsed else "parse_failed",
                        "prompt": user_msg, "response": content,
                        "tokens_in": usage.get("prompt_tokens"),
                        "tokens_out": usage.get("completion_tokens"),
                        "tokens_cached": cached_tokens(data),
                        "reasoning_tokens": reasoning_tokens(data),
                        "reasoning_content": reasoning_text(data),
                        "latency_ms": int((time.monotonic() - t0) * 1000)})
            return parsed
    except Exception as exc:
        log.warning("Verifier failed: %s", exc)
        if log_cb:
            log_cb({"status": "api_error", "prompt": user_msg, "response": "",
                    "error_detail": str(exc),
                    "latency_ms": int((time.monotonic() - t0) * 1000)})
        return None


def round_rank(mechanical: int, challenges: int,
               nodes: int, edges: int) -> tuple[int, int, int]:
    """一轮提取的优劣排序键，**越小越好**。

    字典序而非加权和 —— 这三样的**可信度**根本不同：

    1. ``mechanical`` 机械缺陷：**已证实**的问题（纯函数判定，无逐字原文
       就是无逐字原文，没有解释空间）。
    2. ``challenges`` 证伪质疑：**存疑**的假设（对抗模型提的，可能对也可能误报）。
    3. ``-(nodes+edges)`` 内容量：同分时**偏好更多**。

    等权相加实测是坏的：《我与地坛》三轮全被**第一轮**赢走 —— 回炉让提取
    更丰富（10 节点/23 边 vs 7/13），可挑刺的面随之变大，质疑数上升，于是
    「存疑」压过「证实」，评分不再指向更好的那一轮，整条回炉环变成空转：
    每批白烧两次调用，然后原样丢弃。

    第三项取负是防「说得少就干净」—— 同分时若偏好更少，回炉就会学会把
    东西删掉来消灭缺陷，那是把「撤回」当成了「修正」。
    """
    return (mechanical, challenges, -(nodes + edges))


def challenge_lines(verifier: dict | None) -> list[str]:
    """证伪趟的质疑 → 回给提取器的原话。"""
    if not verifier:
        return []
    out: list[str] = []
    for c in verifier.get("challenges") or []:
        if not isinstance(c, dict):
            continue
        claim = str(c.get("claim") or "").strip()
        if not claim:
            continue
        why = str(c.get("why") or "").strip()
        ev = str(c.get("evidence") or "").strip()
        sev = str(c.get("severity") or "?").strip()
        out.append(f"[证伪·{sev}] {claim}"
                   + (f" —— {why}" if why else "")
                   + (f"　正文：{ev}" if ev else ""))
    return out
