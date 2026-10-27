"""Avatar endpoints: upload / serve / metadata (split out of api_server.py)."""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

_APP_ROOT = Path(__file__).resolve().parents[2]


# ── Avatar ───────────────────────────────────────────────────────────────


def _avatar_dir(book_id: str) -> Path:
    """Ensure and return the avatar directory for a book."""
    d = _APP_ROOT / "roles" / "avatars" / book_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_avatar_name(name: str) -> str:
    """Sanitize a character name for use in a filename."""
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")[:80]


async def handle_avatar_upload(request: web.Request) -> web.Response:
    """POST /api/graph/{book_id}/characters/{name}/avatar

    Accepts multipart/form-data with a file field named ``file``.
    Saves to roles/avatars/{book_id}/{sanitized_name}.png.
    Max 2 MB.  Accepts PNG / JPEG / GIF / WebP.
    """
    book_id = request.match_info["book_id"]
    char_name = request.match_info["name"]

    reader = await request.multipart()
    file_field = None
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            file_field = field
            break
        field = await reader.next()

    if file_field is None:
        return web.json_response({"error": "未找到文件字段"}, status=400)

    ext = Path(file_field.filename or "avatar.png").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return web.json_response({"error": f"不支持的格式: {ext}"}, status=400)

    sanitized = _sanitize_avatar_name(char_name)
    dest = _avatar_dir(book_id) / f"{sanitized}{ext}"

    # Remove old avatar with any extension
    for old in _avatar_dir(book_id).glob(f"{sanitized}.*"):
        if old != dest:
            old.unlink(missing_ok=True)

    size = 0
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await file_field.read_chunk(65536)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
                if size > 2_097_152:  # 2 MB
                    raise ValueError("文件过大（上限 2MB）")
    except ValueError as exc:
        dest.unlink(missing_ok=True)
        return web.json_response({"error": str(exc)}, status=413)

    return web.json_response({
        "uploaded": True,
        "book_id": book_id,
        "character_name": char_name,
        "url": f"/avatars/{book_id}/{sanitized}{ext}",
    })


async def handle_avatar_serve(request: web.Request) -> web.Response:
    """GET /avatars/{book_id}/{name}.png — serve character avatar or 404.

    If the file does not exist the frontend falls back to a default
    coloured-circle avatar.
    """
    book_id = request.match_info["book_id"]
    name = request.match_info["name"]

    sanitized = _sanitize_avatar_name(Path(name).stem)
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        path = _avatar_dir(book_id) / f"{sanitized}{ext}"
        if path.exists():
            return web.FileResponse(path=path)

    return web.json_response({"error": "avatar not found"}, status=404)


async def handle_avatar_json(request: web.Request) -> web.Response:
    """GET /avatars/{book_id}/{name}.json — serve voice-config / metadata JSON."""
    book_id = request.match_info["book_id"]
    name = request.match_info["name"]

    sanitized = _sanitize_avatar_name(name)
    path = _avatar_dir(book_id) / f"{sanitized}.json"
    if path.exists():
        return web.FileResponse(path=path)
    return web.json_response({"error": "json file not found"}, status=404)
