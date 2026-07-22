#!/usr/bin/env python3
"""Verify the builder plugin loads + tools work (HEADLESS, no browser, no secrets)."""

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
    check(hasattr(mod, "unregister"), "plugin has unregister(ctx)")

    captured = {}

    def reg_tool(**kw) -> None:  # noqa: ANN
        captured[kw["name"]] = kw

    ctx = types.SimpleNamespace(register_tool=reg_tool, register_hook=lambda *a, **k: None)
    # If the adapter is already running (active Hermes session bound the
    # port), that is healthy — suppress the startup warning in verification.
    # Real failures still surface via check_fn/adapters returning False.
    import warnings as _warnings
    import logging as _logging
    _old_warn = _logging.Logger.warning

    def _suppress_bind_warning(self_, msg, *args, **kwargs):
        if isinstance(msg, str) and "Address already in use" in msg:
            return
        return _old_warn(self_, msg, *args, **kwargs)

    _logging.Logger.warning = _suppress_bind_warning  # type: ignore[method-assign]
    try:
        mod.register(ctx)
    finally:
        _logging.Logger.warning = _old_warn  # type: ignore[method-assign]
    expected = {"ask_q", "bid_login", "bid_status", "bid_show_identity", "bid_logout", "models", "tags", "q_debug"}
    check(expected.issubset(set(captured)), f"all tools registered: {sorted(captured)}")

    for name, spec in captured.items():
        check(callable(spec["handler"]), f"{name}: handler callable")
        check(callable(spec["check_fn"]), f"{name}: check_fn callable")
        check(spec["check_fn"]() is True, f"{name}: check_fn returns True")

    out = json.loads(mod._handle_bid_status({}))
    check("success" in out, "bid_status returns success key")

    out = json.loads(mod._handle_ask_q({}))
    check(out.get("success") is False, "ask_q rejects empty prompt")
    check(out.get("code") == "missing_prompt", "ask_q uses missing_prompt code")

    out = json.loads(mod._handle_q_debug({}))
    check(out.get("success") is True, "q_debug returns success")
    check("auth" in out and "identity" in out, "q_debug includes auth/identity")
    check("models" in out and "tags" in out, "q_debug includes models/tags")
    blob = json.dumps(out)
    check(
        "access_token" not in blob and "client_secret" not in blob,
        "q_debug never leaks access_token/client_secret",
    )

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
