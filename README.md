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
| `bid_logout` | Stop polling and delete all stored secrets (the local `.bid_*` mirror files). |
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

1. The plugin's BID login store — `.bid_token.json` written by `bid_login`
   (the plugin's sole canonical store).
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
- **Canonical store:** the local `.bid_token.json` mirror under `HERMES_HOME`
  is the sole source of truth (the plugin does not use the Hermes credential
  pool). Secrets are written chmod 600 and never returned by a tool handler.

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

This plugin manages its own Amazon BID (Build ID) OIDC device flow
(RFC 8628) **end-to-end and is fully self-contained.** It does **not** use
the Hermes credential pool or the `hermes auth` mechanism — there is no
integration between the two, by design. Authenticate entirely through the
in-conversation tools (`bid_login`, `bid_status`, `bid_show_identity`,
`bid_logout`); `bid_login` writes the token to this plugin's local
`.bid_token.json` under `HERMES_HOME`, which is exactly what `ask_q` reads
back:

```bash
# inside a Hermes session (or via the aws-build toolset)
bid_login      # device flow; approve the user_code in your browser
bid_status     # report current auth / device-login state
bid_logout     # stop polling and delete all stored secrets
```

Do **not** use `hermes auth add/status/logout aws-build` for this plugin:
that CLI path is unrelated to this plugin's token store and will not affect
`ask_q`. The `bid_*` tools above are the only supported auth interface.

---

## Dashboard card

The plugin ships a dashboard card (`dashboard/`) reachable at the **AWS Build**
tab in the Hermes dashboard (after `env`). It is a thin web UI over the same
`bid_*` tools:

- **Login with Build ID** — starts the RFC 8628 device flow; opens the
  verification URL in a new browser tab and shows the `user_code` to enter.
  The card polls `GET /status` (which actively polls the in-flight flow) and
  flips to *Authenticated* the moment you approve in your browser.
- **Logout** — stops polling and deletes the local `~/.bid_*` mirror files.

The card's backend (`dashboard/plugin_api.py`) reuses the plugin's own
`auth/sso_oidc` module, so the dashboard and the in-conversation `bid_*` tools
share one auth state. No Hermes credential pool is involved.

### Naming — three identifiers, one plugin

These names look similar but live in different layers. Mixing them up is what

| Identifier | Where | Meaning |
|------------|-------|---------|
| `aws-build` | this plugin (directory, `plugin.yaml` `name:`, `toolset=`) | the plugin slug — the only name that matters for loading/running the plugin |
| `bid_*` | this plugin's auth tools (`bid_login`, `bid_status`, `bid_show_identity`, `bid_logout`) | "BID" = **B**uilder **ID** (Amazon's "Build ID" / Builder ID). The `bid_` prefix is the plugin's own, consistent abbreviation |
| `.bid_token.json` / `.bid_registration.json` / `.bid_flow.json` | `HERMES_HOME/plugins/aws-build/` | the plugin's local token/flow mirror files (prefix matches the `bid_*` tools) |
| `aws-bid` | Hermes **core** CLI (`hermes auth add aws-bid`) | core's *separate* device-flow provider id — **not** this plugin's slug and **not** wired to this plugin's `.bid_token.json` store |

Rule of thumb: the plugin is `aws-build` everywhere it controls its own name;
`aws-bid` belongs to Hermes core and is a different (currently un-integrated)
path. Do not rename the plugin's `bid_*` tools to `build_*` — that would
diverge from both the `.bid_*` file mirrors and AWS's `aws-bid` terminology.

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

To rotate: `bid_logout` and re-authenticate via `bid_login`.

---

## Tests

```bash
python3 -m pytest tests/ -q
```

Offline unit tests cover the event-stream parser, token expiry/refresh logic,
local mirror / cache token loading, the dynamic model catalog, tag loading,
and tool registration. `python3 verify.py` sanity-checks that all tools register
and that no handler leaks a secret.
