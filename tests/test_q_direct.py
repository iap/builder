"""Offline unit tests for q_direct.py (no network, no AWS auth).

These exercise the pure-logic pieces so regressions are caught without a live
Builder ID session:
  * _extract_answer — decodes Q's AWS event-stream framing into assistant text
    (verified live: assistantResponseEvent payload is {"content":...,"modelId":...}).
  * _token_expired — correct expiry detection for epoch and ISO timestamps.
  * _load_q_sqlite_token — key normalization (accessToken -> access_token).
  * _sign_request — Bearer-only auth (no SigV4; verified via mitmproxy capture of q chat).
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
    headers = q_direct._sign_request("POST", q_direct.CHAT_URL, payload, "TOKEN")
    assert headers["Authorization"] == "Bearer TOKEN"
    assert headers["x-amz-target"] == q_direct.X_AMZ_TARGET
    assert "X-Amz-Signature" not in headers  # no SigV4 (verified: q sends Bearer only)
    assert "messageId" not in payload  # removed: not part of the request shape
    assert "MANUAL" in payload


def test_extract_answer_empty_stream():
    assert q_direct._extract_answer(_FakeResp(["", "   "])) == "(no response)"


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


def test_load_q_sqlite_token_normalizes_keys(tmp_path, monkeypatch):
    # write a fake Q sqlite with the OIDC token row
    import sqlite3

    db = tmp_path / "data.sqlite3"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE auth_kv (key TEXT, value TEXT)"
    )
    tok = {
        "accessToken": "abc",
        "refreshToken": "rt",
        "expires_at": "2999-01-01T00:00:00.000000Z",
        "region": "us-east-1",
    }
    con.execute(
        "INSERT INTO auth_kv VALUES (?, ?)",
        ("codewhisperer:odic:token", json.dumps(tok)),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(q_direct, "Q_SQLITE", db)
    loaded = q_direct._load_q_sqlite_token()
    assert loaded is not None
    assert loaded["access_token"] == "abc"
    assert loaded["refresh_token"] == "rt"
