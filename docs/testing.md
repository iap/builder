# Testing

## Running tests

```bash
# offline unit + integration tests (no network, no secrets)
python3 -m pytest tests/ -q

# headless load + tool-registration + secret-leak checks
python3 verify.py

# live OIDC test (requires a real Builder ID session)
BUILD_LIVE=1 python3 -m pytest tests/ -q
```

Use the Hermes agent venv so `hermes-agent` and `tools.registry` are importable:

```bash
${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python3 -m pytest tests/ -q
```

## Test isolation

`conftest.py` redirects `HERMES_HOME` to a throwaway temp directory at import time. Tests never read or write the real Hermes profile or token store.

## Test files

| File | What it covers |
|------|---------------|
| `tests/test_backend.py` | Event-stream parser, token resolution, model catalog, `_resolve_model_id`, `_parse_tool_calls`, adapter SSE shape |
| `tests/test_build.py` | Adapter request/response translation, tool-call XML→OpenAI frames, auth flow, CLI commands, provider registration |
| `tests/test_chat_dispatch.py` | Real Hermes tool-dispatch integration: plugin discovery, registry resolution, `models`/`tags` tool output, secret-leak gate |
| `tests/test_import_contract.py` | Public symbol contract guard — fails loudly if a refactor removes a symbol callers depend on |

## Key invariants tested

- **Event-stream parser:** unbalanced braces, escaped quotes, split chunks, error envelopes, non-assistant events filtered by `modelId`.
- **Token resolution:** SSO token returned when authenticated; silent refresh on expiry; `RuntimeError` when no credentials.
- **Model catalog:** static fallback, `plugin.yaml` override, caching, unknown model coerced to `auto`.
- **Adapter SSE shape:** double-newline frame terminators, `ensure_ascii=False`, role frame + content frame + `[DONE]`, OpenAI SDK parse.
- **Tool-call translation:** `<tool_call>` XML → `tool_calls` frames, multiple calls, XML stripped from content, plain text stays chat-only.
- **Auth flow:** `start_login` short-circuits when already authenticated, `InvalidGrantException` downgraded when token present, `get_status` refreshes expired token, `get_status` reports `expired` phase when refresh fails.
- **Provider registration:** legacy entry adoption, foreign entry left alone, no private marker keys written.
- **Secret leak gate:** `verify.py` checks every read-only tool handler for `access_token` / `client_secret` in output.

## `verify.py`

Headless sanity check that does not require a live token:

1. Checks for installed-copy drift vs. repo HEAD (warns, does not fail).
2. Loads the plugin and calls `register(ctx)`.
3. Verifies all expected tools are registered with callable handlers and `check_fn`.
4. Calls read-only handlers (`models`, `tags`, `bid_show_identity`, `q_debug`) and asserts no secret fields in output.
5. Exercises `_provider.register_provider` / `unregister_provider` round-trip (if Hermes config is available).

Keep `verify.py` green — it is the secret-leak gate for CI.
