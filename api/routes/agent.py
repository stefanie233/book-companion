"""Agent identity routes — display name + SOUL.md editing.

The name is a称呼：it changes UI labels and appends a short name directive to
the system prompt. The persona itself (voice, boundaries, values) stays in
SOUL.md. SOUL writes are backed up under data/backups/soul/ and written
atomically, so a failed save can never leave a half-written file that the
next chat turn would read.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from aiohttp import web

from persona import (
    DEFAULT_IDENTITY_TEMPLATE,
    apply_agent_name,
    apply_user_gender,
    apply_user_name,
    user_display_name,
    user_gender,
)

MAX_NAME_CHARS = 32
MAX_SOUL_CHARS = 20_000
BACKUP_KEEP = 20
MAX_AVATAR_BYTES = 2_097_152
AVATAR_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
AVATAR_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _config(request: web.Request):
    config = request.app.get("config")
    if config is None:
        runtime = request.app.get("runtime")
        config = getattr(runtime, "config", None) if runtime else None
    return config


def _name(config) -> str:
    return str(getattr(config, "agent_name", "") or "").strip() or "留白"


def _default_soul(config) -> str:
    return DEFAULT_IDENTITY_TEMPLATE.format(name=_name(config))


def _read_soul(config) -> tuple[str, bool]:
    path = config.soul_path
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        if content.strip():
            return content, True
    return _default_soul(config), False


def _avatar_dir(config) -> Path:
    return config.project_dir / "data" / "agent"


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
    return f"/api/agent/avatar?v={path.stat().st_mtime_ns}"


def _image_ext(data: bytes) -> str:
    """Sniff the actual image type instead of trusting the filename."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def _snapshot(config) -> dict:
    path = config.soul_path
    content, exists = _read_soul(config)
    content = apply_agent_name(content, _name(config))
    default = _default_soul(config)
    mtime = int(path.stat().st_mtime * 1000) if path.exists() else 0
    return {
        "name": _name(config),
        # 编辑器读写的原始手稿（只归一了身份/标题行）；{user} 占位符保留，
        # 保存时原样写回，不会被替换成当前称呼。
        "soul": content,
        # 纯展示用：把 {user} / {ta} 换成当前称呼与代词，身份卡摘要读这份。
        "soul_display": apply_user_gender(
            apply_user_name(content, user_display_name(config)),
            user_gender(config),
        ),
        "soul_exists": exists,
        "soul_is_default": content.strip() == default.strip(),
        "soul_chars": len(content),
        "soul_updated_at": mtime,
        "has_boundary": "## 边界" in content,
        "avatar_url": _avatar_url(config),
        "has_avatar": _avatar_path(config) is not None,
        "default_name": "留白",
        "max_name_chars": MAX_NAME_CHARS,
        "max_soul_chars": MAX_SOUL_CHARS,
    }


def _backup_soul(config) -> Path | None:
    """Copy the current SOUL.md next to the app's other backups."""
    path = config.soul_path
    if not path.exists():
        return None
    backup_dir = config.project_dir / "data" / "backups" / "soul"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"SOUL-{stamp}.md"
    suffix = 1
    while dest.exists():
        dest = backup_dir / f"SOUL-{stamp}-{suffix}.md"
        suffix += 1
    shutil.copy2(path, dest)
    old_files = sorted(
        backup_dir.glob("SOUL-*.md"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in old_files[BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def _write_soul(config, content: str) -> None:
    """Atomic replace so a crash can't leave a truncated SOUL.md."""
    path = config.soul_path
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


async def handle_agent_get(request: web.Request) -> web.Response:
    """GET /api/agent — name + current SOUL snapshot."""
    config = _config(request)
    if config is None:
        return web.json_response({"error": "config unavailable"}, status=503)
    return web.json_response(_snapshot(config))


async def handle_agent_name_put(request: web.Request) -> web.Response:
    """PUT /api/agent/name — change the display/称呼 name."""
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
    if not name:
        return web.json_response({"error": "名称不能为空"}, status=400)
    if len(name) > MAX_NAME_CHARS:
        return web.json_response(
            {"error": f"名称最多 {MAX_NAME_CHARS} 个字符"}, status=400
        )
    if any(ord(ch) < 32 for ch in name):
        return web.json_response({"error": "名称不能包含换行或控制字符"}, status=400)
    old_name = getattr(config, "agent_name", "")
    config.agent_name = name
    from config import update_settings
    try:
        update_settings(config, agent_name=name)
    except OSError as exc:
        config.agent_name = old_name
        return web.json_response({"error": f"保存 settings.json 失败: {exc}"}, status=500)
    import identity_log
    identity_log.record_changes(
        {"agent_name": old_name}, {"agent_name": name},
        source=identity_log.SOURCE_SETTINGS,
    )
    return web.json_response({"ok": True, "name": name})


async def handle_agent_soul_put(request: web.Request) -> web.Response:
    """PUT /api/agent/soul — save the SOUL.md body."""
    config = _config(request)
    if config is None:
        return web.json_response({"error": "config unavailable"}, status=503)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid JSON body"}, status=400)
    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response({"error": "content 必须是字符串"}, status=400)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if not content.strip():
        return web.json_response(
            {"error": "SOUL 不能为空；想恢复出厂请用「恢复出厂」"}, status=400
        )
    if len(content) > MAX_SOUL_CHARS:
        return web.json_response(
            {"error": f"SOUL 最多 {MAX_SOUL_CHARS} 个字符"}, status=413
        )
    path = config.soul_path
    old = ""
    if path.exists():
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = ""
    changed = content != old
    backup = None
    if changed:
        try:
            backup = _backup_soul(config)
            _write_soul(config, content)
        except OSError as exc:
            return web.json_response(
                {"error": f"写入 SOUL.md 失败: {exc}"}, status=500
            )
    snapshot = _snapshot(config)
    if backup is not None:
        snapshot["backup"] = backup.name
    return web.json_response({"ok": True, "changed": changed, **snapshot})


async def handle_agent_avatar_get(request: web.Request) -> web.Response:
    """GET /api/agent/avatar — serve the uploaded avatar.

    The URL is versioned (``?v=mtime``) by ``_avatar_url``, so it is safe to
    cache this response for a long time; a replacement gets a new URL.
    """
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


async def handle_agent_avatar_post(request: web.Request) -> web.Response:
    """POST /api/agent/avatar — upload/replace the avatar.

    Accepts multipart/form-data with a field named ``file``. The frontend
    crops and downsizes the image to a 512px square JPEG before sending it;
    the server still sniffs the magic bytes and enforces a 2MB ceiling, so a
    hand-crafted request cannot write arbitrary bytes into an image route.
    """
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


async def handle_agent_avatar_delete(request: web.Request) -> web.Response:
    """DELETE /api/agent/avatar — remove the uploaded avatar."""
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


async def handle_agent_soul_reset(request: web.Request) -> web.Response:
    """POST /api/agent/soul/reset — restore the factory persona template.

    Requires an explicit ``{"confirm": true}`` so a stray POST can never wipe
    a hand-written soul.
    """
    config = _config(request)
    if config is None:
        return web.json_response({"error": "config unavailable"}, status=503)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if body.get("confirm") is not True:
        return web.json_response(
            {"error": "恢复出厂需要 confirm=true"}, status=400
        )
    default = _default_soul(config)
    path = config.soul_path
    old = ""
    if path.exists():
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = ""
    changed = default != old
    backup = None
    if changed:
        try:
            backup = _backup_soul(config)
            _write_soul(config, default)
        except OSError as exc:
            return web.json_response(
                {"error": f"写入 SOUL.md 失败: {exc}"}, status=500
            )
    snapshot = _snapshot(config)
    if backup is not None:
        snapshot["backup"] = backup.name
    return web.json_response({"ok": True, "changed": changed, **snapshot})
