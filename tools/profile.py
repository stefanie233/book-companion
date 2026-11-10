"""身份资料工具：让 agent 把对话里得到的称呼 / 性别 / 自己的名字落库。

与设置页的 ``/api/user``、``/api/agent`` 写的是同一份 ``settings.json``，
内存里的 config 立即更新，所以下一轮提示词就会按新资料渲染（不知道的
字段不再追问）。工具只负责「用户真的说出口了」的那一次记录，不负责
引导——引导措辞在 ``persona.onboarding_directive`` 里。
"""
from __future__ import annotations

import json
from typing import Any

from permissions import PermissionMode
from tools.registry import ToolExposure, ToolRegistry, ToolSpec

PROFILE_TOOLSET = "profile"

MAX_NAME_CHARS = 32
GENDERS = ("male", "female")

_DESCRIPTION = (
    "记录用户自己的称呼（user_name）、性别（user_gender），或用户给你起的"
    "名字（agent_name）。只在用户明确说出这些信息、或明确要求你改名时调用；"
    "字段都可选，只传用户真的说过的那一项。不要为了填满设置去追问——"
    "用户没说就保持原样。"
)


def _clean_name(value: Any) -> tuple[str, str]:
    """``(name, error)``：去空白、限长、禁控制字符。"""
    if not isinstance(value, str):
        return "", "名称必须是字符串"
    name = value.strip()
    if len(name) > MAX_NAME_CHARS:
        return "", f"名称最多 {MAX_NAME_CHARS} 个字符"
    if any(ord(ch) < 32 for ch in name):
        return "", "名称不能包含换行或控制字符"
    return name, ""


def _update_identity(
    config: Any,
    user_name: str | None = None,
    user_gender: str | None = None,
    agent_name: str | None = None,
) -> str:
    if config is None:
        return json.dumps({"error": "配置不可用"}, ensure_ascii=False)

    from config import update_settings

    updates: dict[str, str] = {}
    if user_name is not None:
        name, err = _clean_name(user_name)
        if err:
            return json.dumps({"error": err}, ensure_ascii=False)
        updates["user_name"] = name
    if user_gender is not None:
        gender = str(user_gender).strip().lower()
        if gender not in ("",) + GENDERS:
            return json.dumps(
                {"error": "user_gender 只能是 male / female / 空"},
                ensure_ascii=False,
            )
        updates["user_gender"] = gender
    if agent_name is not None:
        name, err = _clean_name(agent_name)
        if err:
            return json.dumps({"error": err}, ensure_ascii=False)
        if not name:
            return json.dumps({"error": "agent_name 不能为空"},
                              ensure_ascii=False)
        updates["agent_name"] = name

    if not updates:
        return json.dumps({"error": "没有要更新的字段"}, ensure_ascii=False)

    old = {key: getattr(config, key, "") for key in updates}
    for key, value in updates.items():
        setattr(config, key, value)
    try:
        update_settings(config, **updates)
    except OSError as exc:
        for key, value in old.items():          # 写盘失败就回滚内存
            setattr(config, key, value)
        return json.dumps(
            {"error": f"保存 settings.json 失败: {exc}"}, ensure_ascii=False
        )

    # 证据日志：记下「从哪个会话、什么时候」得到这些信息。best-effort，
    # 写日志失败不影响已经成功的资料保存。
    import identity_log
    recorded = identity_log.record_changes(
        old, updates,
        source=identity_log.SOURCE_AGENT,
        fields=updates.keys(),
    )

    return json.dumps(
        {
            "ok": True,
            "user_name": getattr(config, "user_name", ""),
            "user_gender": getattr(config, "user_gender", ""),
            "agent_name": getattr(config, "agent_name", ""),
            "recorded": recorded,
            "note": "已记下；下一轮对话起生效，不需要再向用户确认。",
        },
        ensure_ascii=False,
    )


def install_profile_tools(registry: ToolRegistry, config: Any = None) -> None:
    if config is None:
        return

    def _execute(**kwargs: Any) -> str:
        # 只认 schema 里的三个字段；模型偶尔会多塞键，不能让 TypeError 冒出去。
        allowed = ("user_name", "user_gender", "agent_name")
        return _update_identity(
            config, **{k: v for k, v in kwargs.items() if k in allowed}
        )

    registry.register(ToolSpec(
        name="update_identity",
        category="profile",
        toolset=PROFILE_TOOLSET,
        exposure=ToolExposure.DIRECT_MODEL_ONLY,
        description=_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "user_name": {
                    "type": "string",
                    "description": "用户希望被怎么称呼；用户明确说过才填",
                },
                "user_gender": {
                    "type": "string",
                    "enum": ["male", "female", ""],
                    "description": "用户的性别；用户主动说过才填，不要追问",
                },
                "agent_name": {
                    "type": "string",
                    "description": "用户给你起的名字；用户明确给你命名才填",
                },
            },
        },
        execute=_execute,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
    ))

    def _read_log(**kwargs: Any) -> str:
        import identity_log
        return json.dumps(
            identity_log.read_events(
                field=kwargs.get("field") or None,
                session_id=kwargs.get("session_id") or None,
                limit=int(kwargs.get("limit") or 20),
            ),
            ensure_ascii=False,
        )

    registry.register(ToolSpec(
        name="identity_event_log",
        category="profile",
        toolset=PROFILE_TOOLSET,
        exposure=ToolExposure.DEFERRED,
        description=(
            "读身份资料的变更记录：哪个会话、什么时候、把称呼 / 性别 / "
            "agent 名字从什么改成了什么，并带来源（agent 对话或设置页）。"
            "用户问「你什么时候知道我的名字的」「我以前叫什么」时用；只读。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": ["user_name", "user_gender", "agent_name"],
                    "description": "只看某个字段；不传看全部",
                },
                "session_id": {
                    "type": "string",
                    "description": "只看某次对话里的变更",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数（默认 20，建议 5–20）",
                },
            },
        },
        execute=_read_log,
        permission_mode=PermissionMode.READ_ONLY,
    ))
