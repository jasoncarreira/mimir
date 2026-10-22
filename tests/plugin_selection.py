"""The one test-side reading of the process plugin selection.

Provider-dependent tests skip or select on this rather than comparing
``MIMIR_PLUGINS`` to a literal, so ``all`` and comma-separated selections run
the same coverage as ``cultivate`` alone.
"""

from __future__ import annotations

import os


def cultivate_selected() -> bool:
    """Whether this process selects Cultivate, parsed the way discovery parses it."""
    from mimir.plugins.discovery import _selection

    all_enabled, names = _selection(os.environ.get("MIMIR_PLUGINS", ""))
    return all_enabled or "cultivate" in names
