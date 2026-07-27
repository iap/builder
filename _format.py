"""Hermes host render preferences — lets the plugin self-adapt to the active
CLI/TUI mode and dashboard theme without manual per-agent calibration.

Hermes core stores ``render_mode`` (auto/cli/tui) and ``theme``
(default/midnight/ember/mono/cyberpunk/rose) in ``config.yaml``. The plugin's
tool-result envelope is mode-agnostic (core renders it), but human-facing
strings (status hints, the q_debug snapshot) should be aware of the host so
the Q-backed agent (driven from inside) and external tooling (driven from
outside) observe a consistent environment. Reading prefs once at register()
time keeps this cheap and side-effect free.

The config loader is injectable (``set_load_config_fn``) so tests can stub
Hermes config without importing core.
"""

from __future__ import annotations

from typing import Callable, Optional

_cache: Optional[dict[str, str]] = None
_load_config_fn: Optional[Callable[[], dict]] = None


def _default_load_config() -> dict:
    from hermes_cli.config import load_config  # type: ignore

    return load_config() or {}


def set_load_config_fn(fn: Optional[Callable[[], dict]]) -> None:
    """Inject a config loader (tests). ``None`` restores the default."""
    global _load_config_fn, _cache
    _load_config_fn = fn
    _cache = None


def load_render_prefs() -> dict[str, str]:
    """Return ``{"render_mode": ..., "theme": ...}`` from Hermes config.

    Best-effort: if config is unavailable (standalone/test), fall back to
    ``auto``/``default``. Results are cached for the process lifetime.
    """
    global _cache
    if _cache is not None:
        return _cache
    mode, theme = "auto", "default"
    try:
        cfg = (_load_config_fn or _default_load_config)() or {}
        mode = str(cfg.get("render_mode") or "auto")
        theme = str(cfg.get("theme") or "default")
    except Exception:  # noqa: BLE001 - best-effort, never block registration
        pass
    _cache = {"render_mode": mode, "theme": theme}
    return _cache


def reset_prefs_cache() -> None:
    """Drop the cached prefs (tests)."""
    global _cache
    _cache = None
