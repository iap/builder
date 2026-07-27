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
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Stable provider identity. Keep in sync with the issue / tests.
PROVIDER_SLUG = "aws-builder"
PROVIDER_NAME = "AWS Builder"
# Marker so unregister() only removes an entry *we* created, never a
# user-configured one that happens to share the slug.
_MANAGED_MARKER = "_builder_managed"


def _declared_models() -> list[str]:
    """Return the plugin's declared models via the single source of truth.

    Delegates to ``backend.list_models()`` (which reads the ``models:``
    override from plugin.yaml with a cached fallback to STATIC_MODELS) so the
    provider registration and the chat backend never disagree on the catalog.
    """
    try:
        from . import backend  # package import
    except ImportError:  # __main__ / direct
        import backend  # type: ignore
    try:
        return list(backend.list_models())
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: backend.list_models() failed, using empty catalog: %s", exc)
        return []


def _adapter_base_url(port: int) -> str:
    # Adapter listens on loopback; the dashboard/gateway reach it locally.
    return f"http://localhost:{port}/v1"


def _adapter_base_url_marker() -> str:
    """Loopback host forms that identify OUR adapter's base_url.

    Legacy ``setup.sh`` wrote the provider entry with ``127.0.0.1``
    (e.g. ``http://127.0.0.1:8088/v1``), while current ``register_provider``
    writes ``localhost`` (``_adapter_base_url``). Both are our loopback
    adapter, so adoption must recognise either form — otherwise a renamed
    legacy entry would never be adopted and the stale ``key_env`` (false
    'No API key' notification) would survive. Returns the port so callers can
    match on the loopback host + port, host-agnostic.
    """
    port = int(os.environ.get("AWS_BUILD_ADAPTER_PORT", "8088"))
    return f":{port}"


def _is_our_base_url(base: str) -> bool:
    """True if ``base`` points at our loopback adapter (127.0.0.1 or localhost
    on the adapter port), regardless of which loopback host string was used."""
    if not isinstance(base, str):
        return False
    marker = _adapter_base_url_marker()  # e.g. ":8088"
    return (f"127.0.0.1{marker}" in base) or (f"localhost{marker}" in base)


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

    # Best-effort: never let a malformed/unreadable config abort plugin
    # registration. Return False (skip) instead of raising.
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: load_config failed, skipping provider registration: %s", exc)
        return False
    # A malformed config can parse to a non-mapping value (e.g. a scalar or
    # list); guard against AttributeError on .get() below (Greptile P1).
    if not isinstance(config, dict):
        logger.warning("builder: config is not a mapping, skipping provider registration")
        return False
    providers = config.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers

    # One-time migration: the provider slug was renamed aws-build -> aws-builder
    # for naming consistency (issue #20 / PR #21). Move any entry we previously
    # wrote under the old slug so existing config isn't orphaned and
    # unregister_provider still finds it.
    _LEGACY_SLUG = "aws-build"
    legacy = providers.get(_LEGACY_SLUG)
    if isinstance(legacy, dict) and legacy.get(_MANAGED_MARKER):
        logger.info("builder: migrating provider '%s' -> '%s'", _LEGACY_SLUG, PROVIDER_SLUG)
        providers.pop(_LEGACY_SLUG, None)
        if not isinstance(providers.get(PROVIDER_SLUG), dict):
            providers[PROVIDER_SLUG] = legacy

    existing = providers.get(PROVIDER_SLUG)

    def _is_user_managed(entry: Any) -> bool:
        """True if an existing entry is a genuine user config we must not clobber.

        A managed entry (ours, with the marker) is always rewritten with the
        current signal. A legacy plugin entry written before the marker existed
        (old setup.sh / hand-edits) is still ours — adopt it (rebuild + mark) so
        a dashboard rebuild self-heals stale fields (e.g. the old
        ``key_env: AWS_BUILD_ADAPTER_DUMMY`` that triggered a false 'No API key'
        notification). Only a real foreign entry — different base_url/name and no
        marker — is treated as user-managed.
        """
        if not isinstance(entry, dict):
            return False
        if entry.get(_MANAGED_MARKER):
            return False  # ours; caller rewrites it
        base = entry.get("base_url") or ""
        name = entry.get("name") or ""
        if _is_our_base_url(base) or name == PROVIDER_NAME:
            return False  # legacy plugin-owned entry; adopt it
        return True  # foreign/user-managed; leave it alone

    if isinstance(existing, dict) and _is_user_managed(existing):
        logger.info("builder: providers.%s present and user-managed; leaving it.", PROVIDER_SLUG)
        return False

    # We own this entry (managed, or a legacy plugin entry we adopt), so
    # rebuild the model list from the currently-declared models rather than
    # merging — otherwise removed/renamed models in plugin.yaml would linger
    # as selectable (Greptile P2). Drop the legacy dummy key_env so a stale
    # AWS_BUILD_ADAPTER_DUMMY can't survive an adoption (it would re-trigger
    # the false 'No API key' notification).
    entry: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    entry.pop("key_env", None)
    entry.update(
        {
            "name": PROVIDER_NAME,
            "base_url": _adapter_base_url(port),
            "model": default_model,
            "discover_models": False,
            _MANAGED_MARKER: True,
            # Adapter authenticates via AWS Builder ID OIDC internally; no key.
            # Signal keyless-by-design honestly so the gateway's credential
            # probe (tui_gateway _probe_credentials) does not emit a false
            # "No API key configured … First message will fail" warning when
            # this model is selected. "no-key-required" is core's canonical
            # placeholder for keyless providers (local servers, Nous free
            # tier, Ollama, …) and must be honored by the probe.
            "api_key": "no-key-required",
        }
    )
    entry["models"] = {m: {} for m in models}
    providers[PROVIDER_SLUG] = entry

    try:
        save_config(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: save_config failed, provider not persisted: %s", exc)
        return False
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

    # Best-effort: never raise on config trouble.
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: load_config failed, skipping provider unregistration: %s", exc)
        return False
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return False
    entry = providers.get(PROVIDER_SLUG)
    if not isinstance(entry, dict) or not entry.get(_MANAGED_MARKER):
        return False

    providers.pop(PROVIDER_SLUG, None)
    try:
        save_config(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: save_config failed, provider not removed: %s", exc)
        return False
    logger.info("builder: removed provider '%s'", PROVIDER_SLUG)
    return True
