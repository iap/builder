# AGENTS.md — builder plugin

Guidance for AI coding agents (Codex, Claude, Copilot, Q, etc.) working in this repo.

## Hard rules

- **Do not modify Hermes core.** This plugin is a guest in the Hermes ecosystem. If a code review finding or bug appears to require a core change, treat it as a false positive or a plugin-side workaround opportunity first. Only escalate to a core change if the issue is definitively confirmed to originate in core and cannot be addressed in the plugin.
- All other constraints are listed in [Architecture constraints](#architecture-constraints) and the [Security checklist](#security-checklist).

## What this repo is

A Hermes Agent plugin that exposes Amazon Q Developer (Claude models) as a chat tool and selectable model, authenticated via Amazon Builder ID (RFC 8628 device flow). No AWS IAM credentials required — the OIDC access token is the chat bearer.

## Repo layout

```
__init__.py          tool registration + register()/unregister() hooks
backend.py           direct HTTPS chat with Amazon Q (GenerateAssistantResponse)
adapter.py           OpenAI-compatible /v1/chat/completions SSE server (:8088)
_provider.py         writes/removes providers.aws-builder in Hermes config.yaml
_format.py           reads Hermes render_mode/theme for q_debug metadata
build_cli.py         standalone CLI (login/status/whoami/logout/models)
verify.py            headless load + tool-registration + secret-leak checks
conftest.py          pytest path setup + throwaway HERMES_HOME fixture
auth/
  __init__.py        re-exports the public auth API
  sso_oidc.py        RFC 8628 device flow, token store, refresh
dashboard/
  plugin_api.py      FastAPI router for the Hermes dashboard card
  manifest.json      dashboard card manifest
scripts/
  setup.sh           idempotent: adds providers:aws-builder to config.yaml
  uninstall.sh       idempotent: removes all builder entries from config.yaml
tests/
  test_backend.py    event-stream parser, token resolution, model catalog
  test_build.py      adapter SSE shape, tool-call translation, auth flow, CLI
  test_chat_dispatch.py  real Hermes tool-dispatch integration tests
  test_import_contract.py  public symbol contract guard
```

## Single source of truth rules

These invariants must never be broken:

- **One token store.** `auth/sso_oidc.py` owns `auth/bid_token.json` exclusively. `backend.py` calls `sso_oidc.get_status()` / `sso_oidc._load_token()` — it never writes a token file itself. No second store, no "newest wins" resolver.
  - Token files live under `<HERMES_HOME>/builder/auth/` (NOT `<HERMES_HOME>/plugins/builder/auth/`). This is deliberate: a dashboard "force reinstall" does `shutil.rmtree` on the plugin dir and would otherwise wipe the live Builder ID login. `sso_oidc` migrates any token found in the old install-dir location to the new safe path on first read.
- **One model catalog.** `backend.list_models()` is the single source. `_provider.py`, `__init__.py` (ask_q schema enum), and `plugin.yaml` all delegate to it. Never hardcode a model list in a second place.
- **One provider slug.** `_provider.PROVIDER_SLUG = "aws-builder"`. `setup.sh`, `uninstall.sh`, and `_provider.py` all use this slug. Do not introduce a second slug.
- **No raw token in tool output.** Tool handlers must never return `access_token`, `client_secret`, or `refresh_token`. `verify.py` enforces this — keep it green.

## Architecture constraints

- `adapter.py` is **loopback-only**. It proxies Amazon Q with the stored Builder ID token and has no auth of its own. `_resolve_bind_host()` rejects any non-loopback host unless `AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1`. Do not weaken this guard.
- `adapter.py` is **in-process**. It runs as a daemon thread launched by `register()` and dies with the Hermes session. There is no separate daemon binary.
- Q's `GenerateAssistantResponse` **rejects a `tools` field**. The adapter injects tool awareness as text (`_TOOL_CALL_INSTRUCTION`) and translates `<tool_call>` XML back to OpenAI `tool_calls` SSE frames. Never attempt to pass a real `tools` field to Q.
- Auth is **Bearer-only, no SigV4**. The OIDC access token from the device flow is the chat bearer. Do not add SigV4 signing.
- Secret files are written **chmod 600** via `_write_secret()` (atomic temp-then-rename). Never write secrets with a plain `open(..., "w")`.

## Making changes

### Adding a tool

1. Add a `_handle_<name>` function in `__init__.py`.
2. Add a `(name, schema, handler, check_fn, emoji)` tuple to `_TOOLS`.
3. Add the tool name to `plugin.yaml` `provides_tools:` (so the manifest stays in sync).
4. Add the handler symbol to `PUBLIC_SYMBOLS` in `tests/test_import_contract.py` (the import-contract guard).
5. Run `verify.py` — it checks all registered tools for secret leaks.

### Changing the model catalog

Edit `plugin.yaml` `models:` only. `backend.list_models()` reads it lazily; no code change needed. The `ask_q` schema enum is built from `list_models()` at import time, so it picks up the change on the next Hermes restart.

### Changing auth flow

All auth logic lives in `auth/sso_oidc.py`. The public API (`start_login`, `get_status`, `logout`, `show_identity`, `refresh_token`, `ensure_valid`) is pinned by `tests/test_import_contract.py`. Do not rename or remove these symbols.

### Adding a tool guard (plugin-level enforcement)

The builder can enforce tool-use boundaries via Hermes's existing `pre_tool_call` hook — no Hermes core changes needed. The guard is registered in `register()` via `ctx.register_hook("pre_tool_call", _plugin_pre_tool_call)`. Hermes core calls `get_pre_tool_call_block_message()` before dispatching each tool call; the first `{"action": "block", "message": "..."}` return wins and prevents execution.

The guard currently blocks:
- Destructive shell patterns (`rm -rf`, `shutil.rmtree`, `chmod -R`, `>/dev/sda`, `mkfs`, `dd if=`)
- Privilege escalation (`sudo`, `su`, `su -`, `pkexec`, `doas`)
- Writes to Hermes core paths (`~/.hermes/hermes-agent/`, `~/.hermes/config.yaml`)

This is additive to Hermes's global `approvals.mode` setting — it does not override or bypass it.

### Changing the adapter

- Keep `_resolve_bind_host()` intact.
- Keep `_parse_tool_calls()` + `_tool_calls_frames()` in sync — the SSE shape is verified by `test_adapter_sse_parses_via_openai_sdk` against the real OpenAI SDK parser.
- `_sse()` must use `ensure_ascii=False` and double-newline (`\n\n`) frame terminators. Both are regression-tested.

### Changing provider registration

`_provider.py` detects ownership via `_is_our_entry()` (loopback base_url only).
Do not add private marker keys (e.g. `_builder_managed`) — Hermes core logs
"unknown config keys ignored" for any key it doesn't recognise.

### Safe YAML surgery in setup.sh / uninstall.sh

These scripts rewrite `~/.hermes/config.yaml` using `yaml.safe_load`/`yaml.safe_dump`.
They must only touch keys they own (`providers.aws-builder`, `plugins.enabled`,
toolset lists under `platform_toolsets`/`known_plugin_toolsets`). ALL other top-level
keys (`mcp_servers`, `mcp`, `memory`, `auxiliary`, `moa`, etc.) must be left
completely untouched. Never write the entire config back from scratch — always
modify the parsed dict and dump it, which preserves unknown keys through the
round-trip. When adding new config sections the plugin owns, add them explicitly
to the YAML-aware logic, not by rewriting the whole file.

## Running tests

```bash
# unit + integration (offline, no network, no secrets)
python3 -m pytest tests/ -q

# headless load + secret-leak gate
python3 verify.py

# live OIDC (requires a real Builder ID session)
BUILD_LIVE=1 python3 -m pytest tests/ -q
```

`conftest.py` redirects `HERMES_HOME` to a throwaway temp directory. Tests never read or write the real Hermes profile.

## Commit style

Conventional Commits-lite with the `builder` scope:

```
feat(builder): <subject>
fix(builder): <subject>
sec(builder): <subject>
refactor(builder): <subject>
test(builder): <subject>
docs(builder): <subject>
chore(builder): <subject>
```

Subject ≤ 72 chars. Body only when a critical bug or security reason must be recorded.

## Security checklist

Before every PR:

- [ ] `verify.py` passes (no secret leak in any tool handler output)
- [ ] `adapter.py` loopback guard untouched
- [ ] Secret files still written via `_write_secret()` (chmod 600, atomic)
- [ ] No new hardcoded credentials or endpoints beyond the pinned Q/OIDC hosts
- [ ] No raw token returned from any tool handler or dashboard endpoint
- [ ] Workflow actions in `.github/workflows/*.yml` are pinned to commit SHAs, not mutable tags like `@v4` or `@v3`
