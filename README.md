# Build Plugin — Amazon BID (Build ID) Device Login

## Overview

The build plugin provides headless SSO-OIDC device authorization (RFC 8628) for
Amazon BID (Amazon Build ID). It allows the Hermes Agent to start a device login
flow, report the user code and verification URL to the human, poll for the token
in the background, and surface auth status, identity metadata, and logout.

No AWS credentials are required on the client side. The OIDC server at
`oidc.us-east-1.amazonaws.com` accepts the public client registration
anonymously.

---

## Architecture

```
__init__.py              wires 5 tools via ctx.register_tool
  │
  └── auth/__init__.py   re-exports public API from sso_oidc.py
        │
        └── auth/sso_oidc.py   RFC 8628 device authorization (botocore)
```

### `auth/sso_oidc.py`

Wraps the `sso-oidc` botocore service against `oidc.us-east-1.amazonaws.com`
with an anonymous public client (no signing). Implements:

- **Client registration** — cached to `.bid_registration.json` under
  HERMES_HOME. The OIDC server issues a client ID + secret; no AWS credentials
  exchanged.
- **Device authorization** — `start_device_authorization` returns a
  `device_code`, `user_code`, and `verification_uri_complete`. The flow state
  is persisted to `.bid_flow.json` so any process (not just the one that
  started the flow) can complete polling.
- **Token polling** — a daemon thread polls `create_token` in the background
  for long-lived agent sessions. `get_status` also performs a single poll pass,
  enabling cross-process completion.
- **Token refresh** — `refresh_token()` uses the stored refresh token to obtain
  a new access token before expiry.

### `auth/__init__.py`

Re-exports the public functions from `sso_oidc.py` so plugin code and tests can
write `from .auth import start_login, get_status, ...`.

### `__init__.py`

Defines five tool handlers (`_handle_bid_*` plus `models`) and a `register(ctx)`
function that calls `ctx.register_tool` for each tool in the `aws-build` toolset.
Each tool has a `check_fn` that verifies `boto3` is importable.

---

## Usage — `hermes auth`

The recommended path is the `hermes auth` CLI, which stores the credential in
the canonical Hermes credential pool (under `HERMES_HOME`, chmod 600):

```bash
# Start a device flow; approve the user_code in Brave (signed into Google)
hermes auth add aws-build

# Show stored credential / logged-in state
hermes auth status aws-build
hermes auth list aws-build

# Clear the stored credential (also wipes plugin mirror files)
hermes auth logout aws-build
```

`bid_status`,
`bid_show_identity`, and `bid_logout` read and clear that same pool entry, so
there is a single source of truth. Naming in docs and code uses "Amazon BID" /
"Amazon Build ID" only.

### Legacy plugin tools

The plugin also exposes agent tools (`bid_login`, `bid_status`,
`bid_show_identity`, `bid_logout`) for in-conversation use. `bid_status` reads
the same pool credential as `hermes auth status aws-build`.
