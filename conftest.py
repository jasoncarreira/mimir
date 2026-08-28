"""Repository-wide pytest collection policy."""

from __future__ import annotations

import pytest
from _pytest.mark.structures import NOTSET


def pytest_itemcollected(item: pytest.Item) -> None:
    """Reject parametrizations that would otherwise become vacuous skips."""
    callspec = getattr(item, "callspec", None)
    if callspec is None or item.get_closest_marker("allow_empty_parametrize"):
        return

    empty_argnames = [
        argname for argname, value in callspec.params.items() if value is NOTSET
    ]
    if empty_argnames:
        test_id = item.nodeid
        if test_id.startswith("::"):
            test_id = f"{item.path.name}{test_id}"
        raise pytest.UsageError(
            f"empty parameter set for {test_id}; "
            f"parametrize argnames: {', '.join(empty_argnames)}"
        )
