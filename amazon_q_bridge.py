#!/usr/bin/env python3
"""
amazon_q_bridge.py — OpenAI-compatible HTTP bridge for Amazon Q `q chat`.

Exposes an /v1/chat/completions endpoint that shells out to the `q chat`
CLI as the inference substrate. Designed to be provider-qualified, to fail
cleanly with structured error envelopes, and to run on CPython 3.9+.

Key invariants (verified by the ad-hoc test harness):
  * `_run_q_chat_pty(prompt, model, timeout) -> tuple[str, Optional[int], bool]`
  * `_error(type, message, http_status=500) -> dict` with an `_http_status` key
  * Subscription gating: if `q chat` output mentions a required subscription
    ("Kiro subscription" / "Q Developer Pro subscription") we return HTTP 403.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

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
DEFAULT_MODEL = "claude-sonnet-4"
REQUEST_TIMEOUT = 90  # seconds given to `q chat` to respond
# Aliases resolve via discover_models(); these are best-effort hints that are
# re-checked against the live catalog before use.
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4",
    "sonnet4": "claude-sonnet-4",
    "sonnet45": "claude-sonnet-4.5",
    "haiku": "claude-haiku-4.5",
    "haiku45": "claude-haiku-4.5",
    "opus": "claude-opus-4",
}
# Fallback used only if `q chat --model help` cannot be queried. The real list
# is discovered live (server-driven catalog that drifts).
FALLBACK_MODELS = (
    "claude-sonnet-4",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    "claude-3.7-sonnet",
)

# Cache for discovered models (TTL-based).
_MODEL_CACHE: list[str] = []
_MODEL_CACHE_TS: float = 0.0
_MODEL_CACHE_TTL = 300.0


def discover_models(force: bool = False) -> list[str]:
    """Return models `q chat --model` accepts, discovered live from the CLI.

    Parses `q chat --model help` -> "Available models: a, b". Caches for
    _MODEL_CACHE_TTL seconds. Falls back to FALLBACK_MODELS if q is missing
    or the parse fails.
    """
    global _MODEL_CACHE, _MODEL_CACHE_TS
    now = time.time()
    if not force and _MODEL_CACHE and (now - _MODEL_CACHE_TS) < _MODEL_CACHE_TTL:
        return _MODEL_CACHE
    try:
        proc = subprocess.run(
            [Q_BIN, "chat", "--model", "help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=6,
        )
        out = proc.stdout.decode(errors="replace")
        m = re.search(r"Available models:\s*([^\n]+)", out)
        if m:
            models = [x.strip() for x in m.group(1).split(",") if x.strip()]
            if models:
                _MODEL_CACHE, _MODEL_CACHE_TS = models, now
                return models
    except Exception:
        pass
    return list(FALLBACK_MODELS)


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
# Substrate: invoke `q chat` via subprocess (no PTY)
# --------------------------------------------------------------------------- #
def _run_q_chat_pty(prompt: str, model: str, timeout: int = REQUEST_TIMEOUT):
    """Run `q chat` via subprocess; return (output_text, exit_code_or_None, ok).

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
        return output_str, status, ok
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        output_str = out.decode(errors="replace")
        if os.environ.get("AMAZON_Q_DEBUG"):
            _dump(model, None, output_str, note="timeout")
        return output_str, None, False
    except Exception as exc:  # pragma: no cover - defensive
        if os.environ.get("AMAZON_Q_DEBUG"):
            _dump(model, -1, str(exc), note="exception")
        # -1 (distinct from None=timeout) so the handler can report a 502
        # upstream error instead of mislabeling it as a timeout.
        return "", -1, False


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


def _normalize_model(model: str) -> str:
    if not model:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(model.lower(), model)


def _subscription_blocked(output: str) -> bool:
    return any(s in output for s in SUBSCRIPTION_GATE_STRINGS)


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

    def _send(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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
        if urlparse(self.path).path != "/v1/chat/completions":
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

        prompt = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                prompt = (m.get("content") or "").strip()
                break
        if not prompt:
            self._send(_error("invalid_request_error", "no user message found", 400), 400)
            return

        model = _normalize_model(data.get("model", DEFAULT_MODEL))
        # Validate against the live-discovered set `q chat --model` accepts.
        if model not in valid_models():
            self._send(
                _error("invalid_request_error", f"invalid model name: {model}", 400),
                400,
            )
            return
        output, status, ok = _run_q_chat_pty(prompt, model)

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
            }
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
