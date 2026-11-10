"""Sub-agent type definitions — toolset allowlists and system prompt templates.

Each sub-agent type declares which **toolsets** (groups) it can access,
rather than listing individual tool names.  The main agent and sub-agents
share the same ``ToolRegistry`` — the difference is which toolsets are
enabled for each.

To add a new sub-agent type, add entries to ``SUBAGENT_TOOLSETS`` and
``SUBAGENT_SYSTEM_PROMPT``; if the type must see only a named subset of its
toolsets, also add it to ``SUBAGENT_INCLUDE_TOOLS``.
"""

from __future__ import annotations

from pathlib import Path

# ── Context-creating tools ────────────────────────────────────────────────

# Tools that launch independent execution contexts (sub-agents, cron jobs,
# forked sessions). These MUST NOT be available inside any sub-agent to
# prevent recursive context nesting.
CONTEXT_CREATING_TOOLS: set[str] = {
    "Agent",
    # author_deepen 内部 fork author_scout —— 如果把它放进子代理的工具表，
    # 就会出现 scout → deepen → scout 的递归。主 agent 仍然可用。
    "author_deepen",
    # Future: "fork_session", "cron_create", ...
}

# ── Toolset allowlists ────────────────────────────────────────────────────

# Map sub-agent type → list of toolset names.  The main pipeline
# (ToolRegistry.get_registry_for_toolsets) resolves these to actual tools,
# automatically excluding CONTEXT_CREATING_TOOLS.

# fmt: off
SUBAGENT_TOOLSETS: dict[str, list[str]] = {
    "reader": [
        "file_reading",
        "knowledge",
        "utility",
    ],
    "extractor": [
        "book_graph",
    ],
    # 导入前分析：结构识别（哪些 spine 是正文）+ 特征识别（正文内部的
    # 文本形态）。工具全是只读探针，不碰库。见 docs/import-subagent/。
    #
    # 两套工具的分工：
    #   epub_probe  —— 统计与信号（selector 计数、裸 CSS、100 字 head）
    #   book_survey —— 书本身（版式样张）+ 切分假设实测器
    # 后者是主力：让 agent **读**而不是从统计量反推。
    "book_classifier": [
        "epub_probe",
        "book_survey",
    ],
    # 结构叙事的**证伪者**。工具与 classifier 完全一致（它得能复算对方的
    # 切分判据），但上下文隔离：只拿结论，不拿推理过程，否则它会替对方辩护。
    "book_verifier": [
        "epub_probe",
        "book_survey",
    ],
    # 作者层的**对话触发**通道。这是全层唯一够格用子代理的地方：
    # 另外五道工序的输入都完全确定（序跋/已有的书），而这个要**自己去够**
    # —— 够搜索结果、够别的书、够台账。判据正是「需不需要去够它没拿到的东西」。
    #
    #   author_graph —— author_lookup，先看台账里已经有什么（免得重搜）
    #   knowledge    —— web_search，只有配了搜索 key 才会真的注册进来
    "author_scout": [
        "author_graph",
        "knowledge",
    ],
    # 写作的**素材侦察兵**：主 agent 写某一章前，把「去书库/素材库找几条
    # 能借的原文」这一趟丢给它，自己只拿回坐标。整个 writing toolset 里
    # 只放行 writing_research —— 改稿、结算、交稿都不该发生在子代理里
    # （见 SUBAGENT_INCLUDE_TOOLS）。
    "writing_researcher": [
        "writing",
    ],
    # v2:
    # "analyst": [
    #     "file_reading", "library_query", "knowledge",
    # ],
    # "librarian": [
    #     "library_query", "library_edit",
    # ],
    # "general": None,  # None = all non-context-creating tools
}
# fmt: on

# Toolset 级白名单之外再收一刀：某些子代理只需要 toolset 里的一两个工具。
# writing_researcher 尤其如此 —— 写作工具全部注册在同一个 toolset 下，
# 按 toolset 过滤会把 writing_draft_chapter / writing_settle 一起带进去。
# None = 不做名称白名单（沿用 toolset 结果）。
SUBAGENT_INCLUDE_TOOLS: dict[str, set[str]] = {
    "writing_researcher": {"writing_research"},
}

# Toolsets whose tools are DEFERRED for the **main** agent (hidden from the
# per-turn tools payload, discoverable via tool_search) but must be DIRECT
# inside a sub-agent that allowlists them.
#
# 两类工具对主 agent 是纯开销：
#   - 导入探针（epub_probe / book_survey）只有 book_classifier / book_verifier
#     用得上，主 agent 每轮背 ≈3.5k tokens 的 schema，读书回合一次都不调；
#   - file_reading 里的 book_info / read_book / list_books / book_search 都要
#     磁盘路径，读书回合里模型拿不到路径（旧轨迹里 book_search 因此空转一次）。
# DEFERRED 后由 tool_search 兜底；子代理侧靠这份白名单提升回 DIRECT
# （子代理的 schema 不进主上下文）。current_passage 保持 DIRECT，不受影响。
SUBAGENT_PROMOTE_DEFERRED: set[str] = {
    "epub_probe", "book_survey", "file_reading",
    # 作者层的工具（author_lookup / author_web_fetch）对主 agent 是 DEFERRED，
    # 但 author_scout 的提示词明确要求「先查台账、再抓页引用」。不提升成
    # DIRECT 的话，它的工具表里只剩 web_search —— 提示词说的两个工具实际
    # 只有一个，agent 会凭记忆答或干脆不查台账。author_deepen 被
    # CONTEXT_CREATING_TOOLS 挡在子代理之外，不会被这条提升带进来。
    "author_graph",
}

SUBAGENT_DEFAULT_TYPE = "reader"

# ── Iteration budgets ────────────────────────────────────────────────────

# ReAct round budget per sub-agent type.  Reading tasks converge in a few
# rounds; the import classifier has to sweep every spine item *and* profile
# the text forms inside body, so it gets a much larger budget.
SUBAGENT_MAX_ITERATIONS: dict[str, int] = {
    "book_classifier": 24,
    # 证伪趟给得比立论还宽：它的工作量随对方结论的**详细程度**增长，而回炉后
    # 的结论会长得多（实测窄门 4994 → 20595 字符）。曾经给 16，第 2 轮用满
    # 预算还没吐结论就被截断，导致「假收敛」（见 import_analysis_service）。
    "book_verifier": 24,
    # 每次检索 = 一次工具调用 + 一次判读。默认 8 够用，但「几条不同的素材
    # 方向」常见 4–6 次检索，留出收尾轮。
    "writing_researcher": 12,
}
SUBAGENT_DEFAULT_MAX_ITERATIONS = 8

# ── System prompts ───────────────────────────────────────────────────────

# ── Prompt templates loaded from prompts/subagent/ ────────────────────────

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "subagent"
_PROMPT_CACHE: dict[str, str] = {}


def load_prompt(name: str, fallback: str) -> str:
    """读 ``prompts/subagent/{name}.md``，缺失时退回 *fallback*（结果缓存）。

    长提示词放 .md 而不是 Python 字符串：这一层最需要反复改，而字符串里的
    转义（JSON 示例的花括号尤其）正是最难改的地方。
    """
    if name not in _PROMPT_CACHE:
        path = _PROMPT_DIR / f"{name}.md"
        text = ""
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        _PROMPT_CACHE[name] = text or fallback
    return _PROMPT_CACHE[name]



SUBAGENT_SYSTEM_PROMPT: dict[str, str] = {
    "reader": (
        "You are a focused sub-agent. Work autonomously — do NOT ask "
        "the user questions. Use only the tools provided to you. "
        "Return a complete, structured result.\n\n"
        "Task: {description}"
    ),
    "book_classifier": load_prompt(
        "book_classifier",
        # 兜底：模板缺失时仍给出最小可用指令（工具本身会说话，不必背长提示词）
        "你是 EPUB 导入分析代理。自主完成工作，不要向用户提问。\n"
        "先调 book_overview 看全书骨架，再用 read_proof 读版式样张，判断体裁、"
        "每个 spine 的体例、正文内部的文本形态；章界判据用 test_boundary_rule "
        "实测。按 JSON 输出。\n\n任务：{description}",
    ),
    "book_verifier": load_prompt(
        "book_verifier",
        # 兜底：模板缺失时仍给出最小可用指令
        "你是结构叙事的校验代理。你的任务是**证伪**下面用户消息里那份结论：\n"
        "用 read_proof / test_boundary_rule / zoom 去书里找反例，每条质疑都要\n"
        "指到具体的 [spine|block]。找不到反例就诚实地说 upheld。按 JSON 输出。\n\n"
        "任务：{description}",
    ),
    "author_scout": load_prompt(
        "author_scout",
        # 兜底：模板缺失时仍给出最小可用指令
        "你在补一位作者的材料。先调 author_lookup 看台账里已经有什么，"
        "再用 web_search 补缺的部分。**每条要落库的条目必须带网址**，"
        "网址只能来自你这次真的搜到的结果。按 JSON 输出。\n\n任务：{description}",
    ),
    "writing_researcher": load_prompt(
        "writing_researcher",
        # 兜底：模板缺失时仍给出最小可用指令
        "你在为一次写作检索参考素材。你只有一个工具 writing_research："
        "按任务拆成几次检索，把结果里**真实出现过的坐标**（book_id + "
        "chapter_index + chunk_index，或 material_id / path）收进 refs，"
        "每条配一句 note 说明可以借什么。不要编坐标，不要写正文。"
        "最后只输出一个 JSON 对象："
        '{"refs": [...], "notes": "一句话"}。\n\n任务：{description}',
    ),
    # v2:
    # "analyst": (
    #     "You are an analysis sub-agent. Read book content and cross-reference "
    #     "with external sources to produce analytical notes.\n\n"
    #     ...
    # ),
}

# ── Default model overrides ──────────────────────────────────────────────

# Use cheaper/faster models for reading sub-agents.
# None = use the main agent's model.
SUBAGENT_DEFAULT_MODEL: dict[str, str | None] = {
    "reader": None,  # v1: same model, no override
    # "reader": "gpt-4o-mini",  # Future: cheaper model for reading
}
