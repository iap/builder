"""Regression tests for scripts/uninstall.sh.

These exercise the REAL cleanup code embedded in the shell heredoc (not a
mirror of it), so the comment-preserving, path-scoped removal stays correct.

Motivated by two Greptile review rounds:
  * unscoped provider / list / model removal (fixed: exact plugin-managed paths)
  * nested `- builder` items under a plugin-managed key's *descendant* list
    (e.g. plugins.enabled.user_groups, platform_toolsets.cli.user_groups) must
    be preserved — they are user-owned lists that merely share the name.
"""

import re
import textwrap
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "uninstall.sh"
_HEREDOC = re.search(
    r"<<'PY'\n(.*?)\nPY\n", _SCRIPT.read_text(encoding="utf-8"), re.DOTALL
)
assert _HEREDOC, "uninstall.sh Python heredoc not found"

# Extract only the helper functions (skip the top-level code that reads
# sys.argv[1], backs up and rewrites the real config file).
_FUNCS = re.search(r"(def _indent.*?)(?=\ntry:)", _HEREDOC.group(1), re.DOTALL).group(1)
_NS = {"removed": []}
# Run the script's own embedded helper functions so these tests exercise the
# real cleanup code rather than a re-implementation of it.
exec(compile(_FUNCS, "<uninstall_helpers>", "exec"), _NS)  # noqa: S102


def _run(config_text):
    _NS["removed"] = []
    lines = _NS["_cleanup"](config_text.splitlines())
    lines = _NS["_prune_empty"](lines)
    return "\n".join(lines) + "\n", list(_NS["removed"])


def _assert(out, removed, absent=(), present=(), removed_has=None):
    for tok in absent:
        assert tok not in out, f"expected '{tok}' to be removed, got:\n{out}"
    for tok in present:
        assert tok in out, f"expected '{tok}' to be preserved, got:\n{out}"
    if removed_has is not None:
        assert removed_has in removed, f"expected {removed_has!r} in {removed!r}"


def test_uninstall_happy_path_removes_only_owned_entries():
    cfg = textwrap.dedent(
        """\
        # my comment
        providers:
          aws-builder:
            type: aws-bid
          other-provider:
            type: foo
        plugins:
          enabled:
            - builder
            - other
        platform_toolsets:
          cli:
            - builder
            - ask_q
        known_plugin_toolsets:
          cli:
            - builder
        model:
          provider: aws-builder
          temperature: 0.7
        # trailing comment
        """
    )
    out, removed = _run(cfg)
    _assert(
        out,
        removed,
        absent=["aws-builder:", "type: aws-bid", "- builder", "provider: aws-builder"],
        present=[
            "# my comment",
            "other-provider:",
            "type: foo",
            "temperature: 0.7",
            "# trailing comment",
            "- other",
            "- ask_q",
        ],
        removed_has="providers:aws-builder",
    )


def test_uninstall_preserves_unrelated_top_level_builder_block():
    cfg = textwrap.dedent(
        """\
        builder:
          unrelated: keep-me
        providers:
          aws-builder:
            type: aws-bid
        """
    )
    out, removed = _run(cfg)
    _assert(
        out,
        removed,
        absent=["aws-builder:", "type: aws-bid"],
        present=["builder:", "unrelated: keep-me"],
        removed_has="providers:aws-builder",
    )


def test_uninstall_preserves_unrelated_builder_list_item():
    cfg = textwrap.dedent(
        """\
        some_other_tools:
          - builder
          - other
        plugins:
          enabled:
            - builder
        """
    )
    out, removed = _run(cfg)
    _assert(out, removed, absent=[], present=["some_other_tools:", "- builder"])


def test_uninstall_preserves_unrelated_model_provider():
    cfg = textwrap.dedent(
        """\
        some_section:
          provider: builder
        model:
          provider: builder
        """
    )
    out, removed = _run(cfg)
    _assert(
        out,
        removed,
        absent=[],
        present=["some_section:", "provider: builder"],
        removed_has="model.provider",
    )


def test_uninstall_idempotent_noop_when_nothing_to_remove():
    cfg = textwrap.dedent(
        """\
        providers:
          other-provider:
            type: foo
        plugins:
          enabled:
            - other
        """
    )
    out, removed = _run(cfg)
    assert removed == [], f"expected no removals, got {removed!r}"
    assert "other-provider:" in out


def test_uninstall_preserves_nested_builder_under_plugins_enabled_descendant():
    # Greptile: plugins.enabled.user_groups is a *user-owned* nested list that
    # merely sits below an `enabled` ancestor — it must not be touched.
    cfg = textwrap.dedent(
        """\
        plugins:
          enabled:
            user_groups:
              - builder
        """
    )
    out, removed = _run(cfg)
    _assert(out, removed, absent=[], present=["user_groups:", "- builder"])
    assert "list:builder" not in removed


def test_uninstall_preserves_nested_builder_under_toolset_descendant():
    # Greptile: platform_toolsets.cli.user_groups / known_plugin_toolsets.cli
    # .user_groups are nested deeper than the toolset list itself — preserve.
    cfg = textwrap.dedent(
        """\
        platform_toolsets:
          cli:
            user_groups:
              - builder
        known_plugin_toolsets:
          cli:
            user_groups:
              - builder
        """
    )
    out, removed = _run(cfg)
    _assert(out, removed, absent=[], present=["user_groups:", "- builder"])
    assert "list:builder" not in removed


def test_uninstall_removes_builder_at_exact_toolset_list_path():
    # The toolset lists live *directly* under the sub-key (depth 2): those are
    # plugin-owned and must be removed.
    cfg = textwrap.dedent(
        """\
        platform_toolsets:
          cli:
            - builder
            - ask_q
        known_plugin_toolsets:
          cli:
            - builder
        """
    )
    out, removed = _run(cfg)
    _assert(out, removed, absent=["- builder"], present=["- ask_q"])
    assert removed.count("list:builder") == 2


def test_uninstall_preserves_unrelated_empty_containers():
    # Greptile re-review: _prune_empty must only remove containers at exact
    # plugin-managed paths (plugins.enabled, toolset sub-lists, providers,
    # model). A user-owned empty container under a managed top-level key
    # (e.g. plugins.user_groups: []) must be preserved.
    cfg = textwrap.dedent(
        """\
        plugins:
          enabled:
            - builder
          user_groups: []
        platform_toolsets:
          cli:
            - builder
          extra: {}
        known_plugin_toolsets:
          cli:
            - builder
          extra: {}
        """
    )
    out, removed = _run(cfg)
    # Builder-owned containers are pruned once emptied...
    _assert(out, removed, absent=["enabled:", "cli:", "- builder"])
    # ...but unrelated empty containers are preserved.
    _assert(out, removed, present=["user_groups: []", "extra: {}"])


def test_uninstall_preserves_custom_toolset_lists():
    # Greptile round 4: only the installer-owned platform_toolsets.cli and
    # known_plugin_toolsets.cli lists are Builder-owned. A custom toolset
    # sub-key (e.g. platform_toolsets.extra) that happens to contain
    # "- builder" is user-owned and must be preserved.
    cfg = textwrap.dedent(
        """\
        platform_toolsets:
          cli:
            - builder
          extra:
            - builder
        known_plugin_toolsets:
          cli:
            - builder
          custom:
            - builder
        """
    )
    out, removed = _run(cfg)
    # The installer-owned cli lists are removed and pruned...
    _assert(out, removed, absent=["cli:"])
    # ...but custom toolset sub-lists (and their builder items) are preserved.
    _assert(out, removed, present=["extra:", "custom:", "- builder"])
    assert removed.count("list:builder") == 2


def test_uninstall_removes_same_indented_toolset_entries():
    # Greptile round 5: YAML allows a block sequence item at the same column as
    # its mapping key (`cli:\n  - builder` parses as `cli: [builder]`). This
    # compact form must still be cleaned up, not left as a dangling reference.
    cfg = (
        "platform_toolsets:\n"
        "  cli:\n"
        "  - builder\n"
        "known_plugin_toolsets:\n"
        "  cli:\n"
        "  - builder\n"
    )
    out, removed = _run(cfg)
    _assert(out, removed, absent=["cli:", "- builder"])
    assert removed.count("list:builder") == 2


def test_uninstall_keeps_compact_sibling_under_cli():
    # Greptile round 6: when a compact toolset list holds builder plus a sibling
    # (cli: - builder, - ask_q), removing builder must not prune cli and
    # re-parent ask_q under platform_toolsets/known_plugin_toolsets.
    cfg = (
        "platform_toolsets:\n"
        "  cli:\n"
        "  - builder\n"
        "  - ask_q\n"
        "known_plugin_toolsets:\n"
        "  cli:\n"
        "  - builder\n"
        "  - ask_q\n"
    )
    out, removed = _run(cfg)
    _assert(out, removed, absent=["- builder"], present=["cli:", "- ask_q"])
    assert removed.count("list:builder") == 2
