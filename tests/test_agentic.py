"""Tests for the agentic backend: tool parser, sandboxed executor, loop limit."""
from __future__ import annotations

import sys
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
    for k in ("AGENTIC_TOOLS", "AGENTIC_ROOT", "AGENTIC_MAX_ITERS", "AGENTIC_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)


def test_parse_tool_call_finds_valid_block(monkeypatch):
    _set_agentic(monkeypatch)
    text = "thinking...\n<tool>fs_write</tool>\n<args>note.txt\nhello</args>\n"
    assert bridge.parse_tool_call(text) == ("fs_write", "note.txt\nhello")


def test_parse_tool_call_ignores_unknown_tool(monkeypatch):
    _set_agentic(monkeypatch, tools=("fs_read", "fs_write"))
    # 'bash' not in allowed tools -> treated as final text (None)
    text = "<tool>bash</tool>\n<args>rm -rf /</args>"
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


def test_run_agentic_executes_tool_via_q_direct(monkeypatch, tmp_path):
    """Full loop with a stubbed q_direct.chat: model writes, then answers."""
    _set_agentic(monkeypatch, tools=("fs_read", "fs_write"), root=str(tmp_path))

    calls = {"n": 0}

    def fake_chat(prompt, model=None, conversation_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                "<tool>fs_write</tool>\n<args>out.txt\nAGENTIC_OK</args>",
                conversation_id,
            )
        return "done writing", conversation_id

    import q_direct

    with mock.patch.object(q_direct, "chat", fake_chat):
        answer, status, ok, _ = bridge.run_agentic("write a file", "claude-haiku-4.5")
    assert ok is True
    assert (tmp_path / "out.txt").read_text() == "AGENTIC_OK"
    assert "done writing" in answer


def test_run_agentic_respects_max_iters(monkeypatch, tmp_path):
    """If the model keeps asking for tools, the loop caps at max_iters."""
    _set_agentic(monkeypatch, tools=("fs_read", "fs_write"), root=str(tmp_path),
                 max_iters="3")

    calls = {"n": 0}

    def fake_chat(prompt, model=None, conversation_id=None):
        calls["n"] += 1
        return "<tool>fs_write</tool>\n<args>loop.txt\nx</args>", conversation_id

    import q_direct

    with mock.patch.object(q_direct, "chat", fake_chat):
        answer, status, ok, _ = bridge.run_agentic("loop", "claude-haiku-4.5")
    assert calls["n"] <= 3
    assert ok is True
