"""Live tests for amazon_q_bridge.py (OpenAI + Anthropic-native modes).

Set BRIDGE_LIVE=1 to actually start the bridge and hit `q chat`. Without it
these are skipped (CI has no `q` binary / auth). The bridge is launched on an
ephemeral port in-process via subprocess and torn down after.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest

BRIDGE = (
    Path(__file__).resolve().parent.parent / "amazon_q_bridge.py"
)
LIVE = os.environ.get("BRIDGE_LIVE") == "1"
MODEL = "claude-sonnet-4"


def _post(port, path, payload, raw=False):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return r.status, dict(r.headers), body if raw else json.loads(body)
    except urllib.error.HTTPError as e:  # noqa: BLE001
        body = e.read()
        return e.code, dict(e.headers), body if raw else json.loads(body)


@pytest.fixture(scope="module")
def bridge():
    if not LIVE:
        pytest.skip("set BRIDGE_LIVE=1")
    # Find a free port.
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for readiness.
    for _ in range(50):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/models", timeout=5
            ) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        proc.kill()


def test_openai_no_regression(bridge):
    status, _, body = _post(
        bridge,
        "/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "reply with exactly: OAI_OK"}]},
    )
    assert status == 200
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]
    assert "OAI_OK" in body["choices"][0]["message"]["content"]


def test_openai_sse_stream(bridge):
    # Hermes' openai_chat transport REQUIRES SSE; a plain JSON body fails with
    # "empty stream, no finish_reason". The bridge must emit text/event-stream.
    status, headers, raw = _post(
        bridge,
        "/v1/chat/completions",
        {"model": MODEL, "stream": True,
         "messages": [{"role": "user", "content": "reply with exactly: SSE_OK"}]},
        raw=True,
    )
    assert status == 200
    # http.client preserves original header case -> look up case-insensitively
    ct = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    assert ct.startswith("text/event-stream")
    body = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
    assert "data: " in body
    assert "data: [DONE]" in body
    # the content chunk carries the answer
    assert "SSE_OK" in body


def test_openai_native_shape(bridge):
    status, headers, body = _post(
        bridge,
        "/v1/anthropic/messages",
        {
            "model": MODEL,
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "reply with exactly: ANTH_OK"}],
        },
    )
    assert status == 200
    # Anthropic Messages API shape (verified against real api.anthropic.com spec)
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"][0]["type"] == "text"
    assert "ANTH_OK" in body["content"][0]["text"]
    assert body["stop_reason"] in ("end_turn", "stop_sequence", "max_tokens")
    assert "usage" in body and "input_tokens" in body["usage"]
    # The anthropic-version header must be present (SDKs inspect it).
    assert headers.get("anthropic-version") == "bedrock-2023-05-31"


def test_anthropic_unique_ids(bridge):
    # Two calls must produce distinct `id`s (reviewer found pid-collision bug).
    _, _, a = _post(bridge, "/v1/anthropic/messages",
                    {"model": MODEL, "messages": [{"role": "user", "content": "say hi"}]})
    _, _, b = _post(bridge, "/v1/anthropic/messages",
                    {"model": MODEL, "messages": [{"role": "user", "content": "say bye"}]})
    assert a["id"] != b["id"]
    assert a["id"].startswith("msg_qbridge-")


def test_anthropic_list_system_and_multiturn(bridge):
    # list-form `system` (prompt-caching shape) must not be dropped, and
    # assistant turns must be included so multi-turn context survives.
    status, _, body = _post(
        bridge,
        "/v1/anthropic/messages",
        {
            "model": MODEL,
            "system": [{"type": "text", "text": "You are terse."}],
            "messages": [
                {"role": "user", "content": "Remember: the code is 42."},
                {"role": "assistant", "content": "Got it, code is 42."},
                {"role": "user", "content": "What was the code? reply with exactly: 42"},
            ],
        },
    )
    assert status == 200
    assert "42" in body["content"][0]["text"]


def test_anthropic_error_header_present(bridge):
    # 400 responses must also carry anthropic-version (reviewer: only 200 had it)
    status, headers, _ = _post(
        bridge,
        "/v1/anthropic/messages",
        {"model": MODEL, "messages": "not-a-list"},
    )
    assert status == 400
    assert headers.get("anthropic-version") == "bedrock-2023-05-31"


def test_anthropic_rejects_stream(bridge):
    status, _, body = _post(
        bridge,
        "/v1/anthropic/messages",
        {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 400
    assert body["type"] == "invalid_request_error"
