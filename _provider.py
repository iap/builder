"""Register the builder adapter as a selectable model provider in Hermes.

The builder plugin starts a local OpenAI-compatible adapter (default :8088,
/v1/chat/completions) and declares ``models:`` in plugin.yaml. Hermes has no
mechanism to surface a plugin's declared models as a pickable provider, so the
Models UI never lists them. This module bridges that gap: on register() we
write a custom provider entry under ``config.yaml`` providers.<slug> pointing
at the running adapter; on unregister() we remove it (only if we wrote it).

See https://github.com/iap/builder/issues/20
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Stable provider identity. Keep in sync with the issue / tests.
PROVIDER_SLUG = "aws-build"
PROVIDER_NAME = "AWS Build"
# Marker so unregister() only removes an entry *we* created, never a
# user-configured one that happens to share the slug.
_MANAGED_MARKER = "_builder_managed"


def _plugin_dir() -> Path:
    return Path(__file__).resolve().parent


def _declared_models() -> list[str]:
    """Read the ``models:`` list from the plugin's own plugin.yaml."""
    import yaml  # hermes-agent dependency; safe in the gateway venv

    manifest = _plugin_dir() / "plugin.yaml"
    if not manifest.exists():
        return []
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        models = data.get("models")
        if isinstance(models, list):
            return [str(m).strip() for m in models if str(m).strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: failed to read plugin.yaml models: %s", exc)
    return []


def _adapter_base_url(port: int) -> str:
    # Adapter listens on loopback; the dashboard/gateway reach it locally.
    return f"http://localhost:{port}/v1"


def register_provider(port: int) -> bool:
    """Write a custom provider entry for the builder adapter.

    Returns True if an entry was written, False if it was skipped (e.g.
    config module unavailable). Does not change the user's current model.
    """
    try:
        from hermes_cli.config import load_config, save_config
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: cannot import hermes_cli.config (%s)", exc)
        return False

    models = _declared_models() or ["claude-haiku-4.5", "claude-sonnet-4", "claude-sonnet-4.5"]
    default_model = models[0]

    config = load_config()
    providers = config.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers

    existing = providers.get(PROVIDER_SLUG)
    if isinstance(existing, dict) and not existing.get(_MANAGED_MARKER):
        # A real user-configured entry already exists — don't clobber it.
        logger.info("builder: providers.%s already present (user-managed); leaving it.", PROVIDER_SLUG)
        return False

    entry: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    entry.update(
        {
            "name": PROVIDER_NAME,
            "base_url": _adapter_base_url(port),
            "model": default_model,
            "discover_models": False,
            _MANAGED_MARKER: True,
            # Adapter authenticates via AWS Builder ID OIDC internally; no key.
            "key_env": "AWS_BUILD_ADAPTER_DUMMY",
        }
    )
    models_map = {m: {} for m in models}
    if isinstance(entry.get("models"), dict):
        entry["models"].update(models_map)
    else:
        entry["models"] = models_map
    providers[PROVIDER_SLUG] = entry

    save_config(config)
    logger.info("builder: registered provider '%s' -> %s", PROVIDER_SLUG, entry["base_url"])
    return True


def unregister_provider() -> bool:
    """Remove the builder-managed provider entry, if present.

    Returns True if an entry was removed. Leaves user-managed entries alone.
    """
    try:
        from hermes_cli.config import load_config, save_config
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: cannot import hermes_cli.config (%s)", exc)
        return False

    config = load_config()
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return False
    entry = providers.get(PROVIDER_SLUG)
    if not isinstance(entry, dict) or not entry.get(_MANAGED_MARKER):
        return False

    providers.pop(PROVIDER_SLUG, None)
    save_config(config)
    logger.info("builder: removed provider '%s'", PROVIDER_SLUG)
    return True
