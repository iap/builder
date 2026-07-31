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


def test_pre_tool_call_blocks_hermes_core_path():
    import os

    import __init__ as plugin
    path = os.path.expanduser("~/.hermes/hermes-agent/foo.py")
    result = plugin._plugin_pre_tool_call("write_file", {"path": path})
    assert result is not None
    assert result["action"] == "block"


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
            payload = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=payload,
                headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
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
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
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

    with patch("auth.sso_oidc.start_login", return_value={
        "user_code": "ABC-123",
        "verification_uri_complete": "https://device.sso.example.com/",
        "expires_in": 600,
        "interval": 0,
    }), patch("auth.sso_oidc.get_status", side_effect=fake_get_status), patch("time.sleep"):
        rc = cmd_login(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Authenticated" in out


def test_cmd_login_error_phase(capsys):
    from build_cli import build_parser, cmd_login
    args = build_parser().parse_args(["login"])
    with patch("auth.sso_oidc.start_login", return_value={
        "user_code": "ABC-123",
        "verification_uri_complete": "https://device.sso.example.com/",
        "expires_in": 600,
        "interval": 0,
    }), patch("auth.sso_oidc.get_status", return_value={"authenticated": False, "phase": "error", "error": "bad"}), patch("time.sleep"):
        rc = cmd_login(args)
    assert rc == 1


def test_cmd_whoami_not_authenticated(capsys):
    from build_cli import build_parser, cmd_whoami
    args = build_parser().parse_args(["whoami"])
    with patch("auth.sso_oidc.show_identity", return_value={"authenticated": False}):
        rc = cmd_whoami(args)
    assert rc == 1
    assert "not authenticated" in capsys.readouterr().out
