"""Robust integration tests for the builder plugin's models/tags tools.

These drive the REAL hermes chat/TUI tool-dispatch path — the same code the
`hermes chat` / TUI loop calls when the agent emits a tool call:

    hermes_cli.plugins (discovery)  ->  tools.registry (registration)
    ->  registry.get_entry(name).handler(args)   (what run_agent.py invokes)

No LLM is in the loop, so the test is deterministic and does not depend on the
live Amazon Q backend or its rate limits. Per hermes-agent AGENTS.md we exercise
the real resolution chain with actual imports (not mocks) and assert behavior
contracts, not current-data snapshots.

Run with the plugin's own pytest (HERMES_HOME is redirected to a temp profile by
conftest.py, so no real user secrets are read).
"""

import json
import sys

import pytest

# conftest.py already put HERMES_AGENT_DIR on sys.path and redirected HERMES_HOME.
from conftest import HERMES_AGENT_DIR, PLUGIN_DIR  # noqa: E402

# Importing model_tools triggers plugin discovery (the same side effect the chat
# CLI relies on), so tools.registry is fully populated with builder's tools.
# Make the plugin importable only; Hermes core paths should come from conftest.
sys.path.insert(0, str(PLUGIN_DIR))

# Importing model_tools triggers plugin discovery ...
if not HERMES_AGENT_DIR.exists():
    pytest.skip("hermes-agent not found", allow_module_level=True)

import model_tools  # noqa: E402,F401  (imports tools.registry, discovers plugins)
from tools.registry import registry  # noqa: E402


def _plugin_manager():
    from hermes_cli import plugins as P

    return P._ensure_plugins_discovered()


def test_build_discovered_and_registers_model_tools():
    mgr = _plugin_manager()
    assert "builder" in mgr._plugins, "builder plugin must be discovered"
    registered = set(mgr._plugins["builder"].tools_registered)
    assert {"models", "tags"}.issubset(registered)


def test_models_and_tags_resolve_in_global_registry():
    # The chat/TUI loop resolves a tool call by name via the global registry.
    for name in ("models", "tags"):
        entry = registry.get_entry(name)
        assert entry is not None, f"{name} must be in the tool registry"
        assert entry.toolset == "builder"


def test_models_tool_returns_plugin_yaml_catalog():
    entry = registry.get_entry("models")
    out = json.loads(entry.handler({}, task_id="test-dispatch"))
    assert out.get("success") is True
    models = out.get("models", [])
    # Behavior contract (not a snapshot): the served catalog must be the
    # plugin.yaml override and must include the current Claude variants Q serves.
    assert isinstance(models, list) and len(models) >= 1
    assert "claude-sonnet-4.5" in models
    assert "claude-sonnet-4" in models
    assert "claude-haiku-4.5" in models
    # Q's chat endpoint rejects claude-opus-*; the catalog must never advertise it.
    assert not any("opus" in m for m in models)
    # models and tags tools must agree on the tag set (single source of truth).
    assert set(out.get("tags", [])) == set(
        json.loads(registry.get_entry("tags").handler({}, task_id="t")).get("tags", [])
    )


def test_tags_tool_returns_stable_identity_tags():
    entry = registry.get_entry("tags")
    out = json.loads(entry.handler({}, task_id="test-dispatch"))
    assert out.get("success") is True
    tags = out.get("tags", [])
    # Behavior contract: identity tags are always present (not a fixed count).
    for required in ("aws", "amazon-q", "claude", "chat", "builder-id", "auth"):
        assert required in tags


def test_tool_outputs_never_leak_secrets():
    # The chat/TUI surfaces these handler outputs to the agent/user; secrets must
    # never appear, regardless of auth state.
    for name in ("models", "tags"):
        entry = registry.get_entry(name)
        blob = json.dumps(json.loads(entry.handler({}, task_id="t")))
        assert "access_token" not in blob
        assert "client_secret" not in blob
        assert "refresh_token" not in blob
