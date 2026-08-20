from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_MODULE_NAME = "_github_poller_under_test"
_POLLER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "poller.py"
_SPEC = importlib.util.spec_from_file_location(_MODULE_NAME, _POLLER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
poller = importlib.util.module_from_spec(_SPEC)
sys.modules[_MODULE_NAME] = poller
_SPEC.loader.exec_module(poller)
