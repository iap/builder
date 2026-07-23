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

    # No secret leak in any handler output.
    # Guard real secret objects such as
    # {"access_token": "...", "refresh_token": "...", ...}.
    # Use exact leaf-key matching to avoid false-positive matches on safe
    # field names like `token_expires_at` or matching values that happen to
    # contain a sensitive word.
    _SECRET_FIELDS = (
        "access_token",
        "client_secret",
        "refresh_token",
        "token_type",
        "id_token",
        "aws_access_key_id",
        "secret",
    )

    def _contains_secret_field(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in _SECRET_FIELDS or _contains_secret_field(child):
                    return True
        elif isinstance(value, list):
            return any(_contains_secret_field(item) for item in value)
        return False

    for name, spec in captured.items():
        try:
            res = json.loads(spec["handler"]({}))
        except Exception as exc:  # noqa: BLE001
            check(False, f"{name}: handler output is not valid JSON: {exc}")
            continue
        check(
            not _contains_secret_field(res),
            f"{name}: no secret leaf fields in output",
        )

    if errors:
        print(f"\n{len(errors)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
