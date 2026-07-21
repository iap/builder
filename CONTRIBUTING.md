# Contributing to builder

Thanks for helping improve the builder plugin. This document covers how to
set up, test, and submit changes.

## Project conventions

- **License:** dual MIT OR Apache-2.0 (see `LICENSE`, `LICENSE-MIT`,
  `LICENSE-APACHE`). The top-level `LICENSE` file explains the intended split:
  **Apache-2.0** for the Amazon-interfacing code (`backend.py`, `adapter.py`,
  `auth/sso_oidc.py`, `__init__.py`) and **MIT** for code not related to Amazon.
  You may apply either license to the whole project; the split is the project's
  intended mapping, not a hard per-file restriction. By contributing you agree
  your contributions are released under these terms. SPDX headers
  (`# SPDX-License-Identifier: MIT OR Apache-2.0`) are kept on source files.
- **Trademarks:** Amazon, AWS, Amazon Q, and Builder ID are trademarks of
  Amazon.com, Inc. This is an unofficial community project, not affiliated with
  or endorsed by Amazon. Keep wording that respects those marks and states
  non-affiliation where appropriate.
- **Commits:** short, label-prefixed subjects using Conventional Commits-lite:
  `feat(builder):`, `fix(builder):`, `chore(builder):`, `docs(builder):`,
  `test(builder):`, `refactor(builder):`, `sec(builder):`. Bodies only
  when a critical bug/security reason must be recorded. Keep subjects ≤ ~72
  chars.
- **Branches:** use one of `fix/`, `bugs/`, or `feature/` plus a short
  descriptor. Examples: `fix/setup-dual-activation`, `feature/chat-tools`.
  This repo uses lightweight auto-labeling, so matching these prefixes keeps
  PRs labeled automatically.

## Development setup

The plugin is a Hermes Agent plugin. It needs:

- Python 3.11+ (the Hermes agent venv is at `<HERMES_HOME>/hermes-agent/venv`).
- `requests` and `botocore` (already present in the Hermes agent venv).
- A Builder ID token for any **live** chat/auth calls (see `bid_login`).

Run the test suite with the Hermes agent's Python so `hermes-agent` and its
`tools.registry` are importable:

```bash
<HERMES_HOME>/hermes-agent/venv/bin/python3 -m pytest tests/ -q
python3 verify.py   # headless load + tool-registration + secret-leak checks
```

`conftest.py` redirects `HERMES_HOME` to a throwaway temp profile, so tests
never read or write your real Hermes state.

## Testing rules

- **Keep tests headless.** No browser, no live secrets, no network. Tests that
  need a token stub `backend.chat` / `auth.sso_oidc` rather than calling Amazon
  Q. The one exception is the live OIDC test, guarded by `BUILD_LIVE=1` and
  skipped otherwise.
- Add a regression test for any parser/transport change (the SSE frame shape,
  the `<tool_call>` → `tool_calls` translation, the event-stream decoder).
- `verify.py` must stay green — it is the secret-leak gate.

## Architecture pointers

- `backend.py` — direct HTTPS chat with Amazon Q (`GenerateAssistantResponse`),
  Bearer-only (no SigV4). Owns token resolution via `auth/sso_oidc`.
- `adapter.py` — optional OpenAI-compatible `/v1/chat/completions` server so
  builder can be a *selectable chat model*. **Loopback-only bridge**: it
  proxies Q with the stored token and has no auth; it refuses to bind any
  non-loopback host unless `AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1` is set. Do not
  weaken this.
- `auth/sso_oidc.py` — RFC 8628 device flow, anonymous public client. Secrets
  (`auth/bid_token.json`, `auth/bid_registration.json`, `auth/bid_flow.json`) are written
  `chmod 600` and gitignored. **Never** return a raw token from a tool handler.
- `region.py` does not exist — endpoints are pinned to `us-east-1` in code.

## Security checklist for changes

- [ ] No raw token/secret in any tool handler output (verify.py enforces this).
- [ ] The adapter stays loopback-only (no new bind path without the guard).
- [ ] Secret files remain `chmod 600` + gitignored; use the `_write_secret`
      atomic temp-then-rename helper.
- [ ] No new hardcoded credentials or endpoints beyond the pinned Q/OIDC hosts.

## Submitting

1. Fork / branch, make focused commits, keep the suite green.
2. Run `pytest` and `verify.py` before pushing.
3. Open a PR describing the change and any live-testing you performed.

By submitting a contribution you certify it is your own work and licensed under
MIT OR Apache-2.0 as described above.
