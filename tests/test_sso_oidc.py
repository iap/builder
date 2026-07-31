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
        "accessToken": "tok", "expiresIn": 3600,
        "refreshToken": "ref", "tokenType": "Bearer",
    }

    class _Client:
        def register_client(self, **kw):
            return {"clientId": "cid", "clientSecret": "sec", "clientSecretExpiresAt": 9_999_999_999}

        def start_device_authorization(self, **kw):
            return {
                "deviceCode": "dc", "userCode": "UC-1234",
                "verificationUri": "https://example.com",
                "verificationUriComplete": "https://example.com?code=UC-1234",
                "expiresIn": 600, "interval": 1,
            }

        def create_token(self, **kw):
            if callable(_token_result):
                return _token_result()
            return _token_result

    class _Session:
        get_credentials = None
        def create_client(self, *a, **kw): return _Client()

    bs.get_session = lambda: _Session()
    bc.session = bs
    bc.exceptions = be
    bc.config = bcfg
    for k, v in [("botocore", bc), ("botocore.session", bs),
                 ("botocore.exceptions", be), ("botocore.config", bcfg)]:
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
        def create_token(self, **kw): raise be.ClientError("AuthorizationPendingException")
    s._cached_client = _PendingClient()
    result = s._poll_once(
        {"client_id": "cid", "client_secret": "sec", "scopes": []},
        {"device_code": "dc"},
    )
    assert result == "pending"


def test_poll_once_slow_down(tmp_path, monkeypatch):
    s, be = _setup(tmp_path, monkeypatch)
    class _SlowClient:
        def create_token(self, **kw): raise be.ClientError("SlowDownException")
    s._cached_client = _SlowClient()
    result = s._poll_once(
        {"client_id": "cid", "client_secret": "sec", "scopes": []},
        {"device_code": "dc"},
    )
    assert result == "slow_down"


def test_poll_once_invalid_grant_no_token(tmp_path, monkeypatch):
    s, be = _setup(tmp_path, monkeypatch)
    class _InvalidClient:
        def create_token(self, **kw): raise be.ClientError("InvalidGrantException")
    s._cached_client = _InvalidClient()
    result = s._poll_once(
        {"client_id": "cid", "client_secret": "sec", "scopes": []},
        {"device_code": "dc"},
    )
    assert result == "error:InvalidGrantException"


def test_poll_once_network_error(tmp_path, monkeypatch):
    s, be = _setup(tmp_path, monkeypatch)
    class _NetErrClient:
        def create_token(self, **kw): raise be.EndpointConnectionError("net")
    s._cached_client = _NetErrClient()
    result = s._poll_once(
        {"client_id": "cid", "client_secret": "sec", "scopes": []},
        {"device_code": "dc"},
    )
    assert result == "error:poll_network_error"


def test_refresh_token_success(tmp_path, monkeypatch):
    s, _ = _setup(tmp_path, monkeypatch)
    # Write an expired token with a refresh token
    s._write_secret(s._token_path(), {
        "access_token": "old", "refresh_token": "ref",
        "expires_at": 1.0, "token_type": "Bearer", "scopes": [],
    })
    s._write_secret(s._reg_path(), {
        "client_id": "cid", "client_secret": "sec",
        "client_secret_expires_at": 9_999_999_999, "scopes": [],
    })
    result = s.refresh_token()
    assert result is True
    assert s._load_token()["access_token"] == "tok"


def test_ensure_valid_refreshes_expired(tmp_path, monkeypatch):
    s, _ = _setup(tmp_path, monkeypatch)
    s._write_secret(s._token_path(), {
        "access_token": "old", "refresh_token": "ref",
        "expires_at": 1.0, "token_type": "Bearer", "scopes": [],
    })
    s._write_secret(s._reg_path(), {
        "client_id": "cid", "client_secret": "sec",
        "client_secret_expires_at": 9_999_999_999, "scopes": [],
    })
    result = s.ensure_valid()
    assert result is True


def test_start_login_already_authenticated(tmp_path, monkeypatch):
    s, _ = _setup(tmp_path, monkeypatch)
    import time
    s._write_secret(s._token_path(), {
        "access_token": "tok", "refresh_token": "ref",
        "expires_at": time.time() + 3600, "token_type": "Bearer", "scopes": [],
    })
    result = s.start_login()
    assert result.get("already_authenticated") is True
