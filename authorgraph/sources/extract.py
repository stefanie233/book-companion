"""一页一次的 LLM 抽取 —— **不跨页合成，不用世界知识**。

设计稿 03 §四.4。为什么一页一次：多页混在一个 prompt 里，模型会跨页
拼事实、把 A 页的评价写成 B 页的传记，而且 ``evidence_quote`` 再也说不清
来自哪一页。一页一次换来的是**可校验**。

本模块只生成与形状归一，不做真假判断 —— 校验在 ``verify.py``，
落库在 ``pipeline.py``。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import aiohttp

import usage_log
from bookgraph.llm_call import (finish_reason, output_budget, reasoning_text,
                                usage_tokens)
from config import Config
from llm_json import loads_tolerant
from provider_store import config_for
from sub_agent.types import load_prompt

log = logging.getLogger("authorgraph")

#: 允许的 target。与 ``verify.TARGETS`` 同源，但这里多一个 ``voice`` 的
#: 拒绝理由 —— M1 不产声音语料，声音走 04 的独立管线。
TARGETS = ("biography", "bibliography", "relation",
           "poetics", "theme", "reception", "context")
KINDS = ("life_event", "work", "publication", "relation")
_CONF = {"high": 0.85, "medium": 0.6, "low": 0.35}

_FALLBACK = """你在读一个网页页面，为一位作者补充资料。

只输出这一页正文里能逐字引用的事实。每条 claim 必须带 evidence_quote，
它是正文里的连续片段，系统会逐字比对，找不到整条作废。
没有逐字引文的条目不要输出；空数组不是失败。

严格 JSON：
{"claims":[{"target":"biography|bibliography|relation|poetics|theme|reception|context",
 "kind":"life_event|work|publication|relation","year":null,"year_end":null,
 "statement":"","evidence_quote":"","locator":"","confidence":"high|medium|low"}],
 "notes":[]}"""


def extract_prompt() -> str:
    return load_prompt("author_source_extract", _FALLBACK)


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _year(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        y = int(v)
        return y if 1000 <= y <= 2999 else None
    digits = "".join(ch for ch in _s(v) if ch.isdigit())
    if len(digits) >= 3:
        y = int(digits[:4])
        return y if 1000 <= y <= 2999 else None
    return None


def normalize_claims(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """形状与取值归一。**不认识的 target 丢弃并报** —— 不装作没看见。"""
    out: list[dict[str, Any]] = []
    defects: list[str] = []
    for i, c in enumerate((parsed.get("claims") or [])[:24], 1):
        if not isinstance(c, dict):
            defects.append(f"claim#{i}: 不是对象")
            continue
        target = _s(c.get("target"))
        if target not in TARGETS:
            defects.append(f"claim#{i}: 未知 target {target!r}，已丢弃")
            continue
        statement = _s(c.get("statement"))
        quote = _s(c.get("evidence_quote"))
        if not statement or not quote:
            defects.append(f"claim#{i}: 缺 statement/evidence_quote，已丢弃")
            continue
        kind = _s(c.get("kind"))
        if target in ("biography", "bibliography", "relation"):
            if kind not in KINDS:
                kind = {"bibliography": "work", "relation": "relation"}.get(
                    target, "life_event")
        else:
            kind = "life_event"
        conf = _CONF.get(_s(c.get("confidence")).lower(), 0.5)
        out.append({
            "target": target, "kind": kind,
            "year": _year(c.get("year")),
            "year_end": _year(c.get("year_end")),
            "statement": statement, "evidence_quote": quote,
            "locator": _s(c.get("locator")), "confidence": conf,
        })
    notes = [_s(n) for n in (parsed.get("notes") or []) if _s(n)]
    return out, defects + notes


def build_extract_msg(page: dict[str, Any], author: dict[str, Any],
                      existing: list[str] | None = None) -> str:
    """给抽取调用的信。**作者身份 + 这一页 + 已有条目**，别的都不给。"""
    name = _s(author.get("canonical_name"))
    who = [name]
    by, dy = author.get("birth_year"), author.get("death_year")
    if by or dy:
        who.append(f"{by or '?'}–{dy or '?'}")
    if author.get("nationality"):
        who.append(_s(author["nationality"]))
    lines = ["## 作者", "  " + "，".join(who), "",
             "## 这一页",
             f"  标题：{_s(page.get('title')) or '(无题)'}",
             f"  网址：{_s(page.get('url'))}",
             f"  发布者：{_s(page.get('publisher')) or '(未知)'}",
             f"  语言：{_s(page.get('language')) or '(未知)'}", ""]
    if existing:
        lines += ["## 台账里已经记过的（**不要重复**）"]
        lines += [f"  - {s}" for s in existing[:60]]
        lines.append("")
    lines += ["## 正文", _s(page.get("main_text")), ""]
    return "\n".join(lines)


async def extract_page(session: aiohttp.ClientSession, cfg: Config,
                       page: dict[str, Any], author: dict[str, Any], *,
                       existing: list[str] | None = None,
                       log_cb: Callable[[dict], None] | None = None
                       ) -> dict[str, Any]:
    """抽一页。返回 ``{status, claims, defects, notes, meta}``；**不 raise**。"""
    cfg = config_for(cfg, "authorgraph.enrich")
    system_prompt = extract_prompt()
    user_msg = build_extract_msg(page, author, existing)
    body = {"model": cfg.model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_msg}],
            "stream": False, "temperature": 0.1,
            "max_tokens": output_budget(cfg)}
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json"}
    t0 = time.monotonic()
    aid = _s(author.get("author_id"))

    def _emit(status: str, *, response: str = "", reasoning: str = "",
              finish: str = "", error_detail: str = "",
              tokens_in: int = 0, tokens_out: int = 0) -> None:
        usage_log.record(source="authorgraph", model=cfg.model,
                         tokens_in=tokens_in, tokens_out=tokens_out,
                         status=status, ref_id=aid)
        if log_cb:
            log_cb({"status": status, "system": system_prompt,
                    "prompt": user_msg, "response": response,
                    "reasoning_content": reasoning, "finish_reason": finish,
                    "error_detail": error_detail, "model": cfg.model,
                    "latency_ms": int((time.monotonic() - t0) * 1000)})

    try:
        async with session.post(
            f"{cfg.api_base}/chat/completions", json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                err = (await resp.text())[:200]
                log.warning("Author source extract API %s: %s", resp.status, err)
                _emit("api_error", error_detail=err)
                return {"status": "api_error", "claims": [], "defects": [err],
                        "notes": []}
            data = json.loads(await resp.text())
            content = ((data.get("choices") or [{}])[0]
                       .get("message") or {}).get("content") or ""
            reasoning = reasoning_text(data)
            finish = finish_reason(data)
            tokens_in, tokens_out = usage_tokens(data)
    except Exception as exc:                          # noqa: BLE001
        log.warning("Author source extract failed: %s", exc)
        _emit("exception", error_detail=str(exc)[:300])
        return {"status": "exception", "claims": [], "defects": [str(exc)],
                "notes": []}

    parsed = loads_tolerant(content, expect=dict)
    if not parsed:
        _emit("parse_failed", response=content, reasoning=reasoning,
              finish=finish, tokens_in=tokens_in, tokens_out=tokens_out)
        return {"status": "parse_failed", "claims": [],
                "defects": [f"解析失败（finish={finish!r}）"],
                "notes": [], "response": content}
    claims, defects = normalize_claims(parsed)
    _emit("ok", response=content, reasoning=reasoning, finish=finish,
          tokens_in=tokens_in, tokens_out=tokens_out)
    return {"status": "ok", "claims": claims, "defects": defects,
            "notes": [_s(n) for n in (parsed.get("notes") or [])]}
