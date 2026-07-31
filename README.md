# builder

Hermes Agent plugin that exposes Amazon Q Developer as a chat tool and selectable model, authenticated via Amazon Builder ID (RFC 8628 device flow).

## Install

```bash
git clone <repo-url> ~/.hermes/plugins/builder
~/.hermes/plugins/builder/scripts/setup.sh
```

`setup.sh` is idempotent:
- backs up `config.yaml`
- writes/refreshes `providers.aws-builder`
- ensures `builder` is in `plugins.enabled`
- stamps plugin `REVISION`
- probes adapter reachability if `curl` is available

## Uninstall

```bash
~/.hermes/plugins/builder/scripts/uninstall.sh
hermes plugins uninstall builder
restart Hermes
```

`uninstall.sh` removes only builder-owned config:
- `providers.aws-builder`
- legacy `providers.builder`
- `plugins.enabled` entry
- `platform_toolsets.*` / `known_plugin_toolsets.*` entries
- dangling `model.provider` if it pointed at builder
- empty `providers` / `plugins` / toolset stubs

## Reinstall / migration notes

- Prefer running `uninstall.sh` before `hermes plugins uninstall builder`. Uninstalling the plugin dir first removes the script.
- After reinstall, restart Hermes so `register()` starts the in-process adapter and registers the provider entry.

## Agent review findings

Key fixes landed in this repo for uninstall/reinstall safety:
- duplicate provider removal in `uninstall.sh` removed
- legacy `builder` slug cleanup added in `_provider.py`
- empty config stubs pruned after uninstall
- `register()`/`unregister()` now idempotent and clean up the `pre_tool_call` hook
- `setup.sh` insertion is indentation-aware and includes an adapter reachability probe

## Verify

```bash
python3 -m pytest tests/ tests/integration -q
python3 verify.py
```
