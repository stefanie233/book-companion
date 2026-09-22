"""LLM formatter for BookGraph query results.

Pure functions: dict -> markdown. No LLM, no DB, no I/O.
Output is compact markdown with chapter anchors, optimized for LLM
consumption - dense, located, schema-free (SQL column names never leak;
Chinese labels used instead).

Stability contract: output must be byte-identical for identical input.
Every iteration uses ``sorted`` over lists - never bare ``set`` or
``dict`` ordering. This is required for system-prompt prefix caching:
the role-play kernel (built on ``format_node``) lives in the static
region and must not vary across turns when the graph is unchanged.

Division of labor:
  - GraphDB layer: max_chapter filtering + future-count computation
  - This layer: presentation only (no filtering, no counting beyond
    what the dict already carries)
"""

from __future__ import annotations

import datetime

# ── Structured field labels (hide SQL column names) ─────────────────────────

_FIELD_LABELS = {
    "personality": "性格",
    "appearance": "外貌",
    "motivation": "动机",
    "role_in_story": "叙事功能",
    "speaking_style": "口吻",
}

_EVENT_TYPE_LABELS = {
    "fact": "事实",
    "perspective": "视角",
    "opinion": "观点",
}

# ── observation scoring (trust × temporal decay) ─────────────────────────────
# score = trust × 0.5^(age/half_life) — exponential half-life decay.
# Half-life in days; 0 = disabled (trust only, no decay).
_OBSERVATION_DECAY_HALF_LIFE_DAYS = 30


def _observation_score(obs: dict, half_life_days: int = _OBSERVATION_DECAY_HALF_LIFE_DAYS) -> float:
    """Trust-weighted score with optional temporal decay.

    Returns float in [0, 1]. Higher = more trustworthy and recent.
    Falls back to trust_score alone if updated_at is missing or unparseable.
    """
    trust = float(obs.get("trust_score", 0.5))
    if half_life_days <= 0:
        return trust

    updated = obs.get("updated_at")
    if not updated:
        return trust

    try:
        # SQLite datetime('now') format: "YYYY-MM-DD HH:MM:SS"
        dt = datetime.datetime.strptime(str(updated)[:19], "%Y-%m-%d %H:%M:%S")
        age_days = (datetime.datetime.now() - dt).total_seconds() / 86400.0
        decay = 0.5 ** (age_days / half_life_days)
        return trust * decay
    except (ValueError, TypeError):
        return trust


# ── search ───────────────────────────────────────────────────────────────────

def format_search(results: list[dict], query: str = "") -> str:
    """Format ``GraphDB.search_all`` results as compact markdown."""
    if not results:
        if query:
            return f"未找到与「{query}」匹配的实体。"
        return "未找到匹配实体。"

    lines = [f"## 搜索结果 ({len(results)} 项)", ""]
    for r in results:
        name = r.get("name") or r.get("canonical_name") or "?"
        kind = r.get("kind", "?")
        ch = r.get("first_appearance_chapter")
        ch_tag = f" [ch{ch}]" if ch is not None else ""
        desc = (r.get("description") or "").strip()
        desc = desc[:120] + "…" if len(desc) > 120 else desc
        if desc:
            lines.append(f"- **{name}**{ch_tag} {kind} - {desc}")
        else:
            lines.append(f"- **{name}**{ch_tag} {kind}")
    return "\n".join(lines)


# ── node detail ──────────────────────────────────────────────────────────────

def format_node(detail: dict, max_chapter: int | None = None) -> str:
    """Format a node detail dict as compact markdown.

    Args:
        detail: result from ``GraphDB.get_node_detail()``. Relations and
                quotes are already max_chapter-filtered by the DB layer;
                ``future_relations_count`` / ``future_quotes_count`` carry
                the spoiler-gated counts.
        max_chapter: reader's current progress. Used to split the
                     chapter_timeline into seen vs. future and to render
                     the "后续" section. None = global mode (no split).
    """
    name = detail.get("canonical_name", "?")
    kind = detail.get("kind", "?")
    first_ch = detail.get("first_appearance_chapter")
    ch_tag = f" [ch{first_ch}]" if first_ch is not None else ""
    desc = (detail.get("description") or "").strip()

    lines = [f"## {name}{ch_tag} {kind}"]
    if desc:
        lines.append(desc)
    lines.append("")

    # ── Identity block ───────────────────────────────────────────────────
    identity_lines: list[str] = []
    aliases = detail.get("aliases") or []
    if aliases:
        identity_lines.append(f"- 别名: {', '.join(aliases)}")
    if identity_lines:
        lines.append("### 身份")
        lines.extend(identity_lines)
        lines.append("")

    # ── Observations (event model: fact / perspective / opinion layers) ─
    observations = detail.get("observations") or []
    if observations:
        lines.append(f"### 观察 ({len(observations)} 条)")
        for ob in observations[:20]:
            field_label = _FIELD_LABELS.get(ob.get("field", ""), ob.get("field", "?"))
            val = ob.get("value", "")
            etype = ob.get("event_type", "perspective")
            type_tag = _EVENT_TYPE_LABELS.get(etype, etype)
            subject = ob.get("subject")
            subj_tag = f"({subject})" if subject else ""
            ch = ob.get("chapter")
            ch_tag = f" ch{ch}" if ch is not None else ""
            lines.append(f"- [{type_tag}{subj_tag}{ch_tag}] {field_label}: {val}")
        if len(observations) > 20:
            lines.append(f"- …还有 {len(observations) - 20} 条观察")
        f_obs = detail.get("future_observations_count", 0)
        if f_obs:
            lines.append(f"- …后续还有 {f_obs} 条观察（读者尚未读到）")
        lines.append("")

    # ── Relations ────────────────────────────────────────────────────────
    relations = detail.get("relations") or []
    if relations:
        lines.append(f"### 关系 ({len(relations)} 条)")
        for r in relations[:20]:
            direction = r.get("direction", "out")
            r_kind = r.get("kind", "?")
            other = r.get("other_name", "?")
            rch = r.get("chapter")
            rch_tag = f" ch{rch}" if rch is not None else ""
            etype = r.get("event_type", "fact")
            type_tag = _EVENT_TYPE_LABELS.get(etype, etype)
            subject = r.get("subject")
            subj_tag = f"({subject})" if subject else ""
            tag = f"{r_kind}{rch_tag}·{type_tag}{subj_tag}"
            if direction == "out":
                lines.append(f"- {name} --[{tag}]--> {other}")
            else:
                lines.append(f"- {other} --[{tag}]--> {name}")
        if len(relations) > 20:
            lines.append(f"- …还有 {len(relations) - 20} 条关系")
        lines.append("")

    # ── Quotes (characters mainly) ───────────────────────────────────────
    quotes = detail.get("quotes") or []
    if quotes:
        lines.append(f"### 经典台词 ({len(quotes)} 条)")
        for q in quotes[:10]:
            text = q.get("text", "")
            ctx = (q.get("context") or "").strip()
            qch = q.get("chapter")
            qch_tag = f" ch{qch}" if qch is not None else ""
            ctx_tag = f" ({ctx})" if ctx else ""
            lines.append(f'- "{text}"{ctx_tag}{qch_tag}')
        if len(quotes) > 10:
            lines.append(f"- …还有 {len(quotes) - 10} 条台词")
        lines.append("")

    # ── Timeline + future summary ────────────────────────────────────────
    timeline = detail.get("chapter_timeline") or []
    if timeline:
        timeline_sorted = sorted(set(timeline))
        if max_chapter is not None:
            seen = [c for c in timeline_sorted if c <= max_chapter]
            future = [c for c in timeline_sorted if c > max_chapter]
            seen_str = ", ".join(f"ch{c}" for c in seen) if seen else "无"
            lines.append("### 出场")
            lines.append(f"{seen_str}  (读者当前: ch{max_chapter})")
            if future:
                future_str = ", ".join(f"ch{c}" for c in future)
                f_rel = detail.get("future_relations_count", 0)
                f_q = detail.get("future_quotes_count", 0)
                f_obs = detail.get("future_observations_count", 0)
                lines.append("")
                lines.append(f"### 后续 (ch{max_chapter + 1}+)")
                lines.append(f"还有 {len(future)} 次出场 ({future_str})。")
                extras = []
                if f_obs:
                    extras.append(f"{f_obs} 条观察")
                if f_rel:
                    extras.append(f"{f_rel} 条关系")
                if f_q:
                    extras.append(f"{f_q} 条台词")
                if extras:
                    lines.append(f"未展开: {'、'.join(extras)}。")
                lines.append(f'查看完整 -> graph_node("{name}", max_chapter=-1)')
        else:
            lines.append("### 出场")
            lines.append(", ".join(f"ch{c}" for c in timeline_sorted))

    return "\n".join(lines)


# ── context (PRIMARY - composes search + node + baked trace) ─────────────────

def format_context(context: dict, max_chapter: int | None = None) -> str:
    """Format graph_context result: entry points + relations + baked
    paths + quotes, all in one focused view.

    One call gives the LLM enough to answer without chaining
    node/trace queries. Paths are "baked" here so the agent doesn't
    need to discover a standalone trace tool.
    """
    question = context.get("question", "")
    entries = context.get("entries", [])
    paths = context.get("paths", [])
    quotes = context.get("quotes", [])

    lines = ["## 图谱上下文", ""]
    if question:
        lines.append(f"**问题:** {question}")
        lines.append("")

    # Entry points
    if entries:
        lines.append("### 入口实体")
        for e in entries:
            name = e.get("canonical_name", "?")
            kind = e.get("kind", "?")
            ch = e.get("first_appearance_chapter")
            ch_tag = f" [ch{ch}]" if ch is not None else ""
            desc = (e.get("description") or "").strip()[:100]
            if desc:
                lines.append(f"- **{name}**{ch_tag} {kind} - {desc}")
            else:
                lines.append(f"- **{name}**{ch_tag} {kind}")
        lines.append("")

    # Relations from entries (deduped)
    seen_rel: set[tuple] = set()
    all_relations: list[tuple[str, dict]] = []
    for e in entries:
        name = e.get("canonical_name", "?")
        for r in e.get("relations", [])[:10]:
            # Dedup by edge endpoints (sorted pair) + kind + chapter, so
            # the same edge isn't shown twice from each endpoint's view.
            other = r.get("other_name", "?")
            pair = tuple(sorted([name, other]))
            key = (pair, r.get("kind"), r.get("chapter"))
            if key not in seen_rel:
                seen_rel.add(key)
                all_relations.append((name, r))
    if all_relations:
        lines.append(f"### 关系 ({len(all_relations)} 条)")
        for name, r in all_relations[:15]:
            rkind = r.get("kind", "?")
            other = r.get("other_name", "?")
            rch = r.get("chapter")
            rch_tag = f" ch{rch}" if rch is not None else ""
            etype = r.get("event_type", "fact")
            type_tag = _EVENT_TYPE_LABELS.get(etype, etype)
            subject = r.get("subject")
            subj_tag = f"({subject})" if subject else ""
            tag = f"{rkind}{rch_tag}·{type_tag}{subj_tag}"
            if r.get("direction") == "out":
                lines.append(f"- {name} --[{tag}]--> {other}")
            else:
                lines.append(f"- {other} --[{tag}]--> {name}")
        if len(all_relations) > 15:
            lines.append(f"- …还有 {len(all_relations) - 15} 条关系")
        lines.append("")

    # Baked paths (trace baked in)
    if paths:
        lines.append("### 关系链")
        for p in paths:
            hops = p.get("hops") or []
            if not hops:
                continue
            chain = hops[0].get("from", "?")
            for h in hops:
                hch = h.get("chapter")
                hch_tag = f" ch{hch}" if hch is not None else ""
                chain += f' --[{h.get("kind", "?")}{hch_tag}]--> {h.get("to", "?")}'
            lines.append(f"- {chain}")
        lines.append("")

    # Quotes
    if quotes:
        lines.append(f"### 相关引文 ({len(quotes)} 条)")
        for q in quotes:
            text = q.get("text", "")
            qch = q.get("chapter")
            qch_tag = f" ch{qch}" if qch is not None else ""
            char = q.get("character", "")
            char_tag = f" ({char})" if char else ""
            lines.append(f'- "{text}"{char_tag}{qch_tag}')
        lines.append("")

    return "\n".join(lines)


# ── trace ────────────────────────────────────────────────────────────────────

def format_trace(path: dict) -> str:
    """Format find_path result as a step-by-step relation path."""
    if path.get("error"):
        return path["error"]

    hops = path.get("hops") or []
    from_name = path.get("from", "?")
    to_name = path.get("to", "?")

    lines = [f"## 关系路径: {from_name} -> {to_name}", ""]

    if not hops:
        if from_name == to_name:
            lines.append("起点和终点是同一实体。")
        else:
            lines.append(path.get("note", "未找到路径。"))
        return "\n".join(lines)

    lines.append(f"_{len(hops)} 跳_")
    lines.append("")
    for i, h in enumerate(hops, 1):
        hch = h.get("chapter")
        hch_tag = f" ch{hch}" if hch is not None else ""
        lines.append(f"{i}. {h.get('from', '?')} --[{h.get('kind', '?')}{hch_tag}]--> {h.get('to', '?')}")

    # Compact chain summary
    chain = hops[0].get("from", "?")
    for h in hops:
        chain += f' --[{h.get("kind", "?")}]--> {h.get("to", "?")}'
    lines.append("")
    lines.append(f"完整链: {chain}")

    return "\n".join(lines)


# ── quotes ───────────────────────────────────────────────────────────────────

def format_quotes(quotes: list[dict], character: str = "") -> str:
    """Format a quote list as compact markdown."""
    if not quotes:
        if character:
            return f"图谱中未找到 {character} 的台词记录。"
        return "未找到台词记录。"

    lines = [f"## 经典台词 ({len(quotes)} 条)", ""]
    for q in quotes:
        text = q.get("text", "")
        ctx_str = (q.get("context") or "").strip()
        qch = q.get("chapter")
        qch_tag = f" ch{qch}" if qch is not None else ""
        ctx_tag = f" ({ctx_str})" if ctx_str else ""
        lines.append(f'- "{text}"{ctx_tag}{qch_tag}')
    return "\n".join(lines)


# ── timeline ─────────────────────────────────────────────────────────────────

def format_timeline(detail: dict, max_chapter: int | None = None) -> str:
    """Format a node's chapter timeline (from get_node_detail).

    Shows seen chapters up front and the future summary when in
    immersive mode - same spoiler-aware split as format_node, but
    timeline-only (no relations/quotes) for the lightweight
    graph_timeline tool.
    """
    name = detail.get("canonical_name", "?")
    kind = detail.get("kind", "?")
    timeline = detail.get("chapter_timeline") or []

    lines = [f"## {name} ({kind}) 出场时间线", ""]

    if not timeline:
        lines.append("无出场章节记录。")
        return "\n".join(lines)

    timeline_sorted = sorted(set(timeline))
    if max_chapter is not None:
        seen = [c for c in timeline_sorted if c <= max_chapter]
        future = [c for c in timeline_sorted if c > max_chapter]
        seen_str = ", ".join(f"ch{c}" for c in seen) if seen else "无"
        lines.append(f"已见出场 ({len(seen)} 章, 读者当前 ch{max_chapter}): {seen_str}")
        if future:
            future_str = ", ".join(f"ch{c}" for c in future)
            lines.append(f"后续出场 ({len(future)} 章, ch{max_chapter + 1}+): {future_str}")
    else:
        lines.append(f"全部出场 ({len(timeline_sorted)} 章): " + ", ".join(f"ch{c}" for c in timeline_sorted))

    return "\n".join(lines)


# ── role-play kernel (identity + minimal extension, static region) ──────────

def format_role_kernel(detail: dict, max_chapter: int | None = None) -> str:
    """Format a character's role-play kernel as compact markdown.

    Pure function - no LLM. Lives in the system prompt static region
    for prefix caching: identity + minimal extension (top-3 relations,
    top-3 quotes) + knowledge boundary. Deep detail is fetched
    on-demand via graph_context / graph_quotes / graph_trace.

    ``detail`` comes from ``GraphDB.get_node_detail()`` (already
    max_chapter-filtered). The knowledge-boundary line is the
    spoiler-aware soft constraint: it tells the agent not to
    proactively reach beyond the reader's current progress.
    """
    name = detail.get("canonical_name", "?")
    first_ch = detail.get("first_appearance_chapter")
    ch_tag = f" [ch{first_ch}]" if first_ch is not None else ""
    desc = (detail.get("description") or "").strip()

    lines = [f"## 角色身份: {name}{ch_tag}"]
    if desc:
        lines.append(desc)
    lines.append("")

    # Identity
    identity_lines: list[str] = []
    aliases = detail.get("aliases") or []
    if aliases:
        identity_lines.append(f"- 别名: {', '.join(aliases)}")
    if identity_lines:
        lines.append("### 身份")
        lines.extend(identity_lines)
        lines.append("")

    # Key observations: facts + latest perspective per field (not all, not a conclusion)
    observations = detail.get("observations") or []
    if observations:
        facts = [o for o in observations if o.get("event_type") == "fact"]
        persp_by_field: dict[str, dict] = {}
        for o in observations:
            if o.get("event_type") == "perspective":
                f = o.get("field", "")
                cur = persp_by_field.get(f)
                if cur is None or (o.get("chapter") or -1) > (cur.get("chapter") or -1):
                    persp_by_field[f] = o
        key_obs = facts + list(persp_by_field.values())
        if key_obs:
            lines.append("### 关键观察")
            for ob in sorted(key_obs, key=lambda x: (-_observation_score(x), x.get("chapter") is None, x.get("chapter") or 0))[:5]:
                field_label = _FIELD_LABELS.get(ob.get("field", ""), ob.get("field", "?"))
                val = ob.get("value", "")
                etype = ob.get("event_type", "perspective")
                type_tag = _EVENT_TYPE_LABELS.get(etype, etype)
                subject = ob.get("subject")
                subj_tag = f"({subject})" if subject else ""
                lines.append(f"- [{type_tag}{subj_tag}] {field_label}: {val}")
            lines.append("")

    # Minimal extension: top-3 relations + top-3 quotes
    relations = detail.get("relations") or []
    if relations:
        lines.append("### 关键关系")
        for r in relations[:3]:
            rkind = r.get("kind", "?")
            other = r.get("other_name", "?")
            rch = r.get("chapter")
            rch_tag = f" ch{rch}" if rch is not None else ""
            etype = r.get("event_type", "fact")
            type_tag = _EVENT_TYPE_LABELS.get(etype, etype)
            subject = r.get("subject")
            subj_tag = f"({subject})" if subject else ""
            tag = f"{rkind}{rch_tag}·{type_tag}{subj_tag}"
            if r.get("direction") == "out":
                lines.append(f"- {name} --[{tag}]--> {other}")
            else:
                lines.append(f"- {other} --[{tag}]--> {name}")
        lines.append("")

    quotes = detail.get("quotes") or []
    if quotes:
        lines.append("### 经典台词")
        for q in quotes[:3]:
            text = q.get("text", "")
            qch = q.get("chapter")
            qch_tag = f" ch{qch}" if qch is not None else ""
            ctx_str = (q.get("context") or "").strip()
            ctx_tag = f" ({ctx_str})" if ctx_str else ""
            lines.append(f'- "{text}"{ctx_tag}{qch_tag}')
        lines.append("")

    # Knowledge boundary (spoiler-aware soft constraint)
    lines.append("### 知识边界")
    if max_chapter is not None:
        lines.append(f"沉浸模式 · 读者当前 ch{max_chapter} · 仅知悉第 {max_chapter} 章及之前的内容。")
        lines.append("勿主动越界查询后续；读者未提及的后续情节，你尚不知晓。")
    else:
        lines.append("全局模式 · 全知视角，可引用全书任意位置的信息。")

    return "\n".join(lines)
