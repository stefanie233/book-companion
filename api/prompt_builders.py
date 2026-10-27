"""System-prompt builders for reading / role-play / author modes.

Pure functions: they take db/config/persona values and return prompt text.
Split out of api_server.py so the chat route and the console usage meter
can share them without importing the web layer.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_APP_ROOT = Path(__file__).resolve().parents[1]


_ROLEPLAY_DIRECTIVES = """## 扮演规则

### 身份
1. 第一人称，你就是这个角色。不知道的事就说不知道，不编。

### 怎么说（优先级高于下面的角色资料）
2. **你在说话，不是写文章。** 一般两到五句，全文不超过 300 字。对方只说了几个字，也给他一个完整的回应，不要用一句话打发。
3. **全篇最多一次「不是X，是Y」**（含"你以为…其实""看似…实则"）。其余时候直接说 Y。
4. **不要每段都以金句收尾。** 能截图的漂亮话，全文至多一句。
5. **不要先报幕**（我读了两遍／说句实话／让我想想），直接从内容接。
6. **引用原文要少。** 一回合最多一处，说"我那时写过"就够，不要改写成格言。
7. **不造景。** 不写天气、光线、"那天下午"这类开场。从对方说的话接。
8. **允许说不清、改口、跑题。**「我说不好」「也许是」「不对，也不全是这样」。

### 情绪与安全
9. 触及角色的痛苦记忆或敏感话题，可以回避、沉默、转移话题。

### 知识边界
10. 沉浸模式下，读者未提及的后续情节你尚不知晓，不要主动透露。"""


def _build_author_card(config, db, book_id: str, role_ref: str,
                       name: str, spoiler_gate: int | None) -> str:
    """扮演作者时的内核。与角色卡**并列**，但不共用一条路 ——
    两者的证据在两个库里（``docs/authorgraph/README.md`` §八）。

    ``role_ref`` 是 ``author_id``：给了就先按 id 取（它跨重导不变），
    没给才退回按名字解析。解析失败**不返回空串** —— 空 role_card 会让
    ``_build_reading_prompt`` 悄悄退回书伴人格，而用户明明选了扮演。
    改为给一张「台账里没有这个人」的卡片：扮演照常，但明确告诉模型
    **不要凭训练数据编造他的生平**（那正是作者层存在的理由）。
    """
    if not (config and db):
        return ""
    from authorgraph.card import load_author_card
    from authorgraph.scope import book_identity
    from storage.authors_db import AuthorsDB, resolve_authors_db_path

    ident: dict[str, Any] = {}
    try:
        ident = book_identity(db, book_id)
        adb = AuthorsDB(resolve_authors_db_path(config))
        try:
            out = load_author_card(
                adb, name=name, author_id=role_ref,
                stable_book_id=ident["stable_book_id"],
                reader_chapter=spoiler_gate)
        finally:
            adb.close()
    except Exception as exc:                             # noqa: BLE001
        logging.getLogger("api").warning("author role_card failed: %s", exc)
        out = {"found": False}
    if out.get("found"):
        card = out["card"]
        # 门挡住的条目**要说给模型听**。不说的话，卡片上只有
        # 「（暂无）」和一句「不要编造」—— 模型会答「我不知道他的生平」，
        # 而真相是「还没让你读」。这两句对读者是完全不同的意思。
        note = out.get("gate_note")
        if note:
            card += f"\n\n### 为什么上面这么少\n\n  {note}\n"
        return card
    logging.getLogger("api").warning(
        "author role_card: no ledger for %r (ref=%r)", name, role_ref)
    title = ident.get("title") or book_id
    return (
        f"## 你要扮演的作者：{name}\n\n"
        f"  作者层还没有这个人的台账。你只知道他是《{title}》的作者。\n\n"
        "### 说话时的约束\n\n"
        "  - **不要凭训练数据编造他的生平、著作年份、亲友姓名。**\n"
        "    那些话会说得像真的，而这里没有任何东西能核对。\n"
        "  - 谈他的作品、他的语气、他被引用的话是可以的 ——\n"
        "    涉及具体经历时说「这个我说不上来」。\n"
    )


def _build_book_note(db, book_id: str, reading_chapter) -> str:
    """扮演提示词里的「本书」段：书名 / book_id / 读者上次读到的章。

    这些全是服务端已有的事实。不写进提示词，模型就得先 `library_list`
    把 21 本书列一遍来拿 id —— 实测一次扮演回合为此多花一整轮。
    """
    if not book_id or db is None:
        return ""
    try:
        book = db.get_book(book_id)
        title = (book.title if book else "") or book_id
        lines = ["## 本书", f"- 书名：《{title}》", f"- book_id: {book_id}"]
        if isinstance(reading_chapter, int) and not isinstance(reading_chapter, bool) \
                and reading_chapter >= 0:
            ch = db.get_chapter(book_id, reading_chapter)
            ctitle = (ch.title if ch else "") or ""
            lines.append(
                f"- 读者上次读到的位置：{('《' + ctitle + '》 ') if ctitle else ''}"
                f"chapter_index={reading_chapter}"
            )
        lines.append(
            "- 取原文：library_search(query) 的每条结果都带 book_id / "
            "chapter_index / chunk_index，照抄给 library_read；"
            "书已经在这里了，不要用 library_list 去找书。")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_reading_prompt(runtime, mode: str = "immersive", role_card: str = "",
                          book_note: str = "") -> str:
    """Build the reading-companion system prompt.

    Structure:
      [STATIC] SOUL persona (role-play: boundary-only) + mode overview
               + tool guide + platform hint (companion modes only)
      ─── DYNAMIC BOUNDARY ───
      [DYNAMIC] mode prompt  OR  role card

    In role-play mode (role_card non-empty):
      SOUL identity replaced by role card. Only SOUL's "## 边界" section
      is kept as a safety floor beneath the role card.
    """
    from persona import load_persona

    parts: list[str] = []
    is_role_play = bool(role_card)

    if is_role_play:
        # Role-play: boundary-only safety floor + reader card + role card
        soul_full = ""
        if runtime and hasattr(runtime, "config"):
            soul_full = load_persona(runtime.config)
        boundary = _extract_soul_boundary(soul_full)
        if boundary:
            parts.append(boundary)
        else:
            parts.append(
                "## 边界\n- 我是阅读伴侣，不是心理医生。如用户表现出需要帮助的信号，温和建议专业支持。\n"
                "- 我不碰用户的代码、文件、系统。"
            )

        # ── Reader card placeholder (dual-card model) ──────────────
        parts.append(_ROLEPLAY_DIRECTIVES)
        parts.append(role_card)
        # 「你在哪本书里」是事实，不是该让模型自己查的东西。没有这一段，
        # 模型会先调 library_list 拿 book_id —— 实测每次扮演回合多花一轮。
        if book_note:
            parts.append(book_note)

        parts.append(
            "# 读者卡\n"
            "读者画像待生成（UserGraph 尚未实现）。\n"
            "暂无读者偏好、阅读习惯、历史对话摘要等信息。"
        )
    else:
        # Normal mode: full SOUL
        if runtime and hasattr(runtime, "config"):
            parts.append(load_persona(runtime.config))
        else:
            parts.append(
                "你是「书伴」，一个阅读陪伴助手。你陪用户一起读书，像朋友一样聊书中的人和事。"
            )

    # Interaction-mode overview (companion modes only — role-play is a persona)
    if not is_role_play and _WEB_MODE_OVERVIEW:
        parts.append(_WEB_MODE_OVERVIEW)

    # Tool guide — role-play tools vs companion tools
    if is_role_play:
        parts.append(
            "## 工具使用\n"
            "- graph_context(question): 用自然语言提问查图谱（首选，一次返回入口+关系+引文）\n"
            "- graph_search(query): 搜索书中实体（人物/事件/地点/概念）\n"
            "- graph_node(name): 查实体详情（身份/关系/台词/出场）\n"
            "- graph_quotes(character): 回忆某个角色的经典台词\n"
            "- library_search(query): 全文检索原文，结果自带 book_id / "
            "chapter_index / chunk_index\n"
            "- library_read(book_id, chapter_index, chunk_index): 按坐标读原文块\n"
            "- current_passage(): 读者**正在阅读页**时给出当前段落；"
            "不在阅读页时只给「上次读到的章节开头」或如实说没有位置"
        )
    else:
        parts.append(
            "## 工具使用\n"
            "- graph_context(question): 用自然语言提问查图谱（首选）\n"
            "- graph_search(query): 搜索书中实体（人物/事件/地点/概念）\n"
            "- graph_node(name): 查实体详情（身份/关系/台词/出场）\n"
            "- library_search(query): 全书库全文搜索，返回匹配段落。需要查书中其他位置时用"
            "\n\n"
            "## 记忆工具调用指南\n"
            "- memory_search(query): 搜索持久化记忆库（用户偏好/历史事件/规则），返回预览与关键词\n"
            "- memory_list_by_tag(tag): 按标签浏览记忆\n"
            "- annotation_search(query): 跨书搜索读者收藏的高亮与笔记\n"
            "⚠️ 调用限额：memory_search 与 annotation_search 每轮合计最多调用 3 次。"
            "首次无结果可换关键词重试，但总次数不超过 3 次；仍无结果说明信息不在记忆中，"
            "直接按已有信息回复，不要继续搜索。"
        )

    # Platform hint + deferred-tool discoverability (companion modes only;
    # role-play is a character, not the reading companion)
    if not is_role_play:
        try:
            from runtime import _PLATFORM_HINT
            if _PLATFORM_HINT:
                parts.append(_PLATFORM_HINT)
        except Exception:
            pass
        if runtime and hasattr(runtime, "tool_registry"):
            try:
                deferred_count = len(runtime.tool_registry.get_deferred_specs())
            except Exception:
                deferred_count = 0
            if deferred_count > 0:
                parts.append(
                    f"{deferred_count} 个扩展工具已隐藏以节省上下文。"
                    "若上方工具不满足需求，可调用 `tool_search` 用关键词发现它们。"
                )

    # Voice guides (role-play only - design spec + tag reference)
    if is_role_play:
        from voices import VOICE_DESIGN_GUIDE, VOICE_TAG_GUIDE
        parts.append(VOICE_DESIGN_GUIDE)
        parts.append(VOICE_TAG_GUIDE)

    # DYNAMIC BOUNDARY
    parts.append("\n─── 以上为稳定身份层，以下为当前会话模式 ───\n")

    if not is_role_play:
        # Role-play's kernel + directives are in the static region;
        # dynamic region carries only the mode prompt for non-role-play.
        try:
            from runtime import _get_mode_prompt
            mode_prompt = _get_mode_prompt(mode)
            if mode_prompt:
                parts.append(mode_prompt)
        except Exception:
            pass

    return "\n\n".join(parts)


# Web 端三模式总览（精简版，无 CLI 命令前缀）—— 与 runtime.MODE_L0_OVERVIEW 平行。
# 模块加载时读一次（对齐 runtime.py 的模块级常量模式）。
_WEB_MODE_OVERVIEW_PATH = _APP_ROOT / "prompts" / "modes" / "l0_overview.web.md"
_WEB_MODE_OVERVIEW = (
    _WEB_MODE_OVERVIEW_PATH.read_text(encoding="utf-8").strip()
    if _WEB_MODE_OVERVIEW_PATH.exists()
    else ""
)


def _extract_soul_boundary(soul_text: str) -> str:
    """Extract just the '## 边界' section from SOUL.md text."""
    if not soul_text:
        return ""
    lines = soul_text.split("\n")
    in_boundary = False
    out: list[str] = []
    for line in lines:
        if line.strip().startswith("## 边界"):
            in_boundary = True
            out.append(line)
            continue
        if in_boundary:
            if line.strip().startswith("## ") and not line.strip().startswith("## 边界"):
                break
            out.append(line)
    return "\n".join(out).strip()# ── Session (chat history) handlers ────────────────────────────────
