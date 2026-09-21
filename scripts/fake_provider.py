"""Fake OpenAI-compatible provider — offline LLM/embedding backend for testbeds.

Layer-1 test infra: bind a slot (console 连接档案) to http://127.0.0.1:8890/v1
and every LLM consumer — chat ReAct, BookGraph extraction, memory pipeline,
import enrichment — runs offline, deterministic, zero-token. Wire format
mirrors exactly what runtime.OpenAIClient and the bookgraph callers parse:
SSE ``data:`` lines, tool_calls deltas accumulated by index, usage-only
terminal chunk, ``[DONE]`` sentinel.

Run: python scripts/fake_provider.py [--port 8890] [--model fake-model] [--dim 64]

Control plane (JSON under /_mock/):
  POST   /_mock/fault     {"mode": http_500|empty_stream|garbage|truncate|
                           truncated_json|delay, "times": N, "param": seconds}
  DELETE /_mock/fault     clear armed fault
  POST   /_mock/scenario  {"rules": [{"contains": str, "model": str?,
                            "response": {"text"?, "reasoning"?,
                             "tool_calls": [{"name": str, "arguments": obj}]}}]}
  DELETE /_mock/scenario  clear rules
  GET    /_mock/requests  ring buffer of received requests (assert retries)
  POST   /_mock/reset     clear faults + rules + request log
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass, field

from aiohttp import web

MODEL_NAME = "fake-model"
EMB_DIM = 64
REQ_LOG_CAP = 100


@dataclass
class Fault:
    mode: str
    times: int = 1          # how many incoming requests it applies to
    param: float = 0.0      # delay seconds for mode=delay


@dataclass
class MockState:
    fault: Fault | None = None
    rules: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)

    def log_request(self, path: str, body: dict, fault: str, matched: bool) -> None:
        msgs = body.get("messages") or []
        self.requests.append({
            "ts": time.strftime("%H:%M:%S"),
            "path": path,
            "model": body.get("model", ""),
            "stream": bool(body.get("stream")),
            "n_messages": len(msgs),
            "has_tools": bool(body.get("tools")),
            "fault": fault,
            "scenario_matched": matched,
        })
        del self.requests[:-REQ_LOG_CAP]


STATE = MockState()


# ── Fault injection + deterministic embeddings ────────────────────────────

def _take_fault() -> Fault | None:
    """Pop the armed fault if one exists (decrement its request counter)."""
    f = STATE.fault
    if f is None:
        return None
    f.times -= 1
    if f.times <= 0:
        STATE.fault = None
    return f


def _fake_vector(text: str, dim: int) -> list[float]:
    """Deterministic pseudo-embedding: sha256-seeded, L2-normalized.

    Same text -> same vector, different text -> different vector, so
    similarity ordering in recall tests stays meaningful.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vec: list[float] = []
    while len(vec) < dim:
        digest = hashlib.sha256(digest).digest()
        vec.extend(b / 255.0 - 0.5 for b in digest)
    vec = vec[:dim]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


# ── Response builders ─────────────────────────────────────────────────────

def _last_user_text(body: dict) -> str:
    msgs = body.get("messages") or []
    last = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
    content = last.get("content", "")
    if isinstance(content, list):  # multimodal (ASR audio parts etc.)
        content = " ".join(
            p.get("text") or f"[{p.get('type', 'part')}]" if isinstance(p, dict)
            else str(p) for p in content)
    return str(content)


def _default_reply(body: dict) -> str:
    """Deterministic echo reply — model name, message count, last-msg tail."""
    n = len(body.get("messages") or [])
    tail = _last_user_text(body).replace("\n", " ")[-60:]
    return f"【fake】model={body.get('model', MODEL_NAME)} messages={n} last=…{tail}"


def _usage(body: dict, out_text: str) -> dict:
    n_in = sum(len(str(m.get("content", ""))) for m in body.get("messages") or []) // 4
    n_out = max(1, len(out_text) // 4)
    return {"prompt_tokens": n_in, "completion_tokens": n_out,
            "total_tokens": n_in + n_out}


def _tool_delta(i: int, c: dict) -> dict:
    return {"index": i, "id": f"call-fake-{i}", "type": "function",
            "function": {"name": c["name"],
                         "arguments": json.dumps(c.get("arguments", {}),
                                                 ensure_ascii=False)}}


def _match_rule(body: dict) -> dict | None:
    """First scenario rule matching the last user message (and model)."""
    text = _last_user_text(body)
    for rule in STATE.rules:
        if rule.get("contains") and rule["contains"] not in text:
            continue
        if rule.get("model") and rule["model"] != body.get("model"):
            continue
        return rule
    return None


def _nonstream(body: dict, reply: str, tool_calls: list[dict],
               reasoning: str) -> dict:
    msg: dict = {"role": "assistant", "content": reply}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = [_tool_delta(i, c) for i, c in enumerate(tool_calls)]
    return {"id": "chatcmpl-fake", "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", MODEL_NAME),
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": _usage(body, reply)}


def _prepare_sse(request: web.Request) -> web.StreamResponse:
    return web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache"})


async def _sse(resp: web.StreamResponse, delta: dict, *, finish: str | None = None,
               usage: dict | None = None) -> None:
    chunk: dict = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": MODEL_NAME,
                   "choices": [{"index": 0, "delta": delta,
                                "finish_reason": finish}]}
    if usage is not None:  # usage-only terminal chunk (include_usage)
        chunk["choices"] = []
        chunk["usage"] = usage
    await resp.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())


async def _stream_reply(request: web.Request, body: dict, reply: str,
                        tool_calls: list[dict], reasoning: str,
                        include_usage: bool, mode: str) -> web.StreamResponse:
    """Full streaming path, including the stream-shaped fault modes."""
    if mode == "empty_stream":      # 200 + immediate [DONE]: zero content
        resp = _prepare_sse(request)
        await resp.prepare(request)
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp
    if mode == "garbage":           # 200 + non-SSE junk lines
        resp = _prepare_sse(request)
        await resp.prepare(request)
        await resp.write(b"this is not sse data\n\nrandom line\n\n")
        await resp.write_eof()
        return resp
    if mode == "truncated_json":    # content holds unterminated JSON
        reply = '{"nodes": ["地下室人"-'  # simulates max_tokens cut
    resp = _prepare_sse(request)
    await resp.prepare(request)
    await _sse(resp, {"role": "assistant", "content": ""})
    if reasoning:  # reasoning must precede content: parser wraps it in <think>
        await _sse(resp, {"reasoning_content": reasoning})
    sent = 0
    for i in range(0, len(reply), 12):
        await _sse(resp, {"content": reply[i:i + 12]})
        sent += 1
        if mode == "truncate" and sent >= 2:   # connection dies mid-stream
            request.transport.close()
            return resp
    for i, c in enumerate(tool_calls):
        await _sse(resp, {"tool_calls": [_tool_delta(i, c)]})
    await _sse(resp, {}, finish="tool_calls" if tool_calls else "stop")
    if include_usage:
        await _sse(resp, {}, usage=_usage(body, reply))
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


async def handle_chat(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "bad json"}}, status=400)
    fault = _take_fault()
    mode = fault.mode if fault else ""
    if mode == "delay":                      # slow, then proceed normally
        await asyncio.sleep(fault.param)
        mode = ""
    rule = _match_rule(body)
    spec = (rule or {}).get("response", {})
    reply = spec.get("text", _default_reply(body))
    tool_calls = spec.get("tool_calls") or []
    reasoning = spec.get("reasoning", "")
    STATE.log_request(request.path, body, mode, matched=rule is not None)
    if mode == "http_500":
        return web.json_response(
            {"error": {"message": "fake-provider 500 (mock_fault)",
                       "type": "mock_fault"}}, status=500)
    if body.get("stream"):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        return await _stream_reply(request, body, reply, tool_calls,
                                   reasoning, include_usage, mode)
    # ── non-stream ──
    if mode == "empty_stream":
        return web.json_response(_nonstream(body, "", [], ""))
    if mode == "garbage":
        return web.Response(text="<<garbage not json>>")
    if mode == "truncated_json":
        return web.json_response(_nonstream(body, '{"nodes": ["地下室人"-', [], ""))
    if mode == "truncate":                   # partial body, connection dies
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(b'{"choices": [{"message": {"content": "partial')
        request.transport.close()
        return resp
    return web.json_response(_nonstream(body, reply, tool_calls, reasoning))


async def handle_embeddings(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "bad json"}}, status=400)
    inputs = body.get("input") or []
    if isinstance(inputs, str):
        inputs = [inputs]
    model = body.get("model", "")
    data = [{"object": "embedding", "index": i,
             "embedding": _fake_vector(f"{model}:{t}", EMB_DIM)}
            for i, t in enumerate(inputs)]
    tokens = sum(max(1, len(str(t)) // 4) for t in inputs)
    return web.json_response({
        "object": "list", "data": data, "model": model,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens}})


async def handle_models(request: web.Request) -> web.Response:
    """Serves the console 测试 button (tts kind probes GET /models) too."""
    return web.json_response({"object": "list", "data": [
        {"id": MODEL_NAME, "object": "model"},
        {"id": "fake-embedding", "object": "model"},
        {"id": "fake-tts", "object": "model"}]})


# ── Control plane (/_mock/*) ──────────────────────────────────────────────

async def handle_fault_set(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    mode = str(body.get("mode", "")).strip()
    valid = {"http_500", "empty_stream", "garbage", "truncate",
             "truncated_json", "delay"}
    if mode not in valid:
        return web.json_response(
            {"ok": False, "error": f"mode must be one of {sorted(valid)}"},
            status=400)
    STATE.fault = Fault(mode=mode, times=int(body.get("times", 1)),
                        param=float(body.get("param", 0)))
    return web.json_response({"ok": True, "armed": vars(STATE.fault)})


async def handle_fault_clear(request: web.Request) -> web.Response:
    STATE.fault = None
    return web.json_response({"ok": True})


async def handle_scenario_set(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    STATE.rules = body.get("rules") or []
    return web.json_response({"ok": True, "n_rules": len(STATE.rules)})


async def handle_scenario_clear(request: web.Request) -> web.Response:
    STATE.rules = []
    return web.json_response({"ok": True})


async def handle_requests(request: web.Request) -> web.Response:
    return web.json_response({"requests": list(reversed(STATE.requests))})


async def handle_reset(request: web.Request) -> web.Response:
    STATE.fault, STATE.rules, STATE.requests = None, [], []
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application()
    r = app.router
    for pre in ("", "/v1"):     # base_url 绑定带不带 /v1 都能命中
        r.add_post(f"{pre}/chat/completions", handle_chat)
        r.add_post(f"{pre}/embeddings", handle_embeddings)
        r.add_get(f"{pre}/models", handle_models)
    r.add_post("/_mock/fault", handle_fault_set)
    r.add_delete("/_mock/fault", handle_fault_clear)
    r.add_post("/_mock/scenario", handle_scenario_set)
    r.add_delete("/_mock/scenario", handle_scenario_clear)
    r.add_get("/_mock/requests", handle_requests)
    r.add_post("/_mock/reset", handle_reset)
    r.add_get("/", lambda req: web.json_response(
        {"service": "fake-provider", "model": MODEL_NAME, "dim": EMB_DIM,
         "hint": "bind base_url http://127.0.0.1:8890/v1 (or set slots in 靶场 console)"}))
    return app


def main() -> None:
    global MODEL_NAME, EMB_DIM
    ap = argparse.ArgumentParser(description="Fake OpenAI-compatible provider")
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--model", default=MODEL_NAME)
    ap.add_argument("--dim", type=int, default=EMB_DIM)
    args = ap.parse_args()
    MODEL_NAME, EMB_DIM = args.model, args.dim
    print(f"[fake-provider] http://127.0.0.1:{args.port}/v1 "
          f"model={MODEL_NAME} dim={EMB_DIM}", flush=True)
    web.run_app(build_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

