"""Tests for plugin auto-start (ensure_bridge) backend resolution."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import __init__ as plugin  # noqa: E402


def test_ensure_bridge_uses_config_yaml_backend(monkeypatch):
    """ensure_bridge must spawn the bridge with the backend read from
    config.yaml (whatever it currently is), not a hardcoded value -- so the
    launchd plist is redundant and the plugin owns the backend choice."""
    # Simulate port free -> it will try to spawn.
    monkeypatch.setattr(
        __import__("socket"), "create_connection", mock.Mock(side_effect=OSError())
    )
    spawned = {}
    monkeypatch.setattr(
        subprocess,
        "Popen",
        mock.Mock(side_effect=lambda *a, **k: spawned.update(k) or mock.Mock()),
    )
    # Env override absent -> backend comes from config.yaml.
    monkeypatch.delenv("AMAZON_Q_BACKEND", raising=False)
    # Determine the expected backend from config.yaml the same way the code does.
    sys.path.insert(0, str(ROOT))
    from amazon_q_bridge import load_plugin_config, _config_str

    expected = _config_str("backend", "direct")
    plugin.ensure_bridge()
    assert spawned.get("env", {}).get("AMAZON_Q_BACKEND") == expected


def test_ensure_bridge_env_override_wins(monkeypatch):
    """An explicit AMAZON_Q_BACKEND env var must override config.yaml."""
    monkeypatch.setattr(
        __import__("socket"), "create_connection", mock.Mock(side_effect=OSError())
    )
    spawned = {}
    monkeypatch.setattr(
        subprocess,
        "Popen",
        mock.Mock(side_effect=lambda *a, **k: spawned.update(k) or mock.Mock()),
    )
    monkeypatch.setenv("AMAZON_Q_BACKEND", "direct")
    plugin.ensure_bridge()
    assert spawned.get("env", {}).get("AMAZON_Q_BACKEND") == "direct"


def test_ensure_bridge_skips_when_already_listening(monkeypatch):
    """If something already listens on the port, don't double-spawn."""
    conn = mock.MagicMock()  # supports `with socket.create_connection(...) as c`
    monkeypatch.setattr(
        __import__("socket"),
        "create_connection",
        mock.Mock(return_value=conn),
    )
    popen = mock.Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    plugin.ensure_bridge()
    popen.assert_not_called()
