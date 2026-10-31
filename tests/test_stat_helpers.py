from __future__ import annotations

import os

import pytest

from tests.stat_helpers import stat_with


@pytest.mark.parametrize(
    "changed_field",
    [None, "st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"],
)
def test_stat_with_preserves_every_stat_attribute(tmp_path, changed_field):
    path = tmp_path / "sample"
    path.write_bytes(b"stat helper regression\n")
    # Use fractional timestamps so rebuilding just the visible tuple cannot
    # accidentally pass on a filesystem whose initial timestamps are integral.
    os.utime(path, ns=(1_700_000_000_123_456_789, 1_700_000_001_987_654_321))
    original = os.stat(path)
    changes = {} if changed_field is None else {changed_field: getattr(original, changed_field) + 1}

    changed = stat_with(original, **changes)

    assert isinstance(changed, os.stat_result)
    for name in dir(original):
        if name.startswith("st_"):
            assert getattr(changed, name) == changes.get(name, getattr(original, name)), name


@pytest.mark.parametrize(
    "field",
    ["st_atime", "st_mtime", "st_ctime", "st_atime_ns", "st_mtime_ns", "st_ctime_ns", "unknown"],
)
def test_stat_with_rejects_time_and_unknown_changes(tmp_path, field):
    original = os.stat(tmp_path)
    with pytest.raises(ValueError):
        stat_with(original, **{field: 1})
