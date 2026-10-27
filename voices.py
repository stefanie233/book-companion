"""Voice registry and prompt templates for TTS system.

Three voice sources converge here:
  - PRESET_VOICES:  built-in mimo voices (mimo-v2.5-tts), direct call
  - CLONE_VOICES:   user-uploaded reference audio (mimo-v2.5-tts-voiceclone)
  - design (dynamic): agent-generated prompt (mimo-v2.5-tts-voicedesign)

Character voice flow: voicedesign casts a ref audio once, then voiceclone
replicates from it for stability. See voice_roles table (wingman/db.py).

VOICE_DESIGN_GUIDE and VOICE_TAG_GUIDE are injected into the role-play
system prompt so the agent knows how to write design prompts and use tags.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Preset voices - built-in mimo voices (mimo-v2.5-tts)
# Source: MiMo-Skills/skills/mimo-v2-5-tts/SKILL.md
# ---------------------------------------------------------------------------

PRESET_VOICES: list[dict] = [
    {"name": "冰糖", "gender": "female", "lang": "zh", "desc": "活泼少女"},
    {"name": "茉莉", "gender": "female", "lang": "zh", "desc": "知性女声"},
    {"name": "苏打", "gender": "male",   "lang": "zh", "desc": "阳光少年"},
    {"name": "白桦", "gender": "male",   "lang": "zh", "desc": "成熟男声"},
    {"name": "Mia",  "gender": "female", "lang": "en", "desc": "Lively girl"},
    {"name": "Chloe","gender": "female", "lang": "en", "desc": "Sweet Dreamy"},
    {"name": "Milo", "gender": "male",   "lang": "en", "desc": "Sunny boy"},
    {"name": "Dean", "gender": "male",   "lang": "en", "desc": "Steady Gentle"},
]

PRESET_VOICE_NAMES = {v["name"] for v in PRESET_VOICES}

# ---------------------------------------------------------------------------
# Clone voices - user DIY reference audio (mimo-v2.5-tts-voiceclone)
# TODO: user registers reference audio here
# ---------------------------------------------------------------------------

CLONE_VOICES: list[dict] = [
    # {
    #     "name": "温润男声",
    #     "ref_audio": "assets/voices/ref/warm_male.wav",
    #     "gender": "male",
    #     "desc": "中年男声，温润沉稳，书卷气",
    # },
]

CLONE_VOICE_MAP: dict[str, dict] = {v["name"]: v for v in CLONE_VOICES}

# ---------------------------------------------------------------------------
# Cast sample text - used when voicedesign casts a character's ref audio
# ---------------------------------------------------------------------------

VOICE_CAST_SAMPLE = (
    "你好，很高兴认识你。"
    "这段声音将作为我的音色参考，希望能在接下来的阅读中陪伴着你。"
)

# ---------------------------------------------------------------------------
# Voice design guide - injected into role-play system prompt
# Agent follows this when writing prompts for the voice_design tool.
# Source: MiMo-Skills SKILL.md "音色描述编写" + tts示例.txt
# ---------------------------------------------------------------------------

VOICE_DESIGN_GUIDE = """\
## 音色设计规范（voice_design 工具的 prompt 编写指南）

为角色铸造音色时，prompt 遵循以下规范：

### 必写项
1. **身份锚点**：年龄段 + 性别（决定基频）
2. **声音质感**：气息走向、共鸣位置、吐字与音色底色（用比喻，不堆形容词）
3. **语速节奏**：稳 / 快 / 慢 / 忽快忽慢 / 连珠带顿挫
4. **情绪底色**：嗓子默认状态（高亢 / 松弛 / 温软 / 克制）

### 推荐
5. **风格/身份标签**：一笔带过的职业锚点（拍卖师风格 / 法庭陈词风格）
6. **辨识度小癖好**：一耳朵记住的习惯（字尾带颤音 / 胸腔闷笑）

### 硬约束
- 一到两句话，白描式，不分段、不列条
- 不写场景、不写动作
- 不用真实演员或 IP 角色名
- 默认普通话；方言仅在明确需要时加

### 推断提示（从角色信息到音色维度）
- 身份锚点 ← 外貌描述（appearance）
- 声音质感 ← 性格 + 口吻（personality + speaking_style）
- 语速节奏 ← 口吻（speaking_style）
- 情绪底色 ← 性格 + 动机（personality + motivation）
- 风格标签 ← 叙事功能（role_in_story）
- 小癖好 ← 台词样本里的习惯性用词/语气（quotes）

### 范例
中年男性，节奏极快，情绪高亢，拍卖师风格。吐字连珠，带抑扬顿挫与紧迫感。
青年男性，电竞解说风格，语速极快且连贯，带明显气口和爆发性强调，兴奋时声音上扬尖锐。
中年男性，法庭陈词风格，声线沉稳偏正式，吐字工整字字顿挫，情绪克制但每个词都很重。"""

# ---------------------------------------------------------------------------
# Voice tag guide - injected into role-play system prompt
# Agent uses these tags when outputting dialogue with tts_speak.
# Source: MiMo-Skills SKILL.md "音频标签控制"
# ---------------------------------------------------------------------------

VOICE_TAG_GUIDE = """\
## 语音标签（输出对白时可内联，控制情绪/节奏）

### 内联标签（中文用 [标签]，放在文本任意位置）
- **节奏**：[停顿] [长停顿] [急促] [拖音] [语速加快] [语速放缓]
- **情绪**：[轻声] [低语] [叹气] [吸气] [哽咽] [强调] [笑] [爽朗大笑]
- **其他**：[欲言又止] [碎碎念] [沉默片刻]

### 整体风格标签（开头添加 `(风格)`）
- 基础情绪：开心/悲伤/愤怒/恐惧/惊讶/兴奋/委屈/平静/冷漠
- 复合情绪：怅然/欣慰/无奈/愧疚/释然/嫉妒/厌倦/忐忑/动情
- 整体语调：温柔/高冷/活泼/严肃/慵懒/俏皮/深沉/干练/凌厉
- 音色定位：磁性/醇厚/清亮/空灵/稚嫩/苍老/甜美/沙哑

### 规则
- **[标签] 只放声音**（语气/情绪/呼吸/笑声/叹气）。
- **`*动作*` 一律不许**：*清嗓子*、*苦笑*、*沉默了很久*、*抬起目光* 都不要。
- **默认不用标签。** 一条回复最多两个内联标签，同一句最多一个。
- 标签语言跟随正文（中文正文配中文标签）。"""
