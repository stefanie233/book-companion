"""Voice tools: TTS, voice design / clone / lookup."""
from __future__ import annotations


import json
from pathlib import Path
from typing import Any
from permissions import PermissionMode
from tools.registry import ToolExposure, ToolRegistry, ToolSpec


def _read_audio_as_b64(path) -> str | None:
    """Read audio file and return data URI for voiceclone API."""
    from pathlib import Path
    import base64
    p = Path(path) if not isinstance(path, Path) else path
    if not p.exists():
        return None
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("utf-8")
    ext = p.suffix.lower()
    mime = "audio/mpeg" if ext in (".mp3", ".mpeg") else "audio/wav"
    return f"data:{mime};base64,{b64}"


def _resolve_voice(voice: str, book_id: str, wingman_db: Any,
                   style: str | None = None) -> dict:
    """Resolve voice parameter to {model, voice_value, user_msg, error}.

    voice forms:
      - preset name ("茉莉")          -> mimo-v2.5-tts
      - "clone:<name>"                 -> mimo-v2.5-tts-voiceclone (ref audio from CLONE_VOICES)
      - "design:<prompt>"              -> mimo-v2.5-tts-voicedesign
      - character name                 -> lookup voice_roles -> voiceclone (or error)
      - "clone_xxx" (legacy voice_id)  -> backward compat (preset path)
    """
    from pathlib import Path
    from voices import PRESET_VOICE_NAMES, CLONE_VOICE_MAP

    result: dict = {"model": "", "voice_value": None, "user_msg": None, "error": None}

    # 1. Preset voice
    if voice in PRESET_VOICE_NAMES:
        result["model"] = "mimo-v2.5-tts"
        result["voice_value"] = voice
        if style:
            result["user_msg"] = style
        return result

    # 2. clone:<name> - user DIY reference audio
    if voice.startswith("clone:"):
        clone_name = voice[6:]
        clone = CLONE_VOICE_MAP.get(clone_name)
        if not clone:
            result["error"] = f"未找到克隆音色 '{clone_name}'，请检查 voices.py 的 CLONE_VOICES"
            return result
        ref_path = Path(__file__).parent / clone["ref_audio"]
        b64 = _read_audio_as_b64(ref_path)
        if not b64:
            result["error"] = f"克隆参考音频文件不存在: {clone['ref_audio']}"
            return result
        result["model"] = "mimo-v2.5-tts-voiceclone"
        result["voice_value"] = b64
        if style:
            result["user_msg"] = style
        return result

    # 3. design:<prompt> - ad-hoc voicedesign
    if voice.startswith("design:"):
        prompt = voice[7:]
        result["model"] = "mimo-v2.5-tts-voicedesign"
        result["user_msg"] = (prompt + " " + style) if style else prompt
        return result

    # 4. Legacy clone_xxx voice_id - temporary backward compat
    if voice.startswith("clone_"):
        result["model"] = "mimo-v2.5-tts"
        result["voice_value"] = voice
        if style:
            result["user_msg"] = style
        return result

    # 5. Character name - lookup voice_roles
    if wingman_db and book_id:
        role = wingman_db.get_voice_role(book_id, voice)
        if role:
            ref_path = Path(__file__).parent / role["ref_audio_path"]
            b64 = _read_audio_as_b64(ref_path)
            if b64:
                result["model"] = "mimo-v2.5-tts-voiceclone"
                result["voice_value"] = b64
                if style:
                    result["user_msg"] = style
                return result
            result["error"] = f"角色 '{voice}' 的参考音频文件丢失，请重新铸造"
            return result
        result["error"] = f"角色 '{voice}' 尚无音色，请先调 voice_design 铸造"
        return result

    # No book_id context - fallback to preset default
    result["model"] = "mimo-v2.5-tts"
    result["voice_value"] = "茉莉"
    if style:
        result["user_msg"] = style
    return result


def _tool_tts_speak(text: str, voice: str = "茉莉", style: str = "",
                    *, config: Any = None, wingman_db: Any = None,
                    book_id: str = "") -> str:
    """Sync fallback - uses urllib to avoid asyncio.run() in running loop."""
    import base64
    import hashlib
    import urllib.request
    from pathlib import Path

    text = text.strip()[:500]
    if not text:
        return json.dumps({"error": "text is required"})

    resolved = _resolve_voice(voice, book_id, wingman_db, style or None)
    if resolved.get("error"):
        return json.dumps({"error": resolved["error"]}, ensure_ascii=False)

    # Console TTS slot model overrides the voice-kind mapping when set
    model = config.tts_model or resolved["model"]
    voice_value = resolved.get("voice_value")
    user_msg = resolved.get("user_msg")

    voice_dir = Path(__file__).parent / "data" / "voice" / "tts"
    voice_dir.mkdir(parents=True, exist_ok=True)

    cache_payload = f"{text}|{model}|{voice_value or ''}|{user_msg or ''}"
    cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()[:16]
    cache_path = voice_dir / f"tts_{cache_key}.wav"

    if cache_path.exists():
        return json.dumps({
            "type": "voice",
            "url": f"/voice/tts_{cache_key}.wav",
            "text": text[:100],
            "voice": voice,
        }, ensure_ascii=False)

    messages = []
    if user_msg:
        messages.append({"role": "user", "content": user_msg})
    messages.append({"role": "assistant", "content": text})

    audio: dict = {"format": "wav"}
    if voice_value:
        audio["voice"] = voice_value

    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "audio": audio,
    }).encode("utf-8")

    tts_key = config.tts_api_key or config.api_key
    req = urllib.request.Request(
        f"{config.tts_api_base or config.api_base}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {tts_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        return json.dumps({"error": f"TTS failed: {exc}"})

    choices = data.get("choices", [])
    if not choices:
        return json.dumps({"error": "TTS returned no data"})

    audio_b64 = choices[0].get("message", {}).get("audio", {}).get("data", "")
    if not audio_b64:
        return json.dumps({"error": "TTS returned no audio"})

    if "," in audio_b64:
        audio_b64 = audio_b64.split(",", 1)[1]

    cache_path.write_bytes(base64.b64decode(audio_b64))

    return json.dumps({
        "type": "voice",
        "url": f"/voice/tts_{cache_key}.wav",
        "text": text[:100],
        "voice": voice,
    }, ensure_ascii=False)


async def _tool_tts_speak_async(text: str, voice: str = "茉莉", style: str = "",
                                *, config: Any = None, wingman_db: Any = None,
                                book_id: str = "") -> str:
    """Call MiMo TTS and return the audio URL for the frontend to render."""
    import base64
    import hashlib
    import aiohttp
    from pathlib import Path

    text = text.strip()[:500]
    if not text:
        return json.dumps({"error": "text is required"})

    resolved = _resolve_voice(voice, book_id, wingman_db, style or None)
    if resolved.get("error"):
        return json.dumps({"error": resolved["error"]}, ensure_ascii=False)

    # Console TTS slot model overrides the voice-kind mapping when set
    model = config.tts_model or resolved["model"]
    voice_value = resolved.get("voice_value")
    user_msg = resolved.get("user_msg")

    voice_dir = Path(__file__).parent / "data" / "voice" / "tts"
    voice_dir.mkdir(parents=True, exist_ok=True)

    cache_payload = f"{text}|{model}|{voice_value or ''}|{user_msg or ''}"
    cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()[:16]
    cache_path = voice_dir / f"tts_{cache_key}.wav"

    if cache_path.exists():
        return json.dumps({
            "type": "voice",
            "url": f"/voice/tts_{cache_key}.wav",
            "text": text[:100],
            "voice": voice,
        }, ensure_ascii=False)

    messages = []
    if user_msg:
        messages.append({"role": "user", "content": user_msg})
    messages.append({"role": "assistant", "content": text})

    audio: dict = {"format": "wav"}
    if voice_value:
        audio["voice"] = voice_value

    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "audio": audio,
    }
    tts_key = config.tts_api_key or config.api_key
    headers = {
        "Authorization": f"Bearer {tts_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.tts_api_base or config.api_base}/chat/completions",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return json.dumps({"error": f"TTS API error: {resp.status}"})
                data = await resp.json()
    except Exception as exc:
        return json.dumps({"error": f"TTS failed: {exc}"})

    choices = data.get("choices", [])
    if not choices:
        return json.dumps({"error": "TTS returned no data"})

    audio_b64 = choices[0].get("message", {}).get("audio", {}).get("data", "")
    if not audio_b64:
        return json.dumps({"error": "TTS returned no audio"})

    if "," in audio_b64:
        audio_b64 = audio_b64.split(",", 1)[1]

    cache_path.write_bytes(base64.b64decode(audio_b64))

    return json.dumps({
        "type": "voice",
        "url": f"/voice/tts_{cache_key}.wav",
        "text": text[:100],
        "voice": voice,
    }, ensure_ascii=False)


# ── Voice design / lookup / list tools ───────────────────────────────


async def _tool_voice_design_async(book_id: str, character_name: str, prompt: str,
                                    *, config: Any = None, wingman_db: Any = None) -> str:
    """Cast a character voice: voicedesign generates ref audio, stored for voiceclone reuse."""
    import base64
    import aiohttp
    from pathlib import Path
    from voices import VOICE_CAST_SAMPLE

    if not book_id or not character_name or not prompt:
        return json.dumps({"error": "book_id, character_name, prompt 都是必填"})
    if not wingman_db:
        return json.dumps({"error": "音色数据库不可用"})

    ref_dir = Path(__file__).parent / "data" / "voice" / "refs"
    ref_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize character name for filename
    safe_name = "".join(c for c in character_name if c.isalnum() or c in "._-") or "char"
    ref_filename = f"refs_{book_id}_{safe_name}.wav"
    ref_path = ref_dir / ref_filename
    ref_rel = f"data/voice/refs/{ref_filename}"

    body = {
        "model": "mimo-v2.5-tts-voicedesign",
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": VOICE_CAST_SAMPLE},
        ],
        "stream": False,
        "audio": {"format": "wav"},
    }
    tts_key = config.tts_api_key or config.api_key
    headers = {
        "Authorization": f"Bearer {tts_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.tts_api_base or config.api_base}/chat/completions",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    return json.dumps({"error": f"voicedesign API error: {resp.status}"})
                data = await resp.json()
    except Exception as exc:
        return json.dumps({"error": f"voicedesign failed: {exc}"})

    choices = data.get("choices", [])
    if not choices:
        return json.dumps({"error": "voicedesign returned no data"})

    audio_b64 = choices[0].get("message", {}).get("audio", {}).get("data", "")
    if not audio_b64:
        return json.dumps({"error": "voicedesign returned no audio"})

    if "," in audio_b64:
        audio_b64 = audio_b64.split(",", 1)[1]

    ref_path.write_bytes(base64.b64decode(audio_b64))

    # Persist to voice_roles table
    wingman_db.upsert_voice_role(book_id, character_name, prompt, ref_rel)

    return json.dumps({
        "type": "voice_design",
        "url": f"/voice/{ref_filename}",
        "character": character_name,
        "prompt": prompt[:200],
        "ref_audio": ref_rel,
    }, ensure_ascii=False)


def _tool_voice_role_lookup(character_name: str = "", *, wingman_db: Any = None,
                            reading_ctx: dict | None = None) -> str:
    """Look up a character's cast voice (design prompt + ref audio)."""
    if not wingman_db:
        return json.dumps({"error": "音色数据库不可用"})
    book_id = ""
    if reading_ctx:
        book_id = reading_ctx.get("book_id", "")
    if not book_id or not character_name:
        return json.dumps({"error": "book_id 和 character_name 都是必填"})
    role = wingman_db.get_voice_role(book_id, character_name)
    if not role:
        return json.dumps({"not_found": True, "character": character_name,
                           "hint": "该角色尚无音色，请调 voice_design 铸造"}, ensure_ascii=False)
    return json.dumps({
        "character": character_name,
        "prompt": role["design_prompt"],
        "ref_audio": role["ref_audio_path"],
        "has_audio": True,
    }, ensure_ascii=False)


def _tool_voice_list() -> str:
    """List available voices (preset + clone). Voicedesign is dynamic, not listed."""
    from voices import PRESET_VOICES, CLONE_VOICES
    presets = [{"name": v["name"], "gender": v.get("gender", ""),
                "desc": v.get("desc", "")} for v in PRESET_VOICES]
    clones = [{"name": v["name"], "gender": v.get("gender", ""),
               "desc": v.get("desc", "")} for v in CLONE_VOICES]
    return json.dumps({
        "preset": presets,
        "clone": clones,
        "design_hint": "角色音色是动态铸造的，调 voice_role_lookup 查询特定角色",
    }, ensure_ascii=False)


def _register_voice_tools(registry: ToolRegistry, config, wingman_db) -> None:
    # ── TTS + voice tools ─────────────────────────────────────────────
    if config is not None:
        # Capture reading_ctx for voice resolution (book_id -> character lookup)
        _reading_ctx = registry.reading_ctx

        registry.register(ToolSpec(
            name="tts_speak",
            category="voice",
            toolset="knowledge",
            exposure=ToolExposure.DIRECT,
            description=(
                "将文本转换为语音并播放。用于角色扮演时主动'说话'。"
                "voice 可传：预置音色名(茉莉/苏打/白桦等)、角色名(查曾用音色)、"
                "'clone:名'(用户DIY音色)、'design:描述'(即兴设计音色)。"
                "text 中可内联语音标签控制情绪节奏，如 [叹气]罢了、[停顿]你信我。"
                "style 可选，传入导演指令控制整体语气情绪。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要朗读的文本（建议 ≤500 字以保证生成速度）",
                    },
                    "voice": {
                        "type": "string",
                        "description": "音色标识：预置名/角色名/clone:名/design:描述。默认茉莉",
                    },
                    "style": {
                        "type": "string",
                        "description": "可选风格指令，如'轻快活泼'/'低沉愤怒'。控制本次语气",
                    },
                },
                "required": ["text"],
            },
            execute=lambda text, voice="茉莉", style="": _tool_tts_speak(
                text, voice, style, config=config, wingman_db=wingman_db,
                book_id=_reading_ctx.get("book_id", "")),
            execute_async=lambda text, voice="茉莉", style="": _tool_tts_speak_async(
                text, voice, style, config=config, wingman_db=wingman_db,
                book_id=_reading_ctx.get("book_id", "")),
            permission_mode=PermissionMode.READ_ONLY,
        ))

        # Voice design - cast a character's voice (voicedesign -> ref audio -> voiceclone reuse)
        if wingman_db is not None:
            registry.register(ToolSpec(
                name="voice_design",
                category="voice",
                toolset="knowledge",
                exposure=ToolExposure.DIRECT,
                description=(
                    "为角色铸造音色。用 voicedesign 模型基于描述生成参考音频，"
                    "后续该角色配音走 voiceclone 保证音色稳定。"
                    "prompt 按《音色设计规范》编写：身份锚点/声音质感/语速节奏/情绪底色。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "character_name": {"type": "string", "description": "角色名"},
                        "prompt": {
                            "type": "string",
                            "description": "音色描述，按音色设计规范编写",
                        },
                    },
                    "required": ["character_name", "prompt"],
                },
                execute=lambda character_name, prompt: json.dumps(
                    {"error": "voice_design 需要异步执行，请使用 async 路径"}),
                execute_async=lambda character_name, prompt: _tool_voice_design_async(
                    _reading_ctx.get("book_id", ""), character_name, prompt,
                    config=config, wingman_db=wingman_db),
                permission_mode=PermissionMode.READ_ONLY,
            ))

            registry.register(ToolSpec(
                name="voice_role_lookup",
                category="voice",
                toolset="knowledge",
                exposure=ToolExposure.DIRECT,
                description="查询角色曾用音色（复用入口）。返回 design prompt 和参考音频路径。",
                parameters={
                    "type": "object",
                    "properties": {
                        "character_name": {"type": "string", "description": "角色名"},
                    },
                    "required": ["character_name"],
                },
                execute=lambda character_name="": _tool_voice_role_lookup(
                    character_name, wingman_db=wingman_db, reading_ctx=_reading_ctx),
                permission_mode=PermissionMode.READ_ONLY,
            ))

        registry.register(ToolSpec(
            name="voice_list",
            category="voice",
            toolset="knowledge",
            exposure=ToolExposure.DIRECT,
            description="列出可用音色（预置 + 克隆）。角色音色是动态铸造的，不在列表中。",
            parameters={"type": "object", "properties": {}},
            execute=lambda: _tool_voice_list(),
            permission_mode=PermissionMode.READ_ONLY,
        ))
