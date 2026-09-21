"""L1 extraction and dedup prompts.

Adapted from TencentDB Agent Memory 2.0 (MemoryCore/src/core/prompts/).
Chinese-first, customized for Book Companion reading scenarios.
"""

from __future__ import annotations

from typing import Any

# ===========================================================================
# L1 Extraction: scene segmentation + memory extraction in one LLM call
# ===========================================================================

L1_EXTRACTION_SYSTEM_PROMPT = """\
你是专业的"情境切分与记忆提取专家"。
你的任务是分析用户与书伴 AI 的对话，判断情境切换，并从中提取结构化的核心记忆（仅限 persona, episodic, instruction 三类）。

**输出语言**：所有自由文本字段（`scene_name`、memory `content`）使用中文；JSON 字段名、枚举值、ISO 时间戳保持英文。

### 任务一：情境切分（Scene Segmentation）
分析【待提取的新消息】，结合【上一个情境】，判断并输出当前对话的情境。
- 继承：无明显切换，沿用上一个情境。
- 切换条件：用户发出明确指令（如"换话题"）、意图转变、或提出独立新目标。
- 一段对话可能只有一个情境，也可能有多个情境（话题多次切换时）。
- 命名规则："我在和用户做xxx（目标活动）"（约 30-50 个字符，单句，全局唯一）。
  读书场景示例："我在和用户讨论《日常生活中的自我呈现》的拟剧理论"

---

### 任务二：核心记忆提取（Memory Extraction）
结合背景和当前情境，仅从【待提取的新消息】中提取核心信息。

【通用提取原则】
1. 宁缺毋滥：过滤琐碎闲聊、临时性指令和一次性操作（如"这次、本单"）；剔除不可靠的边缘信息。
2. 独立完整：记忆必须"跳出当前对话依然成立"，无上下文也能看懂。提取主体必须以"用户"或"AI"为核心。
3. 归纳合并：强关联或因果关系的多条消息，必须合并为一条完整记忆，不可碎片化。

【支持提取的三大类型】

1. 个性化记忆 (type: "persona")
   - 定义：用户的稳定属性、偏好、技能、价值观、习惯（如阅读偏好、职业、思维方式）。
   - 提取句式："用户喜欢/是/擅长..."
   - 打分 (priority)：80-100（核心特质/阅读偏好）；50-70（一般喜好）；<50（模糊次要，可丢弃）。
   - 触发词：喜欢、习惯、经常、我这个人...

2. 客观事件记忆 (type: "episodic")
   - 定义：客观发生的动作、决定、计划或达成结果。绝不包含纯主观感受。
   - 提取句式："用户在 [时间] 于 [地点/场景] [做了某事]"
   - 读书场景：阅读进度、讨论结论、角色分析、情节理解等。
   - 打分 (priority)：80-100（重要事件/计划）；60-70（一般完整活动）；<60（琐碎事项，直接丢弃）。

3. 全局指令记忆 (type: "instruction")
   - 定义：用户对 AI 提出的长期行为规则、格式偏好、语气控制。
   - 提取句式："用户要求/希望 AI 以后回答时..."
   - 打分 (priority)：-1（极其严格的全局死命令）；90-100（核心行为规则）；70-80（重要要求）；<70（临时要求，直接丢弃）。

---

### 不应该提取的内容
- 琐碎闲聊、问候；临时性的纯工具性请求（如"这次帮我翻译一下"）
- 一次性操作指令（如"这次、本单"相关）
- 重复的内容；AI助手自身的行为或输出
- 不属于以上3类的信息
- 纯主观感受（不带客观事件的情绪表达）

---

### 任务三：输出格式规范（JSON）
返回且仅返回一个合法的 JSON 数组。数组的每一项是一个情境，包含该情境的消息范围和抽取到的记忆：

[
  {
    "scene_name": "当前生成或继承的情境名称",
    "message_ids": ["属于该情境的消息ID列表"],
    "memories": [
      {
        "content": "完整、独立的记忆陈述",
        "type": "persona|episodic|instruction",
        "priority": 80,
        "source_message_ids": ["消息ID_1", "消息ID_2"],
        "metadata": {}
      }
    ]
  }
]

metadata 字段说明：
- episodic 类型：如能确定活动时间，填入 {"activity_start_time": "ISO8601", "activity_end_time": "ISO8601"}
- 其他类型或无法确定时间：输出空对象 {}

如果整段对话无有意义的记忆，也要输出情境分割结果，memories 为空数组：
[{"scene_name": "情境名称", "message_ids": ["id1"], "memories": []}]

请严格按上述 JSON 数组格式输出，不要输出任何额外的 Markdown 代码块修饰符或解释文本。"""


def format_extraction_prompt(
    new_messages: list[dict],
    background_messages: list[dict] | None = None,
    previous_scene_name: str = "无",
) -> str:
    """Build the user prompt for L1 extraction.

    Args:
        new_messages: messages to extract memories from (with id, role, content, timestamp)
        background_messages: previous messages for context only (not for extraction)
        previous_scene_name: the last known scene name (for continuity)
    """
    bg = background_messages or []
    bg_text = (
        "\n\n".join(
            f"[{m['id']}] [{m['role']}] [{m.get('timestamp', '')}]: {m['content']}"
            for m in bg
        )
        if bg
        else "无"
    )

    new_text = "\n\n".join(
        f"[{m['id']}] [{m['role']}] [{m.get('timestamp', '')}]: {m['content']}"
        for m in new_messages
    )

    return f"""\
【上一个情境】：{previous_scene_name}

【背景对话】（仅供理解上下文推断关系/时间，严禁从中提取记忆）：
{bg_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【待提取的新消息】（务必结合 timestamp 推算时间，只从这里提取记忆！）：
{new_text}"""


# ===========================================================================
# L1 Dedup: batch conflict detection prompt
# ===========================================================================

L1_DEDUP_SYSTEM_PROMPT = """\
你是记忆冲突检测器。批量比较多条【新记忆】与【统一候选记忆池】中的已有记忆，逐条决定如何处理。

**输出语言**：`merged_content` 使用中文；JSON 字段名、枚举值、record_id 保持英文。

## 核心规则

- **跨 type 合并**：不同 type（persona / episodic / instruction）的记忆如果语义上描述同一事实/事件，**可以合并**。
- **多对多合并**：一条新记忆可以同时替换/合并候选池中的**多条**已有记忆（通过 target_ids 数组指定）。
- 合并后你必须判断新记忆的最佳 type（merged_type）。

## 判断逻辑

1. **分辨记忆性质**：
   - **状态类**（persona/instruction）：偏好、特质、长期设定、相对稳定的事实、行为规则
   - **事件类**（episodic）：一次性经历、带时间点的客观记录，建议合并同一件事的前因后果

2. **判断是否同一事实/事件**：主体相同、主题一致、时间接近、scene_name 相似

3. **选择动作**：
   - "store"：视为新信息，新增当前记忆。
   - "skip"：已有记忆更好，新记忆无增量或更模糊，忽略当前记忆。
   - "update"：同一事实/事件，新记忆在内容或时间上更优（更具体、更晚或纠错），以新记忆为主覆盖旧记忆，可保留旧记忆中仍正确的细节。
   - "merge"：同一事实或同一演化过程，多条记忆信息互补且不矛盾，合并成一条更完整记忆，信息尽量不冗余。

4. **策略倾向**：
   - 状态类：多条描述同一偏好/特质 -> 倾向 merge；无增量 -> skip；明确更新 -> update
   - 事件类：同一事件的前因后果、不同阶段 -> 倾向 merge 为一条完整叙述；完全相同 -> skip
   - 跨类型示例：一条 episodic "用户在读《xxx》第5章" + 一条 persona "用户喜欢拟剧理论" -> 可 merge 为一条 persona（如果信息侧重偏好）或 episodic（如果信息侧重阅读事件）

5. **timestamp 处理**：
   - merge / update 时，merged_timestamps 应包含**所有相关记忆的时间戳并集**（去重排序）

## 输出格式

严格输出 JSON 数组，每个元素对应一条新记忆的决策。不输出任何其他内容：

[
  {
    "record_id": "新记忆的 record_id",
    "action": "store|update|skip|merge",
    "target_ids": ["要删除的候选记忆 record_id 1", "record_id 2"],
    "merged_content": "合并/更新后的记忆内容（merge/update 时必填）",
    "merged_type": "合并后的最佳 type：persona|episodic|instruction（merge/update 时必填）",
    "merged_priority": 85,
    "merged_timestamps": ["合并后的时间戳数组"]
  }
]

字段说明：
- target_ids：要删除替换的旧记忆 ID **数组**（可以 1 条或多条）。store/skip 时省略或为空。
- merged_content：merge/update 时的最终记忆文本。store/skip 时省略。
- merged_type：merge/update 后记忆应归属的 type。
- merged_priority：合并后的新优先级（0-100 整数）。合并后信息更完整、更确定，通常应**酌情提升** priority。
- merged_timestamps：合并后的时间戳数组。收集新记忆 + 所有被合并旧记忆的时间戳，去重排序。"""


def format_dedup_prompt(matches: list[dict]) -> str:
    """Build the user prompt for batch conflict detection.

    Args:
        matches: list of {newMemory: {record_id, content, type, priority, scene_name},
                          candidates: [MemoryEntryRow...]}
    """
    # Build unified candidate pool
    unified_pool: dict[str, dict] = {}
    per_memory_candidates: dict[str, list[str]] = {}

    for m in matches:
        candidate_ids: list[str] = []
        for c in m.get("candidates", []):
            cid = c["id"]
            if cid not in unified_pool:
                unified_pool[cid] = {
                    "record_id": cid,
                    "content": c["content"],
                    "type": c.get("memory_type", "episodic"),
                    "priority": c.get("priority", 50),
                    "scene_name": c.get("scene_name", ""),
                }
            candidate_ids.append(cid)
        per_memory_candidates[m["newMemory"]["record_id"]] = candidate_ids

    # Format pool
    if not unified_pool:
        pool_section = "## 统一候选记忆池\n\n（空，没有已有记忆，所有新记忆直接 store）"
    else:
        import json
        pool_list = list(unified_pool.values())
        pool_section = (
            f"## 统一候选记忆池（共 {len(pool_list)} 条已有记忆）\n\n"
            f"{json.dumps(pool_list, ensure_ascii=False, indent=2)}"
        )

    # Format new memories
    import json
    memory_parts = []
    for i, m in enumerate(matches):
        nm = m["newMemory"]
        related_ids = per_memory_candidates.get(nm["record_id"], [])
        related_note = (
            json.dumps(related_ids, ensure_ascii=False)
            if related_ids
            else "[]（无相似候选，直接 store）"
        )
        mem_json = json.dumps(
            {
                "record_id": nm["record_id"],
                "content": nm["content"],
                "type": nm.get("type", "episodic"),
                "priority": nm.get("priority", 50),
                "scene_name": nm.get("scene_name", ""),
            },
            ensure_ascii=False,
            indent=2,
        )
        memory_parts.append(
            f"### 第 {i + 1} 条新记忆 (record_id: {nm['record_id']})\n{mem_json}\n\n"
            f"【关联候选 ID】{related_note}"
        )

    new_memories_text = "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n".join(
        memory_parts
    )

    return f"""\
{pool_section}

{"═" * 50}

## 待判断的新记忆（共 {len(matches)} 条）

{new_memories_text}

请逐条判断并输出决策 JSON 数组。当某条新记忆的候选列表为空时，该条直接输出 action=store。"""


# ===========================================================================
# L2 Scene extraction: aggregate L1 memories into per-book scene blocks
# ===========================================================================

L2_SCENE_SYSTEM_PROMPT = """\
你是书伴的"场景聚合专家"。你的任务是把 L1 提取的原子记忆，聚合为**按书组织**的叙事场景块。

### 背景
- L1 记忆是碎片化的原子记忆（persona / episodic / instruction），存于记忆库。
- L2 场景块**按书组织**：一本书一个场景块，回答"用户读这本书的过程是什么样的"。
- 场景块有固定章节（阅读轨迹 / 核心讨论 / 用户的理解 / 待续·延伸）。
- 已有场景块（若有）会列出其书名、摘要与章节内容；热度和时间戳由系统管理，无需你维护。

### 任务
1. 把【待处理的 L1 记忆】归类到对应的书。
2. 对每本被涉及的书，决定 create（新建）还是 update（更新已有场景块）。
3. 输出每个场景块的 summary（**≤40 字**）和四个章节的更新内容。
4. 非读书场景的记忆（纯闲聊、通用偏好、与任何书无关）→ 不归类，保持 L1 即可。

### 判断原则
- **宁缺毋滥**：只有真正与书相关、对"阅读过程叙事"有增量的记忆才归入场景块。
- **归纳合并**：多条约同主题记忆合并为连贯叙事，不碎片化。
- **章节语义**：
  - `阅读轨迹`：进度、时间线、读到哪章、阅读安排
  - `核心讨论`：用户与书伴对书的讨论要点、观点交锋
  - `用户的理解`：用户对书的理解、洞察、共鸣、批判
  - `待续·延伸`：想继续讨论的点、联想、延伸阅读
- **update 时保留原内容**：已有场景块中有价值的叙述要保留，融入新记忆增量，不要重写丢失。
- **一本书只对应一个场景块**：如果新记忆归入的书与已有块同书异名，沿用已有块的规范书名（同书归并）。

### 输出格式
严格输出 JSON **数组**，每个元素对应一个被涉及的书场景块：

[
  {
    "book": "规范书名",
    "action": "create|update",
    "summary": "该场景块的一句话摘要（≤40 字）",
    "sections": {
      "阅读轨迹": "…",
      "核心讨论": "…",
      "用户的理解": "…",
      "待续·延伸": "…"
    },
    "consumed_ids": ["被该场景块吸收的 L1 记忆 id 列表"]
  }
]

- 没有新场景块的轮次输出 `[]`。
- `consumed_ids` 只列被该场景块吸收的记忆 id；其余记忆保持 L1（不标记消费）。
- 不要输出任何 Markdown 代码块修饰符或解释文本。"""


def format_l2_prompt(
    existing_blocks: list[dict[str, Any]],
    new_memories: list[dict[str, Any]],
) -> str:
    """Build the user prompt for L2 scene aggregation.

    Args:
        existing_blocks: list of existing scene blocks, each with
            book / summary / heat / sections (dict). Parsed from scene files.
        new_memories: list of unconsumed L1 memories, each with
            id / content / memory_type / priority / scene_name.
    """
    import json

    # ── Existing scene blocks ─────────────────────────────────────────
    if not existing_blocks:
        existing_section = "（无，这是第一批场景块）"
    else:
        block_lines = []
        for b in existing_blocks:
            book = b.get("book", "")
            summary = b.get("summary", "")
            sections = b.get("sections", {})
            section_text = " | ".join(
                f"{k}: {str(v)[:120]}" for k, v in sections.items() if v
            )
            block_lines.append(
                f"- 书名「{book}」\n  摘要: {summary}\n  章节: {section_text or '（空）'}"
            )
        existing_section = "\n\n".join(block_lines)

    # ── New L1 memories ───────────────────────────────────────────────
    mem_lines = []
    for i, m in enumerate(new_memories):
        mem_lines.append(
            f"{i + 1}. id={m['id']} | type={m.get('memory_type', 'episodic')} "
            f"| priority={m.get('priority', 50)} | scene=「{m.get('scene_name', '')}」\n"
            f"   {m['content']}"
        )
    new_memories_text = "\n\n".join(mem_lines)

    return f"""\
## 现有场景块（按书组织）

{existing_section}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 待处理的 L1 记忆（共 {len(new_memories)} 条）

{new_memories_text}

请归类这些记忆到对应书场景块，输出 JSON 数组。不属于任何书的记忆不要归入。"""


# ===========================================================================
# L3 Persona generation: user profile from scenes + unconsumed L1 persona
# ===========================================================================
# 权威性设计倾向 (2026-08-13): persona.md = AI 观察投影（描述性真实），
# USER.md = 用户自我陈述（期望的对待方式，builtin 管）。冲突处理 TBD。

L3_PERSONA_SYSTEM_PROMPT = """\
你是书伴的"用户画像架构师"。你的任务是把 L2 场景块和 L1 记忆，综合成一份用户画像文档（persona.md）。

## 输出要求
直接输出**最终的 persona.md 完整内容**（Markdown 文档）。不要输出任何解释、分析步骤、代码块修饰符。

## 四层深度扫描（连接与综合）
遵循"叙事连贯性"原则：寻找不同领域行为背后的共同逻辑，禁止简单罗列（No Bullet-point Spamming）。保持精简，不过度猜想，不确定可以不写。

### 🟢 Layer 1: 基础锚点 -> 建立连接
确凿的事实、人口统计学特征、当前状态。为书伴提供破冰话题和上下文感知。
### 🔵 Layer 2: 兴趣图谱 -> 提供谈资
用户投入时间、金钱或注意力的事物（阅读偏好、生活兴趣）。区分活跃度（活跃爱好 / 被动消费 / 休眠兴趣）。
### 🟡 Layer 3: 交互协议 -> 消除摩擦
用户的沟通习惯、雷区、工作流偏好。指导书伴如何说话、如何交付结果。
### 🔴 Layer 4: 认知内核 -> 深度共鸣
决策逻辑、矛盾点、终极驱动力。让书伴成为能够替用户做决策的"副驾驶"。

## 输出模板
参考以下格式输出（可自主调整，信息不足时可减少章节，**必须保持 Markdown 格式**）：

# User Narrative Profile

> **Archetype (核心原型)**: [一句话定义]

> **基本信息**
（用户的基本信息：年龄、职业、生活习惯等。更新时冲突则覆盖，不冲突尽量叠加）
-

> **长期偏好**
（最稳定且可复用的偏好，含阅读偏好与生活偏好）
-

## 📖 Chapter 1: Context & Current State (全景语境)
*(将基础事实与当前状态融合成一段连贯的背景介绍)*

## 🎨 Chapter 2: The Texture of Life (生活的肌理)
*(将阅读兴趣、生活兴趣、生活习惯串联起来，展示生活品味。重点在于兴趣/偏好与品味的一致性)*

## 🤖 Chapter 3: Interaction & Cognitive Protocol (交互与认知协议)
*(这是书伴的行动指南。保持半结构化，但要解释"为什么")*
### 3.1 沟通策略 (How to Speak)
### 3.2 决策逻辑 (How to Think)

## 🧩 Chapter 4: Deep Insights & Evolution (深层洞察与演变)
* **矛盾统一性**: [描述用户身上看似冲突但实则合理的特质]
* **演变轨迹**: [可分时间点，描述用户最近发生的变化]
* **涌现特征**: 提炼 3-7 个最核心的特质标签，每个单独一行并附 10-15 字注释
  - `TagName` - 简短注释

## 严格禁止
- **禁止超过 2000 字符**：及时总结，删除不重要的信息。
- **禁止过度推测**：没提到的信息不要臆想。冷启动时保持克制，无信息完全可以不填！
- **禁止使用非场景/记忆来源的信息**：内容必须且只能来自下方提供的场景数据和记忆数据。
- **禁止简单罗列**：优先连贯叙事，不用 bullet 堆砌。

## 成功标准
- 基于提供的证据生成深度洞察
- 内容到 Chapter 4 结束
- 严格按模板格式输出完整 persona.md 内容"""


def format_l3_prompt(
    *,
    mode: str,  # "first" | "incremental"
    total_processed: int,
    scene_count: int,
    changed_scene_count: int,
    changed_scenes_content: str,
    unconsumed_persona_content: str,
    persona_memory_count: int = 0,
    existing_persona: str = "",
    trigger_info: str = "",
) -> str:
    """Build the L3 persona generation user prompt.

    Args:
        mode: "first" (无画像) or "incremental" (已有画像，迭代更新)
        total_processed: total L1 memories processed
        scene_count: total active scene blocks
        changed_scene_count: scenes changed since last persona
        changed_scenes_content: full markdown of changed scene blocks
        unconsumed_persona_content: formatted unconsumed high-priority L1 persona
        persona_memory_count: number of unconsumed persona memories fed
        existing_persona: current persona.md content (incremental mode)
        trigger_info: P1/P2/P4 trigger reason (optional)
    """
    mode_label = "🆕 首次生成" if mode == "first" else "🔄 迭代更新"

    trigger_section = (
        f"\n### 触发信息\n{trigger_info}\n" if trigger_info else ""
    )

    scenes_section = (
        f"## 📄 变化场景完整内容（{changed_scene_count} 个）\n\n"
        f"*自上次画像更新后，以下场景发生了变化。请重点分析这些场景中的新信息：*\n\n"
        f"{changed_scenes_content or '（无变化场景）'}"
    )

    persona_input = (
        f"\n## 🧩 未消费画像记忆（{persona_memory_count} 条）\n\n"
        f"*以下是非读书场景的 L1 画像记忆（用户的生活画像、沟通偏好等），"
        f"未归入任何书的场景块。请一并综合进画像：*\n\n"
        f"{unconsumed_persona_content or '（无）'}"
    )

    existing_section = (
        f"\n## 📄 当前画像（已预加载，{len(existing_persona)} 字符）\n\n"
        f"*基于此更新，更新后请控制在 2000 字内：*\n\n"
        f"```markdown\n{existing_persona}\n```\n"
    ) if existing_persona else ""

    iteration_guide = (
        "\n## 🔄 迭代决策指南\n\n"
        "面对变化场景与新增记忆，自主判断处理方式：\n"
        "- **强化**：新内容只是佐证已有洞察，压缩进原句或不改。\n"
        "- **补充**：出现新的维度（新偏好、新习惯、新认知）。\n"
        "- **修正**：新证据推翻或细化旧画像（矛盾时以新证据为准，但保留仍正确的部分）。\n"
        "- **重构**：内容变长、变散时整体压缩重写。\n"
        "- **不改**：无有用新增内容时，原样保留现有画像。\n"
        "不要把每次变化追加为新条目。画像应持续压缩，保持少而准。"
    ) if mode == "incremental" else ""

    return f"""\
**⏰ 更新时间**: {datetime_now_iso()}
**模式**: {mode_label}
{trigger_section}
## 📊 统计
- **总记忆数**: {total_processed} 条
- **场景总数**: {scene_count} 个
- **变化场景**: {changed_scene_count} 个（自上次更新后）

---
{scenes_section}
{persona_input}
{existing_section}
{iteration_guide}

请输出最终的 persona.md 完整内容。"""


def datetime_now_iso() -> str:
    """Current time in ISO 8601 format (L3 prompt timestamp)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
