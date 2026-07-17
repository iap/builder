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

---

## Runtime — backend

The bridge connects to Amazon Q with **no `q` CLI build required**. Default
backend is `direct` (pure-HTTP via `q_direct.py`, Bearer Builder ID token):

- `direct` (default) — no `amazon-q-developer-cli` binary; satisfies "AWS Build
  connects to Q's server models without building q_cli". Chat-only: Q's API
  rejects `tools`, so the model narrates instead of acting on files.
- `subprocess` — shells out to the `q chat` CLI, which has native tool use
  (`fs_read`/`fs_write`/`fs_exec`). This is what lets the aws model read,
  write, and edit files. Requires the `q` binary on PATH.

### Plugin settings (`config.yaml`)

Runtime behavior is configured in `aws-build/config.yaml` (the **source of
truth**), with environment variables as a higher-precedence override.

**The bridge auto-starts when the plugin loads** (`ensure_bridge()` in
`__init__.py`, called from `register()`). It reads `backend` from
`config.yaml` (env `AMAZON_Q_BACKEND` wins) and spawns `amazon_q_bridge.py`
detached on `127.0.0.1:8088`. No launchd plist / system daemon is required —
the plugin is the sole owner of the bridge lifecycle. A launchd plist, if
present, is redundant and can be removed (the plugin already starts the
bridge with the correct backend from `config.yaml`).

```yaml
backend: direct            # direct | subprocess  (env: AMAZON_Q_BACKEND)
                           # direct  = pure-HTTP, no q CLI, chat-only (default)
                           # subprocess = shells out to `q chat`, enables file/tool use
default_model: claude-haiku-4.5   # env: AMAZON_Q_DEFAULT_MODEL
extra_models:              # appended to /v1/models + validation (env: AMAZON_Q_EXTRA_MODELS, comma-sep)
  []                        # only add models `q chat --model` accepts (see below)
debug: false               # verbose /tmp/q_raw_<pid>.log dump (env: AMAZON_Q_DEBUG)
```

Env var → config key mapping (env always wins):

| Env var | config.yaml key | Effect |
|---------|----------------|--------|
| `AMAZON_Q_BACKEND` | `backend` | `direct` or `subprocess` |
| `AMAZON_Q_DEFAULT_MODEL` | `default_model` | model used when none/unknown sent |
| `AMAZON_Q_EXTRA_MODELS` | `extra_models` | extra catalog entries (comma-sep) |
| `AMAZON_Q_DEBUG` | `debug` | transcript dump |

PyYAML is used when available; a minimal built-in parser handles this flat
config if PyYAML is missing, so a missing dependency never crashes startup.
A missing or broken `config.yaml` falls back to built-in defaults + env vars.

Launch: `python3 amazon_q_bridge.py --host 127.0.0.1 --port 8088`
(backend/model/catalog now come from `config.yaml`, not flags.)

### Multi-turn context (chat history across turns)

Two layers keep a conversation coherent across turns:

1. **Prompt flattening (always on, OpenAI route).** The OpenAI-compatible
   `/v1/chat/completions` handler (`amazon_q_bridge.py`) previously took only the
   *last* user message as the prompt, discarding all prior turns — so the second
   turn lost context. It now flattens the full request via
   `_flatten_openai_messages()`: `system` plus every `user`/`assistant` turn,
   joined with `role:` labels. This matches what the Anthropic
   `/v1/anthropic/messages` route already did via `_extract_anthropic_prompt`.
   Hermes sends the complete `messages` array on every turn, so nothing is lost
   on the agent side.

2. **Server-side conversation memory (optional, `direct` backend).** Q's
   `GenerateAssistantResponse` returns a `conversationId` in its stream. The
   bridge threads it through an `X-Hermes-Conversation-Id` HTTP header:

   - The client may send `X-Hermes-Conversation-Id: <id>` on a request to link
     the turn to an existing Q conversation (native multi-turn memory by Q, not
     re-flattened history).
   - The bridge returns the (possibly new) `conversationId` in the same response
     header so the client can persist it and pass it back on the next turn.

   Example end-to-end threading:

   ```text
   # Turn 1 — no inbound id; Q assigns one
   POST /v1/chat/completions  (X-Hermes-Conversation-Id: absent)
        -> 200, header X-Hermes-Conversation-Id: conv-srv-1

   # Turn 2 — echo the id back; Q continues the same conversation
   POST /v1/chat/completions  (X-Hermes-Conversation-Id: conv-srv-1)
        -> 200, header X-Hermes-Conversation-Id: conv-srv-1
   ```

   **Observed behavior:** this account's Q responses did **not** emit a
   `conversationId` in the stream, so the bridge returns no
   `X-Hermes-Conversation-Id` header and Layer 2 stays inactive. That's fine —
   Layer 1 (prompt flattening) carries multi-turn context on its own. Layer 2
   only engages when the upstream Q account actually returns the id.

   The `subprocess` backend has no native conversation threading, so it returns
   `None` for the header (prompt flattening still carries history).

### Model catalog & calibration

The bridge serves an OpenAI-compatible `/v1/models` list and validates the
`model` field on each `/v1/chat/completions` request. Calibration rules:

- **Provider-prefix stripping.** Hermes may send `aws-build/claude-haiku-4.5`
  (or any `provider/name` form). The bridge strips the prefix before matching,
  so both `claude-haiku-4.5` and `aws-build/claude-haiku-4.5` resolve correctly.
- **Aliases.** Short forms and dash/dot variants map to catalog entries:
  `haiku`/`haiku45` → `claude-haiku-4.5`, `sonnet`/`sonnet45` → `claude-sonnet-4.5`,
  `claude-sonnet-4-5` → `claude-sonnet-4.5`, etc. (No `claude-opus-*` aliases —
  `q chat` rejects those models with "Model does not exist".)
- **No hard 400 on unknown names.** A model that isn't in the catalog (typo,
  brand-new Q variant) **falls back to `DEFAULT_MODEL`** (`claude-haiku-4.5`,
  aligned with `~/.hermes/config.yaml` aws-build `default`) and logs a warning —
  the turn still succeeds instead of erroring out. Valid catalog
  (verified via `q chat --model`): `claude-haiku-4.5`, `claude-sonnet-4`,
  `claude-sonnet-4.5`.
  - **Runtime catalog extension.** Set `AMAZON_Q_EXTRA_MODELS` (comma-separated)
  or the `extra_models` key in `config.yaml` to add models Q has shipped
  without editing code — but only list names `q chat --model` actually accepts,
  or the turn will 502 (q chat exits 1). Example:

  ```bash
  AMAZON_Q_EXTRA_MODELS="claude-sonnet-4" python3 amazon_q_bridge.py --host 127.0.0.1 --port 8088
  ```
  (preferred: add `extra_models: [claude-sonnet-4]` to `config.yaml`).

  The `models` plugin tool (`bid_login` toolset) reports the same catalog via
  `q_direct.list_models()`.

### Tool use & file/context access — IMPORTANT

AWS Build is **chat/reasoning only on the `direct` backend**. This is a hard
constraint of Q's API, not a missing feature:

- Q's `GenerateAssistantResponse` is a chat-completion stream. It **rejects a
  `tools` field in the request** (`ValidationException` / `REQUEST_BODY_INVALID`),
  and the `direct` bridge forwards only `messages` — it has no tool-execution
  layer. So when Hermes sends a tool-enabled request, the tool spec is dropped,
  Q cannot emit a `tool_call`, and the model **narrates the action instead of
  executing it** (`tool_turns=0`, no file written).
- Hermes's own tools (write_file, read_file, bash, delegation, skills) are NOT
  reachable through the `direct` backend. Answers that depend on reading your
  local files or running commands will be guesses — the model has no context.
- Verified behavior: easy chat and format-following replies work; tool-driven
  tasks (e.g. "create index.html") and context-aware answers about local code do
  not execute through `direct`.

**To get tool/context use through AWS Build**, switch to the `subprocess`
backend, which calls `q chat` — that binary has native tool use (its stream
emits `toolUse` events it executes locally):

```bash
AMAZON_Q_BACKEND=subprocess python3 amazon_q_bridge.py --host 127.0.0.1 --port 8088
```

Cost: requires the `amazon-q-developer-cli` binary (the thing `direct` exists
to avoid). Choose `direct` for binary-free chat; choose `subprocess` when a task
needs tools or local file/context access.
