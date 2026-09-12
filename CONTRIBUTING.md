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
- **Commits:** short subjects using Conventional Commits, without a scope:
  `feat:`, `fix:`, `chore:`, `docs:`, `test:`,
  `refactor:`, `sec:`. No `(builder)` scope — this repo contains only the
  builder plugin, so it adds no information. Bodies only
  when a critical bug/security reason must be recorded. Keep subjects ≤ ~72
  chars.

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
  builder can be a *selectable chat model*. **Loopback-only server**: it
  proxies Q with the stored token and has no auth; it refuses to bind any
  non-loopback host unless `AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1` is set. Do not
  weaken this.
- `auth/sso_oidc.py` — RFC 8628 device flow, anonymous public client. Secrets
  (`auth/bid_token.json`, `auth/bid_registration.json`, `auth/bid_flow.json`) are written
  `chmod 600` and gitignored. **Never** return a raw token from a tool handler.
- Endpoints are pinned to `us-east-1` in code.

## Security checklist for changes

- [ ] No raw token/secret in any tool handler output (verify.py enforces this).
- [ ] The adapter stays loopback-only (no new bind path without the guard).
- [ ] Secret files remain `chmod 600` + gitignored; use the `_write_secret`
      atomic temp-then-rename helper.
- [ ] No new hardcoded credentials or endpoints beyond the pinned Q/OIDC hosts.

## Branch flow

1. Branch from `main`: `<type>/kebab-case-topic` (e.g. `fix/adapter-timeout`,
   `docs/branch-flow`). One branch per change; one PR per branch.
2. Push early and open a draft PR when work spans both environments, so the
   other side reviews instead of duplicating the work.
3. Before merge, sync with `main`: rebase if the branch is private to you,
   merge `main` into the branch if both environments share it. Never rewrite
   history someone else may have pulled — force-push only your own PR branch,
   and only with `--force-with-lease`.
4. Merge requirements: CI green, `verify.py` green, review comments addressed.
   Merge with a merge commit (preserves review context); never commit directly
   to `main`.
5. Delete the branch after merge so `git branch -r` stays readable.

## Submitting

1. Fork / branch, make focused commits, keep the suite green.
2. Branch name uses a Conventional Commits type prefix with kebab-case scope:
   `feat/…`, `fix/…`, `sec/…`, `refactor/…`, `test/…`, `docs/…`, `chore/…`.
3. Run `pytest` and `verify.py` before pushing.
4. Open a PR describing the change and any live-testing you performed.
5. Use GitHub alert syntax in the PR body to call out critical information
   (blockquote form — bare `[!...]` markers render as literal text):

   > [!IMPORTANT]
   > changes that affect install/uninstall flow, config migration, or
   > workflow scope requirements (e.g., "CI changes excluded; follow-up
   > PR needed").

   > [!WARNING]
   > hard gates that must not be bypassed (e.g., "`verify.py` reports a
   > secret leak"; "do not merge while adapter loopback guard is weakened").

   > [!NOTE]
   > informational context that affects review (e.g., "tested only on
   > Windows; CI covers Ubuntu").

   > [!CAUTION]
   > behavioral changes that could surprise users (e.g., "token store path
   > changed"; "Python floor lowered").

   Place alerts at the top of the PR body so they are visible without
   scrolling.

By submitting a contribution you certify it is your own work and licensed under
MIT OR Apache-2.0 as described above.
