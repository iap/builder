#!/usr/bin/env bash
# builder plugin: remove builder as a selectable Hermes chat model.
#
# WHY: install (setup.sh) adds a `providers: builder` entry + `plugins.enabled`
# entry so Hermes can route chat to the in-plugin adapter on :8088. Hermes core
# does NOT auto-clean a plugin's config on `hermes plugins uninstall` (that only
# rmtree's the plugin dir), so without this step an uninstall leaves a dangling
# provider pointing at a dead :8088 endpoint and a stale enabled entry.
#
# SAFE: idempotent (no-op if already absent), always backs up config.yaml first.
# User-invoked (never auto-run by the plugin) to respect Hermes' config-write guard.
#
# USAGE:  ${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/uninstall.sh
#         then restart Hermes.

set -euo pipefail

CONFIG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"

if [[ ! -f "$CONFIG" ]]; then
  echo "✗ config.yaml not found at $CONFIG" >&2
  exit 1
fi

# Idempotency: nothing to remove?
if ! grep -qE '^[[:space:]]*builder:' "$CONFIG"; then
  echo "✓ providers: builder already absent from $CONFIG — nothing to do."
  # still normalize plugins.enabled (in case it lists builder without a provider block)
else
  # Backup (once)
  BACKUP="${CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"
  cp "$CONFIG" "$BACKUP"
  echo "✓ backed up config → $BACKUP"

  # Remove the providers: builder block (the 'builder:' key, which is
  # indented under 'providers:', plus its child lines). Match on stripped
  # line == 'builder:' (unique key, any indentation).
  python3 - "$CONFIG" <<'PY'
import sys
cfg = sys.argv[1]
lines = open(cfg).read().splitlines()
out, drop = [], False
for ln in lines:
    if ln.strip() == "builder:":
        drop = True            # start dropping this block
        continue
    if drop:
        if ln and not ln.startswith("  "):
            drop = False       # next top-level (or sibling) key -> stop dropping
        else:
            continue           # still inside the builder block
    out.append(ln)
open(cfg, "w").write("\n".join(out).rstrip("\n") + "\n")
PY
  echo "✓ removed providers: builder from $CONFIG"
fi

# Normalize plugins.enabled (remove builder if present) — backup-safe.
python3 - "$CONFIG" <<'PY'
import sys, yaml
cfg = sys.argv[1]
c = yaml.safe_load(open(cfg))
en = (c.get("plugins") or {}).get("enabled") or []
if "builder" in en:
    c.setdefault("plugins", {})["enabled"] = [x for x in en if x != "builder"]
    yaml.safe_dump(c, open(cfg, "w"), sort_keys=False, default_flow_style=False)
    print("✓ removed builder from plugins.enabled")
else:
    print("✓ builder not in plugins.enabled — nothing to do.")
PY

echo
echo "NEXT: restart Hermes (and run 'hermes plugins uninstall builder' to drop the dir)."
echo "      The :8088 adapter stops when the session ends (or on unregister())."
