"""Integration tests for builder provider registration against real Hermes core.

These require ``hermes_cli.config`` to be importable and a writable
``HERMES_HOME``. In the plugin's standalone CI they are run in a separate
``integration-test`` job that checks out ``NousResearch/hermes-agent`` and
assembles the expected environment; in plugin-local development they run
against the throwaway temp profile created by ``conftest.py``.
"""

import sys

import pytest

from conftest import HERMES_AGENT_DIR

# Hermes-core-backed tests must not crash collection when core is absent.
try:
    if HERMES_AGENT_DIR.exists():
        sys.path.insert(0, str(HERMES_AGENT_DIR))
    from hermes_cli.config import get_compatible_custom_providers
except ImportError:  # pragma: no cover - exercised in standalone CI
    pytest.skip(
        "hermes-agent not importable; skipping provider registration integration tests",
        allow_module_level=True,
    )

import yaml

import _provider


def test_register_provider_adopts_legacy_markerless_entry(monkeypatch, tmp_path):
    """Regression: a providers.aws-builder entry written by an OLD build
    (no _builder_managed marker, stale key_env: AWS_BUILD_ADAPTER_DUMMY) must
    be adopted and rewritten on register_provider — so a dashboard rebuild /
    plugin reload self-heals the false 'No API key' notification without a
    manual config edit or restart.

    A genuine foreign/user-managed entry (different base_url, no marker) must
    be left untouched.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    # Legacy entry: our base_url, no marker, stale dummy key_env.
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "aws-builder": {
                        "name": "AWS Builder ID",
                        "base_url": "http://localhost:8088/v1",
                        "key_env": "AWS_BUILD_ADAPTER_DUMMY",
                    }
                }
            }
        )
    )

    wrote = _provider.register_provider(8088)
    assert wrote is True, "legacy plugin entry must be adopted (rewritten)"

    updated = yaml.safe_load(cfg_path.read_text())["providers"]["aws-builder"]
    assert updated.get("api_key") == "no-key-required", "stale key_env replaced"
    assert all(not k.startswith("_") or k == "_revision" for k in updated), (
        "no private marker key written"
    )
    assert "key_env" not in updated, "dummy key_env removed"


def test_register_provider_adopts_legacy_127_entry_by_base_url(monkeypatch, tmp_path):
    """Regression (Devin review PR #28): legacy setup.sh wrote base_url with
    '127.0.0.1' (not 'localhost'). Adoption must recognise the 127.0.0.1
    loopback form so a renamed legacy entry (name != PROVIDER_NAME) is still
    adopted and self-healed. This guards the exact false-negative Devin found:
    matching only on 'localhost' missed real legacy entries."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    # Real legacy output of setup.sh: 127.0.0.1 host, name RENAMED by user.
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "aws-builder": {
                        "name": "My Renamed Builder",
                        "base_url": "http://127.0.0.1:8088/v1",
                        "key_env": "AWS_BUILD_ADAPTER_DUMMY",
                    }
                }
            }
        )
    )

    wrote = _provider.register_provider(8088)
    assert wrote is True, "legacy 127.0.0.1 entry (renamed) must be adopted"

    updated = yaml.safe_load(cfg_path.read_text())["providers"]["aws-builder"]
    assert updated.get("api_key") == "no-key-required", "stale key_env replaced"
    assert "key_env" not in updated, "dummy key_env removed"


def test_register_provider_leaves_foreign_entry_alone(monkeypatch, tmp_path):
    """A providers.aws-builder entry with a foreign base_url and no marker is
    treated as user-managed and must NOT be clobbered by register_provider."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "aws-builder": {
                        "name": "My Custom Endpoint",
                        "base_url": "https://example.com/v1",
                        "api_key": "«redacted:sk-…»",
                    }
                }
            }
        )
    )

    wrote = _provider.register_provider(8088)
    assert wrote is False, "foreign entry must be skipped"
    kept = yaml.safe_load(cfg_path.read_text())["providers"]["aws-builder"]
    assert kept["api_key"] == "«redacted:sk-…»", "user key preserved"
    assert all(not k.startswith("_") or k == "_revision" for k in kept), (
        "foreign entry not stamped with private key"
    )


def test_aws_builder_resolves_as_cli_tui_model(monkeypatch):
    """Robust check (against the REAL Hermes core resolver) that a
    providers:aws-builder block (what _provider.register_provider writes)
    resolves as a selectable model in CLI/TUI: correct transport, endpoint,
    keyless-by-design signal, and every declared model surfaced — using :8088.

    The provider is keyless (AWS Builder ID OIDC happens inside the adapter),
    so it MUST advertise ``api_key: "no-key-required"`` rather than a dummy
    ``key_env``. That honest signal lets the gateway's credential probe skip
    the false "No API key configured … First message will fail" warning.
    """
    provider_block = {
        "name": "AWS Builder ID",
        "transport": "openai_chat",
        "base_url": "http://127.0.0.1:8088/v1",
        "api_key": "no-key-required",
        "models": ["claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5", "auto"],
    }
    cfg = {"providers": {"aws-builder": provider_block}}
    cps = get_compatible_custom_providers(cfg)
    matches = [c for c in cps if c.get("provider_key") == "aws-builder"]
    assert matches, "aws-builder must appear in resolved providers"
    e = matches[0]
    assert e["api_mode"] == "openai_chat"
    assert e["base_url"].rstrip("/") == "http://127.0.0.1:8088/v1"
    assert e.get("api_key") == "no-key-required", "keyless-by-design signal required"
    assert "key_env" not in e, "no dummy key_env; use no-key-required"
    surfaced = set(e.get("models", {}).keys())
    assert surfaced == {
        "claude-sonnet-4.5",
        "claude-sonnet-4",
        "claude-haiku-4.5",
        "auto",
    }


def test_unregister_provider_removes_managed_entry(monkeypatch, tmp_path):
    """unregister_provider() must remove a builder-managed entry and leave
    foreign entries untouched."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "aws-builder": {
                        "name": "AWS Builder ID",
                        "base_url": "http://localhost:8088/v1",
                        "api_key": "no-key-required",
                    },
                    "example": {"name": "Example", "base_url": "https://example.com"},
                }
            }
        )
    )

    assert _provider.unregister_provider() is True, "managed entry must be removed"

    updated = yaml.safe_load(cfg_path.read_text())
    assert "aws-builder" not in updated.get("providers", {}), "managed entry removed"
    assert "example" in updated.get("providers", {}), "foreign entry preserved"


def test_unregister_provider_leaves_user_entry(monkeypatch, tmp_path):
    """A foreign/user-managed providers.aws-builder entry must survive
    unregister_provider(); ownership is detected from base_url, not slug."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "aws-builder": {
                        "name": "My Custom Endpoint",
                        "base_url": "https://example.com/v1",
                        "api_key": "user-key",
                    }
                }
            }
        )
    )

    assert _provider.unregister_provider() is False, "foreign entry must not be removed"

    kept = yaml.safe_load(cfg_path.read_text())["providers"]["aws-builder"]
    assert kept["api_key"] == "user-key", "user key preserved"
