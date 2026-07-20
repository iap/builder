"""Shared pytest/conftest for the build plugin.

Single source of truth for path resolution so every test + the standalone
verify.py agree on where the plugin and hermes-agent live, and on how to load
the plugin module headlessly. Previously each test file recomputed these paths
independently (with inconsistent parent-depth counts), which broke if the plugin
was ever nested differently.
"""

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

import yaml

PLUGIN_DIR = Path(__file__).resolve().parent
# Plugin lives at <HERMES_HOME>/plugins/build; hermes-agent is at
# <HERMES_HOME>/hermes-agent, i.e. two levels up from PLUGIN_DIR.
HERMES_AGENT_DIR = PLUGIN_DIR.parent.parent / "hermes-agent"

# Make the plugin and hermes-agent importable.
for p in (str(PLUGIN_DIR), str(HERMES_AGENT_DIR)):
    if Path(p).exists() and p not in sys.path:
        sys.path.insert(0, p)

# A throwaway profile so plugin discovery exercises the installed-user-plugin
# path without reading or writing the real Hermes profile.  Discovery scans
# ``HERMES_HOME/plugins`` and user plugins are opt-in, so the isolated profile
# must contain both the plugin link and its enabled-config entry.  Without this,
# tests that import model_tools only prove that an empty profile cannot discover
# build.
TEST_HERMES_HOME = Path(tempfile.mkdtemp(prefix="build-"))
os.environ["HERMES_HOME"] = str(TEST_HERMES_HOME)
(TEST_HERMES_HOME / "plugins").mkdir(parents=True)
(TEST_HERMES_HOME / "plugins" / "build").symlink_to(
    PLUGIN_DIR,
    target_is_directory=True,
)
(TEST_HERMES_HOME / "config.yaml").write_text(
    yaml.safe_dump({"plugins": {"enabled": ["build"]}}),
    encoding="utf-8",
)


def load_plugin(slug: str = "build") -> types.ModuleType:
    """Load the plugin __init__ as a module without installing it."""
    ns = "hermes_plugins"
    if ns not in sys.modules:
        pkg = types.ModuleType(ns)
        pkg.__path__ = []
        pkg.__package__ = ns
        sys.modules[ns] = pkg
    mn = f"{ns}.{slug}"
    spec = importlib.util.spec_from_file_location(
        mn, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = mn
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules[mn] = mod
    spec.loader.exec_module(mod)
    return mod
