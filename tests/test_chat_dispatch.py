"""Tests for the builder plugin registration / dispatch lifecycle.

Two layers, deliberately kept apart:

* Real discovery (``TestDiscoveryAndRegistry``) — the same chain the
  ``hermes chat`` / TUI loop runs at startup:

      hermes_cli.plugins (discovery)  ->  tools.registry (registration)
      ->  registry.get_entry(name).handler(args)      (what run_agent.py calls)

  No LLM and no network, so it is deterministic; the handlers are the real
  ones, so a rename in ``_TOOLS`` or a ``plugin.yaml`` drift fails here.

* Unit lifecycle (``TestPluginLifecycle`` / ``TestSerialization``) — the
  register()/unregister() contract with a stubbed adapter, plus the
  (success, error) serializer shape.

Run with the plugin's own pytest (HERMES_HOME is redirected to a temp profile by
conftest.py, so no real user secrets are read).
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conftest import load_plugin  # noqa: E402


EXPECTED_TOOLS = {
    "ask_q",
    "bid_login",
    "bid_status",
    "bid_show_identity",
    "bid_logout",
    "models",
    "tags",
}


@pytest.fixture(scope="module")
def mod():  # noqa: ANN
    return load_plugin()


@pytest.fixture(scope="module")
def discovered():
    """Drive the REAL plugin discovery chain once, like `hermes chat` startup.

    Yields the global plugin manager; ``tools.registry`` is populated as a side
    effect of ``register()`` (builder's tools land under toolset ``builder``).
    """
    from hermes_cli import plugins as P

    manager = P._ensure_plugins_discovered()
    yield manager
    # register() starts the local adapter; release the listener afterwards so
    # the suite does not leave :8077 bound. Discovery imports the adapter as a
    # submodule, so stop THAT module, not the top-level `adapter`.
    adapter_mod = sys.modules.get("hermes_plugins.builder.adapter")
    if adapter_mod is not None:
        adapter_mod.stop()


class FakeAdapter:
    started = False
    stopped = False
    requested_port = None
    actual_port = None

    @classmethod
    def start(cls, port=None):
        cls.started = True
        cls.requested_port = port
        # Mirror the real adapter's (server, actual_port) contract: the second
        # element is what register() is supposed to report.
        cls.actual_port = port + 1
        return (None, cls.actual_port)

    @classmethod
    def stop(cls):
        cls.stopped = True


def _install_fake_adapter(monkeypatch):
    """Put a stub adapter in place of the real one, for lifecycle tests only."""
    fake_pkg = types.ModuleType("hermes_plugins")
    fake_pkg.__path__ = []
    fake_pkg.__package__ = "hermes_plugins"
    fake_mod = types.ModuleType("hermes_plugins.builder")
    fake_mod.__path__ = []
    fake_mod.__package__ = "hermes_plugins.builder"
    fake_adapter = types.ModuleType("hermes_plugins.builder.adapter")
    fake_adapter.__package__ = "hermes_plugins.builder"
    fake_adapter.start = FakeAdapter.start
    fake_adapter.stop = FakeAdapter.stop
    fake_mod.adapter = fake_adapter
    monkeypatch.setitem(sys.modules, "hermes_plugins", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_plugins.builder", fake_mod)
    monkeypatch.setitem(sys.modules, "hermes_plugins.builder.adapter", fake_adapter)
    return fake_adapter


def _ctx(captured):
    return types.SimpleNamespace(
        register_tool=lambda **kw: captured.update({kw["name"]: kw}),
        register_hook=lambda *a, **k: None,
    )


# --- real discovery -> global registry ---------------------------------------


class TestDiscoveryAndRegistry:
    def test_builder_discovered_and_enabled(self, discovered):
        loaded = discovered._plugins.get("builder")
        assert loaded is not None, "builder plugin must be discovered"
        assert loaded.enabled is True
        assert EXPECTED_TOOLS.issubset(set(loaded.tools_registered))

    def test_tools_resolve_in_global_registry(self, discovered):
        # The chat/TUI loop resolves a tool call by NAME via the global registry.
        from tools.registry import registry

        for name in sorted(EXPECTED_TOOLS):
            entry = registry.get_entry(name)
            assert entry is not None, f"{name} must be in the tool registry"
            assert entry.toolset == "builder"
            assert entry.handler is not None

    def test_models_tool_returns_plugin_yaml_catalog(self, discovered):
        from tools.registry import registry

        out = json.loads(registry.get_entry("models").handler({}, task_id="test-dispatch"))
        assert out.get("success") is True
        models = out.get("models", [])
        assert isinstance(models, list) and len(models) >= 1
        # Behavior contract, not a snapshot: the catalog is the plugin.yaml
        # override, includes the current Claude variants Q serves, and never
        # advertises claude-opus-* (Q's chat endpoint rejects them).
        assert {"claude-sonnet-4", "claude-sonnet-4.5", "claude-haiku-4.5"} <= set(models)
        assert not any("opus" in m for m in models)
        # models and tags tools must agree on the tag set (single source of truth).
        tags = json.loads(registry.get_entry("tags").handler({}, task_id="t")).get("tags", [])
        assert set(out.get("tags", [])) == set(tags)

    def test_tags_tool_returns_stable_identity_tags(self, discovered):
        from tools.registry import registry

        out = json.loads(registry.get_entry("tags").handler({}, task_id="test-dispatch"))
        assert out.get("success") is True
        # Identity tags are always present (not a fixed count).
        for required in ("aws", "amazon-q", "claude", "chat", "builder-id", "auth"):
            assert required in out.get("tags", [])

    def test_tool_outputs_never_leak_secrets(self, discovered):
        # The chat/TUI surfaces these handler outputs to the agent/user.
        from tools.registry import registry

        for name in sorted(EXPECTED_TOOLS):
            blob = json.dumps(json.loads(registry.get_entry(name).handler({}, task_id="t")))
            assert "access_token" not in blob
            assert "client_secret" not in blob
            assert "refresh_token" not in blob


# --- register()/unregister() lifecycle ----------------------------------------


class TestPluginLifecycle:
    def test_register_and_unregister_exist(self, mod):
        assert callable(mod.register)
        assert callable(mod.unregister)

    def test_expected_tools_registered(self, mod):
        captured = {}
        mod.register(_ctx(captured))
        assert EXPECTED_TOOLS.issubset(set(captured))

    def test_register_starts_adapter_on_configured_port(self, mod, monkeypatch, capsys):
        _install_fake_adapter(monkeypatch)
        FakeAdapter.started = False
        FakeAdapter.requested_port = None
        FakeAdapter.actual_port = None
        captured = {}

        mod.register(_ctx(captured))

        assert FakeAdapter.started is True
        # register() must forward the configured port (env or adapter default),
        # not a hardcoded constant, and report the port start() returned.
        expected = int(os.environ.get("AWS_BUILD_ADAPTER_PORT", "8077"))
        assert FakeAdapter.requested_port == expected
        out = capsys.readouterr().out
        assert f"OpenAI adapter listening on :{FakeAdapter.actual_port}" in out

    def test_unregister_stops_adapter(self, mod, monkeypatch):
        _install_fake_adapter(monkeypatch)
        FakeAdapter.stopped = False
        captured = {}

        ctx = _ctx(captured)
        mod.register(ctx)
        mod.unregister(ctx)

        assert FakeAdapter.stopped is True


# --- serializer shape ---------------------------------------------------------


class TestSerialization:
    def test_success_returns_valid_json(self, mod):
        data = json.loads(mod._success({"answer": "ok"}))
        assert data["success"] is True
        assert data["answer"] == "ok"

    def test_error_returns_valid_json(self, mod):
        data = json.loads(mod._error("nope", code="bad"))
        assert data["success"] is False
        assert data["error"] == "nope"
        assert data["code"] == "bad"

    def test_bid_status_handler_returns_success_key(self, mod):
        assert "success" in json.loads(mod._handle_bid_status({}))

    def test_tags_handler_returns_success_key(self, mod):
        out = json.loads(mod._handle_tags({}))
        assert out["success"] is True
        assert "tags" in out

    def test_models_handler_returns_success_key(self, mod):
        out = json.loads(mod._handle_bid_models({}))
        assert out["success"] is True
        assert "models" in out
        assert "tags" in out
