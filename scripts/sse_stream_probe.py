#!/usr/bin/env python
"""SSE streaming probe for the Book Companion chat endpoint.

Why this exists
---------------
"Streaming is broken" has two very different causes that look identical from
the browser:

1. **The code never streams** — the think-parser waits for the closing
   ``</think>`` and holds the whole reasoning block, so the reader stares at
   nothing for the entire reasoning phase and then gets one blob.
2. **The process is stale** — the HTTP layer (``api_server.py`` + ``api/``)
   was rewritten on disk while an older process kept serving the old module
   objects.  Every endpoint still answers, so the bug that was already fixed
   looks alive.

This probe separates them.  It talks raw TCP (bypassing any HTTP-client
buffering), timestamps each ``recv()``, counts the SSE frames that actually
arrive, and cross-checks ``/api/health``'s code fingerprint against the file
on disk.

Usage::

    python scripts/sse_stream_probe.py                 # probe :8766
    python scripts/sse_stream_probe.py --port 8802
    python scripts/sse_stream_probe.py --message "解释熵"

Exit status: 0 = streaming healthy, 1 = not streaming, 2 = server unreachable.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# A live stream must deliver reasoning in many small frames.  One frame per
# phase is the signature of whole-block buffering.
MIN_THINK_EVENTS = 5


def _disk_fingerprint() -> dict:
    """Fingerprint the HTTP layer exactly the way the server reports it.

    Delegates to ``api_server._code_fingerprint`` so the probe and the
    server can never drift apart on scope (the routes live in ``api/``
    as well as ``api_server.py``).
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from api_server import _code_fingerprint
        return _code_fingerprint()
    except Exception as exc:  # pragma: no cover - diagnostics only
        return {"error": str(exc)}


def _health(host: str, port: int) -> dict:
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}/api/health", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc)}


def _probe(host: str, port: int, message: str, mode: str) -> dict:
    """POST to /api/chat over a raw socket and record arrival timing."""
    body = json.dumps({
        "message": message,
        "mode": mode,
        "stream": "v2",
        "book_id": "",
        "history": [],
    }).encode("utf-8")

    request = (
        f"POST /api/chat HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("utf-8")

    t0 = time.time()
    segments: list[tuple[float, int]] = []
    buf = b""
    try:
        sock = socket.create_connection((host, port), timeout=180)
    except OSError as exc:
        return {"reachable": False, "error": str(exc)}

    with sock:
        sock.sendall(request + body)
        while True:
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            segments.append((time.time() - t0, len(chunk)))
            buf += chunk

    text = buf.decode("utf-8", errors="replace")
    for sep in ("\r\n\r\n", "\n\n"):
        if sep in text:
            text = text.split(sep, 1)[1]
            break

    think = []      # (elapsed_at_parse, char_len)
    text_evts = []
    frames = 0
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        frames += 1
        try:
            ev = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "think":
            think.append(len(ev.get("content", "") or ""))
        elif ev.get("type") == "text":
            text_evts.append(len(ev.get("content", "") or ""))

    elapsed = time.time() - t0
    first_byte = segments[0][0] if segments else None
    return {
        "reachable": True,
        "elapsed": elapsed,
        "segments": len(segments),
        "segments_timeline": segments,
        "frames": frames,
        "think_events": len(think),
        "think_chars": sum(think),
        "think_max_frame": max(think) if think else 0,
        "text_events": len(text_evts),
        "text_chars": sum(text_evts),
        "first_byte_at": first_byte,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--message", default="用一句话解释什么是熵")
    ap.add_argument("--mode", default="immersive")
    args = ap.parse_args()

    print(f"── health  http://{args.host}:{args.port}/api/health")
    health = _health(args.host, args.port)
    disk = _disk_fingerprint()

    if "error" in health:
        print(f"   UNREACHABLE: {health['error']}")
        print("   → server is not running (nothing else can be measured)")
        return 2

    print(f"   pid={health.get('pid')} started={health.get('started')}")
    print(f"   serving code sha={health.get('api_server_sha256')} "
          f"mtime={health.get('api_server_mtime')}")
    print(f"   on-disk  code sha={disk.get('api_server_sha256')} "
          f"mtime={disk.get('api_server_mtime')}")

    served_sha = health.get("api_server_sha256")
    if not served_sha:
        # /api/health reports no fingerprint at all → the running build
        # predates the fingerprint, so it is definitely not current.
        stale = True
    else:
        stale = bool(disk.get("api_server_sha256")) and \
            served_sha != disk["api_server_sha256"]

    print(f"\n── streaming probe  ({args.message[:24]}…)")
    r = _probe(args.host, args.port, args.message, args.mode)
    if not r.get("reachable"):
        print(f"   UNREACHABLE: {r.get('error')}")
        return 2

    print(f"   tcp segments = {r['segments']}   (each ≈ one server write)")
    for i, (el, n) in enumerate(r["segments_timeline"][:6]):
        print(f"     recv#{i + 1}  t={el:6.2f}s  {n} bytes")
    if r["segments"] > 6:
        el, n = r["segments_timeline"][-1]
        print(f"     … last     t={el:6.2f}s  {n} bytes")
    print(f"   first byte at t={r['first_byte_at']:.2f}s"
          if r["first_byte_at"] is not None else "   first byte: n/a")
    print(f"   think frames = {r['think_events']} "
          f"(chars={r['think_chars']}, largest frame={r['think_max_frame']})")
    print(f"   text  frames = {r['text_events']} (chars={r['text_chars']})")
    print(f"   total        = {r['elapsed']:.2f}s")

    print("\n── verdict")
    ok = True
    if stale:
        ok = False
        print("   ✗ STALE PROCESS: the running server is not executing the "
              "code on disk.")
        print("     → restart the server; no code change can fix this.")
    if r["think_events"] < MIN_THINK_EVENTS and r["think_chars"] > 200:
        ok = False
        print(f"   ✗ NOT STREAMING: {r['think_chars']} reasoning chars "
              f"arrived in only {r['think_events']} frame(s).")
        print("     → the parser is buffering the whole <think> block "
              "instead of emitting per token.")
    if r["segments"] <= 2:
        ok = False
        print("   ✗ NOT STREAMING: only %d TCP segment(s) received." %
              r["segments"])

    if ok:
        print(f"   ✓ Streaming healthy — reasoning arrives in "
              f"{r['think_events']} incremental frames, first byte "
              f"{r['first_byte_at']:.2f}s.")
        if r["think_max_frame"] and r["think_events"]:
            print(f"     avg frame = "
                  f"{r['think_chars'] / r['think_events']:.1f} chars")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
