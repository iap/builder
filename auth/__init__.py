"""Amazon BID (Build ID) auth package.

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
    is_available,
)

__all__ = [
    "start_login",
    "get_status",
    "logout",
    "show_identity",
    "refresh_token",
    "ensure_valid",
    "is_available",
]
