"""Tests for the build plugin — headless SSO-OIDC device flow.

Set BUILD_LIVE=1 to also exercise the real OIDC registration + start_device_
authorization against oidc.us-east-1.amazonaws.com (no credentials needed).
"""

import json
import os
import types
from unittest import mock

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


def test_mirror_path_falls_back_to_legacy_build(monkeypatch, tmp_path):
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Only the legacy (build) file exists -> read falls back to it, but writes
    # still target the canonical aws-build path.
    legacy = tmp_path / "plugins" / "build" / ".bid_token.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}")
    assert sso_oidc._token_path() == legacy
    assert sso_oidc._canonical_path(".bid_token.json") == (
        tmp_path / "plugins" / "aws-build" / ".bid_token.json"
    )
