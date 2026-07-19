"""Tests for the build plugin — headless SSO-OIDC device flow.

Set BUILD_LIVE=1 to also exercise the real OIDC registration + start_device_
authorization against oidc.us-east-1.amazonaws.com (no credentials needed).
"""

import json
import os
import time
import types
from unittest import mock

import pytest


# --- adapter (OpenAI-compatible front-end) ---

def test_adapter_translates_openai_request_to_q(monkeypatch):
    """The adapter must accept an OpenAI-shape /v1/chat/completions request,
    flatten `messages` into one prompt, and call backend.chat() exactly once
    with that prompt + the requested model. This is the contract that lets
    Hermes treat aws-build as a selectable chat model (Way A) without the
    old standalone :8088 bridge."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")

    calls = {}

    def fake_chat(prompt, model="auto", conversation_id=None, **kw):
        calls["prompt"] = prompt
        calls["model"] = model
        return ("Hello from Q", None, None)

    monkeypatch.setattr(backend, "chat", fake_chat)
    monkeypatch.setattr(adapter, "backend", backend)

    body = {
        "model": "claude-sonnet-4.5",
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "Reply"},
            {"role": "user", "content": "Now answer this."},
        ],
        "stream": True,
    }
    out = adapter._handle_chat(body)
    text = out.decode("utf-8")
    assert "Hello from Q" in text
    assert "data: [DONE]" in text
    # flattened: system prepended, last user turn is the actual ask
    assert calls["prompt"].startswith("System: Be terse.")
    assert calls["prompt"].endswith("Now answer this.")
    assert calls["model"] == "claude-sonnet-4.5"


def test_adapter_sse_shape(monkeypatch):
    """Output frames must be OpenAI SSE: a role frame, a content frame,
    then [DONE] — so Hermes's openai_chat transport can parse it."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    monkeypatch.setattr(backend, "chat", lambda *a, **k: ("x", None, None))
    monkeypatch.setattr(adapter, "backend", backend)

    out = adapter._handle_chat({"messages": [{"role": "user", "content": "hi"}]})
    frames = [l for l in out.decode().splitlines() if l.startswith("data:")]
    assert len(frames) == 3
    assert "assistant" in frames[0]
    assert '"content": "x"' in frames[1]
    assert frames[2] == "data: [DONE]"


def test_adapter_surfaces_chat_errors_as_sse(monkeypatch):
    """When backend.chat() raises (e.g. token missing), the adapter must
    return an OpenAI-style error frame, not crash the HTTP handler."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    monkeypatch.setattr(backend, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("No valid Amazon Q token available")))
    monkeypatch.setattr(adapter, "backend", backend)

    out = adapter._handle_chat({"messages": [{"role": "user", "content": "hi"}]})
    assert "No valid Amazon Q token available" in out.decode()
    assert "data: [DONE]" in out.decode()

import pytest

from conftest import load_plugin


@pytest.fixture(scope="module")
def mod():  # noqa: ANN
    return load_plugin()


def test_register_defines_tools(mod):  # noqa: ANN
    captured = {}
    ctx = types.SimpleNamespace(
        register_tool=lambda **kw: captured.update({kw["name"]: kw}),
        register_hook=lambda *a, **k: None,
    )
    mod.register(ctx)
    assert {"bid_login", "bid_status", "bid_show_identity", "bid_logout"}.issubset(
        captured
    )


def test_handlers_return_success_json(mod):  # noqa: ANN
    for name in ["bid_login", "bid_status", "bid_show_identity", "bid_logout"]:
        pass  # login/status run live below; logout/identity are no-ops here
    res = json.loads(mod._handle_bid_status({}))
    assert "success" in res


def test_no_secrets_in_output(mod):  # noqa: ANN
    for fn in (mod._handle_bid_status, mod._handle_bid_show_identity):
        blob = json.dumps(json.loads(fn({})))
        assert "access_token" not in blob
        assert "client_secret" not in blob


@pytest.mark.skipif(os.environ.get("BUILD_LIVE") != "1", reason="set BUILD_LIVE=1 for live OIDC")
def test_live_device_start(mod):  # noqa: ANN
    res = json.loads(mod._handle_bid_login({}))
    assert res["success"] is True
    assert res["user_code"]
    assert res["verification_uri_complete"].startswith("https://view.awsapps.com/start")
    # Poll will report pending/unauthenticated without human approval
    st = json.loads(mod._handle_bid_status({}))
    assert st["success"] is True
    assert st["phase"] in ("awaiting_approval", "authenticated", "expired", "error")


def test_mirror_path_prefers_canonical_aws_build(monkeypatch, tmp_path):
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Canonical (aws-build) file present -> read resolves to it.
    canonical = tmp_path / "plugins" / "aws-build" / ".bid_token.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("{}")
    assert sso_oidc._token_path() == canonical
    # _canonical_path always points at aws-build regardless of what exists.
    assert sso_oidc._canonical_path(".bid_token.json") == canonical


def test_mirror_path_ignores_legacy_build_dir(monkeypatch, tmp_path):
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # A token left in the old plugins/build dir must NOT be picked up; the
    # resolved path stays canonical (aws-build) so state lives in one place.
    legacy = tmp_path / "plugins" / "build" / ".bid_token.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}")
    assert sso_oidc._token_path() == (
        tmp_path / "plugins" / "aws-build" / ".bid_token.json"
    )


# --- get_status must report the NEWEST valid token (not a stale pool entry) ---
# Regression: a still-valid but older pool token used to shadow a fresh
# .bid_token.json from a re-auth on another account.

def test_get_status_prefers_newest_valid_token(monkeypatch, tmp_path):
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "aws-build"
    base.mkdir(parents=True)
    old = {"access_token": "OLD", "expires_at": time.time() + 3600}
    new = {"access_token": "NEW", "expires_at": time.time() + 7200}
    # .bid_token.json carries the NEWER valid token (single store; no pool).
    (base / ".bid_token.json").write_text(json.dumps(new))

    st = sso_oidc.get_status()
    assert st["authenticated"] is True
    # identity reflects the token from .bid_token.json.
    assert st["token_expires_at"] == new["expires_at"]


def test_get_status_falls_back_when_no_valid_token(monkeypatch, tmp_path):
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sso_oidc, "_load_token", lambda: None)
    monkeypatch.setattr(sso_oidc, "_load_flow", lambda: None)
    st = sso_oidc.get_status()
    assert st["authenticated"] is False
    assert st["phase"] == "idle"


def test_get_status_refreshes_expired_token(monkeypatch, tmp_path):
    """get_status() must silently refresh an expired access token (when a
    refresh token exists) and report authenticated, flagging `refreshed`."""
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "aws-build"
    base.mkdir(parents=True)
    # Expired access token but with a usable refresh token on disk.
    (base / ".bid_token.json").write_text(
        json.dumps(
            {"access_token": "EXPIRED", "refresh_token": "R", "expires_at": time.time() - 10}
        )
    )

    refreshed = {"called": False}

    def fake_refresh():
        refreshed["called"] = True
        # Simulate a successful refresh: write a fresh, valid token.
        (base / ".bid_token.json").write_text(
            json.dumps(
                {"access_token": "NEW", "refresh_token": "R", "expires_at": time.time() + 3600}
            )
        )
        return True

    monkeypatch.setattr(sso_oidc, "refresh_token", fake_refresh)
    monkeypatch.setattr(sso_oidc, "_load_flow", lambda: None)

    st = sso_oidc.get_status()
    assert refreshed["called"] is True
    assert st["authenticated"] is True
    assert st["refreshed"] is True


def test_get_status_reports_expired_when_refresh_dead(monkeypatch, tmp_path):
    """If the access token is expired and refresh fails, get_status() must NOT
    claim authenticated — it reports expired (refreshed: False)."""
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "aws-build"
    base.mkdir(parents=True)
    (base / ".bid_token.json").write_text(
        json.dumps(
            {"access_token": "EXPIRED", "refresh_token": "R", "expires_at": time.time() - 10}
        )
    )

    monkeypatch.setattr(sso_oidc, "refresh_token", lambda: False)
    monkeypatch.setattr(sso_oidc, "_load_flow", lambda: None)

    st = sso_oidc.get_status()
    assert st["authenticated"] is False
    assert st["refreshed"] is False
    # Contract: a token that existed but couldn't be refreshed must report
    # phase == "expired" (not "idle"), so the card shows the real state.
    assert st["phase"] == "expired"


def test_get_status_no_refresh_when_valid(monkeypatch, tmp_path):
    """get_status() must NOT attempt a refresh when the stored token is still
    valid — only when it is expired."""
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "aws-build"
    base.mkdir(parents=True)
    (base / ".bid_token.json").write_text(
        json.dumps(
            {"access_token": "OK", "refresh_token": "R", "expires_at": time.time() + 3600}
        )
    )

    refresh_called = {"called": False}
    monkeypatch.setattr(sso_oidc, "refresh_token", lambda: refresh_called.__setitem__("called", True))
    monkeypatch.setattr(sso_oidc, "_load_flow", lambda: None)

    st = sso_oidc.get_status()
    assert st["authenticated"] is True
    assert refresh_called["called"] is False


def test_get_status_expired_no_refresh_token(monkeypatch, tmp_path):
    """An expired token with NO refresh token must report not-authenticated
    without attempting a refresh."""
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "aws-build"
    base.mkdir(parents=True)
    (base / ".bid_token.json").write_text(
        json.dumps({"access_token": "OLD", "expires_at": time.time() - 10})  # no refresh_token
    )

    refresh_called = {"called": False}
    monkeypatch.setattr(sso_oidc, "refresh_token", lambda: refresh_called.__setitem__("called", True))
    monkeypatch.setattr(sso_oidc, "_load_flow", lambda: None)

    st = sso_oidc.get_status()
    assert st["authenticated"] is False
    assert st["phase"] == "expired"
    assert refresh_called["called"] is False


def test_get_token_refresh_persists_to_origin_store(monkeypatch, tmp_path):
    """Regression: when the expired token came from the sso store
    (.bid_token.json), get_token() must refresh via sso_oidc.refresh_token()
    so the refreshed token is written BACK to .bid_token.json — NOT to the
    legacy .q_token.json (split-brain + redundant double-refresh bug)."""
    import backend
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "aws-build"
    base.mkdir(parents=True)
    sso_file = base / ".bid_token.json"
    q_file = base / ".q_token.json"
    sso_file.write_text(
        json.dumps(
            {"access_token": "OLD", "refresh_token": "R", "expires_at": time.time() - 10}
        )
    )

    def fake_sso_refresh():
        # Simulate a successful sso refresh writing a fresh token to .bid_token.json.
        sso_file.write_text(
            json.dumps(
                {"access_token": "NEW", "refresh_token": "R", "expires_at": time.time() + 3600}
            )
        )
        return True

    monkeypatch.setattr(sso_oidc, "refresh_token", fake_sso_refresh)
    # Ensure backend._refresh (which would write .q_token.json) is NOT used.
    monkeypatch.setattr(backend, "_refresh", lambda c: (_ for _ in ()).throw(AssertionError("backend._refresh must not run for sso token")))

    tok = backend.get_token()
    assert tok["access_token"] == "NEW"
    assert sso_file.exists()
    assert "expires_at" in json.loads(sso_file.read_text())
    assert not q_file.exists(), "get_token() must not write .q_token.json for an sso-origin token"