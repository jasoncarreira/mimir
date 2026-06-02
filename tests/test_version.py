"""``mimir.__version__`` tracks the installed distribution (chainlink #345).

Regression guard for the stale-version bug: ``mimir/__init__.py`` used to
hardcode ``__version__ = "0.2.3"`` and never bump it, so ``version_check`` (which
defaults ``current_version`` to ``mimir.__version__``) reported a false
"update available" even when the installed package was current.
"""

from __future__ import annotations

from importlib.metadata import version

import mimir


def test_version_matches_installed_dist() -> None:
    assert mimir.__version__ == version("mimir-agent")
    # A real numeric version in the test env (mimir-agent is installed) — not
    # the old hardcoded literal and not the no-install fallback.
    assert mimir.__version__[0].isdigit()
    assert "unknown" not in mimir.__version__
