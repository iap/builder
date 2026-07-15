"""Offline unit tests for q_direct.py (no network, no AWS auth).

These exercise the pure-logic pieces so regressions are caught without a live
Builder ID session:
  * _extract_answer — parses Q's Coral JSON event stream into assistant text.
  * _token_expired — correct expiry detection for epoch and ISO timestamps.
  * _load_q_sqlite_token — key normalization (accessToken -> access_token).
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
    stream = (
        '{"generateAssistantResponseResponse":{"event":'
        '{"assistantResponseMessage":{"content":"hello world"}}}}'
    )
    assert q_direct._extract_answer(_FakeResp([stream])) == "hello world"


def test_extract_answer_multiple_events():
    # two events, each a separate JSON line
    e1 = '{"generateAssistantResponseResponse":{"event":{"assistantResponseMessage":{"content":"foo "}}}}'
    e2 = '{"generateAssistantResponseResponse":{"event":{"assistantResponseMessage":{"content":"bar"}}}}'
    out = q_direct._extract_answer(_FakeResp([e1 + "\n", e2 + "\n"]))
    assert out == "foo bar"


def test_extract_answer_split_across_chunks():
    # one JSON object split across two network chunks (no newline boundary)
    payload = '{"generateAssistantResponseResponse":{"event":{"assistantResponseMessage":{"content":"split"}}}}'
    out = q_direct._extract_answer(_FakeResp([payload[:20], payload[20:]]))
    assert out == "split"


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
