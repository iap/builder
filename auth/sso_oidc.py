"""Amazon BID (Build ID) — headless SSO-OIDC device authorization (RFC 8628).

Wraps the Amazon BID (Build ID) device flow for the Amazon build CLI,
but requires neither that binary nor any AWS credentials on the client side.
The OIDC server (oidc.us-east-1.amazonaws.com) accepts the public client
registration anonymously, so the whole flow runs headless: the agent starts it,
the human approves the user_code in their browser (Brave, already signed in to
their Google account), and the plugin polls for the token.

Robustness: the in-flight device authorization is persisted to disk, so any
process calling get_status can complete the flow (the background daemon thread
is a performance optimization for long-lived agent processes, not a
requirement). Secrets (client secret, access/refresh tokens) are written under
HERMES_HOME with chmod 600. Nothing sensitive is ever returned by a tool
handler or kept in session state.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# --- Protocol constants ---
OIDC_REGION = "us-east-1"
OIDC_ENDPOINT = "https://oidc.us-east-1.amazonaws.com"
CLIENT_NAME = "Hermes Agent Build Plugin"
CLIENT_TYPE = "public"
START_URL = "https://view.awsapps.com/start"
SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
]
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT_TYPE = "refresh_token"

# --- Runtime state (process-local; synced to disk for cross-process safety) ---
_lock = threading.Lock()
_stop = threading.Event()
_poll_thread: Optional[threading.Thread] = None

# The Hermes credential pool is the canonical store for the authenticated
# token. hermes auth add aws-build writes it there; this module reads it first
# and falls back to the legacy .bid_* mirror only for transient flow state.
POOL_PROVIDER = "aws-build"


def _load_pool_token() -> Optional[dict]:
    """Read the canonical aws-build credential from the Hermes credential pool.

    Returns a token-shaped dict compatible with _load_token(), or None.
    Defensive: any failure yields None so the caller falls back to the
    legacy .bid_* mirror.
    """
    try:
        from agent.credential_pool import load_pool
    except Exception:  # noqa: BLE001
        return None
    try:
        entries = load_pool(POOL_PROVIDER).entries()
    except Exception:  # noqa: BLE001
        return None
    now_ms = time.time() * 1000
    for e in entries:
        if e.access_token and (e.expires_at_ms or 0) > now_ms:
            extra = e.extra or {}
            return {
                "access_token": e.access_token,
                "refresh_token": e.refresh_token,
                "expires_at": (e.expires_at_ms or 0) / 1000.0,
                "token_type": extra.get("token_type"),
                "scopes": extra.get("scopes"),
            }
    return None


def _clear_pool() -> None:
    """Remove all aws-build credentials from the canonical pool store."""
    try:
        from agent.credential_pool import load_pool
    except Exception:  # noqa: BLE001
        return
    try:
        pool = load_pool(POOL_PROVIDER)
        while pool.entries():
            pool.remove_index(1)
    except Exception:  # noqa: BLE001
        pass


# --- Paths (always HERMES_HOME, never hardcoded ~/.hermes) ---
def _home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


# Canonical mirror directory for this plugin. Matches the plugin's actual
# directory name (`aws-build`). A legacy `build` directory was used by earlier
# versions; `_mirror_path()` falls back to it on read so an already-logged-in
# user isn't logged out by the rename.
_PLUGIN_DIR_NAME = "aws-build"
_LEGACY_PLUGIN_DIR_NAME = "build"


def _mirror_path(filename: str) -> Path:
    """Return the read path for a mirror file, preferring an existing legacy file.

    Reads prefer the canonical `plugins/aws-build/` path but transparently fall
    back to the legacy `plugins/build/` location if only that exists, so a token
    written by an older version is still found. Writes should use
    `_canonical_path()` so state always migrates to the canonical directory.
    """
    canonical = _canonical_path(filename)
    if canonical.exists():
        return canonical
    legacy = _home() / "plugins" / _LEGACY_PLUGIN_DIR_NAME / filename
    if legacy.exists():
        return legacy
    return canonical


def _canonical_path(filename: str) -> Path:
    """Return the canonical mirror path (always `plugins/aws-build/`)."""
    return _home() / "plugins" / _PLUGIN_DIR_NAME / filename


def _reg_path() -> Path:
    return _mirror_path(".bid_registration.json")


def _token_path() -> Path:
    return _mirror_path(".bid_token.json")


def _flow_path() -> Path:
    return _mirror_path(".bid_flow.json")


def _write_secret(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def _read_secret(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - corrupt state => treat as absent
        return None


# --- boto3 client (anonymous; no AWS credentials needed) ---
_cached_client = None
_cached_client_lock = threading.Lock()


def _client():
    import botocore.session
    from botocore import UNSIGNED
    from botocore.config import Config

    global _cached_client
    with _cached_client_lock:
        if _cached_client is None:
            sess = botocore.session.get_session()
            sess.get_credentials = lambda: None  # force anonymous
            _cached_client = sess.create_client(
                "sso-oidc",
                region_name=OIDC_REGION,
                endpoint_url=OIDC_ENDPOINT,
                config=Config(signature_version=UNSIGNED),
            )
        return _cached_client


def is_available() -> bool:
    """check_fn: True when boto3 is importable for the device flow."""
    try:
        import boto3  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


# --- Client registration (cached to disk, server-recommended) ---
def _load_registration() -> Optional[dict]:
    data = _read_secret(_reg_path())
    if not data:
        return None
    exp = data.get("client_secret_expires_at")
    if exp is not None and time.time() >= exp:
        return None
    return data


def _register() -> dict:
    cached = _load_registration()
    if cached:
        return cached
    c = _client()
    out = c.register_client(
        clientName=CLIENT_NAME, clientType=CLIENT_TYPE, scopes=SCOPES
    )
    data = {
        "client_id": out["clientId"],
        "client_secret": out["clientSecret"],
        "client_secret_expires_at": out.get("clientSecretExpiresAt"),
        "scopes": SCOPES,
    }
    _write_secret(_canonical_path(".bid_registration.json"), data)
    return data


# --- Token persistence ---
def _save_token(out: dict, reg: dict) -> None:
    expires_at = time.time() + out.get("expiresIn", 0)
    data = {
        "access_token": out["accessToken"],
        "refresh_token": out.get("refreshToken"),
        "expires_at": expires_at,
        "token_type": out.get("tokenType"),
        "scopes": reg.get("scopes"),
    }
    _write_secret(_canonical_path(".bid_token.json"), data)


def _load_token() -> Optional[dict]:
    return _read_secret(_token_path())


# --- Flow persistence (so any process can complete the poll) ---
def _save_flow(flow: dict) -> None:
    _write_secret(_canonical_path(".bid_flow.json"), flow)


def _load_flow() -> Optional[dict]:
    return _read_secret(_flow_path())


# --- Single poll attempt (shared by daemon thread + get_status) ---
def _poll_once(reg: dict, flow: dict) -> str:
    """Attempt one create_token call. Returns phase: authenticated/pending/error."""
    from botocore.exceptions import ClientError, EndpointConnectionError, ConnectionError

    c = _client()
    try:
        out = c.create_token(
            grantType=DEVICE_GRANT_TYPE,
            clientId=reg["client_id"],
            clientSecret=reg["client_secret"],
            deviceCode=flow["device_code"],
        )
        _save_token(out, reg)
        try:
            _flow_path().unlink()
        except OSError:  # noqa: BLE001
            pass
        return "authenticated"
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "AuthorizationPendingException":
            return "pending"
        if code == "SlowDownException":
            return "slow_down"
        logger.error("device token poll failed: %s", code)
        return "error:" + code
    except (EndpointConnectionError, ConnectionError) as e:
        logger.warning("transient network error during poll: %s", e)
        return "error:poll_network_error"
    except Exception:  # noqa: BLE001
        logger.exception("device token poll crashed")
        return "error:poll_error"


def _poll_loop(reg: dict, flow: dict) -> None:
    interval = max(1, flow.get("interval", 1))
    expires_at = flow.get("started_at", time.time()) + flow.get("expires_in", 600)
    retry_count = 0
    max_retries = 3
    while time.time() < expires_at:
        if _stop.is_set():
            return
        phase = _poll_once(reg, flow)
        if phase == "authenticated":
            return
        if phase == "slow_down":
            interval += 5
            flow["interval"] = interval
            _save_flow(flow)
            retry_count = 0
        if phase.startswith("error:"):
            if phase == "error:poll_network_error" and retry_count < max_retries:
                retry_count += 1
                time.sleep(min(interval * retry_count, 5))
                continue
            return
        retry_count = 0
        time.sleep(interval)


def _start_poll_thread(reg: dict, flow: dict) -> None:
    global _poll_thread
    _stop.clear()
    with _lock:
        if _poll_thread and _poll_thread.is_alive():
            return
    _poll_thread = threading.Thread(
        target=_poll_loop, args=(reg, flow), daemon=True
    )
    _poll_thread.start()


# --- Public API ---
def start_login() -> dict:
    """Start the device flow. Returns the user_code + verification URL only."""
    reg = _register()
    c = _client()
    da = c.start_device_authorization(
        clientId=reg["client_id"],
        clientSecret=reg["client_secret"],
        startUrl=START_URL,
    )
    flow = {
        "device_code": da.get("deviceCode"),
        "user_code": da.get("userCode"),
        "verification_uri": da.get("verificationUri"),
        "verification_uri_complete": da.get("verificationUriComplete"),
        "expires_in": da.get("expiresIn"),
        "interval": max(1, da.get("interval", 1)),
        "started_at": time.time(),
        "phase": "awaiting_approval",
    }
    _save_flow(flow)
    _start_poll_thread(reg, flow)
    return {
        "user_code": flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "verification_uri_complete": flow["verification_uri_complete"],
        "expires_in": flow["expires_in"],
        "interval": flow["interval"],
    }


def _save_pool_token(out: dict, reg: dict) -> None:
    """Mirror a refreshed token into the canonical pool entry, if present."""
    try:
        from agent.credential_pool import load_pool, PooledCredential
    except Exception:  # noqa: BLE001
        return
    try:
        pool = load_pool(POOL_PROVIDER)
        entries = pool.entries()
        if entries:
            e = entries[0]
            pool.remove_index(1)
            updated = PooledCredential(
                provider=POOL_PROVIDER,
                id=e.id,
                label=e.label,
                auth_type=e.auth_type,
                priority=0,
                source=e.source,
                access_token=out.get("accessToken"),
                refresh_token=out.get("refreshToken", e.refresh_token),
                expires_at_ms=int((time.time() + out.get("expiresIn", 0)) * 1000),
                extra={**(e.extra or {}), "token_type": out.get("tokenType"), "scopes": reg.get("scopes")},
            )
            pool.add_entry(updated)
        else:
            updated = PooledCredential(
                provider=POOL_PROVIDER,
                id=None,
                label="Amazon BID (Build ID)",
                auth_type="oauth",
                priority=0,
                source="plugin",
                access_token=out.get("accessToken"),
                refresh_token=out.get("refreshToken"),
                expires_at_ms=int((time.time() + out.get("expiresIn", 0)) * 1000),
                extra={"token_type": out.get("tokenType"), "scopes": reg.get("scopes")},
            )
            pool.add_entry(updated)
    except Exception:  # noqa: BLE001
        pass


def refresh_token() -> bool:
    """Use the refresh token to obtain a new access token. Returns success."""
    tok = _load_pool_token() or _load_token()
    reg = _load_registration()
    if not tok or not reg or not tok.get("refresh_token"):
        return False
    c = _client()
    for attempt in range(3):
        try:
            out = c.create_token(
                grantType=REFRESH_GRANT_TYPE,
                clientId=reg["client_id"],
                clientSecret=reg["client_secret"],
                refreshToken=tok["refresh_token"],
            )
            _save_token(out, reg)
            _save_pool_token(out, reg)
            return True
        except Exception:  # noqa: BLE001
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            logger.exception("token refresh failed")
            return False
    return False


def ensure_valid() -> bool:
    """Refresh the token in place if expired and a refresh token exists.

    Reads the canonical Hermes credential pool first (falling back to the
    legacy .bid_token.json mirror) so a token that lives only in the pool
    is still considered valid instead of triggering a needless refresh.
    """
    tok = _load_pool_token() or _load_token()
    if not tok:
        return False
    if tok.get("expires_at", 0) > time.time():
        return True
    return refresh_token()


def get_status() -> dict:
    """Return current auth/flow state. Actively polls if a flow is pending.

    Never includes the raw token.
    """
    tok = _load_pool_token()
    if not tok or tok.get("expires_at", 0) <= time.time():
        tok = _load_token()
    authenticated = bool(tok) and tok.get("expires_at", 0) > time.time()
    if authenticated:
        return {
            "authenticated": True,
            "phase": "authenticated",
            "user_code": None,
            "verification_uri_complete": None,
            "expires_in": None,
            "interval": None,
            "error": None,
            "token_expires_at": tok.get("expires_at"),
            "token_expires_at_iso": datetime.fromtimestamp(
                tok["expires_at"], tz=timezone.utc
            ).isoformat(),
            "scopes": tok.get("scopes"),
        }

    # Not authenticated: maybe a flow is in progress -> try one poll.
    flow = _load_flow()
    phase = "idle"
    error = None
    if flow:
        reg = _load_registration()
        if reg:
            result = _poll_once(reg, flow)
            if result == "authenticated":
                return get_status()  # token now saved; recurse for clean shape
            if result == "slow_down":
                # RFC 8628 §3.5: bump interval by >=5s and persist for the
                # next poll attempt (this process or another).
                flow["interval"] = flow.get("interval", 1) + 5
                _save_flow(flow)
            if result.startswith("error:"):
                error = result.split(":", 1)[1]
            phase = (
                "awaiting_approval"
                if (result == "pending" or result == "slow_down")
                else "error"
            )
        else:
            phase = "error"
            error = "no_registration"
    return {
        "authenticated": False,
        "phase": phase,
        "user_code": flow.get("user_code") if flow else None,
        "verification_uri_complete": flow.get("verification_uri_complete") if flow else None,
        "expires_in": flow.get("expires_in") if flow else None,
        "interval": flow.get("interval") if flow else None,
        "error": error,
        "token_expires_at": None,
        "token_expires_at_iso": None,
        "scopes": None,
    }


def show_identity() -> dict:
    """Return token identity metadata only (no raw token)."""
    ensure_valid()
    tok = _load_pool_token() or _load_token()
    if not tok:
        return {"authenticated": False}
    return {
        "authenticated": tok.get("expires_at", 0) > time.time(),
        "token_type": tok.get("token_type"),
        "scopes": tok.get("scopes"),
        "has_refresh_token": bool(tok.get("refresh_token")),
        "expires_at": tok.get("expires_at"),
        "expires_at_iso": (
            datetime.fromtimestamp(tok["expires_at"], tz=timezone.utc).isoformat()
            if tok.get("expires_at")
            else None
        ),
    }


def logout() -> None:
    """Stop polling and delete all stored secrets.

    Clears both the canonical Hermes credential pool entry and the legacy
    .bid_* mirror files
    """
    _stop.set()
    global _poll_thread
    thread = _poll_thread
    _poll_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    _clear_pool()
    # Delete mirror files in BOTH the canonical (aws-build) and legacy (build)
    # directories so a rename never leaves an orphaned token behind.
    filenames = (".bid_token.json", ".bid_registration.json", ".bid_flow.json")
    dirs = (
        _home() / "plugins" / _PLUGIN_DIR_NAME,
        _home() / "plugins" / _LEGACY_PLUGIN_DIR_NAME,
    )
    for d in dirs:
        for name in filenames:
            p = d / name
            try:
                if p.exists():
                    p.unlink()
            except OSError:  # noqa: BLE001
                pass
