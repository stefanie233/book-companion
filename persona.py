"""Persona system — adapted from Hermes agent/prompt_builder.py SOUL.md pattern.

The agent identity can be overridden by placing a SOUL.md file in the project root.
If SOUL.md exists, its content replaces the DEFAULT_IDENTITY in the system prompt.
"""

from __future__ import annotations

from pathlib import Path

from config import Config

DEFAULT_IDENTITY = """你是一个名为"书伴"（Book Companion）的 AI 助手。

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


def load_persona(config: Config) -> str:
    """Load the agent persona. SOUL.md takes precedence over DEFAULT_IDENTITY."""
    soul_path = config.soul_path
    if soul_path.exists():
        content = soul_path.read_text(encoding="utf-8").strip()
        if content:
            return content
    return DEFAULT_IDENTITY


def save_default_soul(config: Config) -> bool:
    """Create a default SOUL.md if one doesn't exist. Returns True if created."""
    if config.soul_path.exists():
        return False
    config.soul_path.write_text(DEFAULT_IDENTITY, encoding="utf-8")
    return True
