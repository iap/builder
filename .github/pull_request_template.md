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

This PR was opened from a branch using one of:
- `fix/…`
- `bugs/…`
- `feature/…`

## Checklist

- [ ] Tests pass: `python -m pytest -q -k "not adapter"`
- [ ] `verify.py` is green
- [ ] No raw tokens/secrets in code, logs, or tool output
- [ ] Adapter stays loopback-only unless an explicit guard is added
- [ ] Updated docs/README if user-facing behavior changed
