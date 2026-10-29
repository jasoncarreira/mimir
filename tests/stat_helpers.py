from __future__ import annotations

import os


_STAT_FIELDS = (
    "st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid",
    "st_size", "st_atime", "st_mtime", "st_ctime",
)


def stat_with(result: os.stat_result, **changes: int) -> os.stat_result:
    """Change selected stat fields while retaining a complete stat result."""
    values = list(result)
    for name, value in changes.items():
        values[_STAT_FIELDS.index(name)] = value
    return os.stat_result(values)
