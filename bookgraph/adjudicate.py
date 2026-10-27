"""断言裁决 —— 对一批候选断言逐条判：留、删、改。

**它替换的不是「体检」，是「回炉」。**

旧回路是「提取 → 挑刺 → 把同一批正文再提取一遍」，实测两次都坏：

1. **不收敛。** 窄门 14 批全部跑满 3 轮（只有 1 批提前收），三轮之间的边
   逐字稳定率 19%、观察 1% —— 每一轮都是重新掷骰子，不是修正。评分函数
   只能从中挑一轮，挑哪一轮都是运气。
2. **修的是错的层次。** 证伪提得相当准（「热罗姆」本批正文无据、「装病」
   是母亲的判断冒充事实、关系方向反了、两幕压缩成「临终前随即去世」），
   但这些是**对某一条断言**的意见；重提一遍会把 81% 正确的内容一起扔掉。

所以这里换成一个更窄的动作：**给每条断言编号，让证伪者直接对编号下判决**。
动作只有五种（删 / 改方向 / 改类型 / 改锚点 / 改措辞），全部机械可执行。
正文没问题的部分**一个字都不动** —— 这不是原则，是实测：稳定的输出才谈得上
「最好的一轮」，每轮重写的东西没有可比较的上一版。

留一个后备：修补后仍有**阻塞级**机械缺陷时，走 ``targeted_repair`` ——
只把有缺陷的那几条拿出来重写，不再重提整批。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

import aiohttp

from bookgraph.llm_call import (
    THROTTLE_BACKOFF, cached_tokens, is_throttle, output_budget,
    reasoning_text, reasoning_tokens)
from config import Config
from llm_json import loads_tolerant
from provider_store import config_for
from sub_agent.types import load_prompt

log = logging.getLogger("bookgraph")

_ADJUDICATOR_FALLBACK = """你会收到一批正文和从其中提取的断言（每条有编号）。
逐条判断它在正文里站不站得住，对站不住的给出**可执行的修改**。
严格 JSON：
{"verdict":"upheld|challenged|inconclusive",
 "fixes":[{"id":"E3","action":"drop|reverse|retype|reanchor|rewrite",
           "patch":{},"why":"...","evidence":"正文逐字原文"}],
 "summary":"..."}"""

#: 每个数组的编号前缀。**编号只在本次调用内有效**，不落库。
_PREFIX = {
    "new_nodes": "N",
    "new_edges": "E",
    "new_observations": "O",
    "new_aliases": "A",
    "merge_candidates": "M",
    "quotes": "Q",
}

#: 只有这些动作会被执行。多一个都不认 —— 认不出来的动作静默丢掉比执行错好。
ACTIONS = {"drop", "reverse", "retype", "reanchor", "rewrite"}

#: rewrite 允许改的字段（按数组）。白名单而非黑名单：模型编出来的键
#: 一旦被写进候选，落库层不认识它，等于静默丢字段。
_REWRITABLE = {
    "new_edges": {"kind", "source", "target", "chunk_id", "event_type",
                  "subject", "evidence", "tags"},
    "new_observations": {"field", "value", "event_type", "subject",
                         "evidence_chunk_id", "evidence", "tags"},
    "new_nodes": {"canonical_name", "kind", "why", "evidence"},
    "quotes": {"character_name", "quote_text", "chunk_id", "scene_context",
               "related_nodes"},
    "new_aliases": {"alias", "canonical_name"},
    "merge_candidates": {"name_a", "name_b", "confidence", "reason"},
}

#: 数组 → apply 之后的重新校验要点。改完必须重新体检，见 orchestrator。
_REQUIRED_FIELDS = {
    "new_edges": ("source", "target", "kind"),
    "new_observations": ("node_name", "field", "value"),
    "new_nodes": ("canonical_name", "kind"),
    "quotes": ("character_name", "quote_text"),
    "new_aliases": ("alias", "canonical_name"),
}


def verifier_prompt() -> str:
    return load_prompt("book_adjudicator", _ADJUDICATOR_FALLBACK)


# ── 编号 ──────────────────────────────────────────────────────────────────

def index_assertions(result: dict) -> list[tuple[str, str, dict]]:
    """给每条断言编号：``[(id, array_name, obj), ...]``，顺序即原数组顺序。"""
    out: list[tuple[str, str, dict]] = []
    for arr, prefix in _PREFIX.items():
        for i, obj in enumerate(result.get(arr) or [], 1):
            if isinstance(obj, dict):
                out.append((f"{prefix}{i}", arr, obj))
    return out


def _clip(s: Any, n: int) -> str:
    text = str(s or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def evidence_flags(result: dict, chunk_text: dict[str, str]) -> dict[str, str]:
    """逐条断言的**机械体检**结果：``{断言编号: 问题}``，没问题的不出现。

    裁决者拿到的是一张表，它得靠读正文去发现「这句引文其实不在原文里」——
    而这件事**一秒就能机械判定**。实测一次调用里模型把「他的确疯狂地爱着她」
    抄成「她的确疯狂地爱着她」（同一段通篇主语是「她」），边的方向是对的，
    错的只是证据里的一个字：关系照常落库，逐字校验发现了它，但发现之后
    **没有任何东西把它送到裁决者面前**（``quote_unverifiable`` 非阻塞），
    于是它就那么留在了库里。

    这里把两件事标出来，**只标不改** —— 改法要靠读正文（是引错还是编的）：

    * 证据在**本批正文里逐字找不到**（最贵的一类：关系可能对，依据是编的）
    * **两条断言共用同一句证据**且有不同端点（一句话不可能同时证明两件事）
    """
    norm = {cid: _norm(text) for cid, text in (chunk_text or {}).items()}
    flags: dict[str, str] = {}
    seen_evidence: dict[str, str] = {}

    def _check(aid: str, evidence: Any, anchor: Any, endpoints: str) -> None:
        ev = _norm(str(evidence or ""))
        cid = str(anchor or "")
        if not ev or cid not in norm:
            return
        if ev not in norm[cid]:
            elsewhere = [c for c, text in norm.items() if ev in text]
            if elsewhere:
                flags[aid] = (f"证据不在所锚的块里，逐字在 {elsewhere[0]} 找到了"
                              "（锚点错，不是编的）")
            else:
                flags[aid] = "证据在本批正文里逐字找不到（先核对是否引错，再判断是否 drop）"
        key = ev
        if key in seen_evidence:
            # **这句是提示不是判决。** 原文「我的表姐和表妹」本来就同时
            # 提到两个人，两条关系共用它是对的 —— 实测裁决者对这类提示的
            # 判断是准确的（该改的改、该放过的放过），所以只报不拦。
            flags[aid] = (f"与 {seen_evidence[key]} 共用同一句证据（{endpoints}）"
                          " —— 若该句确实同时提到了双方，忽略这条提示")
        else:
            seen_evidence[key] = aid

    for aid, arr, obj in index_assertions(result):
        if arr == "new_edges":
            _check(aid, obj.get("evidence"), obj.get("chunk_id"),
                   f"{obj.get('source')}→{obj.get('target')}")
        elif arr == "new_observations":
            _check(aid, obj.get("evidence"), obj.get("evidence_chunk_id")
                   or obj.get("chunk_id"), f"{obj.get('node_name')}·{obj.get('field')}")
        elif arr == "quotes":
            quote = obj.get("quote_text")
            cid = str(obj.get("chunk_id") or "")
            if _norm(str(quote or "")) and cid in norm:
                if _norm(str(quote)) not in norm[cid]:
                    flags[aid] = "台词在本块正文里逐字找不到"
    return flags


def render_assertion_table(result: dict, flags: dict[str, str] | None = None) -> str:
    """把候选断言渲染成**带编号的单行表**。

    带编号不是形式主义：证伪者要能说「第 7 条方向反了」。整份 JSON 让它
    用自然语言指代（「阿莉莎避开露希尔那条」），而 ``apply_fixes`` 需要的是
    一个**唯一定位符**。实测旧证伪的输出里，同一条断言有五种叫法。
    """
    flags = flags or {}
    lines: list[str] = []

    def _mark(aid: str) -> str:
        return f"  ⚠ {flags[aid]}" if aid in flags else ""

    for aid, arr, obj in index_assertions(result):
        if arr == "new_nodes":
            lines.append(
                f"[{aid}] 实体「{obj.get('canonical_name','')}」"
                f"({obj.get('kind','?')}) why: {_clip(obj.get('why'), 90)}"
                f" | 原文: {_clip(obj.get('evidence'), 110)}" + _mark(aid))
        elif arr == "new_edges":
            subj = obj.get("subject")
            tag = f"{obj.get('event_type','?')}" + (f"/{subj}" if subj else "")
            lines.append(
                f"[{aid}] {obj.get('source','?')} --{obj.get('kind','?')}--> "
                f"{obj.get('target','?')} [{tag}] chunk={obj.get('chunk_id','?')}"
                f" | 原文: {_clip(obj.get('evidence'), 110)}" + _mark(aid))
        elif arr == "new_observations":
            subj = obj.get("subject")
            tag = f"{obj.get('event_type','?')}" + (f"/{subj}" if subj else "")
            lines.append(
                f"[{aid}] {obj.get('node_name','?')}·{obj.get('field','?')} = "
                f"{_clip(obj.get('value'), 90)} [{tag}]"
                f" chunk={obj.get('evidence_chunk_id','?')}"
                f" | 原文: {_clip(obj.get('evidence'), 110)}" + _mark(aid))
        elif arr == "quotes":
            # **必须带场景说明。** 实测台词归属错得很隐蔽：杰罗姆名下 18 条里
            # 3 条是母亲说的，而 `scene_context` 里明写着「母亲向…抱怨」——
            # 证伪者看不到它，就只能看出「这句像不像他说的」。
            lines.append(
                f"[{aid}] 台词（{obj.get('character_name','?')}）: "
                f"{_clip(obj.get('quote_text'), 90)}"
                f" chunk={obj.get('chunk_id','?')}"
                f" | 场景: {_clip(obj.get('scene_context'), 70)}" + _mark(aid))
        elif arr == "new_aliases":
            lines.append(f"[{aid}] 别名「{obj.get('alias','?')}」→ "
                         f"{obj.get('canonical_name','?')}")
        elif arr == "merge_candidates":
            lines.append(
                f"[{aid}] 合并候选「{obj.get('name_a','?')}」~"
                f"「{obj.get('name_b','?')}」 conf={obj.get('confidence','?')}"
                f" —— {_clip(obj.get('reason'), 70)}")
    return "\n".join(lines)


# ── 提示词拼装 ────────────────────────────────────────────────────────────

def build_adjudicator_msg(book_title: str, chunk_texts: list[tuple[str, str]],
                          result: dict, roster: list[str],
                          flags: dict[str, str] | None = None,
                          rejected: list[dict] | None = None) -> str:
    lines = [f"来源：《{book_title}》", ""]
    if roster:
        lines += ["## 这个世界已有的实体名册（判「是不是同一个人」用）",
                  "  " + "、".join(roster[:80]), ""]
    lines += ["## 本批正文（逐字）", ""]
    for cid, text in chunk_texts:
        lines += [f"=== chunk_id: {cid} ===", text, ""]
    if rejected:
        # 上一轮给出的修改**系统执行不了**。不说清这一点，模型只会照原样再
        # 提一遍 —— 实测 5/14 条卡在这里，全都是「只给 why、不给 patch」。
        lines += ["## 你上一轮提的修改，有一条或多条被系统拒收了", ""]
        for e in rejected[:12]:
            lines.append(f"- [{e.get('id','?')}] {e.get('action','?')}："
                         f"{_clip(e.get('why'), 90)}"
                         f"  → **拒收原因：{_clip(e.get('reason'), 60)}**")
        lines += ["",
                  "拒收不是因为你判错了，而是**修改本身不可执行**：没有 `patch`、"
                  "或 `patch` 里的字段名系统不认识。",
                  "这一次请**按下表的编号**重新给出（前面的条目可能已被删改，"
                  "旧编号不再作数），并且**把新值写进 `patch`**。"
                  "拿不出新值的，改用 `drop`。", ""]
    if flags:
        lines += ["## 机械体检已发现的问题（行尾 ⚠）", "",
                  "下面这些断言，系统已经**机械核对过证据**，不是猜测。"
                  "请优先处理它们：核对原文后给出能直接执行的修改。", ""]
    lines += ["## 待裁决的断言（编号在行首）", "",
              render_assertion_table(result, flags), "",
              f"共 {len(index_assertions(result))} 条。",
              "没有问题的**不要出现在 fixes 里** —— 沉默即通过。"]
    return "\n".join(lines)


async def run_adjudicator(
    session: aiohttp.ClientSession,
    cfg: Config,
    book_title: str,
    chunk_texts: list[tuple[str, str]],
    result: dict,
    roster: list[str] | None = None,
    log_cb: Callable[[dict], None] | None = None,
    flags: dict[str, str] | None = None,
    rejected: list[dict] | None = None,
) -> dict[str, Any] | None:
    """跑一趟裁决。返回 ``{verdict, fixes, summary}``；解析不出返回 None。

    返回 None 不等于「没问题」—— 调用方必须记成 ``inconclusive``。
    """
    cfg = config_for(cfg, "bookgraph.adjudicate")
    sys_prompt = verifier_prompt()
    user_msg = build_adjudicator_msg(book_title, chunk_texts, result,
                                     roster or [], flags, rejected)
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
    # 端点延迟抖动很大（同一提示实测 15s–300s+）。旧实现一次超时就放弃，
    # 那一批直接记 inconclusive —— 实测窄门第 4 批 600s 超时，整批白读。
    # 超时/网络错误重试一次；**解析失败也重试**（不重试的代价是整批断言
    # 无人看过，重试只花一次调用）。
    last_exc: Exception | None = None
    for attempt in range(6):
        if attempt:
            # 限流（429/burst 保护）要等，不能 3 秒后再撞一次 —— 见 explorer
            # 里 _THROTTLE_BACKOFF 的实测数据。其余错误 3/6/12 秒。
            await asyncio.sleep(THROTTLE_BACKOFF[min(attempt - 1, 2)]
                                if is_throttle(last_exc) else 3 * 2 ** (attempt - 1))
        t0 = time.monotonic()
        try:
            async with session.post(
                f"{cfg.api_base}/chat/completions", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=900),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"API {resp.status}: {err[:200]}")
                data = json.loads(await resp.text())
                content = data["choices"][0]["message"].get("content") or ""
                parsed = loads_tolerant(content, expect=dict)
                if log_cb:
                    usage = data.get("usage") or {}
                    log_cb({"status": "ok" if parsed else "parse_failed",
                            "prompt": user_msg, "response": content,
                            "tokens_in": usage.get("prompt_tokens"),
                            "tokens_out": usage.get("completion_tokens"),
                            "tokens_cached": cached_tokens(data),
                            "reasoning_tokens": reasoning_tokens(data),
                            "reasoning_content": reasoning_text(data),
                            "latency_ms": int((time.monotonic() - t0) * 1000)})
                if parsed:
                    return parsed
                last_exc = RuntimeError("parse_failed")
        except Exception as exc:
            last_exc = exc
            log.warning("Adjudicator attempt %d failed: %s", attempt + 1, exc)
            if log_cb:
                log_cb({"status": "api_error", "prompt": user_msg,
                        "response": "", "error_detail": str(exc)[:300],
                        "latency_ms": int((time.monotonic() - t0) * 1000)})
    return None


# ── 执行修改 ──────────────────────────────────────────────────────────────

#: 模型对字段的**别名**。实测它会按自己的心智模型写字段名：
#: 节点写 `description`（协议里叫 `why`）、边写 `object`（协议里叫 `target`）、
#: 观察写 `role_in_story`（把 field 当成节点属性）。这不是错误的理解，
#: 是**命名**问题 —— 照原样拒收等于把它做对的判断整条扔掉。
_PATCH_ALIASES = {
    "new_nodes": {"description": "why", "identity": "why",
                  "name": "canonical_name"},
    # `relation` / `type` 是模型对「关系词」最常见的两种叫法 —— 缺这两个
    # 别名时，改关系词的修改会整条落空（实测重试轮里 `patch.relation`
    # 就被拒成 empty_patch）。
    "new_edges": {"object": "target", "predicate": "kind", "relation": "kind",
                  "type": "kind", "from": "source", "to": "target",
                  "src": "source", "dst": "target"},
    "new_observations": {"content": "value", "text": "value"},
    # 台词没有 `evidence` 字段 —— 台词本身就是证据。裁决者被提示词要求
    # 「把 evidence 改成逐字原文」时，对台词会写成 `patch.evidence`，
    # 而白名单里只有 `quote_text`，于是这条修改被**静默拒绝**
    # （实测一趟里唯一一条裁决就是这么丢的）。这里给它一个落点。
    "quotes": {"evidence": "quote_text", "text": "quote_text"},
}

#: 观察的 field 取值。模型写 `{"role_in_story": "..."}` 时，
#: 意思是「这个字段的值改成这个」。
_OBS_FIELDS = {"personality", "appearance", "motivation", "role_in_story",
               "speaking_style"}


def _clean_patch(arr: str, patch: Any) -> dict:
    if not isinstance(patch, dict):
        return {}
    allowed = _REWRITABLE.get(arr, set())
    aliases = _PATCH_ALIASES.get(arr, {})
    out: dict = {}
    for k, v in patch.items():
        key = aliases.get(k, k)
        if key in allowed:
            out[key] = v
    # 观察：`{"role_in_story": "..."}` → `{"field": "...", "value": "..."}`。
    # 只在**恰好一个**字段名命中时转换 —— 多个键是真正的歧义，不猜。
    if arr == "new_observations" and not out:
        hits = [k for k in patch if k in _OBS_FIELDS]
        if len(hits) == 1:
            out = {"field": hits[0], "value": patch[hits[0]]}
    return out


def apply_fixes(result: dict, fixes: list[dict],
                chunk_ids: set[str] | None = None
                ) -> tuple[dict, list[dict]]:
    """把裁决动作落到候选上。返回 ``(新候选, 动作台账)``。

    **只做机械可判定的动作**：删、换端点、换类型、换锚点、按白名单改字段。
    没有「重写整条」这种动作 —— 那等于把裁决变成了第二次提取。

    *chunk_ids* 给了就校验 reanchor 的落点：锚点不在本批的修改直接拒绝。
    锚错块是这套系统里最会骗人的缺陷（引文逐字为真，只是不在它说的地方），
    不能靠模型自觉。
    """
    by_id = {aid: (arr, obj) for aid, arr, obj in index_assertions(result)}
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in result.items()}
    dropped: dict[str, set[int]] = {}
    ledger: list[dict] = []

    for fix in fixes or []:
        if not isinstance(fix, dict):
            continue
        aid = str(fix.get("id") or "").strip()
        action = str(fix.get("action") or "").strip().lower()
        why = str(fix.get("why") or "").strip()
        entry = {"id": aid, "action": action, "why": why, "applied": False,
                 "reason": ""}
        if aid not in by_id:
            entry["reason"] = "unknown_id"
            ledger.append(entry)
            continue
        if action not in ACTIONS:
            entry["reason"] = "unknown_action"
            ledger.append(entry)
            continue
        arr, obj = by_id[aid]
        if id(obj) in dropped.get(arr, set()):
            entry["reason"] = "already_dropped"
            ledger.append(entry)
            continue

        idx = _index_of(result.get(arr), obj)
        if idx < 0:
            entry["reason"] = "index_lost"
            ledger.append(entry)
            continue

        if action == "drop":
            dropped.setdefault(arr, set()).add(id(obj))
            entry["applied"] = True
            ledger.append(entry)
            continue

        if action == "reverse":
            if arr != "new_edges":
                entry["reason"] = "reverse_on_non_edge"
                ledger.append(entry)
                continue
            out[arr][idx] = dict(out[arr][idx])
            out[arr][idx]["source"], out[arr][idx]["target"] = (
                obj.get("target"), obj.get("source"))
            entry["applied"] = True
            ledger.append(entry)
            continue

        patch = _clean_patch(arr, fix.get("patch"))
        if not patch:
            # retype / reanchor 也允许平铺字段（模型常不写 patch 包一层）
            patch = _clean_patch(arr, {k: v for k, v in fix.items()
                                       if k not in ("id", "action", "why",
                                                    "evidence", "patch")})
        if not patch:
            entry["reason"] = "empty_patch"
            ledger.append(entry)
            continue

        if action == "reanchor" and chunk_ids is not None:
            target = patch.get("chunk_id") or patch.get("evidence_chunk_id")
            if target and target not in chunk_ids:
                entry["reason"] = "anchor_outside_batch"
                ledger.append(entry)
                continue

        new_obj = dict(out[arr][idx])
        new_obj.update(patch)
        missing = [f for f in _REQUIRED_FIELDS.get(arr, ())
                   if not str(new_obj.get(f) or "").strip()]
        if missing:
            entry["reason"] = "patch_breaks_required:" + ",".join(missing)
            ledger.append(entry)
            continue
        out[arr][idx] = new_obj
        entry["applied"] = True
        entry["patch"] = patch
        ledger.append(entry)

    for arr, dead in dropped.items():
        out[arr] = [o for o in out[arr] if id(o) not in dead]

    return out, ledger


def _index_of(seq: list, obj: dict) -> int:
    for i, x in enumerate(seq or []):
        if x is obj:
            return i
    return -1


# ── 机械修（不过模型） ─────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return "".join((s or "").split())


def mechanical_fixes(result: dict, chunk_ids: list[str],
                     chunk_text: dict[str, str] | None = None,
                     book_text: str | None = None) -> tuple[dict, list[dict]]:
    """在送裁决之前，先把**机械可判定**的毛病改掉。

    这些规则都不需要模型判断，而且每一条都对应实测到的走形：

    * **别名必须在全书正文里出现过。** 实测提取器给「杰罗姆」报了别名
      「热罗姆」—— 全书出现 0 次。它是从**别的译本/别的语言**里带来的外部
      知识；一旦落库，别名是全局唯一的，它会永久污染实体解析（读取路径的
      ``locate_node`` 精确别名优先）。这一条只花一次字符串查找。
    * **锚点必须在本批。** 边/观察/引文指向不在本批的 chunk 时，锚定是假的。
      先在正文里找证据落在哪一块 —— 找到了就改指过去，找不到就删（锚点错了
      而证据也对不上，多半是编的）。
    * **同批重复的断言去重。** 同一 (source, kind, target) 在同一批里出现两次
      没有信息增量，只会在度数里灌水。

    返回值形如 ``(新候选, 台账)``，台账的 ``action`` 以 ``auto:`` 开头。
    """
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in result.items()}
    ledger: list[dict] = []
    ids = set(chunk_ids)
    norm_chunks = {cid: _norm(chunk_text.get(cid, "")) for cid in ids} \
        if chunk_text else {}
    book_norm = _norm(book_text) if book_text else ""

    def _locate(evidence: str) -> str | None:
        ev = _norm(evidence)
        if not ev:
            return None
        hits = [cid for cid, text in norm_chunks.items() if ev in text]
        return hits[0] if len(hits) == 1 else None

    def _fix_anchor(obj: dict, anchor_key: str, evidence_key: str,
                    tag: str) -> dict | None:
        """锚点在本批、但证据不在那一块 → 依证据改指到正确的那块。

        这是实测最常见的错误（批首锚点 bug：引文都记在批次的第一块上），
        后果不是「少一条」，是**剧透**：批首章号 ≤ 批内任何块，于是后面章节
        的台词会被 ``max_chapter`` 放行。机械能判，就不该留给模型。
        """
        cid = str(obj.get(anchor_key) or "")
        ev = _norm(str(obj.get(evidence_key) or ""))
        if not cid or not ev or cid not in norm_chunks:
            return None
        if ev in norm_chunks[cid]:
            return None
        moved = _locate(str(obj.get(evidence_key) or ""))
        if moved is None or moved == cid:
            return None
        ledger.append({"id": f"{tag}:{cid}->{moved}",
                       "action": "auto:fix_anchor",
                       "why": "证据不在所锚的块里，在另一块找到了",
                       "applied": True})
        return dict(obj, **{anchor_key: moved})

    # 1) 别名：全书无此写法 → 删
    kept_aliases = []
    for a in out.get("new_aliases") or []:
        name = str(a.get("alias") or "").strip()
        if not name:
            continue
        if book_norm and _norm(name) not in book_norm:
            ledger.append({"id": f"A:{name}", "action": "auto:drop_alias",
                           "why": "全书正文没有这个写法", "applied": True})
            continue
        kept_aliases.append(a)
    if "new_aliases" in out:
        out["new_aliases"] = kept_aliases

    # 2) 边：锚点不在本批 → 依证据重锚，找不到 → 删
    kept_edges = []
    seen_edges: set[tuple] = set()
    for e in out.get("new_edges") or []:
        cid = str(e.get("chunk_id") or "")
        if ids and cid not in ids:
            moved = _locate(str(e.get("evidence") or ""))
            if moved is None:
                ledger.append({"id": f"E:{e.get('source')}->{e.get('target')}",
                               "action": "auto:drop_edge_bad_anchor",
                               "why": f"锚点 {cid} 不在本批，证据也定位不到",
                               "applied": True})
                continue
            e = dict(e, chunk_id=moved)
            ledger.append({"id": f"E:{e.get('source')}->{e.get('target')}",
                           "action": "auto:reanchor_edge",
                           "why": f"{cid} → {moved}", "applied": True})
        else:
            fixed = _fix_anchor(e, "chunk_id", "evidence", "E")
            if fixed is not None:
                e = fixed
        key = (str(e.get("source") or "").strip(),
               str(e.get("kind") or "").strip().lower(),
               str(e.get("target") or "").strip(),
               str(e.get("chunk_id") or ""))
        if key in seen_edges:
            ledger.append({"id": f"E:{key[0]}->{key[2]}", "action": "auto:dedup",
                           "why": "同批同端点同关系重复", "applied": True})
            continue
        seen_edges.add(key)
        kept_edges.append(e)
    if "new_edges" in out:
        out["new_edges"] = kept_edges

    # 3) 观察：锚点不在本批 → 依证据重锚，找不到 → 删
    kept_obs = []
    seen_obs: set[tuple] = set()
    for o in out.get("new_observations") or []:
        cid = str(o.get("evidence_chunk_id") or "")
        if ids and cid not in ids:
            moved = _locate(str(o.get("evidence") or ""))
            if moved is None:
                ledger.append({"id": f"O:{o.get('node_name')}·{o.get('field')}",
                               "action": "auto:drop_obs_bad_anchor",
                               "why": f"锚点 {cid} 不在本批", "applied": True})
                continue
            o = dict(o, evidence_chunk_id=moved)
            ledger.append({"id": f"O:{o.get('node_name')}·{o.get('field')}",
                           "action": "auto:reanchor_obs",
                           "why": f"{cid} → {moved}", "applied": True})
        else:
            fixed = _fix_anchor(o, "evidence_chunk_id", "evidence", "O")
            if fixed is not None:
                o = fixed
        key = (str(o.get("node_name") or "").strip(),
               str(o.get("field") or "").strip(),
               str(o.get("value") or "").strip())
        if key in seen_obs:
            ledger.append({"id": f"O:{key[0]}·{key[1]}", "action": "auto:dedup_obs",
                           "why": "同批同取值重复", "applied": True})
            continue
        seen_obs.add(key)
        kept_obs.append(o)
    if "new_observations" in out:
        out["new_observations"] = kept_obs

    # 4) 引文：锚点不在本批 → 依台词原文重锚
    kept_quotes = []
    for q in out.get("quotes") or []:
        cid = str(q.get("chunk_id") or "")
        if ids and cid not in ids:
            moved = _locate(str(q.get("quote_text") or ""))
            if moved is None:
                ledger.append({"id": f"Q:{q.get('character_name')}",
                               "action": "auto:drop_quote_bad_anchor",
                               "why": f"锚点 {cid} 不在本批且台词定位不到",
                               "applied": True})
                continue
            q = dict(q, chunk_id=moved)
            ledger.append({"id": f"Q:{q.get('character_name')}",
                           "action": "auto:reanchor_quote",
                           "why": f"{cid} → {moved}", "applied": True})
        else:
            fixed = _fix_anchor(q, "chunk_id", "quote_text", "Q")
            if fixed is not None:
                q = fixed
        kept_quotes.append(q)
    if "quotes" in out:
        out["quotes"] = kept_quotes

    # 5) 实体：缺必填字段 → 删（落库层不认识没有名字的节点）
    kept_nodes = []
    for n in out.get("new_nodes") or []:
        if not str(n.get("canonical_name") or "").strip():
            ledger.append({"id": "N:?", "action": "auto:drop_nameless_node",
                           "why": "没有 canonical_name", "applied": True})
            continue
        if not str(n.get("kind") or "").strip():
            n = dict(n, kind="unknown")
            ledger.append({"id": f"N:{n.get('canonical_name')}",
                           "action": "auto:fill_kind",
                           "why": "kind 为空 → unknown", "applied": True})
        kept_nodes.append(n)
    if "new_nodes" in out:
        out["new_nodes"] = kept_nodes

    return out, ledger


def verdict_summary(parsed: dict | None, ledger: list[dict]) -> dict:
    """给进度事件/台账用的紧凑结论。"""
    applied = [e for e in ledger if e["applied"]]
    by_action: dict[str, int] = {}
    for e in applied:
        by_action[e["action"]] = by_action.get(e["action"], 0) + 1
    return {
        "verdict": (str(parsed.get("verdict") or "?") if parsed else "inconclusive"),
        "fixes_proposed": len(ledger),
        "fixes_applied": len(applied),
        "by_action": by_action,
    }
