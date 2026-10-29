"""Make the tracker-poller tests importable and give each a fresh rig."""
from __future__ import annotations

import pytest

from tracker_poller_test_support import Rig


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)
