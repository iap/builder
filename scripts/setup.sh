#!/usr/bin/env bash
# builder plugin: register builder as a selectable Hermes chat model.
#
# WHY: Hermes routes chat through providers declared in ${HERMES_HOME:-$HOME/.hermes}/config.yaml
# with transport: openai_chat. The plugin ships a self-contained OpenAI-
# compatible adapter (adapter.py, launched by register()) that translates to
# Amazon Q. This script adds the providers: builder entry pointing at that
# adapter (localhost :8077) — NO :8088 bridge daemon, no orphaned ref.
# It ALSO adds builder to plugins.enabled so the plugin module loads and
# register() actually starts the adapter. Without both, the model picker
# shows a dead endpoint.
#
# SAFE: idempotent (skips if already present), always backs up config.yaml
# first. Does NOT touch any other provider. User-invoked (never auto-run by
# the plugin) to respect Hermes' config-write guard.
#
# USAGE:  hermes plugins install <url> && ${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/setup.sh
#         then restart Hermes.

set -euo pipefail

CONFIG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"
BACKUP="${CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"
PORT="${AWS_BUILD_ADAPTER_PORT:-8077}"

if [[ ! -f "$CONFIG" ]]; then
  echo "✗ config.yaml not found at $CONFIG" >&2
  exit 1
fi

# Resolve a Python that has PyYAML. Prefer `python3` on PATH; fall back to
# the Hermes venv if the user's PATH is minimal.
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  # Try the Hermes venv under HERMES_HOME or ~/.hermes.
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
cp "$CONFIG" "$BACKUP"
echo "✓ backed up config → $BACKUP"

# Resolve plugin root and manifest.
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/builder"
PLUGIN_YAML="$PLUGIN_DIR/plugin.yaml"

# Write the provider block to a temp file.
# NOTE: model list is read from plugin.yaml at install time so setup.sh and
# backend.py cannot drift. key_env removed: the adapter does not need one.
BLOCK_FILE="$(mktemp)"
cat > "$BLOCK_FILE" <<EOF
  builder:
    name: AWS Builder ID
    transport: openai_chat
    base_url: http://127.0.0.1:${PORT}/v1
    models:
EOF

"$PYTHON" - "$PLUGIN_YAML" "$BLOCK_FILE" <<'PY'
import sys, yaml
from pathlib import Path
yaml_path = Path(sys.argv[1])
block_path = Path(sys.argv[2])
raw = yaml.safe_load(yaml_path.read_text()) or {}
models = raw.get("models") or []
block = block_path.read_text()
if models:
    for m in models:
        block += f"\n      - {m}"
else:
    block += "\n      - auto"
block_path.write_text(block)
PY

# 1) Insert providers: builder block (idempotent: Python exits 0 if already present)
"$PYTHON" - "$CONFIG" "$BLOCK_FILE" <<'PY'
import sys
cfg, blockfile = sys.argv[1], sys.argv[2]
block = open(blockfile).read().rstrip("\n")
lines = open(cfg).read().splitlines()
if any(l.strip() == "builder:" for l in lines):
    sys.exit(0)
# If no providers: block exists, insert one first.
if not any(l.strip() == "providers:" for l in lines):
    lines.append("providers:")
out, i, n, in_prov, done = [], 0, len(lines), False, False
while i < n:
    out.append(lines[i])
    if not done and in_prov and (
        i + 1 == n or (lines[i + 1] and not lines[i + 1].startswith("  "))
    ):
        out.extend(block.splitlines())
        done = True
    if lines[i].strip() == "providers:":
        in_prov = True
    elif lines[i] and not lines[i].startswith("  ") and lines[i].strip() != "providers:":
        in_prov = False
    i += 1
open(cfg, "w").write("\n".join(out) + "\n")
PY
rm -f "$BLOCK_FILE"

# 2) Ensure builder is in plugins.enabled (idempotent)
"$PYTHON" - "$CONFIG" <<'PY'
import sys
import yaml
cfg = sys.argv[1]
c = yaml.safe_load(open(cfg)) or {}
en = (c.get("plugins") or {}).get("enabled") or []
if "builder" not in [str(x) for x in en]:
    c.setdefault("plugins", {})["enabled"] = [str(x) for x in en] + ["builder"]
    yaml.safe_dump(c, open(cfg, "w"), sort_keys=False, default_flow_style=False)
    print("added_builder_to_enabled")
else:
    print("builder_already_enabled")
PY

if grep -qE '^[[:space:]]*builder:' "$CONFIG"; then
  echo "✓ providers: builder → http://127.0.0.1:${PORT}/v1 (transport: openai_chat)"
  echo "✓ NO :8088 bridge — adapter launches inside the plugin on register()."
  echo
  echo "NEXT: restart Hermes, then in TUI/CLI use '-m builder' or pick 'AWS Builder ID'."
  echo "      (login once with: bid_login  — approve in browser)"
else
  echo "✗ insert failed; restored from backup." >&2
  cp "$BACKUP" "$CONFIG"
  exit 1
fi
