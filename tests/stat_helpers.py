from __future__ import annotations

import os


_SEQUENCE_INT_FIELDS = (
    "st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size",
)


def stat_with(result: os.stat_result, **changes: int) -> os.stat_result:
    """Change integer tuple fields, preserving every other stat attribute.

    Reject time-field changes: their integer tuple slots, float attributes and
    nanosecond attributes would otherwise become inconsistent.
    """
    values = list(result)
    for name, value in changes.items():
        values[_SEQUENCE_INT_FIELDS.index(name)] = value
    # The tuple alone loses subsecond times and platform-specific hidden fields
    # (e.g. st_blocks on Linux and st_birthtime on macOS).
    extra = {
        name: getattr(result, name)
        for name in dir(result)
        if name.startswith("st_") and name not in _SEQUENCE_INT_FIELDS
    }
    return os.stat_result(values, extra)
