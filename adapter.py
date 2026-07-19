"""Self-contained OpenAI-compatible adapter for the aws-build plugin.

WHY THIS EXISTS
----------------
Hermes routes chat turns through providers declared in ``config.yaml`` with a
known ``transport`` (e.g. ``openai_chat``). Plugins CANNOT register an LLM
backend directly — core only reads ``providers:`` from config. Amazon Q's API is
NOT OpenAI-compatible (different auth, endpoint, request body, and stream
shape). So to make aws-build a *selectable chat model* in the Hermes TUI/CLI
(Way A), we expose a tiny local HTTP endpoint that speaks OpenAI's
``/v1/chat/completions`` wire format on one side and calls Q (via
``backend.chat()``) on the other.

This is intentionally NOT the old ``:8088`` bridge daemon:
  * it lives inside the plugin (stdlib only, no separate binary),
  * the plugin launches it on ``register()`` (background thread, dies with the
    Hermes session — no orphaned process to forget about),
  * ``config.yaml`` points a ``providers: aws-build`` entry at this listener, so
    there is no dead/roted pointer.

REQUEST (OpenAI shape, received from Hermes)
------------------------------------------------
    POST /v1/chat/completions
    {"model": "claude-sonnet-4.5", "messages": [...], "stream": true}

RESPONSE (OpenAI SSE, streamed back to Hermes)
------------------------------------------------
    data: {"choices":[{"delta":{"role":"assistant","content":"..."},"index":0}]}
    ...
    data: [DONE]

Q is single-prompt, so ``messages`` are flattened into one prompt (the last
user turn). Multi-turn context across *Hermes* turns is not threaded to Q
here — Q is used as a stateless chat endpoint behind Hermes's own loop.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

# backend.chat() is the single source of truth for Q's wire format + token.
try:
    from . import backend  # type: ignore  # package import
except ImportError:  # __main__ / direct execution
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backend  # type: ignore

DEFAULT_PORT = int(os.environ.get("AWS_BUILD_ADAPTER_PORT", "8077"))
HOST = os.environ.get("AWS_BUILD_ADAPTER_HOST", "127.0.0.1")

_server: Optional["ThreadingHTTPServer"] = None
_thread: Optional[threading.Thread] = None


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Collapse OpenAI ``messages`` into a single prompt for Q.

    Q takes one ``userInputMessage`` per call. We join consecutive turns with
    newlines and prefer the last user message if present; falls back to the last
    content block. System prompts are prepended as a leading instruction.
    """
    if not messages:
        return ""
    system_bits: list[str] = []
    convo: list[str] = []
    for m in messages:
        role = (m.get("role") or "").lower()
        content = m.get("content") or ""
        if isinstance(content, list):  # multimodal content blocks
            content = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        if role == "system":
            if content:
                system_bits.append(content)
        else:
            convo.append(content)
    parts = []
    if system_bits:
        parts.append("System: " + "\n".join(system_bits))
    parts.extend([c for c in convo if c])
    return "\n\n".join(parts)


def _sse(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


def _handle_chat(body: dict[str, Any]) -> bytes:
    """Run one chat completion and return OpenAI SSE bytes.

    Returns a bytes blob (newline-joined SSE frames) so the HTTP handler can
    write it in one shot. Kept synchronous + simple; Q already streams
    internally but Hermes only needs the assembled answer framed as OpenAI SSE.
    """
    model = body.get("model") or "auto"
    messages = body.get("messages") or []
    prompt = _flatten_messages(messages)

    try:
        answer, _cid, _tuid = backend.chat(prompt, model=str(model))
    except Exception as exc:  # noqa: BLE001 - surface as OpenAI-style error frame
        err = b"data: " + json.dumps(
            {"error": {"message": str(exc), "type": "aws_build_error"}}
        ).encode("utf-8") + b"\n\n"
        # Still terminate the SSE stream so Hermes's parser sees [DONE]
        # and doesn't hang waiting for the stream to close.
        return err + b"data: [DONE]\n\n"

    frames = [
        b"data: "
        + _sse({"choices": [{"delta": {"role": "assistant"}, "index": 0}]}),
        b"data: "
        + _sse(
            {"choices": [{"delta": {"content": answer}, "index": 0}]}
        ),
        b"data: [DONE]\n\n",
    ]
    return b"".join(frames)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # silence default stderr logging
        pass

    def _send(self, status: int, data: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # health check
        if self.path.rstrip("/") in ("/healthz", "/health", ""):
            self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, b'{"error":"not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            self._send(400, json.dumps({"error": f"bad request: {exc}"}).encode())
            return
        try:
            out = _handle_chat(body)
        except Exception as exc:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(exc)}).encode())
            return
        self._send(200, out, ctype="text/event-stream")


def start(host: str = HOST, port: int = DEFAULT_PORT) -> tuple[ThreadingHTTPServer, int]:
    """Launch the adapter in a daemon background thread.

    Returns (server, actual_port). Idempotent: calling twice returns the
    already-running server. Safe to call from ``register()``.
    """
    global _server, _thread
    if _server is not None:
        return _server, _server.server_address[1]  # type: ignore[union-attr]

    srv = ThreadingHTTPServer((host, port), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _server, _thread = srv, t
    return srv, port


def stop() -> None:
    """Stop the adapter (tests / cleanup). No-op if not running."""
    global _server, _thread
    if _server is not None:
        _server.shutdown()
        _server.server_close()
    _server, _thread = None, None


def is_running() -> bool:
    return _server is not None


if __name__ == "__main__":
    srv, p = start()
    print(f"aws-build adapter listening on http://{HOST}:{p}/v1/chat/completions")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop()
