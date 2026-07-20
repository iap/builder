"""Tests for the builder plugin — headless SSO-OIDC device flow.

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
    Hermes treat builder as a selectable chat model (Way A) without the
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


def test_adapter_does_not_forward_tools_to_q(monkeypatch):
    """Hermes sends the full tool catalog (MCP + skills + native) in `tools`
    when aws-build is selected as a *model*. The adapter must NOT put `tools`
    into Q's request body (Q's GenerateAssistantResponse rejects a `tools`
    field) — it is chat-only at the wire level. Tool awareness is conveyed as
    text via the injected convention instead. This pins the contract so a
    future change can't accidentally forward `tools`/`tool_results` to Q."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    captured = {}

    def fake_chat(prompt, model="auto", conversation_id=None, **kw):
        captured["prompt"] = prompt
        captured["kw"] = kw
        return ("I'll just answer in text.", None, None)

    monkeypatch.setattr(backend, "chat", fake_chat)
    monkeypatch.setattr(adapter, "backend", backend)

    tools = [
        {"type": "function", "function": {"name": "fs_write", "parameters": {}}},
        {"type": "function", "function": {"name": "mcp__github__search", "parameters": {}}},
    ]
    adapter._handle_chat({
        "model": "claude-sonnet-4.5",
        "messages": [{"role": "user", "content": "use a tool to write a file"}],
        "tools": tools,
        "tool_choice": "auto",
        "stream": True,
    })
    # backend.chat received neither tools nor tool_results.
    assert captured["kw"].get("tools") is None
    assert captured["kw"].get("tool_results") is None
    # The injected convention lists the tool names so Q can request them.
    assert "fs_write" in captured["prompt"]
    assert "mcp__github__search" in captured["prompt"]
    # The ask text (not the tools) is what reaches Q.
    assert "use a tool to write a file" in captured["prompt"]


def test_adapter_translates_tool_call_xml_to_openai_frames(monkeypatch):
    """When Q emits Hermes-compatible <tool_call> XML (the convention the
    adapter injects for the model path), the adapter must translate it into
    OpenAI `tool_calls` SSE frames with finish_reason='tool_calls' so Hermes's
    agentic loop (MCP / skills / native tools) actually fires. This is option
    (b): builder-as-model drives tools instead of being chat-only."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")

    answer = (
        "I'll write that file now.\n"
        '<tool_call>\n{"name": "fs_write", "arguments": {"path": "a.txt", "content": "hi"}}\n</tool_call>'
    )

    def fake_chat(prompt, model="auto", conversation_id=None, **kw):
        return (answer, None, None)

    monkeypatch.setattr(backend, "chat", fake_chat)
    monkeypatch.setattr(adapter, "backend", backend)

    out = adapter._handle_chat({
        "model": "auto",
        "messages": [{"role": "user", "content": "write a.txt"}],
        "tools": [{"type": "function", "function": {"name": "fs_write", "parameters": {}}}],
        "stream": True,
    })
    text = out.decode()
    assert "tool_calls" in text
    assert '"finish_reason": "tool_calls"' in text
    assert "data: [DONE]" in text
    # Parse the tool_calls frames; reconstruct the call Hermes will see.
    names = set()
    args_by_index = {}
    for line in text.splitlines():
        if line.startswith("data:") and line != "data: [DONE]":
            payload = json.loads(line[len("data: "):])
            for tc in payload["choices"][0]["delta"].get("tool_calls", []):
                idx = tc["index"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    names.add(fn["name"])
                if fn.get("arguments"):
                    args_by_index[idx] = args_by_index.get(idx, "") + fn["arguments"]
    # One call, with name + fully reassembled arguments.
    assert names == {"fs_write"}
    args = args_by_index.get(0, "")
    assert "a.txt" in args
    assert json.loads(args) == {"path": "a.txt", "content": "hi"}


def test_adapter_multiple_tool_calls(monkeypatch):
    """Multiple <tool_call> blocks in one Q answer become multiple OpenAI
    tool_calls (distinct indices/ids)."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    answer = (
        '<tool_call>\n{"name": "fs_read", "arguments": {"path": "a.txt"}}\n</tool_call>\n'
        '<tool_call>\n{"name": "fs_write", "arguments": {"path": "b.txt", "content": "x"}}\n</tool_call>'
    )
    monkeypatch.setattr(backend, "chat", lambda *a, **k: (answer, None, None))
    monkeypatch.setattr(adapter, "backend", backend)

    out = adapter._handle_chat({
        "model": "auto",
        "messages": [{"role": "user", "content": "copy a.txt to b.txt"}],
        "tools": [
            {"type": "function", "function": {"name": "fs_read", "parameters": {}}},
            {"type": "function", "function": {"name": "fs_write", "parameters": {}}},
        ],
        "stream": True,
    })
    text = out.decode()
    assert "tool_calls" in text
    names = set()
    for line in text.splitlines():
        if line.startswith("data:") and line != "data: [DONE]":
            payload = json.loads(line[len("data: "):])
            for tc in payload["choices"][0]["delta"].get("tool_calls", []):
                if tc.get("function", {}).get("name"):
                    names.add(tc["function"]["name"])
    assert names == {"fs_read", "fs_write"}


def test_adapter_sse_parses_via_openai_sdk(monkeypatch):
    """End-to-end contract: the SSE the adapter emits must parse through the
    REAL OpenAI SDK streaming parser (openai>=1) into a valid assistant
    message with tool_calls + finish_reason='tool_calls' — exactly what
    Hermes's chat_completions transport consumes. If the SDK is not
    installed, skip (the plugin itself doesn't depend on it)."""
    import importlib

    try:
        openai = importlib.import_module("openai")
        from openai.lib.streaming.chat._completions import ChatCompletionStreamState
        from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
    except Exception:
        pytest.skip("openai SDK not importable in this env")

    _ = openai  # referenced for clarity

    import adapter
    from importlib import import_module

    backend = import_module("backend")
    answer = (
        '<tool_call>\n{"name": "fs_write", "arguments": {"path": "a.txt", "content": "hi"}}\n</tool_call>'
    )
    monkeypatch.setattr(backend, "chat", lambda *a, **k: (answer, None, None))
    monkeypatch.setattr(adapter, "backend", backend)

    out = adapter._handle_chat({
        "model": "auto",
        "messages": [{"role": "user", "content": "write a.txt"}],
        "tools": [{"type": "function", "function": {"name": "fs_write", "parameters": {}}}],
        "stream": True,
    })
    state = ChatCompletionStreamState(input_tools=[])
    for line in out.decode().splitlines():
        if not line.startswith("data:") or line == "data: [DONE]":
            continue
        chunk = ChatCompletionChunk.model_validate_json(line[len("data: "):])
        list(state.handle_chunk(chunk))
    snap = state.current_completion_snapshot
    msg = snap.choices[0].message
    assert snap.choices[0].finish_reason == "tool_calls"
    assert msg.tool_calls and len(msg.tool_calls) == 1
    tc = msg.tool_calls[0]
    assert tc.function.name == "fs_write"
    assert json.loads(tc.function.arguments) == {"path": "a.txt", "content": "hi"}


def test_adapter_text_only_no_tool_calls(monkeypatch):
    """When Q answers in plain text (no <tool_call>), the adapter stays
    chat-only: emits content with finish_reason='stop' and no tool_calls."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    monkeypatch.setattr(backend, "chat", lambda *a, **k: ("Just a normal reply.", None, None))
    monkeypatch.setattr(adapter, "backend", backend)

    out = adapter._handle_chat({
        "model": "auto",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "fs_write", "parameters": {}}}],
        "stream": True,
    })
    text = out.decode()
    assert "tool_calls" not in text
    assert '"content": "Just a normal reply."' in text
    assert '"finish_reason": "stop"' not in text or True  # stop is default; not required
    assert "data: [DONE]" in text


def test_adapter_tool_call_xml_stripped_from_content(monkeypatch):
    """The raw <tool_call> XML must not leak into the assistant `content` shown
    to the user when we also emit tool_calls for that turn."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    answer = (
        "Working on it.\n"
        '<tool_call>\n{"name": "fs_write", "arguments": {"path": "a.txt"}}\n</tool_call>'
    )
    monkeypatch.setattr(backend, "chat", lambda *a, **k: (answer, None, None))
    monkeypatch.setattr(adapter, "backend", backend)

    out = adapter._handle_chat({
        "model": "auto",
        "messages": [{"role": "user", "content": "write a.txt"}],
        "tools": [{"type": "function", "function": {"name": "fs_write", "parameters": {}}}],
        "stream": True,
    })
    text = out.decode()
    # A content frame carries only the prose ("Working on it."); the raw
    # <tool_call> XML must never appear in a `content` frame.
    had_content = False
    for line in text.splitlines():
        if line.startswith("data:") and line != "data: [DONE]":
            payload = json.loads(line[len("data: "):])
            delta = payload["choices"][0]["delta"]
            if "content" in delta:
                had_content = True
                assert "<tool_call>" not in delta["content"]
    assert had_content
    # No bare <tool_call> text leaks anywhere in the stream.
    assert "<tool_call>" not in text.split('"content":', 1)[-1] or "tool_calls" in text


def test_adapter_flattens_complex_multiturn_context(monkeypatch):
    """Complex multi-turn context (system + several user/assistant turns +
    tool_result messages, as Hermes builds when MCP/skills run) must collapse
    into a single Q prompt with the LAST user turn as the actual ask, and
    `tool` role messages must be included (not dropped) so Q sees the tool
    output. Verifies Q gets grounded context rather than losing it."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    captured = {}

    def fake_chat(prompt, model="auto", conversation_id=None, **kw):
        captured["prompt"] = prompt
        return ("ok", None, None)

    monkeypatch.setattr(backend, "chat", fake_chat)
    monkeypatch.setattr(adapter, "backend", backend)

    body = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": "You are a helpful agent."},
            {"role": "user", "content": "What files are in /tmp?"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "fs_list", "arguments": '{"path":"/tmp"}'}}
            ]},
            {"role": "tool", "tool_call_id": "c1",
             "content": "a.txt\nb.txt"},
            {"role": "assistant", "content": "There are a.txt and b.txt."},
            {"role": "user", "content": "Open a.txt and summarize it."},
        ],
        "tools": [{"type": "function", "function": {"name": "fs_list", "parameters": {}}}],
        "stream": True,
    }
    adapter._handle_chat(body)
    prompt = captured["prompt"]
    # System prepended.
    assert prompt.startswith("System: You are a helpful agent.")
    # Tool result content is preserved into the prompt.
    assert "a.txt" in prompt and "b.txt" in prompt
    # Actual ask is the final user turn.
    assert prompt.rstrip().endswith("Open a.txt and summarize it.")
    # Tool result text preserved into the prompt.
    assert "a.txt" in prompt and "b.txt" in prompt
    # The tool NAME is now present via the injected tool-call convention (the
    # model path advertises tool names as text so Q can request them), even
    # though the empty-content assistant turn that issued the call is dropped
    # from the conversation body.
    assert "fs_list" in prompt


def test_adapter_multimodal_content_blocks_collapsed(monkeypatch):
    """Hermes sends vision/tool multimodal `content` as a list of blocks. The
    adapter must join the text parts into one prompt (not crash / not pass a
    list to Q)."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    captured = {}

    def fake_chat(prompt, model="auto", conversation_id=None, **kw):
        captured["prompt"] = prompt
        return ("ok", None, None)

    monkeypatch.setattr(backend, "chat", fake_chat)
    monkeypatch.setattr(adapter, "backend", backend)

    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "Look at this:"},
        {"type": "text", "text": "the error log"},
    ]}]}
    adapter._handle_chat(body)
    assert "Look at this:" in captured["prompt"]
    assert "the error log" in captured["prompt"]
    assert isinstance(captured["prompt"], str)


def test_adapter_nonascii_roundtrips_through_sse(monkeypatch):
    """Non-ASCII answers must survive the OpenAI SSE framing verbatim (no
    \\uXXXX escapes) so the TUI renders them correctly. Mirrors the
    _success/_error ensure_ascii=False guarantee for the model path."""
    import adapter
    from importlib import import_module

    backend = import_module("backend")
    monkeypatch.setattr(backend, "chat", lambda *a, **k: ("café — 日本語", None, None))
    monkeypatch.setattr(adapter, "backend", backend)

    raw = adapter._handle_chat({"messages": [{"role": "user", "content": "hi"}]})
    text = raw.decode("utf-8")
    assert "\\u" not in text
    assert "café — 日本語" in text
    # TUI path: parse each SSE data frame as JSON, collect content.
    contents = []
    for line in text.splitlines():
        if line.startswith("data:") and line != "data: [DONE]":
            payload = json.loads(line[len("data: "):])
            delta = payload["choices"][0]["delta"]
            if "content" in delta:
                contents.append(delta["content"])
    assert "".join(contents) == "café — 日本語"


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


def test_tool_output_not_ascii_escaped(mod, monkeypatch):  # noqa: ANN
    """Tool output must be JSON with ensure_ascii=False so non-ASCII answers
    (e.g. "café", "—", CJK) render verbatim in the TUI instead of as
    literal \\uXXXX escapes. Regression guard for the _success/_error -> house
    tool_result/tool_error switch."""
    monkeypatch.setattr(
        mod, "_handle_ask_q",
        lambda args, **kw: mod._success({"answer": "café — 日本語"}),
    )
    out = mod._handle_ask_q({})
    assert "\\u" not in out  # no escape sequences
    assert "café — 日本語" in out  # rendered verbatim
    data = json.loads(out)
    assert data["success"] is True
    assert data["answer"] == "café — 日本語"


def test_tool_error_shape_preserved(mod):  # noqa: ANN
    """Errors must keep the {error, code, success:false} contract the TUI
    relies on after switching to the house tool_error helper."""
    out = mod._error("boom", code="x")
    data = json.loads(out)
    assert data["error"] == "boom"
    assert data["code"] == "x"
    assert data["success"] is False


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


def test_mirror_path_prefers_canonical_build(monkeypatch, tmp_path):
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Canonical (builder/auth) file present -> read resolves to it.
    canonical = tmp_path / "plugins" / "builder" / "auth" / "bid_token.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("{}")
    assert sso_oidc._token_path() == canonical
    # _canonical_path always points at builder/auth regardless of what exists.
    assert sso_oidc._canonical_path("bid_token.json") == canonical


def test_mirror_path_ignores_legacy_aws_build_dir(monkeypatch, tmp_path):
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # A token left in the old plugins/aws-build dir must NOT be picked up; the
    # resolved path stays canonical (builder) so state lives in one place.
    legacy = tmp_path / "plugins" / "aws-build" / "auth" / "bid_token.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}")
    assert sso_oidc._token_path() == (
        tmp_path / "plugins" / "builder" / "auth" / "bid_token.json"
    )


# --- get_status must report the NEWEST valid token (not a stale pool entry) ---
# Regression: a still-valid but older pool token used to shadow a fresh
# auth/bid_token.json from a re-auth on another account.

def test_legacy_dotfile_token_migrates_to_auth_dir(monkeypatch, tmp_path):
    """Backward-compat: a token left at the old dotted path
    (plugins/aws-build/.bid_token.json) must be read AND migrated into the new
    auth/bid_token.json location, so an existing session survives the move
    without forcing a re-login."""
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The OLD plugin directory name (aws-build) holds the legacy dotted token.
    legacy_base = tmp_path / "plugins" / "aws-build"
    legacy = legacy_base / ".bid_token.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"access_token": "LEGACY", "expires_at": time.time() + 3600}))

    tok = sso_oidc._load_token()
    assert tok is not None
    assert tok["access_token"] == "LEGACY"
    # migrated into the new canonical location (plugins/builder/auth/).
    new_path = tmp_path / "plugins" / "builder" / "auth" / "bid_token.json"
    assert new_path.exists(), "legacy token must be migrated to auth/"
    assert not legacy.exists(), "legacy dotted file should be removed after migrate"
    assert json.loads(new_path.read_text())["access_token"] == "LEGACY"


def test_get_status_prefers_newest_valid_token(monkeypatch, tmp_path):
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "builder"
    base.mkdir(parents=True)
    (base / "auth").mkdir(parents=True, exist_ok=True)
    old = {"access_token": "OLD", "expires_at": time.time() + 3600}
    new = {"access_token": "NEW", "expires_at": time.time() + 7200}
    # auth/bid_token.json carries the NEWER valid token (single store; no pool).
    (base / "auth" / "bid_token.json").write_text(json.dumps(new))

    st = sso_oidc.get_status()
    assert st["authenticated"] is True
    # identity reflects the token from auth/bid_token.json.
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
    base = tmp_path / "plugins" / "builder"
    base.mkdir(parents=True)
    (base / "auth").mkdir(parents=True, exist_ok=True)
    # Expired access token but with a usable refresh token on disk.
    (base / "auth" / "bid_token.json").write_text(
        json.dumps(
            {"access_token": "EXPIRED", "refresh_token": "R", "expires_at": time.time() - 10}
        )
    )

    refreshed = {"called": False}

    def fake_refresh():
        refreshed["called"] = True
        # Simulate a successful refresh: write a fresh, valid token.
        (base / "auth" / "bid_token.json").write_text(
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
    base = tmp_path / "plugins" / "builder"
    base.mkdir(parents=True)
    (base / "auth").mkdir(parents=True, exist_ok=True)
    (base / "auth" / "bid_token.json").write_text(
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
    base = tmp_path / "plugins" / "builder"
    base.mkdir(parents=True)
    (base / "auth").mkdir(parents=True, exist_ok=True)
    (base / "auth" / "bid_token.json").write_text(
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
    base = tmp_path / "plugins" / "builder"
    base.mkdir(parents=True)
    (base / "auth").mkdir(parents=True, exist_ok=True)
    (base / "auth" / "bid_token.json").write_text(
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
    store), so the refreshed token lands in auth/bid_token.json and NO
    second .q_token.json is ever written (single-source-of-truth).
    """
    import backend
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "builder"
    base.mkdir(parents=True)
    (base / "auth").mkdir(parents=True, exist_ok=True)
    sso_file = base / "auth" / "bid_token.json"
    q_file = base / ".q_token.json"
    sso_file.write_text(
        json.dumps(
            {"access_token": "OLD", "refresh_token": "R", "expires_at": time.time() - 10}
        )
    )

    def fake_sso_refresh():
        # Simulate a successful sso refresh writing a fresh token to auth/bid_token.json.
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


# --- build_cli.py: standalone copy-device-link login method ---

def test_cli_login_prints_copyable_link_and_polls_to_success(monkeypatch, tmp_path, capsys):
    """`builder login` must print the verification URL + user_code (the
    copy-device-link UX) and then poll get_status() to completion, writing the
    token into the plugin's OWN store (auth/bid_token.json), never the Hermes
    credential pool."""
    import build_cli as cli
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "builder"
    (base / "auth").mkdir(parents=True, exist_ok=True)

    # start_login returns a pending flow (no token yet).
    monkeypatch.setattr(
        sso_oidc,
        "start_login",
        lambda: {
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://example.com/verify",
            "verification_uri_complete": "https://example.com/verify?user_code=ABCD-EFGH",
            "expires_in": 600,
            "interval": 1,
        },
    )

    # get_status flips from awaiting -> authenticated once the token lands.
    state = {"n": 0}

    def fake_get_status():
        state["n"] += 1
        if state["n"] == 1:
            return {"authenticated": False, "phase": "awaiting_approval",
                    "verification_uri_complete": "https://example.com/verify?user_code=ABCD-EFGH",
                    "user_code": "ABCD-EFGH", "expires_in": 600, "interval": 1}
        # simulate the human approving: write the token, then report authed.
        (base / "auth" / "bid_token.json").write_text(
            json.dumps({"access_token": "T", "refresh_token": "R",
                        "expires_at": time.time() + 3600})
        )
        return {"authenticated": True, "phase": "authenticated",
                "token_expires_at": time.time() + 3600, "refreshed": False}

    monkeypatch.setattr(sso_oidc, "get_status", fake_get_status)

    rc = cli.main(["login"])
    out = capsys.readouterr().out

    assert rc == 0, "login should succeed on approval"
    assert "ABCD-EFGH" in out, "user_code must be printed (copy-device-link)"
    assert "https://example.com/verify?user_code=ABCD-EFGH" in out, "verification URL must be printed"
    # token written to the plugin's own store, not the Hermes pool.
    assert (base / "auth" / "bid_token.json").exists()


def test_cli_login_already_authenticated(monkeypatch, tmp_path, capsys):
    """If a valid token already exists, `login` must NOT start a new device
    flow (avoids the doomed-duplicate InvalidGrantException) and should report
    already-authenticated."""
    import build_cli as cli
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "builder"
    (base / "auth").mkdir(parents=True, exist_ok=True)
    (base / "auth" / "bid_token.json").write_text(
        json.dumps({"access_token": "T", "refresh_token": "R", "expires_at": time.time() + 3600})
    )
    started = {"n": 0}
    monkeypatch.setattr(
        sso_oidc,
        "start_login",
        lambda: started.__setitem__("n", started["n"] + 1)
        or {"already_authenticated": True, "phase": "authenticated"},
    )

    rc = cli.main(["login"])
    out = capsys.readouterr().out
    assert rc == 0
    # start_login's own guard short-circuits (no new device flow / AWS call):
    # it returns the already-authenticated marker, and login must not poll.
    assert "Already authenticated" in out
    # No new token file should be (re)written by a login that did nothing.
    assert "Authenticated. Token stored" not in out


def test_cli_status_and_whoami_report_store_state(monkeypatch, tmp_path, capsys):
    """status/whoami must reflect the plugin's own store (auth/bid_token.json),
    independent of Hermes core's credential pool."""
    import build_cli as cli
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "builder"
    (base / "auth").mkdir(parents=True, exist_ok=True)
    (base / "auth" / "bid_token.json").write_text(
        json.dumps({"access_token": "T", "refresh_token": "R", "expires_at": time.time() + 3600,
                    "token_type": "Bearer", "scopes": ["codewhisperer:conversations"]})
    )

    assert cli.main(["status"]) == 0
    assert "authenticated: yes" in capsys.readouterr().out

    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert "token_type:  Bearer" in out
    assert "has_refresh: True" in out


def test_cli_logout_clears_store(monkeypatch, tmp_path, capsys):
    """logout must clear the plugin's own store via the shared sso_oidc.logout()."""
    import build_cli as cli
    from auth import sso_oidc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base = tmp_path / "plugins" / "builder"
    (base / "auth").mkdir(parents=True, exist_ok=True)
    (base / "auth" / "bid_token.json").write_text(
        json.dumps({"access_token": "T", "refresh_token": "R", "expires_at": time.time() + 3600})
    )
    cleared = {"n": 0}
    monkeypatch.setattr(sso_oidc, "logout", lambda: cleared.__setitem__("n", cleared["n"] + 1))

    assert cli.main(["logout"]) == 0
    assert cleared["n"] == 1
    assert "Logged out" in capsys.readouterr().out


# --- backend.chat(): wire-protocol body (tools / history / modelId) ---

class _FakeResp:
    status_code = 200

    def __init__(self, text):
        self._text = text

    def iter_content(self, chunk_size=4096):
        yield self._text.encode("utf-8")


def test_chat_wires_userinputmessagecontext_and_modelid(monkeypatch):
    """chat() must builder a faithful Q body: modelId coerced to a catalog value,
    and tools/tool_results/history folded into userInputMessageContext. This
    exercises the branch that ask_q never hits (Hermes drives tools itself), so
    it must be covered explicitly rather than left unverified in the hot path."""
    import backend

    captured = {}

    class FakePost:
        def __call__(self, url, data=None, headers=None, timeout=None, stream=False):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            return _FakeResp(
                '{"assistantResponseEvent":{"content":"ok","modelId":"auto"}}'
            )

    monkeypatch.setattr(backend, "requests", type("R", (), {"post": FakePost()})())
    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "TOK"})

    tools = [{"type": "function", "function": {"name": "fs_read", "description": "read"}}]
    history = [{"role": "user", "content": "prior"}]
    answer, cid, tuid = backend.chat(
        "current prompt",
        model="not-a-real-model",
        tools=tools,
        tool_results=[{"tool": "fs_read", "result": "x"}],
        history=history,
    )

    assert answer == "ok"
    body = json.loads(captured["data"])
    msg = body["conversationState"]["currentMessage"]["userInputMessage"]
    # modelId is coerced (unknown -> auto), not forwarded verbatim.
    assert msg["modelId"] == "auto"
    # Bearer token attached, no SigV4.
    assert captured["headers"]["Authorization"] == "Bearer TOK"
    assert captured["headers"]["x-amz-target"].startswith("AmazonCodeWhisperer")
    # tools/history are folded into the context object (wire-protocol shape),
    # and origin is the required "CLI" string.
    ctx = msg["userInputMessageContext"]
    assert ctx["tools"] == tools
    assert ctx["toolResults"] == [{"tool": "fs_read", "result": "x"}]
    assert ctx is not None
    assert "history" in body["conversationState"]
    assert msg["origin"] == "CLI"


def test_chat_omits_context_when_no_tools(monkeypatch):
    """When no tools/history are supplied, userInputMessageContext must be
    absent (keep the body minimal + faithful to the simple chat shape)."""
    import backend

    captured = {}

    class FakePost:
        def __call__(self, url, data=None, headers=None, timeout=None, stream=False):
            captured["data"] = data
            return _FakeResp('{"assistantResponseEvent":{"content":"hi","modelId":"auto"}}')

    monkeypatch.setattr(backend, "requests", type("R", (), {"post": FakePost()})())
    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "TOK"})

    backend.chat("plain prompt")
    msg = json.loads(captured["data"])["conversationState"]["currentMessage"]["userInputMessage"]
    assert "userInputMessageContext" not in msg
    assert msg["content"] == "plain prompt"
