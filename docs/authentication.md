# Authentication

How the builder plugin authenticates with Amazon Q Developer.

## Overview

Authentication uses the **Amazon Builder ID (BID) OIDC device flow** (RFC 8628). No AWS IAM credentials are required — the OIDC access token from the device flow is the chat bearer.

The plugin manages its own token store end-to-end. It does **not** use the Hermes credential pool (`hermes auth add/status/logout`) — that path is unrelated to this plugin's `auth/bid_token.json` store.

## Device flow

```
bid_login
  → sso_oidc.start_login()
  → register_client (anonymous, cached)
  → start_device_authorization
  → returns user_code + verification_uri_complete
  → background thread polls create_token every interval seconds

[human approves in browser]

  → create_token succeeds
  → token written to auth/bid_token.json (chmod 600)
  → background thread exits
```

## Token resolution (`get_token()`)

`backend.chat()` calls `get_token()` before every request:

1. `sso_oidc.get_status()` → `sso_oidc._load_token()` — returns the stored token if valid.
2. If expired but a refresh token exists: `sso_oidc.refresh_token()` → re-read.
3. Otherwise: `RuntimeError("No valid Amazon Q token available. Authenticate via bid_login …")`.

A 400/401 from Q with "invalid" in the body triggers one silent refresh-then-retry before giving up.

## Token store

Single file: `$HERMES_HOME/builder/auth/bid_token.json`

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1234567890.0,
  "token_type": "Bearer",
  "scopes": ["codewhisperer:completions", "codewhisperer:analysis", "codewhisperer:conversations"]
}
```

Written chmod 600 via `_write_secret()` (atomic temp-then-rename). Never returned by any tool handler.

## OIDC endpoints

| Operation | Endpoint |
|-----------|----------|
| Client registration | `POST https://oidc.us-east-1.amazonaws.com/client/register` |
| Device authorization | `POST https://oidc.us-east-1.amazonaws.com/device_authorization` |
| Token exchange / refresh | `POST https://oidc.us-east-1.amazonaws.com/token` |

Client type: `public`. Scopes: `codewhisperer:completions`, `codewhisperer:analysis`, `codewhisperer:conversations`. Start URL: `https://view.awsapps.com/start`.

## Auth tools

| Tool | What it does |
|------|-------------|
| `bid_login` | Start device flow; returns `user_code` + `verification_uri_complete`. |
| `bid_status` | Report current auth state; actively polls a pending flow once. |
| `bid_show_identity` | Return token metadata (type, scopes, expiry). No raw token. |
| `bid_logout` | Stop polling; delete all `auth/bid_*` files. |

## Standalone CLI

```bash
python3 build_cli.py login     # prints verification URL, polls to completion
python3 build_cli.py status    # current auth state
python3 build_cli.py whoami    # token identity (no raw token)
python3 build_cli.py logout    # clear stored secrets
```

The CLI shares the exact same `auth/bid_token.json` store as the in-agent tools.

## Token refresh

`sso_oidc.refresh_token()` exchanges the stored `refresh_token` for a new access token via `create_token` with `grant_type=refresh_token`. On success it overwrites `auth/bid_token.json`. Retries up to 3 times with exponential backoff.

`get_status()` silently refreshes an expired token before reporting, so `bid_status` stays "Authenticated" across the ~1h access-token boundary as long as the refresh token is valid.

## Legacy migration

On first read, `_read_secret()` checks for legacy files:
- `plugins/builder/auth/bid_token.json` (previous install-dir location — the store was relocated out of the plugin dir so a dashboard force-reinstall can't wipe it)
- `plugins/builder/.bid_token.json` (old dotted name in plugin root)
- `plugins/aws-build/auth/bid_token.json` (old plugin directory name)
- `plugins/aws-build/.bid_token.json`

If found, the file is copied to the canonical location (`$HERMES_HOME/builder/auth/`) and the legacy file is deleted. No re-login required.
