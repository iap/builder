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


def _content_indent(ln):
    """Column where this line's content begins.

    A block sequence item (`- foo`) begins its content after the `- `
    indicator, so it is logically nested under a mapping key at the *same*
    indentation column (the compact YAML form `cli:\n  - builder`)."""
    s = ln.lstrip()
    ind = len(ln) - len(s)
    if s.startswith("-") and (len(s) == 1 or s[1] == " "):
        return ind + 2
    return ind


def _strip_inline_comment(s):
    """Drop a trailing YAML `#` comment (whitespace-preceded, outside quotes)."""
    in_single = in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and i > 0 and s[i - 1] in " \t":
            return s[:i].rstrip()
    return s.rstrip()


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1].strip()
    return s


def _mapping_key(s):
    """Unquoted key of a `key: ...` line (comment stripped), else None."""
    s = _strip_inline_comment(s)
    if ":" not in s:
        return None
    return _unquote(s.split(":", 1)[0].strip())


def _provider_value(s):
    """Unquoted value after `provider:` (comment stripped), else None."""
    s = _strip_inline_comment(s)
    if not s.startswith("provider:"):
        return None
    return _unquote(s.split(":", 1)[1].strip())


def _is_builder_item(s):
    s = _strip_inline_comment(s).strip()
    if s == "builder":
        return True
    if s.startswith("-"):
        return _unquote(s[1:].strip()) == "builder"
    return False


removed = []
emptied = set()  # container paths (tuples) that _cleanup may have emptied


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

        while stack and stack[-1][0] >= _content_indent(ln):
            stack.pop()

        if not s or s.startswith("#"):
            out.append(ln)
            i += 1
            continue

        path = [k for (_i, k) in stack]

        # 1) provider blocks: aws-builder:/builder: directly under `providers`
        provider_slug = _mapping_key(s) if path == ["providers"] else None
        if provider_slug in ("aws-builder", "builder"):
            removed.append("providers:" + provider_slug)
            emptied.add(tuple(path))
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
        #    lists are the installer-owned `platform_toolsets.cli` and
        #    `known_plugin_toolsets.cli`. Any other toolset sub-key (or a list
        #    nested deeper, e.g. plugins.enabled.user_groups) is user-owned —
        #    leave it.
        if _is_builder_item(s):
            if path == ["plugins", "enabled"]:
                removed.append("list:builder")
                emptied.add(tuple(path))
                i += 1
                continue
            if path in (["platform_toolsets", "cli"], ["known_plugin_toolsets", "cli"]):
                removed.append("list:builder")
                emptied.add(tuple(path))
                i += 1
                continue

        # 3) dangling model.provider pointing at a removed slug
        if path == ["model"] and _provider_value(s) in ("aws-builder", "builder"):
            removed.append("model.provider")
            emptied.add(tuple(path))
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
    # Drop only containers that _cleanup actually emptied, cascading to their
    # empty parents. Candidates come from `emptied` (the exact container paths
    # a builder entry was removed from), so an unrelated empty container such
    # as plugins.user_groups: [] is never touched.

    changed = True
    while changed:
        changed = False
        out = []
        stack = []  # [(indent, key), ...] — ancestor mapping keys of current line
        for idx, ln in enumerate(lines):
            s = ln.strip()
            ind = _indent(ln)
            if not s or s.startswith("#"):
                out.append(ln)
                continue
            while stack and stack[-1][0] >= _content_indent(ln):
                stack.pop()
            ancestors = [k for (_i, k) in stack]
            keyname = s.split(":", 1)[0].strip() if ":" in s else None
            is_empty_literal = s.endswith("[]") or s.endswith("{}")
            is_map_key = s.endswith(":") and not s.startswith("-")
            container_path = tuple(ancestors + ([keyname] if keyname else []))
            if (is_empty_literal or is_map_key) and container_path in emptied:
                # empty if no non-comment child at greater indent
                has_child = False
                j = idx + 1
                while j < len(lines):
                    nxt = lines[j]
                    if nxt.strip() == "":
                        j += 1
                        continue
                    if _content_indent(nxt) <= ind:
                        break
                    has_child = True
                    break
                if not has_child:
                    changed = True  # drop this empty container
                    if ancestors:
                        emptied.add(tuple(ancestors))  # cascade to the parent
                    continue
            out.append(ln)
            if is_map_key and keyname:
                stack.append((ind, keyname))
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
