"""Tests for the Hermes tool adapter (plugin-owned executor).

The adapter dispatches tool requests through a pluggable executor. In the live
plugin this is ``ctx.dispatch_tool`` (the Hermes PluginContext). In tests we
inject a fake executor so we can verify request mapping and transport without a
running Hermes session.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path
from unittest import mock

import pytest
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hermes_tool_adapter as adapter  # noqa: E402


def test_socket_path_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AMAZON_Q_TOOL_SOCKET", str(tmp_path / "explicit.sock"))
    assert adapter._socket_path() == str(tmp_path / "explicit.sock")

    monkeypatch.delenv("AMAZON_Q_TOOL_SOCKET", raising=False)
    monkeypatch.setenv("AMAZON_Q_TOOL_SOCKET_DIR", str(tmp_path))
    assert adapter._socket_path().endswith("aws-build-tools.sock")


def test_safe_under_root_blocks_escape(tmp_path):
    root = tmp_path
    ok = adapter._safe_under_root(str(root / "a" / "b.txt"), str(root))
    assert ok == str(root / "a" / "b.txt")
    with pytest.raises(ValueError):
        adapter._safe_under_root(str(tmp_path.parent / "escape.txt"), str(root))


def test_map_request_routes_bridge_tools_to_hermes():
    name, args = adapter._map_request(
        "fs_write", {"content": "note.txt\nhello", "root": "/srv"}
    )
    assert name == "write_file"
    assert args["path"].endswith("note.txt")
    assert args["content"] == "hello"

    name, args = adapter._map_request("fs_read", {"path": "x.txt", "root": "/srv"})
    assert name == "read_file"
    assert args["path"].endswith("x.txt")

    name, args = adapter._map_request("bash", {"command": "ls", "root": "/srv"})
    assert name == "terminal"
    assert args["command"] == "ls"
    assert args["workdir"] is not None


def test_map_request_blocks_sandbox_escape():
    import pytest

    with pytest.raises(ValueError):
        adapter._map_request("fs_write", {"content": "../evil.txt\nx", "root": "/srv"})


def test_dispatch_uses_injected_executor():
    captured = {}

    def fake_executor(name, args):
        captured["name"] = name
        captured["args"] = args
        return json.dumps({"ok": True, "result": "RAN"})

    adapter.set_executor(fake_executor)
    try:
        out = adapter._dispatch("fs_write", {"content": "a.txt\nhi", "root": "/srv"})
        assert captured["name"] == "write_file"
        assert "RAN" in out
    finally:
        adapter.set_executor(None)


def test_dispatch_without_executor_errors():
    adapter.set_executor(None)
    out = adapter._dispatch("fs_write", {"content": "a.txt\nhi", "root": "/srv"})
    assert "not initialized" in out


def test_start_and_stop_tool_server_roundtrip():
    """Start a server; dispatch a tool via a real socket client + fake executor."""
    sock_dir = tempfile.mkdtemp(prefix="awsx")
    os.environ["AMAZON_Q_TOOL_SOCKET"] = os.path.join(sock_dir, "tools.sock")

    received = {}

    def fake_executor(name, args):
        received["name"] = name
        received["args"] = args
        return json.dumps({"ok": True, "result": "EXECUTED"})

    adapter.set_executor(fake_executor)
    adapter.stop_tool_server()  # ensure clean state across the shared module
    path = adapter.start_tool_server()
    assert os.path.exists(path)
    try:
        payload = json.dumps(
            {"tool": "fs_write", "args": {"content": "note.txt\nhello", "root": str(ROOT)}}
        ).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(path)
            s.sendall(payload)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        envelope = json.loads(buf.decode().strip())
        assert envelope["ok"] is True
        assert "EXECUTED" in envelope["result"]
        assert received["name"] == "write_file"
    finally:
        adapter.stop_tool_server()
        adapter.set_executor(None)
        if os.path.exists(path):
            os.remove(path)
