# SPDX-License-Identifier: MIT OR Apache-2.0
"""Amazon BID (Builder ID) auth package.

Re-exports the headless SSO-OIDC device-authorization library so plugin code
and tests can `from .auth import start_login, get_status, ...`.
"""

from .sso_oidc import (
    ensure_valid,
    get_status,
    logout,
    refresh_token,
    show_identity,
    start_login,
)

__all__ = [
    "ensure_valid",
    "get_status",
    "logout",
    "refresh_token",
    "show_identity",
    "start_login",
]
