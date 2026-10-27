"""SSE v2 wire helpers shared by the chat and writing streams."""
from __future__ import annotations

import json

from aiohttp import web


async def _sse_write_json(resp: web.StreamResponse, event: dict) -> None:
    """Write a JSON-structured SSE v2 event line."""
    await resp.write(
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
    )


# ── SSE v2 helpers ──────────────────────────────────────────────────────────


def _partial_tag_suffix(buf: str, tag: str) -> int:
    """Length of the longest suffix of *buf* that is a proper prefix of *tag*.

    ``<think>`` / ``</think>`` can be split across chunk boundaries, so we
    hold back at most ``len(tag) - 1`` trailing chars that might still grow
    into the real tag.  Everything before that is safe to stream out now.
    """
    for n in range(min(len(buf), len(tag) - 1), 0, -1):
        if buf.endswith(tag[:n]):
            return n
    return 0


class _ThinkParser:
    """State machine that splits LLM text chunks into structured SSE v2 events.

    runtime.py wraps DeepSeek reasoning in ``<think>...</think>`` tags.
    This parser consumes chunks and emits typed events:
      - ``{"type":"think_start"}``
      - ``{"type":"think","content":"..."}``
      - ``{"type":"think_end"}``
      - ``{"type":"text","content":"..."}``

    Streaming contract (enforced structurally, not by convention)
    ------------------------------------------------------------
    The whole point of this class is that reasoning must reach the browser
    *while the model is still thinking*.  A parser that waits for the
    closing ``</think>`` before emitting turns a 20-second reasoning phase
    into 20 seconds of silence followed by one giant blob — which is
    indistinguishable from "no streaming at all" to the reader.

    So the invariant here is: ``feed()`` emits every byte it can prove is
    not part of a tag, in the same call.  Only the trailing chars that
    could still grow into ``<think>``/``</think>`` are retained, and
    ``_pending`` is therefore bounded by ``len(tag) - 1`` (6 / 7 chars).
    There is no unbounded accumulation anywhere in this class, so the old
    "buffer the entire reasoning block" bug cannot be reintroduced by a
    future edit that forgets the contract.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self):
        self._state = "normal"       # "normal" | "in_think"
        # Only ever holds ≤ len(tag)-1 chars (a possible partial tag).
        self._pending = ""

    def feed(self, chunk: str) -> list[dict]:
        """Feed a text chunk; return SSE v2 events emitted *now*."""
        events: list[dict] = []
        data = self._pending + chunk
        self._pending = ""

        while data:
            if self._state == "normal":
                idx = data.find(self._OPEN)
                if idx == -1:
                    # No opening tag in flight: release everything except a
                    # possible partial "<think>" tail.  Holding back more
                    # than that would stall visible text for no reason.
                    hold = _partial_tag_suffix(data, self._OPEN)
                    emit = data[: len(data) - hold]
                    if emit:
                        events.append({"type": "text", "content": emit})
                    data = data[len(data) - hold:]
                    if data:                       # partial tag → carry over
                        self._pending = data
                    break
                # Text preceding the tag is visible output.
                if idx > 0:
                    events.append({"type": "text", "content": data[:idx]})
                data = data[idx + len(self._OPEN):]
                self._state = "in_think"
                events.append({"type": "think_start"})

            else:  # in_think
                idx = data.find(self._CLOSE)
                if idx == -1:
                    # Closing tag not here yet — stream the reasoning out
                    # immediately and keep only a possible partial tail.
                    hold = _partial_tag_suffix(data, self._CLOSE)
                    emit = data[: len(data) - hold]
                    if emit:
                        events.append({"type": "think", "content": emit})
                    data = data[len(data) - hold:]
                    if data:
                        self._pending = data
                    break
                if idx > 0:
                    events.append({"type": "think", "content": data[:idx]})
                data = data[idx + len(self._CLOSE):]
                self._state = "normal"
                events.append({"type": "think_end"})

        return events

    def flush(self) -> list[dict]:
        """Flush the held tail (called when the underlying stream ends)."""
        events: list[dict] = []
        if self._pending:
            if self._state == "in_think":
                # Unclosed think — surface the reasoning, then close the row
                # so the frontend label never sticks on "思考中…".
                events.append({"type": "think", "content": self._pending})
                events.append({"type": "think_end"})
            else:
                events.append({"type": "text", "content": self._pending})
            self._pending = ""
        self._state = "normal"
        return events

    @property
    def pending_chars(self) -> int:
        """Diagnostics: how many chars are held back awaiting a tag match."""
        return len(self._pending)
