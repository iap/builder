"""Targeted tests for uncovered paths in __init__, adapter, build_cli."""

# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

# --- __init__.py: pre_tool_call guard ---


def test_pre_tool_call_blocks_rm_rf():
    import __init__ as plugin

    result = plugin._plugin_pre_tool_call("terminal", {"command": "rm -rf /tmp/x"})
    assert result is not None
    assert result["action"] == "block"


def test_pre_tool_call_blocks_sudo():
    import __init__ as plugin

    result = plugin._plugin_pre_tool_call("terminal", {"command": "sudo apt install x"})
    assert result is not None
    assert result["action"] == "block"


def test_pre_tool_call_blocks_windows_format_drive():
    """`format <drive>:` is destructive and must stay blocked (#117)."""
    import __init__ as plugin

    for command in (
        "format C:",
        "FORMAT C:",
        "format  D:",
        "format C: /q",
        # switches may precede the drive (Greptile P1 on #119)
        "format /Q D:",
        "format /FS:NTFS E:",
        "format /q D: /v:MyVol",
        # the legacy executable name; the old bare substring missed these too
        "format.com C:",
        "format.com /Q D:",
    ):
        result = plugin._plugin_pre_tool_call("terminal", {"command": command})
        assert result is not None, command
        assert result["action"] == "block", command


def test_pre_tool_call_blocks_wrapped_windows_format_drive():
    """A wrapped `format <drive>:` must still block after the #117 narrowing.

    Anchoring the match to the start of the segment would miss these, because the
    caller splits on shell separators and hands each segment over separately.
    """
    import __init__ as plugin

    for command in (
        "env format C:",
        "xargs format C:",
        "cmd /c format C:",
        "echo hi; format C:",
    ):
        result = plugin._plugin_pre_tool_call("terminal", {"command": command})
        assert result is not None, command
        assert result["action"] == "block", command


def test_pre_tool_call_blocks_other_windows_destructive():
    """The remaining Windows patterns must not regress with the #117 change."""
    import __init__ as plugin

    for command in (
        "del /f a.txt",
        "del /s /q d",
        "rd /s /q d",
        "rd /q d",
        "fsutil volume list",
    ):
        result = plugin._plugin_pre_tool_call("terminal", {"command": command})
        assert result is not None, command
        assert result["action"] == "block", command


@pytest.mark.parametrize(
    "command",
    [
        "ruff format --check .",
        "ruff format .",
        ".venv/bin/python -m ruff format --check .",
        "npm run format",
        "clang-format -i main.c",
        "black --check .",
        # a drive-path target must not be mistaken for a Windows format drive
        # once a flag like `--check` sits between them (Greptile P1 on #119)
        "ruff format --check D:\\project",
        "ruff format . D:\\project",
        "clang-format -i D:\\project",
        "black --check D:\\project",
        "prettier --write D:\\project",
        "cd D:\\project && ruff format --check .",
    ],
)
def test_pre_tool_call_allows_format_substring_commands(command):
    """`format ` must not match as a bare substring (#117).

    The old ``"format "`` entry blocked the repo's own CI lint gate. Only the
    ``format <drive>:`` shape is destructive, so ordinary formatter invocations
    have to pass.
    """
    import __init__ as plugin

    assert plugin._plugin_pre_tool_call("terminal", {"command": command}) is None


def test_pre_tool_call_blocks_format_followed_immediately_by_drive():
    """`format D:` is indistinguishable from a formatter run on a drive path.

    ``npm run format D:\\project`` and ``format D:\\project`` reduce to the same
    ``format D:`` text once the tool name is out of view, and both are blocked.
    A flag between the two (``ruff format --check D:\\project``) is what
    separates a formatter invocation from the destructive command, so only the
    flagless form stays blocked. Closing this would mean modelling tool names,
    which turns a pattern list into a parser.
    """
    import __init__ as plugin

    for command in ("format D:\\project", "npm run format D:\\project"):
        result = plugin._plugin_pre_tool_call("terminal", {"command": command})
        assert result is not None, command
        assert result["action"] == "block", command


def test_guard_follows_symlink_swapped_after_import():
    """Protected paths must resolve per call, not freeze at import (#120 P1).

    Hoisting ``realpath`` into a module constant made the roots disagree with
    the freshly resolved write target once a protected path was replaced by a
    symlink, so the guard stopped escalating. Roots stay cached as strings;
    only the resolution is per call.
    """
    import os
    import tempfile
    from pathlib import Path

    import __init__ as plugin

    root = Path(tempfile.mkdtemp(prefix="builder-symlink-"))
    protected = root / "config.yaml"
    outside = root / "evil.yaml"
    protected.write_text("original\n")
    outside.write_text("evil\n")

    original_paths = plugin._HERMES_CORE_PATHS
    original_realpath = os.path.realpath
    try:
        plugin._HERMES_CORE_PATHS = (str(protected),)
        # simulate the swap: the protected path becomes a symlink elsewhere
        resolved_frozen = os.path.realpath(str(protected))
        protected.unlink()
        os.symlink(str(outside), str(protected))

        result = plugin._plugin_pre_tool_call("write_file", {"path": str(protected)})
        assert result is not None
        assert result["action"] == "approve"
        # the frozen root and the live target genuinely diverge here
        assert os.path.realpath(str(protected)) != resolved_frozen
    finally:
        plugin._HERMES_CORE_PATHS = original_paths
        os.path.realpath = original_realpath


def test_guard_does_not_read_an_unknown_config_root():
    """The guard must not consult config for its approval behavior (#118).

    It used to read ``cfg["builder"]``, but ``builder`` is not a known Hermes
    config root, so the lookup always returned ``{}`` and the silent-block
    fallback was unreachable. Reading config on this path also pulled
    ``hermes_cli.config`` into every ``write_file``/``patch`` call.
    """
    import pathlib

    import __init__ as plugin

    source = pathlib.Path(plugin.__file__).read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    assert "load_config" not in code
    assert "guard_approval_enabled" not in code
    assert "approval_enabled" not in code

    # the guard must still escalate protected writes rather than silently allow
    result = plugin._plugin_pre_tool_call(
        "write_file", {"path": str(pathlib.Path.home() / ".hermes/config.yaml")}
    )
    assert result is not None
    assert result["action"] == "approve"


def test_pre_tool_call_escalates_hermes_core_path_to_approval():
    """Protected-path writes escalate to the human-approval gate.

    The guard no longer hard-blocks here: it returns ``approve`` and core's
    dispatch layer runs the same human gate as a Tier-2 dangerous command
    (``request_tool_approval``), failing CLOSED when no human is present. The
    destructive-command and privilege-escalation guards above stay hard blocks.
    """
    import os

    import __init__ as plugin

    path = os.path.expanduser("~/.hermes/hermes-agent/foo.py")
    result = plugin._plugin_pre_tool_call("write_file", {"path": path})
    assert result is not None
    assert result["action"] == "approve"
    # rule_key namespaces the [a]lways allowlist grain so plugin approvals never
    # collide with a real command-pattern key.
    assert result["rule_key"] == "builder-guard"
    # The message must name the target so the approver sees what they are allowing.
    assert path in result["message"]


def test_pre_tool_call_allows_sibling_of_protected_path():
    """A sibling like ~/.hermes/config.yaml.bak is NOT protected.

    Guards the path-component comparison against regressing to a prefix match,
    which would wrongly block every file whose name merely starts with a
    protected name.
    """
    import os

    import __init__ as plugin

    sibling = os.path.expanduser("~/.hermes/config.yaml.bak")
    assert plugin._plugin_pre_tool_call("write_file", {"path": sibling}) is None


def test_spawn_provider_registration_returns_before_slow_write_completes():
    """_spawn_provider_registration() must hand the slow work to a thread.

    _provider.register_provider() lazily imports hermes_cli.config, measured at
    4.7s-12.6s cold on this host, and Hermes caps import + register() at
    plugins.load_timeout_seconds (default 10s). Paying that inline makes the
    plugin load non-deterministic: under CPU pressure the load times out and the
    plugin's tools vanish for the session (issue #115).
    """
    import sys
    import threading
    import types

    import __init__ as plugin

    started = threading.Event()
    released = threading.Event()
    completed = threading.Event()

    def _blocking_register(port):
        started.set()
        released.wait(timeout=5)
        completed.set()

    stub = types.SimpleNamespace(register_provider=_blocking_register)
    # Keep the patch active across the waits: Thread.start() gives no
    # happens-before guarantee that the worker reaches its import before this
    # block exits, and a restored patch would let the real register_provider run.
    with patch.dict(sys.modules, {"_provider": stub}):
        plugin._spawn_provider_registration(18092)
        # The worker is still inside register_provider, so the spawn returned
        # before the work finished. A wall-clock budget would be flaky under
        # load; this asserts the ordering the deadline fix actually depends on.
        assert started.wait(timeout=5), "background registration never started"
        assert not completed.is_set(), "spawn blocked until the write completed"
        released.set()
        assert completed.wait(timeout=5), "background registration never completed"


def test_register_does_not_call_register_provider_inline(monkeypatch):
    """register() hands off to _spawn_provider_registration, not register_provider.

    Pins the actual call site in register(): if someone inlines
    _provider.register_provider(actual) back into the deadline path, this fails
    because the spy on the spawn helper never fires.
    """
    import sys
    import types

    import __init__ as plugin

    spawned = []

    adapter_stub = types.SimpleNamespace(
        HOST="localhost",
        start=lambda port=8088: (object(), 18092),
        is_running=lambda host="localhost", port=8088: False,
        stop=lambda: None,
    )
    ctx = types.SimpleNamespace(
        register_tool=lambda **kw: None,
        register_hook=lambda *a, **k: None,
    )

    monkeypatch.setitem(sys.modules, "adapter", adapter_stub)
    monkeypatch.setattr(plugin, "_spawn_provider_registration", spawned.append)
    monkeypatch.setattr(plugin, "_registered", False, raising=False)

    plugin.register(ctx)

    assert spawned == [18092], (
        "register() must hand the provider write to _spawn_provider_registration; "
        f"got {spawned}"
    )
    monkeypatch.setattr(plugin, "_registered", False, raising=False)


def test_unregister_joins_provider_worker_before_removing_entry():
    """Teardown must wait for the in-flight provider write before removing the entry.

    Otherwise unregister() deletes providers.aws-builder and the still-running
    worker recreates it for the adapter teardown just stopped — a stale entry
    pointing at a dead port. Atomicity does not order the two writes.
    """
    import sys
    import threading
    import types

    import __init__ as plugin

    order = []
    release = threading.Event()
    worker_started = threading.Event()

    def _slow_register(port):
        worker_started.set()
        release.wait(timeout=5)
        order.append("write")

    stub = types.SimpleNamespace(
        register_provider=_slow_register,
        unregister_provider=lambda: order.append("remove"),
    )
    adapter_stub = types.SimpleNamespace(
        HOST="localhost",
        start=lambda port=8088: (object(), 18093),
        is_running=lambda host="localhost", port=8088: False,
        stop=lambda: order.append("stop"),
    )
    ctx = types.SimpleNamespace(
        unregister_hook=lambda *a, **k: None,
    )

    real_mod = sys.modules.get("adapter")
    try:
        sys.modules["adapter"] = adapter_stub
        sys.modules["_provider"] = stub
        plugin._registered = True

        plugin._spawn_provider_registration(18093)
        assert worker_started.wait(timeout=5), "worker never started"

        # Tear down while the worker is still blocked inside register_provider.
        teardown = threading.Thread(target=plugin.unregister, args=(ctx,), daemon=True)
        teardown.start()

        # While the worker is held, teardown may stop the adapter but must NOT
        # have removed the entry yet — removal must come after the write.
        assert "remove" not in order, (
            f"teardown removed before the write landed: {order}"
        )

        release.set()
        teardown.join(timeout=10)
        assert not teardown.is_alive(), "unregister() never returned"
    finally:
        release.set()
        if real_mod is not None:
            sys.modules["adapter"] = real_mod
        else:
            sys.modules.pop("adapter", None)
        sys.modules.pop("_provider", None)
        plugin._provider_worker = None
        plugin._registered = False

    # The contract is write-before-remove. Whether "stop" or "write" lands
    # first is a scheduling artifact of two independent threads, so assert the
    # invariant rather than an exact sequence.
    assert order.index("write") < order.index("remove"), (
        f"provider entry would survive teardown: {order}"
    )


def test_join_provider_worker_is_a_noop_when_nothing_is_running():
    """Teardown must not block when no registration is in flight."""
    import time

    import __init__ as plugin

    plugin._provider_worker = None
    t0 = time.perf_counter()
    plugin._join_provider_worker()
    assert (time.perf_counter() - t0) < 0.05


def test_pre_tool_call_allows_safe():
    import __init__ as plugin

    result = plugin._plugin_pre_tool_call("terminal", {"command": "echo hello"})
    assert result is None


# --- adapter.py: HTTP handler via live server ---


def test_handler_get_healthz():
    import urllib.request

    from adapter import start, stop

    try:
        _srv, port = start(port=0)  # OS picks free port
        url = f"http://127.0.0.1:{port}/healthz"
        with urllib.request.urlopen(url, timeout=2) as r:
            body = json.loads(r.read())
        assert body["status"] == "ok"
    finally:
        stop()


def test_handler_get_unknown_path():
    import urllib.error
    import urllib.request

    from adapter import start, stop

    try:
        _srv, port = start(port=0)
        url = f"http://127.0.0.1:{port}/unknown"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url, timeout=2)
        assert exc_info.value.code == 404
    finally:
        stop()


def test_handler_post_chat_completions():
    import urllib.request

    from adapter import start, stop

    with patch("backend.chat", return_value=("hello", "", "")):
        try:
            _srv, port = start(port=0)
            payload = json.dumps(
                {"messages": [{"role": "user", "content": "hi"}]}
            ).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                body = r.read()
            assert b"[DONE]" in body
        finally:
            stop()


def test_handler_post_unknown_path():
    import urllib.error
    import urllib.request

    from adapter import start, stop

    try:
        _srv, port = start(port=0)
        payload = b"{}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/unknown",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2)
        assert exc_info.value.code == 404
    finally:
        stop()


# --- build_cli.py: polling loop and edge cases ---


def test_cmd_login_polls_until_authenticated(capsys):

    from build_cli import build_parser, cmd_login

    args = build_parser().parse_args(["login"])
    call_count = 0

    def fake_get_status():
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            return {"authenticated": True, "token_expires_at": 9999999999.0}
        return {"authenticated": False, "phase": "awaiting_approval"}

    with (
        patch(
            "auth.sso_oidc.start_login",
            return_value={
                "user_code": "ABC-123",
                "verification_uri_complete": "https://device.sso.example.com/",
                "expires_in": 600,
                "interval": 0,
            },
        ),
        patch("auth.sso_oidc.get_status", side_effect=fake_get_status),
        patch("time.sleep"),
    ):
        rc = cmd_login(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Authenticated" in out


def test_cmd_login_error_phase(capsys):
    from build_cli import build_parser, cmd_login

    args = build_parser().parse_args(["login"])
    with (
        patch(
            "auth.sso_oidc.start_login",
            return_value={
                "user_code": "ABC-123",
                "verification_uri_complete": "https://device.sso.example.com/",
                "expires_in": 600,
                "interval": 0,
            },
        ),
        patch(
            "auth.sso_oidc.get_status",
            return_value={"authenticated": False, "phase": "error", "error": "bad"},
        ),
        patch("time.sleep"),
    ):
        rc = cmd_login(args)
    assert rc == 1


def test_cmd_whoami_not_authenticated(capsys):
    from build_cli import build_parser, cmd_whoami

    args = build_parser().parse_args(["whoami"])
    with patch("auth.sso_oidc.show_identity", return_value={"authenticated": False}):
        rc = cmd_whoami(args)
    assert rc == 1
    assert "not authenticated" in capsys.readouterr().out
