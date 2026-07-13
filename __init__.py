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
except ImportError:
    from auth import get_status, logout, show_identity, start_login

# Available models for AWS Builder ID / Amazon Q Developer
AVAILABLE_MODELS = [
    "claude-sonnet-4",
    "claude-sonnet-4.5",
    "claude-haiku-4.5",
]

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
