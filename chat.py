"""Native AWS Build chat tool — drives the public Amazon Q Developer CLI
(`q chat`) directly, no HTTP bridge required.

This is the agent-facing path: Hermes calls `aws_chat` and the plugin shells
out to `q chat --no-interactive --model <m> <prompt>`, then extracts the
clean answer from the noisy CLI output.

The public CLI (amazon-q-developer-cli) is open source; we drive it as the
inference/agent substrate. We are developers, not subscribers — no
subscription/anti-abuse/Bedrock gating is handled here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Any

# Fallback model set used only if `q chat --model help` cannot be queried
# (e.g. not logged in, or q missing). The authoritative list is discovered
# at runtime via discover_models() because the catalog is server-returned
# and drifts — hardcoding it produced stale/false-positive rejections.
FALLBACK_MODELS = (
    "claude-sonnet-4",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
    "claude-3.7-sonnet",
)
DEFAULT_MODEL = "claude-sonnet-4"
REQUEST_TIMEOUT = 120  # seconds given to `q chat` to respond

# Tool names `q chat --trust-tools` accepts (verified in source:
# crates/chat-cli/src/cli/chat/tools/mod.rs:74 NATIVE_TOOLS).
# MCP tools use the `@server/tool` form and are allowed through as-is.
NATIVE_TOOLS = {
    "fs_read",
    "fs_write",
    "execute_cmd",
    "execute_bash",
    "use_aws",
    "gh_issue",
    "knowledge",
    "thinking",
    "todo_list",
    "delegate",
}

# Cache for discovered models (TTL-based; catalog is server-driven).
_MODEL_CACHE: tuple[str, ...] = ()
_MODEL_CACHE_TS: float = 0.0
_MODEL_CACHE_TTL = 300.0  # seconds


def discover_models(force: bool = False) -> tuple[str, ...]:
    """Return the models `q chat --model` accepts, discovered live.

    Parses `q chat --model help` -> "Available models: a, b". Caches for
    _MODEL_CACHE_TTL seconds. Falls back to FALLBACK_MODELS if q is missing,
    not logged in, or the parse fails (so the tool still degrades gracefully).
    """
    global _MODEL_CACHE, _MODEL_CACHE_TS
    now = time.time()
    if not force and _MODEL_CACHE and (now - _MODEL_CACHE_TS) < _MODEL_CACHE_TTL:
        return _MODEL_CACHE
    try:
        proc = subprocess.run(
            [_q_bin(), "chat", "--model", "help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        out = proc.stdout.decode(errors="replace")
        # Example: "Available models: claude-sonnet-4, claude-3.7-sonnet"
        m = re.search(r"Available models:\s*([^\n]+)", out)
        if m:
            models = tuple(x.strip() for x in m.group(1).split(",") if x.strip())
            if models:
                _MODEL_CACHE, _MODEL_CACHE_TS = models, now
                return models
    except Exception:
        pass
    # Fallback (do not cache the fallback indefinitely).
    return FALLBACK_MODELS


def valid_models() -> tuple[str, ...]:
    return discover_models()

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _q_bin() -> str:
    for cand in (
        "/Users/iap/.local/bin/q",
        "/opt/homebrew/bin/q",
        "/usr/local/bin/q",
        "q",
    ):
        if cand == "q" or os.path.exists(cand):
            return cand
    return "q"


def _extract_answer(raw: str) -> str:
    """Pull the assistant answer out of `q chat --no-interactive` output.

    Output is noisy: a banner ("You are chatting with ..."), hook/spinner
    progress lines, ANSI cursor-move escapes, and a trailing repl prompt
    "> " immediately before the actual answer. Strip all ANSI, then take
    everything after the LAST "> " repl prompt.
    """
    text = _ANSI_RE.sub("", raw)
    text = text.replace("\x1b[0m", "").replace("\x1b", "")
    text = re.sub(r"\[0m|\[\?25[hl]|\[\d+[GK]", "", text)
    if ">" in text:
        text = text.rsplit("> ", 1)[-1]
    text = text.strip()
    while "<think>" in text and "<//think>" in text:
        start = text.index("<think>")
        end = text.index("<//think>") + len("<//think>")
        text = (text[:start] + text[end:]).strip()
    return text or "(no response)"


def run_chat(
    prompt: str,
    model: str = DEFAULT_MODEL,
    trust_tools: str | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> tuple[str, int | None, bool]:
    """Run `q chat` non-interactively. Returns (output_text, exit_code, ok)."""
    valid = valid_models()
    if model not in valid:
        return f"invalid model: {model} (valid: {', '.join(valid)})", 2, False

    if trust_tools:
        names = [t.strip() for t in trust_tools.split(",") if t.strip()]
        bad = [
            t
            for t in names
            if t not in NATIVE_TOOLS and not t.startswith("@")
        ]
        if bad:
            return (
                f"invalid trust_tools: {', '.join(bad)} "
                f"(valid native: {', '.join(sorted(NATIVE_TOOLS))}; "
                f"MCP tools use '@server/tool')",
                2,
                False,
            )

    cmd = [_q_bin(), "chat", "--no-interactive", "--model", model]
    if trust_tools:
        cmd += ["--trust-tools", trust_tools]
    cmd.append(prompt)

    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        return out.decode(errors="replace"), None, False
    except FileNotFoundError:
        return "q chat binary not found on PATH", 127, False

    out = (completed.stdout or b"").decode(errors="replace")
    return out, completed.returncode, completed.returncode == 0


def extract_answer(raw: str) -> str:
    """Public wrapper used by tests and the tool handler."""
    return _extract_answer(raw)


def chat(
    prompt: str,
    model: str = DEFAULT_MODEL,
    trust_tools: str | None = None,
) -> str:
    """Hermes-facing chat call. Returns a JSON string with the clean answer."""
    import json

    output, status, ok = run_chat(prompt, model=model, trust_tools=trust_tools)
    if not ok:
        return json.dumps(
            {
                "success": False,
                "error": _extract_answer(output) if output.strip() else "q chat failed",
                "exit_code": status,
                "model": model,
            }
        )
    return json.dumps(
        {"success": True, "model": model, "answer": _extract_answer(output)}
    )
