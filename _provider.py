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

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Stable provider identity. Keep in sync with the issue / tests.
PROVIDER_SLUG = "aws-builder"
PROVIDER_NAME = "AWS Builder"
# We never write a private marker key into config.yaml: Hermes core warns
# "providers.<slug>: unknown config keys ignored" for any provider key it
# doesn't know (verified 2026-07-28: 2026 such warnings from
# `_builder_managed`). Instead we identify an entry *we* own purely from its
# loopback base_url (set by this plugin's adapter) and/or our provider name,
# via _is_our_entry(). That keeps register/unregister safe (won't clobber or
# delete a genuine user-managed entry at our slug) while leaving the user's
# config free of undocumented keys.


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
        logger.warning(
            "builder: backend.list_models() failed, using empty catalog: %s", exc
        )
        return []


def _adapter_base_url(port: int) -> str:
    # Adapter listens on loopback; the dashboard/gateway reach it locally.
    return f"http://localhost:{port}/v1"


def _stamp_path() -> Any:
    """Path of the provider-entry stamp.

    Lives under ``<HERMES_HOME>/builder/`` (the plugin's data dir that survives
    reinstalls, same reasoning as the token store) — never inside config.yaml,
    where an extra key would trigger Hermes core's "unknown config keys" warning.
    """
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "builder" / "adapter_stamp.json"


def _stamp_provider_entry(entry: dict) -> None:
    """Best-effort: persist the provider entry the plugin just wrote.

    Atomic write (tempfile + os.replace) so a crash mid-write doesn't
    corrupt adapter_stamp.json and degrade ownership detection.
    """
    import tempfile

    try:
        stamp = _stamp_path()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(stamp.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(entry))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(stamp))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError):
        logger.debug("builder: could not persist provider entry stamp", exc_info=True)


def _stamped_entry() -> dict | None:
    """The provider entry (dict) the plugin last wrote, from the stamp."""
    try:
        data = json.loads(_stamp_path().read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _matches_stamp(entry: Any) -> bool:
    """True if the entry still carries everything the plugin last wrote.

    Only string-valued stamp fields gate ownership (``models`` is rewritten
    on every register and ``discover_models`` is constant, so neither may
    veto); ``api_key`` treats the ``"***"`` redaction sentinel and the
    canonical ``"no-key-required"`` as equal. A user who repurposes the slug
    for their own service changes at least one stamped field (name,
    api_key, base_url, …) and no longer matches."""
    stamped = _stamped_entry()
    if not stamped or not stamped.get("base_url"):
        return False
    if not isinstance(entry, dict) or not entry.get("base_url"):
        return False
    for key, value in stamped.items():
        if not isinstance(value, str):
            continue
        ours = entry.get(key)
        if (
            key == "api_key"
            and value in ("***", "no-key-required")
            and ours in ("***", "no-key-required")
        ):
            continue
        if ours != value:
            return False
    return True


def _owned_ports() -> set:
    """Ports the adapter binds as of this process: the AWS_BUILD_ADAPTER_PORT
    env override and the 8088 default. Custom ports from past runs are
    covered by the entry stamp (see _matches_stamp), never by a bare port
    match — a port must not outlive the entry it was recorded for."""
    ports = {8088}
    env_port = os.environ.get("AWS_BUILD_ADAPTER_PORT")
    if env_port:
        try:
            env_val = int(env_port)
        except ValueError:
            env_val = None
        if env_val and env_val > 0:
            ports.add(env_val)
    return ports


def _is_our_base_url(base: str) -> bool:
    """True if ``base`` points at our loopback adapter as bound right now
    (127.0.0.1 or localhost on the env/default port), regardless of which
    loopback host string was used.

    Parses the URL instead of substring matching so a foreign entry like
    ``http://localhost:80880/v1`` (port prefix) or a host that merely embeds
    ``localhost:8088`` is not misclassified as ours. The port must be stated
    explicitly (every writer of our entries emits it); a port-less loopback
    URL is a foreign provider on its default port."""
    if not isinstance(base, str):
        return False
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    if port is None:
        return False
    return host in ("127.0.0.1", "localhost") and port in _owned_ports()


def _is_our_entry(entry: Any) -> bool:
    """True if an existing ``providers.<slug>`` entry belongs to this plugin.

    Two-tier, observable-fields-only (no private marker key in config.yaml,
    which Hermes core flags as "unknown config keys ignored"):

    1. stamp match — the entry still carries everything the plugin last
       wrote (adapter_stamp.json, recorded after a successful write), which
       recognises our entries even when AWS_BUILD_ADAPTER_PORT is no longer
       set while rejecting a user-repurposed entry at the same port;
    2. base_url match — loopback on the env/default adapter port, which
       recognises entries written before stamps existed.

    False for a genuinely foreign/user-managed entry (different base_url, or
    a repurposed slug whose fields no longer match the stamp)."""
    if not isinstance(entry, dict):
        return False
    return _matches_stamp(entry) or _is_our_base_url(entry.get("base_url") or "")


def _entries_equivalent(a: Any, b: Any) -> bool:
    """True if two provider entries are semantically equivalent.

    ``register_provider`` rebuilds an entry in memory and only saves it when
    it differs from what's already in config, so a no-op plugin load doesn't
    rewrite config.yaml (which would strip its comments). Treats the ``"***"``
    redaction sentinel and the canonical ``"no-key-required"`` keyless value
    as equal (``hermes_cli`` normalises the former to the latter on save).
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    a_key = a.get("api_key")
    b_key = b.get("api_key")
    if a_key in ("***", "no-key-required") and b_key in ("***", "no-key-required"):
        a = {**a, "api_key": "no-key-required"}
        b = {**b, "api_key": "no-key-required"}
    return a == b


def register_provider(port: int) -> bool:
    """Ensure the builder adapter's provider entry exists in config.yaml.

    Returns True when the entry is present and correct (written now or
    already current), False when it is skipped (config unavailable, or a
    user-managed entry at our slug that must not be clobbered).

    The write is a full ``load_config()``/``save_config()`` round-trip that
    strips comments, so it is skipped when the entry is already equivalent —
    ``setup.sh`` (line-based, comment-preserving) is the primary writer.
    """
    try:
        from hermes_cli.config import load_config, save_config
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: cannot import hermes_cli.config (%s)", exc)
        return False

    models = _declared_models()
    if not models:
        # _declared_models() already falls back to backend.STATIC_MODELS via
        # backend.list_models(); an empty result means backend is unavailable,
        # so there is nothing to advertise. Avoid a third hardcoded copy of the
        # catalog here — the single fallback source is STATIC_MODELS.
        logger.warning(
            "builder: no models available (backend unavailable); skipping provider registration"
        )
        return False
    default_model = models[0]

    # Best-effort: never let a malformed/unreadable config abort plugin
    # registration. Return False (skip) instead of raising.
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "builder: load_config failed, skipping provider registration: %s", exc
        )
        return False
    # A malformed config can parse to a non-mapping value (e.g. a scalar or
    # list); guard against AttributeError on .get() below (Greptile P1).
    if not isinstance(config, dict):
        logger.warning(
            "builder: config is not a mapping, skipping provider registration"
        )
        return False
    # hermes_cli.config caches the loaded dict (keyed by path/mtime); never
    # mutate that cached object in place. Deep-copy so our rebuild below can't
    # leak into the framework's cache or bleed across test/session boundaries.
    import copy

    config = copy.deepcopy(config)
    providers = config.get("providers")
    if isinstance(providers, dict):
        pass
    elif providers is None:
        providers = {}
        config["providers"] = providers
    else:
        # Non-dict providers (list, scalar, null from manual edit or old format):
        # can't safely inject a dict entry into it. Log and skip rather than
        # silently destroying all existing providers.
        logger.warning(
            "builder: providers is not a mapping (got %s); skipping provider registration",
            type(providers).__name__,
        )
        return False

    # One-time migration: the provider slug was renamed aws-build -> aws-builder
    # for naming consistency (issue #20 / PR #21). Move any entry we previously
    # wrote under the old slug so existing config isn't orphaned and
    # unregister_provider still finds it.
    _LEGACY_SLUG = "aws-build"
    changed = False
    legacy = providers.get(_LEGACY_SLUG)
    if isinstance(legacy, dict) and _is_our_entry(legacy):
        logger.info(
            "builder: migrating provider '%s' -> '%s'", _LEGACY_SLUG, PROVIDER_SLUG
        )
        providers.pop(_LEGACY_SLUG, None)
        if not isinstance(providers.get(PROVIDER_SLUG), dict):
            providers[PROVIDER_SLUG] = legacy
        changed = True

    existing = providers.get(PROVIDER_SLUG)

    def _is_user_managed(entry: Any) -> bool:
        """True if an existing entry is a genuine user config we must not clobber.

        Ownership is determined from observable fields only: the adapter's
        loopback base_url (127.0.0.1/localhost:<adapter port>). We do not
        write undocumented marker keys into config.yaml. A foreign entry with
        a different base_url, even if named "AWS Builder", is left untouched.
        """
        if not isinstance(entry, dict):
            return False
        return not _is_our_entry(entry)

    if isinstance(existing, dict) and _is_user_managed(existing):
        logger.info(
            "builder: providers.%s present and user-managed; leaving it.", PROVIDER_SLUG
        )
        return False

    # We own this entry (managed, or a legacy plugin entry we adopt), so
    # rebuild the model list from the currently-declared models rather than
    # merging — otherwise removed/renamed models in plugin.yaml would linger
    # as selectable (Greptile P2). Drop the legacy dummy key_env so a stale
    # AWS_BUILD_ADAPTER_DUMMY can't survive an adoption (it would re-trigger
    # the false 'No API key' notification).
    entry: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    entry.pop("key_env", None)
    # Drop any legacy private marker key (e.g. the old `_builder_managed`)
    # carried over from a pre-fix entry, so we never re-persist an
    # undocumented key that Hermes core flags as "unknown config keys ignored".
    entry.pop("_builder_managed", None)
    entry.update(
        {
            "name": PROVIDER_NAME,
            "transport": "openai_chat",  # matches setup.sh; core defaults to this, set explicitly
            "base_url": _adapter_base_url(port),
            "model": default_model,
            "discover_models": False,
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

    # Idempotency (comment preservation): skip the save when the rebuilt
    # entry is already equivalent to config AND no legacy migration happened.
    # load_config()/save_config() is a full YAML round-trip that strips every
    # comment, so rewriting on every plugin load would destroy user comments.
    if _entries_equivalent(entry, existing) and not changed:
        # Entry is already live in config, so record it as our last write —
        # a fresh stamp keeps ownership valid across env-var changes.
        _stamp_provider_entry(entry)
        logger.info(
            "builder: provider '%s' already current; skipping write", PROVIDER_SLUG
        )
        return True

    providers[PROVIDER_SLUG] = entry
    try:
        save_config(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("builder: save_config failed, provider not persisted: %s", exc)
        return False
    # Stamp only after the save succeeded: a stamp without a live entry would
    # make a later user-owned entry at that endpoint look like ours.
    _stamp_provider_entry(entry)
    logger.info(
        "builder: registered provider '%s' -> %s", PROVIDER_SLUG, entry["base_url"]
    )
    return True


def unregister_provider() -> bool:
    """Remove the builder-managed provider entry, if present.

    Returns True if an entry was removed. Leaves user-managed entries alone.
    Removes both the current slug ``aws-builder`` and the legacy ``builder``
    slug from pre-rename installs.
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
        logger.warning(
            "builder: load_config failed, skipping provider unregistration: %s",
            exc,
        )
        return False
    # Deep-copy: hermes_cli.config caches the loaded dict; never mutate it
    # in place (would leak into the framework cache / bleed across tests).
    import copy

    config = copy.deepcopy(config)
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return False

    removed = False
    for slug in (PROVIDER_SLUG, "builder"):
        entry = providers.get(slug)
        if not isinstance(entry, dict) or not _is_our_entry(entry):
            continue
        providers.pop(slug, None)
        removed = True

    if removed:
        if not providers:
            config.pop("providers", None)
        try:
            save_config(config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("builder: save_config failed, provider not removed: %s", exc)
            return False
        logger.info("builder: removed provider entries for aws-builder/builder")
        return True

    return False
