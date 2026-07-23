# SPDX-License-Identifier: MIT OR Apache-2.0
"""Amazon BID (Builder ID) auth package.

Re-exports the headless SSO-OIDC device-authorization library so plugin code
and tests can `from .auth import start_login, get_status, ...`.
"""

from .sso_oidc import (  # noqa: F401
    start_login,
    get_status,
    logout,
    show_identity,
    refresh_token,
    ensure_valid,
)

__all__ = [
    "start_login",
    "get_status",
    "logout",
    "show_identity",
    "refresh_token",
    "ensure_valid",
]
