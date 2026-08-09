# Changelog

All notable changes to the Hermes Builder plugin are documented here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/),
commits use Conventional Commits-lite with the `builder` scope.

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
