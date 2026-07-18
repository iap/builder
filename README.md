# aws-build Plugin — Amazon Q / Claude for Hermes (direct HTTPS chat)

## Overview

The `aws-build` plugin lets the Hermes Agent talk to **Amazon Q Developer
(Claude models)** through a **direct HTTPS backend** — `backend.py` calls
Amazon Q's `GenerateAssistantResponse` API straight over the wire, with no
HTTP bridge and no subprocess. Hermes drives the agentic loop; this plugin
exposes Q as a single chat tool:

```
ask_q(prompt, model?, conversation_id?) -> answer
```

It also provides Amazon Builder ID (BID) device-login auth tools so you can
authenticate headlessly (RFC 8628 device flow), plus `models` and `tags` tools
that describe the available Claude variants and the plugin itself.

Authentication is **Bearer-only (no SigV4) and needs no AWS IAM credentials** —
the device-flow access token is the chat bearer.

---

## Tools

| Tool | Purpose |
|------|---------|
| `ask_q` | Send a prompt to Amazon Q (Claude) and return the answer. Optional `model` and `conversation_id`. |
| `bid_login` | Start an Amazon BID device login; returns a `user_code` + verification URL to approve in a browser. |
| `bid_status` | Report current auth / device-login state (polls once if a flow is pending). Never returns the raw token. |
| `bid_show_identity` | Return token identity metadata (type, scopes, expiry) — no raw token. |
| `bid_logout` | Stop polling and delete all stored secrets (pool entry + mirror files). |
| `models` | List available AWS Build models (`backend.list_models()`) and plugin tags. |
| `tags` | List free-form tags describing the plugin (`backend.load_tags()`). |

---

## Architecture

```
__init__.py            registers tools via ctx.register_tool
  ├── backend.py       direct HTTPS chat with Amazon Q (ask_q, models)
  └── auth/
        ├── __init__.py    re-exports the public auth API
        └── sso_oidc.py    RFC 8628 device authorization (botocore, anonymous)
```

### `backend.py` — chat backend

Pure-HTTP calls to Amazon Q's chat API, authenticated with an AWS Builder ID
OIDC access token (Bearer only — **no SigV4**, verified live).

- **Endpoint:** `POST https://q.us-east-1.amazonaws.com/`
  with `x-amz-target: AmazonCodeWhispererStreamingService.GenerateAssistantResponse`.
- **Request body:** `conversationState.currentMessage.userInputMessage`, with
  `chatTriggerType: "MANUAL"`.
- **Response:** an AWS event-stream (binary-framed `assistantResponseEvent`
  payloads: `{"content": ..., "modelId": ...}`). `backend` decodes these with
  an escape/brace-aware parser so code containing unbalanced braces/quotes in
  the answer is never mis-split.
- **Multi-turn:** Q may return a `conversationId`; `chat()` surfaces it so the
  caller can thread it back via `conversation_id` for server-side memory. When
  the account doesn't emit one, callers just re-send context in the prompt.

**Token resolution order** (`get_token()`):

1. The Hermes credential pool / `.bid_token.json` written by `bid_login`
   (canonical store).
2. `.q_token.json` (this plugin's persisted cache), if valid.
3. If a stored token is expired but has a refresh token, a silent OIDC
   `refresh_token` exchange is attempted (no browser).
4. Otherwise `RuntimeError` with an actionable message (run `bid_login`).

### `auth/sso_oidc.py` — headless device login

Wraps the `sso-oidc` botocore service against `oidc.us-east-1.amazonaws.com`
with an anonymous public client (unsigned — no AWS credentials needed):

- **Client registration** cached under `HERMES_HOME`.
- **Device authorization** persisted to a flow file so any process can complete
  polling; a daemon thread polls `create_token` in the background.
- **Token refresh** via the stored refresh token.
- **Canonical store:** the Hermes credential pool (`aws-build` provider) is the
  source of truth; the local `.bid_*` files are a mirror. Secrets are written
  chmod 600 and never returned by a tool handler.

---

## Model catalog

The served catalog is resolved by `backend.list_models()` (verified against
what Amazon Q accepts):

1. An optional `models:` override in `plugin.yaml` (operator-editable — add or
   remove variants without touching code).
2. The built-in `backend.STATIC_MODELS` fallback:

   - `claude-haiku-4.5`
   - `claude-sonnet-4`
   - `claude-sonnet-4.5`

> `claude-opus-*` is **not** offered — Amazon Q rejects it ("Model does not
> exist").

`ask_q` defaults to `claude-sonnet-4`. The `models` tool reads the catalog
lazily, so an edit to `plugin.yaml` takes effect on the next call without
restarting Hermes. An unknown `model` name is still passed through to
`backend.chat` for API compatibility (Q selects the model server-side).

Free-form **tags** are likewise read from `plugin.yaml` (`tags:`) with a
`STATIC_TAGS` fallback, and exposed via the `tags` tool.

---

## Usage — authentication

Recommended path is the `hermes auth` CLI (stores the credential in the
canonical Hermes credential pool):

```bash
hermes auth add aws-build      # device flow; approve the user_code in a browser
hermes auth status aws-build
hermes auth logout aws-build   # clears pool entry + mirror files
```

The in-conversation tools (`bid_login`, `bid_status`, `bid_show_identity`,
`bid_logout`) read and clear the same pool entry, so there is a single source of
truth.

---

## Tool use & local file/context access — important

`ask_q` is **chat/reasoning only**. Amazon Q's `GenerateAssistantResponse` is a
chat-completion stream that rejects a `tools` field, so the model **cannot
execute Hermes tools** (write files, run commands) through this plugin — it will
narrate an action rather than perform it.

Hermes remains the agent: use Hermes's own tools for file edits, commands, and
context. Use `ask_q` when you want Claude-via-Q's reasoning/answers.

---

## Secrets

These files hold live credentials and are **gitignored** (never commit them):

- `.q_token.json`, `.bid_token.json`, `.bid_registration.json`, `.bid_flow.json`
- `auth/oidc_client_secret.json`

To rotate: `bid_logout` (or `hermes auth logout aws-build`) and re-authenticate.

---

## Tests

```bash
python3 -m pytest tests/ -q
```

Offline unit tests cover the event-stream parser, token expiry/refresh logic,
credential-pool / cache token loading, the dynamic model catalog, tag loading,
and tool registration. `python3 verify.py` sanity-checks that all tools register
and that no handler leaks a secret.
