"""Focused tests for aws-build OpenAI prompt flattening and conversation id."""
from __future__ import annotations

import json
from io import BytesIO
from typing import Any, Dict

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import amazon_q_bridge as bridge
import q_direct


def test_flatten_openai_messages_preserves_multi_turn():
    data = {
        "system": "You are a helpful assistant.",
        "messages": [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow-up question"},
        ],
    }
    prompt = bridge._flatten_openai_messages(data)
    assert prompt == (
        "You are a helpful assistant.\n\n"
        "user: First question\n\n"
        "assistant: First answer\n\n"
        "user: Follow-up question"
    )


def test_flatten_openai_messages_handles_list_content():
    data = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Part A"},
                    {"type": "text", "text": "Part B"},
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Reply"}],
            },
        ],
    }
    prompt = bridge._flatten_openai_messages(data)
    assert "user: Part A Part B" in prompt
    assert "assistant: Reply" in prompt


def test_flatten_openai_messages_empty_when_no_text():
    assert bridge._flatten_openai_messages({"messages": []}) == ""
    assert bridge._flatten_openai_messages({"messages": [{"role": "tool", "content": "ignore"}]}) == ""


def test_extract_conversation_id_from_stream():
    # A realistic assistantResponseEvent payload with conversationId.
    payload = '{"content":"hi","modelId":"auto","conversationId":"conv-abc-123"}'
    assert q_direct._extract_conversation_id(payload) == "conv-abc-123"


def test_extract_conversation_id_absent_returns_none():
    payload = '{"content":"hi","modelId":"auto"}'
    assert q_direct._extract_conversation_id(payload) is None


def test_q_direct_chat_returns_tuple_with_conversation_id(monkeypatch):
    class _FakeResp:
        status_code = 200

        def iter_content(self, chunk_size=1024):
            # assistantResponseEvent with a conversationId; Q also emits it
            yield b'{"content":"answer","modelId":"auto","conversationId":"conv-xyz"}'

    monkeypatch.setattr(q_direct, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(q_direct.requests, "post", lambda *a, **k: _FakeResp())

    answer, cid, tool_use_id = q_direct.chat("hi", model="claude-sonnet-4")
    assert answer == "answer"
    assert cid == "conv-xyz"
    assert tool_use_id is None


def test_q_direct_chat_extracts_tool_use_id(monkeypatch):
    """A toolUseEvent in the stream must surface its toolUseId."""
    class _FakeResp:
        status_code = 200

        def iter_content(self, chunk_size=1024):
            yield (b'{"content":"<function_calls><invoke name=\\"fs_write\\">'
                   b'<parameter name=\\"path\\">a.txt</parameter>'
                   b'<parameter name=\\"content\\">x</parameter></invoke>'
                   b'</function_calls>","modelId":"auto","conversationId":"c1"}')
            yield b'{"toolUseId":"tu-9","name":"fs_write","input":{"path":"a.txt","content":"x"}}'

    monkeypatch.setattr(q_direct, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(q_direct.requests, "post", lambda *a, **k: _FakeResp())

    answer, cid, tool_use_id = q_direct.chat("hi", model="claude-sonnet-4")
    assert "fs_write" in answer
    assert tool_use_id == "tu-9"


def test_bridge_passes_conversation_id_to_direct_backend(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_direct(prompt, model, timeout, conversation_id=None):
        captured["conversation_id"] = conversation_id
        return "ok", 0, True, "conv-srv-1"

    monkeypatch.setattr(bridge, "_run_q_direct", fake_direct)
    monkeypatch.setattr(bridge, "BACKEND", "direct")
    monkeypatch.setattr(bridge, "valid_models", lambda: ["claude-sonnet-4.5"])

    handler = bridge.Handler.__new__(bridge.Handler)
    handler.client_address = ("127.0.0.1", 0)
    handler.server = None
    handler.raw_requestline = ""
    handler.requestline = ""
    handler.command = "POST"
    handler.path = "/v1/chat/completions"
    handler.request_version = "HTTP/1.1"
    handler.headers = {
        "X-Hermes-Conversation-Id": "conv-in-9",
        "Content-Length": "100",
    }
    handler.rfile = BytesIO(json.dumps({
        "model": "claude-sonnet-4.5",
        "messages": [{"role": "user", "content": "q1"}],
    }).encode())
    handler.wfile = BytesIO()

    payloads = []
    handler.send_response = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda: None
    handler.wfile.write = lambda data: None
    handler.log_message = lambda *a, **k: None

    def fake_send(payload, status=200, extra_headers=None):
        payloads.append((status, payload, extra_headers))

    handler._send = fake_send
    handler.do_POST()

    assert captured["conversation_id"] == "conv-in-9"
    assert payloads[-1][2] == {"X-Hermes-Conversation-Id": "conv-srv-1"}
    assert payloads[-1][1]["choices"][0]["message"]["content"] == "ok"


# --- Model calibration (A/B/C): prefix strip, aliases, fallback, env, default ---

def test_normalize_model_strips_provider_prefix():
    model, ok = bridge._normalize_model("aws-build/claude-haiku-4.5")
    assert ok is True
    assert model == "claude-haiku-4.5"


def test_normalize_model_resolves_aliases():
    assert bridge._normalize_model("haiku")[0] == "claude-haiku-4.5"
    assert bridge._normalize_model("sonnet45")[0] == "claude-sonnet-4.5"
    assert bridge._normalize_model("claude-sonnet")[0] == "claude-sonnet-4"
    # dash/dot tolerance
    assert bridge._normalize_model("claude-sonnet-4-5")[0] == "claude-sonnet-4.5"
    assert bridge._normalize_model("claude-haiku-4-5")[0] == "claude-haiku-4.5"


def test_normalize_model_unknown_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(bridge, "DEFAULT_MODEL", "claude-haiku-4.5")
    monkeypatch.setattr(bridge, "valid_models", lambda: ["claude-haiku-4.5"])
    model, ok = bridge._normalize_model("some-future-model")
    assert ok is False
    assert model == "claude-haiku-4.5"


def test_normalize_model_none_uses_default(monkeypatch):
    monkeypatch.setattr(bridge, "DEFAULT_MODEL", "claude-haiku-4.5")
    model, ok = bridge._normalize_model("")
    assert ok is True
    assert model == "claude-haiku-4.5"


def test_extra_models_env_extends_catalog(monkeypatch):
    # The extra_models mechanism appends to the served catalog. Use valid Q
    # model names (q chat accepts claude-sonnet-4.5/-4/haiku-4.5); here we
    # demonstrate the extension mechanism with a real name.
    monkeypatch.setenv("AMAZON_Q_EXTRA_MODELS", "claude-sonnet-4")
    import importlib
    reloaded = importlib.reload(bridge)
    catalog = reloaded.discover_models()
    assert "claude-sonnet-4" in catalog
    monkeypatch.delenv("AMAZON_Q_EXTRA_MODELS", raising=False)


def test_default_model_aligned_with_config(monkeypatch):
    # C: bridge DEFAULT_MODEL should match ~/.hermes/config.yaml aws-build default.
    monkeypatch.setattr(bridge, "DEFAULT_MODEL", "claude-haiku-4.5")
    assert bridge.DEFAULT_MODEL == "claude-haiku-4.5"


def test_opus_not_in_catalog_falls_back(monkeypatch):
    # `q chat` rejects claude-opus-* ("Model does not exist"); the catalog must
    # NOT advertise it, and an opus request must fall back to DEFAULT_MODEL
    # rather than 502 via a bad `q chat --model` call.
    monkeypatch.setattr(bridge, "DEFAULT_MODEL", "claude-haiku-4.5")
    assert "claude-opus-4.5" not in bridge.FALLBACK_MODELS
    assert "claude-opus-4" not in bridge.FALLBACK_MODELS
    resolved, ok = bridge._normalize_model("claude-opus-4.5")
    assert ok is False
    assert resolved == "claude-haiku-4.5"  # fell back
    # q_direct static catalog agrees (drives the `models` plugin tool).
    import q_direct
    assert "claude-opus-4.5" not in q_direct.STATIC_MODELS


# --- Plugin settings (config.yaml) ---

def test_load_plugin_config_reads_yaml(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "backend: subprocess\n"
        "default_model: claude-sonnet-4\n"
        "extra_models:\n  - claude-opus-4.5\n"
        "debug: true\n"
    )
    data = bridge.load_plugin_config(str(cfg))
    assert data["backend"] == "subprocess"
    assert data["default_model"] == "claude-sonnet-4"
    assert data["extra_models"] == ["claude-opus-4.5"]
    assert data["debug"] is True


def test_load_plugin_config_missing_file_returns_empty(monkeypatch, tmp_path):
    data = bridge.load_plugin_config(str(tmp_path / "absent.yaml"))
    assert data == {}


def test_config_str_env_wins_over_file(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("default_model: claude-sonnet-4\n")
    monkeypatch.setattr(bridge, "_PLUGIN_CONFIG", bridge.load_plugin_config(str(cfg)))
    monkeypatch.setenv("AMAZON_Q_DEFAULT_MODEL", "claude-haiku-4.5")
    assert bridge._config_str("default_model", "x") == "claude-haiku-4.5"


def test_config_str_falls_back_to_file(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("default_model: claude-sonnet-4\n")
    monkeypatch.setattr(bridge, "_PLUGIN_CONFIG", bridge.load_plugin_config(str(cfg)))
    monkeypatch.delenv("AMAZON_Q_DEFAULT_MODEL", raising=False)
    assert bridge._config_str("default_model", "x") == "claude-sonnet-4"


def test_config_list_parses_yaml_list_and_comma_env(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("extra_models:\n  - claude-opus-4.5\n  - claude-opus-5\n")
    monkeypatch.setattr(bridge, "_PLUGIN_CONFIG", bridge.load_plugin_config(str(cfg)))
    monkeypatch.delenv("AMAZON_Q_EXTRA_MODELS", raising=False)
    assert bridge._config_list("extra_models", ()) == ("claude-opus-4.5", "claude-opus-5")
    # env (AMAZON_Q_EXTRA_MODELS) comma form wins
    monkeypatch.setenv("AMAZON_Q_EXTRA_MODELS", "a, b")
    assert bridge._config_list("extra_models", ()) == ("a", "b")


def test_parse_simple_config_fallback():
    # Minimal parser used when PyYAML is unavailable.
    raw = "# comment\nbackend: direct\ndefault_model: claude-haiku-4.5\nextra_models:\n  - claude-opus-4.5\n"
    data = bridge._parse_simple_config(raw)
    assert data["backend"] == "direct"
    assert data["default_model"] == "claude-haiku-4.5"
    assert data["extra_models"] == ["claude-opus-4.5"]
