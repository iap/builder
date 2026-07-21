"""Offline unit tests for backend.py (no network, no AWS auth).

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
from auth import sso_oidc

# import backend without triggering any network import side effects
_spec = importlib.util.spec_from_file_location("backend", ROOT / "backend.py")
backend = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backend)


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
    assert backend._extract_answer(_FakeResp([payload])) == "hello world"


def test_extract_answer_multiple_events():
    # multiple assistantResponseEvent frames concatenated in the stream
    p1 = '{"content":"foo ","modelId":"auto"}'
    p2 = '{"content":"bar","modelId":"auto"}'
    out = backend._extract_answer(_FakeResp([p1, p2]))
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
    assert backend._extract_answer(_FakeResp([framed])) == "CHATDIRECT_OK"


def test_extract_answer_split_across_chunks():
    # one JSON payload split across two network chunks (no boundary)
    payload = '{"content":"split","modelId":"auto"}'
    out = backend._extract_answer(_FakeResp([payload[:20], payload[20:]]))
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
    headers = backend._sign_request("TOKEN")
    assert headers["Authorization"] == "Bearer TOKEN"
    assert headers["x-amz-target"] == backend.X_AMZ_TARGET
    assert "X-Amz-Signature" not in headers  # no SigV4 (verified: q sends Bearer only)
    assert "messageId" not in payload  # removed: not part of the request shape
    assert "MANUAL" in payload


def test_extract_answer_surfaces_error_envelope():
    # A mid-stream error envelope (no content/modelId) must be surfaced as a
    # "(Q error: <type>)" string instead of silently returning "(no response)".
    payload = '{"__type":"ThrottlingException","message":"slow down"}'
    out = backend._extract_answer(_FakeResp([payload]))
    assert out == "(Q error: ThrottlingException)"


def test_extract_answer_unbalanced_brace_in_content():
    # Regression: assistant text containing an unbalanced '}' must NOT be
    # dropped. The old brace-scanner mis-split the JSON and returned "(no response)".
    payload = '{"content":"use m.get(k) } end","modelId":"claude-sonnet-4.5"}'
    out = backend._extract_answer(_FakeResp([payload]))
    assert out == "use m.get(k) } end"
    assert "}" in out


def test_extract_answer_ignores_non_assistant_events():
    # A payload carrying `content` but no `modelId` is not an assistant event
    # and must be skipped (matches the brace-regex but lacks `modelId`).
    non_assistant = '{"content":"should be ignored","eventType":"other"}'
    assistant = '{"content":"keep me","modelId":"auto"}'
    out = backend._extract_answer(_FakeResp([non_assistant, assistant]))
    assert out == "keep me"


def test_extract_answer_escaped_quotes_and_braces():
    # `content` with escaped quotes and embedded braces inside the text.
    payload = '{"content":"print(\\"x { y }\\")","modelId":"auto"}'
    out = backend._extract_answer(_FakeResp([payload]))
    assert out == 'print("x { y }")'


def test_extract_answer_empty_stream():
    assert backend._extract_answer(_FakeResp(["", "   "])) == "(no response)"


def test_extract_conversation_id_from_stream():
    # A realistic assistantResponseEvent payload with conversationId.
    payload = '{"content":"hi","modelId":"auto","conversationId":"conv-abc-123"}'
    assert backend._extract_conversation_id(payload) == "conv-abc-123"


def test_extract_conversation_id_absent_returns_none():
    payload = '{"content":"hi","modelId":"auto"}'
    assert backend._extract_conversation_id(payload) is None


def test_chat_returns_tuple_with_conversation_id(monkeypatch):
    class _FakeResp:
        status_code = 200

        def iter_content(self, chunk_size=1024):
            # assistantResponseEvent with a conversationId; Q also emits it
            yield b'{"content":"answer","modelId":"auto","conversationId":"conv-xyz"}'

    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(backend.requests, "post", lambda *a, **k: _FakeResp())

    answer, cid, tool_use_id = backend.chat("hi", model="claude-sonnet-4")
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

    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(backend.requests, "post", lambda *a, **k: _FakeResp())

    answer, cid, tool_use_id = backend.chat("hi", model="claude-sonnet-4")
    assert "fs_write" in answer
    assert tool_use_id == "tu-9"


def test_list_models_static_catalog():
    models = backend.list_models()
    assert "claude-sonnet-4" in models
    assert "claude-haiku-4.5" in models
    # Q's chat endpoint rejects claude-opus-*; the catalog must not advertise it.
    assert not any("opus" in m for m in models)


# --- get_token(): delegates to auth.sso_oidc (single store) ---


def test_get_token_returns_sso_token(monkeypatch):
    "get_token() returns the sso_oidc mirror token when authenticated."
    fake_status = {"authenticated": True}
    fake_tok = {"access_token": "SSO", "expires_at": 9_999_999_999}
    monkeypatch.setattr(sso_oidc, "get_status", lambda: fake_status)
    monkeypatch.setattr(sso_oidc, "_load_token", lambda: fake_tok)
    monkeypatch.setattr(sso_oidc, "refresh_token", lambda: False)
    assert backend.get_token()["access_token"] == "SSO"


def test_get_token_silent_refresh_on_expiry(monkeypatch):
    "Expired sso token -> silent refresh_token(), then re-read."
    refreshed = {"access_token": "REFRESHED", "expires_at": 9_999_999_999}
    calls = {"refresh": 0, "reread": 0}

    def _refresh():
        calls["refresh"] += 1
        return True

    def _load():
        # In the real flow refresh_token() persists the new token, then the
        # single post-refresh _load_token() call reads it back.
        calls["reread"] += 1
        return refreshed

    monkeypatch.setattr(sso_oidc, "get_status", lambda: {"authenticated": False})
    monkeypatch.setattr(sso_oidc, "_load_token", _load)
    monkeypatch.setattr(sso_oidc, "refresh_token", _refresh)
    tok = backend.get_token()
    assert tok["access_token"] == "REFRESHED"
    assert calls["refresh"] == 1


def test_get_token_raises_when_no_credentials(monkeypatch):
    "No sso token and refresh fails -> actionable RuntimeError."
    monkeypatch.setattr(sso_oidc, "get_status", lambda: {"authenticated": False})
    monkeypatch.setattr(sso_oidc, "_load_token", lambda: None)
    monkeypatch.setattr(sso_oidc, "refresh_token", lambda: False)
    with pytest.raises(RuntimeError):
        backend.get_token()


def test_list_models_uses_static_fallback_when_no_override(monkeypatch):
    monkeypatch.setattr(backend, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(backend, "_load_model_override", lambda: None)
    assert backend.list_models() == backend.STATIC_MODELS


def test_list_models_uses_plugin_yaml_override(monkeypatch):
    monkeypatch.setattr(backend, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(
        backend, "_load_model_override", lambda: ["custom-model-a", "custom-model-b"]
    )
    assert backend.list_models() == ["custom-model-a", "custom-model-b"]


def test_list_models_caches_override(monkeypatch):
    calls = {"n": 0}

    def _fake_override():
        calls["n"] += 1
        return ["cached-model"]

    monkeypatch.setattr(backend, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(backend, "_load_model_override", _fake_override)
    assert backend.list_models() == ["cached-model"]
    assert backend.list_models() == ["cached-model"]
    # override loader is only consulted once (cached)
    assert calls["n"] == 1


def test_list_models_empty_override_falls_back_to_static(monkeypatch):
    monkeypatch.setattr(backend, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(backend, "_load_model_override", lambda: [])
    assert backend.list_models() == backend.STATIC_MODELS


# --- load_tags(): plugin.yaml override, cached, static fallback ---


def test_load_tags_uses_static_fallback_when_no_override(monkeypatch):
    monkeypatch.setattr(backend, "_TAG_OVERRIDE", None)
    monkeypatch.setattr(backend, "_load_tag_override", lambda: None)
    assert backend.load_tags() == backend.STATIC_TAGS


def test_load_tags_uses_plugin_yaml_override(monkeypatch):
    monkeypatch.setattr(backend, "_TAG_OVERRIDE", None)
    monkeypatch.setattr(
        backend, "_load_tag_override", lambda: ["custom-tag-x", "custom-tag-y"]
    )
    assert backend.load_tags() == ["custom-tag-x", "custom-tag-y"]


def test_load_tags_caches_override(monkeypatch):
    calls = {"n": 0}

    def _fake_override():
        calls["n"] += 1
        return ["cached-tag"]

    monkeypatch.setattr(backend, "_TAG_OVERRIDE", None)
    monkeypatch.setattr(backend, "_load_tag_override", _fake_override)
    assert backend.load_tags() == ["cached-tag"]
    assert backend.load_tags() == ["cached-tag"]
    assert calls["n"] == 1


def test_load_tags_empty_override_falls_back_to_static(monkeypatch):
    monkeypatch.setattr(backend, "_TAG_OVERRIDE", None)
    monkeypatch.setattr(backend, "_load_tag_override", lambda: [])
    assert backend.load_tags() == backend.STATIC_TAGS


# --- chat(): refresh-then-retry is bounded (no infinite recursion) ---


def test_chat_bounded_refresh_retry(monkeypatch):
    """A 401 that persists after a successful refresh must raise, not recurse
    forever.
    """

    class _FailResp:
        status_code = 401

        @property
        def text(self):
            return '{"__type":"UnauthorizedException","message":"invalid"}'

        def iter_content(self, chunk_size=1024):
            return iter([])

    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(backend.requests, "post", lambda *a, **k: _FailResp())
    # Refresh "succeeds" but the next 401 still happens -> bounded retry.
    monkeypatch.setattr(sso_oidc, "refresh_token", lambda: True)
    with pytest.raises(RuntimeError):
        # The first call retries once after refresh; the second 401 must raise
        # rather than recursing forever.
        backend.chat("hi", model="claude-sonnet-4")


def test_chat_sends_model_id(monkeypatch):
    """chat() must forward `model` to Q as `modelId` (verified live: Q accepts
    and echoes it)."""
    captured = {}

    class _OkResp:
        status_code = 200

        def iter_content(self, chunk_size=1024):
            # minimal valid stream carrying content + modelId
            yield '{"content":"ok","modelId":"claude-sonnet-4.5"}'.encode()

    def _fake_post(url, **kwargs):
        captured["body"] = kwargs.get("data")
        return _OkResp()

    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(backend.requests, "post", _fake_post)

    backend.chat("hi", model="claude-sonnet-4.5")
    body = json.loads(captured["body"])
    assert body["conversationState"]["currentMessage"]["userInputMessage"]["modelId"] == "claude-sonnet-4.5"


def test_chat_defaults_model_to_auto(monkeypatch):
    """When no model is given, chat() must default modelId to 'auto' so a
    Free-tier Builder ID gets a usable response rather than an entitlement
    error."""
    captured = {}

    class _OkResp:
        status_code = 200

        def iter_content(self, chunk_size=1024):
            yield '{"content":"ok","modelId":"auto"}'.encode()

    def _fake_post(url, **kwargs):
        captured["body"] = kwargs.get("data")
        return _OkResp()

    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(backend.requests, "post", _fake_post)

    backend.chat("hi")
    body = json.loads(captured["body"])
    assert body["conversationState"]["currentMessage"]["userInputMessage"]["modelId"] == "auto"


def test_chat_surfaces_subscription_error(monkeypatch):
    """A Q entitlement/subscription rejection must be surfaced as a clear
    RuntimeError (not swallowed, not treated as a token error)."""

    class _SubResp:
        status_code = 403

        @property
        def text(self):
            return '{"__type":"AccessDeniedException","message":"not subscribed to Q Developer"}'

        def iter_content(self, chunk_size=1024):
            return iter([])

    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(backend.requests, "post", lambda *a, **k: _SubResp())

    with pytest.raises(RuntimeError) as exc:
        backend.chat("hi", model="claude-sonnet-4.5")
    assert "entitlement" in str(exc.value).lower() or "subscri" in str(exc.value).lower()
    # Must NOT attempt a token refresh on a subscription error.
    assert "rejected the bearer token" not in str(exc.value)



# --- chat() works across EVERY model the plugin advertises ---
# Robust check: each model from list_models() (+ the "auto" passthrough)
# must survive a full chat() call -> Q request -> streamed answer parse.
# Uses a fake Q responder (no live token / network).


import itertools as _it

_ALL_PLUGIN_MODELS = list(backend.list_models()) + ["auto"]


def test_chat_works_for_every_advertised_model(monkeypatch):
    """Every model the plugin exposes must round-trip through chat()."""
    captured = {}

    class _OkResp:
        status_code = 200

        def iter_content(self, chunk_size=1024):
            # Minimal valid assistantResponseEvent stream.
            yield (
                '{"content":"ok","modelId":"%s"}' % captured["model"]
            ).encode()

    def _fake_post(url, **kwargs):
        captured["body"] = kwargs.get("data")
        captured["model"] = kwargs.get("model")  # not used; body holds it
        return _OkResp()

    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(backend.requests, "post", _fake_post)

    for m in _ALL_PLUGIN_MODELS:
        captured.clear()
        answer, _cid, _tuid = backend.chat("ping", model=m)
        body = json.loads(captured["body"])
        sent = body["conversationState"]["currentMessage"]["userInputMessage"]["modelId"]
        assert sent == m, f"model '{m}' not forwarded as modelId (got '{sent}')"
        assert answer == "ok", f"model '{m}' yielded no parsed answer"
    # No model left untested.
    assert set(_ALL_PLUGIN_MODELS) == {
        "claude-haiku-4.5",
        "claude-sonnet-4",
        "claude-sonnet-4.5",
        "auto",
    }


def test_chat_empty_model_defaults_to_auto(monkeypatch):
    "When model is ''/None, chat() must send modelId 'auto'."
    captured = {}

    class _OkResp:
        status_code = 200

        def iter_content(self, chunk_size=1024):
            yield '{"content":"ok","modelId":"auto"}'.encode()

    def _fake_post(url, **kwargs):
        captured["body"] = kwargs.get("data")
        return _OkResp()

    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(backend.requests, "post", _fake_post)

    backend.chat("hi", model="")
    body = json.loads(captured["body"])
    assert (
        body["conversationState"]["currentMessage"]["userInputMessage"]["modelId"] == "auto"
    )


# --- model-id resolution (unknown model -> auto; guards Q's opaque HTTP 500) ---
def test_resolve_model_id_passes_known_and_auto():
    """Known catalog names + 'auto' are forwarded verbatim."""
    for m in [*backend.list_models(), "auto"]:
        assert backend._resolve_model_id(m) == m


def test_resolve_model_id_empty_and_none_default_to_auto():
    assert backend._resolve_model_id("") == "auto"
    assert backend._resolve_model_id(None) == "auto"
    assert backend._resolve_model_id("   ") == "auto"


def test_resolve_model_id_coerces_unknown_to_auto():
    """Unknown names and plausible typos coerce to 'auto'.

    Verified live: Q returns an opaque HTTP 500 (InternalServerException) for
    ANY unsupported modelId, including the dashed typo 'claude-sonnet-4-5' and
    unrelated names like 'gpt-4-turbo'. Coercing to 'auto' turns that crash
    into a usable response.
    """
    assert backend._resolve_model_id("gpt-4-turbo") == "auto"
    assert backend._resolve_model_id("claude-sonnet-4-5") == "auto"  # dashed typo
    assert backend._resolve_model_id("does-not-exist") == "auto"


def test_chat_coerces_unknown_model_to_auto(monkeypatch):
    """End-to-end: an unknown model reaches Q as modelId 'auto', not the raw
    string (which would 500)."""
    captured = {}

    class _OkResp:
        status_code = 200

        def iter_content(self, chunk_size=1024):
            yield '{"content":"ok","modelId":"auto"}'.encode()

    def _fake_post(url, **kwargs):
        captured["body"] = kwargs.get("data")
        return _OkResp()

    monkeypatch.setattr(backend, "get_token", lambda: {"access_token": "tok"})
    monkeypatch.setattr(backend.requests, "post", _fake_post)

    backend.chat("hi", model="gpt-4-turbo")
    body = json.loads(captured["body"])
    assert (
        body["conversationState"]["currentMessage"]["userInputMessage"]["modelId"] == "auto"
    )


def test_import_sso_oidc_returns_module():
    """_import_sso_oidc() resolves the auth.sso_oidc module regardless of load
    style (relative-first, absolute-fallback). Guards the live regression where
    a bare `from auth import sso_oidc` raised 'No module named auth' under
    core's package load and masked the real token error."""
    mod = backend._import_sso_oidc()
    assert hasattr(mod, "get_status")
    assert hasattr(mod, "refresh_token")
    assert hasattr(mod, "_load_token")

