# Architecture

How the builder plugin is structured and why.

## Overview

```
Hermes Agent
  │
  ├── ask_q tool ──────────────────────────────► backend.py ──► Amazon Q (HTTPS)
  │                                                  │
  │                                              auth/sso_oidc.py
  │                                              (token store)
  │
  └── -m aws-builder (model path)
        │
        └── adapter.py (:8088, loopback only)
              │  OpenAI /v1/chat/completions
              └──► backend.py ──► Amazon Q (HTTPS)
```

Hermes owns the agentic loop in both paths. The plugin is a reasoning backend only.

## Components

### `backend.py` — chat backend

Direct HTTPS to `POST https://q.us-east-1.amazonaws.com/` with target `AmazonCodeWhispererStreamingService.GenerateAssistantResponse`.

- Auth: `Authorization: Bearer <access_token>`. No SigV4.
- Request: `conversationState.currentMessage.userInputMessage` with `chatTriggerType: "MANUAL"`.
- Response: AWS binary event-stream. `_extract_answer_with_conversation_id()` decodes it with a brace/escape-aware parser so unbalanced braces in assistant text are never mis-split.
- Multi-turn: Q may return a `conversationId`; `chat()` surfaces it so callers can thread it back.
- Unknown `modelId` values are coerced to `"auto"` by `_resolve_model_id()` — Q returns an opaque HTTP 500 for any unsupported modelId.

Token resolution in `get_token()`:
1. `sso_oidc.get_status()` → `sso_oidc._load_token()` if authenticated.
2. Silent refresh via `sso_oidc.refresh_token()` if expired but refreshable.
3. `RuntimeError` with an actionable message otherwise.

### `adapter.py` — OpenAI-compatible model path

A stdlib `ThreadingHTTPServer` speaking OpenAI `/v1/chat/completions` SSE. Launched as a daemon thread by `register()`, dies with the Hermes session.

**Security:** loopback-only by design. `_resolve_bind_host()` rejects any non-loopback host unless `AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1`. No auth on the endpoint — safe only because it is not network-reachable.

**Tool-call shim:** Q rejects a real `tools` field. The adapter injects the tool-call convention as text (`_TOOL_CALL_INSTRUCTION`) and translates `<tool_call>` XML blocks in Q's answer back to OpenAI `tool_calls` SSE frames with `finish_reason: "tool_calls"`. Hermes's `openai_chat` transport parses these identically to a native function-calling model.

Message flattening: `_flatten_messages()` collapses OpenAI `messages` into a single Q prompt (system prepended, tool names injected, conversation joined).

### `auth/sso_oidc.py` — device flow + token store

RFC 8628 device authorization against `https://oidc.us-east-1.amazonaws.com` with an anonymous public client (no AWS credentials needed).

- Client registration cached to `auth/bid_registration.json`.
- Device flow persisted to `auth/bid_flow.json` so any process can complete polling.
- Background daemon thread polls `create_token` during an active flow.
- Token stored at `auth/bid_token.json` (chmod 600, atomic write via `_write_secret()`).
- Silent refresh via `refresh_token()` on expiry.
- `get_status()` actively polls a pending flow on each call and silently refreshes an expired token before reporting.

**This is the sole token store.** `backend.py` reads from it; nothing else writes tokens.

### `_provider.py` — Hermes config integration

Writes/removes a `providers.aws-builder` entry in Hermes `config.yaml` so the adapter appears as a selectable model in the TUI/CLI. Called by `register()` / `unregister()`.

Ownership detection uses `_is_our_entry()` (loopback base_url + provider name) — no private marker keys, which Hermes core would flag as unknown.

Legacy migration: entries written by old `setup.sh` (with `key_env: AWS_BUILD_ADAPTER_DUMMY` or `127.0.0.1` host) are adopted and rewritten on `register_provider()`.

### `__init__.py` — tool registration

Registers all tools via `ctx.register_tool()` and starts the adapter on `register()`. Tool handlers use `_success()` / `_error()` which delegate to Hermes's `tools.registry.tool_result` / `tool_error` (with `ensure_ascii=False` so non-ASCII text is never escaped to `\uXXXX`).

### `dashboard/plugin_api.py` — dashboard card backend

FastAPI router mounted at `/api/plugins/builder/`. Reuses `auth/sso_oidc` directly so the dashboard and in-conversation `bid_*` tools share one auth state.

> **Build-artifact note:** `dashboard/dist/index.js` and `dashboard/dist/style.css` are
> prebuilt, committed artifacts. Their frontend source is not yet in this repo, so
> they cannot be rebuilt from source here — edit them directly for now, or add a
> `dashboard/` build step (and `package.json` + `src/`) before relying on CI to
> regenerate them. Treat them as opaque until that exists.

## Data flow: `ask_q` tool path

```
Hermes agent
  → _handle_ask_q(args)
  → backend.chat(prompt, model, conversation_id)
  → get_token() → sso_oidc._load_token()
  → requests.post(CHAT_URL, headers=Bearer, data=body, stream=True)
  → _extract_answer_with_conversation_id(response)
  → (answer, conversation_id, tool_use_id)
  → _success({"answer": answer, "conversation_id": ...})
```

Q cannot execute Hermes tools from this path — it is chat/reasoning only.

## Data flow: `-m aws-builder` model path

```
Hermes openai_chat transport
  → POST http://127.0.0.1:8088/v1/chat/completions
  → adapter._handle_chat(body)
  → _flatten_messages(messages, tools)  # collapses to one Q prompt
  → backend.chat(prompt, model)
  → _parse_tool_calls(answer)
  → if tool calls: _tool_calls_frames(calls)  # OpenAI tool_calls SSE
    else:          content SSE frames
  → Hermes openai_chat parser reassembles tool_calls message
  → Hermes agentic loop dispatches tools (MCP / skills / native)
```

Tool calls actually fire in this path.

## Secret file layout

All under `$HERMES_HOME/plugins/builder/auth/`, chmod 600:

| File | Contents |
|------|----------|
| `bid_token.json` | `access_token`, `refresh_token`, `expires_at`, `scopes` |
| `bid_registration.json` | `client_id`, `client_secret`, `client_secret_expires_at` |
| `bid_flow.json` | in-flight device flow state (deleted on completion) |

Legacy dotted files (`plugins/builder/.bid_*.json`, `plugins/aws-build/...`) are migrated on first read and deleted.
