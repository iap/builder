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
#
# IMPORTANT: even when the provider block exists, we still run the Python
# updater below. This ensures that if the model catalog changes (new models
# added, models removed, default model changed), config.yaml is always
# brought up to date. The updater merges the current catalog with any
# user-customized fields (name, api_key, etc.) so nothing is lost.
#
# Previously setup.sh skipped the entire update when the provider key
# was already present, which left config.yaml stale after plugin upgrades.

# Always run the Python updater so config.yaml stays current with the
# plugin's declared model catalog (including new models, default model
# changes, and the _revision bump that forces a re-read).

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
import sys, yaml

cfg_path, blockfile = sys.argv[1], sys.argv[2]
block = open(blockfile).read().rstrip("\n")

with open(cfg_path) as f:
    raw = f.read()

# If aws-builder already exists, update it in place rather than skipping.
# This ensures config.yaml always reflects the current model catalog
# (including newly added models like 'auto' after plugin upgrades).
if "aws-builder:" in raw:
    c = yaml.safe_load(raw) or {}
    providers = c.setdefault("providers", {})

    # Parse the block to extract the model list.
    new_models = {}
    current_model = None
    in_models = False
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("model:"):
            current_model = stripped.split(":", 1)[1].strip()
        elif stripped == "models:":
            in_models = True
        elif in_models and stripped.startswith("- "):
            m = stripped[2:].strip()
            new_models[m] = {}
        elif in_models and stripped and not stripped.startswith("-") and not stripped.startswith("#"):
            # key: {} entry
            if ":" in stripped:
                k = stripped.split(":")[0].strip()
                if k and k != "":
                    new_models[k] = {}
        elif in_models and not stripped:
            pass  # blank line within models block

    existing = providers.get("aws-builder", {})
    if isinstance(existing, dict):
        # Preserve user-customized fields (name, api_key, etc.) but
        # update the model catalog and transport/base_url.
        existing["models"] = new_models
        if current_model:
            existing["model"] = current_model
        # Keep the existing base_url or set the default
        existing.setdefault("base_url", "http://localhost:8088/v1")
        existing.setdefault("transport", "openai_chat")
        existing.setdefault("api_key", "no-key-required")
        providers["aws-builder"] = existing
    else:
        # Existing entry is not a dict (unlikely but guard).
        providers["aws-builder"] = {
            "name": "AWS Builder",
            "base_url": "http://localhost:8088/v1",
            "transport": "openai_chat",
            "api_key": "no-key-required",
            "model": current_model or "auto",
            "models": new_models,
        }

    with open(cfg_path, "w") as f:
        yaml.safe_dump(c, f, default_flow_style=False, sort_keys=False)
    print("✓ updated providers: aws-builder in config.yaml (model catalog refreshed)")
    # Don't also append the block — we're done.
    sys.exit(0)

# No aws-builder entry yet — insert inside existing providers: block or append new one.
lines = raw.splitlines()
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
    open(cfg_path, "w").write("\n".join(out) + "\n")
else:
    # No providers: block yet — append one with the aws-builder entry.
    with open(cfg_path, "a") as fh:
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
  echo "NEXT: restart Hermes, then in TUI/CLI use '-m aws-builder' or pick 'AWS Builder'."
  echo "      (login once with: bid_login  — approve in browser)"
else
  echo "✗ insert failed; restored from backup." >&2
  cp "$BACKUP" "$CONFIG"
  exit 1
fi
