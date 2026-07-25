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
    _warn(
        f"installed plugin REVISION ({installed[:10]}) is behind repo HEAD "
        f"({head[:10]}). Reinstall to pick up merged fixes: "
        "`hermes plugins uninstall builder && hermes plugins install <repo>`"
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
    _READONLY = {"models", "tags", "bid_show_identity"}
    for name, spec in captured.items():
        if name not in _READONLY:
            continue
        res = json.loads(spec["handler"]({}))
        blob = json.dumps(res)
        check(
            "access_token" not in blob and "client_secret" not in blob,
            f"{name}: no secret fields in output",
        )

    if errors:
        print(f"\n{len(errors)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
