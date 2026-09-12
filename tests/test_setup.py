"""Regression tests for scripts/setup.sh provider-block generation.

These exercise the REAL block-generation heredoc embedded in setup.sh (not a
mirror of it), so the `model:` default stays consistent with register_provider().
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup.sh"
_HEREDOC = re.search(
    r"<<'PY'\r?\nimport sys\r?\n\r?\nblockfile, plugin_yaml, port = sys\.argv\[1\], sys\.argv\[2\], sys\.argv\[3\]\r?\n(.*?)\r?\nPY\r?\n",
    _SCRIPT.read_text(encoding="utf-8"),
    re.DOTALL,
)
assert _HEREDOC, "setup.sh block-generation heredoc not found"
_BLOCK_CODE = (
    "import sys\nblockfile, plugin_yaml, port = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    + _HEREDOC.group(1)
)


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
    out = _generate(
        "models:\n  - custom-model\n  - other-model\n", tmp_path, monkeypatch
    )
    assert 'model: "custom-model"' in out
    assert 'model: "auto"' not in out


def test_setup_default_model_keeps_auto_when_declared(tmp_path, monkeypatch):
    out = _generate("models:\n  - auto\n  - claude-sonnet-4.5\n", tmp_path, monkeypatch)
    assert 'model: "auto"' in out


def test_setup_models_fallback_matches_static_models(tmp_path, monkeypatch):
    out = _generate("name: AWS Builder\n", tmp_path, monkeypatch)
    assert "claude-sonnet-4.5" in out
    assert "claude-haiku-4.5" in out


def test_setup_models_scalar_is_not_iterated(tmp_path, monkeypatch):
    out = _generate("models: auto\n", tmp_path, monkeypatch)
    assert 'model: "auto"' in out
    assert 'model: "a"' not in out


def _run_setup(home, plugin_yaml=None):
    (home / "config.yaml").write_text("other: value\n", encoding="utf-8")
    if plugin_yaml is not None:
        installed = home / "plugins" / "builder"
        installed.mkdir(parents=True)
        (installed / "plugin.yaml").write_text(plugin_yaml, encoding="utf-8")
    subprocess.run(
        ["bash", str(_SCRIPT)],
        check=True,
        capture_output=True,
        timeout=60,
        env={**os.environ, "HERMES_HOME": str(home), "AWS_BUILD_ADAPTER_PORT": "8088"},
    )
    return (home / "config.yaml").read_text(encoding="utf-8")


def test_setup_runs_from_source_checkout_without_installed_plugin(tmp_path):
    out = _run_setup(tmp_path)
    assert "aws-builder:" in out
    assert "http://localhost:8088/v1" in out
    assert "claude-haiku-4.5" in out


def test_setup_prefers_installed_manifest_over_source(tmp_path):
    out = _run_setup(tmp_path, plugin_yaml="models:\n  - custom-a\n  - custom-b\n")
    assert '"custom-a"' in out
    assert "claude-haiku-4.5" not in out


def test_setup_fails_cleanly_when_no_manifest_exists(tmp_path):
    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    shutil.copy(_SCRIPT, src / "scripts" / "setup.sh")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("other: value\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(src / "scripts" / "setup.sh")],
        check=False,
        capture_output=True,
        timeout=60,
        env={**os.environ, "HERMES_HOME": str(home), "AWS_BUILD_ADAPTER_PORT": "8088"},
    )
    assert proc.returncode == 1
    assert b"plugin.yaml not found" in proc.stderr


def test_setup_persists_provider_entry_stamp(tmp_path):
    _run_setup(tmp_path)
    stamp = json.loads(
        (tmp_path / "builder" / "adapter_stamp.json").read_text(encoding="utf-8")
    )
    assert stamp["base_url"] == "http://localhost:8088/v1"
    assert stamp["name"] == "AWS Builder"


def test_setup_then_uninstall_roundtrip_at_custom_port(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("other: value\n", encoding="utf-8")
    setup_env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "AWS_BUILD_ADAPTER_PORT": "9999",
    }
    subprocess.run(
        ["bash", str(_SCRIPT)],
        check=True,
        capture_output=True,
        timeout=60,
        env=setup_env,
    )
    stamp = json.loads(
        (home / "builder" / "adapter_stamp.json").read_text(encoding="utf-8")
    )
    assert stamp["base_url"] == "http://localhost:9999/v1"
    assert "http://localhost:9999/v1" in (home / "config.yaml").read_text(
        encoding="utf-8"
    )

    uninstall_env = {
        k: v for k, v in os.environ.items() if k != "AWS_BUILD_ADAPTER_PORT"
    }
    uninstall_env["HERMES_HOME"] = str(home)
    uninstall = Path(__file__).resolve().parents[1] / "scripts" / "uninstall.sh"
    subprocess.run(
        ["bash", str(uninstall)],
        check=True,
        capture_output=True,
        timeout=60,
        env=uninstall_env,
    )

    cfg = (home / "config.yaml").read_text(encoding="utf-8")
    assert "aws-builder" not in cfg
    assert "http://localhost:9999/v1" not in cfg
