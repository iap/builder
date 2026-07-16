"""AWS Build plugin — Amazon Q Developer for Hermes Agent (binary-free).

Two capabilities, both pure-Python (no `amazon-q-developer-cli` build):

1. Amazon BID (Build ID) device login — headless SSO-OIDC so the agent can
   start a login, report the user_code + verification URL, poll for the token
   in the background, and report auth status / identity / logout. No AWS
   credentials required on the client.

2. Chat provider — AWS Build is exposed to Hermes as the `aws-build` custom
   provider (config.yaml) -> the auto-started OpenAI-compatible bridge on
   :8088 -> pure-Python `q_direct` (Bearer Builder ID token). All secrets live
   under HERMES_HOME (chmod 600); tool handlers never return them.
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from .auth import get_status, logout, show_identity, start_login
    from .q_direct import list_models
except ImportError:
    # Allow loading as a flat module under pytest (conftest.py puts the
    # plugin dir on sys.path without a package parent).
    from auth import get_status, logout, show_identity, start_login
    from q_direct import list_models

# Model catalog sourced from the direct backend (q_direct.list_models), which
# serves a static catalog matching config.yaml aws-build.models. No `q chat
# --model help` subprocess probing needed.
AVAILABLE_MODELS = list(list_models())

logger = logging.getLogger(__name__)


def ensure_bridge(host: str = "127.0.0.1", port: int = 8088) -> None:
    """Auto-start the OpenAI-compatible bridge if it isn't already listening.

    Lets AWS Build work with no manual `python3 amazon_q_bridge.py` launch:
    Hermes calls ``register()`` on plugin load, which spawns the bridge once
    (it's a server — only start if the port is free). Pure-Python ``direct``
    backend, so no ``q`` CLI binary is required. No Hermes-core change.

    Failures are non-fatal: if the bridge can't start, chat requests will fail
    with a clear upstream error rather than crashing plugin registration.
    """
    import os
    import socket
    import subprocess
    import sys

    # Already listening? Leave it alone (don't double-spawn a server).
    try:
        with socket.create_connection((host, port), timeout=1):
            logger.info("aws-build bridge already listening on %s:%s", host, port)
            return
    except OSError:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    bridge = os.path.join(here, "amazon_q_bridge.py")
    if not os.path.exists(bridge):
        logger.warning("aws-build bridge not found at %s; skipping auto-start", bridge)
        return

    try:
        # Detached so it survives this process and isn't reaped on exit.
        subprocess.Popen(
            [sys.executable, bridge, "--host", host, "--port", str(port)],
            env={**os.environ, "AMAZON_Q_BACKEND": "direct"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("aws-build bridge auto-started on %s:%s (direct backend)", host, port)
    except Exception as exc:  # noqa: BLE001
        logger.warning("aws-build bridge auto-start failed: %s", exc)


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
    """List available AWS Build models."""
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
            "description": "List available AWS Build models (Claude variants).",
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_bid_models,
        lambda: True,
        "📋",
    ),
)


def register(ctx) -> None:
    """Register all build plugin tools (and auto-start the bridge)."""
    # Auto-start the OpenAI-compatible bridge so AWS Build works without a
    # manual launch — no Hermes-core fork, pure-Python direct backend.
    ensure_bridge()
    for name, schema, handler, check_fn, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="aws-build",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            emoji=emoji,
        )
