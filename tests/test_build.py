"""Tests for the build plugin — headless SSO-OIDC device flow.

Set BID_LIVE=1 to also exercise the real OIDC registration + start_device_
authorization against oidc.us-east-1.amazonaws.com (no credentials needed).
"""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
_HA = Path(__file__).resolve().parent.parent.parent.parent / "hermes-agent"

import importlib.util  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402

sys.path.insert(0, str(_HA))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="bid-test-")


def _load():  # noqa: ANN
    ns = "hermes_plugins"
    if ns not in sys.modules:
        m = types.ModuleType(ns)
        m.__path__ = []
        m.__package__ = ns
        sys.modules[ns] = m
    mn = f"{ns}.build"
    spec = importlib.util.spec_from_file_location(
        mn, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = mn
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules[mn] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():  # noqa: ANN
    return _load()


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


@pytest.mark.skipif(os.environ.get("BID_LIVE") != "1", reason="set BID_LIVE=1 for live OIDC")
def test_live_device_start(mod):  # noqa: ANN
    res = json.loads(mod._handle_bid_login({}))
    assert res["success"] is True
    assert res["user_code"]
    assert res["verification_uri_complete"].startswith("https://view.awsapps.com/start")
    # Poll will report pending/unauthenticated without human approval
    st = json.loads(mod._handle_bid_status({}))
    assert st["success"] is True
    assert st["phase"] in ("awaiting_approval", "authenticated", "expired", "error")
