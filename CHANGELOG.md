# Changelog

All notable changes to the Hermes Builder plugin are documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/).
History uses Conventional Commits-lite with the `builder` scope;
new commits use scope-less Conventional Commits (see `AGENTS.md`).

## [Unreleased]

### Fixed

- **`_provider.py` — `unregister_provider()` legacy slug mismatch** — the loop removed `"builder"` entries but the actual legacy slug is `"aws-build"`, leaving old config entries orphaned after unregister. Now iterates `("aws-builder", "aws-build")`.
- **`conftest.py` — Windows symlink failure** — the test fixture used `symlink_to()` which requires admin privileges on Windows, breaking the entire test suite for non-admin users. Falls back to `shutil.copytree` when symlinks fail with `OSError`.
- **`tests/test_dashboard.py` — hard `fastapi` import** — importing `fastapi` at module level aborted test collection when `fastapi` wasn't installed. Now uses `pytest.importorskip()` so the suite runs without dev deps.
- **`scripts/setup.sh` — `python3` not found on Windows** — the script assumed `python3` is on PATH (it isn't on Windows). Now uses `${PYTHON:-python3}` with a `python` fallback, and the `PYTHON` env var for explicit control.
- **`scripts/uninstall.sh` — same `python3` issue** — applied the same `PYTHON` fallback as `setup.sh`.

### Changed

- **`dashboard/manifest.json` — version bump 1.0.0 → 1.1.0** — matched the version in `plugin.yaml` and `pyproject.toml`.
- **`pyproject.toml` — removed empty `[tool.ty]` section** — `ty` config was unused dead config.
- **`pytest.ini` — clarified filterwarnings comment** — the comment incorrectly implied no filter existed, but `pyproject.toml` has one.
- **`pyproject.toml` — Python 3.12 → 3.11** — matching hermes-agent's `requires-python = ">=3.11,<3.14"` floor.
- **`CONTRIBUTING.md` and `docs/architecture.md` — removed confusing `region.py` reference** — the file doesn't exist; replaced with a direct statement that endpoints are pinned to `us-east-1`.
- **`docs/testing.md` — removed `test_chat_dispatch.py` reference** — file doesn't exist at that path; the integration test exists at `tests/integration/test_chat_dispatch.py` but is not a top-level test.

### Tests

- 241 tests pass, 1 skipped (live OIDC), 0 failures.
- `verify.py` — all 30 checks passed.

## [1.1.0] — 2026-08-08

### Summary

Complete codebase audit and cleanup pass. Removed dead code, fixed misleading
commit history, standardized configuration, hardened the CI pipeline, added
fuzz coverage for the adapter's tool-call XML parser, and resolved the
StarletteDeprecationWarning noise in tests.

### Fixed

- **starlette deprecation warning noise** — suppressed at import-time via
  `conftest.py` (`warnings.filterwarnings`) and registered in
  `pyproject.toml` (`[tool.pytest.ini_options] filterwarnings` with fully
  qualified `starlette.exceptions.StarletteDeprecationWarning`)
- **`plugin.yaml` version never actually bumped** — commit 3f6c283 claimed
  to bump to 1.1.0 but only changed the author field; the version field was
  still 1.0.0. Corrected.
- **`_provider.py` `_revision` noise** — removed 17 lines that wrote an
  undocumented `_revision` key to `config.yaml`, eliminating "unknown config
  keys ignored" log spam on every Hermes load
- **`httpx` version too loose** — pinned `httpx>=0.28` to prevent the
  StarletteDeprecationWarning from `httpx2` from becoming a hard failure
- **`fastapi` pin misleadingly tight** — relaxed from `>=0.139` to `>=0.115`
  (the installed 0.139 already satisfies this; 0.115 is the true floor)
- **CI integration tests skipped silently** — the main `test` job didn't
  check out `hermes-agent`, causing integration tests to skip via
  `pytest.skip(allow_module_level=True)`. Now checked out as a sibling
  directory with `HERMES_HOME` + `PYTHONPATH` configured.
- **CI checked out upstream repo** — changed `NousResearch/hermes-agent`
  to `iap/hermes-agent` in both the `test` and `integration-test` jobs.

### Changed

- **`setup.sh` model list dynamic** — replaced hardcoded model list with
  runtime read of `plugin.yaml`, enforcing single-source-of-truth per
  AGENTS.md rules
- **Stale `_revision` test assertions** — removed the `or k == "_revision"`
  exception from "no private marker key" checks in
  `tests/integration/test_build_provider.py`

### Added

- **16 fuzz tests for adapter tool-call XML parser** — `test_parse_tool_calls_fuzz`
  (12 parametrized edge cases: nested braces, escaped quotes, malformed JSON,
  duplicate names, very long arguments, deeply nested objects, unclosed JSON,
  empty strings, empty argument objects, mixed OpenAI-style fields) +
  `test_parse_tool_calls_cap_at_20`, `test_strip_and_parse_xml_style_call`,
  `test_parse_tool_calls_empty_string`, `test_extract_balanced_brace_nested_quotes`
- **Regression test for StarletteDeprecationWarning suppression** —
  `test_starlette_deprecation_warning_suppressed` verifies both `conftest.py`
  and `pyproject.toml` have the suppression configured
- **`CHANGELOG.md`** — this file

### Security

- No token exposure in tool handler output — `verify.py` continues to pass
  (no `access_token`, `client_secret`, or `refresh_token` in any output)
- `adapter.py` loopback guard untouched (`_resolve_bind_host()` rejects
  non-loopback hosts unless `AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1`)
- Secret files written via `_write_secret()` (chmod 600, atomic temp-then-rename)

### Tests

- 186 tests → 187 passing, 1 skipped (live OIDC), 0 warnings
- `verify.py` — all checks passed

## [1.0.0] — 2024-12-15

### Added

- Initial release — Amazon Q Developer (Claude models) as a Hermes chat tool
  and selectable model, authenticated via Amazon Builder ID (RFC 8628 device flow)
- `backend.py` — direct HTTPS chat with Amazon Q (`GenerateAssistantResponse`)
- `adapter.py` — OpenAI-compatible `/v1/chat/completions` SSE server (`:8088`)
- `auth/sso_oidc.py` — RFC 8628 device flow, token store, refresh
- `dashboard/` — FastAPI dashboard card for login/profile/models
- `build_cli.py` — standalone CLI (login/status/whoami/logout/models)
- `verify.py` — headless load + tool-registration + secret-leak checks
