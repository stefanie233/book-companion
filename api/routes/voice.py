"""Voice endpoints: TTS, ASR, voice-file upload/serve (split out of api_server.py)."""
from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from runtime_paths import data_path


# ── Voice (TTS + ASR) ────────────────────────────────────────────────────

# Lazy-loaded SenseVoiceSmall singleton (loaded once, reused across requests).
#
# 线程安全说明：后台预热跑在线程里，/api/asr 也经 asyncio.to_thread 跑在线程里，
# 两者可能同时首次触达这里。裸 check-then-set 会让并发调用各加载一份
# （实测 4 并发 = 加载 4 次，内存翻倍且全局变量被后写覆盖）。改为双检锁后
# 只加载一次，后到者阻塞等待、加载完成后复用——这同时天然实现了
# "预热尚未就绪时，ASR 请求等待而非失败"的语义。
_sense_voice_model: Any = None
_sense_voice_lock = threading.Lock()
# idle → loading → ready | failed，便于排查与观测
_SENSE_VOICE_STATE: dict = {"state": "idle", "error": None, "loaded_at": None}


def _get_sense_voice():
    """Return the global SenseVoiceSmall model, loading on first call.

    线程安全（双检锁）：并发调用只会真正加载一次，其余调用等待后复用同一实例。
    始终通过 asyncio.to_thread 调用——模型构造会短暂抢占 GIL
    （实测单次事件循环抖动约 1.5s），绝不能直接跑在事件循环上。
    """
    global _sense_voice_model
    if _sense_voice_model is not None:      # 快路径：已就绪，零锁开销
        return _sense_voice_model

    with _sense_voice_lock:
        if _sense_voice_model is not None:  # 双检：可能已被别的线程加载完
            return _sense_voice_model

        model_dir = data_path("models", "iic", "SenseVoiceSmall")
        if not model_dir.exists():
            _SENSE_VOICE_STATE.update(
                state="failed", error=f"model not found: {model_dir}")
            raise RuntimeError(
                f"SenseVoiceSmall model not found at {model_dir}. "
                "Download it: pip install modelscope && python scripts/download_asr_model.py"
            )

        _SENSE_VOICE_STATE["state"] = "loading"
        try:
            from funasr_onnx import SenseVoiceSmall
            model = SenseVoiceSmall(
                str(model_dir),
                batch_size=1,
                quantize=True,
                device_id=-1,  # CPU
            )
        except Exception as exc:
            _SENSE_VOICE_STATE.update(state="failed", error=str(exc))
            raise

        _sense_voice_model = model
        _SENSE_VOICE_STATE.update(
            state="ready", error=None,
            loaded_at=datetime.now().isoformat(timespec="seconds"),
        )
        return _sense_voice_model


async def warm_asr_background(grace_s: float = 2.5) -> None:
    """在后台预热 ASR 模型——不阻塞启动通道。

    调用方应把它排成后台任务（asyncio.create_task），而不是 await：
    HTTP 服务先开始监听，模型随后在后台线程加载。就绪之前到达的
    /api/asr 会在 _get_sense_voice 的锁上等待并复用结果，不会重复加载。

    grace_s: 先让出若干秒再开始，避开前端首屏加载期。模型构造阶段有
             一次约 1.5s 的 GIL 抢占（实测，仅此一次），错开可让抖动
             落在用户空闲窗口内。设为 0 表示立即开始。
    """
    if grace_s > 0:
        await asyncio.sleep(grace_s)

    if _sense_voice_model is not None:  # 已被首个 ASR 请求抢先加载
        return

    import tempfile
    import wave

    print("  ASR: SenseVoiceSmall 后台预热中（不阻塞启动）...")
    try:
        model = await asyncio.to_thread(_get_sense_voice)

        # dummy 推理：逼 ONNX 建好计算图与内存池。
        # 实测首次推理 6.2s、第二次仅 0.19s，预热可把这 6s 从用户首次录音中省掉。
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            dummy = tmp.name
        try:
            with wave.open(dummy, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 8000)  # 0.5s 静音
            await asyncio.to_thread(model, [dummy], language="auto", use_itn=True)
        finally:
            Path(dummy).unlink(missing_ok=True)

        print("  ASR: 后台预热完成，语音输入已就绪")
    except Exception as exc:
        # 不抛出：预热失败不应影响已启动的服务；首个 ASR 请求会自行重试加载
        _SENSE_VOICE_STATE.update(state="failed", error=str(exc))
        print(f"  ASR: 后台预热失败 ({exc})")



_VOICE_DIR = data_path("voice")
_VOICE_TTS_DIR = _VOICE_DIR / "tts"
_VOICE_REC_DIR = _VOICE_DIR / "recordings"
_VOICE_REFS_DIR = _VOICE_DIR / "refs"


def _voice_hash(text: str, voice: str, speed: float) -> str:
    """Stable cache key for TTS (text + voice + speed)."""
    import hashlib
    payload = f"{text}|{voice}|{speed:.1f}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _voice_find(name: str) -> Path | None:
    """Find a voice file across subdirectories (tts/ or recordings/)."""
    for d in (_VOICE_TTS_DIR, _VOICE_REC_DIR, _VOICE_REFS_DIR):
        p = d / f"{name}.wav"
        if p.exists():
            return p
    # Legacy: also check the root voice dir
    p = _VOICE_DIR / f"{name}.wav"
    return p if p.exists() else None


async def handle_tts(request: web.Request) -> web.Response:
    """POST /api/tts  — text-to-speech via MiMo TTS.

    Body: ``{"text": "...", "voice": "茉莉", "speed": 1.0}``
    Returns: ``{"url": "/voice/tts_{hash}.wav", "duration_ms": 2340}``

    Caches by (text + voice + speed) hash so identical requests hit disk.
    """
    import base64
    import hashlib

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text is required"}, status=400)
    voice = body.get("voice", "茉莉")
    speed = float(body.get("speed", 1.0))

    # ── Cache check ──────────────────────────────────────────────────
    _VOICE_TTS_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = _voice_hash(text, voice, speed)
    cache_path = _VOICE_TTS_DIR / f"tts_{cache_key}.wav"
    if cache_path.exists():
        return web.json_response({
            "url": f"/voice/tts_{cache_key}.wav",
            "voice": voice,
            "cached": True,
        })

    # ── Call MiMo TTS ────────────────────────────────────────────────
    config = request.app.get("config")
    if not config:
        return web.json_response({"error": "config not available"}, status=503)

    import aiohttp
    is_clone = voice.startswith("clone_")
    tts_opts = {"speed": speed}
    if is_clone:
        tts_opts["voice_id"] = voice
    else:
        tts_opts["voice"] = voice
    tts_body = {
        "model": "mimo-v2.5-tts",
        "messages": [
            {"role": "user", "content": "请用自然的声音朗读以下内容"},
            {"role": "assistant", "content": text},
        ],
        "stream": False,
        "extra_body": {"tts_options": tts_opts},
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.api_base}/chat/completions",
                json=tts_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    return web.json_response(
                        {"error": f"TTS API error: {resp.status}", "detail": err[:200]},
                        status=502,
                    )
                data = json.loads(await resp.text())
    except Exception as exc:
        return web.json_response({"error": f"TTS request failed: {exc}"}, status=502)

    choices = data.get("choices", [])
    if not choices:
        return web.json_response({"error": "TTS returned no data"}, status=502)

    audio_b64 = choices[0].get("message", {}).get("audio", {}).get("data", "")
    if not audio_b64:
        return web.json_response({"error": "TTS returned no audio"}, status=502)

    # Strip optional data URL prefix
    if "," in audio_b64:
        audio_b64 = audio_b64.split(",", 1)[1]

    wav_bytes = base64.b64decode(audio_b64)
    cache_path.write_bytes(wav_bytes)

    return web.json_response({
        "url": f"/voice/tts_{cache_key}.wav",
        "voice": voice,
        "cached": False,
    })


async def _asr_via_api(config, wav_path: str, model: str) -> dict:
    """Cloud ASR via the bound console ASR slot. Two protocols:
      ``mimo-`` prefix  → chat/completions + input_audio (MiMo multimodal)
      anything else     → OpenAI whisper-compatible /audio/transcriptions

    Credentials follow the TTS same-key fallback: ``asr_api_key/base`` from
    the console binding, else the main LLM ``api_key/api_base``.

    Raises on upstream failure/timeout; the caller maps it to HTTP 502.
    Returns ``{"text", "emotion", "language"}`` — emotion is always empty
    (cloud ASR has no emotion granularity; the frontend handles that).
    """
    import base64

    import aiohttp

    key = config.asr_api_key or config.api_key
    base = (config.asr_api_base or config.api_base).rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    timeout = aiohttp.ClientTimeout(total=30)

    # ── MiMo multimodal channel (chat/completions + input_audio) ──────
    if model.startswith("mimo-"):
        with open(wav_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:audio/wav;base64,{audio_b64}",
                            },
                        }
                    ],
                }
            ],
            "stream": False,
            "extra_body": {"asr_options": {"language": "auto"}},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/chat/completions",
                json=body,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(f"ASR API error: {resp.status} {err[:200]}")
                data = json.loads(await resp.text())
        choices = data.get("choices", [])
        text = (choices[0].get("message", {}).get("content", "") or "").strip()
        if not text:
            raise RuntimeError("ASR API returned empty text")
        return {"text": text, "emotion": "", "language": "auto"}

    # ── Whisper-compatible channel (/audio/transcriptions, multipart) ──
    with open(wav_path, "rb") as f:
        audio_bytes = f.read()
    content_type = "audio/webm" if wav_path.endswith(".webm") else "audio/wav"
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=Path(wav_path).name,
                   content_type=content_type)
    form.add_field("model", model)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base}/audio/transcriptions",
            data=form,
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise RuntimeError(f"ASR API error: {resp.status} {err[:200]}")
            data = json.loads(await resp.text())
    text = (data.get("text") or "").strip()
    if not text:
        raise RuntimeError("ASR API returned empty text")
    return {"text": text, "emotion": "", "language": "auto"}


async def handle_asr(request: web.Request) -> web.Response:
    """POST /api/asr  — speech-to-text.

    Accepts ``multipart/form-data`` with a ``file`` field (WAV/WebM).
    Returns ``{"text": "转写结果", "emotion": "HAPPY", "language": "zh"}``.

    Channel: if the console ASR slot is bound (``config.asr_model`` non-empty),
    audio goes to the cloud ASR API; otherwise the local SenseVoiceSmall
    engine handles it. The emotion field is optional — it may be empty for
    non-speech audio.
    """
    import re
    import tempfile

    reader = await request.multipart()
    file_field = None
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            file_field = field
            break
        field = await reader.next()

    if file_field is None:
        return web.json_response({"error": "未找到音频文件"}, status=400)

    audio_bytes = b""
    while True:
        chunk = await file_field.read_chunk(65536)
        if not chunk:
            break
        audio_bytes += chunk

    if not audio_bytes:
        return web.json_response({"error": "音频文件为空"}, status=400)

    # Write to temp file (SenseVoiceSmall needs a file path)
    suffix = ".wav"
    if file_field.filename and file_field.filename.endswith(".webm"):
        suffix = ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    config = request.app.get("config")
    asr_model = (getattr(config, "asr_model", "") or "").strip() if config else ""

    if asr_model:
        # ── Cloud channel: console ASR slot bound ─────────────────────
        try:
            result = await _asr_via_api(config, tmp_path, asr_model)
        except Exception as exc:
            return web.json_response({"error": f"ASR failed: {exc}"}, status=502)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return web.json_response(result)

    try:
        # Local SenseVoice channel.
        # Load in thread too — first call imports funasr_onnx (numpy chain),
        # which must never run on the event loop (GIL/import-lock freeze).
        model = await asyncio.to_thread(_get_sense_voice)
        # Inference in thread (funasr_onnx is sync, avoid blocking event loop)
        result = await asyncio.to_thread(
            model, [tmp_path], language="auto", use_itn=True,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not result or not result[0]:
        return web.json_response({"error": "ASR failed"}, status=502)

    raw = str(result[0])
    # Parse structured output: <|lang|><|emotion|><|event|><|itn|>text
    m = re.match(r'<\|(.+?)\|><\|(.+?)\|><\|(.+?)\|><\|(.+?)\|>(.*)', raw)
    if m:
        lang = m.group(1)
        emotion = m.group(2) if m.group(2) not in ("EMO_UNKNOWN", "") else ""
        text = m.group(5).strip()
        return web.json_response({
            "text": text,
            "emotion": emotion,
            "language": lang,
        })
    else:
        # Fallback: return raw output as text
        return web.json_response({"text": raw.strip()})


async def handle_voice_upload(request: web.Request) -> web.Response:
    """POST /api/voice/upload — persist a user recording.

    Accepts ``multipart/form-data`` with a ``file`` field (WAV).
    Returns ``{"url": "/voice/rec_{id}.wav", "duration_ms": 5000}``.
    """
    import uuid
    import struct

    reader = await request.multipart()
    file_field = None
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            file_field = field
            break
        field = await reader.next()

    if file_field is None:
        return web.json_response({"error": "未找到音频文件"}, status=400)

    audio_bytes = b""
    while True:
        chunk = await file_field.read_chunk(65536)
        if not chunk:
            break
        audio_bytes += chunk

    if not audio_bytes or len(audio_bytes) < 44:
        return web.json_response({"error": "音频文件无效"}, status=400)

    # Parse WAV header for duration (16kHz, 16-bit, mono expected)
    duration_ms = 0
    try:
        riff, size, wave = struct.unpack_from("<4sI4s", audio_bytes, 0)
        if riff == b"RIFF" and wave == b"WAVE":
            # Read fmt chunk for sample rate and channels
            fmt_offset = 12
            while fmt_offset < min(size + 8, 256):
                chunk_id, chunk_size = struct.unpack_from("<4sI", audio_bytes, fmt_offset)
                if chunk_id == b"fmt ":
                    audio_fmt, channels, sample_rate = struct.unpack_from(
                        "<HHI", audio_bytes, fmt_offset + 8
                    )
                    byte_rate, block_align, bits = struct.unpack_from(
                        "<IHH", audio_bytes, fmt_offset + 16
                    )
                    data_size = size - (fmt_offset + 8 + chunk_size) + 8  # rough
                    if byte_rate > 0:
                        duration_ms = int((data_size / byte_rate) * 1000)
                    break
                fmt_offset += 8 + chunk_size
    except Exception:
        pass  # Use default duration_ms=0 if header parse fails

    file_id = f"rec_{uuid.uuid4().hex[:12]}"
    _VOICE_REC_DIR.mkdir(parents=True, exist_ok=True)
    dest = _VOICE_REC_DIR / f"{file_id}.wav"
    dest.write_bytes(audio_bytes)

    return web.json_response({
        "url": f"/voice/{file_id}.wav",
        "duration_ms": duration_ms,
        "size": len(audio_bytes),
    })


async def handle_voice_serve(request: web.Request) -> web.Response:
    """GET /voice/{id}.wav — serve cached voice audio."""
    file_id = request.match_info["id"]
    path = _voice_find(file_id)
    if not path:
        return web.json_response({"error": "voice file not found"}, status=404)
    return web.FileResponse(path=path)
