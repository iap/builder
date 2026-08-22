"""Tests for auth/sso_oidc botocore paths — botocore fully mocked."""

# SPDX-License-Identifier: MIT
from __future__ import annotations

import importlib
import sys
import types


def _setup(tmp_path, monkeypatch, create_token_result=None):
    """Inject fake botocore, set HERMES_HOME, reload sso_oidc."""
    # Build minimal botocore stub
    bc = types.ModuleType("botocore")
    bc.UNSIGNED = "unsigned"
    bs = types.ModuleType("botocore.session")
    be = types.ModuleType("botocore.exceptions")
    bcfg = types.ModuleType("botocore.config")

    class ClientError(Exception):
        def __init__(self, code):
            self.response = {"Error": {"Code": code}}

    be.ClientError = ClientError
    be.EndpointConnectionError = OSError
    be.ConnectionError = OSError
    bcfg.Config = lambda **kw: kw

    _token_result = create_token_result or {
        "accessToken": "tok",
        "expiresIn": 3600,
        "refreshToken": "ref",
        "tokenType": "Bearer",
    }

    class _Client:
        def register_client(self, **kw):
            return {
                "clientId": "cid",
                "clientSecret": "sec",
                "clientSecretExpiresAt": 9_999_999_999,
            }

        def start_device_authorization(self, **kw):
            return {
                "deviceCode": "dc",
                "userCode": "UC-1234",
                "verificationUri": "https://example.com",
                "verificationUriComplete": "https://example.com?code=UC-1234",
                "expiresIn": 600,
                "interval": 1,
            }

        def create_token(self, **kw):
            if callable(_token_result):
                return _token_result()
            return _token_result

    class _Session:
        get_credentials = None

        def create_client(self, *a, **kw):
            return _Client()

    bs.get_session = lambda: _Session()
    bc.session = bs
    bc.exceptions = be
    bc.config = bcfg
    for k, v in [
        ("botocore", bc),
        ("botocore.session", bs),
        ("botocore.exceptions", be),
        ("botocore.config", bcfg),
    ]:
        sys.modules[k] = v

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import auth.sso_oidc as s

    # Reset module-level state
    s._cached_client = None
    s._poll_thread = None
    s._stop.clear()
    importlib.reload(s)
    return s, be


def test_client_creates_and_caches(tmp_path, monkeypatch):
    s, _ = _setup(tmp_path, monkeypatch)
    c1 = s._client()
    c2 = s._client()
    assert c1 is c2


def test_register_writes_and_caches(tmp_path, monkeypatch):
    s, _ = _setup(tmp_path, monkeypatch)
    reg = s._register()
    assert reg["client_id"] == "cid"
    # Second call returns cached (no second botocore call needed)
    reg2 = s._register()
    assert reg2["client_id"] == "cid"


def test_poll_once_authenticated(tmp_path, monkeypatch):
    s, _ = _setup(tmp_path, monkeypatch)
    result = s._poll_once(
        {"client_id": "cid", "client_secret": "sec", "scopes": []},
        {"device_code": "dc"},
    )
    assert result == "authenticated"
    assert s._load_token()["access_token"] == "tok"


def test_poll_once_pending(tmp_path, monkeypatch):
    s, be = _setup(tmp_path, monkeypatch)

    # Patch _client() to return a client that raises AuthorizationPendingException
    class _PendingClient:
        def create_token(self, **kw):
            raise be.ClientError("AuthorizationPendingException")

    s._cached_client = _PendingClient()
    result = s._poll_once(
        {"client_id": "cid", "client_secret": "sec", "scopes": []},
        {"device_code": "dc"},
    )
    assert result == "pending"


def test_poll_once_slow_down(tmp_path, monkeypatch):
    s, be = _setup(tmp_path, monkeypatch)

    class _SlowClient:
        def create_token(self, **kw):
            raise be.ClientError("SlowDownException")

    s._cached_client = _SlowClient()
    result = s._poll_once(
        {"client_id": "cid", "client_secret": "sec", "scopes": []},
        {"device_code": "dc"},
    )
    assert result == "slow_down"


def test_poll_once_invalid_grant_no_token(tmp_path, monkeypatch):
    s, be = _setup(tmp_path, monkeypatch)

    class _InvalidClient:
        def create_token(self, **kw):
            raise be.ClientError("InvalidGrantException")

    s._cached_client = _InvalidClient()
    result = s._poll_once(
        {"client_id": "cid", "client_secret": "sec", "scopes": []},
        {"device_code": "dc"},
    )
    assert result == "error:InvalidGrantException"


def test_poll_once_network_error(tmp_path, monkeypatch):
    s, be = _setup(tmp_path, monkeypatch)

    class _NetErrClient:
        def create_token(self, **kw):
            raise be.EndpointConnectionError("net")

    s._cached_client = _NetErrClient()
    result = s._poll_once(
        {"client_id": "cid", "client_secret": "sec", "scopes": []},
        {"device_code": "dc"},
    )
    assert result == "error:poll_network_error"


def test_refresh_token_success(tmp_path, monkeypatch):
    s, _ = _setup(tmp_path, monkeypatch)
    # Write an expired token with a refresh token
    s._write_secret(
        s._token_path(),
        {
            "access_token": "old",
            "refresh_token": "ref",
            "expires_at": 1.0,
            "token_type": "Bearer",
            "scopes": [],
        },
    )
    s._write_secret(
        s._reg_path(),
        {
            "client_id": "cid",
            "client_secret": "sec",
            "client_secret_expires_at": 9_999_999_999,
            "scopes": [],
        },
    )
    result = s.refresh_token()
    assert result is True
    assert s._load_token()["access_token"] == "tok"


def test_ensure_valid_refreshes_expired(tmp_path, monkeypatch):
    s, _ = _setup(tmp_path, monkeypatch)
    s._write_secret(
        s._token_path(),
        {
            "access_token": "old",
            "refresh_token": "ref",
            "expires_at": 1.0,
            "token_type": "Bearer",
            "scopes": [],
        },
    )
    s._write_secret(
        s._reg_path(),
        {
            "client_id": "cid",
            "client_secret": "sec",
            "client_secret_expires_at": 9_999_999_999,
            "scopes": [],
        },
    )
    result = s.ensure_valid()
    assert result is True


def test_start_login_already_authenticated(tmp_path, monkeypatch):
    s, _ = _setup(tmp_path, monkeypatch)
    import time

    s._write_secret(
        s._token_path(),
        {
            "access_token": "tok",
            "refresh_token": "ref",
            "expires_at": time.time() + 3600,
            "token_type": "Bearer",
            "scopes": [],
        },
    )
    result = s.start_login()
    assert result.get("already_authenticated") is True


def test_poll_loop_rereads_persisted_interval(tmp_path, monkeypatch):
    """M13: _poll_loop must re-read the persisted interval each pass, so a
    slow_down bump from get_status() (same or another process) is honoured."""
    import time as _time

    s, _ = _setup(tmp_path, monkeypatch)
    flow = {
        "device_code": "dc",
        "interval": 1,
        "started_at": _time.time(),
        "expires_in": 600,
    }
    reg = {"client_id": "cid", "client_secret": "csecret", "scopes": []}
    s._save_flow(flow)

    # Simulate a slow_down bump made by another path: raise the persisted interval.
    flow["interval"] = 42
    s._save_flow(flow)

    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        s._stop.set()  # break the loop after one iteration

    monkeypatch.setattr(s.time, "sleep", fake_sleep)
    monkeypatch.setattr(s, "_poll_once", lambda r, f: "pending")

    s._poll_loop(reg, flow)

    # Honoured the re-read interval (42), not the stale local 1.
    assert sleeps == [42]


def test_get_status_skips_poll_when_thread_alive(tmp_path, monkeypatch):
    """M13: get_status() must not double-poll when a background thread is already
    driving the flow."""
    import time as _time

    s, _ = _setup(tmp_path, monkeypatch)
    s._write_secret(
        s._reg_path(),
        {
            "client_id": "cid",
            "client_secret": "csecret",
            "client_secret_expires_at": 9_999_999_999,
            "scopes": [],
        },
    )
    s._save_flow(
        {
            "device_code": "dc",
            "user_code": "UC",
            "verification_uri_complete": "https://x",
            "expires_in": 600,
            "interval": 1,
            "started_at": _time.time(),
            "phase": "awaiting_approval",
        }
    )

    # A "live" poll thread exists.
    s._poll_thread = types.SimpleNamespace(is_alive=lambda: True)

    poll_calls = []

    def fake_poll_once(reg, flow):
        poll_calls.append(1)
        return "pending"

    monkeypatch.setattr(s, "_poll_once", fake_poll_once)

    st = s.get_status()

    assert poll_calls == []  # no manual double-poll
    assert st["phase"] == "awaiting_approval"


def test_start_login_starts_flow_when_token_expired(tmp_path, monkeypatch):
    """L8: an expired, non-refreshable token must not short-circuit start_login."""
    import time as _time

    s, _ = _setup(tmp_path, monkeypatch)
    s._write_secret(
        s._token_path(),
        {
            "access_token": "expired-tok",
            "expires_at": _time.time() - 60,  # expired; no refresh_token
            "token_type": "Bearer",
            "scopes": [],
        },
    )
    monkeypatch.setattr(s, "_start_poll_thread", lambda reg, flow: None)

    result = s.start_login()

    assert "already_authenticated" not in result
    assert result.get("user_code") == "UC-1234"
    assert result.get("verification_uri") == "https://example.com"


def test_refresh_token_does_not_retry_invalid_grant(tmp_path, monkeypatch):
    """L8: a terminal InvalidGrantException returns False immediately (no 3x retry)."""
    s, be = _setup(tmp_path, monkeypatch)
    s._write_secret(
        s._token_path(),
        {
            "access_token": "expired-tok",
            "refresh_token": "dead-rtok",
            "expires_at": 1.0,
            "token_type": "Bearer",
            "scopes": [],
        },
    )
    s._write_secret(
        s._reg_path(),
        {
            "client_id": "cid",
            "client_secret": "csecret",
            "client_secret_expires_at": 9_999_999_999,
            "scopes": [],
        },
    )

    calls = {"n": 0}

    class _InvalidGrantClient:
        def create_token(self, **kw):
            calls["n"] += 1
            raise be.ClientError("InvalidGrantException")

    s._cached_client = _InvalidGrantClient()

    result = s.refresh_token()

    assert result is False
    assert calls["n"] == 1  # terminal error: no retry
