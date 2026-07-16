"""Direct Amazon Q Developer chat backend — no `amazon-q-developer-cli` binary.

This replaces the `q chat` subprocess with pure-HTTP calls to Amazon Q's
chat API, authenticated via an AWS Builder ID device-login (OAuth RFC 8628).
It is the "no-build" path: Hermes can use Claude-via-Q without compiling the
Rust CLI.

Wire protocol reverse-engineered from the `amazon-q-developer-cli` source
(file:line references in comments):

  OIDC (device flow):  https://oidc.us-east-1.amazonaws.com
    register_client  -> client_name="Amazon Q Developer for command line",
                         client_type="public",
                         scopes=codewhisperer:completions,analysis,conversations,
                         start_url=https://view.awsapps.com/start
                         (chat-cli/src/auth/consts.rs, constants.rs)
    start_device_authorization, create_token (device grant)
  Chat:  POST https://q.us-east-1.amazonaws.com/
    Headers: Content-Type application/x-amz-json-1.0,
             x-amz-target AmazonCodeWhispererStreamingService.GenerateAssistantResponse,
             Authorization: Bearer <access_token>
    Body:    {"conversationState": {"currentMessage": {...}, "chatTriggerType":"CHAT"},
              "profileArn"?, "agentMode"?}
    (operation/generate_assistant_response.rs:241 x-amz-target;
     protocol_serde/shape_conversation_state.rs body shape)
    SigV4 signed (bearer kept in Authorization; signature in X-Amz-*).

Token is persisted locally (not in git / not in ~/.hermes/.env) so the
device-login survives across bridge restarts and is refreshable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import datetime
from pathlib import Path
from typing import Optional

import requests

# --- Q endpoints / constants (source-verified) ---
OIDC_URL = "https://oidc.us-east-1.amazonaws.com"
CHAT_HOST = "q.us-east-1.amazonaws.com"
CHAT_URL = f"https://{CHAT_HOST}"
REGION = "us-east-1"
SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
]
START_URL = "https://view.awsapps.com/start"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT = "refresh_token"
X_AMZ_TARGET = "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"

# AWS's published public OIDC client for Amazon Q Builder ID. `q` (chat-cli) uses
# this same client for the device flow. It is a public client credential (not a
# private secret) — equivalent to what the `q` binary embeds. Verified live this
# session: /device_authorization + /token succeed with it, no AWS IAM creds.
CLIENT_ID = "Du6YHbT0KZM9waiS4jCtznVzLWVhc3QtMQ"
# The client secret is loaded from a gitignored local file so it isn't committed.
_CLIENT_SECRET_FILE = Path(__file__).resolve().parent / "auth" / "oidc_client_secret.json"


def _load_oidc_secret() -> str:
    if _CLIENT_SECRET_FILE.exists():
        try:
            return json.loads(_CLIENT_SECRET_FILE.read_text()).get("clientSecret", "")
        except Exception:
            pass
    # Fallback: empty — a real secret must be present in the gitignored
    # auth/oidc_client_secret.json (captured from `q`, or your own). We do NOT
    # embed a fake/truncated value here.
    return ""


TOKEN_FILE = Path(__file__).resolve().parent / ".q_token.json"


# --- token storage ---
# Q's own authenticated session is cached here by the `q` CLI. We reuse it so the
# direct backend can call the chat API without the CLI binary. A from-scratch
# Builder ID device login in pure Python IS possible: AWS SSO OIDC exposes plain
# REST/JSON endpoints (/client/register unsigned, /device_authorization, /token)
# that need NO SigV4 and NO AWS IAM credentials — verified live this session.
# The `q` CLI uses AWS's published public OIDC client (CLIENT_ID below).
Q_SQLITE = Path.home() / "Library" / "Application Support" / "amazon-q" / "data.sqlite3"


def _load_token() -> Optional[dict]:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text())
        except Exception:
            return None
    return None


def _load_q_sqlite_token() -> Optional[dict]:
    """Reuse Q's existing authenticated session (no binary needed for chat)."""
    if not Q_SQLITE.exists():
        return None
    try:
        import sqlite3

        db = sqlite3.connect(str(Q_SQLITE))
        row = db.execute(
            "SELECT value FROM auth_kv WHERE key='codewhisperer:odic:token'"
        ).fetchone()
        if not row:
            return None
        tok = json.loads(row[0])
        # normalize key names to what chat() expects
        tok.setdefault("access_token", tok.get("accessToken"))
        tok.setdefault("refresh_token", tok.get("refreshToken"))
        tok.setdefault("region", tok.get("region", REGION))
        return tok
    except Exception:
        return None


def _save_token(tok: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(tok, indent=2))
    os.chmod(TOKEN_FILE, 0o600)


def _token_expired(tok: dict, skew: int = 120) -> bool:
    exp = tok.get("expires_at") or tok.get("expiresAt")
    if not exp:
        return True
    if isinstance(exp, (int, float)):
        return time.time() + skew >= exp
    try:
        import datetime

        dt = datetime.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        return time.time() + skew >= dt.timestamp()
    except Exception:
        return True


# --- device login ---
def device_login() -> dict:
    """Run the Builder ID device flow. Returns the token dict and prints the
    user-code / verification URL for the human to approve in a browser.

    Steps (chat-cli/src/auth/builder_id.rs):
      1. register_client
      2. start_device_authorization
      3. poll create_token until approved
    """
    sess = requests.Session()

    # 1) Use AWS's published public OIDC client (the one `q` uses). A freshly
    #    registered client is rejected by /device_authorization (invalid_client),
    #    so we reuse the known public client instead of self-registering.
    client_id = CLIENT_ID
    client_secret = _load_oidc_secret()
    if not client_secret:
        raise RuntimeError(
            "OIDC client secret missing — write it to auth/oidc_client_secret.json "
            "(captured from `q`, or your own)."
        )

    # 2) start device authorization
    da = sess.post(
        f"{OIDC_URL}/device_authorization",
        headers={"Content-Type": "application/json"},
        json={
            "clientId": client_id,
            "clientSecret": client_secret,
            "startUrl": START_URL,
        },
        timeout=30,
    ).json()
    print("── Amazon Q Builder ID device login ──")
    print("Open this URL and enter the code:")
    print(f"  URL : {da.get('verificationUriComplete') or da.get('verification_uri_complete') or da.get('verificationUri')}")
    print(f"  CODE: {da.get('userCode') or da.get('user_code')}")
    print("──────────────────────────────────────")

    # 3) poll for the token
    if not (da.get("verificationUriComplete") or da.get("verificationUri")):
        raise RuntimeError(f"device_authorization failed: {da}")
    interval = max(int(da.get("interval") or 5), 1)
    expires_in = int(da.get("expiresIn") or 600)
    device_code = da.get("deviceCode") or da.get("device_code")
    if not device_code:
        raise RuntimeError(f"device_authorization response missing deviceCode: {da}")
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        r = sess.post(
            f"{OIDC_URL}/token",
            headers={"Content-Type": "application/json"},
            json={
                "grantType": DEVICE_GRANT,
                "clientId": client_id,
                "clientSecret": client_secret,
                "deviceCode": device_code,
            },
            timeout=30,
        )
        if r.status_code == 200:
            tok = r.json()
            # Stamp an absolute expiry so _token_expired()/get_token() treat the
            # fresh token as valid (the API returns relative expiresIn only).
            exp_in = int(tok.get("expiresIn") or 3600)
            tok["expires_at"] = int(time.time()) + exp_in
            tok.update(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "region": REGION,
                    "start_url": START_URL,
                    "scopes": SCOPES,
                }
            )
            _save_token(tok)
            print("Login successful.")
            return tok
        # 400 pending / slow_down -> keep polling
        try:
            err = r.json().get("error", "")
        except ValueError:
            err = ""
        if err not in ("authorization_pending", "slow_down"):
            raise RuntimeError(f"device login failed: {r.status_code} {r.text}")
    raise TimeoutError("device login timed out (user did not approve in time)")


def _refresh(tok: dict) -> Optional[dict]:
    refresh = tok.get("refresh_token") or tok.get("refreshToken")
    if not refresh:
        return None
    r = requests.post(
        f"{OIDC_URL}/token",
        headers={"Content-Type": "application/json"},
        json={
            "grantType": REFRESH_GRANT,
            "clientId": tok.get("client_id") or tok.get("clientId", ""),
            "clientSecret": tok.get("client_secret") or tok.get("clientSecret", ""),
            "refreshToken": refresh,
        },
        timeout=30,
    )
    if r.status_code != 200:
        return None
    new = r.json()
    tok.update(new)
    # Refresh responses omit expires_at; stamp it so _token_expired() works.
    # Always recompute from expiresIn — tok may already carry a STALE
    # expires_at (from the pre-refresh token), and the `if "expires_at" not
    # in tok` guard would skip the update, leaving an expired timestamp on
    # disk (bug: every call re-refreshed because the saved token looked
    # expired). Recompute whenever expiresIn is present.
    if new.get("expiresIn"):
        import time as _time

        tok["expires_at"] = int(_time.time()) + int(new["expiresIn"])
    _save_token(tok)
    return tok


def _load_sso_token() -> Optional[dict]:
    """Consult the plugin's BID login store (auth.sso_oidc) for a token.

    `bid_login` (the plugin's login tool) writes here, not to .q_token.json.
    Without this, a user who authenticates via the tool cannot chat because
    get_token() only looked at .q_token.json / Q's sqlite. Lazy-import to
    avoid pulling botocore at module load and to dodge circular imports.
    """
    try:
        from auth import sso_oidc
    except Exception:  # noqa: BLE001
        return None
    try:
        tok = sso_oidc._load_pool_token()
        if tok:
            return tok
        return sso_oidc._load_token()
    except Exception:  # noqa: BLE001
        return None


def get_token() -> dict:
    """Return a valid token.

    Order:
      1. Our persisted token (TOKEN_FILE, from a successful device_login()), if still valid.
      2. Q's existing authenticated session cached in its sqlite (reused so the
         direct backend works without the CLI binary for chat).
      3. The plugin's BID login store (auth.sso_oidc: Hermes credential pool or
         .bid_token.json), written by the `bid_login` tool — so that login
         actually enables chat.
      4. If a stored token is present but expired, attempt a silent OIDC
         refresh_token exchange (no browser/interaction) before giving up.
      5. Otherwise raise with an actionable message.
    """
    candidates = []
    tok = _load_token()
    if tok:
        candidates.append(tok)
    q_tok = _load_q_sqlite_token()
    if q_tok:
        candidates.append(q_tok)
    sso_tok = _load_sso_token()
    if sso_tok:
        candidates.append(sso_tok)

    for c in candidates:
        if c and not _token_expired(c):
            return c
    # Expired but refreshable -> silent refresh (no interactive device flow).
    for candidate in candidates:
        if candidate and (candidate.get("refresh_token") or candidate.get("refreshToken")):
            refreshed = _refresh(candidate)
            if refreshed:
                return refreshed
    raise RuntimeError(
        "No valid Amazon Q token available. AWS Build is binary-free: authenticate "
        "via the `bid_login` plugin tool (or `hermes auth add aws-build`), which "
        "performs the SigV4-free OIDC device flow and writes the token the chat "
        "path reads. Then retry. A refresh is attempted automatically on expiry."
    )

# --- request auth (Bearer only) ---
# Verified live via mitmproxy capture of `q chat`: the CodeWhisperer
# GenerateAssistantResponse call sends `Authorization: Bearer <OIDC accessToken>`
# with x-amz-target + Content-Type, and NO SigV4 signed-headers. The OIDC
# access_token from device_login() IS the chat bearer. (Earlier dual-auth
# SigV4 attempt was wrong — Q rejected the extra X-Amz-* signed headers.)
def _sign_request(method: str, url: str, bearer: str) -> dict:
    return {
        "Content-Type": "application/x-amz-json-1.0",
        "Authorization": f"Bearer {bearer}",
        "x-amz-target": X_AMZ_TARGET,
    }


# --- chat ----
def chat(prompt: str, model: str = "claude-sonnet-4", conversation_id: Optional[str] = None) -> str:
    """Send `prompt` to Q's GenerateAssistantResponse and return the answer text.

    `model` is accepted for API compatibility with the subprocess backend but is
    not sent to Q's chat API (Q selects the model server-side); it is ignored
    here to avoid sending an unknown field.

    NOTE: the OIDC access_token from device_login() is the chat bearer (verified
    live via mitmproxy capture of `q chat` — no SigV4, no token-exchange). This
    call reuses Q's existing authenticated session if present, or a fresh token
    from device_login(). If no valid token is available, get_token() raises a
    clear RuntimeError.
    """
    tok = get_token()
    access = tok.get("access_token") or tok.get("accessToken")
    if not access:
        raise RuntimeError("Amazon Q token missing access_token")

    body = {
        "conversationState": {
            "currentMessage": {
                "userInputMessage": {"content": prompt},
            },
            "chatTriggerType": "MANUAL",
        }
    }
    if conversation_id:
        body["conversationState"]["conversationId"] = conversation_id

    payload = json.dumps(body)
    headers = _sign_request("POST", CHAT_URL, access)
    r = requests.post(
        CHAT_URL,
        data=payload,
        headers=headers,
        timeout=120,
        stream=True,
    )
    if r.status_code != 200:
        err = r.text[:300]
        # Auth failure (expired/revoked bearer). Attempt a silent refresh and
        # ONE retry before giving up — don't nuke a possibly-valid token on a
        # generic 400, and don't require user interaction. (m1/m3)
        if r.status_code in (400, 401) and "invalid" in err.lower():
            for cand in (_load_token(), _load_q_sqlite_token(), _load_sso_token()):
                if cand and (cand.get("refresh_token") or cand.get("refreshToken")):
                    refreshed = _refresh(cand)
                    if refreshed:
                        return chat(prompt, model=model, conversation_id=conversation_id)
            raise RuntimeError(
                "Amazon Q rejected the bearer token (expired/revoked). AWS Build is "
                "binary-free: re-authenticate via the `bid_login` plugin tool (or "
                "`hermes auth add aws-build`) — it performs the SigV4-free OIDC "
                "device flow. A refresh is attempted automatically on expiry."
            )
        raise RuntimeError(f"Q chat HTTP {r.status_code}: {err}")
    return _extract_answer(r)


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

    The streaming service returns binary-framed events (`:event-type`,
    `:content-type`, `:message-type` headers, then a JSON payload), not
    newline-delimited Coral JSON. Each `assistantResponseEvent` payload is
    `{"content": "...", "modelId": "..."}` where the text may contain code with
    unbalanced braces/brackets/quotes/backslashes.

    We pull `content` from every payload that also carries `modelId` (so
    non-assistant event types are ignored). The `content` value is matched as a
    JSON string — escape-aware — so its own braces never mis-split the JSON.
    """
    raw = b""
    for chunk in response.iter_content(chunk_size=4096):
        raw += chunk
    text = raw.decode("utf-8", "replace")
    parts: list[str] = []
    for m in _CONTENT_RE.finditer(text):
        # Bound the enclosing object so we can confirm it's an assistant event.
        obj_start = text.rfind("{", 0, m.start())
        if obj_start == -1:
            continue
        obj_end = _match_brace(text, obj_start)
        if '"modelId"' not in text[obj_start : obj_end + 1]:
            continue
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            continue
    text = "".join(parts).strip()
    # Mid-stream auth/upstream error can arrive as a JSON error envelope after
    # 200 headers (m3). Surface it instead of silently returning "(no response)".
    if not text:
        err = re.search(r'"__type"\s*:\s*"([^"]+)"', raw.decode("utf-8", "replace"))
        if err:
            return f"(Q error: {err.group(1)})"
        return "(no response)"
    return text


# Static catalog — single source of truth (matches config.yaml aws-build.models).
# `q chat --model help` used to provide this live, but that requires the `q`
# binary (removed). The dedicated ListAvailableModels Smithy API exists
# (chat-cli/src/api_client/mod.rs:277) but its X-Amz-Target prefix lives in the
# aws-smithy runtime and is not derivable without the service model; live probes
# of `AmazonCodeWhisperer(ListAvailableModels)` on the chat endpoint return 404.
# So we keep the static list and treat any live fetch as a best-effort override.
STATIC_MODELS = [
    "claude-sonnet-4.5",
    "claude-sonnet-4",
    "claude-haiku-4.5",
]


def list_models() -> list[str]:
    """Return available AWS Build models.

    The catalog is the static STATIC_MODELS list (matches config.yaml
    aws-build.models). A genuine live ListAvailableModels call is not wired
    because its Smithy X-Amz-Target prefix lives in the aws-smithy runtime and
    is not derivable without the service model (live probes 404). Returning the
    static list directly also avoids requiring a live token at import time
    (the plugin builds AVAILABLE_MODELS from this at load).
    """
    return list(STATIC_MODELS)


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "reply with exactly: DIRECT_OK"
    print(chat(p))
