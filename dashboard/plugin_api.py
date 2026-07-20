"""Dashboard backend for the builder plugin.

Exposes the Amazon Builder ID (BID) device-login flow over the Hermes
plugin API surface, mounted at ``/api/plugins/builder/...``. Reuses the
plugin's own ``sso_oidc`` module so the auth state lives in one place (no core
files touched). The plugin's tools (bid_login / bid_status / bid_logout) call
the *same* functions, so the dashboard and the conversation stay in sync.

The flow is RFC 8628: a ``POST /login`` starts the device authorization and
returns the user_code + verification URL for the human to approve in their own
browser. ``GET /status`` actively polls an in-flight flow (get_status does the
poll), so the card reflects authentication the moment the user approves — even
if the flow was started from a different process.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_plugin_package() -> str:
    """Make the builder plugin importable as ``hermes_plugins.builder``.

    The dashboard backend is mounted by the web server as a standalone module,
    so a bare ``from auth.sso_oidc import ...`` fails. We register the plugin
    package the same way the agent plugin loader does (``hermes_plugins.builder``)
    so the backend reuses the same functions (and the same on-disk token/flow
    files) as the agent tools — one auth state, no drift.
    """
    root = Path(__file__).resolve().parent.parent  # plugin root under HERMES_HOME/plugins/builder
    ns = "hermes_plugins"
    if ns not in sys.modules:
        ns_pkg = types.ModuleType(ns)
        ns_pkg.__path__ = []
        ns_pkg.__package__ = ns
        sys.modules[ns] = ns_pkg
    pkg_name = "hermes_plugins.builder"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(root)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
    return pkg_name


def _sso():
    """Return the shared sso_oidc module (no heavy imports at module load)."""
    _ensure_plugin_package()
    from hermes_plugins.builder.auth import sso_oidc

    return sso_oidc


@router.get("/status")
async def status() -> dict[str, Any]:
    """Return current device-login / auth state (actively polls a pending flow)."""
    try:
        return _sso().get_status()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("builder status failed", exc_info=True)
        return {"authenticated": False, "phase": "error", "error": str(exc)}


@router.post("/login")
async def login() -> dict[str, Any]:
    """Start the Builder ID device flow; returns user_code + verification URL."""
    try:
        info = _sso().start_login()
        return {
            "success": True,
            "user_code": info.get("user_code"),
            "verification_uri": info.get("verification_uri"),
            "verification_uri_complete": info.get("verification_uri_complete"),
            "expires_in": info.get("expires_in"),
            "interval": info.get("interval"),
            "message": (
                "Open the verification URL in your browser and enter the "
                "user_code to approve. The card polls automatically."
            ),
        }
    except Exception as exc:
        logger.exception("builder dashboard login failed")
        return {"success": False, "error": str(exc), "code": "login_failed"}


@router.post("/logout")
async def logout() -> dict[str, Any]:
    """Stop polling and delete all stored secrets (local mirror files)."""
    try:
        _sso().logout()
        return {"success": True, "message": "Logged out; secrets cleared."}
    except Exception as exc:
        logger.exception("builder dashboard logout failed")
        return {"success": False, "error": str(exc), "code": "logout_failed"}
