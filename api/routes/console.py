"""Console endpoints: providers, slots, routes, usage analytics, context meter."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import aiohttp
from aiohttp import web

import usage_log

from api.prompt_builders import _build_reading_prompt


# ── Console (provider/slot management) ─────────────────────────────────

from provider_store import (  # noqa: E402 (lightweight, no cycles)
    ProviderInUseError,
    SLOTS,
    guess_model_kind,
)


def _console_store(request: web.Request):
    """App's ProviderStore; lazily built in degraded runs that have a config.

    Falls back to the process-wide instance so the console sees exactly
    what every LLM call point resolves (same file, same cache).
    """
    store = request.app.get("provider_store")
    if store is not None:
        return store
    config = request.app.get("config")
    if config is None:
        raise web.HTTPServiceUnavailable(
            text="console unavailable: no config in this run mode")
    from provider_store import get_active_store
    store = get_active_store(config)
    request.app["provider_store"] = store
    return store


def _console_apply(store, request: web.Request) -> dict:
    """Hot-apply console bindings to the shared Config; no-op without config."""
    config = request.app.get("config")
    return store.apply_to_config(config) if config is not None else {}


async def handle_console_config(request: web.Request) -> web.Response:
    """GET /api/console/config — providers (keys masked) + slots + effective resolution."""
    store = _console_store(request)
    return web.json_response(store.snapshot(request.app["config"]))


async def handle_provider_save(request: web.Request) -> web.Response:
    """POST /api/console/providers — create/edit. api_key empty on edit = keep old."""
    store = _console_store(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    try:
        entry = store.upsert_provider(body)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    # The edited provider may be bound to a slot — re-apply so key/base edits hot-switch
    applied = _console_apply(store, request)
    masked = dict(entry)
    masked["api_key"] = store.mask_key(store.reveal_key(entry["id"]))
    return web.json_response({"ok": True, "provider": masked, "applied": applied})


async def handle_provider_delete(request: web.Request) -> web.Response:
    """DELETE /api/console/providers/{id} — refused (409) while a slot binds it."""
    store = _console_store(request)
    try:
        store.delete_provider(request.match_info["provider_id"])
    except ProviderInUseError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    return web.json_response({"ok": True})


async def handle_slot_switch(request: web.Request) -> web.Response:
    """PUT /api/console/slots/{slot} — bind provider+model (provider_id="" unbinds).

    Hot-applies to the shared Config instance; the next request from any
    consumer (OpenAIClient/bookgraph/tts) picks it up. Streaming responses
    already in flight are unaffected.
    """
    store = _console_store(request)
    slot = request.match_info["slot"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    try:
        entry = store.set_slot(
            slot,
            provider_id=body["provider_id"] if "provider_id" in body else None,
            model=body.get("model"),
            params=body.get("params"),
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    applied = _console_apply(store, request)
    return web.json_response({"ok": True, "slot": entry, "applied": applied})


async def handle_route_switch(request: web.Request) -> web.Response:
    """PUT /api/console/routes/{scenario} — 绑定某个调用点到 (连接, 模型)。

    ``provider_id=""`` 解绑，回退到父级键、再到主模型。与 slot 不同，
    route 是**按调用点**分流：下一次该调用点发起时生效，在途请求不受影响。
    """
    store = _console_store(request)
    scenario = request.match_info["scenario"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    try:
        entry = store.set_route(
            scenario,
            provider_id=body["provider_id"] if "provider_id" in body else None,
            model=body.get("model"),
            params=body.get("params"),
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    applied = _console_apply(store, request)
    return web.json_response({"ok": True, "route": entry, "applied": applied})


_FRIENDLY_HTTP_ERRORS = {
    401: "认证失败：密钥无效或过期",
    403: "禁止访问：密钥无此端点权限",
    404: "端点不存在：检查 base_url 是否需要以 /v1 结尾",
}


def _console_test_detail(kind: str, text: str) -> str:
    """One-line human detail from a successful probe response body."""
    try:
        data = json.loads(text)
    except Exception:
        return ""
    if kind == "embedding":
        items = data.get("data") or []
        dims = len(items[0].get("embedding", [])) if items else 0
        return f"embedding dims={dims}"
    if kind == "tts":
        return f"models={len(data.get('data') or [])}"
    return "chat ok"


async def handle_provider_test(request: web.Request) -> web.Response:
    """POST /api/console/providers/{id}/test — probe connectivity (10s timeout).

    llm: 1-token chat completion; embedding: real single embed; tts: GET /models
    (synthesis is heavy — auth + reachability is what a key test needs).
    Model precedence: request body > slot binding > provider's cached list.
    """
    store = _console_store(request)
    provider = store.get_provider(request.match_info["provider_id"])
    if provider is None:
        return web.json_response({"error": "provider not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Model-first: the probe targets one model; kind derives from it
    # (body > any slot bound to this provider > first cached model).
    slot_model = ""
    for s in SLOTS:
        slot = store.get_slot(s)
        if slot.get("provider_id") == provider["id"] and slot.get("model"):
            slot_model = slot["model"]
            break
    model = str(body.get("model") or slot_model
                or ((provider.get("models") or [{}])[0].get("id") or ""))
    kind = (str(body.get("kind") or "").strip()
            or store.model_kind(provider["id"], model)
            or guess_model_kind(model))

    base = provider["base_url"].rstrip("/")
    headers = {
        "Authorization": f"Bearer {store.reveal_key(provider['id'])}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=10)
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            if kind == "llm":
                payload = {"model": model,
                           "messages": [{"role": "user", "content": "ping"}],
                           "max_tokens": 1, "stream": False}
                async with sess.post(f"{base}/chat/completions", json=payload,
                                     headers=headers) as resp:
                    status, text = resp.status, await resp.text()
            elif kind == "embedding":
                payload = {"model": model, "input": ["ping"]}
                async with sess.post(f"{base}/embeddings", json=payload,
                                     headers=headers) as resp:
                    status, text = resp.status, await resp.text()
            else:  # tts
                async with sess.get(f"{base}/models", headers=headers) as resp:
                    status, text = resp.status, await resp.text()
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "timeout after 10s"})
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    latency_ms = round((time.monotonic() - t0) * 1000)
    if status in (200, 201):
        return web.json_response({"ok": True, "status": status,
                                  "latency_ms": latency_ms,
                                  "detail": _console_test_detail(kind, text)})
    friendly = _FRIENDLY_HTTP_ERRORS.get(status)
    out: dict = {"ok": False, "status": status, "latency_ms": latency_ms,
                 "error": friendly or f"HTTP {status}"}
    if not friendly:
        out["detail"] = text[:200]
    return web.json_response(out)


async def handle_models_fetch(request: web.Request) -> web.Response:
    """POST /api/console/models/fetch — pull GET {base}/models for the form.

    Body: {base_url, api_key?, provider_id?}. Empty key with a provider_id
    resolves the stored key (edit mode). Returns kind-guessed model rows;
    the console lets the user correct kinds before saving. No fool-proofing:
    vendor errors surface verbatim.
    """
    store = _console_store(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    base = str(body.get("base_url") or "").strip().rstrip("/")
    key = str(body.get("api_key") or "").strip()
    pid = str(body.get("provider_id") or "").strip()
    if not key and pid:
        key = store.reveal_key(pid)
    if not base or not key:
        return web.json_response(
            {"error": "base_url 与 api_key 均为空（编辑模式可只填 base_url）"},
            status=400)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(f"{base}/models", headers=headers) as resp:
                status, text = resp.status, await resp.text()
    except asyncio.TimeoutError:
        return web.json_response(
            {"ok": False, "error": "timeout after 10s"}, status=502)
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=502)
    if status not in (200, 201):
        return web.json_response(
            {"ok": False, "status": status,
             "error": _FRIENDLY_HTTP_ERRORS.get(status) or f"HTTP {status}"},
            status=502)
    try:
        data = json.loads(text)
        ids = [str(m.get("id") or "").strip()
               for m in (data.get("data") or []) if isinstance(m, dict)]
    except Exception:
        return web.json_response(
            {"ok": False, "error": "响应不是合法的 /models JSON"}, status=502)
    models = [{"id": mid, "kind": guess_model_kind(mid)} for mid in ids if mid]
    return web.json_response({"ok": True, "models": models})


# ── Context meter · 上下文水位回查 ───────────────────────────────────────


async def handle_context_usage(request: web.Request) -> web.Response:
    """GET /api/context/usage?session_id=&mode= — ring data on session load.

    Snapshot hit (runtime just finished THIS session) → exact snapshot.
    Otherwise rebuild: system/tools baseline from runtime + conversation
    from persisted messages (real tokenizer counts); totals real when the
    last assistant message carries usage, estimated otherwise.
    """
    runtime = request.app.get("runtime")
    config = request.app.get("config")
    session_service = request.app.get("session_service")
    sid = str(request.query.get("session_id", "")).strip()
    mode = str(request.query.get("mode", "immersive")).strip() or "immersive"
    alert_ratio = getattr(config, "context_alert_ratio", 0.35) if config else 0.35
    model = getattr(config, "model", "") if config else ""

    snap = getattr(runtime, "last_context_usage", None) if runtime else None
    if (snap is not None and sid
            and getattr(runtime, "last_context_usage_session", "") == sid):
        return web.json_response({"ok": True, **snap.to_dict(alert_ratio=alert_ratio)})

    import asyncio as _asyncio
    from content import TokenUsage as _TU
    from context_meter import measure, resolve_profile

    system_prompt = ""
    tools_schema: list[dict] = []
    if runtime is not None:
        try:
            system_prompt = _build_reading_prompt(runtime, mode, "")
            tools_schema = runtime.tool_registry.get_schemas()
        except Exception:
            pass

    history_messages: list[dict] = []
    real_usage = None
    if sid and session_service is not None:
        try:
            loaded = session_service.load_messages(sid)
        except Exception:
            loaded = None
        if loaded is not None:
            for _m in reversed(loaded.messages):
                if _m.usage and (_m.usage.input_tokens or _m.usage.output_tokens):
                    real_usage = _TU(
                        input_tokens=_m.usage.input_tokens,
                        output_tokens=_m.usage.output_tokens,
                    )
                    break
            conv = loaded.messages[-40:]  # cap: proportional counting only
            if runtime is not None:
                try:
                    history_messages = [runtime._msg_to_api_dict(m) for m in conv]
                except Exception:
                    history_messages = []
            if not history_messages:
                history_messages = [
                    {"role": m.role, "content": m.first_text() or ""}
                    for m in conv
                ]

    prof = resolve_profile(model, config)
    snapshot = await _asyncio.to_thread(
        measure,
        system_prompt=system_prompt,
        tools_schema=tools_schema,
        history_messages=history_messages,
        injected_context="",
        user_input="",
        usage=real_usage,
        window=prof["window"],
        model=model,
        tokenizer_path=prof["tokenizer_path"],
        session_id=sid,
    )
    return web.json_response({"ok": True, **snapshot.to_dict(alert_ratio=alert_ratio)})


# ── Console · 用量分析 handlers ──────────────────────────────────────────


def _usage_profiles(request: web.Request):
    """Shared ModelProfileStore for the usage handlers."""
    from model_profiles import get_profiles_store
    return get_profiles_store(request.app.get("config"))


async def handle_usage_profiles_get(request: web.Request) -> web.Response:
    """GET /api/console/usage/profiles — pricing + windows + tokenizer."""
    return web.json_response({"ok": True, "profiles": _usage_profiles(request).get_profiles()})


async def handle_usage_profiles_put(request: web.Request) -> web.Response:
    """PUT /api/console/usage/profiles — replace {models, tokenizer, vendors}."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    try:
        _usage_profiles(request).save_profiles(
            body.get("models") or {}, body.get("tokenizer") or {},
            body.get("vendors") or {})
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    # 路径可能刚被修好/文件刚下载完——清缓存让新配置立即生效，不必重启
    from tokenizer_service import clear_cache
    clear_cache()
    return web.json_response({"ok": True})


def _month_bounds(month: str) -> tuple[int, int] | None:
    """Local-timezone [start_ms, end_ms) for 'YYYY-MM'; None if invalid."""
    try:
        y, m = month.split("-")
        year, mon = int(y), int(m)
        if not 1 <= mon <= 12 or year < 2000:
            return None
    except (ValueError, AttributeError):
        return None
    start = datetime(year, mon, 1)
    end_year, end_mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    end = datetime(end_year, end_mon, 1)
    return (
        int(start.timestamp() * 1000),
        int(end.timestamp() * 1000),
    )


async def handle_usage_overview(request: web.Request) -> web.Response:
    """GET /api/console/usage/overview?month=YYYY-MM — 用量分析页一次取全:
    统计卡 totals + 每日×模型 days(堆叠柱)+ 按模型汇总 by_model(模型卡)。"""
    month = request.query.get("month") or datetime.now().strftime("%Y-%m")
    bounds = _month_bounds(month)
    if bounds is None:
        return web.json_response({"error": "month 需为 YYYY-MM"}, status=400)
    rows = usage_log.get_store().usage_daily_by_model(bounds[0], bounds[1])

    days: dict[str, list] = {}
    by_model: dict[str, dict] = {}
    for r in rows:
        days.setdefault(r["day"], []).append(r)
        m = by_model.setdefault(r["model"], {
            "model": r["model"], "calls": 0, "tokens_in": 0,
            "tokens_out": 0, "errors": 0})
        for k in ("calls", "tokens_in", "tokens_out", "errors"):
            m[k] += r[k]

    # totals 由 by_model 求和派生 → 与 days 恒等,统计卡与图表数字永远一致
    totals = {
        "calls": sum(m["calls"] for m in by_model.values()),
        "tokens_in": sum(m["tokens_in"] for m in by_model.values()),
        "tokens_out": sum(m["tokens_out"] for m in by_model.values()),
        "errors": sum(m["errors"] for m in by_model.values()),
    }
    return web.json_response({
        "ok": True,
        "month": month,
        "totals": totals,
        "days": [{"day": d, "models": days[d]} for d in sorted(days)],
        "by_model": sorted(by_model.values(),
                           key=lambda m: m["calls"], reverse=True),
    })
