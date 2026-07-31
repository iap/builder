#!/usr/bin/env python3
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Verify the builder plugin loads + tools work (HEADLESS, no browser, no secrets)."""

import json
import os
import subprocess
import sys
import types

# Capture the REAL Hermes home BEFORE importing conftest — conftest redirects
# HERMES_HOME to a throwaway test profile at import time, so reading it later
# would point at the temp dir, not the real installed plugin.
_REAL_HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")

from conftest import load_plugin

errors = []


def check(cond: bool, msg: str) -> None:
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {msg}")
    if not cond:
        errors.append(msg)


def _warn(msg: str) -> None:
    """Non-blocking notice (drift is degraded, not broken)."""
    print(f"[WARN] {msg}")


def check_drift() -> None:
    """Warn when the installed plugin copy is behind the source repo HEAD.

    setup.sh stamps <plugin>/REVISION with the source git SHA at install time.
    A stale install still works but misses merged fixes — surface it without
    failing the run (per issue #10).
    """
    home = _REAL_HERMES_HOME
    rev_file = os.path.join(home, "plugins", "builder", "REVISION")
    if not os.path.isfile(rev_file):
        return  # not installed, or pre-stamp install — nothing to compare
    installed = open(rev_file).read().strip()
    repo_root = os.path.dirname(os.path.abspath(__file__))
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return  # not a git repo — can't compare
    if installed == head:
        return
    # Date fallback stamps (non-SHA) can't be compared reliably — skip.
    if len(installed) != 40 or len(head) != 40:
        return
    # Distinguish "behind" (installed is an ancestor of HEAD → missing merged
    # fixes) from "diverged/ahead" (different or newer history). Only the
    # former warrants the reinstall prompt; the latter is informational.
    behind = False
    try:
        behind = subprocess.run(
            ["git", "merge-base", "--is-ancestor", installed, head],
            cwd=repo_root, capture_output=True, check=True,
        ).returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError):
        behind = False
    if behind:
        _warn(
            f"installed plugin REVISION ({installed[:10]}) is behind repo HEAD "
            f"({head[:10]}). Reinstall to pick up merged fixes: "
            "`hermes plugins uninstall builder && hermes plugins install <repo>`"
        )
        return
    # Squash-merge breaks the ancestor check: the install commit is no longer
    # an ancestor of the post-squash HEAD even though its content is merged.
    # Compare trees — if identical, the install is current despite the SHA
    # mismatch (this is the common "REVISION differs from HEAD" false alarm).
    try:
        installed_tree = subprocess.run(
            ["git", "rev-parse", f"{installed}^{{tree}}"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        head_tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if installed_tree and installed_tree == head_tree:
            return  # trees identical → install is current
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    _warn(
        f"installed plugin REVISION ({installed[:10]}) differs from repo HEAD "
        f"({head[:10]}) — install predates or diverged from current source. "
        "Reinstall if you want the current build."
    )


def main() -> int:
    check_drift()
    mod = load_plugin()
    check(hasattr(mod, "register"), "plugin has register(ctx)")

    # Fake ctx to capture registration
    captured = {}

    def reg_tool(**kw):  # noqa: ANN
        captured[kw["name"]] = kw

    ctx = types.SimpleNamespace(register_tool=reg_tool, register_hook=lambda *a, **k: None)
    mod.register(ctx)
    expected = {"ask_q", "bid_login", "bid_status", "bid_show_identity", "bid_logout", "models", "tags"}
    check(expected.issubset(set(captured)), f"all tools registered: {sorted(captured)}")

    for name, spec in captured.items():
        check(callable(spec["handler"]), f"{name}: handler callable")
        check(callable(spec["check_fn"]), f"{name}: check_fn callable")
        check(spec["check_fn"]() is True, f"{name}: check_fn returns True")

    # Handler return shape
    out = json.loads(mod._handle_bid_status({}))
    check("success" in out, "bid_status returns success key")

    # No secret leak in handler output. Only probe READ-ONLY handlers here:
    # bid_login/bid_status/bid_logout have live side effects (network calls,
    # token writes, poll threads) that contradict this script's "HEADLESS,
    # no browser, no secrets" contract and are not safe to invoke with {}.
    _READONLY = {"models", "tags", "bid_show_identity", "q_debug"}
    for name, spec in captured.items():
        if name not in _READONLY:
            continue
        res = json.loads(spec["handler"]({}))
        blob = json.dumps(res)
        check(
            "access_token" not in blob and "client_secret" not in blob,
            f"{name}: no secret fields in output",
        )

    # Provider registration (issue #20): the adapter must surface as a
    # selectable model provider. Verify the _provider helper writes + cleans
    # the config entry deterministically, restoring any pre-existing entry.
    try:
        from hermes_cli.config import load_config, save_config
        # _provider lives in the plugin dir; make it importable like the
        # plugin itself is (load_plugin sets submodule_search_locations).
        _plugin_dir = os.path.dirname(os.path.abspath(__file__))
        if _plugin_dir not in sys.path:
            sys.path.insert(0, _plugin_dir)
        import _provider as _prov_mod

        PROVIDER_SLUG = _prov_mod.PROVIDER_SLUG
        register_provider = _prov_mod.register_provider
        unregister_provider = _prov_mod.unregister_provider

        cfg = load_config()
        providers = cfg.get("providers") or {}
        prior = providers.get(PROVIDER_SLUG)  # may be None

        try:
            wrote = register_provider(8088)
            cfg2 = load_config()
            entry = (cfg2.get("providers") or {}).get(PROVIDER_SLUG)
            check(
                wrote and isinstance(entry, dict) and "localhost:8088/v1" in str(entry.get("base_url")),
                "provider registration writes providers.aws-builder -> adapter base_url",
            )
        finally:
            unregister_provider()
            # Restore any user-managed entry we may have displaced. Guard the
            # restore write so a save failure is reported, not silently lost
            # (Greptile P1: a failed restore must not delete state quietly).
            try:
                cfg3 = load_config()
                if prior is not None:
                    cfg3.setdefault("providers", {})[PROVIDER_SLUG] = prior
                else:
                    cfg3.get("providers", {}).pop(PROVIDER_SLUG, None)
                save_config(cfg3)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"provider restore failed: {exc}")
        # If no entry pre-existed, assert it's gone after cleanup. If a
        # user-managed entry pre-existed, assert it was preserved (the
        # correct outcome — not a failure). Greptile P1.
        final_providers = load_config().get("providers") or {}
        if prior is None:
            check(
                PROVIDER_SLUG not in final_providers,
                "provider unregistration removes the entry (no leftover)",
            )
        else:
            check(
                PROVIDER_SLUG in final_providers,
                "provider unregistration preserves a pre-existing user-managed entry",
            )
    except Exception as exc:  # noqa: BLE001
        _warn(f"provider registration check skipped (config unavailable): {exc}")

    if errors:
        print(f"\n{len(errors)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
