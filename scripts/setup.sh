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

# Idempotency: already present? Match the provider key specifically
# (^aws-builder:), NOT a bare `builder:` which also appears under unrelated
# keys like `plugins.entries.builder:` — a loose match there caused setup.sh
# to falsely report "already present" and skip (re)writing the provider entry
# even when providers.aws-builder was actually absent (see issue from #26).
if grep -qE '^[[:space:]]*aws-builder:' "$CONFIG"; then
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
    base_url: http://localhost:${PORT}/v1
    api_key: no-key-required
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
  echo "✓ added providers: aws-builder → http://localhost:${PORT}/v1 (transport: openai_chat, in-process adapter on :${PORT}, api_key: no-key-required)"
  # Ensure builder is in plugins.enabled so the dashboard tab + the plugin
  # loader actually activate it. The builder plugin is kind: standalone, which
  # is opt-in via plugins.enabled; without this entry it is silently gated out
  # of both the dashboard sidebar and the agent plugin loader (see uninstall.sh,
  # which removes the same key). Idempotent + sibling-safe.
  python3 - "$CONFIG" <<'PY'
import sys, yaml
p = sys.argv[1]
c = yaml.safe_load(open(p)) or {}
if not isinstance(c.get("plugins"), dict):
    c["plugins"] = {}
c["plugins"].setdefault("enabled", [])
if "builder" not in c["plugins"]["enabled"]:
    c["plugins"]["enabled"].append("builder")
    yaml.safe_dump(c, open(p, "w"), default_flow_style=False, sort_keys=False)
    print("✓ added builder to plugins.enabled")
else:
    print("✓ builder already in plugins.enabled")
PY
  echo
  echo "NEXT: restart Hermes, then in TUI/CLI use '-m builder' or pick 'AWS Builder'."
  echo "      (login once with: bid_login  — approve in browser)"
else
  echo "✗ insert failed; restored from backup." >&2
  cp "$BACKUP" "$CONFIG"
  exit 1
fi
