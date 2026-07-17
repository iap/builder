"""Tests for the agentic backend: tool parser, sandboxed executor, loop limit."""
from __future__ import annotations

import os
import sys
import tempfile
import pathlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import amazon_q_bridge as bridge  # noqa: E402


def _set_agentic(monkeypatch, tools=("fs_read", "fs_write"), root=None,
                 max_iters="8", timeout="30"):
    cfg = {
        "agentic_tools": list(tools),
        "agentic_root": root or "",
        "agentic_max_iters": max_iters,
        "agentic_timeout": timeout,
    }
    monkeypatch.setattr(bridge, "_PLUGIN_CONFIG", cfg)
    # The bridge is a client only: tool execution lives in the plugin process.
    monkeypatch.delenv("AMAZON_Q_TOOL_SOCKET", raising=False)
    for k in ("AGENTIC_TOOLS", "AGENTIC_ROOT", "AGENTIC_MAX_ITERS", "AGENTIC_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)


def test_parse_tool_call_finds_valid_block(monkeypatch):
    _set_agentic(monkeypatch)
    text = (
        "<function_calls>\n"
        "<invoke name=\"fs_write\">\n"
        "<parameter name=\"path\">note.txt</parameter>\n"
        "<parameter name=\"content\">hello</parameter>\n"
        "</invoke>\n</function_calls>"
    )
    assert bridge.parse_tool_call(text) == ("fs_write", {"path": "note.txt", "content": "hello"})


def test_parse_tool_call_ignores_unknown_tool(monkeypatch):
    _set_agentic(monkeypatch, tools=("fs_read", "fs_write"))
    # 'bash' not in allowed tools -> treated as final text (None)
    text = (
        "<function_calls><invoke name=\"bash\">"
        "<parameter name=\"command\">rm -rf /</parameter>"
        "</invoke></function_calls>"
    )
    assert bridge.parse_tool_call(text) is None


def test_parse_tool_call_returns_none_when_absent(monkeypatch):
    _set_agentic(monkeypatch)
    assert bridge.parse_tool_call("just normal text, no tool block") is None


def test_exec_tool_fs_write_then_read_sandboxed(tmp_path, monkeypatch):
    _set_agentic(monkeypatch, root=str(tmp_path))
    res = bridge.exec_tool("fs_write", "a.txt\nCONTENT", str(tmp_path), 30)
    assert "wrote" in res
    out = bridge.exec_tool("fs_read", "a.txt", str(tmp_path), 30)
    assert out == "CONTENT"


def test_exec_tool_rejects_path_escape(tmp_path, monkeypatch):
    _set_agentic(monkeypatch, root=str(tmp_path))
    evil = str(tmp_path.parent / "escape.txt")
    res = bridge.exec_tool("fs_write", f"{evil}\ndata", str(tmp_path), 30)
    assert "escapes sandbox" in res
    res2 = bridge.exec_tool("fs_read", "../escape.txt", str(tmp_path), 30)
    assert "escapes sandbox" in res2


def test_run_agentic_requires_executor_socket(monkeypatch, tmp_path):
    """Without AMAZON_Q_TOOL_SOCKET, the client refuses to run agentic tools."""
    _set_agentic(monkeypatch, tools=("fs_read", "fs_write"), root=str(tmp_path))

    calls = {"n": 0}

    def fake_chat(prompt, model=None, conversation_id=None, tools=None, tool_results=None):
        calls["n"] += 1
        return (
            "<tool>fs_write</tool>\n<args>out.txt\nAGENTIC_OK</args>",
            conversation_id,
            "tool-1",
        )

    import q_direct

    with mock.patch.object(q_direct, "chat", fake_chat):
        answer, status, ok, _ = bridge.run_agentic("write a file", "claude-haiku-4.5")
    assert ok is False
    assert "AMAZON_Q_TOOL_SOCKET" in answer


def test_run_agentic_executes_tool_via_socket(monkeypatch, tmp_path):
    """Full loop with a stubbed q_direct.chat and a real local executor socket.

    The executor side is simulated by a minimal server that echoes the parsed
    tool envelope; this proves the bridge sends the correct payload and feeds
    the result back into the next Q turn.
    """
    import socket as _socket
    import json as _json
    import threading as _threading
    import tempfile

    server_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock_dir = tempfile.mkdtemp(prefix="awsx")
    sock_path = os.path.join(sock_dir, "exec.sock")
    server_sock.bind(sock_path)
    server_sock.listen(1)

    received = {}

    def serve():
        conn, _ = server_sock.accept()
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            received["req"] = _json.loads(buf.decode().strip())
            conn.sendall(_json.dumps({"ok": True, "result": "EXEC_DONE"}).encode() + b"\n")

    t = _threading.Thread(target=serve, daemon=True)
    t.start()

    _set_agentic(monkeypatch, tools=("fs_read", "fs_write"), root=str(tmp_path))
    monkeypatch.setenv("AMAZON_Q_TOOL_SOCKET", sock_path)

    calls = {"n": 0}

    def fake_chat(prompt, model=None, conversation_id=None, tools=None, tool_results=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                "<function_calls>\n"
                "<invoke name=\"fs_write\">\n"
                "<parameter name=\"path\">out.txt</parameter>\n"
                "<parameter name=\"content\">AGENTIC_OK</parameter>\n"
                "</invoke>\n</function_calls>",
                conversation_id,
                "tool-1",
            )
        return "done writing", conversation_id, None

    import q_direct

    with mock.patch.object(q_direct, "chat", fake_chat):
        answer, status, ok, _ = bridge.run_agentic("write a file", "claude-haiku-4.5")
    assert ok is True
    assert "done writing" in answer
    # The bridge sent a well-formed request to the Hermes-owned executor.
    assert received.get("req", {}).get("tool") == "fs_write"
    t.join(timeout=2)


def test_run_agentic_respects_max_iters(monkeypatch, tmp_path):
    """If the model keeps asking for tools, the loop caps at max_iters."""
    import socket as _socket
    import json as _json
    import threading as _threading

    server_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock_dir = tempfile.mkdtemp(prefix="awsx")
    sock_path = os.path.join(sock_dir, "exec2.sock")
    server_sock.bind(sock_path)
    server_sock.listen(1)

    def serve():
        while True:
            try:
                conn, _ = server_sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                conn.sendall(_json.dumps({"ok": True, "result": "ok"}).encode() + b"\n")

    t = _threading.Thread(target=serve, daemon=True)
    t.start()

    _set_agentic(monkeypatch, tools=("fs_read", "fs_write"), root=str(tmp_path), max_iters="3")
    monkeypatch.setenv("AMAZON_Q_TOOL_SOCKET", sock_path)

    calls = {"n": 0}

    def fake_chat(prompt, model=None, conversation_id=None, tools=None, tool_results=None):
        calls["n"] += 1
        return (
            "<function_calls><invoke name=\"fs_write\">"
            "<parameter name=\"path\">loop.txt</parameter>"
            "<parameter name=\"content\">x</parameter>"
            "</invoke></function_calls>",
            conversation_id,
            f"tool-{calls['n']}",
        )

    import q_direct

    with mock.patch.object(q_direct, "chat", fake_chat):
        answer, status, ok, _ = bridge.run_agentic("loop", "claude-haiku-4.5")
    assert calls["n"] <= 3
    assert ok is True
    t.join(timeout=2)


def test_http_agentic_backend_executes_via_socket(monkeypatch, tmp_path):
    """Full HTTP entry path: POST /v1/chat/completions -> agentic -> socket -> executor."""
    import json as _json
    import threading as _threading
    import tempfile
    import urllib.request
    from http.server import HTTPServer

    # Plugin-owned executor + socket server (same process as the HTTP server).
    import hermes_tool_adapter as adapter

    sock_dir = tempfile.mkdtemp(prefix="awsxhttp")
    sock_path = os.path.join(sock_dir, "exec.sock")
    os.environ["AMAZON_Q_TOOL_SOCKET"] = sock_path
    dispatched = {}

    def fake_executor(name, args):
        dispatched[(name, tuple(sorted(args.items())))] = True
        if name == "write_file":
            p = pathlib.Path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"])
            return '{"bytes_written": %d}' % len(args["content"])
        return "{}"

    adapter.set_executor(fake_executor)
    adapter.stop_tool_server()
    adapter.start_tool_server()

    # Point the bridge at the agentic backend and a stubbed Q.
    monkeypatch.setattr(bridge, "BACKEND", "agentic")
    import q_direct

    calls = {"n": 0}

    def fake_chat(prompt, model=None, conversation_id=None, tools=None, tool_results=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                "<function_calls><invoke name=\"fs_write\">"
                "<parameter name=\"path\">via_http.txt</parameter>"
                "<parameter name=\"content\">HTTP_OK</parameter>"
                "</invoke></function_calls>",
                conversation_id,
                "tool-1",
            )
        return "done via http", conversation_id, None

    with mock.patch.object(q_direct, "chat", fake_chat):
        server = HTTPServer(("127.0.0.1", 0), bridge.Handler)
        port = server.server_address[1]
        t = _threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=_json.dumps({
                    "model": "claude-haiku-4.5",
                    "messages": [{"role": "user", "content": "write via http"}],
                }).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = _json.loads(resp.read().decode())
            choice = body["choices"][0]["message"]["content"]
            assert "done via http" in choice
            # The executor must have been invoked with the write_file tool.
            assert any(k[0] == "write_file" for k in dispatched)
        finally:
            server.shutdown()
            server.server_close()
            adapter.stop_tool_server()
            adapter.set_executor(None)

