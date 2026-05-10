"""Make ``poller.py`` importable from the sibling skill dir for tests.

The skill ships as a flat directory (``poller.py`` lives next to
``SKILL.md`` and ``pollers.json``), not as a package, so tests need
to inject the parent dir onto ``sys.path`` before they can ``import
poller``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))
