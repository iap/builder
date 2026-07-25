#!/usr/bin/env bash
# builder plugin: remove builder as a selectable Hermes chat model.
#
# WHY: install (`hermes plugins install` + setup.sh) adds a `providers: builder`
# entry (setup.sh) and a `plugins.enabled` entry (the plugin installer) so Hermes
# can route chat to the in-plugin adapter on :8088. `hermes plugins install` also
# registers the builder toolset under `platform_toolsets.cli` and
# `known_plugin_toolsets.cli`. Hermes core does NOT auto-clean any of these on
# `hermes plugins uninstall` (that only rmtree's the plugin dir), so without this
# step an uninstall leaves a dangling provider (dead :8088 endpoint), a stale
# enabled entry, and dangling toolset-list entries pointing at a removed plugin.
#
# SAFE: idempotent (no-op if builder is already absent everywhere), backs up
# config.yaml once before any rewrite. User-invoked (never auto-run by the
# plugin) to respect Hermes' config-write guard.
#
# USAGE:  ${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/uninstall.sh
#         then run 'hermes plugins uninstall builder' to drop the dir, and restart Hermes.

set -euo pipefail

CONFIG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"

if [[ ! -f "$CONFIG" ]]; then
  echo "✗ config.yaml not found at $CONFIG" >&2
  exit 1
fi

# One-pass, YAML-aware cleanup. Removes ONLY builder's own entries:
#   * providers.builder            (setup.sh)
#   * plugins.enabled entry        (the plugin installer)
#   * platform_toolsets.cli        (the plugin installer)
#   * known_plugin_toolsets.cli    (the plugin installer)
# Sibling keys/providers are preserved — we never delete a line by indentation
# alone, so an unrelated provider block after `builder` is left intact.
python3 - "$CONFIG" <<'PY'
import sys, yaml

cfg_path = sys.argv[1]
with open(cfg_path) as fh:
    raw = fh.read()

# Idempotency: nothing to remove at all?
if "builder" not in raw:
    print("✓ builder already absent from", cfg_path, "— nothing to do.")
    sys.exit(0)

# Back up once, before any rewrite.
import os
from datetime import datetime
backup = f"{cfg_path}.bak.{datetime.now():%Y%m%d_%H%M%S}"
with open(backup, "w") as bf:
    bf.write(raw)
print("✓ backed up config →", backup)

c = yaml.safe_load(raw) or {}

# 1) providers.builder block
providers = c.get("providers")
if isinstance(providers, dict) and "builder" in providers:
    del providers["builder"]
    if not providers:
        c.pop("providers", None)
    print("✓ removed providers: builder")

# 2) plugins.enabled entry
plugins = c.setdefault("plugins", {})
enabled = plugins.get("enabled") or []
if "builder" in enabled:
    plugins["enabled"] = [x for x in enabled if x != "builder"]
    print("✓ removed builder from plugins.enabled")

# 3) toolset lists (platform / known_plugin)
removed_lists = []
for key in ("platform_toolsets", "known_plugin_toolsets"):
    block = c.get(key)
    if isinstance(block, dict):
        for sub, val in block.items():
            if isinstance(val, list) and "builder" in val:
                block[sub] = [x for x in val if x != "builder"]
                removed_lists.append(f"{key}.{sub}")
if removed_lists:
    print("✓ removed builder from toolset lists:", ", ".join(removed_lists))

yaml.safe_dump(c, open(cfg_path, "w"), sort_keys=False, default_flow_style=False)
PY

echo
echo "NEXT: run 'hermes plugins uninstall builder' to drop the dir, then restart Hermes."
echo "      The :8088 adapter stops when the session ends (or on unregister())."
