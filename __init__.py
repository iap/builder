"""AWS Builder ID plugin — Amazon Q Developer for Hermes Agent (direct HTTPS chat).
# SPDX-License-Identifier: MIT OR Apache-2.0

Hermes drives the agentic loop. This plugin exposes Q as a single tool:
`ask_q(prompt)` → calls backend.chat() and returns the answer.

Auth tools (bid_login / bid_status / bid_show_identity / bid_logout) and
model/tag listing are also registered.
"""

from __future__ import annotations

import logging
import os
from typing import Any

try:
    from .auth import get_status, logout, show_identity, sso_oidc, start_login
    from .backend import chat, list_models, load_tags
except ImportError:
    from auth import get_status, logout, show_identity, sso_oidc, start_login
    from backend import chat, list_models, load_tags

logger = logging.getLogger(__name__)


def _plugin_pre_tool_call(
    tool_name: str,
    args: dict[str, Any],
    **kwargs: Any,
) -> dict[str, str] | None:
    """Builder plugin guard: blocks dangerous tool calls.

    Registered as a ``pre_tool_call`` hook in ``register()``. Hermes
    core calls ``get_pre_tool_call_block_message()`` before dispatching
    each tool call; the first ``{"action": "block", "message": "..."}``
    return wins and prevents execution.

    This defense-in-depth guard covers (a) future builder tool additions
    and (b) any tool call that Hermes routes through the plugin's hook.
    It is additive to Hermes's global ``approvals.mode`` — it does not
    replace or override it.
    """
    _HERMES_CORE = (
        os.path.expanduser("~/.hermes/hermes-agent"),
        os.path.expanduser("~/.hermes/config.yaml"),
    )
    _DESTRUCTIVE = (
        "rm -rf ",
        "shutil.rmtree",
        "chmod -R",
        ">/dev/sda",
        "mkfs",
        "dd if=",
    )
    _PRIVILEGE = (
        "sudo ",
        "su -",
        "su ",
        "pkexec ",
        "doas ",
    )

    if tool_name == "terminal":
        cmd = (args.get("command") or "").strip()
        for pattern in _DESTRUCTIVE:
            if pattern in cmd:
                return {
                    "action": "block",
                    "message": (
                        "\u26a0 Destructive shell command blocked by builder guard: "
                        f"`{cmd[:200]}`. Set approvals.mode to 'off' in your "
                        "Hermes config to allow auto-approval of non-destructive "
                        "commands, or run this command directly from a terminal."
                    ),
                }
        for pattern in _PRIVILEGE:
            if cmd.startswith(pattern):
                return {
                    "action": "block",
                    "message": (
                        "\u26a0 Privilege escalation blocked by builder guard: "
                        f"`{cmd[:200]}`. Plugin-originated shell commands do "
                        "not support sudo/su. Run such commands directly from "
                        "a terminal session."
                    ),
                }
    elif tool_name in ("write_file", "patch"):
        target = str(args.get("path") or args.get("file") or "")
        for core_path in _HERMES_CORE:
            if target.startswith(core_path):
                return {
                    "action": "block",
                    "message": (
                        "\u26a0 Write to Hermes protected path blocked by builder "
                        f"guard: `{target}`. Modifying Hermes core files may "
                        "break the installation."
                    ),
                }
    return None


def _tool_result_helpers():
    """Return Hermes's house (success, error) serializers with ensure_ascii=False.

    Delegates to ``tools.registry.tool_result`` / ``tool_error`` so plugin
    output is byte-identical to core tools: valid JSON with ``ensure_ascii=False``
    (non-ASCII text like "café" / "—" / CJK is NOT escaped to ``\u0058\u0058\u0058\u0058`` escapes
    in JSON, which corrupts the answer when the TUI renders it verbatim). Relative-first/absolute
    fallback import matches the pattern auth/sso_oidc uses under Hermes core.
    """
    try:
        from tools.registry import tool_error, tool_result  # type: ignore
        return tool_result, tool_error
    except ImportError:  # __main__ / tests where hermes-agent is on sys.path
        pass
    try:
        from registry import tool_error, tool_result  # type: ignore
        return tool_result, tool_error
    except ImportError:
        pass
    # Standalone / test fallback: same JSON contract, no hard dependency on core.
    import json as _json

    def _result(*, success: bool, **payload):
        payload["success"] = success
        return _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _error(message: str, code: str = "error", *, success: bool = False):
        payload = {"error": message, "code": code, "success": success}
        return _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return _result, _error


def _success(data: dict[str, Any]) -> str:
    tool_result, _ = _tool_result_helpers()
    return tool_result(success=True, **data)


def _error(message: str, code: str = "error") -> str:
    _, tool_error = _tool_result_helpers()
    return tool_error(message, code=code, success=False)


def _check_available() -> bool:
    """Placeholder check_fn — always True.

    The real auth/import guard is per-tool: each handler wraps its own
    calls in try/except and surfaces errors cleanly (see
    _handle_ask_q, _handle_bid_status, etc.). Maintaining a separate
    pre-flight check that only tests whether get_status can be imported
    (not whether the user is authenticated) was a false positive gate
    that unnecessarily blocked read-only tools like q_debug and models
    even when the plugin was fully functional."""
    return True


# --- tool handlers ---

def _handle_ask_q(args: dict[str, Any], **kwargs: Any) -> str:
    """Send a prompt to AWS Builder ID (Q) and return the answer."""
    prompt = args.get("prompt", "")
    if not prompt:
        return _error("prompt is required", code="missing_prompt")
    model = args.get("model", "auto")
    conversation_id = args.get("conversation_id")
    try:
        answer, _cid, _tool_use_id = chat(prompt, model=model, conversation_id=conversation_id)
        result: dict[str, Any] = {"answer": answer}
        if _cid:
            result["conversation_id"] = _cid
        return _success(result)
    except Exception as exc:
        logger.exception("ask_q failed")
        return _error(str(exc), code="chat_failed")


def _handle_bid_login(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        # Single token store (auth/sso_oidc auth/bid_token.json). start_login()
        # guards re-auth when already authenticated, so no stale-token
        # cleanup is needed here.
        info = start_login()
        if info.get("already_authenticated"):
            return _success({
                "message": "Already authenticated with Amazon Q. No new login needed.",
                **info,
            })
        return _success({
            "message": (
                "Open the verification URL in your browser and enter the "
                "user_code to approve. Call bid_status to check completion."
            ),
            **info,
        })
    except Exception as exc:
        logger.exception("bid_login failed")
        return _error(str(exc), code="login_failed")


def _handle_bid_status(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        return _success(get_status())
    except Exception as exc:
        logger.exception("bid_status failed")
        return _error(str(exc), code="status_failed")


def _handle_bid_show_identity(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        return _success(show_identity())
    except Exception as exc:
        logger.exception("bid_show_identity failed")
        return _error(str(exc), code="identity_failed")


def _handle_bid_logout(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        # logout() clears the sso mirror (auth/bid_token.json, auth/bid_registration.json, auth/bid_flow.json).
        logout()
        return _success({"message": "Logged out; secrets cleared."})
    except Exception as exc:
        logger.exception("bid_logout failed")
        return _error(str(exc), code="logout_failed")


def _handle_bid_models(args: dict[str, Any], **kwargs: Any) -> str:
    return _success({"models": list_models(), "tags": load_tags()})


def _handle_tags(args: dict[str, Any], **kwargs: Any) -> str:
    return _success({"tags": load_tags()})


def _handle_q_debug(args: dict[str, Any], **kwargs: Any) -> str:
    """Lightweight calibration/debug snapshot for Hermes TUI/CLI tuning.

    Returns auth/model metadata plus the active host render prefs (mode/theme)
    so a Q-backed agent can self-adapt its output to the running Hermes CLI/TUI
    without manual calibration. No raw token, no client secret.
    """
    try:
        status = get_status()
    except Exception as exc:
        logger.exception("q_debug status failed")
        return _error(str(exc), code="status_failed")
    try:
        identity = show_identity()
    except Exception as exc:
        logger.exception("q_debug identity failed")
        return _error(str(exc), code="identity_failed")
    try:
        from . import _format  # package import
    except ImportError:  # __main__ / direct
        import _format  # type: ignore
    prefs = _format.load_render_prefs()
    payload = {
        "auth": {
            "authenticated": bool(status.get("authenticated")),
            "phase": status.get("phase"),
            "token_expires_at": status.get("token_expires_at"),
            "refreshed": status.get("refreshed"),
        },
        "identity": {
            "token_type": identity.get("token_type"),
            "has_refresh_token": identity.get("has_refresh_token"),
            "scopes": identity.get("scopes"),
            "expires_at": identity.get("expires_at"),
        },
        "models": list_models(),
        "tags": load_tags(),
        "render": prefs,
    }
    return _success(payload)


# --- tool registry ---

_TOOLS = (
    (
        "ask_q",
        {
            "name": "ask_q",
            "description": (
                "Send a prompt to AWS Builder ID and return the answer. "
                "Hermes drives the agentic loop; Q answers single prompts. "
                "Optionally pass conversation_id to continue a prior Q conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The prompt to send to Q."},
                    "model": {
                        "type": "string",
                        "description": "Model to use; sent to Q as modelId. Defaults to 'auto' (Q picks). Named Claude variants are advertised but the account's entitlement decides which are usable.",
                        "enum": [*list_models()],
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "Optional Q conversation ID for multi-turn context.",
                    },
                },
                "required": ["prompt"],
            },
        },
        _handle_ask_q,
        _check_available,
        "🤖",
    ),
    (
        "bid_login",
        {
            "name": "bid_login",
            "description": (
                "Start an Amazon BID (Builder ID) device login. Returns a "
                "user_code and verification URL to approve in your browser."
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
            "description": "Return current Amazon BID device-login / auth state.",
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
            "description": "Return Amazon BID token identity metadata (no raw token).",
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
            "description": "List available AWS Builder ID models (Claude variants) and plugin tags.",
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_bid_models,
        lambda: True,
        "📋",
    ),
    (
        "tags",
        {
            "name": "tags",
            "description": "List free-form tags describing the AWS Builder ID plugin (aws, amazon-q, claude, chat, builder-id, auth).",
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_tags,
        lambda: True,
        "🏷️",
    ),
    (
        "q_debug",
        {
            "name": "q_debug",
            "description": "Lightweight calibration snapshot: auth state, identity metadata, models, and tags. No raw secrets.",
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_q_debug,
        _check_available,
        "🔬",
    ),
)


def register(ctx) -> None:
    """Register all builder plugin tools + start the OpenAI adapter.

    The adapter lets builder be a *selectable chat model* in the Hermes
    TUI/CLI (Way A): it speaks OpenAI's /v1/chat/completions wire
    format on the Hermes side and translates to Q via backend.chat(). It is
    launched as a daemon background thread here (dies with the Hermes
    session) — the plugin's own in-process OpenAI adapter on :8088
    (NOT a separate standalone server). If it fails to bind
    we log and continue; the ask_q tool still works tool-only.
    """
    # Register the plugin-level tool guard as a pre_tool_call hook.
    # Hermes core calls get_pre_tool_call_block_message() before
    # dispatching each tool call; the first {"action": "block"}
    # return wins and prevents execution.
    ctx.register_hook("pre_tool_call", _plugin_pre_tool_call)

    for name, schema, handler, check_fn, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="builder",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            emoji=emoji,
        )
    # Best-effort: start the local OpenAI-compatible adapter so Hermes can
    # route chat turns to builder as a model. No-op if already running.
    try:
        from . import adapter  # package import
    except ImportError:  # __main__ / direct
        import adapter  # type: ignore
    port = int(__import__("os").environ.get("AWS_BUILD_ADAPTER_PORT", "8088"))
    try:
        srv, actual = adapter.start(port=port)
        print(f"[builder] OpenAI adapter listening on :{actual} (model-provider mode)")
    except OSError as exc:
        # If the adapter is already running (another active Hermes session
        # bound the port), that is healthy — skip the warning. Surface
        # everything else. Probe the port (not just this process's _server)
        # so the warning is suppressed when another session owns it.
        if not adapter.is_running(host=adapter.HOST, port=port):
            logger.warning("builder adapter failed to start (tool-only mode OK): %s", exc)
        _srv, actual = None, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder adapter failed to start (tool-only mode OK): %s", exc)
        _srv, actual = None, None
    # Surface the adapter as a selectable model provider in the dashboard
    # Models picker (see https://github.com/iap/builder/issues/20). Best-effort:
    # if config is unavailable or a user already manages this provider, skip.
    if actual is not None:
        try:
            from . import _provider  # package import
        except ImportError:  # __main__ / direct
            import _provider  # type: ignore
        _provider.register_provider(actual)


def unregister(ctx) -> None:
    """Best-effort teardown: stop the local OpenAI adapter so the :8088
    listener is released immediately (otherwise it lingers until process
    exit). Hermes core does not currently invoke this hook, but defining
    it is the correct plugin contract and makes reinstall/rebind clean.
    """
    try:
        from . import adapter  # package import
    except ImportError:  # __main__ / direct
        import adapter  # type: ignore
    try:
        adapter.stop()
        print("[builder] OpenAI adapter stopped (model-provider mode off)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder adapter stop failed (ignore): %s", exc)
    # Drop the model-provider entry we registered (no-op if user-managed).
    try:
        from . import _provider  # package import
    except ImportError:  # __main__ / direct
        import _provider  # type: ignore
    try:
        _provider.unregister_provider()
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder provider unregister failed (ignore): %s", exc)
