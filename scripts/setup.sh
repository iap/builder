#!/usr/bin/env bash
# builder plugin: register builder as a selectable Hermes chat model.
#
# WHY: Hermes routes chat through providers declared in ${HERMES_HOME:-$HOME/.hermes}/config.yaml
# with transport: openai_chat. The plugin ships a self-contained OpenAI-
# compatible adapter (adapter.py, launched by register()) that translates to
# Amazon Q. This script adds the providers: aws-builder entry pointing at that
# adapter (localhost :8088) — no daemon, no orphaned ref.
#
# SAFE: idempotent (skips if already present), always backs up config.yaml
# first. Does NOT touch any other provider. User-invoked (never auto-run by
# the plugin) to respect Hermes' config-write guard.
#
# USAGE:  hermes plugins install <url> && ${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/setup.sh
#         then restart Hermes.

set -euo pipefail

# The Python heredocs print Unicode (✓/→/✗); force UTF-8 so they don't crash
# when stdout is a non-UTF-8 pipe (e.g. Windows cp1252 under redirect).
export PYTHONUTF8=1

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

# Always run the Python updater so config.yaml stays current with the
# plugin's declared model catalog (including new models, default model
# changes, and the provider entry is refreshed on each run).

# Backup
cp "$CONFIG" "$BACKUP"
echo "✓ backed up config → $BACKUP"

# Detect existing providers: block indentation so the inserted aws-builder
# block matches the file's style instead of hard-coding 2 spaces.
INDENT=$(python3 - "$CONFIG" <<'PY'
import sys
from pathlib import Path

cfg = Path(sys.argv[1])
if not cfg.exists():
    raise SystemExit(2)
text = cfg.read_text()
for line in text.splitlines():
    if line.strip() == "providers:":
        # Next non-empty line under providers: determines indent
        idx = text.splitlines().index(line)
        for nxt in text.splitlines()[idx + 1:]:
            if nxt.strip():
                indent = len(nxt) - len(nxt.lstrip())
                print(max(indent, 2))
                raise SystemExit(0)
        print(2)
        raise SystemExit(0)
print(2)
PY
)
if [[ ! "$INDENT" =~ ^[0-9]+$ ]]; then
  INDENT=2
fi

# Build the provider block from plugin.yaml (single source of truth for the
# model catalog) instead of hardcoding — setup.sh should not duplicate the
# model list that also lives in plugin.yaml and backend.list_models().
BLOCK_FILE="$(mktemp)"
# Manifest source: prefer the installed copy so the generated block matches
# what actually runs; fall back to this checkout so setup.sh also works when
# invoked from a source repo before `hermes plugins install` lands a copy.
PLUGIN_YAML="${HERMES_HOME:-$HOME/.hermes}/plugins/builder/plugin.yaml"
if [[ ! -f "$PLUGIN_YAML" ]]; then
  PLUGIN_YAML="$SRC_ROOT/plugin.yaml"
fi
if [[ ! -f "$PLUGIN_YAML" ]]; then
  echo "✗ plugin.yaml not found (installed plugin or $SRC_ROOT)" >&2
  exit 1
fi
python3 - "$BLOCK_FILE" "$PLUGIN_YAML" "$PORT" <<'PY'
import sys, yaml

blockfile, plugin_yaml, port = sys.argv[1], sys.argv[2], sys.argv[3]
with open(plugin_yaml) as fh:
    manifest = yaml.safe_load(fh) or {}

models = manifest.get("models")
if not isinstance(models, list) or not models:
    # Keep in sync with backend.STATIC_MODELS. setup.sh cannot import
    # backend.py (it depends on `requests`), so the fallback catalog is
    # duplicated here deliberately. A missing, empty, or non-list `models:`
    # in a custom plugin.yaml is treated as "use the built-in catalog", the
    # same way backend.list_models() falls back to STATIC_MODELS.
    models = ["auto", "claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5"]
# Coerce all model identifiers to str — YAML 1.1 may parse numeric-looking
# values as int/float (e.g. "4.0" as float). The model catalog must be
# exact strings; backend.list_models() already does this coercion for the
# same reason.
models = [str(m) for m in models]
# Derive the default model from the first declared model, matching
# register_provider() at runtime. A custom catalog without "auto" must still
# advertise a default that is present in its own models block — hardcoding
# "auto" would select a model the provider does not actually offer.
model = models[0]
model_scalar = yaml.dump(model, default_style='"').splitlines()[0]
# Use yaml.dump for serialized model identifiers to ensure proper scalar
# serialization. This handles edge cases like embedded quotes, newlines,
# and YAML-special characters that would break the config or silently alter
# identifiers. The models block is emitted as a YAML mapping ({m: {} for m in
# models}) to match what Hermes core expects in config.yaml providers entries.
# We include the "models:" wrapper key in the dump so the indentation is
# handled correctly by yaml itself.
model_mapping = {m: {} for m in models}
model_yaml = yaml.dump({"models": model_mapping}, default_flow_style=False, sort_keys=False)
model_lines = model_yaml.rstrip("\n").splitlines()
# Indent each line by 4 spaces so the block sits correctly under aws-builder
model_lines = ["    " + ln for ln in model_lines]
lines = [
    "  aws-builder:",
    "    name: AWS Builder",
    "    transport: openai_chat",
    f"    base_url: http://localhost:{port}/v1",
    "    api_key: no-key-required",
    f"    model: {model_scalar}",
    "    discover_models: false",
]
lines.extend(model_lines)

with open(blockfile, "w") as fh:
    fh.write("\n".join(lines) + "\n")
PY

# Rewrite the temp file with the detected indent.
python3 - "$CONFIG" "$INDENT" "$BLOCK_FILE" "$PORT" <<'PY'
import sys
from pathlib import Path

cfg_path, indent_str, blockfile, port = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
indent = int(indent_str)
raw = Path(cfg_path).read_text()
block = Path(blockfile).read_text().rstrip("\n")
prefix = " " * indent
lines = []
for line in block.splitlines():
    if line.startswith("  "):
        lines.append(prefix + line[2:])
    else:
        lines.append(line)
block = "\n".join(lines)

# Re-parse with a fixed prefix width so YAML loading is based on config
# content, not shell-quoted text.
prefix = " " * indent

# Parse the config to check if aws-builder already exists under providers.
# Use YAML parsing instead of string matching so we detect any form of the
# entry (dict, scalar, alias) — not just "aws-builder:\n".
c = {}
try:
    import yaml
    c = yaml.safe_load(raw) or {}
except Exception:
    c = {}
providers = c.get("providers")
has_existing = isinstance(providers, dict) and "aws-builder" in providers

if has_existing:
    # Line-based replacement of the existing aws-builder block. Preserves the
    # rest of config.yaml (comments, formatting, other providers) verbatim; a
    # full yaml.safe_load + safe_dump round-trip would strip every comment.
    # The freshly generated `block` (above) carries the canonical model catalog.
    raw_lines = raw.splitlines()
    ab_idx = None
    for i, ln in enumerate(raw_lines):
        if ln.strip() == "aws-builder:":
            ab_idx = i
            break
    if ab_idx is None:
        raw_lines.extend(block.splitlines())
    else:
        ab_indent = len(raw_lines[ab_idx]) - len(raw_lines[ab_idx].lstrip())
        end = ab_idx + 1
        while end < len(raw_lines):
            ln = raw_lines[end]
            if ln.strip() == "":
                end += 1
                continue
            if (len(ln) - len(ln.lstrip())) <= ab_indent:
                break
            end += 1
        block_lines = block.splitlines()
        base = None
        for ln in block_lines:
            if ln.strip():
                base = len(ln) - len(ln.lstrip())
                break
        reindented = []
        for ln in block_lines:
            if not ln.strip():
                reindented.append("")
            else:
                rel = (len(ln) - len(ln.lstrip())) - base
                reindented.append(" " * max(0, ab_indent + rel) + ln.lstrip())
        raw_lines[ab_idx:end] = reindented

def _atomic_write_lines(path, lines):
    """Atomic write to path: temp file adjacent to target, then os.replace()."""
    import os
    from tempfile import NamedTemporaryFile
    tmp = NamedTemporaryFile(
        mode="w", dir=os.path.dirname(str(path)), delete=False, encoding="utf-8"
    )
    tmp.write("\n".join(lines) + "\n")
    tmp.flush()
    os.fsync(tmp.fileno())
    tmp.close()
    os.replace(tmp.name, str(path))

    _atomic_write_lines(cfg_path, raw_lines)
    print("✓ updated providers: aws-builder in config.yaml (model catalog refreshed)")
    sys.exit(0)

# No aws-builder entry yet — insert under the existing providers: block, or
# create one. Handles every provider form (M14): `providers:` with children, an
# empty `providers:` stub, `providers: {}` / `providers: []` inline, or no
# providers key at all. The `block` is already re-indented to `indent` above.
raw_lines = raw.splitlines()
prov_idx = None
for i, ln in enumerate(raw_lines):
    s = ln.strip()
    if s == "providers:" or s in ("providers: {}", "providers: []"):
        prov_idx = i
        break

if prov_idx is None:
    raw_lines.extend(["", "providers:"])
    raw_lines.extend(block.splitlines())
else:
    s = raw_lines[prov_idx].strip()
    if s in ("providers: {}", "providers: []"):
        raw_lines[prov_idx] = " " * (len(raw_lines[prov_idx]) - len(raw_lines[prov_idx].lstrip())) + "providers:"
    raw_lines[prov_idx + 1:prov_idx + 1] = block.splitlines()
_atomic_write_lines(cfg_path, raw_lines)
PY

if ! grep -qE '^[[:space:]]*aws-builder:' "$CONFIG"; then
  echo "✗ insert failed; restored from backup." >&2
  cp "$BACKUP" "$CONFIG"
  exit 1
fi

# Persist the provider entry we just wrote so uninstall.sh can recognise it
# as plugin-owned even when AWS_BUILD_ADAPTER_PORT is no longer set (setup
# may have used a custom port, e.g. :9999). The stamp records the FULL entry
# — a bare port would stay trusted forever and make a later user-owned entry
# at that port look like ours — and is written only after the config update
# is verified. It lives under <HERMES_HOME>/builder/ (the plugin's data dir,
# which survives reinstalls — same reasoning as the token store), never as
# an extra key in config.yaml. Best-effort: a failed stamp only degrades
# uninstall to the env/8088 ownership heuristics.
PORT_DIR="${HERMES_HOME:-$HOME/.hermes}/builder"
mkdir -p "$PORT_DIR"
python3 - "$BLOCK_FILE" "$PORT_DIR/adapter_stamp.json" <<'PY'
import json
import sys

import yaml

block_path, stamp_path = sys.argv[1], sys.argv[2]
with open(block_path, encoding="utf-8") as fh:
    block = yaml.safe_load(fh) or {}
entry = block.get("aws-builder")
if isinstance(entry, dict) and entry.get("base_url"):
    with open(stamp_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(entry))
PY

# Ensure builder is in plugins.enabled so the dashboard tab + the plugin
# loader actually activate it. The builder plugin is kind: standalone, which
# is opt-in via plugins.enabled; without this entry it is silently gated out
# of both the dashboard sidebar and the agent plugin loader.
python3 - "$CONFIG" <<'PY'
import sys
from pathlib import Path

# Line-based insertion into plugins.enabled so the rest of config.yaml
# (comments, formatting, unrelated keys) is preserved verbatim. A full
# yaml.safe_load + safe_dump round-trip would strip every comment in the file.
p = Path(sys.argv[1])
text = p.read_text()
lines = text.splitlines()


def _indent(ln):
    return len(ln) - len(ln.lstrip())


def _is_builder_item(s):
    return s == "builder" or s == "- builder" or (s.startswith("-") and s[1:].strip() == "builder")


plugins_idx = None
for i, ln in enumerate(lines):
    if ln.strip() == "plugins:" and _indent(ln) == 0:
        plugins_idx = i
        break

def _atomic_write(path_obj, text):
    """Atomic write: temp file adjacent to target, then os.replace()."""
    from tempfile import NamedTemporaryFile
    tmp = NamedTemporaryFile(
        mode="w", dir=str(path_obj.parent), delete=False, encoding="utf-8"
    )
    tmp.write(text)
    tmp.flush()
    os.fsync(tmp.fileno())
    tmp.close()
    os.replace(tmp.name, str(path_obj))

if plugins_idx is None:
    lines.append("")
    lines.append("plugins:")
    lines.append("  enabled:")
    lines.append("    - builder")
    _atomic_write(p, "\n".join(lines) + "\n")
    print("✓ added builder to plugins.enabled")
    sys.exit(0)

enabled_idx = None
for i in range(plugins_idx + 1, len(lines)):
    ln = lines[i]
    if ln.strip() == "":
        continue
    if _indent(ln) == 0:
        break
    if ln.strip() == "enabled:" and _indent(ln) > 0:
        enabled_idx = i
        break

if enabled_idx is None:
    lines.insert(plugins_idx + 1, "  enabled:")
    lines.insert(plugins_idx + 2, "    - builder")
    _atomic_write(p, "\n".join(lines) + "\n")
    print("✓ added builder to plugins.enabled")
    sys.exit(0)

enabled_indent = _indent(lines[enabled_idx])
item_indent = " " * (enabled_indent + 2)
already = False
insert_after = enabled_idx
for i in range(enabled_idx + 1, len(lines)):
    ln = lines[i]
    if ln.strip() == "":
        continue
    if _indent(ln) <= enabled_indent:
        break
    if _is_builder_item(ln.strip()):
        already = True
        break
    insert_after = i

if already:
    print("✓ builder already in plugins.enabled")
    sys.exit(0)

lines.insert(insert_after + 1, f"{item_indent}- builder")
_atomic_write(p, "\n".join(lines) + "\n")
print("✓ added builder to plugins.enabled")

PY

echo
echo "NEXT: restart Hermes, then in TUI/CLI use '-m aws-builder' or pick 'AWS Builder'."
echo "      (login once with: bid_login  — approve in browser)"

# Best-effort reachability probe: if the adapter is already running on the
# configured port, verify it answers. This does not start the adapter;
# register() does that when Hermes loads the plugin.
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 2 "http://localhost:${PORT}/healthz" >/dev/null 2>&1; then
    echo "✓ adapter reachability probe passed on :${PORT}"
  else
    echo "ℹ adapter not reachable yet on :${PORT} — it will start when Hermes loads the plugin"
  fi
else
  echo "ℹ curl not available; skipping adapter reachability probe"
fi
