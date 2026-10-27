"""EPUB import probe + layout survey tools for the import sub-agents."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from permissions import PermissionMode
from tools.registry import ToolRegistry, ToolSpec, ToolExposure


def _register_epub_probe_tools(registry: ToolRegistry, db: Any) -> None:
    """Register read-only EPUB probe tools for the ``book_classifier`` sub-agent.

    These never touch the library DB — they read the *source* EPUB only, so
    they are safe to run before an import commits anything.  Two jobs:
    structural (which spine items are body) and textual-form (which kinds of
    prose live inside body).  Design notes: ``docs/import-subagent/``.
    """
    from pathlib import Path

    from file_readers import open_book
    from file_readers.epub_probe import (
        ensure_book_dir, probe_css_rules, probe_grep, probe_heading_outline,
        probe_spine_list, probe_tag_themes, read_book_file,
    )
    from file_readers.epub_reader import EpubReader
    from services.library_service import _bookbank_dir, _hash_file

    def _reader(path: str) -> EpubReader:
        r = open_book(path)
        if not isinstance(r, EpubReader):
            raise ValueError("only EPUB files are supported")
        r._ensure_loaded()
        return r

    def _close(r) -> None:
        if hasattr(r, "close"):
            r.close()

    def _extracted(path: str) -> Path:
        """解压落点：data/bookbank/_extracted/<md5>/（惰性，已解压则复用）。"""
        out = _bookbank_dir(db) / "_extracted" / _hash_file(Path(path))
        return ensure_book_dir(path, out)

    def _ints(s: str) -> list[int] | None:
        vals = [int(p) for p in s.split(",") if p.strip().lstrip("-").isdigit()]
        return vals or None

    def _strs(s: str) -> list[str] | None:
        vals = [p.strip() for p in s.split(",") if p.strip()]
        return vals or None

    def _dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

    # ── 结构层：列文件 ────────────────────────────────────────────────────
    def _tool_probe_spine_list(path: str, head_chars: int = 100) -> str:
        r = _reader(path)
        try:
            return _dump(probe_spine_list(r, head_chars))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="probe_spine_list",
        category="book",
        toolset="epub_probe",
        exposure=ToolExposure.DEFERRED,
        description=(
            "列出 EPUB 的全部 spine 文件，每项含 index / href / NCX 标签 / "
            "字数 / 开头若干字。这是**结构识别**的第一手材料 —— 靠 head 判断"
            "哪些文件是正文、哪些是版权页/目录/序言。"
            "head_chars 默认 100，足够判断结构；要深读某个文件用 "
            "read_book_file。返回的 href 可直接当作 read_book_file 的 "
            "file_path。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "head_chars": {
                    "type": "integer",
                    "description": "每个文件返回开头多少字（默认 100）",
                },
            },
            "required": ["path"],
        },
        execute=_tool_probe_spine_list,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── 特征层：统计 ──────────────────────────────────────────────────────
    def _tool_probe_tag_themes(path: str, spine_indices: str = "") -> str:
        r = _reader(path)
        try:
            return _dump(probe_tag_themes(r, _ints(spine_indices)))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="probe_tag_themes",
        category="book",
        toolset="epub_probe",
        exposure=ToolExposure.DEFERRED,
        description=(
            "统计各 <p class> 主题的分布：段数、字数、均长、占比。这是"
            "**特征识别**的第一步 —— 找出占比最高的几个主题。"
            "只统计 <p> 标签内部的文本；<p> 之外的内容量单列在 "
            "uncovered_chars。"
            "spine_indices 留空表示全部文件，通常应传入已判定为正文的文件"
            "索引（如 \"3,4,5,6\"）。拿到统计后必须再用 probe_css_rules 查"
            "这些主题的样式定义 —— 只有样式能区分「同一本书里并列的两种"
            "叙述层」（窄门的正文与信件段均长都是 70 字上下，靠 font-size "
            "1em vs 0.875em 才分得开）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "spine_indices": {
                    "type": "string",
                    "description": "逗号分隔的 spine 索引，如 \"3,4,5\"；留空=全部",
                },
            },
            "required": ["path"],
        },
        execute=_tool_probe_tag_themes,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_probe_css_rules(path: str, selectors: str = "") -> str:
        r = _reader(path)
        try:
            return _dump(probe_css_rules(r, _strs(selectors)))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="probe_css_rules",
        category="book",
        toolset="epub_probe",
        exposure=ToolExposure.DEFERRED,
        description=(
            "给出选择器在 stylesheet.css 里的真实定义（font-size / "
            "text-align / text-indent / font-weight 等）。这是判断文本形态的"
            "**依据来源** —— 没有它就说不出「为什么我认为这是信件」。"
            "selectors 可直接用 probe_tag_themes 返回的 selector（形如 "
            "p.calibre14），内部会自动回退匹配样式表里的 .calibre14。"
            "留空返回全部规则，其中大量类从未被使用，通常不是你想要的。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "selectors": {
                    "type": "string",
                    "description": "逗号分隔的选择器，如 \"p.calibre14,p.calibre19\"",
                },
            },
            "required": ["path"],
        },
        execute=_tool_probe_css_rules,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    def _tool_probe_grep(
        path: str, pattern: str, spine_indices: str = "", limit: int = 50,
    ) -> str:
        r = _reader(path)
        try:
            return _dump(probe_grep(r, pattern, _ints(spine_indices), limit))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="probe_grep",
        category="book",
        toolset="epub_probe",
        exposure=ToolExposure.DEFERRED,
        description=(
            "在指定文件范围内检索正则，回报匹配段落及**所属标签主题**。"
            "用途是**验证**你对某个主题性质的假设 —— 例如搜一个叙述常用"
            "虚词，看它落在哪些主题；若集中在某个主题，即可印证那是叙述层。"
            "不要盲猜关键词撞运气，先用 probe_tag_themes 找出主题再针对性地验。"
            "limit 默认 50，建议不超过 100。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "pattern": {"type": "string", "description": "正则表达式"},
                "spine_indices": {
                    "type": "string",
                    "description": "逗号分隔的 spine 索引；留空=全部",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条匹配（默认 50）",
                },
            },
            "required": ["path", "pattern"],
        },
        execute=_tool_probe_grep,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── 结构层：标题轮廓 ──────────────────────────────────────────────────
    def _tool_probe_heading_outline(
        path: str, spine_indices: str = "", limit: int = 300,
    ) -> str:
        r = _reader(path)
        try:
            return _dump(probe_heading_outline(r, _ints(spine_indices), limit))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="probe_heading_outline",
        category="book",
        toolset="epub_probe",
        exposure=ToolExposure.DEFERRED,
        description=(
            "回报各文件的**标题轮廓**：每个标题的 tag（h1..h6）、文字、是否在"
            "书目录里（in_toc）、目录深度（toc_depth）。这是判断章节层级环境的"
            "直接依据 —— 真小节通常 tag=h3 且 in_toc=true；图注/地图/插图表"
            "常是 h5/h6 且 in_toc=false，它们不在书目录里，只是文件内部结构，"
            "会被排到跟真小节同级。回报的标题集合就是导入时会切成章的那一批。"
            "spine_indices 留空=全部文件（先跑 probe_spine_list 拿索引）。"
            "limit 是标题条数硬顶，默认 300，建议不超过 500。只读，不改任何库。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "spine_indices": {
                    "type": "string",
                    "description": "逗号分隔的 spine 索引，如 \"3,4,5\"；留空=全部",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条标题（默认 300）",
                },
            },
            "required": ["path"],
        },
        execute=_tool_probe_heading_outline,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── 原始文件访问 ──────────────────────────────────────────────────────
    def _tool_read_book_file(
        path: str, file_path: str, offset: int = 0,
        limit: int = 4000, strip_tags: bool = False,
    ) -> str:
        book_dir = _extracted(path)
        return _dump(read_book_file(book_dir, file_path, offset, limit, strip_tags))

    registry.register(ToolSpec(
        name="read_book_file",
        category="book",
        toolset="epub_probe",
        exposure=ToolExposure.DEFERRED,
        description=(
            "读取 EPUB 内某个文件的正文，支持分页续读。file_path 用 "
            "probe_spine_list 返回的 href。"
            "默认返回原始 HTML —— 保留标签是为了让你看得见 "
            "<p class=\"…\"> 的包裹关系；strip_tags=true 只留纯文本，"
            "此时 offset/limit 作用于纯文本，与默认模式不是同一把尺子。"
            "单文件可达 2 万字符，**务必看 truncated 和 next_offset**："
            "truncated=true 表示没读完，续读传 next_offset。"
            "limit 默认 4000，建议不超过 8000。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "file_path": {
                    "type": "string",
                    "description": "文件在书内的相对路径，如 text/part0011.html",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始字符位置，续读时传上一次的 next_offset",
                },
                "limit": {
                    "type": "integer",
                    "description": "本次最多返回多少字符（默认 4000）",
                },
                "strip_tags": {
                    "type": "boolean",
                    "description": "true 时剥掉 HTML 标签只留纯文本（默认 false）",
                },
            },
            "required": ["path", "file_path"],
        },
        execute=_tool_read_book_file,
        permission_mode=PermissionMode.READ_ONLY,
    ))


# ── 版式勘测工具（book_classifier / book_verifier 子代理专用）─────────────


def _register_book_survey_tools(registry: ToolRegistry, db: Any) -> None:
    """Register the "read the book" instruments.

    The ``epub_probe`` toolset hands the agent **statistics** (selector counts,
    raw CSS declarations, 100-char heads) and asks it to infer structure from
    them.  This toolset hands it the **book** instead — every paragraph with its
    *computed* effective typography — plus an instrument that measures a
    splitting hypothesis against the whole book and reports what contradicts it.

    Read-only, same contract as ``epub_probe``: reads the source EPUB, never the
    library DB.  Design notes: ``docs/import-subagent/``.
    """
    from file_readers import open_book
    from file_readers.book_proof import (
        PROOF_CHAR_LIMIT, book_overview, compare_regions, render_proof,
        test_boundary_rule, zoom,
    )
    from file_readers.epub_reader import EpubReader

    def _reader(path: str) -> EpubReader:
        r = open_book(path)
        if not isinstance(r, EpubReader):
            raise ValueError("only EPUB files are supported")
        r._ensure_loaded()
        return r

    def _close(r) -> None:
        if hasattr(r, "close"):
            r.close()

    def _dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

    # ── 俯瞰：全书法医报告 ────────────────────────────────────────────────
    def _tool_book_overview(path: str) -> str:
        r = _reader(path)
        try:
            return _dump(book_overview(r))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="book_overview",
        category="book",
        toolset="book_survey",
        exposure=ToolExposure.DEFERRED,
        description=(
            "**先调这个。** 一次拿到全书法医报告：书的目录树、每个 spine 文件"
            "的视觉类指纹与首末段原文、全书视觉类清单（含视觉等价组）、"
            "guide/landmarks 结构信号。\n"
            "「视觉类」是按**算出来的有效排版**（字号倍数、粗斜体、对齐、缩进）"
            "分组的结果，selector 形如 p.calibre19；equiv_groups 里同一组 = "
            "排版完全相同。空 spine 不进列表（无信息可判），只记在 empty_spines。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
            },
            "required": ["path"],
        },
        execute=_tool_book_overview,
        permission_mode=PermissionMode.READ_ONLY,
    ))
    # ── 看：版式样张 ──────────────────────────────────────────────────────
    def _tool_read_proof(path: str, spine_from: int = 0,
                         spine_to: int = -1, block_from: int = 0,
                         max_chars: int = 0) -> str:
        r = _reader(path)
        try:
            return _dump(render_proof(
                r, spine_from,
                None if spine_to < 0 else spine_to,
                block_from=block_from,
                max_chars=max_chars or PROOF_CHAR_LIMIT,
            ))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="read_proof",
        category="book",
        toolset="book_survey",
        exposure=ToolExposure.DEFERRED,
        description=(
            "**读书。** 返回版式样张：每个段落一行，前缀是 "
            "`[spine|block] selector`，后接该段原文。\n"
            "这就是「看排版稿」—— 段落的排版身份直接写在它前面，不需要你去"
            "心算 CSS 层叠。读样张判断内容性质（这是信、这是题词、这是版权页），"
            "**不要**靠统计量猜。\n"
            "`[spine|block]` 是证据坐标：引用它，用 zoom 取回它。\n"
            "全书通常一次给得完；返回 truncated=true 时，把返回的 next_spine 与 "
            "next_block **一起**传回 spine_from / block_from 续读（只传 spine "
            "会在超大文件上重复同一段）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "spine_from": {"type": "integer", "description": "起始 spine 序号（默认 0）"},
                "spine_to": {"type": "integer", "description": "结束 spine 序号，不含（默认到书尾）"},
                "block_from": {"type": "integer", "description": "续读时的起始块序号（传上次返回的 next_block）"},
                "max_chars": {"type": "integer", "description": "本次返回字符上限，默认 500000（整本大部头通常一次给得完）"},
            },
            "required": ["path"],
        },
        execute=_tool_read_proof,
        permission_mode=PermissionMode.READ_ONLY,
    ))
    # ── 量：切分假设实测 ──────────────────────────────────────────────────
    def _tool_test_boundary_rule(path: str, rule: dict) -> str:
        r = _reader(path)
        try:
            return _dump(test_boundary_rule(r, rule))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="test_boundary_rule",
        category="book",
        toolset="book_survey",
        exposure=ToolExposure.DEFERRED,
        description=(
            "**把切分假设全书量一遍。** 你提出「章界长什么样」，这个工具告诉你"
            "它在这本书里命中多少处、分布多均匀、和书自己的目录对得上几条、"
            "以及**哪些地方自相矛盾**（conflicts）。\n"
            "先猜后量，不要猜了就直接写结论。\n"
            "规则是一个对象，kind 取：tag / selector / class / text_regex / "
            "align / page_break / min_chars / max_chars / size_ratio_lt / "
            "size_ratio_gt；组合用 all_of / any_of / not（用 rules 或 rule 字段）。\n"
            "例：{\"kind\":\"tag\",\"value\":\"h3\"}；"
            "{\"kind\":\"all_of\",\"rules\":[{\"kind\":\"tag\",\"value\":\"p\"},"
            "{\"kind\":\"align\",\"value\":\"center\"},"
            "{\"kind\":\"max_chars\",\"value\":20}]}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "rule": {
                    "type": "object",
                    "description": "切分判据，见工具描述里的 kind 列表",
                    "properties": {
                        "kind": {"type": "string"},
                        "value": {},
                        "rules": {"type": "array", "items": {"type": "object"}},
                        "rule": {"type": "object"},
                    },
                    "required": ["kind"],
                },
            },
            "required": ["path", "rule"],
        },
        execute=_tool_test_boundary_rule,
        permission_mode=PermissionMode.READ_ONLY,
    ))
    # ── 比：两个区段并排 ──────────────────────────────────────────────────
    def _tool_compare_regions(path: str, a_from: int, a_to: int,
                              b_from: int, b_to: int) -> str:
        r = _reader(path)
        try:
            return _dump(compare_regions(r, {"from": a_from, "to": a_to},
                                         {"from": b_from, "to": b_to}))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="compare_regions",
        category="book",
        toolset="book_survey",
        exposure=ToolExposure.DEFERRED,
        description=(
            "并排比较两个 spine 区段（左闭右开），回报各自的排版构成、段均长、"
            "是否在书目录里有条目，以及机械算出的**差异点**。\n"
            "判「这一片是正文还是体例页」时用它 —— 比在脑子里比两堆数字可靠。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "a_from": {"type": "integer", "description": "区段 A 起始 spine"},
                "a_to": {"type": "integer", "description": "区段 A 结束 spine（不含）"},
                "b_from": {"type": "integer", "description": "区段 B 起始 spine"},
                "b_to": {"type": "integer", "description": "区段 B 结束 spine（不含）"},
            },
            "required": ["path", "a_from", "a_to", "b_from", "b_to"],
        },
        execute=_tool_compare_regions,
        permission_mode=PermissionMode.READ_ONLY,
    ))

    # ── 回看原文：行内结构 ────────────────────────────────────────────────
    def _tool_zoom(path: str, spine_index: int, block_index: int) -> str:
        r = _reader(path)
        try:
            return _dump(zoom(r, spine_index, block_index))
        finally:
            _close(r)

    registry.register(ToolSpec(
        name="zoom",
        category="book",
        toolset="book_survey",
        exposure=ToolExposure.DEFERRED,
        description=(
            "取回某一块（用样张里的 [spine|block] 坐标）的全文、原文 HTML 片段、"
            "以及前后邻块。\n"
            "样张给的是折过空白的文本；题词两行同属一段、某行被 <span> 包着这类"
            "**行内结构**只有回原文才看得见。拿不准某段到底怎么排的，用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "EPUB 文件的完整路径"},
                "spine_index": {"type": "integer", "description": "spine 序号"},
                "block_index": {"type": "integer", "description": "该文件内的块序号"},
            },
            "required": ["path", "spine_index", "block_index"],
        },
        execute=_tool_zoom,
        permission_mode=PermissionMode.READ_ONLY,
    ))
