"""Unit tests for ``mimir.config`` folder/permission wiring."""

from __future__ import annotations

import pytest

from mimir.config import DEFAULT_FOLDERS, _parse_folders


class TestParseFolders:
    def test_empty_returns_default(self) -> None:
        assert _parse_folders("") == DEFAULT_FOLDERS
        assert _parse_folders("   ") == DEFAULT_FOLDERS

    def test_only_invalid_pairs_returns_default(self) -> None:
        # No `:` separator, nothing parseable → fall through to defaults
        # rather than handing the agent an empty folders dict (which
        # would imply zero writable roots — too easy to footgun).
        assert _parse_folders(",,,") == DEFAULT_FOLDERS
        assert _parse_folders("garbage,more-garbage") == DEFAULT_FOLDERS

    def test_rejects_dot_and_dotdot_names(self, caplog: pytest.LogCaptureFixture) -> None:
        # Pre-fix ``.:rw`` aliased ``/`` and made EVERY directory
        # writable. Now rejected with a warning.
        with caplog.at_level("WARNING"):
            out = _parse_folders(".:rw,..:rw,state:rw")
        assert out == {"state": "rw"}
        assert any(".:rw" in r.message or "..:rw" in r.message for r in caplog.records)

    def test_rejects_path_separators(self, caplog: pytest.LogCaptureFixture) -> None:
        # ``a/b`` implies a nested writable root — WriteGuardBackend
        # wouldn't honor it cleanly anyway. Reject + log.
        with caplog.at_level("WARNING"):
            out = _parse_folders("foo/bar:rw,state:rw")
        assert out == {"state": "rw"}
        assert any("foo/bar" in r.message for r in caplog.records)

    def test_unknown_mode_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            out = _parse_folders("state:weird")
        assert out == {"state": "ro"}
        assert any("weird" in r.message for r in caplog.records)

    def test_malformed_logs_warning_and_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Pairs are present but every one is rejected → log + default.
        with caplog.at_level("WARNING"):
            out = _parse_folders(".:rw,..:rw")
        assert out == DEFAULT_FOLDERS
        assert any("no valid folders" in r.message for r in caplog.records)

    def test_explicit_override(self) -> None:
        out = _parse_folders("state:rw,logs:ro")
        assert out == {"state": "rw", "logs": "ro"}

    def test_unknown_mode_coerces_to_ro(self) -> None:
        # Fail safe: any non-rw/ro mode is treated as ro so a typo
        # doesn't accidentally widen write permissions.
        out = _parse_folders("state:weird,memory:RW,logs:WRITE")
        assert out == {"state": "ro", "memory": "rw", "logs": "ro"}

    def test_strips_whitespace_and_slashes(self) -> None:
        out = _parse_folders(" /state/ : rw , memory : RW ")
        assert out == {"state": "rw", "memory": "rw"}

    def test_skips_empty_names(self) -> None:
        out = _parse_folders(":rw,state:rw,/:ro")
        assert out == {"state": "rw"}


def test_writable_dirs_preserves_insertion_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.setenv("MIMIR_FOLDERS", "alpha:rw,beta:ro,gamma:rw")
    cfg = Config.from_env()
    assert cfg.writable_dirs == ["alpha", "gamma"]
    assert cfg.all_dirs == ["alpha", "beta", "gamma"]


def test_from_env_uses_mimir_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.setenv("MIMIR_FOLDERS", "state:rw,memory:rw,logs:ro")
    cfg = Config.from_env()
    assert cfg.folders == {"state": "rw", "memory": "rw", "logs": "ro"}
    assert cfg.writable_dirs == ["state", "memory"]
    assert cfg.all_dirs == ["state", "memory", "logs"]


def test_from_env_default_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.delenv("MIMIR_FOLDERS", raising=False)
    cfg = Config.from_env()
    assert cfg.folders == DEFAULT_FOLDERS
    # state, memory, attachments, skills are the four rw defaults
    assert set(cfg.writable_dirs) == {"state", "memory", "attachments", "skills"}
