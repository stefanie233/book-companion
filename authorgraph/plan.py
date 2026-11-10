"""作者采集攻略 —— 读之前的先验。

**攻略不是游记。** 这一层只回答「先去哪、花多少预算、什么先跳过」，
它写的每一条都是假设，永远不能直接变成事实。事实在 ``details.py``
读完之后、带逐字引文才产生。

三条纪律：

1. **模型先验一律 ``grey``。** 白/黑名单只能由用户改 —— 模型说「这本
   没价值」不能变成静默跳过。
2. **先验必须可证伪。** 每条都要写「预期找到什么」和「怎么验」；
   读完后由 ``details.py`` 回写 ``confirmed/refuted``。
3. **身份未定时只给通用条目。** ``provisional/conflict`` 不许生成
   「某书是自传体」这类具体先验，否则会把同名材料算到别人头上。
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
from storage.authors_db import (OBSERVATION_BASES, PLAN_VALUES)

log = logging.getLogger("authorgraph")

_FALLBACK = """你在为一位作者制定采集攻略。只输出假设，不要写事实。

给你这位作者已入库的书（书名、他在书里的位置、目录）。请判断：
哪些书的哪些部分最可能包含「一般百科写不到的细节」——序跋、译者序、
自传体小说里的现实投影、作者自己的散文/访谈。

每条攻略必须写清：
- target: 书的 stable_book_id + "#paratext" / "#body" /
  "#chapter:<标题里的一段原词>"（目录里点名的序跋、附录、年表、致辞，
  即使被标成 body 也能读；关键词必须原样出现在章标题里）
- hypothesis: 预期这里能找到什么（一句话）
- expected_basis: author_statement | paratext | autobiographical_projection |
  fiction_projection | reported | model
- anchor_required: 读完拿什么当依据（没有就留空）
- expected_value: high | medium | low | unknown
- expected_cost: 预计几次调用（整数）
- verification_action: 怎么验（一句话）
- why: 你为什么这么猜

严格 JSON：{"plans":[{...}]}。不要输出事实性陈述；不要凭记忆补内容。
"""


def plan_prompt() -> str:
    from sub_agent.types import load_prompt
    return load_prompt("author_plan", _FALLBACK)


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


def _chapters(db: Any, book_id: str) -> list[dict[str, Any]]:
    if not book_id:
        return []
    try:
        return [dict(r) for r in db.get_conn().execute(
            "SELECT chapter_index, title, chapter_type FROM book_chapters"
            " WHERE book_id=? ORDER BY chapter_index", (book_id,)).fetchall()]
    except Exception as exc:                            # noqa: BLE001
        log.warning("plan 读 chapters 失败 (%s): %s", book_id, exc)
        return []


def book_shape(db: Any, book_id: str) -> tuple[bool, list[str]]:
    """这本书的目录形状：有没有序跋、目录行。"""
    chapters = _chapters(db, book_id)
    has_paratext = any((c.get("chapter_type") or "") in
                       ("preface", "postscript") for c in chapters)
    lines = [f"  ch{c['chapter_index']:>3} [{c.get('chapter_type') or '?'}]"
             f" {c.get('title') or '(无题)'}" for c in chapters[:40]]
    return has_paratext, lines


def deterministic_plan(db: Any, adb: Any, author_id: str, *,
                       write: bool = True) -> list[dict[str, Any]]:
    """零模型也能跑的那部分：序跋必看；正文只在位置明确时进灰名单。"""
    author = adb.get_author(author_id) or {}
    identity = (author.get("identity_status") or "provisional")
    out: list[dict[str, Any]] = []

    def _put(*a: Any, **kw: Any) -> str:
        return adb.upsert_plan(*a, **kw) if write else ""

    for b in adb.books_of(author_id):
        stable = _s(b.get("stable_book_id"))
        book_id = _s(b.get("book_id"))
        if not stable:
            continue
        has_paratext, _ = book_shape(db, book_id)
        if has_paratext:
            pid = _put(
                author_id, target=f"{stable}#paratext",
                hypothesis="序跋/译序/编者说明可能含作者介绍、生活细节与可追的钩子",
                stable_book_id=stable, book_id=book_id, scope="paratext",
                expected_basis="paratext", expected_value="high",
                expected_cost=1,
                verification_action="逐字引文 + 写明说话人（作者/译者/编者）",
                list_type="grey", created_by="rule",
                prompt_version="author-plan-v1")
            out.append({"plan_id": pid, "target": f"{stable}#paratext"})
        # 身份没定就不生成「某书是自传体」这类具体先验 —— 同名会串账。
        if identity != "resolved":
            continue
        pos = _s(b.get("position"))
        read_as = adb.read_as_of(author_id, stable)
        if pos == "narrator" or read_as == "autobiographical":
            pid = _put(
                author_id, target=f"{stable}#body",
                hypothesis="作者以第一人称讲自己的事，可能含百科未收的经历、习惯与语气",
                stable_book_id=stable, book_id=book_id, scope="body",
                expected_basis=("author_statement" if pos == "narrator"
                                else "autobiographical_projection"),
                expected_value="medium",
                anchor_required=("用户标记 read_as=autobiographical"
                                 if read_as == "autobiographical" else ""),
                expected_cost=2,
                verification_action="逐字引文；投射必须带自传体依据锚",
                list_type="grey", created_by="rule",
                prompt_version="author-plan-v1")
            out.append({"plan_id": pid, "target": f"{stable}#body"})
        elif pos == "avatar":
            pid = _put(
                author_id, target=f"{stable}#body",
                hypothesis="自传体小说可能含现实投影，但只能作为投射记录",
                stable_book_id=stable, book_id=book_id, scope="body",
                expected_basis="autobiographical_projection",
                expected_value="low",
                anchor_required="需要自传体声明（序跋/访谈）或用户标记",
                expected_cost=1,
                verification_action="逐字引文 + 依据锚；无锚降级为 fiction_projection",
                list_type="grey", created_by="rule",
                prompt_version="author-plan-v1")
            out.append({"plan_id": pid, "target": f"{stable}#body"})
    return out


def _book_title(db: Any, book_id: str, fallback: str = "") -> str:
    if not book_id:
        return fallback
    try:
        row = db.get_conn().execute(
            "SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
        return (row["title"] if row else "") or fallback
    except Exception:                                   # noqa: BLE001
        return fallback


def build_plan_msg(adb: Any, db: Any, author: dict[str, Any]) -> str:
    aid = author.get("author_id") or ""
    stats = adb.stats(aid)
    lines = [
        "# 作者",
        f"  名字：{author.get('canonical_name') or '(未知)'}",
        f"  身份状态：{author.get('identity_status') or 'provisional'}",
        f"  国籍：{author.get('nationality') or '(未知)'}",
        f"  生卒：{author.get('birth_year') or '?'} — "
        f"{author.get('death_year') or '?'}",
        "",
        "# 已入库的书",
    ]
    for b in adb.books_of(aid):
        stable = _s(b.get("stable_book_id"))
        book_id = _s(b.get("book_id"))
        if not stable:
            continue
        title = _book_title(db, book_id, stable)
        has_paratext, toc = book_shape(db, book_id)
        lines += [
            f"- 《{title}》 stable_book_id={stable}",
            f"  他在书里的位置：{b.get('position') or 'unknown'}"
            f"　role={b.get('role') or 'author'}"
            f"　read_as={adb.read_as_of(aid, stable) or '（未标记）'}",
            f"  有有序跋：{'是' if has_paratext else '否'}",
            "  目录（前 40 章）：", *toc,
        ]
    hooks = adb.hooks_for(aid, status="open", limit=20)
    lines += [
        "", "# 台账已有",
        f"  编年条目：{stats.get('records', 0)}　解释性陈述：{stats.get('claims', 0)}"
        f"　观察：{len(adb.observations_for(aid, limit=1000))}"
        f"　未关闭钩子：{len(hooks)}",
    ]
    if hooks:
        lines.append("  当前钩子：")
        for h in hooks[:10]:
            lines.append(f"  - {h.get('topic') or 'other'}："
                         f"{(h.get('missing') or '')[:80]}")
    lines += ["", "# 输出", "严格 JSON：{\"plans\":[...]}。只写假设，不写事实。"]
    return "\n".join(lines)


def normalize_plan(raw: Any, *, known: dict[str, str],
                   identity: str) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型先验收成落库形状。**只认已入库的书**，只给 grey。"""
    defects: list[str] = []
    out: list[dict[str, Any]] = []
    for r in _rows((raw or {}).get("plans") if isinstance(raw, dict) else raw):
        target = _s(r.get("target"))
        stable, _, scope = target.partition("#")
        if scope.startswith("chapter:"):
            kw = _s(scope.split(":", 1)[1])[:40]
            if not kw:
                defects.append(f"chapter 目标缺标题关键词：{target!r}")
                continue
            scope = f"chapter:{kw}"
        elif scope not in ("paratext", "body"):
            defects.append(f"target 不在已入库书目里：{target!r}")
            continue
        if stable not in known:
            defects.append(f"target 不在已入库书目里：{target!r}")
            continue
        hyp = _s(r.get("hypothesis"))
        if not hyp:
            defects.append("hypothesis 为空")
            continue
        basis = _s(r.get("expected_basis"))
        if basis and basis not in OBSERVATION_BASES:
            defects.append(f"expected_basis 词表外：{basis!r}")
            basis = ""
        if (identity != "resolved"
                and (scope == "body" or scope.startswith("chapter:"))
                and basis == "autobiographical_projection"):
            defects.append("身份未定，拒绝生成具体作品的自传体先验")
            continue
        val = _s(r.get("expected_value"))
        if val not in PLAN_VALUES:
            if val:
                defects.append(f"expected_value 词表外：{val!r} → unknown")
            val = "unknown"
        try:
            cost = max(1, min(int(r.get("expected_cost") or 1), 5))
        except (TypeError, ValueError):
            cost = 1
        out.append({
            "stable_book_id": stable, "book_id": known[stable],
            "target": f"{stable}#{scope}", "scope": scope,
            "hypothesis": hyp, "expected_basis": basis,
            "anchor_required": _s(r.get("anchor_required")),
            "expected_value": val, "expected_cost": cost,
            "verification_action": _s(r.get("verification_action")),
        })
    return out, defects


async def model_plan(session: aiohttp.ClientSession, cfg: Config,
                     adb: Any, db: Any, author_id: str, *,
                     temperature: float = 0.2, write: bool = True,
                     log_cb: Callable[[dict], None] | None = None
                     ) -> dict[str, Any] | None:
    """跑一趟模型先验。``None`` = 没跑成；空数组 = 诚实的「没新想法」。"""
    author = adb.get_author(author_id)
    if not author:
        return None
    identity = (author.get("identity_status") or "provisional")
    known = {_s(b.get("stable_book_id")): _s(b.get("book_id"))
             for b in adb.books_of(author_id) if _s(b.get("stable_book_id"))}
    if not known:
        return {"plans": [], "defects": ["作者没有挂任何书"]}
    cfg = config_for(cfg, "authorgraph.harvest")
    user_msg = build_plan_msg(adb, db, author)
    system_prompt = plan_prompt()
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_msg}],
        "stream": False, "temperature": temperature,
        "max_tokens": output_budget(cfg),
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}",
               "Content-Type": "application/json"}
    t0 = time.monotonic()

    def _emit(status: str, **kw: Any) -> None:
        usage_log.record(
            source="authorgraph", model=cfg.model,
            tokens_in=int(kw.get("tokens_in") or 0),
            tokens_out=int(kw.get("tokens_out") or 0), status=status,
            ref_id=author_id)
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
            timeout=aiohttp.ClientTimeout(total=300),
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
            plans, defects = normalize_plan(parsed, known=known,
                                            identity=identity)
            stored: list[dict[str, Any]] = []
            for p in plans:
                pid = adb.upsert_plan(
                    author_id, target=p["target"], hypothesis=p["hypothesis"],
                    stable_book_id=p["stable_book_id"], book_id=p["book_id"],
                    scope=p["scope"], expected_basis=p["expected_basis"],
                    anchor_required=p["anchor_required"],
                    expected_value=p["expected_value"],
                    expected_cost=p["expected_cost"],
                    verification_action=p["verification_action"],
                    list_type="grey", created_by="model_prior",
                    prompt_version="author-plan-v2") if write else ""
                stored.append(dict(p, plan_id=pid))
            _emit("ok", response=content, reasoning=reasoning, finish=finish,
                  tokens_in=tokens_in, tokens_out=tokens_out)
            return {"plans": stored, "defects": defects}
    except Exception as exc:                            # noqa: BLE001
        log.warning("Author plan failed: %s", exc)
        _emit("exception", error_detail=str(exc)[:300])
        return None

