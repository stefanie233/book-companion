"""从已入库书籍里采「百科不写的细节」—— 观察与钩子。

与 ``harvest.py`` 的分工：

- ``harvest`` 只看序跋，目的是**认作者**（身份、语料、位置）。
- 本模块按攻略看序跋/正文，目的是**采纹理**（习惯、口癖、居所、关系、
  写作现场……）和**埋钩子**（提到了但没说清的事）。

四条机械闸门（模型只负责发现与分类，闸门在这里）：

1. **引文必须逐字命中候选块**。命中不了就重定位；再找不到 → 丢弃。
2. **依据类型必须在词表内**。投射必须有锚（用户 ``read_as`` 或
   ``basis_quote``）；否则降级成 ``fiction_projection``。
3. **用途必须在词表内**。投射不能进 ``biography`` / ``roleplay_texture``，
   只能进 ``dossier``。
4. **说话人不能串**。译者序里的话不能写成作者自述；串了就降级。
"""

from __future__ import annotations

import hashlib
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
from storage.authors_db import (HOOK_ACTIONS, OBSERVATION_BASES,
                                OBSERVATION_TOPICS, OBSERVATION_USES)

log = logging.getLogger("authorgraph")

_FALLBACK = """你在读一本书的序跋/指定正文，为一位作者采集「百科不会写的细节」。
只写你在这段文字里读到的东西；拿不出逐字引文的条目不要写。

观察（observations）收：习惯、口癖、居所与物件、身体、关系张力、写作现场、
对人的判断、反复意象、忌讳。**不收**百科式生平（那归另一道工序）。

每条观察必须给：
- quote：候选文本里的逐字连续片段
- basis：author_statement（作者自述）| paratext（译者/编者/出版方）|
  autobiographical_projection（自传体投射）| fiction_projection（虚构投射）|
  reported（转述他人）
- basis_quote：支撑 basis 的逐字原文（投射必须有；作者自述/序跋可留空）
- speaker：author | translator | editor | publisher | other | unknown
- use：biography（可当经历）| roleplay_texture（可当扮演纹理）|
  dossier（只在档案里展示）
- topic：habit|language|body|dwelling|relation|writing_scene|judgment|motif|fear|other

钩子（hooks）收「提到了，但这段没说清」的事：
- quote：提到它的逐字原文
- known：已知部分
- missing：缺什么
- why：为什么值得追
- suggested_action：web_search | another_book | ask_user

严格 JSON：
{"observations":[...],"hooks":[...],"notes":["这一趟没找到什么"]}
不要给置信度 —— 系统按依据类型推导。不要用你的世界知识补内容。"""


def details_prompt() -> str:
    from sub_agent.types import load_prompt
    return load_prompt("author_details", _FALLBACK)


def _s(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        vals = list(value.values())
        if vals and all(isinstance(x, dict) for x in vals):
            return list(vals)
        return [value]
    return []


def speaker_hint(chapter_title: str, chapter_type: str = "") -> str:
    """标题只给**提示**，最终判据仍是文本本身。"""
    t = chapter_title or ""
    if any(k in t for k in ("译者序", "译序", "译后记", "译者")):
        return "translator"
    if any(k in t for k in ("编者", "编后记", "编选说明",
                            "年表", "生平", "大事记", "附录")):
        return "editor"
    if any(k in t for k in ("出版说明", "再版说明", "版权页")):
        return "publisher"
    if any(k in t for k in ("推荐序", "序一", "序二", "名家")):
        return "other"
    if any(k in t for k in ("自序", "作者序", "后记", "代跋", "前言")):
        return "author"
    return "unknown"


def _confidence(basis: str, speaker: str, hint: str, anchored: bool) -> str:
    """置信等级由依据推导，**不让模型自报数字**。"""
    if basis == "author_statement":
        return "high" if (hint == "author" or speaker == "author") else "medium"
    if basis == "paratext":
        return "high" if speaker in ("translator", "editor", "publisher") else "medium"
    if basis == "reported":
        return "medium" if speaker not in ("", "unknown") else "low"
    if basis == "autobiographical_projection":
        return "medium" if anchored else "low"
    return "low"


def build_details_msg(ident: dict[str, Any], author: dict[str, Any],
                      chunks: list[dict[str, Any]],
                      plans: list[dict[str, Any]],
                      hooks: list[dict[str, Any]],
                      read_as: str = "") -> str:
    lines = [
        "# 这本书",
        f"  书名：{ident.get('title') or '(未知)'}",
        f"  署名：{ident.get('author') or '(未知)'}",
        f"  作者位置：{author.get('position') or 'unknown'}",
        f"  用户标记的读法：{read_as or '（未标记）'}",
        "",
        "# 本次攻略（只在这些范围内找）",
    ]
    for p in plans:
        lines.append(
            f"- {p.get('target')}｜预期：{p.get('hypothesis')}"
            f"｜依据：{p.get('expected_basis') or '未定'}"
            f"｜锚：{p.get('anchor_required') or '无'}")
    if hooks:
        lines += ["", "# 未关闭的钩子（这一段能补就补，补不了别硬写）"]
        for h in hooks[:12]:
            lines.append(f"- {h.get('topic') or 'other'}：{h.get('missing')}"
                         f"（原话：{(h.get('quote') or '')[:60]}）")
    lines += ["", "# 候选文本", ""]
    for c in chunks:
        hint = speaker_hint(c.get("chapter_title") or "",
                            c.get("chapter_type") or "")
        lines += [
            f"=== chunk_id: {c['chunk_id']} ===",
            f"[ch{c.get('chapter_index')} 《{c.get('chapter_title') or ''}》"
            f" 分类={c.get('chapter_type') or '?'} 说话人提示={hint}]",
            c.get("text") or "",
            "",
        ]
    lines += ["", "# 输出", "严格 JSON；没有就返回空数组，不要硬写。"]
    return "\n".join(lines)


def _locate(quote: str, cid: str,
            chunk_text: dict[str, str]) -> tuple[str, bool, bool]:
    """返回 ``(chunk_id, 是否找到, 是否发生重定位)``。"""
    q = _norm_ws(quote)
    if not q:
        return cid, False, False
    if cid in chunk_text and q in _norm_ws(chunk_text[cid]):
        return cid, True, False
    hit = next((k for k, t in chunk_text.items()
                if q in _norm_ws(t)), None)
    return (hit, True, True) if hit else (cid, False, False)


def _anchor_ok(basis_quote: str, chunk_text: dict[str, str]) -> bool:
    q = _norm_ws(basis_quote)
    return bool(q) and any(q in _norm_ws(t) for t in chunk_text.values())


_PROJECTIONS = ("autobiographical_projection", "fiction_projection")
_THIRD_PARTY = ("translator", "editor", "publisher")


def normalize_details(raw: Any, chunk_text: dict[str, str],
                      chunk_meta: dict[str, dict[str, Any]], *,
                      read_as: str = "") -> tuple[dict[str, Any], list[str]]:
    """把模型输出收成可落库形状。**所有降级都报进 defects。**"""
    defects: list[str] = []
    d = raw if isinstance(raw, dict) else {}
    observations: list[dict[str, Any]] = []

    for r in _rows(d.get("observations")):
        st = _s(r.get("statement"))
        quote = _s(r.get("quote"))
        if not st:
            continue
        if not quote:
            defects.append(f"observation 无引文：{st[:24]!r}")
            continue
        cid = _s(r.get("chunk_id"))
        cid, found, moved = _locate(quote, cid, chunk_text)
        if not found:
            defects.append(f"fabricated_quote：{quote[:24]!r} 不在候选块里")
            continue
        if moved:
            defects.append(f"reanchor {_s(r.get('chunk_id'))[:8]}→{cid[:8]}")
        meta = chunk_meta.get(cid) or {}
        hint = meta.get("speaker_hint") or "unknown"

        topic = _s(r.get("topic")) or "other"
        if topic not in OBSERVATION_TOPICS:
            defects.append(f"topic 词表外：{topic!r} → other")
            topic = "other"
        basis = _s(r.get("basis"))
        if basis not in OBSERVATION_BASES:
            defects.append(f"basis 词表外或缺失：{basis!r} → 丢弃")
            continue
        use = _s(r.get("use")) or "dossier"
        if use not in OBSERVATION_USES:
            defects.append(f"use 词表外：{use!r} → dossier")
            use = "dossier"
        speaker = _s(r.get("speaker")) or (
            hint if hint != "unknown" else "unknown")
        basis_quote = _s(r.get("basis_quote"))
        if basis_quote and not _anchor_ok(basis_quote, chunk_text):
            defects.append(f"basis_quote 不在候选块里，已清空：{basis_quote[:24]!r}")
            basis_quote = ""
        if basis in _PROJECTIONS and (hint in _THIRD_PARTY
                                      or speaker in _THIRD_PARTY):
            defects.append(f"projection 来自第三方（{hint}）→ 丢弃：{st[:24]!r}")
            continue
        if basis == "author_statement" and (hint in _THIRD_PARTY
                                            or speaker in _THIRD_PARTY):
            who = speaker if speaker in _THIRD_PARTY else hint
            defects.append(f"自述出现在 {who} 的文字里 → paratext：{st[:24]!r}")
            basis = "paratext"
            speaker = who
        if basis == "paratext" and speaker in ("", "author", "unknown"):
            if hint in _THIRD_PARTY:
                speaker = hint
            else:
                defects.append(f"paratext 缺说话人 → reported：{st[:24]!r}")
                basis, speaker = "reported", "unknown"
        anchored = read_as == "autobiographical" or bool(basis_quote)
        if basis == "autobiographical_projection" and not anchored:
            defects.append(f"投射无依据锚 → fiction_projection：{st[:24]!r}")
            basis, use = "fiction_projection", "dossier"
        if basis == "fiction_projection":
            use = "dossier"
        if use == "biography" and basis in _PROJECTIONS:
            defects.append(f"投射不能当经历 → dossier：{st[:24]!r}")
            use = "dossier"
        if use == "roleplay_texture" and basis in _PROJECTIONS:
            defects.append(f"投射不能进扮演 → dossier：{st[:24]!r}")
            use = "dossier"
        if use == "roleplay_texture" and basis == "reported":
            defects.append(f"转述不是作者口吻 → dossier：{st[:24]!r}")
            use = "dossier"
        observations.append({
            "chunk_id": cid, "chapter_index": meta.get("chapter_index"),
            "chunk_index": meta.get("chunk_index"), "topic": topic,
            "statement": st, "quote": quote, "basis": basis,
            "basis_quote": basis_quote, "speaker": speaker, "use": use,
            "confidence": _confidence(basis, speaker, hint, anchored),
            "content_hash": hashlib.sha1(
                (chunk_text.get(cid) or "").encode("utf-8")).hexdigest()[:16],
        })

    hooks: list[dict[str, Any]] = []
    seen_hooks: set[tuple[str, str, str]] = set()
    for h in _rows(d.get("hooks")):
        quote = _s(h.get("quote"))
        missing = _s(h.get("missing"))
        if not missing:
            continue
        if not quote:
            defects.append(f"hook 无原话：{missing[:24]!r}")
            continue
        cid = _s(h.get("chunk_id"))
        cid, found, moved = _locate(quote, cid, chunk_text)
        if not found:
            defects.append(f"hook 引文不在候选块里：{quote[:24]!r}")
            continue
        if moved:
            defects.append(f"hook reanchor → {cid[:8]}")
        topic = _s(h.get("topic")) or "other"
        if topic not in OBSERVATION_TOPICS:
            defects.append(f"hook topic 词表外：{topic!r} → other")
            topic = "other"
        action = _s(h.get("suggested_action")) or "web_search"
        if action not in HOOK_ACTIONS:
            defects.append(f"hook action 词表外：{action!r} → web_search")
            action = "web_search"
        key = (cid, quote, missing)
        if key in seen_hooks:
            continue
        seen_hooks.add(key)
        meta = chunk_meta.get(cid) or {}
        hooks.append({
            "chunk_id": cid, "chapter_index": meta.get("chapter_index"),
            "topic": topic, "quote": quote, "known": _s(h.get("known")),
            "missing": missing, "why": _s(h.get("why")),
            "suggested_action": action,
            "priority": "high" if _s(h.get("why")) else "medium",
        })

    notes = [_s(x) for x in (d.get("notes") or []) if _s(x)] \
        if isinstance(d.get("notes"), list) else []
    return {"observations": observations, "hooks": hooks,
            "notes": notes}, defects


async def extract_details(session: aiohttp.ClientSession, cfg: Config,
                          ident: dict[str, Any], author: dict[str, Any],
                          chunks: list[dict[str, Any]],
                          plans: list[dict[str, Any]],
                          hooks: list[dict[str, Any]],
                          *, read_as: str = "", temperature: float = 0.2,
                          log_cb: Callable[[dict], None] | None = None
                          ) -> dict[str, Any] | None:
    """跑一趟细节采集。``None`` = 没跑成；空数组 = 诚实回答。"""
    if not chunks:
        return {"observations": [], "hooks": [], "notes": ["没有候选文本"],
                "defects": []}
    cfg = config_for(cfg, "authorgraph.harvest")
    user_msg = build_details_msg(ident, author, chunks, plans, hooks, read_as)
    system_prompt = details_prompt()
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_msg}],
        "stream": False, "temperature": temperature,
        "max_tokens": output_budget(cfg),
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json"}
    chunk_text = {c["chunk_id"]: c.get("text") or "" for c in chunks}
    chunk_meta = {c["chunk_id"]: {
        "chapter_index": c.get("chapter_index"),
        "chunk_index": c.get("chunk_index"),
        "chapter_title": c.get("chapter_title") or "",
        "speaker_hint": speaker_hint(c.get("chapter_title") or "",
                                     c.get("chapter_type") or ""),
    } for c in chunks}
    t0 = time.monotonic()

    def _emit(status: str, **kw: Any) -> None:
        usage_log.record(
            source="authorgraph", model=cfg.model,
            tokens_in=int(kw.get("tokens_in") or 0),
            tokens_out=int(kw.get("tokens_out") or 0), status=status,
            ref_id=str(ident.get("stable_book_id") or ""))
        if log_cb:
            log_cb({"status": status, "system": system_prompt,
                    "prompt": user_msg, "response": kw.get("response") or "",
                    "reasoning_content": kw.get("reasoning") or "",
                    "finish_reason": kw.get("finish") or "",
                    "error_detail": kw.get("error_detail") or "",
                    "model": cfg.model,
                    "latency_ms": int((time.monotonic() - t0) * 1000)})

    try:
        async with session.post(
            f"{cfg.api_base}/chat/completions", json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
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
            result, defects = normalize_details(
                parsed, chunk_text, chunk_meta, read_as=read_as)
            result["defects"] = defects
            result["chunk_text"] = chunk_text
            _emit("ok", response=content, reasoning=reasoning, finish=finish,
                  tokens_in=tokens_in, tokens_out=tokens_out)
            return result
    except Exception as exc:                            # noqa: BLE001
        log.warning("Author details failed: %s", exc)
        _emit("exception", error_detail=str(exc)[:300])
        return None


def store_details(adb: Any, ident: dict[str, Any], author_id: str,
                  result: dict[str, Any], *, plan_id: str = "",
                  model: str = "", prompt_version: str = "author-details-v1"
                  ) -> dict[str, Any]:
    """落观察与钩子。观察与钩子**都带逐字证据**；钩子不升格成事实。"""
    stable = _s(ident.get("stable_book_id")) or _s(ident.get("book_id"))
    book_id = _s(ident.get("book_id"))
    book_sid = adb.book_source_id(stable) if stable else ""
    obs_added = hooks_added = 0
    try:
        for o in (result.get("observations") or []):
            oid = adb.add_observation(
                author_id, stable_book_id=stable, book_id=book_id,
                statement=o.get("statement") or "", quote=o.get("quote") or "",
                chunk_id=o.get("chunk_id") or "",
                chapter_index=o.get("chapter_index"),
                chunk_index=o.get("chunk_index"), topic=o.get("topic"),
                basis=o.get("basis"), basis_quote=o.get("basis_quote"),
                speaker=o.get("speaker"), use=o.get("use"),
                status="accepted", confidence=o.get("confidence"),
                prompt_version=prompt_version, model=model,
                content_hash=o.get("content_hash"))
            if oid:
                obs_added += 1
                adb.add_evidence(
                    author_id, "observation", oid, source_id=book_sid or None,
                    quote=o.get("quote") or "",
                    locator=f"chunk:{o.get('chunk_id') or ''}",
                    relation="supports",
                    confidence=0.8 if o.get("confidence") == "high" else
                    (0.6 if o.get("confidence") == "medium" else 0.35))
        for h in (result.get("hooks") or []):
            hid = adb.add_hook(
                author_id, stable_book_id=stable, book_id=book_id,
                quote=h.get("quote") or "", missing=h.get("missing") or "",
                known=h.get("known") or "", why=h.get("why") or "",
                chunk_id=h.get("chunk_id") or "",
                chapter_index=h.get("chapter_index"), topic=h.get("topic"),
                suggested_action=h.get("suggested_action"),
                priority=h.get("priority") or "medium",
                prompt_version=prompt_version)
            if hid:
                hooks_added += 1
                if h.get("quote"):
                    adb.add_evidence(
                        author_id, "hook", hid, source_id=book_sid or None,
                        quote=h.get("quote") or "",
                        locator=f"chunk:{h.get('chunk_id') or ''}",
                        relation="contextualizes", confidence=0.4)
    finally:
        if plan_id:
            adb.mark_plan_result(
                plan_id, accepted=obs_added, rejected=len(result.get("defects") or []),
                status="done", collapsed_by="")
    return {"observations_added": obs_added, "hooks_added": hooks_added,
            "defects": result.get("defects") or [],
            "notes": result.get("notes") or []}


