"""Tests for mimir._atomic:atomic_write_json (chainlink #239).

The canonical CR#7 contract — fsync file + fsync parent dir — is now
shared across oauth_usage_poller, rate_limits, and quota_pause. These
tests pin the contract in one place so a future "simplify the helper"
refactor that drops fsync gets caught.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from mimir._atomic import atomic_write_json


class TestBasicWrite:
    def test_writes_payload(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        atomic_write_json(path, {"a": 1, "b": [1, 2, 3]})
        assert json.loads(path.read_text()) == {"a": 1, "b": [1, 2, 3]}

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        path.write_text('{"old": true}')
        atomic_write_json(path, {"new": True})
        assert json.loads(path.read_text()) == {"new": True}

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deep" / "out.json"
        atomic_write_json(path, {"ok": True})
        assert path.exists()
        assert json.loads(path.read_text()) == {"ok": True}

    def test_default_mode_is_0o600(self, tmp_path: Path) -> None:
        """Sidecar files (refresh tokens, rate-limit state) should not be
        world-readable. Default mode pins this."""
        path = tmp_path / "out.json"
        atomic_write_json(path, {"secret": "value"})
        actual = stat.S_IMODE(path.stat().st_mode)
        assert actual == 0o600, f"mode is {oct(actual)}, expected 0o600"

    def test_custom_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        atomic_write_json(path, {"ok": True}, mode=0o644)
        actual = stat.S_IMODE(path.stat().st_mode)
        assert actual == 0o644

    def test_indent_none_compacts(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        atomic_write_json(path, {"a": 1}, indent=None)
        assert path.read_text() == '{"a": 1}'

    def test_serializes_path_via_default_str(self, tmp_path: Path) -> None:
        """``default=str`` is set on json.dumps so caller dicts holding
        Path / datetime survive without an explicit conversion pass."""
        path = tmp_path / "out.json"
        payload = {"home": Path("/tmp/home")}
        atomic_write_json(path, payload)
        data = json.loads(path.read_text())
        assert data["home"] == "/tmp/home"


class TestDurabilityContract:
    """Pin the CR#7 invariant: fsync file + parent dir."""

    def test_calls_fsync_on_file(self, tmp_path: Path) -> None:
        with patch("mimir._atomic.os.fsync") as mock_fsync:
            atomic_write_json(tmp_path / "out.json", {"ok": True})
        assert mock_fsync.call_count >= 1, (
            "atomic_write_json must fsync the file before rename — "
            "CR#7 invariant. A non-fsynced rename can revert across a crash."
        )

    def test_calls_fsync_on_parent_dir(self, tmp_path: Path) -> None:
        """Two fsyncs: one for the temp file, one for the parent dir
        (so the rename itself is durable)."""
        fsync_targets: list[int] = []
        real_fsync = os.fsync

        def _wrap_fsync(fd: int) -> None:
            fsync_targets.append(fd)
            real_fsync(fd)

        with patch("mimir._atomic.os.fsync", side_effect=_wrap_fsync):
            atomic_write_json(tmp_path / "out.json", {"ok": True})

        # Expect at least 2 fsyncs (file + parent dir).
        assert len(fsync_targets) >= 2, (
            f"got {len(fsync_targets)} fsyncs, expected ≥2 "
            f"(file + parent dir per CR#7)"
        )

    def test_parent_dir_fsync_failure_is_tolerated(self, tmp_path: Path) -> None:
        """Windows + some network FS reject O_RDONLY on directories. The
        rename is still atomic from userspace; the parent-dir fsync is
        defense-in-depth, not load-bearing for correctness."""
        path = tmp_path / "out.json"

        real_open = os.open
        real_fsync = os.fsync

        def _open_raises_on_dir(p, flags, *a, **kw):
            try:
                is_dir = os.path.isdir(p)
            except TypeError:
                is_dir = False
            if is_dir:
                raise OSError("simulated platform restriction")
            return real_open(p, flags, *a, **kw)

        with patch("mimir._atomic.os.open", side_effect=_open_raises_on_dir):
            atomic_write_json(path, {"ok": True})

        # File still got written despite the parent-dir fsync failing.
        assert path.exists()
        assert json.loads(path.read_text()) == {"ok": True}


class TestFailureSemantics:
    def test_temp_file_removed_on_write_failure(self, tmp_path: Path) -> None:
        """If anything between mkstemp and rename fails, the temp file
        must not litter the directory."""

        with patch("mimir._atomic.os.write", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                atomic_write_json(tmp_path / "out.json", {"ok": True})

        leftovers = list(tmp_path.glob("out.json.*.tmp"))
        assert leftovers == [], (
            f"temp files leaked after failure: {leftovers}"
        )

    def test_temp_file_removed_on_replace_failure(self, tmp_path: Path) -> None:
        with patch("mimir._atomic.os.replace", side_effect=OSError("perm denied")):
            with pytest.raises(OSError, match="perm denied"):
                atomic_write_json(tmp_path / "out.json", {"ok": True})

        leftovers = list(tmp_path.glob("out.json.*.tmp"))
        assert leftovers == []

    def test_destination_unchanged_on_failure(self, tmp_path: Path) -> None:
        """The CR#7 contract's core promise: either the new write
        succeeded or the old file is intact. No half-state."""
        path = tmp_path / "out.json"
        path.write_text('{"old": "intact"}')

        with patch("mimir._atomic.os.write", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                atomic_write_json(path, {"new": "value"})

        assert json.loads(path.read_text()) == {"old": "intact"}, (
            "destination must remain at the prior state on failure"
        )
