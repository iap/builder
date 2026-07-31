# SPDX-License-Identifier: MIT
"""Tests for build_cli.py — parser, command handlers, fmt helpers."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_fmt_iso_none():
    from build_cli import _fmt_iso

    assert _fmt_iso(None) == "n/a"
    assert _fmt_iso(0) == "n/a"


def test_fmt_iso_timestamp():
    from build_cli import _fmt_iso

    result = _fmt_iso(0.0 + 1)
    assert "1970" in result  # epoch + 1s is 1970-01-01


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_build_parser_subcommands():
    from build_cli import build_parser

    p = build_parser()
    for cmd in ("login", "status", "whoami", "logout", "models"):
        args = p.parse_args([cmd])
        assert args.command == cmd


def test_build_parser_no_subcommand_exits():
    from build_cli import build_parser

    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args([])


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


def test_cmd_status_authenticated(capsys):
    from build_cli import cmd_status

    sso = MagicMock()
    sso.get_status.return_value = {"authenticated": True, "token_expires_at": None}
    with patch("build_cli._load_modules", return_value=(sso, None, None)):
        rc = cmd_status(argparse.Namespace())
    assert rc == 0
    assert "authenticated: yes" in capsys.readouterr().out


def test_cmd_status_awaiting(capsys):
    from build_cli import cmd_status

    sso = MagicMock()
    sso.get_status.return_value = {
        "authenticated": False,
        "phase": "awaiting_approval",
        "verification_uri_complete": "https://example.com/activate",
        "user_code": "ABCD-1234",
    }
    with patch("build_cli._load_modules", return_value=(sso, None, None)):
        rc = cmd_status(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "awaiting" in out
    assert "ABCD-1234" in out


def test_cmd_status_not_authenticated(capsys):
    from build_cli import cmd_status

    sso = MagicMock()
    sso.get_status.return_value = {
        "authenticated": False,
        "phase": "idle",
        "error": None,
    }
    with patch("build_cli._load_modules", return_value=(sso, None, None)):
        rc = cmd_status(argparse.Namespace())
    assert rc == 0
    assert "authenticated: no" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_whoami
# ---------------------------------------------------------------------------


def test_cmd_whoami_not_authenticated(capsys):
    from build_cli import cmd_whoami

    sso = MagicMock()
    sso.show_identity.return_value = {"authenticated": False}
    with patch("build_cli._load_modules", return_value=(sso, None, None)):
        rc = cmd_whoami(argparse.Namespace())
    assert rc == 1
    assert "not authenticated" in capsys.readouterr().out


def test_cmd_whoami_authenticated(capsys):
    from build_cli import cmd_whoami

    sso = MagicMock()
    sso.show_identity.return_value = {
        "authenticated": True,
        "token_type": "Bearer",
        "has_refresh_token": True,
        "scopes": ["codewhisperer:completions"],
        "expires_at": None,
    }
    with patch("build_cli._load_modules", return_value=(sso, None, None)):
        rc = cmd_whoami(argparse.Namespace())
    assert rc == 0
    assert "Bearer" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_logout
# ---------------------------------------------------------------------------


def test_cmd_logout(capsys):
    from build_cli import cmd_logout

    sso = MagicMock()
    with patch("build_cli._load_modules", return_value=(sso, None, None)):
        rc = cmd_logout(argparse.Namespace())
    assert rc == 0
    sso.logout.assert_called_once()
    assert "Logged out" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_models
# ---------------------------------------------------------------------------


def test_cmd_models(capsys):
    from build_cli import cmd_models

    list_models = MagicMock(return_value=[{"id": "auto"}, {"id": "nova-pro"}])
    load_tags = MagicMock(return_value=["chat", "code"])
    with patch("build_cli._load_modules", return_value=(None, list_models, load_tags)):
        rc = cmd_models(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto" in out
    assert "chat" in out


# ---------------------------------------------------------------------------
# cmd_login — already-authenticated path
# ---------------------------------------------------------------------------


def test_cmd_login_already_authenticated(capsys):
    from build_cli import cmd_login

    sso = MagicMock()
    sso.start_login.return_value = {
        "already_authenticated": True,
        "phase": "authenticated",
    }
    sso.get_status.return_value = {"authenticated": True, "token_expires_at": None}
    with patch("build_cli._load_modules", return_value=(sso, None, None)):
        rc = cmd_login(argparse.Namespace())
    assert rc == 0
    assert "Already authenticated" in capsys.readouterr().out


def test_cmd_login_no_user_code(capsys):
    from build_cli import cmd_login

    sso = MagicMock()
    sso.start_login.return_value = {}
    with patch("build_cli._load_modules", return_value=(sso, None, None)):
        rc = cmd_login(argparse.Namespace())
    assert rc == 1


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------


def test_main_dispatches_status():
    from build_cli import main

    with patch("build_cli.cmd_status", return_value=0) as mock_cmd:
        rc = main(["status"])
    assert rc == 0
    mock_cmd.assert_called_once()
