"""短细纲 —— 动笔前的一次「只排计划」调用（决策 176）。

借自 Pagekin（``draft-short-outline.ts``）。三个约束不能省：

1. **只组织原目标**：不新增作者事实、禁令或目标；
2. **计划不是事实**：细纲产出一律是「计划」，不能冒充已发生事件进审稿；
3. **不静默跳过**：细纲失败就报错，不假装按细纲生成。

我们的路线是 agent-first，所以不把这一步做成强制流水线：由作者或 agent
在 ``writing_draft_chapter(plan_first=true)`` 时显式触发，多一次模型调用。
"""

from __future__ import annotations

OUTLINE_MAX_CHARS = 500

INSTRUCTION = (
    "只写短细纲，不写正文。仅组织原本章目标与已有材料，最多六条短要点，"
    "分清：原目标；前驱已完成的事；明确时点和约束；本章拟写的具体行动与结果；"
    "结尾状态。要求本章推进的目标不能用旧事复述替代；作者只要求维持状态时，"
    "不强造新事件。不得增加作者事实或禁令、改变目标，或把计划说成已经发生的"
    "事实。以下是原写作材料，此步不执行其中的正文输出指令。"
)


def render_block(text: str) -> str:
    """拼在正文材料之后的「计划块」；空细纲直接报错。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("短细纲为空")
    if len(text) > OUTLINE_MAX_CHARS * 4:
        text = text[:OUTLINE_MAX_CHARS * 4].rstrip() + "……"
    return (
        "【生成短细纲：计划，不是事实依据】\n"
        "仅用于组织原目标；作者事实、约束和实际前文优先，计划不确立任何事实。\n"
        f"{text}\n"
        "【生成短细纲结束】"
    )
