import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

_HA = PLUGIN_DIR.parent.parent.parent / "hermes-agent"
if _HA.exists() and str(_HA) not in sys.path:
    sys.path.insert(0, str(_HA))
