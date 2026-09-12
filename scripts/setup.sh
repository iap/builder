#!/usr/bin/env bash
# builder plugin: register builder as a selectable Hermes chat model.
set -euo pipefail

export PYTHONUTF8=1

CONFIG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"
BACKUP="${CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"
PORT="${AWS_BUILD_ADAPTER_PORT:-8088}"

if [[ ! -f "$CONFIG" ]]; then
  echo "✗ config.yaml not found at $CONFIG" >&2
  exit 1
fi

# Stamp the installed copy with the source revision.
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/builder"
SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v git >/dev/null 2>&1 && git -C "$SRC_ROOT" rev-parse >/dev/null 2>&1; then
  git -C "$SRC_ROOT" rev-parse HEAD > "$PLUGIN_DIR/REVISION" 2>/dev/null \
    && echo "✓ stamped plugin REVISION ($(cat "$PLUGIN_DIR/REVISION"))" \
    || echo "  (skipped REVISION stamp)" >&2
else
  date +%Y-%m-%d > "$PLUGIN_DIR/REVISION" 2>/dev/null \
    && echo "✓ stamped plugin REVISION (date fallback)" \
    || echo "  (skipped REVISION stamp)" >&2
fi

# Backup
cp "$CONFIG" "$BACKUP"
echo "✓ backed up config → $BACKUP"

# Detect existing providers: block indentation.
INDENT=$(python3 - "$CONFIG" <<'PY'
import sys
from pathlib import Path

cfg = Path(sys.argv[1])
if not cfg.exists():
    raise SystemExit(2)
text = cfg.read_text()
for line in text.splitlines():
    if line.strip() == "providers:":
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

# Build the provider block from plugin.yaml.
BLOCK_FILE="$(mktemp)"
PLUGIN_YAML="${HERMES_HOME:-$HOME/.hermes}/plugins/builder/plugin.yaml"
if [[ ! -f "$PLUGIN_YAML" ]]; then
  PLUGIN_YAML="$SRC_ROOT/plugin.yaml"
fi
if [[ ! -f "$PLUGIN_YAML" ]]; then
  echo "✗ plugin.yaml not found (installed plugin or $SRC_ROOT)" >&2
  exit 1
fi

# Generate the provider block. Uses pyyaml if available, otherwise emits a
# plain mapping. Works without pyyaml so setup.sh runs in minimal environments.
python3 - "$BLOCK_FILE" "$PLUGIN_YAML" "$PORT" <<'PY'
import sys

blockfile, plugin_yaml, port = sys.argv[1], sys.argv[2], sys.argv[3]

models = None
try:
    import yaml
    with open(plugin_yaml) as fh:
        manifest = yaml.safe_load(fh) or {}
    models = manifest.get("models")
except Exception:
    pass

if not isinstance(models, list) or not models:
    models = ["auto", "claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5"]
models = [str(m) for m in models]
model = models[0]
model_scalar = '"' + model + '"'
model_mapping = {m: {} for m in models}
try:
    import yaml
    model_yaml = yaml.dump({"models": model_mapping}, default_flow_style=False, sort_keys=False)
except Exception:
    model_lines = ["models:"]
    for m in models:
        model_lines.append(f"  {m}: {{}}")
    model_yaml = "\n".join(model_lines) + "\n"
model_lines = model_yaml.rstrip("\n").splitlines()
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

# Rewrite the temp file with the detected indent and insert into config.
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

# Parse the config to check if aws-builder already exists under providers.
c = {}
try:
    import yaml
    c = yaml.safe_load(raw) or {}
except Exception:
    c = {}
providers = c.get("providers")
has_existing = isinstance(providers, dict) and "aws-builder" in providers

if has_existing:
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
    # Atomic write
    import os
    from tempfile import NamedTemporaryFile
    tmp = NamedTemporaryFile(mode="w", dir=os.path.dirname(cfg_path), delete=False, encoding="utf-8")
    try:
        tmp.write("\n".join(raw_lines) + "\n")
        tmp.flush()
        tmp.close()
        os.replace(tmp.name, cfg_path)
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        raise
    print("✓ updated providers: aws-builder in config.yaml (model catalog refreshed)")
    sys.exit(0)

# No aws-builder entry yet — insert under the existing providers: block.
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

# Atomic write
import os
from tempfile import NamedTemporaryFile
tmp = NamedTemporaryFile(mode="w", dir=os.path.dirname(cfg_path), delete=False, encoding="utf-8")
try:
    tmp.write("\n".join(raw_lines) + "\n")
    tmp.flush()
    tmp.close()
    os.replace(tmp.name, cfg_path)
except Exception:
    try:
        os.unlink(tmp.name)
    except Exception:
        pass
    raise
PY

if ! grep -qE '^[[:space:]]*aws-builder:' "$CONFIG"; then
  echo "✗ insert failed; restored from backup." >&2
  cp "$BACKUP" "$CONFIG"
  exit 1
fi

# Persist the provider entry stamp.
PORT_DIR="${HERMES_HOME:-$HOME/.hermes}/builder"
mkdir -p "$PORT_DIR"
python3 - "$BLOCK_FILE" "$PORT_DIR/adapter_stamp.json" <<'PY'
import json
import sys

block_path, stamp_path = sys.argv[1], sys.argv[2]
try:
    with open(block_path, encoding="utf-8") as fh:
        text = fh.read()
    import re
    match = re.search(r'aws-builder:\n(.*?)(?=\n\s*\w+:\n|\Z)', text, re.DOTALL)
    if match:
        block_text = "aws-builder:\n" + match.group(1)
        entry = {}
        for line in block_text.splitlines()[1:]:
            if ':' in line:
                key, val = line.split(':', 1)
                entry[key.strip()] = val.strip()
        if entry.get("base_url"):
            with open(stamp_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(entry))
except Exception as exc:
    print(f"  (skipped stamp: {exc})" >&2)
PY

# Ensure builder is in plugins.enabled.
python3 - "$CONFIG" <<'PY'
import sys
from pathlib import Path

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

if plugins_idx is None:
    lines.append("")
    lines.append("plugins:")
    lines.append("  enabled:")
    lines.append("    - builder")
    p.write_text("\n".join(lines) + "\n")
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
    p.write_text("\n".join(lines) + "\n")
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
p.write_text("\n".join(lines) + "\n")
print("✓ added builder to plugins.enabled")

PY

echo
echo "NEXT: restart Hermes, then in TUI/CLI use '-m aws-builder' or pick 'AWS Builder'."
echo "      (login once with: bid_login  — approve in browser)"

# Best-effort reachability probe.
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 2 "http://localhost:${PORT}/healthz" >/dev/null 2>&1; then
    echo "✓ adapter reachability probe passed on :${PORT}"
  else
    echo "ℹ adapter not reachable yet on :${PORT} — it will start when Hermes loads the plugin"
  fi
else
  echo "ℹ curl not available; skipping adapter reachability probe"
fi
