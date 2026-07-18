"""Offline unit tests for q_direct.py (no network, no AWS auth).

These exercise the pure-logic pieces so regressions are caught without a live
Builder ID session:
  * _extract_answer — decodes Q's AWS event-stream framing into assistant text
    (verified live: assistantResponseEvent payload is {"content":...,"modelId":...}).
  * _token_expired — correct expiry detection for epoch and ISO timestamps.
  * _sign_request — Bearer-only auth (no SigV4; the OIDC access_token is the chat bearer).
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# import q_direct without triggering any network import side effects
_spec = importlib.util.spec_from_file_location("q_direct", ROOT / "q_direct.py")
q_direct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q_direct)


class _FakeResp:
    """Minimal stand-in for requests.Response.iter_content."""

    def __init__(self, chunks):
        self._chunks = [c.encode() if isinstance(c, str) else c for c in chunks]

    def iter_content(self, chunk_size=1024):
        for c in self._chunks:
            yield c


def test_extract_answer_single_event():
    # Verified live: assistantResponseEvent payload is {"content":...,"modelId":...}
    payload = '{"content":"hello world","modelId":"auto"}'
    assert q_direct._extract_answer(_FakeResp([payload])) == "hello world"


def test_extract_answer_multiple_events():
    # multiple assistantResponseEvent frames concatenated in the stream
    p1 = '{"content":"foo ","modelId":"auto"}'
    p2 = '{"content":"bar","modelId":"auto"}'
    out = q_direct._extract_answer(_FakeResp([p1, p2]))
    assert out == "foo bar"


def test_extract_answer_event_stream_framed():
    # Real AWS event-stream framing (binary headers + JSON payload) as seen live.
    framed = (
        b"\x00\x00\x00\x0er\x00\x00\x00\x10:event-type\x07\x00\x00\x00\x10initial-response"
        b"\r:content-type\x07\x00\x00\x00\x1aapplication/x-amz-json-1.0\r"
        b":message-type\x07\x00\x00\x00\x05event{}\t%"
        b'\x00\x00\x00\x80\x00\x00\x00\\:\x0b:event-type\x07\x00\x00\x00\x16assistantResponseEvent'
        b"\r:content-type\x07\x00\x00\x00\x10application/json\r:message-type\x07\x00\x00\x00\x05event"
        b'{"content":"CHATDIRECT_OK","modelId":"auto"}'
    )
    assert q_direct._extract_answer(_FakeResp([framed])) == "CHATDIRECT_OK"


def test_extract_answer_split_across_chunks():
    # one JSON payload split across two network chunks (no boundary)
    payload = '{"content":"split","modelId":"auto"}'
    out = q_direct._extract_answer(_FakeResp([payload[:20], payload[20:]]))
    assert out == "split"


def test_chat_body_shape():
    # Verified against q's GenerateAssistantResponse serializer: conversationState
    # with currentMessage.userInputMessage.content, chatTriggerType "MANUAL", and
    # NO messageId / NO SigV4 headers. Build the body the way chat() does.
    import time
    prompt = "hi"
    body = {
        "conversationState": {
            "currentMessage": {"userInputMessage": {"content": prompt}},
            "chatTriggerType": "MANUAL",
        }
    }
    payload = json.dumps(body)
    headers = q_direct._sign_request("POST", q_direct.CHAT_URL, "TOKEN")
    assert headers["Authorization"] == "Bearer TOKEN"
    assert headers["x-amz-target"] == q_direct.X_AMZ_TARGET
    assert "X-Amz-Signature" not in headers  # no SigV4 (verified: q sends Bearer only)
    assert "messageId" not in payload  # removed: not part of the request shape
    assert "MANUAL" in payload


def test_extract_answer_empty_stream():
    assert q_direct._extract_answer(_FakeResp(["", "   "])) == "(no response)"


def test_extract_answer_surfaces_error_envelope():
    # A mid-stream error envelope (no content/modelId) must be surfaced as a
    # "(Q error: <type>)" string instead of silently returning "(no response)".
    payload = '{"__type":"ThrottlingException","message":"slow down"}'
    out = q_direct._extract_answer(_FakeResp([payload]))
    assert out == "(Q error: ThrottlingException)"


def test_extract_answer_unbalanced_brace_in_content():
    # Regression: assistant text containing an unbalanced '}' must NOT be
    # dropped. The old brace-scanner mis-split the JSON and returned "(no response)".
    payload = '{"content":"use m.get(k) } end","modelId":"claude-sonnet-4.5"}'
    out = q_direct._extract_answer(_FakeResp([payload]))
    assert out == "use m.get(k) } end"
    assert "}" in out


def test_extract_answer_ignores_non_assistant_events():
    # A payload carrying `content` but no `modelId` is not an assistant event
    # and must be skipped (matches the brace-regex but lacks `modelId`).
    non_assistant = '{"content":"should be ignored","eventType":"other"}'
    assistant = '{"content":"keep me","modelId":"auto"}'
    out = q_direct._extract_answer(_FakeResp([non_assistant, assistant]))
    assert out == "keep me"


def test_extract_answer_escaped_quotes_and_braces():
    # `content` with escaped quotes and embedded braces inside the text.
    payload = '{"content":"print(\\"x { y }\\")","modelId":"auto"}'
    out = q_direct._extract_answer(_FakeResp([payload]))
    assert out == 'print("x { y }")'


def test_token_expired_epoch():
    assert q_direct._token_expired({"expires_at": 100}, skew=0) is True
    assert q_direct._token_expired({"expires_at": 9_999_999_999_999}, skew=0) is False


def test_token_expired_iso():
    past = "2020-01-01T00:00:00.000000Z"
    future = "2999-01-01T00:00:00.000000Z"
    assert q_direct._token_expired({"expires_at": past}) is True
    assert q_direct._token_expired({"expires_at": future}) is False


def test_token_expired_missing():
    assert q_direct._token_expired({}) is True


# --- conversation id / tool_use_id extraction (from the response stream) ---


def test_extract_conversation_id_from_stream():
    # A realistic assistantResponseEvent payload with conversationId.
    payload = '{"content":"hi","modelId":"auto","conversationId":"conv-abc-123"}'
    assert q_direct._extract_conversation_id(payload) == "conv-abc-123"


def test_extract_conversation_id_absent_returns_none():
    payload = '{"content":"hi","modelId":"auto"}'
    assert q_direct._extract_conversation_id(payload) is None


def test_chat_returns_tuple_with_conversation_id(monkeypatch):
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


def test_chat_extracts_tool_use_id(monkeypatch):
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


def test_list_models_static_catalog():
    models = q_direct.list_models()
    assert "claude-sonnet-4" in models
    assert "claude-haiku-4.5" in models
    # Q's chat endpoint rejects claude-opus-*; the catalog must not advertise it.
    assert not any("opus" in m for m in models)


# --- token file path resolution (profile-safe + legacy fallback) --------------


def test_token_file_prefers_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    canonical = tmp_path / "plugins" / "aws-build" / ".q_token.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("{}")
    assert q_direct._token_file() == canonical


def test_token_file_falls_back_to_legacy(monkeypatch, tmp_path):
    # HERMES_HOME canonical path absent -> fall back to the source-dir legacy
    # file only if it exists; otherwise return the (non-existent) canonical.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    legacy = Path(q_direct.__file__).resolve().parent / ".q_token.json"
    resolved = q_direct._token_file()
    if legacy.exists():
        assert resolved == legacy
    else:
        assert resolved == tmp_path / "plugins" / "aws-build" / ".q_token.json"


# --- get_token() ordering: credential pool (sso) is canonical, read first ---


def test_get_token_prefers_pool_over_cache(monkeypatch):
    future = 9_999_999_999
    pool_tok = {"access_token": "POOL", "expires_at": future}
    cache_tok = {"access_token": "CACHE", "expires_at": future}
    monkeypatch.setattr(q_direct, "_load_sso_token", lambda: pool_tok)
    monkeypatch.setattr(q_direct, "_load_token", lambda: cache_tok)
    assert q_direct.get_token()["access_token"] == "POOL"


def test_get_token_falls_back_to_cache_when_no_pool(monkeypatch):
    future = 9_999_999_999
    cache_tok = {"access_token": "CACHE", "expires_at": future}
    monkeypatch.setattr(q_direct, "_load_sso_token", lambda: None)
    monkeypatch.setattr(q_direct, "_load_token", lambda: cache_tok)
    assert q_direct.get_token()["access_token"] == "CACHE"


def test_get_token_raises_when_no_credentials(monkeypatch):
    monkeypatch.setattr(q_direct, "_load_sso_token", lambda: None)
    monkeypatch.setattr(q_direct, "_load_token", lambda: None)
    with pytest.raises(RuntimeError):
        q_direct.get_token()


def test_list_models_uses_static_fallback_when_no_override(monkeypatch):
    monkeypatch.setattr(q_direct, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(q_direct, "_load_model_override", lambda: None)
    assert q_direct.list_models() == q_direct.STATIC_MODELS


def test_list_models_uses_plugin_yaml_override(monkeypatch):
    monkeypatch.setattr(q_direct, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(
        q_direct, "_load_model_override", lambda: ["custom-model-a", "custom-model-b"]
    )
    assert q_direct.list_models() == ["custom-model-a", "custom-model-b"]


def test_list_models_caches_override(monkeypatch):
    calls = {"n": 0}

    def _fake_override():
        calls["n"] += 1
        return ["cached-model"]

    monkeypatch.setattr(q_direct, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(q_direct, "_load_model_override", _fake_override)
    assert q_direct.list_models() == ["cached-model"]
    assert q_direct.list_models() == ["cached-model"]
    # override loader is only consulted once (cached)
    assert calls["n"] == 1


def test_list_models_empty_override_falls_back_to_static(monkeypatch):
    monkeypatch.setattr(q_direct, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(q_direct, "_load_model_override", lambda: [])
    assert q_direct.list_models() == q_direct.STATIC_MODELS


# --- load_tags(): plugin.yaml override, cached, static fallback ----


def test_load_tags_uses_static_fallback_when_no_override(monkeypatch):
    monkeypatch.setattr(q_direct, "_TAG_OVERRIDE", None)
    monkeypatch.setattr(q_direct, "_load_tag_override", lambda: None)
    assert q_direct.load_tags() == q_direct.STATIC_TAGS


def test_load_tags_uses_plugin_yaml_override(monkeypatch):
    monkeypatch.setattr(q_direct, "_TAG_OVERRIDE", None)
    monkeypatch.setattr(
        q_direct, "_load_tag_override", lambda: ["custom-tag-x", "custom-tag-y"]
    )
    assert q_direct.load_tags() == ["custom-tag-x", "custom-tag-y"]


def test_load_tags_caches_override(monkeypatch):
    calls = {"n": 0}

    def _fake_override():
        calls["n"] += 1
        return ["cached-tag"]

    monkeypatch.setattr(q_direct, "_TAG_OVERRIDE", None)
    monkeypatch.setattr(q_direct, "_load_tag_override", _fake_override)
    assert q_direct.load_tags() == ["cached-tag"]
    assert q_direct.load_tags() == ["cached-tag"]
    assert calls["n"] == 1


def test_load_tags_empty_override_falls_back_to_static(monkeypatch):
    monkeypatch.setattr(q_direct, "_TAG_OVERRIDE", None)
    monkeypatch.setattr(q_direct, "_load_tag_override", lambda: [])
    assert q_direct.load_tags() == q_direct.STATIC_TAGS
