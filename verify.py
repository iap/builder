#!/usr/bin/env python3
"""Verify the build plugin loads + tools work (HEADLESS, no browser, no secrets)."""

import json
import os
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
_HA = Path(__file__).resolve().parent.parent.parent / "hermes-agent"


def _load() -> types.ModuleType:
    sys.path.insert(0, str(_HA))
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="build-verify-")
    ns = "hermes_plugins"
    if ns not in sys.modules:
        m = types.ModuleType(ns)
        m.__path__ = []
        m.__package__ = ns
        sys.modules[ns] = m
    slug = "build"
    mn = f"{ns}.{slug}"
    spec = importlib.util.spec_from_file_location(
        mn, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = mn
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules[mn] = mod
    spec.loader.exec_module(mod)
    return mod


import importlib.util  # noqa: E402

errors = []


def check(cond: bool, msg: str) -> None:
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {msg}")
    if not cond:
        errors.append(msg)


def main() -> int:
    mod = _load()
    check(hasattr(mod, "register"), "plugin has register(ctx)")

    # Fake ctx to capture registration
    captured = {}

    def reg_tool(**kw):  # noqa: ANN
        captured[kw["name"]] = kw

    ctx = types.SimpleNamespace(register_tool=reg_tool, register_hook=lambda *a, **k: None)
    mod.register(ctx)
    expected = {"bid_login", "bid_status", "bid_show_identity", "bid_logout"}
    check(expected.issubset(set(captured)), f"all 4 tools registered: {sorted(captured)}")

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
