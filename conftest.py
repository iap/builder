"""Shared pytest/conftest for the aws-build plugin.

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

PLUGIN_DIR = Path(__file__).resolve().parent
# Plugin lives at <HERMES_HOME>/plugins/aws-build; hermes-agent is at
# <HERMES_HOME>/hermes-agent, i.e. two levels up from PLUGIN_DIR.
HERMES_AGENT_DIR = PLUGIN_DIR.parent.parent / "hermes-agent"

# Make the plugin and hermes-agent importable.
for p in (str(PLUGIN_DIR), str(HERMES_AGENT_DIR)):
    if Path(p).exists() and p not in sys.path:
        sys.path.insert(0, p)

# A throwaway profile so the plugin's get_hermes_home()-based paths resolve.
os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="build-"))


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
