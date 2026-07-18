#!/usr/bin/env python3
"""Verify the build plugin loads + tools work (HEADLESS, no browser, no secrets)."""

import json
import sys
import types

from conftest import load_plugin

errors = []


def check(cond: bool, msg: str) -> None:
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {msg}")
    if not cond:
        errors.append(msg)


def main() -> int:
    mod = load_plugin()
    check(hasattr(mod, "register"), "plugin has register(ctx)")

    # Fake ctx to capture registration
    captured = {}

    def reg_tool(**kw):  # noqa: ANN
        captured[kw["name"]] = kw

    ctx = types.SimpleNamespace(register_tool=reg_tool, register_hook=lambda *a, **k: None)
    mod.register(ctx)
    expected = {"ask_q", "bid_login", "bid_status", "bid_show_identity", "bid_logout", "models"}
    check(expected.issubset(set(captured)), f"all tools registered: {sorted(captured)}")

    for name, spec in captured.items():
        check(callable(spec["handler"]), f"{name}: handler callable")
        check(callable(spec["check_fn"]), f"{name}: check_fn callable")
        check(spec["check_fn"]() is True, f"{name}: check_fn returns True")

    # Handler return shape
    out = json.loads(mod._handle_bid_status({}))
    check("success" in out, "bid_status returns success key")

    # No secret leak in any handler output
    for name, spec in captured.items():
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
