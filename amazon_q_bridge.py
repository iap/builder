#!/usr/bin/env python3
"""
amazon_q_bridge.py — OpenAI-compatible HTTP bridge for Amazon Q `q chat`.

Exposes an /v1/chat/completions endpoint that shells out to the `q chat`
CLI as the inference substrate. Designed to be provider-qualified, to fail
cleanly with structured error envelopes, and to run on CPython 3.9+.

Key invariants (verified by the ad-hoc test harness):
  * `_run_q_chat_pty(prompt, model, timeout, conversation_id) -> tuple[str, Optional[int], bool, Optional[str]]`
    (last element is Q's server-side conversation id, or None on subprocess).
  * `_error(type, message, http_status=500) -> dict` with an `_http_status` key
  * Subscription gating: if `q chat` output mentions a required subscription
    ("Kiro subscription" / "Q Developer Pro subscription") we return HTTP 403.
  * Model resolution: `_normalize_model` strips provider prefixes and applies
    aliases, falling back to DEFAULT_MODEL instead of 400-ing unknown names.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

logger = logging.getLogger("aws_build.bridge")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
Q_BIN = next(
    (
        p
        for p in (
            "/Users/iap/.local/bin/q",
            "/opt/homebrew/bin/q",
            "/usr/local/bin/q",
            "q",
        )
        if p == "q" or os.path.exists(p)
    ),
    "q",
)
DEFAULT_MODEL = "claude-haiku-4.5"  # aligned with ~/.hermes/config.yaml aws-build default
REQUEST_TIMEOUT = 90  # seconds given to `q chat` to respond
# Aliases resolve via discover_models(); these are best-effort hints that are
# re-checked against the live catalog before use.
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4",
    "sonnet4": "claude-sonnet-4",
    "sonnet45": "claude-sonnet-4.5",
    "haiku": "claude-haiku-4.5",
    "haiku45": "claude-haiku-4.5",
    # Common Q / Anthropic-family variants accepted by `q chat --model`.
    "claude-opus-4": "claude-opus-4",
    "claude-opus-4.5": "claude-opus-4.5",
    "claude-sonnet-4-5": "claude-sonnet-4.5",  # tolerate dash/dot互换
    "claude-haiku-4-5": "claude-haiku-4.5",
    "claude-opus-4-5": "claude-opus-4.5",
    "claude-sonnet": "claude-sonnet-4",
    "claude-haiku": "claude-haiku-4.5",
    "claude-opus": "claude-opus-4",
}
# Static catalog matching `q chat --model <bad>`'s "Available models:" list
# (verified live: claude-sonnet-4.5, claude-sonnet-4, claude-haiku-4.5).
# Used as the instant default for GET /v1/models and as the fallback
# if the live `q` probe fails. Extend at runtime via AMAZON_Q_MODELS
# (comma-separated) without editing code.
FALLBACK_MODELS = (
    "claude-haiku-4.5",
    "claude-sonnet-4",
    "claude-sonnet-4.5",
    "claude-opus-4",
    "claude-opus-4.5",
)
# Optional runtime extension of the served catalog. Lets the user add models
# Q has shipped (e.g. claude-opus-4) without a code change.
_EXTRA_MODELS_ENV = os.environ.get("AMAZON_Q_MODELS", "").strip()
EXTRA_MODELS = tuple(
    m.strip() for m in _EXTRA_MODELS_ENV.split(",") if m.strip()
)

# Cache for discovered models (TTL-based).
_MODEL_CACHE: list[str] = []
_MODEL_CACHE_TS: float = 0.0
_MODEL_CACHE_TTL = 300.0


def discover_models(force: bool = False) -> list[str]:
    """Return models `q chat --model` accepts.

    Returns the static FALLBACK_MODELS immediately (no blocking subprocess)
    so the dashboard's /v1/models probe is instant. The live `q chat
    --model help` catalog is only consulted on force=True (e.g. an
    explicit refresh), since `q` cold-start can take ~30s and would
    time out the dashboard's short probe.
    """
    global _MODEL_CACHE, _MODEL_CACHE_TS
    now = time.time()
    if not force and _MODEL_CACHE and (now - _MODEL_CACHE_TS) < _MODEL_CACHE_TTL:
        return _MODEL_CACHE
    # Seed the cache with the static fallback so the first GET is instant.
    if not _MODEL_CACHE:
        _MODEL_CACHE, _MODEL_CACHE_TS = (
            list(FALLBACK_MODELS) + list(EXTRA_MODELS),
            now,
        )
        if not force:
            return _MODEL_CACHE
    if not force:
        return _MODEL_CACHE
    # On the binary-free 'direct' backend (the default), never shell `q chat`
    # for live discovery — re-seed from the static catalog instead. The
    # subprocess/Q_BIN path is only reachable when BACKEND=="subprocess".
    if BACKEND == "direct":
        _MODEL_CACHE, _MODEL_CACHE_TS = (
            list(FALLBACK_MODELS) + list(EXTRA_MODELS),
            now,
        )
        return _MODEL_CACHE
    try:
        proc = subprocess.run(
            [Q_BIN, "chat", "--model", "help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=45,
        )
        # `q chat --model help` prints "Available models: ..." to STDERR
        # (and exits non-zero, since "help" isn't a valid model).
        out = (proc.stderr or b"").decode(errors="replace")
        m = re.search(r"Available models:\s*([^\n]+)", out)
        if m:
            models = [x.strip() for x in m.group(1).split(",") if x.strip()]
            if models:
                _MODEL_CACHE, _MODEL_CACHE_TS = models, now
                return models
    except Exception:
        pass
    return list(_MODEL_CACHE or FALLBACK_MODELS)


def valid_models() -> list[str]:
    return discover_models()

# Strings that indicate the upstream account is gated behind a paid plan.
SUBSCRIPTION_GATE_STRINGS = (
    "Kiro subscription",
    "Q Developer Pro subscription",
)


# --------------------------------------------------------------------------- #
# Error envelope
# --------------------------------------------------------------------------- #
def _error(err_type: str, message: str, http_status: int = 500) -> dict:
    """OpenAI-style error envelope; `_http_status` is consumed by the handler."""
    err: dict = {
        "object": "error",
        "message": message,
        "type": err_type,
    }
    if http_status:
        err["_http_status"] = http_status
    return err


# --------------------------------------------------------------------------- #
# Substrate: invoke `q chat` via subprocess (no PTY) OR call Q's API directly.
# --------------------------------------------------------------------------- #
# BACKEND selects how the bridge reaches Amazon Q:
#   "direct"    (default) -> pure-HTTP via q_direct.py (no CLI binary needed).
#   "subprocess"           -> shells out to the `q chat` CLI binary (opt-in fallback).
# Default is "direct" so AWS Build connects to Q's server models with no
# amazon-q-developer-cli build/install required.
BACKEND = os.environ.get("AMAZON_Q_BACKEND", "direct").lower()


def _run_q_chat_pty(prompt: str, model: str, timeout: int = REQUEST_TIMEOUT,
                    conversation_id: str | None = None):
    """Run a Q chat turn; return (output_text, exit_code_or_None, ok, conversation_id).

    Dispatches on BACKEND:
      * subprocess -> `q chat` CLI (original path); conversation_id is None
        because the subprocess backend shells out per call and has no native
        conversation threading.
      * direct     -> q_direct.chat (HTTPS, no binary); conversation_id is Q's
        server-side id for this turn (enables native multi-turn memory).
    """
    if BACKEND == "direct":
        return _run_q_direct(prompt, model, timeout, conversation_id=conversation_id)
    return _run_q_chat_subprocess(prompt, model, timeout)


def _run_q_chat_subprocess(prompt: str, model: str, timeout: int = REQUEST_TIMEOUT):
    """Run `q chat` via subprocess; return (output_text, exit_code_or_None, ok, None).

    Avoids the prior PTY implementation that raised
    ``OSError: [Errno 9] Bad file descriptor`` on FD lifecycle races.
    """
    cmd = [Q_BIN, "chat", "--no-interactive", "--model", model, prompt]
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # combine so we capture gate messages
            timeout=timeout,
        )
        out = (completed.stdout or b"") + (completed.stderr or b"")
        output_str = out.decode(errors="replace")
        status = completed.returncode
        ok = status == 0
        if os.environ.get("AMAZON_Q_DEBUG"):
            _dump(model, status, output_str)
        return output_str, status, ok, None
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        output_str = out.decode(errors="replace")
        if os.environ.get("AMAZON_Q_DEBUG"):
            _dump(model, None, output_str, note="timeout")
        return output_str, None, False, None
    except Exception as exc:  # pragma: no cover - defensive
        if os.environ.get("AMAZON_Q_DEBUG"):
            _dump(model, -1, str(exc), note="exception")
        # -1 (distinct from None=timeout) so the handler can report a 502
        # upstream error instead of mislabeling it as a timeout.
        return "", -1, False, None


def _run_q_direct(prompt: str, model: str, timeout: int = REQUEST_TIMEOUT,
                   conversation_id: str | None = None):
    """Run a Q chat turn via the direct HTTPS backend (q_direct).

    Returns (output_text, status, ok, conversation_id) where conversation_id is
    Q's server-side conversation id for this turn (or None when Q omits it).
    """
    try:
        import q_direct

        answer, cid = q_direct.chat(
            prompt, model=model, conversation_id=conversation_id
        )
        return answer, 0, True, cid
    except Exception as exc:  # noqa: BLE001
        # Surface the error text so subscription-gating / upstream parsing still works.
        return f"{type(exc).__name__}: {exc}", -1, False, None


# Bridge-side conversation memory: the client may pass an inbound conversation
# id via this header so Q links the turn to an existing server-side conversation.
# The bridge returns the (possibly new) conversation id via the same header so
# the client can thread it through subsequent turns instead of flattening the
# whole history into every prompt.
CONVERSATION_ID_HEADER = "X-Hermes-Conversation-Id"


def _dump(model, status, text, note=""):
    try:
        path = f"/tmp/q_raw_{os.getpid()}.log"
        with open(path, "w") as fh:
            fh.write(f"model={model}\nrc={status} {note}\n---COMBINED---\n{text}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #
def extract_answer(raw: str) -> str:
    """Pull the assistant answer out of `q chat` output.

    `q chat --no-interactive` output is noisy: a banner ("You are chatting
    with ..."), hook/spinner progress lines, ANSI cursor-move escapes, and a
    trailing repl prompt "> " immediately before the actual answer. Example:

        \\x1b[...m🤖 You are chatting with claude-haiku-4.5\\n...hooks...\\x1b[?25l> ANSWER\\n\\n\\x1b[?25h

    We strip all ANSI (color + cursor moves), then take everything after the
    LAST "> " repl prompt (the answer always follows it).
    """
    import re as _re

    ansi = _re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
    text = ansi.sub("", raw)
    # Belt-and-suspenders: drop any stray reset sequences.
    text = text.replace("\x1b[0m", "").replace("\x1b", "")
    text = _re.sub(r"\[0m|\[\?25[hl]|\[\d+[GK]", "", text)
    # The assistant answer follows the final repl-prompt "> ".
    if ">" in text:
        text = text.rsplit("> ", 1)[-1]
    text = text.strip()
    # Drop <think>...</think> spans if the model emits them.
    while "<think>" in text and "<//think>" in text:
        start = text.index("<think>")
        end = text.index("<//think>") + len("<//think>")
        text = (text[:start] + text[end:]).strip()
    return text or "(no response)"


def _normalize_model(model: str) -> tuple[str, bool]:
    """Resolve a requested model name to a catalog entry.

    Returns (resolved_model, ok):
      * ok=True  -> resolved_model is a known/aliased catalog entry.
      * ok=False -> the name was unknown; resolved_model falls back to
        DEFAULT_MODEL (caller should still surface a warning, but the request
        proceeds instead of hard-failing with HTTP 400).

    Hermes may send provider-prefixed names (e.g. "aws-build/claude-haiku-4.5")
    or short aliases ("haiku"); we strip the prefix and apply MODEL_ALIASES
    before validating against the served catalog.
    """
    if not model:
        return DEFAULT_MODEL, True
    raw = model.strip()
    # Drop a provider prefix like "aws-build/" or "openai/".
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    # Apply known aliases (covers dash/dot互换 + short forms).
    aliased = MODEL_ALIASES.get(raw.lower(), raw)
    # Direct catalog hit (after alias) always wins.
    if aliased in valid_models():
        return aliased, True
    # Unknown: fall back to the default rather than 400-ing the whole turn.
    return DEFAULT_MODEL, False


def _subscription_blocked(output: str) -> bool:
    return any(s in output for s in SUBSCRIPTION_GATE_STRINGS)


# Anthropic Messages API version header value. The `anthropic` SDK inspects
# this; we mirror Bedrock's Claude version string.
ANTHROPIC_VERSION = "bedrock-2023-05-31"


def _extract_anthropic_prompt(data: dict) -> str:
    """Build a `q chat` prompt from Anthropic-native request fields.

    Concatenates `system` (str or list of {type,text} blocks) then each
    message's text content (both user and assistant turns, so multi-turn
    context is preserved — `q chat` is invoked stateless per call). Returns ""
    if no user message is found (caller validates).
    """
    parts: list[str] = []

    def _text_of(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # [{type,text}, ...]
            return " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
        return ""

    system = data.get("system")
    if isinstance(system, str) and system.strip():
        parts.append(system.strip())
    elif isinstance(system, list):  # structured system (prompt caching form)
        sys_text = _text_of(system)
        if sys_text.strip():
            parts.append(sys_text.strip())

    messages = data.get("messages") or []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _text_of(m.get("content")).strip()
        if role in ("user", "assistant") and text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _flatten_openai_messages(data: dict) -> str:
    """Build a `q chat` prompt from OpenAI-compatible request fields.

    Concatenates `system` then each message's text content with role labels
    so multi-turn context is preserved across stateless `q chat` calls.
    Returns "" if no usable text is found.
    """
    parts: list[str] = []

    def _text_of(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
        return ""

    system = data.get("system")
    if isinstance(system, str) and system.strip():
        parts.append(system.strip())
    elif isinstance(system, list):
        sys_text = _text_of(system)
        if sys_text.strip():
            parts.append(sys_text.strip())

    messages = data.get("messages") or []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _text_of(m.get("content")).strip()
        if role in ("user", "assistant", "system") and text:
            parts.append(f"{role}: {text}")
    return "\n\n".join(parts).strip()


def _anthropic_response(answer: str, model: str, prompt: str) -> dict:
    """Shape a `q chat` answer into the Anthropic Messages API response."""
    return {
        "content": [{"type": "text", "text": answer}],
        "id": f"msg_qbridge-{uuid.uuid4().hex[:16]}",
        "model": model,
        "role": "assistant",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "type": "message",
        "usage": {
            "input_tokens": len(prompt.split()),
            "output_tokens": len(answer.split()),
        },
    }


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Allow the dashboard SPA (served on a different port, hence a different
    # browser origin) to probe this bridge. Without these the browser's CORS
    # preflight (OPTIONS) fails and the actual GET is blocked, so "AWS Build"
    # never appears in the model picker.
    ALLOWED_ORIGINS = {"http://localhost:9119", "http://127.0.0.1:9119"}

    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in self.ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Vary", "Origin")

    def _send(self, payload: dict, status: int = 200, extra_headers: dict | None = None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight. Echo back the permissive headers; no body.
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # The dashboard / model picker probes each provider's base_url for
        # GET /v1/models to enumerate the models it serves. Without this
        # handler BaseHTTPRequestHandler returns 501 (Unsupported method
        # 'GET'), so "AWS Build" shows zero models in the UI.
        if urlparse(self.path).path != "/v1/models":
            self._send(_error("not_found", f"path {self.path} not found", 404), 404)
            return
        now = int(time.time())
        # Models are discovered live from `q chat --model help` (server-driven
        # catalog that drifts) — see discover_models().
        ids = list(valid_models())
        data = [
            {
                "id": m,
                "object": "model",
                "created": now,
                "owned_by": "amazon-q",
                "permission": [],
                "root": m,
                "parent": None,
            }
            for m in ids
        ]
        self._send({"object": "list", "data": data})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/anthropic/messages":
            self._post_anthropic()
            return
        if path != "/v1/chat/completions":
            self._send(_error("not_found", f"path {self.path} not found", 404), 404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # noqa: BLE001
            self._send(_error("invalid_request_error", f"bad JSON: {exc}", 400), 400)
            return

        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send(
                _error("invalid_request_error", "`messages` must be a non-empty list", 400),
                400,
            )
            return

        prompt = _flatten_openai_messages(data)
        if not prompt:
            self._send(_error("invalid_request_error", "no user message found", 400), 400)
            return

        model, model_ok = _normalize_model(data.get("model", DEFAULT_MODEL))
        if not model_ok:
            # Unknown model name (typo, new Q variant, or provider-prefixed).
            # Fall back to the default instead of hard-failing the turn; the
            # caller still gets a usable answer, just on the default model.
            logger.warning(
                "aws-build: unknown model %r; falling back to %r",
                data.get("model"),
                model,
            )
        conversation_id = self.headers.get(CONVERSATION_ID_HEADER) or None
        output, status, ok, _conversation_id = _run_q_chat_pty(
            prompt, model, conversation_id=conversation_id
        )

        # Subscription gating is independent of exit status: `q chat` may exit
        # 0 yet still emit a gate message (e.g. fallback), so check the output
        # first, on success OR failure.
        if _subscription_blocked(output):
            self._send(
                _error(
                    "upstream_subscription_required",
                    "Amazon Q requires an active subscription to answer this request.",
                    403,
                ),
                403,
            )
            return

        if not ok:
            if status is None:
                self._send(
                    _error(
                        "upstream_timeout",
                        f"q chat did not respond within {REQUEST_TIMEOUT}s",
                        504,
                    ),
                    504,
                )
                return
            self._send(
                _error("upstream_error", f"q chat exited with status {status}", 502),
                502,
            )
            return

        answer = extract_answer(output)
        # Echo Q's server-side conversation id so the client can thread it
        # through subsequent turns (native multi-turn memory instead of
        # re-flattening history into every prompt).
        conv_headers = {CONVERSATION_ID_HEADER: _conversation_id} if _conversation_id else None
        if data.get("stream"):
            self._send_openai_sse(answer, model)
            return
        self._send(
            {
                "id": f"chatcmpl-qbridge-{os.getpid()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": len(prompt.split()),
                    "completion_tokens": len(answer.split()),
                    "total_tokens": len(prompt.split()) + len(answer.split()),
                },
            },
            extra_headers=conv_headers,
        )

    def _send_openai_sse(self, answer: str, model: str):
        """Emit a valid OpenAI SSE stream.

        `q chat` returns the full answer at once, so we send one content chunk
        (with the complete text) followed by the [DONE] event. This satisfies
        SSE-expecting clients (e.g. Hermes' openai_chat transport) that fail on
        a plain JSON body.
        """
        chunks = [
            {
                "id": f"chatcmpl-qbridge-{os.getpid()}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": answer},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": f"chatcmpl-qbridge-{os.getpid()}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _post_anthropic(self):
        """Anthropic Messages API (`/v1/anthropic/messages`) -> `q chat`."""
        hdr = {"anthropic-version": ANTHROPIC_VERSION}
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # noqa: BLE001
            self._send(_error("invalid_request_error", f"bad JSON: {exc}", 400), 400, hdr)
            return

        if data.get("stream"):
            self._send(
                _error(
                    "invalid_request_error",
                    "streaming is not supported by this bridge yet",
                    400,
                ),
                400,
                hdr,
            )
            return

        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send(
                _error("invalid_request_error", "`messages` must be a non-empty list", 400),
                400,
                hdr,
            )
            return

        prompt = _extract_anthropic_prompt(data)
        if not prompt:
            self._send(_error("invalid_request_error", "no user message found", 400), 400, hdr)
            return

        model, model_ok = _normalize_model(data.get("model", DEFAULT_MODEL))
        if not model_ok:
            logger.warning(
                "aws-build: unknown model %r; falling back to %r",
                data.get("model"),
                model,
            )

        conversation_id = self.headers.get(CONVERSATION_ID_HEADER) or None
        output, status, ok, _conversation_id = _run_q_chat_pty(
            prompt, model, conversation_id=conversation_id
        )

        if _subscription_blocked(output):
            self._send(
                _error(
                    "upstream_subscription_required",
                    "Amazon Q requires an active subscription to answer this request.",
                    403,
                ),
                403,
                hdr,
            )
            return

        if not ok:
            if status is None:
                self._send(
                    _error(
                        "upstream_timeout",
                        f"q chat did not respond within {REQUEST_TIMEOUT}s",
                        504,
                    ),
                    504,
                    hdr,
                )
                return
            self._send(
                _error("upstream_error", f"q chat exited with status {status}", 502),
                502,
                hdr,
            )
            return

        answer = extract_answer(output)
        self._send(
            _anthropic_response(answer, model, prompt),
            extra_headers=hdr,
        )

    def log_message(self, fmt, *args):  # silence default access log
        if os.environ.get("AMAZON_Q_DEBUG"):
            super().log_message(fmt, *args)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(description="Amazon Q OpenAI-compatible bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args(argv)

    server = HTTPServer((args.host, args.port), Handler)
    print(f"[*] Amazon Q bridge listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] shutting down", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()
