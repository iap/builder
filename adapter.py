"""Self-contained OpenAI-compatible adapter for the aws-build plugin.
# SPDX-License-Identifier: MIT OR Apache-2.0

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
-----------------------------------------------
    POST /v1/chat/completions
    {"model": "claude-sonnet-4.5", "messages": [...], "tools": [...], "stream": true}

RESPONSE (OpenAI SSE, streamed back to Hermes)
-----------------------------------------------
    # chat-only turn:
    data: {"choices":[{"delta":{"role":"assistant","content":"..."},"index":0}]}
    ...
    data: [DONE]

    # tool-call turn (option b): Q emits <tool_call> XML, translated to
    # OpenAI tool_calls so Hermes's agentic loop (MCP / skills / native tools)
    # fires:
    data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_awsbuild_0",
            "type":"function","function":{"name":"fs_write","arguments":""}}]},"index":0}]}
    data: {"choices":[{"delta":{"tool_calls":[{"index":0,
            "function":{"arguments":"{\"path\":\"a.txt\"}"}}]},"index":0}]}
    data: {"choices":[{"delta":{},"index":0,"finish_reason":"tool_calls"}]}
    data: [DONE]

Q is single-prompt, so ``messages`` (+ the advertised ``tools`` as text) are
flattened into one prompt (the last user turn). Q's GenerateAssistantResponse
rejects a real ``tools`` field, so tool awareness is conveyed via an injected
<tool_call> convention (text), and any <tool_call> blocks in Q's answer are
parsed back into OpenAI tool_calls frames. Multi-turn context across *Hermes*
turns is not threaded to Q here — Q is used as a stateless chat endpoint behind
Hermes's own loop.
"""

from __future__ import annotations

import json
import os
import re
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

# The adapter is a LOCAL-ONLY bridge: it forwards requests to Amazon Q using the
# plugin's stored Builder ID token. It must never be reachable from the network.
# Bind loopback by default; refuse to publish on a non-loopback host unless the
# operator opts in explicitly via AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1.
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _resolve_bind_host(requested: str) -> str:
    if requested in _LOOPBACK:
        return requested
    if os.environ.get("AWS_BUILD_ADAPTER_ALLOW_PUBLIC") == "1":
        return requested
    raise RuntimeError(
        f"aws-build adapter refused to bind to non-loopback host {requested!r}. "
        "The adapter is a local-only token bridge and must not be network-exposed. "
        "Bind 127.0.0.1 (default) or set AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1 to override."
    )

_server: Optional["ThreadingHTTPServer"] = None
_thread: Optional[threading.Thread] = None


# Tool-call convention injected into Q's single prompt. Q's
# GenerateAssistantResponse rejects a real `tools` field (it is chat-only and
# cannot do native function calling), so to let aws-build drive Hermes's
# agentic loop (MCP / skills / native tools) as a *model*, we ask Q to emit
# Hermes-compatible <tool_call> XML blocks (the same shape Hermes's own
# tool-call system prompt uses) and translate them back into OpenAI
# `tool_calls` frames on the way out. See _parse_tool_calls.
_TOOL_CALL_INSTRUCTION = (
    "If you need to use a tool, emit ONE OR MORE blocks in exactly this format "
    "and nothing else in that turn:\n"
    '<tool_call>\n{"name": <tool-name>, "arguments": <args-object>}\n</tool_call>\n'
    "Use only the tool names you are given. Otherwise reply in plain text."
)


def _flatten_messages(messages: list[dict[str, Any]], tools: Optional[list] = None) -> str:
    """Collapse OpenAI ``messages`` into a single prompt for Q.

    Q takes one ``userInputMessage`` per call. We join consecutive turns with
    newlines and prefer the last user message if present; falls back to the last
    content block. System prompts are prepended as a leading instruction.

    When Hermes advertises ``tools`` (the model path), Q cannot receive a real
    ``tools`` field, so we inject the tool-call convention plus the tool names
    as text so Q can request a tool via the <tool_call> shim (see
    _parse_tool_calls). This is the wire-protocol-safe way to give Q tool
    awareness without the rejected field.
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
    if tools:
        names = []
        for t in tools:
            fn = (t or {}).get("function") or {}
            nm = fn.get("name")
            if nm:
                names.append(nm)
        if names:
            parts.append(
                "Available tools you may call: " + ", ".join(names) + ".\n"
                + _TOOL_CALL_INSTRUCTION
            )
    parts.extend([c for c in convo if c])
    return "\n\n".join(parts)


def _parse_tool_calls(answer: str) -> list[dict[str, Any]]:
    """Extract structured tool calls from Q's text answer.

    Supports Hermes's ``<tool_call>{"name":..,"arguments":..}</tool_call>`` XML
    (the convention injected by _flatten_messages) and a fenced
    ```json function-call block. Returns a list of
    ``{"name": str, "arguments": str-json}`` dicts (arguments is a JSON *string*
    ready for an OpenAI ``tool_calls[].function.arguments`` delta). Empty list
    when there is no tool-call intent — the caller then treats the turn as plain
    text. Best-effort: malformed blocks are skipped rather than crashing the
    stream.
    """
    calls: list[dict[str, Any]] = []

    # 1) <tool_call> ... </tool_call>
    for m in re.finditer(r"<tool_call>\s*", answer):
        start = m.end()
        obj, end = _extract_balanced_brace(answer, start)
        if obj is None:
            continue
        close = answer.find("</tool_call>", end)
        if close == -1:
            continue
        try:
            parsed = json.loads(obj)
        except Exception:
            continue
        name = parsed.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = parsed.get("arguments", {})
        if not isinstance(args, dict):
            args = {}
        calls.append({"name": name, "arguments": json.dumps(args, ensure_ascii=False)})

    # 2) fenced ```json blocks shaped like {"name":..,"arguments":..}
    if not calls:
        for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.DOTALL):
            try:
                obj = json.loads(m.group(1))
            except Exception:
                continue
            name = obj.get("name")
            if isinstance(name, str) and name:
                args = obj.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}
                calls.append(
                    {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}
                )
    return calls


def _extract_balanced_brace(text: str, start: int) -> tuple[Optional[str], int]:
    """Return (json_object_str, end_index) for the balanced `{...}` at `start`.

    Non-greedy `.*?` can't span nested braces (tool arguments are JSON objects),
    so we scan depth-aware. Returns (None, start) when `text[start]` isn't `{`.
    """
    if start >= len(text) or text[start] != "{":
        return None, start
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1], i + 1
        elif c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return None, start


def _strip_tool_call_xml(answer: str) -> str:
    """Remove <tool_call> blocks from an answer so any residual XML isn't shown
    to the user as assistant text when we also emit tool_calls."""
    out = answer
    while True:
        m = re.search(r"<tool_call>\s*", out)
        if not m:
            break
        obj, end = _extract_balanced_brace(out, m.end())
        close = out.find("</tool_call>", end if obj else m.end())
        if close == -1:
            break
        out = out[: m.start()] + out[close + len("</tool_call>"):]
    return out.strip()


def _sse(choices: list, model: str = "aws-build") -> bytes:
    # SSE / OpenAI streaming requires each event to be terminated by a BLANK
    # line, i.e. "\n\n" — not a single "\n". With only one newline, Hermes's
    # openai_chat parser reads two `data:` frames as a single chunk, strips the
    # first `data: `, json.loads() the first object, then hits the next `data:`
    # line and fails with "Extra data: line 2 column 1". The trailing [DONE]
    # frame was already correct; the per-event frames were not.
    # ensure_ascii=False keeps non-ASCII answers (café, —, CJK) verbatim so the
    # TUI renders them instead of as \uXXXX escapes (same contract as the tool
    # path's tool_result/tool_error helpers).
    # The OpenAI SDK's streaming parser requires the standard chunk envelope
    # (id/created/model/object) on every data: frame, so include them.
    import time as _time

    payload = {
        "id": "chatcmpl-awsbuild",
        "object": "chat.completion.chunk",
        "created": int(_time.time()),
        "model": model,
        "choices": choices,
    }
    return (json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _handle_chat(body: dict[str, Any]) -> bytes:
    """Run one chat completion and return OpenAI SSE bytes.

    Returns a bytes blob (newline-joined SSE frames) so the HTTP handler can
    write it in one shot. Kept synchronous + simple; Q already streams
    internally but Hermes only needs the assembled answer framed as OpenAI SSE.

    Tool-call translation (option b): when Q's answer contains <tool_call>
    blocks (the convention injected for the model path), we emit OpenAI
    ``tool_calls`` deltas with ``finish_reason: "tool_calls"`` so Hermes's
    agentic loop (MCP / skills / native tools) actually fires. Otherwise we
    emit the text as ``content`` with ``finish_reason: "stop"`` (chat-only).
    """
    model = body.get("model") or "auto"
    messages = body.get("messages") or []
    tools = body.get("tools") or None
    prompt = _flatten_messages(messages, tools=tools)

    try:
        answer, _cid, _tuid = backend.chat(prompt, model=str(model))
    except Exception as exc:  # noqa: BLE001 - surface as OpenAI-style error frame
        err = b"data: " + json.dumps(
            {"error": {"message": str(exc), "type": "aws_build_error"}}
        ).encode("utf-8") + b"\n\n"
        # Still terminate the SSE stream so Hermes's parser sees [DONE]
        # and doesn't hang waiting for the stream to close.
        return err + b"data: [DONE]\n\n"

    calls = _parse_tool_calls(answer) if tools else []
    if calls:
        return _tool_calls_frames(
            calls, text=_strip_tool_call_xml(answer), model=str(model)
        )

    frames = [
        b"data: "
        + _sse([{"delta": {"role": "assistant"}, "index": 0}], model=model),
        b"data: "
        + _sse(
            [{"delta": {"content": answer}, "index": 0}], model=model
        ),
        b"data: [DONE]\n\n",
    ]
    return b"".join(frames)


def _tool_calls_frames(calls: list[dict[str, Any]], text: str = "", model: str = "aws-build") -> bytes:
    """Emit OpenAI streaming `tool_calls` frames for parsed Q tool calls.

    Mirrors what a native function-calling model streams: a role frame, one
    tool_calls delta per call carrying id + type + function.name, then
    incremental function.arguments deltas, then a final delta with
    ``finish_reason: "tool_calls"`` and [DONE]. Hermes's openai_chat transport
    reassembles these into a normal assistant(tool_calls) message and dispatches
    the tools. Any surrounding prose is dropped from content (the tool calls are
    the action for this turn).
    """
    frames: list[bytes] = [
        b"data: "
        + _sse([{"delta": {"role": "assistant"}, "index": 0}], model=model),
    ]
    if text:
        frames.append(
            b"data: "
            + _sse([{"delta": {"content": text}, "index": 0}], model=model)
        )
    for i, call in enumerate(calls):
        call_id = f"call_awsbuild_{i}"
        # Emit the whole tool call in ONE delta (id + type + name + arguments).
        # The OpenAI SDK's incremental tool_calls merge is fragile when the
        # name and arguments are split across fragments (it overwrites the
        # function object and can drop the call), so a single complete delta
        # per call is the robust, SDK-valid shape.
        frames.append(
            b"data: "
            + _sse(
                [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": i,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": call["name"],
                                        "arguments": call["arguments"],
                                    },
                                }
                            ]
                        },
                        "index": 0,
                    }
                ],
                model=model,
            )
        )
    frames.append(
        b"data: "
        + _sse(
            [{"delta": {}, "index": 0, "finish_reason": "tool_calls"}],
            model=model,
        )
    )
    frames.append(b"data: [DONE]\n\n")
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

    SECURITY — LOCAL-ONLY BRIDGE: the adapter proxies requests to Amazon Q
    using the plugin's stored Builder ID token, so it must never be reachable
    from the network. It binds loopback (``127.0.0.1`` / ``::1`` / ``localhost``)
    by default. Binding any other host is rejected by ``_resolve_bind_host``
    unless ``AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1`` is set explicitly. There is no
    auth on the endpoint itself — that is safe ONLY because it is loopback-only.
    """
    global _server, _thread
    if _server is not None:
        return _server, _server.server_address[1]  # type: ignore[union-attr]

    bind_host = _resolve_bind_host(host)
    srv = ThreadingHTTPServer((bind_host, port), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _server, _thread = srv, t
    return srv, srv.server_address[1]  # return the ACTUAL bound port (port=0 -> OS picks)


def stop() -> None:
    """Stop the adapter (tests / cleanup). No-op if not running."""
    global _server, _thread
    if _server is not None:
        _server.shutdown()
        _server.server_close()
    _server, _thread = None, None


if __name__ == "__main__":
    srv, p = start()
    print(f"aws-build adapter listening on http://{HOST}:{p}/v1/chat/completions")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop()
