#!/usr/bin/env python3
"""
amazon_q_bridge.py - OpenAI-compatible HTTP bridge for Amazon Q.

By default (BACKEND="direct") it calls Q's HTTPS API through q_direct.py and
needs NO `q chat` CLI binary. An opt-in "subprocess" backend that shells out to
the `q chat` CLI is also provided but is not the default. Designed to be
provider-qualified, to fail cleanly with structured error envelopes, and to run
on CPython 3.9+.

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
import socket
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
# `q` is the Amazon Q Developer CLI binary. The bridge is binary-FREE by
# default (BACKEND="direct" uses q_direct.py over HTTPS); `Q_BIN` is only
# consulted on the opt-in "subprocess" backend. Resolve from installed
# locations / PATH only — the local source build under
# ~/amazon-q-developer-cli/target is intentionally NOT referenced (it was a
# 14GB dead build cache, removed per user request).
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
# --------------------------------------------------------------------------- #
# Plugin settings — config.yaml (source of truth) with env-var override
# --------------------------------------------------------------------------- #
def load_plugin_config(path: str | None = None) -> dict:
    """Load aws-build plugin settings from config.yaml.

    Falls back to empty dict when the file is absent or unparsable, so the
    bridge still runs with built-in defaults + env vars. Env vars always win
    over config.yaml (see the BACKEND/DEFAULT_MODEL wiring below).

    PyYAML is preferred; if it's unavailable we fall back to a minimal parser
    that handles the flat `key: value` and simple `- item` list shapes this
    plugin's config uses, so a missing dependency never crashes startup.
    """
    cfg_path = path or os.path.join(os.path.dirname(__file__), "config.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw) or {}
        return data if isinstance(data, dict) else {}
    except ImportError:
        return _parse_simple_config(raw)
    except Exception:
        # A broken config must not crash startup; fall back to defaults.
        return {}


def _parse_simple_config(raw: str) -> dict:
    """Minimal fallback parser for flat `key: value` + `- item` lists."""
    result: dict = {}
    list_key: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if list_key is not None:
            if stripped.startswith("- "):
                result.setdefault(list_key, []).append(stripped[2:].strip())
                continue
            list_key = None
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key, val = key.strip(), val.strip()
        if val == "":
            list_key = key  # next lines may be list items
            result.setdefault(key, [])
        else:
            result[key] = val
    return result


_PLUGIN_CONFIG = load_plugin_config()


def _config_str(key: str, default: str) -> str:
    env_val = os.environ.get(f"AMAZON_Q_{key.upper()}")
    if env_val:
        return env_val
    val = _PLUGIN_CONFIG.get(key)
    return str(val) if isinstance(val, (str, int, float, bool)) else default


def _config_bool(key: str, default: bool) -> bool:
    env_val = os.environ.get(f"AMAZON_Q_{key.upper()}")
    if env_val:
        return env_val.strip().lower() in {"1", "true", "yes", "on"}
    val = _PLUGIN_CONFIG.get(key)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _config_list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    env_val = os.environ.get(f"AMAZON_Q_{key.upper()}")
    if env_val:
        return tuple(m.strip() for m in env_val.split(",") if m.strip())
    val = _PLUGIN_CONFIG.get(key)
    if isinstance(val, str):
        return tuple(m.strip() for m in val.split(",") if m.strip())
    if isinstance(val, (list, tuple)):
        return tuple(str(m).strip() for m in val if str(m).strip())
    return default


DEFAULT_MODEL = _config_str("default_model", "claude-haiku-4.5")
REQUEST_TIMEOUT = 90  # seconds given to `q chat` to respond
# Aliases resolve via discover_models(); these are best-effort hints that are
# re-checked against the live catalog before use.
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4",
    "sonnet4": "claude-sonnet-4",
    "sonnet45": "claude-sonnet-4.5",
    "haiku": "claude-haiku-4.5",
    "haiku45": "claude-haiku-4.5",
    # Dash/dot tolerance for the models `q chat` actually accepts.
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-haiku-4-5": "claude-haiku-4.5",
    "claude-sonnet": "claude-sonnet-4",
    "claude-haiku": "claude-haiku-4.5",
    # NOTE: no claude-opus-* aliases — `q chat` rejects those models
    # ("Model does not exist"). Add them only via extra_models once real.
}
# Static catalog matching `q chat --model <bad>`'s "Available models:" list
# (verified live via `q chat --model <x>`: only claude-sonnet-4.5,
# claude-sonnet-4, claude-haiku-4.5 are accepted; claude-opus-* is REJECTED
# with "Model does not exist"). Used as the instant default for GET /v1/models
# and as the fallback if the live `q` probe fails. Extend at runtime via
# AMAZON_Q_EXTRA_MODELS (comma-separated) or config.yaml `extra_models` when
# Q actually ships a new model — do NOT hardcode assumed names here.
FALLBACK_MODELS = (
    "claude-haiku-4.5",
    "claude-sonnet-4",
    "claude-sonnet-4.5",
)
# Optional runtime extension of the served catalog. Lets the user add models
# Q has shipped (e.g. claude-opus-4) without a code change. Sourced from the
# AMAZON_Q_MODELS env var or config.yaml `extra_models`, with env winning.
EXTRA_MODELS = _config_list("extra_models", ())

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
# amazon-q-developer-cli build/install required. Sourced from AMAZON_Q_BACKEND
# Sourced from AMAZON_Q_BACKEND env or config.yaml `backend`, with env winning.
BACKEND = _config_str("backend", "direct").lower()
# Verbose transcript dump to /tmp/q_raw_<pid>.log. Env AMAZON_Q_DEBUG or
# config.yaml `debug` (env wins).
DEBUG = _config_bool("debug", False)


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


def cli(subcommand: str = "chat", *args: str, timeout: int = REQUEST_TIMEOUT) -> tuple[str, int]:
    """Run `q <subcommand> ...` and return (combined_output, returncode).

    This is the single entrypoint for invoking the Amazon Q Developer CLI
    from the bridge. Callers can use it for arbitrary subcommands, not just
    `q chat`, while preserving the same output/error semantics as the old
    inline subprocess calls.
    """
    cmd = [Q_BIN, subcommand, *args]
    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    output = (completed.stdout or b"").decode(errors="replace")
    return output, completed.returncode


def _run_q_chat_subprocess(prompt: str, model: str, timeout: int = REQUEST_TIMEOUT):
    """Run `q chat` via subprocess; return (output_text, exit_code_or_None, ok, None).

    Avoids the prior PTY implementation that raised
    ``OSError: [Errno 9] Bad file descriptor`` on FD lifecycle races.
    """
    try:
        output, status = cli("chat", "--no-interactive", "--model", model, prompt, timeout=timeout)
        ok = status == 0
        if DEBUG:
            _dump(model, status, output)
        return output, status, ok, None
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode(errors="replace")
        if DEBUG:
            _dump(model, None, output, note="timeout")
        return output, None, False, None
    except Exception as exc:  # pragma: no cover - defensive
        if DEBUG:
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


# --------------------------------------------------------------------------- #
# Agentic backend (binary-free tool use, no `q` CLI)
# --------------------------------------------------------------------------- #
# The `agentic` backend turns the chat-only `direct` API into a ReAct-style
# agent: it instructs Q (via the prompt) to emit a structured tool call, parses
# it, sends the call to Hermes over Unix IPC, feeds the result back, and loops
# until Q returns a final answer. This is exactly what `q chat` does
# internally -- reimplemented in-plugin so it works without the `q` binary and
# and without any Hermes-core change. Hermes remains the sole owner of tool
# execution; the bridge is only the Q inference/client loop.
import re as _re

# Default tool set: file read/write only (tight safety story). `bash` may be
# added via config.yaml `agentic_tools` when command execution is wanted.
DEFAULT_AGENTIC_TOOLS = ("fs_read", "fs_write")
_TOOL_BLOCK_RE = _re.compile(
    r"<function_calls>\s*<invoke\s+name=\"(?P<name>[a-zA-Z_][\w-]*)\">"
    r"(?P<args>.*?)</invoke>\s*</function_calls>",
    _re.DOTALL,
)
# Legacy fallback: some Q turns (or older prompts) emit a simpler block.
_TOOL_BLOCK_LEGACY_RE = _re.compile(
    r"<tool>\s*(?P<name>[a-zA-Z_][\w-]*)\s*</tool>\s*<args>(?P<args>.*?)</args>",
    _re.DOTALL,
)


def _agentic_root() -> str:
    """Sandbox directory the agentic tools may touch. Configurable; defaults
    to an isolated temp dir so the agent cannot wander the whole filesystem."""
    root = _config_str("agentic_root", "")
    root = root.strip() if root else ""
    if not root:
        import tempfile

        root = os.path.join(tempfile.gettempdir(), "awsbuild_agentic")
    os.makedirs(root, exist_ok=True)
    return os.path.abspath(root)


def _agentic_tools() -> tuple:
    return _config_list("agentic_tools", DEFAULT_AGENTIC_TOOLS)


def _agentic_max_iters() -> int:
    try:
        return max(1, int(_config_str("agentic_max_iters", "8")))
    except (TypeError, ValueError):
        return 8


def _agentic_timeout() -> int:
    try:
        return max(1, int(_config_str("agentic_timeout", "30")))
    except (TypeError, ValueError):
        return 30


def _tool_protocol_prompt(user_prompt: str, tools: tuple, root: str) -> str:
    """Wrap the user prompt with the tool-use protocol Q must follow."""
    tool_lines = []
    for t in tools:
        if t == "fs_read":
            tool_lines.append("- fs_read: read a file. args = absolute or relative path under the sandbox.")
        elif t == "fs_write":
            tool_lines.append("- fs_write: write/append a file. args = 'path\\n<content>' (first line is the path).")
        elif t == "bash":
            tool_lines.append("- bash: run a shell command (non-interactive). args = the command. CWD is the sandbox.")
    tools_block = "\n".join(tool_lines) if tool_lines else "(no tools available)"
    return (
        "You are an agentic coding assistant. To use a tool, respond with EXACTLY "
        "this block on its own, then stop and wait for the result:\n"
        "<tool>TOOL_NAME</tool>\n"
        "<args>TOOL_ARGS</args>\n"
        "When you have the final answer (no more tools needed), reply in normal text.\n"
        f"Available tools (sandbox root = {root}):\n{tools_block}\n\n"
        f"USER REQUEST:\n{user_prompt}"
    )


def _exec_tool_via_socket(tool: str, args: dict, root: str, timeout: int = 30) -> str:
    """Send a tool call to the Hermes-owned executor over the Unix IPC socket.

    `args` is the parsed parameter dict from ``parse_tool_call`` (e.g.
    {"path": ..., "content": ...} for fs_write). The plugin process owns the
    socket and dispatches through Hermes's native tool runtime; this function is
    a client only and returns an error string if ``AMAZON_Q_TOOL_SOCKET`` is
    unset or the socket is missing.
    """
    socket_path = os.environ.get("AMAZON_Q_TOOL_SOCKET")
    if not socket_path or not os.path.exists(socket_path):
        return "(Hermes tool socket not available)"
    if tool == "fs_write":
        content = f'{args.get("path", "")}\n{args.get("content", "")}'
        payload = json.dumps({
            "tool": "fs_write",
            "args": {"content": content, "root": root},
        }) + "\n"
    elif tool == "fs_read":
        payload = json.dumps({
            "tool": "fs_read",
            "args": {"path": args.get("path", ""), "root": root},
        }) + "\n"
    elif tool == "bash":
        payload = json.dumps({
            "tool": "bash",
            "args": {"command": args.get("command", ""), "root": root},
        }) + "\n"
    else:
        payload = json.dumps({"tool": tool, "args": args}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(socket_path)
            s.sendall(payload.encode("utf-8"))
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
        text = buf.decode("utf-8", errors="replace").strip()
        if not text:
            return "(empty response from tool server)"
        try:
            envelope = json.loads(text)
        except Exception:
            return text
        if envelope.get("ok"):
            result = envelope.get("result", "")
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return envelope.get("error") or json.dumps(envelope, ensure_ascii=False)
    except Exception as exc:
        return f"tool socket error: {type(exc).__name__}: {exc}"


def parse_tool_call(text: str):
    """Return (name, args_dict) if `text` contains a well-formed tool block, else None.

    Accepts Q's native agentic serialization (the same format the `q chat` CLI
    parses), plus a legacy `<tool>/<args>` fallback:

        <function_calls>
        <invoke name="fs_write">
        <parameter name="path">greeting.txt</parameter>
        <parameter name="content">HELLO</parameter>
        </invoke>
        </function_calls>

    Each <parameter name="k">v</parameter> becomes an entry in the returned
    args dict. `name` must be one of the configured agentic tools. Malformed
    blocks are ignored (treated as final text) so the agent never crashes on a
    slightly-off model response.
    """
    import re as _re_inner

    m = _TOOL_BLOCK_RE.search(text)
    if m:
        name = m.group("name")
        if name not in _agentic_tools():
            return None
        args = {}
        for pm in _re_inner.finditer(
            r'<parameter\s+name="(?P<k>[^"]+)">(?P<v>.*?)</parameter>',
            m.group("args"), _re.DOTALL,
        ):
            args[pm.group("k")] = pm.group("v")
        if not args and m.group("args").strip().startswith("{"):
            try:
                args = json.loads(m.group("args").strip())
            except Exception:
                args = {}
        return name, args

    # Legacy fallback: <tool>NAME</tool><args>path\ncontent</args>
    m = _TOOL_BLOCK_LEGACY_RE.search(text)
    if m:
        name = m.group("name")
        if name not in _agentic_tools():
            return None
        raw = m.group("args").strip()
        args = {}
        if name == "fs_write":
            lines = raw.split("\n", 1)
            args = {"path": lines[0].strip(),
                    "content": lines[1] if len(lines) > 1 else ""}
        elif name == "fs_read":
            args = {"path": raw.strip()}
        elif name == "bash":
            args = {"command": raw.strip()}
        else:
            args = {"text": raw}
        return name, args

    return None


def exec_tool(name: str, args: str, root: str, timeout: int) -> str:
    """Execute a single tool locally, sandboxed to `root`. Returns a short
    result string (or an error string -- never raises)."""
    root = os.path.abspath(root)

    def _safe_path(p: str) -> str:
        cand = os.path.abspath(os.path.join(root, p))
        if not (cand == root or cand.startswith(root + os.sep)):
            raise ValueError(f"path escapes sandbox: {p}")
        return cand

    try:
        if name == "fs_read":
            path = _safe_path(args.strip())
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()[:20000]
        if name == "fs_write":
            lines = args.split("\n", 1)
            path = _safe_path(lines[0].strip())
            content = lines[1] if len(lines) > 1 else ""
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return f"wrote {len(content)} bytes to {path}"
        if name == "bash":
            import subprocess as _sp

            completed = _sp.run(
                args,
                shell=True,
                cwd=root,
                stdout=_sp.PIPE,
                stderr=_sp.STDOUT,
                timeout=timeout,
            )
            return (completed.stdout or b"").decode(errors="replace")[:5000]
        return f"unknown tool: {name}"
    except Exception as exc:  # noqa: BLE001
        return f"tool error: {type(exc).__name__}: {exc}"


def _q_tool_specs(tools: tuple) -> list:
    """Build the API `tools` array advertised to Q (enables agentic mode).

    Mirrors the exact shape the `q chat` CLI sends in
    userInputMessageContext.tools (verified against the open-source
    amazon-q-developer-cli serializer: toolSpecification/inputSchema/json).
    The names map to our executor's tool names (fs_write/fs_read/bash). Without
    this array + `origin: "CLI"`, Q replies "agentic-coding OFF".
    """
    specs = []
    schema_props = {
        "fs_write": {"content": {"type": "string"}},
        "fs_read": {"path": {"type": "string"}},
        "bash": {"command": {"type": "string"}},
    }
    desc = {
        "fs_write": "Write text content to a file in the sandbox. "
                    "args: first line is the relative path, rest is content.",
        "fs_read": "Read a file's text content from the sandbox. args: the relative path.",
        "bash": "Run a non-interactive shell command in the sandbox. args: the command string.",
    }
    for t in tools:
        if t not in schema_props:
            continue
        specs.append({
            "toolSpecification": {
                "name": t,
                "description": desc[t],
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": schema_props[t],
                    "required": list(schema_props[t].keys()),
                }},
            }
        })
    return specs


def run_agentic(prompt: str, model: str, max_iters: int | None = None,
                timeout: int | None = None):
    """ReAct loop over the direct Q API. Returns (answer_text, status, ok, cid).

    Agentic mode is enabled by advertising `tools` + `origin: "CLI"` on the
    first call (verified live). Q emits a `<function_calls>` block; we execute
    it via the Hermes-owned socket and feed the result back through
    `toolResults` so Q's agentic loop continues to a final answer.
    """
    import q_direct

    root = _agentic_root()
    tools = _agentic_tools()
    tool_specs = _q_tool_specs(tools)
    max_iters = max_iters or _agentic_max_iters()
    timeout = timeout or _agentic_timeout()
    socket_path = os.environ.get("AMAZON_Q_TOOL_SOCKET")

    last_cid = None
    tool_use_id = None
    tool_results = None
    # First turn: the user's request. Later turns: a continuation telling Q the
    # tool result is in, so it should emit the next step or a final answer.
    convo = _tool_protocol_prompt(prompt, tools, root)
    answer = convo
    for _ in range(max_iters):
        try:
            answer, last_cid, tool_use_id = q_direct.chat(
                convo, model=model,
                conversation_id=last_cid,
                tools=tool_specs if tool_results is None else None,
                tool_results=tool_results,
            )
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}", -1, False, last_cid
        call = parse_tool_call(answer)
        if not call:
            # No tool block -> final answer.
            return answer.strip(), 0, True, last_cid
        name, args = call
        if not socket_path:
            return (
                "Hermes tool executor unavailable: "
                "AMAZON_Q_TOOL_SOCKET is not set",
                -1,
                False,
                last_cid,
            )
        result = _exec_tool_via_socket(name, args, root, timeout=timeout)
        # Feed the executed result back to Q so its agentic loop continues.
        tool_results = [{
            "toolUseId": tool_use_id or f"tool-{len(tool_results or []) + 1}",
            "content": [{"text": result}],
            "status": "SUCCESS",
        }]
        # Reset to a continuation prompt for the next turn.
        convo = "Tool executed. Continue with the next step or give the final answer."
    # Hit iteration cap: return the last model text so the turn still produces
    # a usable (if possibly incomplete) answer.
    return answer.strip(), 0, True, last_cid


def _run_agentic(prompt: str, model: str, conversation_id: str | None = None):
    """Public entry used by do_POST. Mirrors _run_q_chat_pty's return shape."""
    return run_agentic(prompt, model)


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
        if BACKEND == "agentic":
            output, status, ok, _conversation_id = _run_agentic(
                prompt, model, conversation_id=conversation_id
            )
        else:
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
        if DEBUG:
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
