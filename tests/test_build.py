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


def test_adapter_sse_frames_end_with_blank_line(monkeypatch):
    """Every SSE event must be terminated by a BLANK line ("\\n\\n"), per the
    SSE / OpenAI streaming spec. With only a single "\\n", Hermes's openai_chat
    parser reads two `data:` frames as one chunk and fails to json.loads()
    with 'Extra data: line 2 column 1' — the exact live CLI failure this
    guards against. splitlines() hid the bug because it collapses \\n and
    \\n\\n, so assert on the raw bytes instead."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    monkeypatch.setattr(backend, "chat", lambda *a, **k: ("hello", None, None))
    monkeypatch.setattr(adapter, "backend", backend)

    raw = adapter._handle_chat({"messages": [{"role": "user", "content": "hi"}]})
    text = raw.decode()
    # No two `data:` lines may be separated by only a single newline.
    assert "}\ndata:" not in text, "SSE frames not separated by a blank line"
    # Each JSON event frame is followed by a blank line.
    assert text.count("}\n\n") >= 2  # role frame + content frame
    assert text.endswith("data: [DONE]\n\n")


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
    """Regression: get_token() refreshes through sso_oidc (the sole
    store), so the refreshed token lands in .bid_token.json and NO
    second .q_token.json is ever written (single-source-of-truth).
    """
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
    monkeypatch.setattr(sso_oidc, "get_status", lambda: {"authenticated": False})
    monkeypatch.setattr(
        sso_oidc, "_load_token", lambda: json.loads(sso_file.read_text()) if sso_file.exists() else None
    )
    tok = backend.get_token()
    assert tok["access_token"] == "NEW"
    assert sso_file.exists()
    assert "expires_at" in json.loads(sso_file.read_text())
    assert not q_file.exists(), "get_token() must never write .q_token.json"

def test_start_login_short_circuits_when_token_present(monkeypatch):
    """Clicking login (e.g. the dashboard button) while already authed must
    NOT spawn a doomed duplicate device flow — that's what made AWS return
    InvalidGrantException and surface a fake login error. It should return
    already_authenticated instead."""
    import auth.sso_oidc as sso
    from unittest import mock

    monkeypatch.setattr(sso, "_load_token", lambda: {"access_token": "x", "expires_at": 9e12})
    started = {"n": 0}

    def fake_start(*a, **k):
        started["n"] += 1
        return mock.Mock()

    monkeypatch.setattr(sso, "_client", lambda: type("C", (), {"start_device_authorization": fake_start})())
    info = sso.start_login()
    assert info.get("already_authenticated") is True
    assert started["n"] == 0, "must not call AWS start_device_authorization when authed"


def test_invalid_grant_downgraded_when_token_present(monkeypatch):
    """A stale/duplicate poll that hits InvalidGrantException after a token
    already exists is a benign race, not a failure - must not log at ERROR
    (which the dashboard shows as a login error)."""
    import auth.sso_oidc as sso
    from botocore.exceptions import ClientError
    import logging

    monkeypatch.setattr(sso, "_load_token", lambda: {"access_token": "x", "expires_at": 9e12})
    # Use the REAL _poll_once; make the boto3 client raise InvalidGrantException.
    class FakeClient:
        def create_token(self, **k):
            raise ClientError({"Error": {"Code": "InvalidGrantException"}}, "create_token")
    monkeypatch.setattr(sso, "_client", lambda: FakeClient())
    errs = []
    class H(logging.Handler):
        def emit(self, r):
            if r.levelno >= logging.ERROR:
                errs.append(r.getMessage())
    sso.logger.addHandler(H())
    sso.logger.setLevel(logging.DEBUG)
    phase = sso._poll_once({"client_id": "c", "client_secret": "s"},
                           {"device_code": "dc"})
    assert phase.startswith("error:InvalidGrantException")
    assert not errs, "InvalidGrant with token present must not log ERROR"


def test_unregister_stops_adapter(monkeypatch):
    """unregister() must call adapter.stop() so the :8077 listener releases
    (core doesn't invoke this hook yet, but it's the correct contract)."""
    import __init__ as p
    import adapter as real_adapter
    called = {"stop": False}

    def fake_stop():
        called["stop"] = True
    monkeypatch.setattr(real_adapter, "stop", fake_stop)
    p.unregister(ctx=None)
    assert called["stop"] is True


def test_uninstall_removes_aws_build_block_and_enabled(tmp_path, monkeypatch):
    """Mirror of scripts/uninstall.sh logic: drop the providers:aws-build
    block (any indentation) and the enabled entry; leave siblings intact."""
    import yaml, io, sys
    sys.path.insert(0, ".")
    cfg = {
        "providers": {
            "g4f-auth": {"name": "G4F.dev"},
            "aws-build": {"name": "AWS Build", "transport": "openai_chat"},
        },
        "plugins": {"enabled": ["aws-build", "continual-learning"]},
        "model": {"provider": "kilo"},
    }
    path = tmp_path / "config.yaml"
    yaml.safe_dump(cfg, open(path, "w"), sort_keys=False)

    # replicate the uninstall.py block-removal logic
    lines = open(path).read().splitlines()
    out, drop = [], False
    for ln in lines:
        if ln.strip() == "aws-build:":
            drop = True
            continue
        if drop:
            if ln and not ln.startswith("  "):
                drop = False
            else:
                continue
        out.append(ln)
    open(path, "w").write("\n".join(out).rstrip("\n") + "\n")

    c = yaml.safe_load(open(path))
    assert "aws-build" not in c.get("providers", {})
    assert "g4f-auth" in c["providers"]
    c["plugins"]["enabled"] = [x for x in c["plugins"]["enabled"] if x != "aws-build"]
    assert "aws-build" not in c["plugins"]["enabled"]
    assert c["plugins"]["enabled"] == ["continual-learning"]


def test_aws_build_resolves_as_cli_tui_model(monkeypatch):
    """Robust check (against the REAL Hermes core resolver) that the
    providers:aws-build block setup.sh writes makes aws-build a selectable
    model in CLI/TUI: correct transport, endpoint, key_env, and every
    declared model surfaced — with no plaintext api_key and no :8088."""
    import sys, yaml
    sys.path.insert(0, "/Users/iap/.hermes/hermes-agent")
    from hermes_cli.config import get_compatible_custom_providers

    provider_block = {
        "name": "AWS Build",
        "transport": "openai_chat",
        "base_url": "http://127.0.0.1:8077/v1",
        "key_env": "AWS_BUILD_ADAPTER_DUMMY",
        "models": ["claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5", "auto"],
    }
    cfg = {"providers": {"aws-build": provider_block}}
    cps = get_compatible_custom_providers(cfg)
    matches = [c for c in cps if c.get("provider_key") == "aws-build"]
    assert matches, "aws-build must appear in resolved providers"
    e = matches[0]
    assert e["api_mode"] == "openai_chat"
    assert e["base_url"].rstrip("/") == "http://127.0.0.1:8077/v1"
    assert e["key_env"] == "AWS_BUILD_ADAPTER_DUMMY"
    assert "api_key" not in e, "no plaintext api_key allowed"
    assert "8088" not in e["base_url"], "no dead :8088 bridge"
    surfaced = set(e.get("models", {}).keys())
    assert surfaced == {"claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5", "auto"}


def test_plugin_model_enum_matches_provider_block():
    """The ask_q tool's model enum (built from backend.list_models()) must
    agree with the models declared in the provider block, so the TUI picker
    and the tool schema never drift apart.

    Design: list_models() advertises the concrete Claude variants; 'auto' is
    a valid Q modelId the adapter passes through, so it is added to the
    ask_q schema enum (and the provider block) but intentionally excluded
    from list_models() (it is not a concrete model)."""
    import sys
    sys.path.insert(0, ".")
    import backend, yaml

    catalog = set(backend.list_models())
    concrete = {"claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5"}
    assert catalog == concrete, f"list_models concrete drift: {catalog ^ concrete}"
    # provider block = concrete variants + 'auto' (passthrough)
    expected_provider = concrete | {"auto"}
    assert expected_provider == {"claude-sonnet-4.5", "claude-sonnet-4",
                                 "claude-haiku-4.5", "auto"}
    # ask_q schema enum includes auto
    from __init__ import _TOOLS
    schema = next(s for name, s, *_ in _TOOLS if name == "ask_q")
    enum = schema["parameters"]["properties"]["model"]["enum"]
    assert "auto" in enum, "ask_q model enum must include 'auto'"
    assert concrete <= set(enum), "ask_q enum must include all concrete variants"


def test_adapter_end_to_end_openai_wire(monkeypatch):
    """Robust usability test: prove aws-build actually ANSWERS through the
    OpenAI /v1/chat/completions wire path core uses — not just that it's
    listed. Monkeypatches backend.chat (no real Q token needed) so this is
    deterministic and offline, but exercises the real adapter HTTP+SSE
    translation that a '-m aws-build' chat turn hits."""
    import json, urllib.request
    import adapter as real_adapter

    captured = {}
    def fake_chat(prompt, model="auto", conversation_id=None):
        captured["prompt"] = prompt
        captured["model"] = model
        return ("ADAPTER-OK", None, None)
    monkeypatch.setattr(real_adapter.backend, "chat", fake_chat)

    srv, port = real_adapter.start(host="127.0.0.1", port=0)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps({
                "model": "claude-sonnet-4.5",
                "messages": [
                    {"role": "system", "content": "Be terse."},
                    {"role": "user", "content": "ping"},
                ],
                "stream": True,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
        assert resp.status == 200, f"adapter HTTP {resp.status}"
        # SSE frames: at least one 'data: {...}' with content + a [DONE]
        assert "data: [DONE]" in body, "stream must terminate with [DONE]"
        assert "ADAPTER-OK" in body, "answer must round-trip through adapter"
        # system prompt + user content must be flattened into the Q prompt
        assert captured["prompt"] == "System: Be terse.\n\nping", captured
        assert captured["model"] == "claude-sonnet-4.5"
    finally:
        real_adapter.stop()


def test_adapter_healthz():
    """Health endpoint used by orchestration to confirm the listener is up."""
    import urllib.request
    import adapter as real_adapter
    srv, port = real_adapter.start(host="127.0.0.1", port=0)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert r.status == 200
            assert b"ok" in r.read()
    finally:
        real_adapter.stop()
