#!/usr/bin/env bash
# builder plugin: remove builder as a selectable Hermes chat model.
#
# WHY: install (`hermes plugins install` + setup.sh) adds a `providers: builder`
# entry (setup.sh) and a `plugins.enabled` entry (the plugin installer) so Hermes
# can route chat to the in-plugin adapter on :8088. `hermes plugins install` also
# registers the builder toolset under `platform_toolsets.cli` and
# `known_plugin_toolsets.cli`. Hermes core does NOT auto-clean any of these on
# `hermes plugins uninstall` (that only rmtree's the plugin dir), so without this
# step an uninstall leaves a dangling provider (dead :8088 endpoint), a stale
# enabled entry, and dangling toolset-list entries pointing at a removed plugin.
#
# SAFE: idempotent (no-op if builder is already absent everywhere), backs up
# config.yaml once before any rewrite, and restores that backup on any failure.
# User-invoked (never auto-run by the plugin) to respect Hermes' config-write
# guard.
#
# USAGE:  ${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/uninstall.sh
#         then run 'hermes plugins uninstall builder' to drop the dir, and restart Hermes.

set -euo pipefail

# The Python heredocs print Unicode (✓/→/✗); force UTF-8 so they don't crash
# when stdout is a non-UTF-8 pipe (e.g. Windows cp1252 under redirect).
export PYTHONUTF8=1

CONFIG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"

if [[ ! -f "$CONFIG" ]]; then
  echo "✗ config.yaml not found at $CONFIG" >&2
  exit 1
fi

# Line-based, comment-preserving cleanup. Removes ONLY builder's own entries:
#   * providers.builder            (setup.sh)
#   * plugins.enabled entry        (the plugin installer)
#   * platform_toolsets.cli        (the plugin installer)
#   * known_plugin_toolsets.cli    (the plugin installer)
#   * dangling model.provider      (if it pointed at the removed slug)
# Sibling keys/providers and all user comments/formatting are preserved — we
# never do a yaml.safe_load + safe_dump round-trip (that strips comments).
python3 - "$CONFIG" <<'PY'
import sys
import os
from pathlib import Path
from datetime import datetime

cfg_path = sys.argv[1]
raw = Path(cfg_path).read_text()

# Idempotency: nothing to remove at all?
if "builder" not in raw:
    print("✓ builder already absent from", cfg_path, "— nothing to do.")
    sys.exit(0)

# Back up once, before any rewrite.
backup = f"{cfg_path}.bak.{datetime.now():%Y%m%d_%H%M%S}"
Path(backup).write_text(raw)
print("✓ backed up config →", backup)


def _indent(ln):
    return len(ln) - len(ln.lstrip())


def _is_builder_item(s):
    return s == "builder" or s == "- builder" or (s.startswith("-") and s[1:].strip() == "builder")


removed = []


def _cleanup(lines):
    # Path-scoped removal (Greptile review): only touch entries this plugin
    # actually owns, so an unrelated user key/list that merely shares the name
    # "builder" is left alone. Tracks the YAML ancestor path via an indent-key
    # stack instead of matching raw text/indentation in isolation.
    out = []
    stack = []  # [(indent, key), ...] — ancestor mapping keys of the current line
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        s = ln.strip()
        ind = _indent(ln)

        while stack and stack[-1][0] >= ind:
            stack.pop()

        if not s or s.startswith("#"):
            out.append(ln)
            i += 1
            continue

        path = [k for (_i, k) in stack]

        # 1) provider blocks: aws-builder:/builder: directly under `providers`
        if s in ("aws-builder:", "builder:") and path == ["providers"]:
            removed.append("providers:" + s.rstrip(":"))
            ki = ind
            j = i + 1
            while j < n:
                nxt = lines[j]
                if nxt.strip() == "" or _indent(nxt) > ki:
                    j += 1
                    continue
                break
            i = j
            continue

        # 2) `- builder` list items: only at the exact plugin-managed list
        #    paths. `plugins.enabled` is the enabled-plugin list; the toolset
        #    lists live directly under a toolset sub-key (the installer writes
        #    platform_toolsets.cli / known_plugin_toolsets.cli). A list nested
        #    deeper (e.g. plugins.enabled.user_groups) is user-owned — leave it.
        if _is_builder_item(s):
            if path == ["plugins", "enabled"]:
                removed.append("list:builder")
                i += 1
                continue
            if len(path) == 2 and path[0] in ("platform_toolsets", "known_plugin_toolsets"):
                removed.append("list:builder")
                i += 1
                continue

        # 3) dangling model.provider pointing at a removed slug
        if (
            s in ("provider: aws-builder", "provider: builder",
                  'provider: "aws-builder"', 'provider: "builder"')
            and path == ["model"]
        ):
            removed.append("model.provider")
            i += 1
            continue

        out.append(ln)

        # Push this mapping key so following (deeper) lines can see it.
        if ":" in s and not s.startswith("-"):
            key = s.split(":", 1)[0].strip()
            if key:
                stack.append((ind, key))

        i += 1

    return out


def _prune_empty(lines):
    # 4) drop containers left empty by the removals above, under the keys this
    # plugin manages. Iterates until stable so emptying a child cascades to its
    # parent. Unrelated empty containers (e.g. mcp_servers: {}) are untouched.
    managed = {"providers", "plugins", "platform_toolsets", "known_plugin_toolsets", "model"}
    changed = True
    while changed:
        changed = False
        out = []
        cur_top = None
        for idx, ln in enumerate(lines):
            s = ln.strip()
            ind = _indent(ln)
            if not s or s.startswith("#"):
                out.append(ln)
                continue
            if ind == 0:
                cur_top = s.split(":", 1)[0].strip() if ":" in s else None
            keyname = s.split(":", 1)[0].strip() if ":" in s else None
            is_managed = (ind == 0 and keyname in managed) or (ind > 0 and cur_top in managed)
            if not is_managed:
                out.append(ln)
                continue
            is_empty_literal = s.endswith("[]") or s.endswith("{}")
            is_map_key = s.endswith(":") and not s.startswith("-")
            if not (is_empty_literal or is_map_key):
                out.append(ln)
                continue
            has_child = False
            j = idx + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() == "":
                    j += 1
                    continue
                if _indent(nxt) <= ind:
                    break
                has_child = True
                break
            if has_child:
                out.append(ln)
                continue
            changed = True  # drop this empty container
        lines = out
    return lines


try:
    lines = raw.splitlines()
    lines = _cleanup(lines)
    lines = _prune_empty(lines)
except Exception as exc:
    # Restore the pristine config on any unexpected failure — never leave a
    # half-removed or truncated file behind.
    Path(cfg_path).write_text(raw)
    print(f"✗ uninstall failed, config restored: {exc}", file=sys.stderr)
    sys.exit(2)

if not removed:
    print("✓ no builder entries found to remove")
    sys.exit(0)

# Atomic write (temp + replace) so a failure never leaves a truncated config.
tmp = Path(cfg_path).with_name(Path(cfg_path).name + ".tmp")
tmp.write_text("\n".join(lines) + "\n")
os.replace(tmp, cfg_path)
print("✓ removed builder entries:", ", ".join(removed))

PY

echo
echo "NEXT: run 'hermes plugins uninstall builder' to drop the dir, then restart Hermes."
echo "      The :8088 adapter stops when the session ends (or on unregister())."
