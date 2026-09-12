## What kind of change is this?

- [ ] bug fix
- [ ] bug fix + test
- [ ] feature
- [ ] refactor / cleanup
- [ ] docs / comments
- [ ] test-only
- [ ] security fix

## Summary

Briefly describe the change and why it is needed.

## Branch

This PR was opened from a branch using one of (matching a Conventional
Commits-lite scope in `AGENTS.md`):
- `feat/…`
- `fix/…`
- `sec/…`
- `refactor/…`
- `test/…`
- `docs/…`
- `chore/…`

> [!IMPORTANT]
> Branch prefix must match the PR's scope.

## Checklist

- [ ] Tests pass: `python3 -m pytest tests/ -q`
- [ ] `verify.py` is green
- [ ] No raw tokens/secrets in code, logs, or tool output
- [ ] Adapter stays loopback-only unless an explicit guard is added
- [ ] Updated docs/README if user-facing behavior changed

> [!WARNING]
> Do NOT merge if `verify.py` reports a secret leak. This is a hard gate.
