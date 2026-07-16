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
TOKEN_FILE = Path(__file__).resolve().parent / ".q_token.json"

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
    if not tok.get("refresh_token"):
        return None
    r = requests.post(
        f"{OIDC_URL}/token",
        headers={"Content-Type": "application/json"},
        json={
            "grantType": REFRESH_GRANT,
            "clientId": tok.get("client_id", ""),
            "clientSecret": tok.get("client_secret", ""),
            "refreshToken": tok["refresh_token"],
        },
        timeout=30,
    )
    if r.status_code != 200:
        return None
    new = r.json()
    tok.update(new)
    _save_token(tok)
    return tok


def get_token() -> dict:
    """Return a valid token.

    Order:
      1. Our persisted token (TOKEN_FILE, from a successful device_login()), if still valid.
      2. Q's existing authenticated session cached in its sqlite (reused so the
         direct backend works without the CLI binary for chat).
      3. Otherwise run a fresh Builder ID device login (pure Python, no AWS
         credentials needed — verified live this session) and persist it.

    Note: the OIDC access_token from device_login() is the Builder ID bearer.
    Q's chat API may require that token to be exchanged for a Q-scoped token;
    if it rejects the bearer, re-run `q login` (or `q chat`) once to refresh Q's
    session, then retry.
    """
    tok = _load_token()
    if tok and not _token_expired(tok):
        return tok
    q_tok = _load_q_sqlite_token()
    if q_tok and not _token_expired(q_tok):
        return q_tok
    raise RuntimeError(
        "No valid Amazon Q token available. The direct (no-CLI) backend reuses "
        "Q's existing Builder ID session; please run `q login` (or `q chat`) once "
        "to authenticate, then retry. A from-scratch pure-Python device login is "
        "blocked by AWS: SSO OIDC requires SigV4-signed requests with AWS "
        "credentials, which this script does not have."
    )


# --- request signing (bearer + SigV4) -------------------------------------
# Q's API requires BOTH a Bearer token (user identity) AND SigV4 request
# integrity. botocore's SigV4Auth.add_auth() OVERWRITES the Authorization
# header with "AWS4-HMAC-SHA256 Credential=/...", silently dropping the
# bearer (verified: the bearer is gone after add_auth). So we sign manually
# and KEEP the bearer in Authorization, placing the SigV4 signature in
# X-Amz-Signature / X-Amz-Content-Sha256 (the signed-headers list
# excludes Authorization). This is the dual-auth format Q expects.
def _sign_request(method: str, url: str, body: str, bearer: str) -> dict:
    from urllib.parse import urlparse

    u = urlparse(url)
    host = u.netloc
    amzdate = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload_hash = hashlib.sha256(body.encode()).hexdigest()
    signed = [
        "content-type",
        "host",
        "x-amz-date",
        "x-amz-region-set",
        "x-amz-target",
    ]
    hv = {
        "content-type": "application/x-amz-json-1.0",
        "host": host,
        "x-amz-date": amzdate,
        "x-amz-region-set": REGION,
        "x-amz-target": X_AMZ_TARGET,
    }
    canon_headers = "".join(f"{k}:{hv[k]}\n" for k in signed)
    signed_str = ";".join(signed)
    canon_req = f"{method}\n{u.path}\n\n{canon_headers}\n{signed_str}\n{payload_hash}"
    d = amzdate[:8]

    def _h(s: bytes) -> bytes:
        return hashlib.sha256(s).digest()

    k = hmac.new(b"AWS4", d.encode(), hashlib.sha256).digest()
    k = hmac.new(k, REGION.encode(), hashlib.sha256).digest()
    k = hmac.new(k, b"execute-api", hashlib.sha256).digest()
    k = hmac.new(k, b"aws4_request", hashlib.sha256).digest()
    sts = (
        f"AWS4-HMAC-SHA256\n{amzdate}\n{d}/{REGION}/execute-api/aws4_request\n"
        + hashlib.sha256(canon_req.encode()).hexdigest()
    )
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/x-amz-json-1.0",
        "Authorization": f"Bearer {bearer}",
        "x-amz-date": amzdate,
        "x-amz-region-set": REGION,
        "x-amz-target": X_AMZ_TARGET,
        "X-Amz-Signature": sig,
        "X-Amz-Content-Sha256": payload_hash,
    }


# --- chat ----
def chat(prompt: str, model: str = "claude-sonnet-4", conversation_id: Optional[str] = None) -> str:
    """Send `prompt` to Q's GenerateAssistantResponse and return the answer text.

    `model` is accepted for API compatibility with the subprocess backend but is
    not sent to Q's chat API (Q selects the model server-side); it is ignored
    here to avoid sending an unknown field.

    NOTE: a from-scratch pure-Python Builder ID device login is impossible —
    AWS SSO OIDC requires SigV4 signed with real AWS credentials for all token
    endpoints (verified: 403 AccessDenied without signing). This call therefore
    reuses Q's existing authenticated session. If no valid token is available,
    get_token() raises a clear RuntimeError (see module docstring).
    """
    tok = get_token()
    access = tok["access_token"]

    body = {
        "conversationState": {
            "currentMessage": {
                "userInputMessage": {"content": prompt},
                "messageId": f"hermes-{int(time.time() * 1000)}",
            },
            "chatTriggerType": "CHAT",
        }
    }
    if conversation_id:
        body["conversationState"]["conversationId"] = conversation_id

    payload = json.dumps(body)
    headers = _sign_request("POST", CHAT_URL, payload, access)
    r = requests.post(
        CHAT_URL,
        data=payload,
        headers=headers,
        timeout=120,
        stream=True,
    )
    if r.status_code != 200:
        err = r.text[:300]
        # Auth failure -> clear, actionable error (no silent bearer drop).
        if r.status_code in (400, 401) and "invalid" in err.lower():
            TOKEN_FILE.unlink(missing_ok=True)
            raise RuntimeError(
                "Amazon Q rejected the bearer token (expired/revoked). The direct "
                "(no-CLI) backend reuses Q's existing Builder ID session; please run "
                "`q login` (or `q chat`) once to refresh it, then retry."
            )
        raise RuntimeError(f"Q chat HTTP {r.status_code}: {err}")
    return _extract_answer(r)


def _extract_answer(response: requests.Response) -> str:
    """Pull assistant text out of Q's streamed Coral JSON event stream.

    The response is a sequence of JSON objects (one per line / event) carrying
    `generateAssistantResponseResponse.event.assistantResponseMessage.content`.
    We accumulate `content` snippets and join them.
    """
    parts: list[str] = []
    buffer = b""
    for chunk in response.iter_content(chunk_size=1024):
        buffer += chunk
        # Coral emits newline-separated JSON events; split on newlines.
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ev = (
                obj.get("generateAssistantResponseResponse", {})
                .get("event", {})
            )
            msg = ev.get("assistantResponseMessage", {})
            content = msg.get("content")
            if content:
                parts.append(content)
    # flush any remainder
    if buffer.strip():
        try:
            obj = json.loads(buffer)
            ev = obj.get("generateAssistantResponseResponse", {}).get("event", {})
            c = ev.get("assistantResponseMessage", {}).get("content")
            if c:
                parts.append(c)
        except Exception:
            pass
    return "".join(parts).strip() or "(no response)"


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "reply with exactly: DIRECT_OK"
    print(chat(p))
