"""写作侧的**只读**选择器 —— 按当前场景挑候选，不认识 WriteGraph。

职责边界（设计稿 06 §三）：

- 这里只读 ``authors.db``，返回数据；打分/裁剪属于 ``writegraph/voice_lens``。
- **不做注入决定**。它只保证「给出去的东西是合规的」：正文层、
  ``voice_domain`` 是小说/非虚构、``transferable=1``、``source_bound`` 不进。

M4 的 ``voice_lens`` 会在这里的结果上再排序和压缩。把两道闸分开，
是为了让「谁能被注入」这条规则只有一个出口（``eligible_traits``）。
"""

from __future__ import annotations

from typing import Any

from authorgraph.voice.vocab import LENS_LAYERS

#: 写作透镜允许的声音域。``paratext/public/mixed/unknown`` 一律不进 ——
#: 序跋写得再像也不是小说文风（设计稿 04 §五）。
LENS_DOMAINS = ("fiction", "nonfiction")


def eligible_traits(adb: Any, author_id: str, *, limit: int = 12,
                    per_dimension: bool = True) -> list[dict[str, Any]]:
    """能进写作透镜的 trait。**唯一出口** —— 别在调用侧再写一遍过滤。

    ``per_dimension`` 默认每个维度只留一条（置信度最高的）。库里同一个
    维度会有多条 —— 两种说法、或不同书的判断 —— 那是**账**，不是
    「注入十条同义句」的理由。需要全量做 UI 归组时传 False。
    """
    rows = adb.voice_traits(author_id, statuses=("active",),
                            domains=LENS_DOMAINS, transferable_only=True,
                            limit=max(limit * 4, 40) if per_dimension
                            else limit * 2)
    if per_dimension:
        seen: set[str] = set()
        picked: list[dict[str, Any]] = []
        for r in rows:                    # 已按 confidence/strength 降序
            dim = r.get("dimension") or ""
            if dim in seen:
                continue
            seen.add(dim)
            picked.append(r)
        rows = picked
    return rows[:limit]


def select_for_scene(adb: Any, author_id: str,
                     scene_profile: dict[str, Any] | None = None, *,
                     limit: int = 12, trait_limit: int = 8
                     ) -> dict[str, Any]:
    """按场景取候选。返回 ``{samples, traits, scene, warnings}``。

    ``scene_profile`` 的键与设计稿 06 一致：``scene_type / pov / period /
    emotion / pace / dialogue_density / keywords``。缺的键按「不限」处理。
    """
    sp = dict(scene_profile or {})
    warnings: list[str] = []
    author_id = (sp.get("author_id") or author_id or "").strip()
    if not author_id:
        return {"samples": [], "traits": [], "scene": sp,
                "warnings": ["没有 author_id"]}

    scene_type = (sp.get("scene_type") or "").strip()
    period = (sp.get("period") or "").strip()
    rows = adb.voice_corpus_for_scene(
        author_id, layers=LENS_LAYERS, scene_type=scene_type,
        period=period, limit=max(limit * 4, 20))
    # 场景/时期是**偏好**，不是硬门。很多作者只有三类场景，硬筛会让
    # 「选了作者却一段例证都没有」。先放宽时期，再放宽场景；放宽要报。
    if not rows and period:
        rows = adb.voice_corpus_for_scene(
            author_id, layers=LENS_LAYERS, scene_type=scene_type,
            period="", limit=max(limit * 4, 20))
        if rows:
            warnings.append(f"没有「{period}」时期的样本，已放宽时期")
    if not rows and scene_type:
        rows = adb.voice_corpus_for_scene(
            author_id, layers=LENS_LAYERS, scene_type="",
            period="", limit=max(limit * 4, 20))
        if rows:
            warnings.append("没有与本章场景对应的样本，已按相近场景排序")

    keywords = [str(k).strip() for k in (sp.get("keywords") or [])
                if str(k).strip()]
    want_pov = (sp.get("pov") or "").strip()
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        tag = 1.0 if scene_type and r.get("scene_type") == scene_type else 0.0
        kw = 0.0
        if keywords:
            hit = sum(1 for k in keywords if k in (r.get("excerpt") or ""))
            kw = min(1.0, hit / max(len(keywords), 1))
        pd = 1.0 if period and r.get("period") == period else 0.0
        pov = 1.0 if want_pov and r.get("pov") == want_pov else 0.0
        tier = int(r.get("source_tier") or 3)
        quality = {1: 1.0, 2: 0.9, 3: 0.75}.get(tier, 0.5)
        score = 0.40 * tag + 0.25 * kw + 0.15 * pd + 0.10 * pov + 0.10 * quality
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1].get("id") or 0))
    samples = []
    for score, r in scored[:limit]:
        samples.append({
            "corpus_id": r.get("id"),
            "excerpt": r.get("excerpt") or "",
            "scene_type": r.get("scene_type") or "",
            "scene_note": r.get("scene_note") or "",
            "narrative_layer": r.get("narrative_layer") or "",
            "pov": r.get("pov") or "", "period": r.get("period") or "",
            "chapter_index": r.get("chapter_index"),
            "stable_book_id": r.get("stable_book_id") or "",
            "content_hash": r.get("content_hash") or "",
            "source_tier": r.get("source_tier"),
            "score": round(score, 3),
        })
    traits = eligible_traits(adb, author_id, limit=trait_limit)
    if not samples:
        warnings.append("这位作者还没有正文声音语料（只有序跋/访谈或未采集）")
    if not traits:
        warnings.append("这位作者还没有可迁移的文风特征")
    return {"samples": samples, "traits": traits, "scene": sp,
            "warnings": warnings}


__all__ = ["LENS_DOMAINS", "LENS_LAYERS", "eligible_traits", "select_for_scene"]
