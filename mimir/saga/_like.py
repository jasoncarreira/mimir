"""SQLite LIKE helpers for saga recall queries."""
from __future__ import annotations


LIKE_ESCAPE = "\\"


def escape_like_pattern(value: str, *, escape: str = LIKE_ESCAPE) -> str:
    """Escape SQLite LIKE metacharacters in a literal search term."""
    return (
        value.replace(escape, escape + escape)
        .replace("%", escape + "%")
        .replace("_", escape + "_")
    )
