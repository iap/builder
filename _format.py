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

from collections.abc import Callable
from typing import Any

_cache: dict[str, str] | None = None
_load_config_fn: Callable[[], dict] | None = None


def _default_load_config() -> dict:
    from hermes_cli.config import load_config  # type: ignore

    return load_config() or {}


def set_load_config_fn(fn: Callable[[], dict] | None) -> None:
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


def _plugin_transform_tool_result(
    tool_name: str,
    args: dict,
    result: str,
    **kwargs: Any,
) -> str | None:
    """Transform tool result for TUI display only.

    Preserves the JSON envelope for structured callers (verify.py, scripts).
    Only replaces the display string when render_mode is explicitly "tui".
    """
    try:
        prefs = load_render_prefs()
    except (KeyError, TypeError):
        return None

    render_mode = prefs.get("render_mode", "auto")
    if render_mode != "tui":
        return None

    try:
        import json

        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None

    if tool_name == "models":
        models = payload.get("models", [])
        tags = payload.get("tags", [])
        lines = ["Available models:"]
        for m in models:
            lines.append(f"  • {m}")
        if tags:
            lines.append("Tags:")
            for t in tags:
                lines.append(f"  • {t}")
        return "\n".join(lines)

    if tool_name == "tags":
        tags = payload.get("tags", [])
        return "Tags:\n" + "\n".join(f"  • {t}" for t in tags)

    if tool_name == "q_debug":
        return _format_q_debug_tui(payload)

    return None


def _format_q_debug_tui(payload: dict) -> str:
    auth = payload.get("auth", {})
    identity = payload.get("identity", {})
    models = payload.get("models", [])
    tags = payload.get("tags", [])
    render = payload.get("render", [])

    lines = ["Builder ID Status"]
    lines.append(
        f"  Auth: {'authenticated' if auth.get('authenticated') else 'not authenticated'} {auth.get('phase', 'unknown')}"
    )
    if auth.get("token_expires_at_iso"):
        lines.append(f"  Expires: {auth['token_expires_at_iso']}")
    if identity.get("token_type"):
        lines.append(f"  Token: {identity['token_type']}")
    if identity.get("has_refresh_token") is not None:
        lines.append(f"  Refresh: {'yes' if identity['has_refresh_token'] else 'no'}")
    if identity.get("scopes"):
        lines.append(f"  Scopes: {', '.join(identity['scopes'])}")
    lines.append(f"  Models: {', '.join(models)}")
    lines.append(f"  Tags: {', '.join(tags)}")
    if render:
        lines.append(f"  Render: {render}")
    return "\n".join(lines)
