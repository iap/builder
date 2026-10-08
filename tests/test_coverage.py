"""Targeted tests for uncovered paths in __init__, adapter, build_cli."""

# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

# --- __init__.py: pre_tool_call guard ---


def test_pre_tool_call_blocks_rm_rf():
    import __init__ as plugin

    result = plugin._plugin_pre_tool_call("terminal", {"command": "rm -rf /tmp/x"})
    assert result is not None
    assert result["action"] == "block"


def test_pre_tool_call_blocks_sudo():
    import __init__ as plugin

    result = plugin._plugin_pre_tool_call("terminal", {"command": "sudo apt install x"})
    assert result is not None
    assert result["action"] == "block"


def test_pre_tool_call_escalates_hermes_core_path_to_approval():
    """Protected-path writes escalate to the human-approval gate.

    The guard no longer hard-blocks here: it returns ``approve`` and core's
    dispatch layer runs the same human gate as a Tier-2 dangerous command
    (``request_tool_approval``), failing CLOSED when no human is present. The
    destructive-command and privilege-escalation guards above stay hard blocks.
    """
    import os

    import __init__ as plugin

    path = os.path.expanduser("~/.hermes/hermes-agent/foo.py")
    result = plugin._plugin_pre_tool_call("write_file", {"path": path})
    assert result is not None
    assert result["action"] == "approve"
    # rule_key namespaces the [a]lways allowlist grain so plugin approvals never
    # collide with a real command-pattern key.
    assert result["rule_key"] == "builder-guard"
    # The message must name the target so the approver sees what they are allowing.
    assert path in result["message"]


def test_pre_tool_call_allows_sibling_of_protected_path():
    """A sibling like ~/.hermes/config.yaml.bak is NOT protected.

    Guards the path-component comparison against regressing to a prefix match,
    which would wrongly block every file whose name merely starts with a
    protected name.
    """
    import os

    import __init__ as plugin

    sibling = os.path.expanduser("~/.hermes/config.yaml.bak")
    assert plugin._plugin_pre_tool_call("write_file", {"path": sibling}) is None


def test_spawn_provider_registration_returns_before_slow_write_completes():
    """_spawn_provider_registration() must hand the slow work to a thread.

    _provider.register_provider() lazily imports hermes_cli.config, measured at
    4.7s-12.6s cold on this host, and Hermes caps import + register() at
    plugins.load_timeout_seconds (default 10s). Paying that inline makes the
    plugin load non-deterministic: under CPU pressure the load times out and the
    plugin's tools vanish for the session (issue #115).
    """
    import sys
    import threading
    import time
    import types

    import __init__ as plugin

    started = threading.Event()
    finished = threading.Event()

    class _SlowProvider:
        register_provider = staticmethod(
            lambda port: (started.set(), time.sleep(0.5), finished.set())
        )

    stub = types.SimpleNamespace(register_provider=_SlowProvider.register_provider)
    t0 = time.perf_counter()
    with patch.dict(sys.modules, {"_provider": stub}):
        plugin._spawn_provider_registration(18092)
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.1, f"spawn blocked the caller for {elapsed:.3f}s"
    assert started.wait(timeout=5), "background registration never started"
    assert finished.wait(timeout=5), "background registration never completed"


def test_register_does_not_call_register_provider_inline(monkeypatch):
    """register() hands off to _spawn_provider_registration, not register_provider.

    Pins the actual call site in register(): if someone inlines
    _provider.register_provider(actual) back into the deadline path, this fails
    because the spy on the spawn helper never fires.
    """
    import sys
    import types

    import __init__ as plugin

    spawned = []

    adapter_stub = types.SimpleNamespace(
        HOST="localhost",
        start=lambda port=8088: (object(), 18092),
        is_running=lambda host="localhost", port=8088: False,
        stop=lambda: None,
    )
    ctx = types.SimpleNamespace(
        register_tool=lambda **kw: None,
        register_hook=lambda *a, **k: None,
    )

    monkeypatch.setitem(sys.modules, "adapter", adapter_stub)
    monkeypatch.setattr(plugin, "_spawn_provider_registration", spawned.append)
    monkeypatch.setattr(plugin, "_registered", False, raising=False)

    plugin.register(ctx)

    assert spawned == [18092], (
        "register() must hand the provider write to _spawn_provider_registration; "
        f"got {spawned}"
    )
    monkeypatch.setattr(plugin, "_registered", False, raising=False)


def test_pre_tool_call_allows_safe():
    import __init__ as plugin

    result = plugin._plugin_pre_tool_call("terminal", {"command": "echo hello"})
    assert result is None


# --- adapter.py: HTTP handler via live server ---


def test_handler_get_healthz():
    import urllib.request

    from adapter import start, stop

    try:
        _srv, port = start(port=0)  # OS picks free port
        url = f"http://127.0.0.1:{port}/healthz"
        with urllib.request.urlopen(url, timeout=2) as r:
            body = json.loads(r.read())
        assert body["status"] == "ok"
    finally:
        stop()


def test_handler_get_unknown_path():
    import urllib.error
    import urllib.request

    from adapter import start, stop

    try:
        _srv, port = start(port=0)
        url = f"http://127.0.0.1:{port}/unknown"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url, timeout=2)
        assert exc_info.value.code == 404
    finally:
        stop()


def test_handler_post_chat_completions():
    import urllib.request

    from adapter import start, stop

    with patch("backend.chat", return_value=("hello", "", "")):
        try:
            _srv, port = start(port=0)
            payload = json.dumps(
                {"messages": [{"role": "user", "content": "hi"}]}
            ).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                body = r.read()
            assert b"[DONE]" in body
        finally:
            stop()


def test_handler_post_unknown_path():
    import urllib.error
    import urllib.request

    from adapter import start, stop

    try:
        _srv, port = start(port=0)
        payload = b"{}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/unknown",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2)
        assert exc_info.value.code == 404
    finally:
        stop()


# --- build_cli.py: polling loop and edge cases ---


def test_cmd_login_polls_until_authenticated(capsys):

    from build_cli import build_parser, cmd_login

    args = build_parser().parse_args(["login"])
    call_count = 0

    def fake_get_status():
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            return {"authenticated": True, "token_expires_at": 9999999999.0}
        return {"authenticated": False, "phase": "awaiting_approval"}

    with (
        patch(
            "auth.sso_oidc.start_login",
            return_value={
                "user_code": "ABC-123",
                "verification_uri_complete": "https://device.sso.example.com/",
                "expires_in": 600,
                "interval": 0,
            },
        ),
        patch("auth.sso_oidc.get_status", side_effect=fake_get_status),
        patch("time.sleep"),
    ):
        rc = cmd_login(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Authenticated" in out


def test_cmd_login_error_phase(capsys):
    from build_cli import build_parser, cmd_login

    args = build_parser().parse_args(["login"])
    with (
        patch(
            "auth.sso_oidc.start_login",
            return_value={
                "user_code": "ABC-123",
                "verification_uri_complete": "https://device.sso.example.com/",
                "expires_in": 600,
                "interval": 0,
            },
        ),
        patch(
            "auth.sso_oidc.get_status",
            return_value={"authenticated": False, "phase": "error", "error": "bad"},
        ),
        patch("time.sleep"),
    ):
        rc = cmd_login(args)
    assert rc == 1


def test_cmd_whoami_not_authenticated(capsys):
    from build_cli import build_parser, cmd_whoami

    args = build_parser().parse_args(["whoami"])
    with patch("auth.sso_oidc.show_identity", return_value={"authenticated": False}):
        rc = cmd_whoami(args)
    assert rc == 1
    assert "not authenticated" in capsys.readouterr().out
