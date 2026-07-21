"""Direct AWS Builder ID chat backend.
# SPDX-License-Identifier: MIT OR Apache-2.0

Pure-HTTP calls to AWS Builder ID's chat API, authenticated via an AWS Builder ID
device-login (OAuth RFC 8628). Hermes drives the agentic loop and calls this as
a plain reasoning tool (`ask_q`); the plugin is a direct backend — just
`requests` to Q's HTTPS endpoint, with no subprocess and no local HTTP bridge.

Wire protocol (verified live against Amazon Q's endpoints):

  OIDC (device flow):  https://oidc.us-east-1.amazonaws.com
    register_client  -> client_type="public",
                         scopes=codewhisperer:completions,analysis,conversations,
                         start_url=https://view.awsapps.com/start
    start_device_authorization, create_token (device grant)
  Chat:  POST https://q.us-east-1.amazonaws.com/
    Headers: Content-Type application/x-amz-json-1.0,
             x-amz-target AmazonCodeWhispererStreamingService.GenerateAssistantResponse,
             Authorization: Bearer ***
    Body:    {"conversationState": {"currentMessage": {...},
              "chatTriggerType": "MANUAL"}}
    Auth is Bearer-only (no SigV4; verified live).

Token is persisted locally (gitignored, under HERMES_HOME) so the device-login
survives across restarts and is refreshable.

Token storage
-------------
The plugin owns ONE token store end-to-end: the BID login mirror at
auth/sso_oidc (auth/bid_token.json under HERMES_HOME). backend.chat() is a
pure HTTP client to Q — it NEVER persists a token. get_token()
delegates entirely to sso_oidc, so there is exactly one source of
truth (no dual-store, no split-brain, no "newest wins" resolver).

A from-scratch Builder ID device login in pure Python IS possible: AWS SSO
OIDC exposes plain REST/JSON endpoints (client/register unsigned,
/device_authorization, /token) needing NO SigV4 and NO AWS IAM
credentials — verified live this session. The device flow
(auth/sso_oidc.start_login) registers its own public client.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import requests


def _import_sso_oidc():
    """Import the auth.sso_oidc module robustly regardless of load style.

    Hermes core loads this plugin as a *package* submodule (e.g.
    ``hermes_plugins.builder.backend``) WITHOUT placing the plugin directory
    on the top-level ``sys.path``. In that case a bare ``from auth import
    sso_oidc`` raises ``ModuleNotFoundError: No module named 'auth'`` — which
    used to mask the real "please authenticate" message at chat time (the
    adapter surfaced the import error instead of the token error).

    So try the package-relative import first (works under core's package
    load), then fall back to the absolute import (works when the plugin dir is
    on sys.path, e.g. standalone/tests/verify.py). Mirrors the same
    relative-first/absolute-fallback pattern used in __init__.py.
    """
    try:
        from .auth import sso_oidc  # type: ignore  # package load (core)
        return sso_oidc
    except ImportError:
        from auth import sso_oidc  # type: ignore  # dir-on-path (standalone)
        return sso_oidc


# --- Q endpoints / constants (source-verified) ---
CHAT_HOST = "q.us-east-1.amazonaws.com"
CHAT_URL = f"https://{CHAT_HOST}"
X_AMZ_TARGET = "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"


def get_token() -> dict:
    """Return a valid Builder ID token (delegated to the BID login store).

    The plugin's token store is owned entirely by auth.sso_oidc
    (auth/bid_token.json). On expiry it silently refreshes via the same
    module (so the refreshed token lands back in auth/bid_token.json, never a
    second file). Raises RuntimeError with an actionable message when no
    valid token exists.
    """
    sso_oidc = _import_sso_oidc()

    if sso_oidc.get_status().get("authenticated"):
        tok = sso_oidc._load_token()
        if tok:
            return tok
    # Expired but refreshable -> silent refresh, then re-read.
    if sso_oidc.refresh_token():
        tok = sso_oidc._load_token()
        if tok:
            return tok
    raise RuntimeError(
        "No valid Amazon Q token available. Authenticate via the `bid_login` plugin "
        "tool, which performs the OIDC device flow and writes the token the chat "
        "path reads. Then retry. A refresh is attempted automatically on expiry."
    )


# --- request auth (Bearer only) ---
# Verified live: the CodeWhisperer GenerateAssistantResponse call sends
# `Authorization: Bearer <OIDC accessToken>` with x-amz-target + Content-Type,
# and NO SigV4 signed-headers. The OIDC access_token from the BID device
# login IS the chat bearer. (An earlier dual-auth SigV4 attempt was wrong
# — Q rejected the extra X-Amz-* signed headers.)
def _sign_request(bearer: str) -> dict:
    return {
        "Content-Type": "application/x-amz-json-1.0",
        "Authorization": f"Bearer {bearer}",
        "x-amz-target": X_AMZ_TARGET,
    }


# --- chat ---
def _resolve_model_id(model: Optional[str]) -> str:
    """Map a requested model name to a modelId Q will accept.

    Q returns an opaque HTTP 500 (InternalServerException) for ANY modelId
    outside its supported set — verified live, including plausible typos like
    "claude-sonnet-4-5" (note the dashes) and unrelated names like
    "gpt-4-turbo". Rather than forward an arbitrary string straight through
    (which surfaces that cryptic 500 to the caller / Hermes model UI), coerce
    anything not in our advertised catalog to "auto", which always resolves to
    a usable model.

    Empty/None -> "auto". The catalog is ``list_models()`` (which already
    honors the operator's ``models:`` override in plugin.yaml) plus the special
    "auto" passthrough, so extending the catalog via config keeps that model
    usable without code changes.
    """
    requested = (model or "").strip()
    if not requested:
        return "auto"
    allowed = set(list_models()) | {"auto"}
    if requested in allowed:
        return requested
    import logging

    logging.getLogger(__name__).debug(
        "builder: unknown model %r not in catalog %s; using 'auto' "
        "(Q returns HTTP 500 for unsupported modelId)",
        requested,
        sorted(allowed),
    )
    return "auto"


def chat(
    prompt: str,
    model: str = "auto",
    conversation_id: Optional[str] = None,
    tools: Optional[list] = None,
    tool_results: Optional[list] = None,
    history: Optional[list] = None,
    _retries: int = 0,
) -> tuple[str, Optional[str], Optional[str]]:
    """Send `prompt` to Q's GenerateAssistantResponse and return (answer, conversation_id, tool_use_id).

    `model` is sent to Q as `modelId` in the request body (verified live: Q
    accepts and echoes it, e.g. "claude-sonnet-4.5"). When `model` is omitted or
    empty, `modelId` defaults to "auto" so a Free-tier Builder ID always gets a
    usable response instead of an entitlement error.

    `conversation_id` (optional) links the turn to an existing Q conversation so
    multi-turn context is preserved server-side by Q rather than flattened into
    the prompt. When None, Q starts a new conversation and returns a fresh id
    via the `conversationId` field in the response stream; that id is extracted
    and returned so the caller can thread it through subsequent turns.

    `tools` / `tool_results` (optional) exist for wire-protocol completeness.
    Hermes drives the agentic loop and executes tools itself, so `ask_q` never
    passes these — Q is used as a chat/reasoning endpoint only. They are kept so
    the request-body shape stays faithful to Q's `userInputMessageContext`.

    `history` (optional) is a list of prior ChatMessage objects, used to give Q
    full conversational context across turns.

    NOTE: the OIDC access_token from the BID device login is the chat
    bearer (verified live — no SigV4, no token-exchange). This call reuses
    an existing authenticated session if present, or a fresh token from
    get_token(). If no valid token is available, get_token() raises a clear
    RuntimeError.
    """
    tok = get_token()
    access = tok.get("access_token") or tok.get("accessToken")
    if not access:
        raise RuntimeError("Amazon Q token missing access_token")

    ctx: dict = {}
    if tools:
        ctx["tools"] = tools
    if tool_results:
        ctx["toolResults"] = tool_results
    # `origin` is a required wire-protocol string in Q's request body (not a
    # reference to any local CLI); "CLI" is the value Q's API expects here.
    user_msg: dict = {"content": prompt, "origin": "CLI"}
    if ctx:
        user_msg["userInputMessageContext"] = ctx
    # Send the model to Q as `modelId` (verified live: Q accepts and echoes it).
    # Default to "auto" so a Free-tier Builder ID always gets a usable model
    # rather than an entitlement error on a pinned Pro-only name. Unknown model
    # names are coerced to "auto" because Q returns an opaque HTTP 500
    # (InternalServerException) for any modelId outside its supported set —
    # verified live, including a plausible typo like "claude-sonnet-4-5". See
    # _resolve_model_id.
    model_id = _resolve_model_id(model)
    user_msg["modelId"] = model_id
    body = {
        "conversationState": {
            "currentMessage": {"userInputMessage": user_msg},
            "chatTriggerType": "MANUAL",
        }
    }
    if conversation_id:
        body["conversationState"]["conversationId"] = conversation_id
    if history:
        body["conversationState"]["history"] = history

    payload = json.dumps(body)
    headers = _sign_request(access)
    r = requests.post(
        CHAT_URL,
        data=payload,
        headers=headers,
        timeout=120,
        stream=True,
    )
    if r.status_code != 200:
        err = r.text[:600]
        err_low = err.lower()
        # Entitlement / subscription failures: Q returns a non-200 with an
        # AccessDenied / subscription-style body. Surface it clearly and point
        # at activation — do NOT treat it as a token problem (no refresh here).
        if any(k in err_low for k in ("subscri", "accessdenied", "not.*entitled", "not activat", "free tier", "q developer")):
            raise RuntimeError(
                "Amazon Q rejected the chat request due to entitlement/subscription. "
                "This Builder ID may not have Amazon Q Developer activated. Activate it "
                "(one-time, free) at console.aws.amazon.com/amazonq using 'Sign in with "
                "Builder ID', or retry with model='auto'. Underlying error: " + err[:300]
            )
        # Auth failure (expired/revoked bearer). Attempt a silent refresh and
        # ONE retry before giving up — don't nuke a possibly-valid token on a
        # generic 400, and don't require user interaction. (m1/m3)
        if r.status_code in (400, 401) and "invalid" in err.lower():
            # Bound the refresh-then-retry to a single attempt. After a
            # refresh (which now stamps a fresh expires_at), get_token() will
            # return the valid token; a second 400/401 means the credentials
            # are genuinely rejected, so stop rather than recursing forever.
            if _retries >= 1:
                raise RuntimeError(
                    "Amazon Q rejected the bearer token even after a silent "
                    "refresh. Re-authenticate via the `bid_login` plugin tool."
                )
            # Refresh through sso_oidc (the store owner) — never a second
            # file — so the refreshed token lands in auth/bid_token.json.
            sso_oidc = _import_sso_oidc()

            if sso_oidc.refresh_token():
                return chat(
                    prompt,
                    model=model,
                    conversation_id=conversation_id,
                    _retries=_retries + 1,
                )
            raise RuntimeError(
                "Amazon Q rejected the bearer token (expired/revoked). Re-authenticate "
                "via the `bid_login` plugin tool — it performs the OIDC device flow. "
                "A refresh is attempted automatically on expiry."
            )
        raise RuntimeError(f"Q chat HTTP {r.status_code}: {err}")
    return _extract_answer_with_conversation_id(r)


# Matches the JSON *string* value of a `"content"` key. The value is a properly
# quoted JSON string, so the escape-aware pattern captures it intact — braces,
# brackets, quotes and backslashes inside the assistant text cannot confuse it.
_CONTENT_RE = re.compile(r'"content"\s*:\s*("(?:[^"\\]|\\.)*")')


def _match_brace(text: str, start: int) -> int:
    """Return the index of the `}` matching the `{` at `text[start]`, or len(text).

    String/escape aware (so a `}` or `{` inside the assistant text, including
    unbalanced ones, never breaks the scan). Used only to bound the object that
    carries a `"content"` so we can check it also carries `"modelId"`.
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _extract_answer(response: requests.Response) -> str:
    """Decode Q's AWS event-stream response and return the assistant text.

    Thin wrapper over `_extract_answer_with_conversation_id` (the canonical
    parser) that discards the conversation/tool-use ids. Kept for the tests and
    any caller that only needs the text.
    """
    answer, _cid, _tool_use_id = _extract_answer_with_conversation_id(response)
    return answer


def _extract_conversation_id(text: str) -> Optional[str]:
    """Pull Q's `conversationId` from the response stream.

    The `assistantResponseEvent` payload carries both `content`/`modelId` and a
    `conversationId` that links the turn to Q's server-side conversation. We
    reuse the brace-aware scanner to grab it from the first assistant event that
    has one. Returns None when absent (e.g. a single-shot, non-conversational
    response).
    """
    for m in _CONTENT_RE.finditer(text):
        obj_start = text.rfind("{", 0, m.start())
        if obj_start == -1:
            continue
        obj_end = _match_brace(text, obj_start)
        obj = text[obj_start : obj_end + 1]
        if "modelId" not in obj:
            continue
        cid = re.search(r'"conversationId"\s*:\s*("(?:[^"\\]|\\.)*"|\S+)', obj)
        if cid:
            val = cid.group(1)
            if val.startswith('"'):
                try:
                    return json.loads(val)
                except Exception:
                    return val.strip('"')
            return val
    return None


def _extract_tool_use_id(text: str) -> Optional[str]:
    """Pull Q's `toolUseId` from a `toolUseEvent` in the response stream.

    Unlike `assistantResponseEvent` (which carries `modelId`), the `toolUseEvent`
    carries `toolUseId`/`name`/`input` and no `modelId`, so the modelId-gated
    scanner misses it. We match the `toolUseId` JSON string directly. Returns
    None when absent (e.g. a plain chat turn with no tool call).
    """
    m = re.search(r'"toolUseId"\s*:\s*("(?:[^"\\]|\\.)*")', text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return m.group(1).strip('"')
    return None


def _extract_answer_with_conversation_id(response: requests.Response) -> tuple[str, Optional[str], Optional[str]]:
    """Like `_extract_answer`, but also returns Q's `conversationId` and `toolUseId`."""
    raw = b""
    for chunk in response.iter_content(chunk_size=4096):
        raw += chunk
    text = raw.decode("utf-8", "replace")
    parts: list[str] = []
    for m in _CONTENT_RE.finditer(text):
        obj_start = text.rfind("{", 0, m.start())
        if obj_start == -1:
            continue
        obj_end = _match_brace(text, obj_start)
        if "modelId" not in text[obj_start : obj_end + 1]:
            continue
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            continue
    answer = "".join(parts).strip()
    if not answer:
        err = re.search(r'"__type"\s*:\s*"([^"]+)"', text)
        if err:
            answer = f"(Q error: {err.group(1)})"
        else:
            answer = "(no response)"
    return answer, _extract_conversation_id(text), _extract_tool_use_id(text)


# Static catalog — single source of truth for the served model list.
# A dedicated live ListAvailableModels Smithy API exists, but its X-Amz-Target
# prefix lives in the aws-smithy runtime and is not derivable without the
# service model (live probes return 404). So we keep this static list and treat
# any future live fetch as a best-effort override.
STATIC_MODELS = [
    "claude-haiku-4.5",
    "claude-sonnet-4",
    "claude-sonnet-4.5",
]

_PLUGIN_YAML = Path(__file__).resolve().parent / "plugin.yaml"
_MODEL_OVERRIDE: Optional[list[str]] = None  # None = not yet loaded


def _load_model_override() -> Optional[list[str]]:
    """Read an optional `models:` list from plugin.yaml.

    Returns the list if present and non-empty, otherwise None so the caller
    falls back to STATIC_MODELS. Missing pyyaml or file is treated as "no
    override" rather than an error.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        with open(_PLUGIN_YAML, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, ValueError):
        return None
    models = data.get("models")
    if isinstance(models, list) and models:
        return [str(m) for m in models]
    return None


def list_models() -> list[str]:
    """Return available AWS Builder ID models.

    Resolution order:
      1. `models:` override in plugin.yaml (operator-editable, no code change).
      2. Built-in STATIC_MODELS fallback.

    The override is loaded lazily and cached on first call, so editing
    plugin.yaml is picked up on the next call without restarting Hermes. A
    genuine live ListAvailableModels call is not wired because its Smithy
    X-Amz-Target prefix lives in the aws-smithy runtime and is not derivable
    without the service model (live probes 404).
    """
    global _MODEL_OVERRIDE
    if _MODEL_OVERRIDE is None:
        _MODEL_OVERRIDE = _load_model_override()
    return list(_MODEL_OVERRIDE if _MODEL_OVERRIDE else STATIC_MODELS)


STATIC_TAGS = [
    "aws",
    "amazon-q",
    "claude",
    "chat",
    "builder-id",
    "auth",
]

_TAG_OVERRIDE: Optional[list[str]] = None  # None = not yet loaded


def _load_tag_override() -> Optional[list[str]]:
    """Read an optional `tags:` list from plugin.yaml.

    Returns the list if present and non-empty, otherwise None so the caller
    falls back to STATIC_TAGS. Missing pyyaml or file is treated as "no
    override" rather than an error.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        with open(_PLUGIN_YAML, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, ValueError):
        return None
    tags = data.get("tags")
    if isinstance(tags, list) and tags:
        return [str(t) for t in tags]
    return None


def load_tags() -> list[str]:
    """Return free-form tags describing this plugin.

    Resolution order:
      1. `tags:` override in plugin.yaml (operator-editable).
      2. Built-in STATIC_TAGS fallback.

    The override is loaded lazily and cached on first call, so editing
    plugin.yaml is picked up on the next call without restarting Hermes.
    """
    global _TAG_OVERRIDE
    if _TAG_OVERRIDE is None:
        _TAG_OVERRIDE = _load_tag_override()
    return list(_TAG_OVERRIDE if _TAG_OVERRIDE else STATIC_TAGS)


if __name__ == "__main__":
    import sys

    p = sys.argv[1] if len(sys.argv) > 1 else "reply with exactly: DIRECT_OK"
    answer, _cid, _tool_use_id = chat(p)
    print(answer)
