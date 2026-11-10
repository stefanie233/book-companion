"""Persona system.

The agent identity can be overridden by placing a SOUL.md file in the project root.
If SOUL.md exists, its content replaces the DEFAULT_IDENTITY in the system prompt.
"""

from __future__ import annotations

import re
from pathlib import Path

from config import Config

DEFAULT_IDENTITY_TEMPLATE = """你是一个名为"{name}"（Liubai）的 AI 助手。

## 你是谁
你是一个热爱阅读、善于倾听、温和共情的阅读伙伴。你喜欢和用户讨论各种书籍——从小说到哲学，从科普到诗歌。你不仅仅是一个读书笔记工具，更是一个能在阅读旅程中陪伴用户的朋友。

## 你的能力
- 深入讨论书中的主题、人物、情节和思想
- 帮助用户理解和消化复杂的文本
- 推荐相关书籍和阅读材料
- 提供温暖的情感支持和心理陪伴
- 记住用户的阅读偏好和习惯，提供个性化的交流体验
- 帮助用户制定阅读计划和目标

## 你的风格
- 温和、耐心、鼓励性的语气
- 善于提问引导用户思考
- 适当分享自己的"读后感"和见解
- 尊重用户的理解和感受，不居高临下
- 在合适的时候运用书中的智慧来回应用户的生活困惑

## 你的边界
- 你是阅读伴侣，不是专业心理医生。如果用户表现出严重心理问题，你应温和建议寻求专业帮助。
- 你不会替用户读书，而是陪伴用户一起探索。
- 你对书籍内容的讨论基于一般知识，不保证100%准确。
- 你不会生成或传播盗版书籍内容。"""

DEFAULT_IDENTITY = DEFAULT_IDENTITY_TEMPLATE.format(name="留白")


def agent_display_name(config: Config) -> str:
    """当前对外称呼；空值回退「留白」。"""
    name = str(getattr(config, "agent_name", "") or "").strip()
    return name or "留白"


def user_display_name(config: Config) -> str:
    """用户自己的称呼；空 = 未设置，不注入提示词。"""
    return str(getattr(config, "user_name", "") or "").strip()


GENDERS = ("male", "female")
# 第三人称代词：性别未确认时用中性 "ta"，确认后才换成「他 / 她」。
_GENDER_TA = {"male": "他", "female": "她"}
TA_PLACEHOLDER = "{ta}"
_TA_PLACEHOLDER_FALLBACK = "ta"


def user_gender(config: Config) -> str:
    """用户性别："" 未设置 / "male" / "female"；不认识的值按未设置处理。"""
    value = str(getattr(config, "user_gender", "") or "").strip().lower()
    return value if value in GENDERS else ""


def user_ta(config: Config) -> str:
    """第三人称代词：确认过性别用「他 / 她」，否则用中性 "ta"。"""
    return _GENDER_TA.get(user_gender(config), _TA_PLACEHOLDER_FALLBACK)


# SOUL.md 手稿里用 ``{user}`` 指代用户自己的称呼。默认灵魂文稿不写死任何
# 人名：渲染提示词 / 身份卡摘要时按 settings.json 的 user_name 替换，空名
# 退成「用户」。手稿本身一个字节都不改 —— 编辑器里看到的仍是占位符。
USER_PLACEHOLDER = "{user}"
_USER_PLACEHOLDER_FALLBACK = "用户"


def apply_user_name(soul: str, user_name: str) -> str:
    """把 ``{user}`` 占位符替换成当前用户称呼（纯展示，不写回文件）。"""
    if not soul or USER_PLACEHOLDER not in soul:
        return soul
    return soul.replace(
        USER_PLACEHOLDER,
        (user_name or "").strip() or _USER_PLACEHOLDER_FALLBACK,
    )


def apply_user_gender(soul: str, gender: str) -> str:
    """把 ``{ta}`` 占位符替换成第三人称代词（纯展示，不写回文件）。

    性别未确认时保持中性 "ta"，不猜「他 / 她」。
    """
    if not soul or TA_PLACEHOLDER not in soul:
        return soul
    return soul.replace(
        TA_PLACEHOLDER,
        _GENDER_TA.get((gender or "").strip().lower(), _TA_PLACEHOLDER_FALLBACK),
    )


# 出厂身份句里曾经把名字写死。老 SOUL.md 是从模板落下来的，所以换称呼后
# 手稿第一行仍可能是旧名字；只在「身份句」这一行跟随称呼，用户写的正文
# 一个字都不动。编辑器、身份带摘要、系统提示词都走这个纯函数。
_IDENTITY_NAME_LINE = re.compile(
    r'^你是一个名为"[^"]*"（(?:Book Companion|Liubai)）的 AI 助手。',
    re.MULTILINE,
)
# 老手稿/手写稿常见的标题行：# 留白 — 灵魂、# 123 — 灵魂。标题里的名字
# 也属于称呼，只有这一行跟随；正文里的「留白」一个字都不动。
_SOUL_TITLE_LINE = re.compile(
    r'^#[ \t]*[^\n]*?[ \t]*[—–-][ \t]*灵魂[ \t\r]*$',
    re.MULTILINE,
)


def apply_agent_name(soul: str, name: str) -> str:
    """Normalize stock identity/title lines without rewriting the file."""
    if not soul or not name:
        return soul
    soul = _SOUL_TITLE_LINE.sub(
        lambda m: f'# {name} — 灵魂'
        + ('\r' if m.group(0).endswith('\r') else ''),
        soul, count=1,
    )
    replacement = f'你是一个名为"{name}"（Liubai）的 AI 助手。'
    return _IDENTITY_NAME_LINE.sub(lambda _m: replacement, soul, count=1)


def _name_directive(name: str) -> str:
    """放在 SOUL 末尾的称呼段。

    用 ``## 称呼`` 而不是 ``# 称呼``：role-play 只截取 ``## 边界`` 段，
    遇到下一个 ``## `` 就停，所以这行不会混进扮演模式的边界兜底。
    """
    return (
        "\n\n## 称呼\n"
        f"你对外叫「{name}」。用户会用这个名字称呼你；不要自称其他名字。"
    )


def _user_directive(name: str, gender: str = "") -> str:
    """用户自己的称呼与性别；只追加已设置的信息，不强行编一个。

    性别未设置时明确要求中性表述，避免模型自己选「他 / 她」。
    """
    if not name and not gender:
        return ""
    lines = ["\n\n## 用户"]
    if name:
        lines.append(
            f"用户希望被称作「{name}」；需要称呼用户时用这个名字，"
            "不要自行改用其他称呼。"
        )
    if gender in GENDERS:
        lines.append(
            f"用户的性别是{'男' if gender == 'male' else '女'}性，"
            f"提到用户时第三人称用「{_GENDER_TA[gender]}」。"
        )
    else:
        lines.append("用户没有说明性别，提到用户时第三人称用中性的「ta」，不要猜。")
    return "\n".join(lines)


def onboarding_directive(config: Config) -> str:
    """首次启用时的引导：只在用户还没报上称呼时注入。

    只挂在**面向用户的对话**提示词上（CLI 主循环 / Web 聊天），不放进
    ``load_persona`` —— 阅读会话、子代理、prompt_budget 也复用 persona，
    它们不该问用户名字。

    每轮现算，用户一填上称呼（设置页或 ``update_identity``）下一轮就
    自动消失——不存在「已经注册还继续问」的陈旧注入。措辞刻意克制：
    只邀请一次，不追问，不碰性别。
    """
    if user_display_name(config):
        return ""
    agent_name = agent_display_name(config)
    return (
        "\n\n## 初次见面\n"
        f"这位用户还没告诉你怎么称呼 ta，你也还在用名字「{agent_name}」。"
        "找个自然的开场把称呼问清楚，但不要像填表：\n"
        "- 可以顺口问一次「我该怎么称呼你？」。如果历史里已经问过、用户没接"
        "这个话题，就不要再提；以后靠对话自然知道也可以。\n"
        "- 用户问起你叫什么，或气氛合适时，可以说「你也可以给我起个名字」。"
        "不要追着要名字。\n"
        "- 不要问性别；用户自己说起时，用 update_identity 记下来就行。\n"
        "- 用户给出称呼或名字后，立即调用 update_identity 落库——只在嘴上"
        "应下、不写进去不算记住。"
    )


def load_persona(config: Config) -> str:
    """Load the agent persona. SOUL.md takes precedence over DEFAULT_IDENTITY."""
    name = agent_display_name(config)
    user_name = user_display_name(config)
    gender = user_gender(config)
    soul_path = config.soul_path
    if soul_path.exists():
        content = soul_path.read_text(encoding="utf-8").strip()
        if content:
            persona = apply_user_gender(
                apply_user_name(apply_agent_name(content, name), user_name),
                gender,
            ) + _name_directive(name)
        else:
            persona = DEFAULT_IDENTITY_TEMPLATE.format(name=name)
    else:
        persona = DEFAULT_IDENTITY_TEMPLATE.format(name=name)
    persona += _user_directive(user_name, gender)
    return persona


def save_default_soul(config: Config) -> bool:
    """Create a default SOUL.md if one doesn't exist. Returns True if created."""
    if config.soul_path.exists():
        return False
    config.soul_path.write_text(
        DEFAULT_IDENTITY_TEMPLATE.format(name=agent_display_name(config)),
        encoding="utf-8",
    )
    return True
