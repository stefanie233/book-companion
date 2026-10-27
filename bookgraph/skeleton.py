"""骨架趟 —— 在逐批提取之前，先把「这个世界有谁、谁和谁是什么关系」建出来。

**为什么要在上游做这一趟。** 逐批提取看得见的是五块正文加一份名字索引，看不见
的是**结构**：谁是长女、谁是弟弟、舅舅是谁的舅舅。实测窄门 8 章，人工 gold 45
条关键关系里只命中了 25 条（55.6%），漏掉的**几乎全是骨架**：三组姐妹、三组
父女父子、夫妻、收养、谁当过兵、谁在哪读书。批次提取器把注意力花在「这一场谁
对谁说了什么」，这是任务本身决定的 —— 它拿到的单位就是一幕戏。

骨架趟的做法正好相反：**一次看完整本书，只输出小而稳的东西** —— 实体（规范名、
别称、一句话身份）和结构关系。产物体积小（几十条），不受输出预算挤压；而且它
是全局的，跨场景的同一人才可能被认成同一个人。

它跑在批次之前，所以批次拿到的不只是一份名字表，而是**已经规范过的世界状态**：
全名、别称、身份说明、亲缘边。这正是「上游拦截」—— 后面所有的消歧、证伪、
合并，成本都花在补这一层没做好的事上。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

import aiohttp

from bookgraph.invariants import RELATION_KINDS, finality_hit, normalize_kind
from bookgraph.llm_call import (
    cached_tokens, is_throttle, output_budget, reasoning_text,
    reasoning_tokens, retry_delay)
from config import Config
from llm_json import loads_tolerant
from provider_store import config_for
from sub_agent.types import load_prompt

log = logging.getLogger("bookgraph")

_SKELETON_FALLBACK = """你在读一本书的正文，只做一件事：认清书里有哪些实体、
他们之间是什么关系。输出严格 JSON：
{"entities":[{"name","kind","aliases","identity","chapter","evidence"}],
 "relations":[{"source","kind","target","chapter","evidence","detail"}]}
实体 kind 只用 character/place/organization/work/concept/object；
关系 kind 只用 parent_of/sibling_of/spouse_of/relative_of/adopted_by/
friend_of/ally_of/rival_of/enemy_of/mentor_of/student_of/colleague_of/
servant_of/patron_of/loves/hates/desires/fears/trusts/distrusts/admires/
despises/envies/pities/protects/harms/betrays/deceives/obeys/commands/
rescues/abandons/pursues/avoids/writes_to/speaks_with/confides_in/advises/
reproaches/accuses/thanks/lives_in/visits/travels_to/born_in/dies_in/marries。
每条都要逐字 evidence。拿不准就不写。"""

#: 一段一次调用最多喂多少字。整本窄门 5.8 万字一次吃完；更长的书按章切组，
#: 前一组找到的实体名会作为名册传给后一组（同一条「不许换叫法」的约束）。
MAX_CHARS_PER_CALL = 110_000

ENTITY_KINDS = {"character", "place", "organization", "work", "concept",
                "object", "event"}

#: 模型写出来的近义说法 → 词表里的词。**不认识的值原样保留并上报**，
#: 不装作没判定（见 CLAUDE.md「模型输出宽容三层」）。
_KIND_ALIASES = {
    "parent": "parent_of", "father_of": "parent_of", "mother_of": "parent_of",
    "child_of": "parent_of", "sibling": "sibling_of", "sister_of": "sibling_of",
    "brother_of": "sibling_of", "spouse": "spouse_of", "husband_of": "spouse_of",
    "wife_of": "spouse_of", "married_to": "marries", "friend": "friend_of",
    "love": "loves", "hate": "hates", "fear": "fears", "trust": "trusts",
    "sibling_of": "sibling_of",
}


def skeleton_prompt() -> str:
    return load_prompt("book_skeleton", _SKELETON_FALLBACK)


# ── 拼消息 ────────────────────────────────────────────────────────────────

def group_chapters(chapter_blocks: list[tuple[int, str, str]],
                   max_chars: int = MAX_CHARS_PER_CALL) -> list[list]:
    """按字数把 (chapter_index, title, text) 分组，不拆章。"""
    groups: list[list] = []
    cur: list = []
    cur_chars = 0
    for block in chapter_blocks:
        n = len(block[2])
        if cur and cur_chars + n > max_chars:
            groups.append(cur)
            cur, cur_chars = [], 0
        cur.append(block)
        cur_chars += n
    if cur:
        groups.append(cur)
    return groups


def build_skeleton_msg(book_title: str, book_author: str,
                       blocks: list[tuple[int, str, str]],
                       roster: list[str] | None = None,
                       part: int = 1, total_parts: int = 1,
                       collection: bool = False) -> str:
    lines = [f"来源：《{book_title}》（作者：{book_author}）"]
    if collection:
        # 集类书里「母亲」在第 1 篇和第 5 篇是两个世界的人。骨架是全书视角，
        # 最容易被这件事骗到 —— 不给这句话，它会把各篇的人物连成一家。
        lines += ["", "**这是一部集类书（短篇/散文/随笔集）：各篇只共享纸张，"
                  "不共享人物。** 只有当正文明确说「是同一个人」时，才把"
                  "不同篇里的名字连起来；拿不准就当成两个不同的人，"
                  "在 identity 里写清是哪一篇的。"]
    if total_parts > 1:
        lines.append(f"这是全书的第 {part}/{total_parts} 部分。"
                     "只依据下面这部分正文判断；"
                     "别的部分里已经有的人物，沿用名册里的叫法。")
    if roster:
        lines += ["", "## 已确认的实体名册（沿用这些写法，不要另起名字）",
                  "  " + "、".join(roster[:120])]
    lines += ["", "## 正文（每章开头标注 chapter_index）", ""]
    for ch_idx, title, text in blocks:
        lines += [f"=== chapter_index: {ch_idx} | {title} ===", text, ""]
    return "\n".join(lines)


# ── 形状宽容 ──────────────────────────────────────────────────────────────

def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        # 模型常把列表再包一层 {"entities": {...}} 或返回单条
        return [v]
    return []


def normalize_skeleton(parsed: dict | None) -> tuple[list[dict], list[dict],
                                                    list[str]]:
    """把模型输出收成 ``(entities, relations, notes)``。

    形状不对**照报**：``notes`` 里记下被丢掉的东西，不静默吞。
    """
    if not isinstance(parsed, dict):
        return [], [], ["输出不是对象"]
    # 有时整份包在 {"skeleton": {...}} 里
    for key in ("skeleton", "result", "data"):
        inner = parsed.get(key)
        if isinstance(inner, dict) and ("entities" in inner or "relations" in inner):
            parsed = inner
            break

    notes: list[str] = []
    entities: list[dict] = []
    seen_names: set[str] = set()
    for raw in _as_list(parsed.get("entities") or parsed.get("nodes")):
        if not isinstance(raw, dict):
            notes.append(f"实体不是对象: {type(raw).__name__}")
            continue
        name = str(raw.get("name") or raw.get("canonical_name") or "").strip()
        if not name:
            notes.append("实体缺 name")
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        kind = str(raw.get("kind") or "unknown").strip().lower()
        if kind not in ENTITY_KINDS:
            norm_kind, _ = normalize_kind(kind)
            kind = norm_kind if norm_kind in ENTITY_KINDS else "unknown"
            if kind == "unknown":
                notes.append(f"未识别实体 kind: {raw.get('kind')}")
        aliases = [str(a).strip() for a in _as_list(raw.get("aliases"))
                   if str(a).strip() and str(a).strip() != name]
        entities.append({
            "name": name, "kind": kind, "aliases": aliases,
            "identity": str(raw.get("identity") or raw.get("description")
                            or "").strip(),
            "chapter": _as_int(raw.get("chapter")),
            "evidence": str(raw.get("evidence") or "").strip(),
        })

    relations: list[dict] = []
    for raw in _as_list(parsed.get("relations") or parsed.get("edges")):
        if not isinstance(raw, dict):
            notes.append(f"关系不是对象: {type(raw).__name__}")
            continue
        src = str(raw.get("source") or "").strip()
        tgt = str(raw.get("target") or "").strip()
        kind = str(raw.get("kind") or "").strip().lower()
        kind = _KIND_ALIASES.get(kind, kind)
        if not src or not tgt:
            notes.append(f"关系缺端点: {src}/{tgt}")
            continue
        if src == tgt:
            continue
        if kind not in RELATION_KINDS:
            notes.append(f"词表外关系 kind: {raw.get('kind')}")
            # **原样保留** —— 不认识的词是数据，丢掉才是损失。
        relations.append({
            "source": src, "target": tgt, "kind": kind,
            "chapter": _as_int(raw.get("chapter")),
            "evidence": str(raw.get("evidence") or "").strip(),
            "detail": str(raw.get("detail") or "").strip(),
        })
    return entities, relations, notes


# ── 调用 ──────────────────────────────────────────────────────────────────

async def run_skeleton(
    session: aiohttp.ClientSession,
    cfg: Config,
    book_title: str,
    book_author: str,
    blocks: list[tuple[int, str, str]],
    roster: list[str] | None = None,
    part: int = 1,
    total_parts: int = 1,
    collection: bool = False,
    log_cb: Callable[[dict], None] | None = None,
) -> dict[str, Any] | None:
    cfg = config_for(cfg, "bookgraph.skeleton")
    sys_prompt = skeleton_prompt()
    user_msg = build_skeleton_msg(book_title, book_author, blocks, roster,
                                  part, total_parts, collection)
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
    # 骨架是**一次调用定全书**：它失败，后面所有批次就没有规范名与亲缘边。
    # 旧实现在一次 429 上就直接返回 None —— 整套上游拦截白做。限流要等，
    # 解析失败也要重试（模型偶尔写坏 JSON，重放常常就好）。
    last_exc: Exception | None = None
    throttled = 0
    for attempt in range(6):
        if attempt:
            await asyncio.sleep(retry_delay(attempt, throttled, base=3.0))
        t0 = time.monotonic()
        try:
            async with session.post(
                f"{cfg.api_base}/chat/completions", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=900),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    last_exc = RuntimeError(f"API {resp.status}: {err[:200]}")
                    if is_throttle(last_exc):
                        throttled += 1
                    log.warning("Skeleton API %s（第 %d 次）: %s",
                                resp.status, attempt + 1, err[:160])
                    continue
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
            if is_throttle(exc):
                throttled += 1
            log.warning("Skeleton failed（第 %d 次）: %s", attempt + 1, exc)
    if log_cb:
        log_cb({"status": "api_error", "prompt": user_msg, "response": "",
                "error_detail": str(last_exc)[:300],
                "latency_ms": 0})
    return None


# ── 落库 ──────────────────────────────────────────────────────────────────

#: 骨架关系里哪些是**解读**（其余按事实落库）。骨架趟不输出 `event_type`，
#: 这里按关系词机械判定。让「谁爱谁」以 `fact` 落库，等于把解读冒充真相 ——
#: 而这正是角色卡上 `[事实]` / `[视角]` 标签的来源，也是下游按可信度降权的依据。
_PERSPECTIVE_KINDS = frozenset({
    "loves", "hates", "desires", "fears", "trusts", "distrusts",
    "admires", "despises", "envies", "pities",
    "protects", "harms", "betrays", "deceives", "obeys", "commands",
    "rescues", "abandons", "pursues", "avoids", "cares_for",
    "friend_of", "ally_of", "rival_of", "enemy_of", "mentor_of",
    "student_of", "confides_in", "advises", "reproaches", "accuses",
    "thanks",
})


def _norm_ws(s: str) -> str:
    return "".join((s or "").split())


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _chapter_of(chunk_id: str | None, chunks: list[dict]) -> int | None:
    if not chunk_id:
        return None
    for c in chunks:
        if c.get("chunk_id") == chunk_id:
            return _as_int(c.get("chapter_index"))
    return None


def locate_chunk(evidence: str, chapter_index, chunks: list[dict]) -> str | None:
    """把一条断言锚到块：先按证据逐字找（限同章），再退回该章第一块。

    **章号可能是字符串**（模型常把 `13` 写成 `"13"`），不转换就永远对不上，
    整条关系会变成「无锚点」—— 而剧透门对无锚点一律不展示，等于这条关系
    在读者面前不存在。所以这里先归一，再逐级退让：
    同章找证据 → 全书找证据 → 该章第一块。
    """
    ch = _as_int(chapter_index)
    ev = _norm_ws(evidence)
    same_ch = [c for c in chunks if c.get("chapter_index") == ch]
    if ev:
        for c in same_ch:
            if ev in _norm_ws(c.get("content") or ""):
                return c["chunk_id"]
        hits = [c["chunk_id"] for c in chunks
                if ev in _norm_ws(c.get("content") or "")]
        if len(hits) == 1:
            return hits[0]
    if same_ch:
        return same_ch[0]["chunk_id"]
    return None


def record_skeleton(graph_db, book_id: str, entities: list[dict],
                    relations: list[dict], chunks: list[dict]) -> dict:
    """把骨架写进图。返回统计（供事件台账）。

    实体先写、关系后写 —— 关系端点要靠 ``name_to_node_id`` 解析，实体不在
    库里就整条落空。**没声明的端点兜底建节点**（kind=unknown 并上报），
    与批次提取器的准入规则一致：宁可说「不知道」，不丢边。
    """
    stats = {"entities": 0, "aliases": 0, "relations": 0,
             "unresolved_endpoints": [], "unknown_kinds": [],
             # 身份说明会**无条件**进角色卡（不受剧透门约束）。提示词要求
             # 「不写结局」，模型仍可能写 —— 这里数出来，别让它静默过去。
             "identity_warnings": [],
             # 骨架关系**不经过裁决环**（它看的是整本书，裁决者只看一批），
             # 于是它是全链路里唯一一处「证据没人核」的写入。实测 40 条里
             # 有 1 条是把两句话拼起来的假引文。核不了就报出来 —— 不静默。
             "evidence_unverified": []}
    name_to_id: dict[str, str] = {}
    book_norm = "".join(_norm_ws(c.get("content") or "") for c in chunks)

    for ent in entities:
        ident = ent.get("identity") or ""
        hit = finality_hit(ident)
        chunk_id = locate_chunk(ent.get("evidence", ""), ent.get("chapter"),
                                chunks)
        if hit:
            # 带上锚点章号：落在末章的多半是「读者读到这里才该知道的事」，
            # 落在开头的才是真泄露。不分章报，警告会被噪音淹掉。
            stats["identity_warnings"].append({
                "name": ent["name"], "word": hit,
                "chapter": (ent.get("chapter") or 0),
                "first_chunk_chapter": _chapter_of(chunk_id, chunks),
            })
        node_id = graph_db.upsert_node(
            book_id, ent["name"], ent["kind"] or "unknown",
            description=ent.get("identity") or None,
            first_chunk_id=chunk_id,
        )
        name_to_id[ent["name"]] = node_id
        stats["entities"] += 1
        for alias in ent.get("aliases") or []:
            if graph_db.add_alias(book_id, alias, node_id):
                stats["aliases"] += 1

    for rel in relations:
        ev = _norm_ws(rel.get("evidence") or "")
        if ev and ev not in book_norm:
            # 关系照建（结构比一句引文值钱），但**记下来**：这条证据不是
            # 逐字原文。拼接、改写、跨段合并都归到这里。
            stats["evidence_unverified"].append({
                "source": rel["source"], "kind": rel["kind"],
                "target": rel["target"], "evidence": (ev[:60]),
            })
        src_id = name_to_id.get(rel["source"]) or graph_db.name_to_node_id(
            book_id, rel["source"])
        tgt_id = name_to_id.get(rel["target"]) or graph_db.name_to_node_id(
            book_id, rel["target"])
        for raw, holder in ((rel["source"], "src"), (rel["target"], "tgt")):
            if (src_id if holder == "src" else tgt_id):
                continue
            node_id = graph_db.upsert_node(book_id, raw, "unknown")
            if holder == "src":
                src_id = node_id
            else:
                tgt_id = node_id
            stats["unresolved_endpoints"].append(raw)
        if rel["kind"] not in RELATION_KINDS:
            stats["unknown_kinds"].append(rel["kind"])
        chunk_id = locate_chunk(rel.get("evidence", ""), rel.get("chapter"),
                                chunks)
        interpreted = rel["kind"] in _PERSPECTIVE_KINDS
        if graph_db.add_edge(
            book_id, src_id, tgt_id, rel["kind"], chunk_id=chunk_id,
            event_type="perspective" if interpreted else "fact",
            subject="LLM提取" if interpreted else None,
            tags=["骨架"] + ([rel["detail"]] if rel.get("detail") else []),
            evidence_text=rel.get("evidence") or "",
        ):
            stats["relations"] += 1
    return stats
