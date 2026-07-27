#!/usr/bin/env bash
# builder plugin: register builder as a selectable Hermes chat model.
#
# WHY: Hermes routes chat through providers declared in ${HERMES_HOME:-$HOME/.hermes}/config.yaml
# with transport: openai_chat. The plugin ships a self-contained OpenAI-
# compatible adapter (adapter.py, launched by register()) that translates to
# Amazon Q. This script adds the providers: builder entry pointing at that
# adapter (localhost :8088) — no daemon, no orphaned ref.
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
PORT="${AWS_BUILD_ADAPTER_PORT:-8088}"


if [[ ! -f "$CONFIG" ]]; then
  echo "✗ config.yaml not found at $CONFIG" >&2
  exit 1
fi

# Stamp the installed copy with the source revision so a later merge can be
# detected as drift (see verify.py's staleness check). Runs BEFORE the
# idempotency early-exit below so existing installations get stamped too, not
# just fresh ones. Source repo = parent of this script's dir (scripts/). Falls
# back to a date stamp if not a git repo.
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/builder"
SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v git >/dev/null 2>&1 && git -C "$SRC_ROOT" rev-parse >/dev/null 2>&1; then
  git -C "$SRC_ROOT" rev-parse HEAD > "$PLUGIN_DIR/REVISION" 2>/dev/null \
    && echo "✓ stamped plugin REVISION ($(cat "$PLUGIN_DIR/REVISION"))" \
    || echo "  (skipped REVISION stamp: not a git repo)" >&2
else
  date +%Y-%m-%d > "$PLUGIN_DIR/REVISION" 2>/dev/null \
    && echo "✓ stamped plugin REVISION (date fallback)" \
    || echo "  (skipped REVISION stamp)" >&2
fi

# Idempotency: already present? Matches either slug the plugin may have used
# (aws-builder is the current slug; builder was the pre-rename fallback).
if grep -qE '^[[:space:]]*(aws-)?builder:' "$CONFIG"; then
  echo "✓ providers: aws-builder already present in $CONFIG — nothing to do."
  echo "  Restart Hermes if you haven't since installing the plugin."
  exit 0
fi

# Backup
cp "$CONFIG" "$BACKUP"
echo "✓ backed up config → $BACKUP"

# Insert the block as a top-level providers: key, using Python
# (reliable indentation handling). Idempotent: only if absent.
# Write the block to a temp file (real newlines, not escaped).
BLOCK_FILE="$(mktemp)"
cat > "$BLOCK_FILE" <<EOF
  aws-builder:
    name: AWS Builder
    transport: openai_chat
    base_url: http://127.0.0.1:${PORT}/v1
    key_env: AWS_BUILD_ADAPTER_DUMMY
    models:
      - auto
      - claude-sonnet-4.5
      - claude-sonnet-4
      - claude-haiku-4.5
EOF

python3 - "$CONFIG" "$BLOCK_FILE" <<'PY'
import sys
cfg, blockfile = sys.argv[1], sys.argv[2]
block = open(blockfile).read().rstrip("\n")
lines = open(cfg).read().splitlines()
if any(l.strip() == "aws-builder:" for l in lines):
    sys.exit(0)  # idempotent guard (shell already checked)
# Insert inside an existing providers: block, OR append a new providers:
# block at EOF if none exists (common when config has no providers: yet).
if any(l.strip() == "providers:" for l in lines):
    out, i, n, in_prov, done = [], 0, len(lines), False, False
    while i < n:
        out.append(lines[i])
        if not done and in_prov and (
            i + 1 == n or (lines[i + 1] and not lines[i + 1].startswith("  "))
        ):
            out.extend(block.splitlines())
            done = True
        if lines[i] == "providers:":
            in_prov = True
        elif lines[i] and not lines[i].startswith("  ") and lines[i] != "providers:":
            in_prov = False  # left the providers block (next top-level key)
        i += 1
    open(cfg, "w").write("\n".join(out) + "\n")
else:
    # No providers: block yet — append one with the aws-builder entry.
    with open(cfg, "a") as fh:
        fh.write("\nproviders:\n" + "\n".join("  " + ln for ln in block.splitlines()) + "\n")
PY
rm -f "$BLOCK_FILE"

if grep -qE '^[[:space:]]*aws-builder:' "$CONFIG"; then
  echo "✓ added providers: aws-builder → http://127.0.0.1:${PORT}/v1 (transport: openai_chat, in-process adapter on :8088)"
  echo
  echo "NEXT: restart Hermes, then in TUI/CLI use '-m builder' or pick 'AWS Builder'."
  echo "      (login once with: bid_login  — approve in browser)"
else
  echo "✗ insert failed; restored from backup." >&2
  cp "$BACKUP" "$CONFIG"
  exit 1
fi
