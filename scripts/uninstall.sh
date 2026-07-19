#!/usr/bin/env bash
# aws-build plugin: remove aws-build as a selectable Hermes chat model.
#
# WHY: install (setup.sh) adds a `providers: aws-build` entry + `plugins.enabled`
# entry so Hermes can route chat to the in-plugin adapter on :8077. Hermes core
# does NOT auto-clean a plugin's config on `hermes plugins uninstall` (that only
# rmtree's the plugin dir), so without this step an uninstall leaves a dangling
# provider pointing at a dead :8077 endpoint and a stale enabled entry.
#
# SAFE: idempotent (no-op if already absent), always backs up config.yaml first.
# User-invoked (never auto-run by the plugin) to respect Hermes' config-write guard.
#
# USAGE:  ~/.hermes/plugins/aws-build/scripts/uninstall.sh
#         then restart Hermes.

set -euo pipefail

CONFIG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"

if [[ ! -f "$CONFIG" ]]; then
  echo "✗ config.yaml not found at $CONFIG" >&2
  exit 1
fi

# Idempotency: nothing to remove?
if ! grep -qE '^[[:space:]]*aws-build:' "$CONFIG"; then
  echo "✓ providers: aws-build already absent from $CONFIG — nothing to do."
  # still normalize plugins.enabled (in case it lists aws-build without a provider block)
else
  # Backup (once)
  BACKUP="${CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"
  cp "$CONFIG" "$BACKUP"
  echo "✓ backed up config → $BACKUP"

  # Remove the providers: aws-build block (the 'aws-build:' key, which is
  # indented under 'providers:', plus its child lines). Match on stripped
  # line == 'aws-build:' (unique key, any indentation).
  "$HOME/.hermes/hermes-agent/venv/bin/python3" - "$CONFIG" <<'PY'
import sys
cfg = sys.argv[1]
lines = open(cfg).read().splitlines()
out, drop = [], False
for ln in lines:
    if ln.strip() == "aws-build:":
        drop = True            # start dropping this block
        continue
    if drop:
        if ln and not ln.startswith("  "):
            drop = False       # next top-level (or sibling) key -> stop dropping
        else:
            continue           # still inside the aws-build block
    out.append(ln)
open(cfg, "w").write("\n".join(out).rstrip("\n") + "\n")
PY
  echo "✓ removed providers: aws-build from $CONFIG"
fi

# Normalize plugins.enabled (remove aws-build if present) — backup-safe.
"$HOME/.hermes/hermes-agent/venv/bin/python3" - "$CONFIG" <<'PY'
import sys, yaml
cfg = sys.argv[1]
c = yaml.safe_load(open(cfg))
en = (c.get("plugins") or {}).get("enabled") or []
if "aws-build" in en:
    c.setdefault("plugins", {})["enabled"] = [x for x in en if x != "aws-build"]
    yaml.safe_dump(c, open(cfg, "w"), sort_keys=False, default_flow_style=False)
    print("✓ removed aws-build from plugins.enabled")
else:
    print("✓ aws-build not in plugins.enabled — nothing to do.")
PY

echo
echo "NEXT: restart Hermes (and run 'hermes plugins uninstall aws-build' to drop the dir)."
echo "      The :8077 adapter stops when the session ends (or on unregister())."
