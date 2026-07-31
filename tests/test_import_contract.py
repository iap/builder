"""Import / public-contract regression guard.

The 2026-07 break was a *one-sided refactor*: callers in ``__init__.py`` /
``backend.py`` imported symbols (``BID_TOKEN_FILE``, ``_load_tokens``,
``device_code_flow``, ...) that the token-store authority ``auth/sso_oidc.py``
no longer defined. That surfaced only as a hard ``ImportError`` at chat time
and zero collected tests — never as a clear failure.

This test pins the public contract so the same class of break fails loudly at
collection time (and therefore in CI, which runs ``pytest``). It is additive
and intentionally cheap: it asserts the package imports and exposes the
symbols the rest of the suite + ``verify.py`` depend on.

When adding a tool: register it in ``__init__.py`` ``_TOOLS`` AND add its
handler to ``PUBLIC_SYMBOLS`` below so the guard stays meaningful.
"""

from conftest import load_plugin


PUBLIC_SYMBOLS = (
    "register",
    "unregister",
    "_success",
    "_error",
    "_handle_ask_q",
    "_handle_bid_login",
    "_handle_bid_status",
    "_handle_bid_show_identity",
    "_handle_bid_logout",
    "_handle_bid_models",
    "_handle_tags",
    "_handle_q_debug",
)

# The token-store authority (auth/sso_oidc) is the single source of truth for
# the auth API. Callers must not import anything it does not define.
SSO_OIDC_SYMBOLS = (
    "get_status",
    "show_identity",
    "start_login",
    "logout",
    "_load_token",
    "refresh_token",
)


def test_package_imports_and_exposes_contract():
    """The plugin must import and expose its public tool surface."""
    mod = load_plugin()
    missing = [name for name in PUBLIC_SYMBOLS if not hasattr(mod, name)]
    assert not missing, f"missing public symbols: {missing}"


def test_sso_oidc_token_store_contract():
    """auth/sso_oidc must expose the API its callers rely on."""
    try:
        from .auth import sso_oidc
    except ImportError:  # __main__ / direct execution
        from auth import sso_oidc
    missing = [name for name in SSO_OIDC_SYMBOLS if not hasattr(sso_oidc, name)]
    assert not missing, f"missing sso_oidc symbols: {missing}"


def test_ask_q_model_enum_has_no_duplicate_auto():
    """The ask_q model enum must include 'auto' exactly once."""
    mod = load_plugin()
    catalog = list(mod.list_models())
    assert "auto" in catalog
    ask_q_tool = next(tool for tool in mod._TOOLS if tool[0] == "ask_q")
    enum = ask_q_tool[1]["parameters"]["properties"]["model"]["enum"]
    assert enum.count("auto") == 1
    assert len(enum) == len(catalog)
