#!/usr/bin/env python3
"""builder — standalone CLI for the Amazon Builder ID (BID) auth + chat plugin.

This is the copy-device-link method you can run from a terminal, without the
Hermes dashboard. It reuses the plugin's own auth/chat modules exactly as the
in-agent tools do, so it shares the SAME token store (``auth/bid_token.json``
under HERMES_HOME) — NOT Hermes core's credential pool (``hermes auth add
aws-bid`` writes to a different store and will not make ``ask_q``/``bid_status``
report logged-in).

USAGE:
    python3 build_cli.py login      # start device flow, print copyable link
    python3 build_cli.py status     # show auth/flow state
    python3 build_cli.py whoami     # token identity (no raw token)
    python3 build_cli.py logout     # clear stored secrets
    python3 build_cli.py models     # list advertised models + tags

Set HERMES_HOME to point at a non-default Hermes home (same var the plugin uses).
# SPDX-License-Identifier: MIT OR Apache-2.0
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Optional

# Make the plugin importable whether invoked from its own dir or elsewhere.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def _load_modules():
    """Import the plugin's auth + backend MODULES (attribute access at call
    time, so tests can monkeypatch ``sso_oidc.get_status`` etc.)."""
    try:
        from auth import sso_oidc
        from backend import list_models, load_tags
    except ImportError:  # package-style import under Hermes core
        from .auth import sso_oidc
        from .backend import list_models, load_tags
    return sso_oidc, list_models, load_tags


def _fmt_iso(expires_at: Optional[float]) -> str:
    if not expires_at:
        return "n/a"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()


def cmd_login(args: argparse.Namespace) -> int:
    sso_oidc, _list_models, _load_tags = _load_modules()
    info = sso_oidc.start_login()
    if info.get("already_authenticated"):
        print("Already authenticated with Amazon Builder ID. No new login needed.")
        st = sso_oidc.get_status()
        print(f"  expires_at: {_fmt_iso(st.get('token_expires_at'))}")
        return 0
    if not info.get("user_code"):
        print("error: device authorization failed to start (no user_code returned).", file=sys.stderr)
        return 1

    print("Approve this Amazon Builder ID login in your browser:")
    print(f"  {info['verification_uri_complete']}")
    print(f"  user_code: {info['user_code']}")
    print("Waiting for approval (Ctrl-C to cancel)...")

    deadline = time.time() + info.get("expires_in", 600)
    interval = max(1, info.get("interval", 1))
    status = None
    try:
        while time.time() < deadline:
            status = sso_oidc.get_status()
            if status.get("authenticated"):
                break
            if str(status.get("error") or "").startswith("error:"):
                print(f"error: login failed: {status['error']}", file=sys.stderr)
                return 1
            if status.get("phase") == "error":
                print(f"error: login failed: {status.get('error')}", file=sys.stderr)
                return 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\ncancelled; the pending flow is saved — run `status` to resume polling.")
        return 130

    if not status or not status.get("authenticated"):
        print("error: login was not approved in time. Re-run `login` to try again.", file=sys.stderr)
        return 1

    print("Authenticated. Token stored at the plugin's auth/bid_token.json.")
    print(f"  expires_at: {_fmt_iso(status.get('token_expires_at'))}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    sso_oidc, _list_models, _load_tags = _load_modules()
    st = sso_oidc.get_status()
    if st.get("authenticated"):
        print(f"authenticated: yes (expires {_fmt_iso(st.get('token_expires_at'))})")
        return 0
    if st.get("phase") in ("awaiting_approval", "pending", "slow_down"):
        print("authenticated: no — awaiting approval")
        if st.get("verification_uri_complete"):
            print(f"  {st['verification_uri_complete']}")
        if st.get("user_code"):
            print(f"  user_code: {st['user_code']}")
        return 0
    print(f"authenticated: no (phase={st.get('phase')})")
    if st.get("error"):
        print(f"  error: {st['error']}")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    sso_oidc, _list_models, _load_tags = _load_modules()
    ident = sso_oidc.show_identity()
    if not ident.get("authenticated"):
        print("not authenticated")
        return 1
    print(f"token_type:  {ident.get('token_type')}")
    print(f"has_refresh: {ident.get('has_refresh_token')}")
    print(f"scopes:      {ident.get('scopes')}")
    print(f"expires_at:  {_fmt_iso(ident.get('expires_at'))}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    sso_oidc, _list_models, _load_tags = _load_modules()
    sso_oidc.logout()
    print("Logged out; secrets cleared.")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    _sso_oidc, list_models, load_tags = _load_modules()
    print("models:")
    for m in list_models():
        print(f"  - {m}")
    print("tags:")
    for t in load_tags():
        print(f"  - {t}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="builder",
        description="CLI for the builder plugin (Amazon Builder ID auth + chat).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="start device login; print a copyable verification link")
    sub.add_parser("status", help="show current auth / device-flow state")
    sub.add_parser("whoami", help="show token identity (no raw token)")
    sub.add_parser("logout", help="clear stored secrets")
    sub.add_parser("models", help="list advertised models and tags")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers: dict[str, Any] = {
        "login": cmd_login,
        "status": cmd_status,
        "whoami": cmd_whoami,
        "logout": cmd_logout,
        "models": cmd_models,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
