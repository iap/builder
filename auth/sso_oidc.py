"""Amazon BID (Builder ID) — headless SSO-OIDC device authorization (RFC 8628).
# SPDX-License-Identifier: MIT OR Apache-2.0

Wraps the Amazon BID (Builder ID) device flow for the Amazon builder CLI,
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

# This plugin is fully self-contained: the authenticated token lives ONLY in
# the local `auth/bid_token.json` mirror under HERMES_HOME. It does NOT use the
# Hermes credential pool / `hermes auth` mechanism (that integration was never
# wired up in core, so the plugin owns its own token store end-to-end).
# --- Paths (always HERMES_HOME, never hardcoded ~/.hermes) ---
def _home() -> Path:
    """Return HERMES_HOME as a Path, preferring env or core."""
    # 1. Env var (tests set this explicitly)
    if "HERMES_HOME" in os.environ:
        return Path(os.environ["HERMES_HOME"])
    # 2. Core Hermes path mechanism
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        pass
    # 3. Standalone fallback: plugin dir parent
    return Path(__file__).resolve().parent.parent


# Canonical directory for this plugin. Matches the plugin's actual
# directory name (`builder`). Secrets live in an `auth/` subdir (scoped to
# this plugin, NOT Hermes core's `auth/` namespace) as plain, non-hidden
# JSON files written chmod 600.
_PLUGIN_DIR_NAME = "builder"
_AUTH_DIR_NAME = "auth"

# De-dotted, non-hidden secret filenames under <plugin>/auth/.
_REG_FILENAME = "bid_registration.json"
_TOKEN_FILENAME = "bid_token.json"
_FLOW_FILENAME = "bid_flow.json"

# Legacy dotted names (pre-migration). Kept only for the one-time read
# fallback so existing sessions survive the move; never written.
_LEGACY_REG_FILENAME = ".bid_registration.json"
_LEGACY_TOKEN_FILENAME = ".bid_token.json"
_LEGACY_FLOW_FILENAME = ".bid_flow.json"


def _auth_dir() -> Path:
    return _home() / "plugins" / _PLUGIN_DIR_NAME / _AUTH_DIR_NAME


def _canonical_path(filename: str) -> Path:
    """Return the canonical secret path under `plugins/builder/auth/`."""
    return _auth_dir() / filename


def _reg_path() -> Path:
    return _canonical_path(_REG_FILENAME)


def _token_path() -> Path:
    return _canonical_path(_TOKEN_FILENAME)


def _flow_path() -> Path:
    return _canonical_path(_FLOW_FILENAME)


def _write_secret(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # This local auth store is intentionally left as cleartext JSON. The plugin
    # relies on OS-level access control instead: Hermes home is private to the
    # user, and the file is created with 0o600 so only the owner can read it.
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read_secret(path: Path) -> Optional[dict]:
    """Read a secret, with a one-time legacy (dotted) path fallback + migrate.

    If the canonical (de-dotted) file under `auth/` is absent but a legacy
    `.bid_*.json` exists in the plugin root, read it and copy it into the new
    location so an existing session survives the layout change without
    re-login.
    """
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:  # noqa: BLE001 - corrupt state => treat as absent
            return None
    # Legacy files lived as dotted names directly in the plugin root
    # (plugins/builder/.bid_token.json), not inside auth/. Also support the older
    # plugin directory name `aws-build` so a directory rename (aws-build -> builder)
    # migrates an existing session's secrets without forcing a re-login.
    legacy_candidates = [
        _home() / "plugins" / _PLUGIN_DIR_NAME / ("." + path.name),
        _home() / "plugins" / "aws-build" / "auth" / path.name,
        _home() / "plugins" / "aws-build" / ("." + path.name),
    ]
    for legacy in legacy_candidates:
        if legacy.exists():
            try:
                data = json.loads(legacy.read_text())
            except Exception:  # noqa: BLE001
                return None
            _write_secret(path, data)  # migrate into the new auth/ layout
            try:
                legacy.unlink()
            except OSError:  # noqa: BLE001
                pass
            return data
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
    _write_secret(_canonical_path(_REG_FILENAME), data)
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
    _write_secret(_canonical_path(_TOKEN_FILENAME), data)


def _load_token() -> Optional[dict]:
    return _read_secret(_token_path())


# --- Flow persistence (so any process can complete the poll) ---


def _save_flow(flow: dict) -> None:
    _write_secret(_canonical_path(_FLOW_FILENAME), flow)


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
        # InvalidGrantException is expected when a device code was already
        # consumed/expired or a duplicate flow collided (e.g. the dashboard
        # login button re-triggers the same device flow that already
        # completed). If a valid token already exists on disk, this is a
        # benign race, not a real failure — log it at debug and stop, so it
        # is not surfaced to the user as an auth error.
        if code == "InvalidGrantException":
            if _load_token():
                logger.debug("stale device poll after grant (token present): %s", code)
            else:
                logger.error("device token poll failed: %s", code)
            return "error:" + code
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
    """Start the device flow. Returns the user_code + verification URL only.

    If a valid token already exists, do NOT start a new device flow — that
    would spawn a doomed duplicate whose stale code makes AWS return
    ``InvalidGrantException`` (surfaced as a login error in the dashboard).
    Return an already-authenticated marker instead.
    """
    if _load_token():
        return {"already_authenticated": True, "phase": "authenticated"}
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


def refresh_token() -> bool:
    """Use the refresh token to obtain a new access token. Returns success."""
    tok = _load_token()
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
            return True
        except Exception:  # noqa: BLE001
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            logger.exception("token refresh failed")
            return False
    return False


def ensure_valid() -> bool:
    """Refresh the token in place if expired and a refresh token exists."""
    tok = _load_token()
    if not tok:
        return False
    if tok.get("expires_at", 0) > time.time():
        return True
    return refresh_token()


def get_status() -> dict:
    """Return current auth/flow state. Actively polls if a flow is pending.

    Never includes the raw token.

    If the stored access token is expired but a refresh token exists, this
    silently refreshes it *before* reporting (so the card / bid_status stay
    "Authenticated" across the ~1h access-token boundary and only flip to
    "expired" when the refresh token itself is dead). Reads the local
    `auth/bid_token.json` mirror (this plugin's sole token store — it does not use
    the Hermes credential pool).
    """
    mirror_tok = _load_token()
    tok = mirror_tok if (mirror_tok and mirror_tok.get("expires_at", 0) > time.time()) else None
    refreshed = False
    expired_token_present = bool(
        mirror_tok and mirror_tok.get("expires_at", 0) <= time.time()
    )
    if not tok and mirror_tok and mirror_tok.get("refresh_token"):
        # Expired access token but refreshable -> renew silently.
        refreshed = refresh_token()
        if refreshed:
            tok = _load_token()
    authenticated = tok is not None
    if authenticated:
        return {
            "authenticated": True,
            "phase": "authenticated",
            "user_code": None,
            "verification_uri_complete": None,
            "expires_in": None,
            "interval": None,
            "error": None,
            "refreshed": refreshed,
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
        "phase": "expired" if expired_token_present else phase,
        "user_code": flow.get("user_code") if flow else None,
        "verification_uri_complete": flow.get("verification_uri_complete") if flow else None,
        "expires_in": flow.get("expires_in") if flow else None,
        "interval": flow.get("interval") if flow else None,
        "error": error,
        "refreshed": False,
        "token_expires_at": None,
        "token_expires_at_iso": None,
    }


def show_identity() -> dict:
    """Return token identity metadata only (no raw token)."""
    ensure_valid()
    tok = _load_token()
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
    """Stop polling and delete all stored secrets (local mirror files)."""
    _stop.set()
    global _poll_thread
    thread = _poll_thread
    _poll_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    # Delete mirror files in the canonical plugin auth/ directory (and any
    # legacy dotted files left in the plugin root from before the migration).
    new_names = (_TOKEN_FILENAME, _REG_FILENAME, _FLOW_FILENAME)
    legacy_names = (_LEGACY_TOKEN_FILENAME, _LEGACY_REG_FILENAME, _LEGACY_FLOW_FILENAME)
    dirs = (
        _auth_dir(),
        _home() / "plugins" / _PLUGIN_DIR_NAME,
    )
    for d in dirs:
        for name in (*new_names, *legacy_names):
            p = d / name
            try:
                if p.exists():
                    p.unlink()
            except OSError:  # noqa: BLE001
                pass