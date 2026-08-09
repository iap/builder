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
PLUGIN_YAML="${HERMES_HOME:-$HOME/.hermes}/plugins/builder/plugin.yaml"
python3 - "$BLOCK_FILE" "$PLUGIN_YAML" "$PORT" <<'PY'
import sys, yaml

blockfile, plugin_yaml, port = sys.argv[1], sys.argv[2], sys.argv[3]
with open(plugin_yaml) as fh:
    manifest = yaml.safe_load(fh) or {}

models = manifest.get("models") or ["auto"]
# Coerce all model identifiers to str — YAML 1.1 may parse numeric-looking
# values as int/float (e.g. "4.0" as float). The model catalog must be
# exact strings; backend.list_models() already does this coercion for the
# same reason.
models = [str(m) for m in models]
# Use yaml.dump for serialized model identifiers to ensure proper scalar
# serialization. This handles edge cases like embedded quotes, newlines,
# and YAML-special characters that would break the config or silently alter
# identifiers. The models block is emitted as a YAML mapping ({m: {} for m in
# models}) to match what Hermes core expects in config.yaml providers entries.
# We include the "models:" wrapper key in the dump so the indentation is
# handled correctly by yaml itself.
model_mapping = {m: {} for m in models}
model_yaml = yaml.dump({"models": model_mapping}, default_flow_style=False)
model_lines = model_yaml.rstrip("\n").splitlines()
# Indent each line by 4 spaces so the block sits correctly under aws-builder
model_lines = ["    " + ln for ln in model_lines]
lines = [
    "  aws-builder:",
    "    name: AWS Builder",
    "    transport: openai_chat",
    f"    base_url: http://localhost:{port}/v1",
    "    api_key: no-key-required",
    "    model: \"auto\"",
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
providers = c.setdefault("providers", {})
has_existing = "aws-builder" in providers

if has_existing:

    new_models = {}
    current_model = None
    # Parse the generated block YAML directly rather than manual string
    # extraction — this preserves exact model identifiers with proper
    # unquoting and handles edge cases (quotes, newlines, special chars).
    try:
        block_parsed = yaml.safe_load(block) or {}
    except Exception:
        block_parsed = {}
    provider_block = block_parsed.get("aws-builder", {})
    if isinstance(provider_block, dict):
        current_model = provider_block.get("model")
        models_field = provider_block.get("models") or []
        # models can be a list (from yaml.dump of a list) or a mapping
        # (from yaml.dump of {m: {}}). Handle both.
        if isinstance(models_field, list):
            for m in models_field:
                new_models[str(m)] = {}
        elif isinstance(models_field, dict):
            for m in models_field:
                new_models[str(m)] = {}

    existing = providers.get("aws-builder", {})
    if isinstance(existing, dict):
        existing["models"] = new_models
        if current_model:
            existing["model"] = current_model
        existing.setdefault("base_url", f"http://localhost:{port}/v1")
        existing.setdefault("transport", "openai_chat")
        existing.setdefault("api_key", "no-key-required")
        providers["aws-builder"] = existing
    else:
        providers["aws-builder"] = {
            "name": "AWS Builder",
            "base_url": f"http://localhost:{port}/v1",
            "transport": "openai_chat",
            "api_key": "no-key-required",
            "model": current_model or "auto",
            "models": new_models,
        }

    try:
        import yaml

        # yaml.safe_dump strips quotes from string keys that look like
        # numbers (e.g. "4o", "4.0"), causing them to be re-parsed as
        # float/int by Hermes core. Use a SafeDumper variant that
        # double-quotes any string scalar whose YAML-1.1 interpretation
        # would be non-string.
        import re as _re
        _num_like = _re.compile(r"^(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|true|True|TRUE|false|False|FALSE|null|Null|NULL|~)$")

        class _QuotedDumper(yaml.SafeDumper):
            pass

        def _str_representer(dumper, data):
            if _num_like.match(data):
                return dumper.represent_scalar(
                    "tag:yaml.org,2002:str", data, style='"'
                )
            return dumper.represent_scalar(
                "tag:yaml.org,2002:str", data
            )

        _QuotedDumper.add_representer(str, _str_representer)
        Path(cfg_path).write_text(
            yaml.dump(c, Dumper=_QuotedDumper, default_flow_style=False, sort_keys=False) + "\n"
        )
    except Exception as exc:
        print(f"✗ failed to update config: {exc}", file=sys.stderr)
        sys.exit(2)
    print("✓ updated providers: aws-builder in config.yaml (model catalog refreshed)")
    sys.exit(0)

# No aws-builder entry yet — insert under existing providers: block or append new one.
lines = raw.splitlines()
if any(line.strip() == "providers:" for line in lines):
    out, i, n, in_prov, done = [], 0, len(lines), False, False
    while i < n:
        out.append(lines[i])
        if (
            not done
            and in_prov
            and (i + 1 == n or (lines[i + 1] and not lines[i + 1].startswith(prefix)))
        ):
            out.extend(block.splitlines())
            done = True
        if lines[i].strip() == "providers:":
            in_prov = True
        elif lines[i] and not lines[i].startswith(prefix) and lines[i].strip() != "providers:":
            in_prov = False
        i += 1
    Path(cfg_path).write_text("\n".join(out) + "\n")
else:
    # No providers: block yet — append one with the aws-builder entry.
    with open(cfg_path, "a") as fh:
        fh.write("\nproviders:\n" + "\n".join(prefix + ln for ln in block.splitlines()) + "\n")
PY

if ! grep -qE '^[[:space:]]*aws-builder:' "$CONFIG"; then
  echo "✗ insert failed; restored from backup." >&2
  cp "$BACKUP" "$CONFIG"
  exit 1
fi

# Ensure builder is in plugins.enabled so the dashboard tab + the plugin
# loader actually activate it. The builder plugin is kind: standalone, which
# is opt-in via plugins.enabled; without this entry it is silently gated out
# of both the dashboard sidebar and the agent plugin loader.
python3 - "$CONFIG" <<'PY'
import sys, yaml
from pathlib import Path

p = sys.argv[1]
c = yaml.safe_load(Path(p).read_text()) or {}
if not isinstance(c.get("plugins"), dict):
    c["plugins"] = {}
c["plugins"].setdefault("enabled", [])
if "builder" not in c["plugins"]["enabled"]:
    c["plugins"]["enabled"].append("builder")
    Path(p).write_text(
        yaml.safe_dump(c, default_flow_style=False, sort_keys=False) + "\n"
    )
    print("✓ added builder to plugins.enabled")
else:
    print("✓ builder already in plugins.enabled")
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
