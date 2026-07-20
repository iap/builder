# builder Plugin — Amazon Q / Claude for Hermes (direct HTTPS chat)

> [!IMPORTANT]
> Unofficial, experimental community plugin for the Hermes Agent. It authenticates
> via Amazon Builder ID (AWS BID) and is not affiliated with or endorsed by Amazon.
> See [Licenses](#licenses) for terms.

## Overview

The `builder` plugin lets the Hermes Agent talk to **Amazon Q Developer
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

|--

## Installation

```bash
# 1) install the plugin (git URL or owner/repo)
hermes plugins install <aws-build-repo-url>

# 2) register aws-build as a selectable chat model in Hermes
#    (backs up ~/.hermes/config.yaml, then adds providers: aws-build
#     pointing at the in-plugin adapter on :8077 — no :8088 bridge)
~/.hermes/plugins/builder/scripts/setup.sh

# 3) restart Hermes so config reloads + the adapter launches on register()
# 4) one-time auth, then chat via the model or the ask_q tool
bid_login   # approve the user_code in your browser
```

`setup.sh` is **idempotent** (skips if `providers: aws-build` is already
present) and **always backs up `config.yaml` first**. It does NOT write
the guarded config file silently — it is user-invoked by design (Hermes core
does not let a plugin register an LLM backend or edit `config.yaml` itself).
The adapter it points at is launched inside the plugin on `register()` and
dies with the Hermes session — there is no separate daemon to manage.

After install you can pick **AWS Build** as a model in the TUI/CLI
(`-m aws-build`) or keep using the `ask_q` tool directly.

## Uninstall

`hermes plugins uninstall aws-build` only deletes the plugin **directory** —
Hermes core does NOT auto-remove the `providers: aws-build` config entry it
added via `setup.sh`, so an uninstall otherwise leaves a dangling provider
pointing at a dead `:8077` endpoint plus a stale `plugins.enabled` entry.

Run the companion script first (it backs up `config.yaml`, is idempotent,
and only touches aws-build's own entries):

```bash
~/.hermes/plugins/builder/scripts/uninstall.sh   # removes providers:aws-build + enabled entry
hermes plugins uninstall aws-build                  # drops the plugin dir
# restart Hermes
```

The `:8077` adapter listener stops when the session ends; if the plugin is
unloaded it also calls `unregister()` → `adapter.stop()` for an immediate
release. No `:8088` bridge, no orphaned refs.

|--

## Architecture

```
__init__.py            registers tools via ctx.register_tool
  ├── backend.py       direct HTTPS chat with Amazon Q (ask_q, models)
  ├── adapter.py       OpenAI-compatible /v1/chat/completions SSE server
  │                    (the model path) — runs on :8077, launched on register()
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

1. The plugin's BID login store — `auth/bid_token.json` written by `bid_login`
   (the plugin's sole canonical store).
2. If the stored token is expired but has a refresh token, a silent OIDC
   refresh is attempted via `auth.sso_oidc.refresh_token()` (no browser).
3. Otherwise `RuntimeError` with an actionable message (run `bid_login`).

### `adapter.py` — OpenAI-compatible model path (optional)

When AWS Build is registered as a model (`providers: aws-build` → `:8077`),
`adapter.py` exposes a tiny stdlib HTTP server speaking OpenAI
`/v1/chat/completions`. It receives Hermes's OpenAI-shaped request
(`messages`, `tools`, `stream`), calls `backend.chat()` (single Q prompt —
messages are flattened, the `tools` field is conveyed as injected text because Q
rejects it server-side), then streams SSE back. For turns where Q emits
`<tool_call>` blocks, it emits OpenAI `tool_calls` frames with
`finish_reason: "tool_calls"` so Hermes's agentic loop (MCP/skills/native tools)
fires. Launched on `register()` in a daemon thread, dies with the session — no
`:8088` bridge, no orphaned process.

**Security — local-only bridge.** The endpoint proxies to Amazon Q with the
plugin's stored Builder ID token, so it is **loopback-only by design**
(`AWS_BUILD_ADAPTER_HOST` defaults to `127.0.0.1`). There is intentionally no
auth on the endpoint — safe *only* because it is not network-reachable. Binding
any non-loopback host is rejected unless `AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1` is
explicitly set. Never expose `:8077` on a shared/multi-user host.

### `auth/sso_oidc.py` — headless device login

Wraps the `sso-oidc` botocore service against `oidc.us-east-1.amazonaws.com`
with an anonymous public client (unsigned — no AWS credentials needed):

- **Client registration** cached under `HERMES_HOME`.
- **Device authorization** persisted to a flow file so any process can complete
  polling; a daemon thread polls `create_token` in the background.
- **Token refresh** via the stored refresh token.
- **Canonical store:** the local `auth/bid_token.json` mirror under `HERMES_HOME`
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

`ask_q` defaults to `auto` (Q picks the model server-side). The `models` tool
reads the catalog lazily, so an edit to `plugin.yaml` takes effect on the next
call without restarting Hermes. An unknown `model` name is coerced to `auto`
rather than passed through verbatim — Q returns an opaque HTTP 500 for any
modelId it doesn't recognize, so the original string is never sent to
`backend.chat`.

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
`auth/bid_token.json` under `HERMES_HOME`, which is exactly what `ask_q` reads
back:

```bash
# inside a Hermes session (or via the builder toolset)
bid_login      # device flow; approve the user_code in your browser
bid_status     # report current auth / device-login state
bid_logout     # stop polling and delete all stored secrets
```

Do **not** use `hermes auth add/status/logout aws-build` for this plugin:
that CLI path is unrelated to this plugin's token store and will not affect
`ask_q`. The `bid_*` tools above are the only supported auth interface.

### Standalone CLI (copy-device-link, no dashboard needed)

For a terminal copy-device-link flow without the dashboard, use the bundled
CLI. It imports the plugin's own `auth/sso_oidc` + `backend` modules, so it
shares the **exact same** `auth/bid_token.json` store as the `bid_*` tools
(not Hermes core's credential pool):

```bash
python3 build_cli.py login     # prints a copyable verification URL + user_code, then polls to completion
python3 build_cli.py status    # current auth / device-flow state
python3 build_cli.py whoami    # token identity (no raw token)
python3 build_cli.py logout     # clear stored secrets
python3 build_cli.py models     # list advertised models + tags
# convenience shim (anywhere): ~/.hermes/plugins/builder/bin/builder login
```

`login` prints the `verification_uri_complete` link to copy into a browser and
waits for approval (Ctrl-C cancels; the pending flow is persisted, so `status`
can resume polling). Override the home with `HERMES_HOME`.

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
| `builder` | this plugin (directory, `plugin.yaml` `name:`, `toolset=`) | the plugin slug — the only name that matters for loading/running the plugin |
| `bid_*` | this plugin's auth tools (`bid_login`, `bid_status`, `bid_show_identity`, `bid_logout`) | "BID" = **B**uilder **ID** (Amazon's "Build ID" / Builder ID). The `bid_` prefix is the plugin's own, consistent abbreviation |
| `bid_token.json` / `bid_registration.json` / `bid_flow.json` | `HERMES_HOME/plugins/builder/auth/` | the plugin's local token/flow mirror files (prefix matches the `bid_*` tools) |
| `aws-bid` | Hermes **core** CLI (`hermes auth add aws-bid`) | core's *separate* device-flow provider id — **not** this plugin's slug and **not** wired to this plugin's `auth/bid_token.json` store |

Rule of thumb: the plugin is `builder` everywhere it controls its own name;
`aws-bid` belongs to Hermes core and is a different (currently un-integrated)
path. Do not rename the plugin's `bid_*` tools to `build_*` — that would
diverge from both the `.bid_*` file mirrors and AWS's `aws-bid` terminology.

---

## Tool use & local file/context access — important

Two distinct paths, two distinct rules:

- **`ask_q` tool path — chat/reasoning only.** Amazon Q's
  `GenerateAssistantResponse` is a chat-completion stream that rejects a `tools`
  field, so the `ask_q` tool **cannot execute Hermes tools** (write files, run
  commands) — it will narrate an action rather than perform it. Use `ask_q` when
  you want Claude-via-Q's reasoning/answers and let Hermes drive any follow-up
  tool use.

- **`-m aws-build` model path — tool calls DO fire.** When AWS Build is selected
  as a *model*, the in-plugin adapter (`adapter.py`) speaks OpenAI
  `/v1/chat/completions`. Because Q can't receive a real `tools` field, the
  adapter injects the tool-call convention (plus the tool names) as text, asks Q
  to emit Hermes-compatible `<tool_call>` blocks, and **translates those blocks
  back into OpenAI `tool_calls` SSE frames** (`finish_reason: "tool_calls"`).
  Hermes's `openai_chat` transport parses those frames exactly like a native
  function-calling model, so **MCP / skills / native tools actually run** through
  the aws-build model. Multiple tool calls per turn are supported; surrounding
  prose is stripped from content. This is a text-based function-calling shim — it
  depends on Q following the injected convention. Verified end-to-end against the
  real OpenAI SDK streaming parser (frames parse into a valid
  `assistant(tool_calls)` message).

Hermes remains the agent in both paths: it owns the tool loop, context, and file
access; aws-build is the reasoning backend behind it.

---

## Secrets

These files hold live credentials and are **gitignored** (never commit them).
They live under the plugin's own `auth/` directory (scoped to builder, not
Hermes core's `auth/` namespace), written `chmod 600`:

- `plugins/builder/auth/bid_token.json`
- `plugins/builder/auth/bid_registration.json`
- `plugins/builder/auth/bid_flow.json`

A one-time migration reads any legacy `.bid_*.json` from the plugin root and
moves it into `auth/`, so an existing session survives the layout change.

To rotate: `bid_logout` and re-authenticate via `bid_login`.

---

## License

Dual-licensed under your choice of **MIT** (`LICENSE-MIT`) or **Apache
License 2.0** (`LICENSE-APACHE`). SPDX: `MIT OR Apache-2.0`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for development and submission guidelines.

The dual license is intentional, not a generic "pick one" — see the top-level
`LICENSE` file for the full mapping. In short:

- **Apache License 2.0** is the license intended for the **Amazon-interfacing
  code** — `backend.py` (chat with Amazon Q), `adapter.py` (the OpenAI bridge
  to Q), `auth/sso_oidc.py` (AWS Builder ID login), and `__init__.py`. Apache-2.0's
  `NOTICE`/attribution and trademark clauses keep the Amazon / AWS / Amazon Q /
  Builder ID branding boundary explicit and respectful.
- **MIT License** is the license intended for the **code not related to Amazon**
  (generic helpers, dashboard UI shell, tests, tooling) — the simpler,
  more permissive option for that code.

You may still apply **either** license to the whole project (that is what
`MIT OR Apache-2.0` means); the split above is the project's *intended* mapping,
stated so redistributors are not confused about which license spirit applies
where. **Amazon**, **AWS**, **Amazon Q**, and **Builder ID** are trademarks of
Amazon.com, Inc. and are used only to describe interoperability. This project is
not affiliated with, endorsed by, or sponsored by Amazon.

## Tests

```bash
python3 -m pytest tests/ -q
```

Offline unit tests cover the event-stream parser, token expiry/refresh logic,
local mirror / cache token loading, the dynamic model catalog, tag loading,
and tool registration. `python3 verify.py` sanity-checks that all tools register
and that no handler leaks a secret.

---

## Licenses

- [MIT License](LICENSE-MIT)
- [Apache License 2.0](LICENSE-APACHE)
- [Contributing](CONTRIBUTING.md)

“Amazon Web Services” and all related marks, including logos, graphic designs, and service names, are trademarks or trade dress of AWS in the U.S. and other countries. AWS’s trademarks and trade dress may not be used in connection with any product or service that is not AWS’s, in any manner that is likely to cause confusion among customers, or in any manner that disparages or discredits AWS.

Copyright © 2026 Iko. Not affiliated with or endorsed by Amazon.com, Inc.
