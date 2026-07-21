#!/usr/bin/env bash
# builder plugin: remove builder as a selectable Hermes chat model.
#
# WHY: install (setup.sh) adds a `providers: builder` entry + `plugins.enabled`
# entry so Hermes can route chat to the in-plugin adapter on :8077. Hermes core
# does NOT auto-clean a plugin's config on `hermes plugins uninstall` (that only
# rmtree's the plugin dir), so without this step an uninstall leaves a dangling
# provider pointing at a dead :8077 endpoint and a stale enabled entry.
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

# Resolve a Python that can execute the inline config-edit scripts.
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  HM="${HERMES_HOME:-$HOME/.hermes}"
  for candidate in \
    "$HM/hermes-agent/venv/bin/python3" \
    "$HM/venv/bin/python3"; do
    if [[ -x "$candidate" ]]; then
      PYTHON="$candidate"
      break
    fi
  done
fi
if [[ -z "$PYTHON" ]]; then
  echo "✗ python3 is required for config edits (install Python or add it to PATH)." >&2
  exit 1
fi

# Backup once
BACKUP="${CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"
cp "$CONFIG" "$BACKUP"
echo "✓ backed up config → $BACKUP"

# 1) Remove the providers: builder block (idempotent)
"$PYTHON" - "$CONFIG" <<'PY'
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

if grep -qE '^[[:space:]]*builder:' "$CONFIG"; then
  echo "✓ providers: builder still present — check backup $BACKUP"
else
  echo "✓ removed providers: builder from $CONFIG"
fi

# 2) Normalize plugins.enabled (remove builder if present)
"$PYTHON" - "$CONFIG" <<'PY'
import sys, yaml
cfg = sys.argv[1]
c = yaml.safe_load(open(cfg)) or {}
en = (c.get("plugins") or {}).get("enabled") or []
en = [str(x) for x in en]
if "builder" in en:
    c.setdefault("plugins", {})["enabled"] = [x for x in en if x != "builder"]
    yaml.safe_dump(c, open(cfg, "w"), sort_keys=False, default_flow_style=False)
    print("removed_builder_from_enabled")
else:
    print("builder_not_in_enabled")
PY

echo
echo "NEXT: restart Hermes (and run 'hermes plugins uninstall builder' to drop the dir)."
echo "      The :8077 adapter stops when the session ends (or on unregister())."
