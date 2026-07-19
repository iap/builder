"""AWS Build plugin — Amazon Q Developer for Hermes Agent (direct HTTPS chat).

Hermes drives the agentic loop. This plugin exposes Q as a single tool:
`ask_q(prompt)` → calls backend.chat() and returns the answer.

Auth tools (bid_login / bid_status / bid_show_identity / bid_logout) and
model/tag listing are also registered.
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from .auth import get_status, logout, show_identity, start_login
    from .backend import chat, invalidate_q_token, list_models, load_tags
except ImportError:
    from auth import get_status, logout, show_identity, start_login
    from backend import chat, invalidate_q_token, list_models, load_tags

logger = logging.getLogger(__name__)


def _success(data: dict[str, Any]) -> str:
    return json.dumps({"success": True, **data})


def _error(message: str, code: str = "error") -> str:
    return json.dumps({"success": False, "error": message, "code": code})


def _check_available() -> bool:
    try:
        from .auth import get_status  # noqa: F401
        return True
    except ImportError:
        try:
            from auth import get_status  # noqa: F401
            return True
        except ImportError:
            return False


# --- tool handlers ---

def _handle_ask_q(args: dict[str, Any], **kwargs: Any) -> str:
    """Send a prompt to AWS Build (Q) and return the answer."""
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("ask_q failed")
        return _error(str(exc), code="chat_failed")


def _handle_bid_login(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        # Drop any stale .q_token.json so the new login is the sole token store.
        invalidate_q_token()
        info = start_login()
        return _success({
            "message": (
                "Open the verification URL in your browser and enter the "
                "user_code to approve. Call bid_status to check completion."
            ),
            **info,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("bid_login failed")
        return _error(str(exc), code="login_failed")


def _handle_bid_status(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        return _success(get_status())
    except Exception as exc:  # noqa: BLE001
        logger.exception("bid_status failed")
        return _error(str(exc), code="status_failed")


def _handle_bid_show_identity(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        return _success(show_identity())
    except Exception as exc:  # noqa: BLE001
        logger.exception("bid_show_identity failed")
        return _error(str(exc), code="identity_failed")


def _handle_bid_logout(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        logout()
        # Clear any stale .q_token.json alongside the BID mirror files.
        invalidate_q_token()
        return _success({"message": "Logged out; secrets cleared."})
    except Exception as exc:  # noqa: BLE001
        logger.exception("bid_logout failed")
        return _error(str(exc), code="logout_failed")


def _handle_bid_models(args: dict[str, Any], **kwargs: Any) -> str:
    return _success({"models": list_models(), "tags": load_tags()})


def _handle_tags(args: dict[str, Any], **kwargs: Any) -> str:
    return _success({"tags": load_tags()})


# --- tool registry ---

_TOOLS = (
    (
        "ask_q",
        {
            "name": "ask_q",
            "description": (
                "Send a prompt to AWS Build (Amazon Q / Claude) and return the answer. "
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
                        "enum": ["auto", *list_models()],
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
                "Start an Amazon BID (Build ID) device login. Returns a "
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
            "description": "List available AWS Build models (Claude variants) and plugin tags.",
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
            "description": "List free-form tags describing the AWS Build plugin (aws, amazon-q, claude, chat, builder-id, auth).",
            "parameters": {"type": "object", "properties": {}},
        },
        _handle_tags,
        lambda: True,
        "🏷️",
    ),
)


def register(ctx) -> None:
    """Register all aws-build plugin tools + start the OpenAI adapter.

    The adapter lets aws-build be a *selectable chat model* in the Hermes
    TUI/CLI (Way A): it speaks OpenAI's /v1/chat/completions wire
    format on the Hermes side and translates to Q via backend.chat(). It is
    launched as a daemon background thread here (dies with the Hermes
    session) — NOT the old standalone `:8088` bridge. If it fails to bind
    we log and continue; the ask_q tool still works tool-only.
    """
    for name, schema, handler, check_fn, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="aws-build",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            emoji=emoji,
        )
    # Best-effort: start the local OpenAI-compatible adapter so Hermes can
    # route chat turns to aws-build as a model. No-op if already running.
    try:
        from . import adapter  # package import
    except ImportError:  # __main__ / direct
        import adapter  # type: ignore
    port = int(__import__("os").environ.get("AWS_BUILD_ADAPTER_PORT", "8077"))
    try:
        srv, actual = adapter.start(port=port)
        print(f"[aws-build] OpenAI adapter listening on :{actual} (model-provider mode)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("aws-build adapter failed to start (tool-only mode OK): %s", exc)
