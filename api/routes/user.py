"""User profile routes — the reader's own display name and avatar.

This is deliberately separate from ``/api/agent``: the agent has a persona
(SOUL.md) and a称呼; the user has only a lightweight identity used by the
settings strip, chat bubbles, and an optional ``## 用户`` prompt segment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aiohttp import web

from api.routes.agent import (
    AVATAR_EXTS,
    AVATAR_MIME,
    MAX_AVATAR_BYTES,
    _image_ext,
)

MAX_NAME_CHARS = 32
GENDERS = ("male", "female")   # "" = 未设置


def _config(request: web.Request):
    config = request.app.get("config")
    if config is None:
        runtime = request.app.get("runtime")
        config = getattr(runtime, "config", None) if runtime else None
    return config


def _name(config) -> str:
    return str(getattr(config, "user_name", "") or "").strip()


def _gender(config) -> str:
    value = str(getattr(config, "user_gender", "") or "").strip().lower()
    return value if value in GENDERS else ""


def _avatar_dir(config) -> Path:
    return config.project_dir / "data" / "user"


def _avatar_path(config) -> Path | None:
    directory = _avatar_dir(config)
    if not directory.exists():
        return None
    for ext in AVATAR_EXTS:
        path = directory / f"avatar{ext}"
        if path.exists():
            return path
    return None


def _avatar_url(config) -> str:
    path = _avatar_path(config)
    if path is None:
        return ""
    return f"/api/user/avatar?v={path.stat().st_mtime_ns}"


def _snapshot(config) -> dict:
    return {
        "name": _name(config),
        "gender": _gender(config),
        "avatar_url": _avatar_url(config),
        "has_avatar": _avatar_path(config) is not None,
        "max_name_chars": MAX_NAME_CHARS,
    }


async def handle_user_get(request: web.Request) -> web.Response:
    """GET /api/user — own name + avatar snapshot."""
    config = _config(request)
    if config is None:
        return web.json_response({"error": "config unavailable"}, status=503)
    return web.json_response(_snapshot(config))


async def handle_user_name_put(request: web.Request) -> web.Response:
    """PUT /api/user/name — set/clear the reader's name.

    Empty is a valid value here (unlike the agent's称呼): it means "don't
    inject a user name; let the agent use whatever the conversation gives it".
    """
    config = _config(request)
    if config is None:
        return web.json_response({"error": "config unavailable"}, status=503)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid JSON body"}, status=400)
    raw_name = body.get("name")
    if not isinstance(raw_name, str):
        return web.json_response({"error": "name 必须是字符串"}, status=400)
    name = raw_name.strip()
    if len(name) > MAX_NAME_CHARS:
        return web.json_response(
            {"error": f"名称最多 {MAX_NAME_CHARS} 个字符"}, status=400
        )
    if any(ord(ch) < 32 for ch in name):
        return web.json_response({"error": "名称不能包含换行或控制字符"}, status=400)
    old_name = getattr(config, "user_name", "")
    config.user_name = name
    from config import update_settings
    try:
        update_settings(config, user_name=name)
    except OSError as exc:
        config.user_name = old_name
        return web.json_response(
            {"error": f"保存 settings.json 失败: {exc}"}, status=500
        )
    # 证据日志：设置页没有对话会话，session_id 留空、来源记 settings。
    import identity_log
    identity_log.record_changes(
        {"user_name": old_name}, {"user_name": name},
        source=identity_log.SOURCE_SETTINGS,
    )
    return web.json_response({"ok": True, "name": name})


async def handle_user_gender_put(request: web.Request) -> web.Response:
    """PUT /api/user/gender — set/clear the reader's gender.

    ``""`` means "not set": prompts then use the neutral 「ta」 instead of
    guessing 「他 / 她」.
    """
    config = _config(request)
    if config is None:
        return web.json_response({"error": "config unavailable"}, status=503)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid JSON body"}, status=400)
    raw = body.get("gender", "")
    if not isinstance(raw, str):
        return web.json_response({"error": "gender 必须是字符串"}, status=400)
    gender = raw.strip().lower()
    if gender not in ("",) + GENDERS:
        return web.json_response(
            {"error": "gender 只能是 male / female / 空"}, status=400
        )
    old = getattr(config, "user_gender", "")
    config.user_gender = gender
    from config import update_settings
    try:
        update_settings(config, user_gender=gender)
    except OSError as exc:
        config.user_gender = old
        return web.json_response(
            {"error": f"保存 settings.json 失败: {exc}"}, status=500
        )
    import identity_log
    identity_log.record_changes(
        {"user_gender": old}, {"user_gender": gender},
        source=identity_log.SOURCE_SETTINGS,
    )
    return web.json_response({"ok": True, "gender": gender})


async def handle_user_avatar_get(request: web.Request) -> web.Response:
    """GET /api/user/avatar — serve the uploaded reader avatar."""
    config = _config(request)
    if config is None:
        return web.json_response({"error": "config unavailable"}, status=503)
    path = _avatar_path(config)
    if path is None:
        return web.json_response({"error": "avatar not found"}, status=404)
    return web.FileResponse(
        path=path,
        headers={
            "Content-Type": AVATAR_MIME.get(
                path.suffix.lower(), "application/octet-stream"
            ),
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


async def handle_user_avatar_post(request: web.Request) -> web.Response:
    """POST /api/user/avatar — upload/replace the reader avatar."""
    config = _config(request)
    if config is None:
        return web.json_response({"error": "config unavailable"}, status=503)
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError):
        return web.json_response({"error": "需要 multipart/form-data"}, status=400)

    file_field = None
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            file_field = field
            break
        field = await reader.next()
    if file_field is None:
        return web.json_response({"error": "未找到文件字段"}, status=400)

    data = b""
    while True:
        chunk = await file_field.read_chunk(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_AVATAR_BYTES:
            return web.json_response(
                {"error": "头像过大（上限 2MB）"}, status=413
            )
    if not data:
        return web.json_response({"error": "文件为空"}, status=400)

    ext = _image_ext(data)
    if not ext:
        return web.json_response(
            {"error": "只支持 PNG / JPEG / GIF / WebP 图片"}, status=400
        )

    directory = _avatar_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"avatar{ext}"
    tmp = directory / "avatar.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return web.json_response({"error": f"写入头像失败: {exc}"}, status=500)

    for old in directory.glob("avatar.*"):
        if old != dest and old.suffix.lower() in AVATAR_EXTS:
            old.unlink(missing_ok=True)

    return web.json_response({"ok": True, **_snapshot(config)})


async def handle_user_avatar_delete(request: web.Request) -> web.Response:
    """DELETE /api/user/avatar — remove the uploaded reader avatar."""
    config = _config(request)
    if config is None:
        return web.json_response({"error": "config unavailable"}, status=503)
    directory = _avatar_dir(config)
    if directory.exists():
        for old in directory.glob("avatar.*"):
            if old.suffix.lower() in AVATAR_EXTS:
                try:
                    old.unlink()
                except OSError as exc:
                    return web.json_response(
                        {"error": f"移除头像失败: {exc}"}, status=500
                    )
    return web.json_response({"ok": True, **_snapshot(config)})
