"""Regression tests for scripts/setup.sh provider-block generation.

These exercise the REAL block-generation heredoc embedded in setup.sh (not a
mirror of it), so the `model:` default stays consistent with register_provider().

Motivated by a Greptile review round:
  * setup.sh hardcoded `model: "auto"` even when a custom catalog did not
    declare `auto`, selecting a model outside the advertised catalog.
"""

import re
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup.sh"
_HEREDOC = re.search(
    r"<<'PY'\nimport sys, yaml\n(.*?)\nPY\n",
    _SCRIPT.read_text(encoding="utf-8"),
    re.DOTALL,
)
assert _HEREDOC, "setup.sh block-generation heredoc not found"
_BLOCK_CODE = "import sys, yaml\n" + _HEREDOC.group(1)


def _generate(manifest_text, tmp_path, monkeypatch):
    plugin_yaml = tmp_path / "plugin.yaml"
    plugin_yaml.write_text(manifest_text, encoding="utf-8")
    blockfile = tmp_path / "block.txt"
    monkeypatch.setattr(
        sys, "argv", ["setup.py", str(blockfile), str(plugin_yaml), "8088"]
    )
    exec(compile(_BLOCK_CODE, "<setup_block>", "exec"), {})  # noqa: S102
    return blockfile.read_text(encoding="utf-8")


def test_setup_default_model_matches_runtime_catalog(tmp_path, monkeypatch):
    """Custom catalog without `auto` must set model to the first declared
    model, matching register_provider() -> models[0]."""
    out = _generate(
        "models:\n  - custom-model\n  - other-model\n", tmp_path, monkeypatch
    )
    assert 'model: "custom-model"' in out
    assert 'model: "auto"' not in out


def test_setup_default_model_keeps_auto_when_declared(tmp_path, monkeypatch):
    """When `auto` is declared first (the shipped default), model stays auto."""
    out = _generate("models:\n  - auto\n  - claude-sonnet-4.5\n", tmp_path, monkeypatch)
    assert 'model: "auto"' in out
