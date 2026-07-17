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

    answer, cid = q_direct.chat("hi", model="claude-sonnet-4")
    assert answer == "answer"
    assert cid == "conv-xyz"


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
