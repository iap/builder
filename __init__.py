"""build plugin — Amazon BID (Build ID) device login for Hermes Agent.

Exposes headless SSO-OIDC device authorization so the agent can start an
Amazon BID login, report the user_code + verification URL, poll for the token
in the background, and report auth status / identity / logout. No AWS
credentials required on the client. All secrets live under HERMES_HOME (chmod
600); tool handlers never return them.
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from .auth import get_status, logout, show_identity, start_login
    from .chat import chat as _chat, valid_models, DEFAULT_MODEL
except ImportError:
    from auth import get_status, logout, show_identity, start_login
    from chat import chat as _chat, valid_models, DEFAULT_MODEL

# Models are discovered live from `q chat --model help` (server-driven catalog
# that drifts) — expose the current set for the `models` tool and the
# aws_chat schema enum.
AVAILABLE_MODELS = list(valid_models())

logger = logging.getLogger(__name__)


def _success(data: dict[str, Any]) -> str:
    return json.dumps({"success": True, **data})


def _error(message: str, code: str = "error") -> str:
    return json.dumps({"success": False, "error": message, "code": code})


def _check_available() -> bool:
    try:
        import boto3  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _handle_bid_login(args: dict[str, Any], **kwargs: Any) -> str:
    """Start an Amazon BID (Build ID) device login.

    Returns the user_code and verification URL for the human to approve in
    their browser (e.g. Brave, already signed in to their Google account).
    The plugin polls for the token in the background; check bid_status.
    """
    try:
        info = start_login()
        return _success(
            {
                "message": (
                    "Open the verification URL in your browser and enter the "
                    "user_code to approve. The agent polls in the background; "
                    "call bid_status to check completion."
                ),
                **info,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("bid_login failed")
        return _error(str(exc), code="login_failed")


def _handle_bid_status(args: dict[str, Any], **kwargs: Any) -> str:
    """Return the current Amazon BID auth / flow state."""
    try:
        return _success(get_status())
    except Exception as exc:  # noqa: BLE001
        logger.exception("bid_status failed")
        return _error(str(exc), code="status_failed")


def _handle_bid_show_identity(args: dict[str, Any], **kwargs: Any) -> str:
    """Return Amazon BID token identity metadata (no raw token)."""
    try:
        return _success(show_identity())
    except Exception as exc:  # noqa: BLE001
        logger.exception("bid_show_identity failed")
        return _error(str(exc), code="identity_failed")


def _handle_bid_logout(args: dict[str, Any], **kwargs: Any) -> str:
    """Log out: stop polling and delete stored secrets."""
    try:
        logout()
        return _success({"message": "Logged out; secrets cleared."})
    except Exception as exc:  # noqa: BLE001
        logger.exception("bid_logout failed")
        return _error(str(exc), code="logout_failed")


def _handle_bid_models(args: dict[str, Any], **kwargs: Any) -> str:
    """List available AWS Builder ID models."""
    return _success({"models": AVAILABLE_MODELS})


def _handle_aws_chat(args: dict[str, Any], **kwargs: Any) -> str:
    """Chat with AWS Build (Amazon Q Developer) directly via the `q chat` CLI.

    Drives `q chat --no-interactive --model <m> <prompt>` natively and returns
    the cleaned answer. Optional `trust_tools` maps to `q chat --trust-tools`
    so the model may run allowed tools (e.g. fs_read,fs_write) during the turn.
    """
    prompt = (args or {}).get("prompt")
    if not prompt or not str(prompt).strip():
        return _error("`prompt` is required", code="missing_prompt")
    model = (args or {}).get("model") or DEFAULT_MODEL
    trust_tools = (args or {}).get("trust_tools") or None
    try:
        return _chat(prompt, model=model, trust_tools=trust_tools)
    except Exception as exc:  # noqa: BLE001
        logger.exception("aws_chat failed")
        return _error(str(exc), code="chat_failed")


_TOOLS = (
    (
        "bid_login",
        {
            "name": "bid_login",
            "description": (
                "Start an Amazon BID (Build ID) device login. Returns a "
                "user_code and verification URL to approve in your browser "
                "(Brave, already signed in to Google). Polling runs in the "
                "background; use bid_status to check completion."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_bid_login,
        _check_available,
        "🔐",
    ),
    (
        "bid_status",
        {
            "name": "bid_status",
            "description": (
                "Return current Amazon BID device-login / auth state: phase, "
                "user_code, verification URL, authenticated flag, and expiry."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_bid_status,
        _check_available,
        "📊",
    ),
    (
        "bid_show_identity",
        {
            "name": "bid_show_identity",
            "description": (
                "Return Amazon BID token identity metadata (type, scopes, "
                "expiry, has_refresh_token) without exposing the raw token."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_bid_show_identity,
        _check_available,
        "🪪",
    ),
    (
        "bid_logout",
        {
            "name": "bid_logout",
            "description": "Log out of Amazon BID: stop polling and delete stored secrets.",
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_bid_logout,
        _check_available,
        "🚪",
    ),
    (
        "aws_chat",
        {
            "name": "aws_chat",
            "description": (
                "Chat with AWS Build (Amazon Q Developer) using the public "
                "`q chat` CLI directly. Returns the cleaned model answer. "
                "Supports `model` (claude-sonnet-4.5, claude-sonnet-4, "
                "claude-haiku-4.5) and optional `trust_tools` (comma-separated "
                "q tool names, e.g. fs_read,fs_write) to let the model run "
                "allowed tools during the turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The question or instruction for AWS Build.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model id (default claude-sonnet-4.5).",
                        "enum": list(AVAILABLE_MODELS),
                    },
                    "trust_tools": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated q chat tool names to "
                            "trust for this turn (e.g. 'fs_read,fs_write')."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
        _handle_aws_chat,
        lambda: True,
        "💬",
    ),
    (
        "models",
        {
            "name": "models",
            "description": "List available AWS Builder ID models (Claude variants).",
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_bid_models,
        lambda: True,
        "📋",
    ),
)


def register(ctx) -> None:
    """Register all build plugin tools."""
    for name, schema, handler, check_fn, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="aws-build",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            emoji=emoji,
        )
