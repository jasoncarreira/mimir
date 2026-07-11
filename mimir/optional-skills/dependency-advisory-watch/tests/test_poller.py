"""Tests for the dependency-advisory-watch poller."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import scanner


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    sys.modules.pop("poller", None)
    return home


@pytest.fixture
def fresh_poller(tmp_home, monkeypatch):
    sys.modules.pop("poller", None)
    return importlib.import_module("poller")


def _events(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _advisory(package="test-pkg", version="1.0.0", vuln_id="TEST-001"):
    return scanner.Advisory(
        package=package,
        current_version=version,
        affected_range=">=0",
        severity="7.5",
        advisory_url=f"https://osv.dev/vulnerability/{vuln_id}",
        remediation_version="1.0.1",
    )


class TestFirstRunSeed:
    def test_first_run_seeds_cursor_without_emitting_events(self, tmp_home, fresh_poller, monkeypatch, capsys):
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", return_value={"results": []}):
                with patch("scanner.extract_advisories", return_value=[]):
                    assert fresh_poller.main() == 0

        assert _events(capsys) == []

        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        assert cursor_path.exists()
        cursor_data = json.loads(cursor_path.read_text())
        assert cursor_data["advisory_ids"] == []

    def test_first_run_with_advisories_seeds_only(self, tmp_home, fresh_poller, monkeypatch, capsys):
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")
        advisories = [_advisory(vuln_id="VULN-001"), _advisory(vuln_id="VULN-002", package="other-pkg")]

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", return_value={"results": []}):
                with patch("scanner.extract_advisories", return_value=advisories):
                    assert fresh_poller.main() == 0

        events = _events(capsys)
        assert events == []

        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        cursor_data = json.loads(cursor_path.read_text())
        assert set(cursor_data["advisory_ids"]) == {"VULN-001", "VULN-002"}


class TestNewMatch:
    def test_second_run_emits_only_new_advisories(self, tmp_home, fresh_poller, monkeypatch, capsys):
        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        cursor_path.write_text(json.dumps({"advisory_ids": ["VULN-001"], "version": 1}), encoding="utf-8")
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        new_advisories = [
            _advisory(vuln_id="VULN-001"),
            _advisory(vuln_id="VULN-002"),
        ]

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", return_value={"results": []}):
                with patch("scanner.extract_advisories", return_value=new_advisories):
                    assert fresh_poller.main() == 0

        events = _events(capsys)
        assert len(events) == 1
        assert events[0]["advisory_id"] == "VULN-002"
        assert events[0]["package"] == "test-pkg"

    def test_emits_complete_prompt_with_all_fields(self, tmp_home, fresh_poller, monkeypatch, capsys):
        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        cursor_path.write_text(json.dumps({"advisory_ids": [], "version": 1}), encoding="utf-8")
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        advisories = [
            scanner.Advisory(
                package="aiohttp",
                current_version="3.9.0",
                affected_range=">=0,<3.9.1",
                severity="7.5",
                advisory_url="https://osv.dev/vulnerability/TEST-001",
                remediation_version="3.9.1",
            )
        ]

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", return_value={"results": []}):
                with patch("scanner.extract_advisories", return_value=advisories):
                    assert fresh_poller.main() == 0

        events = _events(capsys)
        assert len(events) == 1
        e = events[0]
        assert e["poller"] == "dependency-advisory-watch"
        assert e["event_type"] == "dependency_advisory"
        assert e["advisory_id"] == "TEST-001"
        assert e["package"] == "aiohttp"
        assert e["current_version"] == "3.9.0"
        assert e["affected_range"] == ">=0,<3.9.1"
        assert e["severity"] == "7.5"
        assert e["advisory_url"] == "https://osv.dev/vulnerability/TEST-001"
        assert e["remediation_version"] == "3.9.1"
        assert "aiohttp@3.9.0" in e["prompt"]
        assert "3.9.1" in e["prompt"]


class TestRepeatDeduplication:
    def test_repeated_advisories_are_silent(self, tmp_home, fresh_poller, monkeypatch, capsys):
        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        cursor_path.write_text(json.dumps({"advisory_ids": ["VULN-001", "VULN-002"], "version": 1}), encoding="utf-8")
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        same_advisories = [
            _advisory(vuln_id="VULN-001"),
            _advisory(vuln_id="VULN-002"),
        ]

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", return_value={"results": []}):
                with patch("scanner.extract_advisories", return_value=same_advisories):
                    assert fresh_poller.main() == 0

        events = _events(capsys)
        assert events == []


class TestResolvedReappearing:
    def test_resolved_advisories_removed_from_cursor(self, tmp_home, fresh_poller, monkeypatch, capsys):
        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        cursor_path.write_text(json.dumps({"advisory_ids": ["VULN-001", "VULN-002"], "version": 1}), encoding="utf-8")
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        only_one = [_advisory(vuln_id="VULN-001")]

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", return_value={"results": []}):
                with patch("scanner.extract_advisories", return_value=only_one):
                    assert fresh_poller.main() == 0

        events = _events(capsys)
        assert events == []

        cursor_data = json.loads(cursor_path.read_text())
        assert cursor_data["advisory_ids"] == ["VULN-001"]

    def test_reappearing_advisories_emit_events(self, tmp_home, fresh_poller, monkeypatch, capsys):
        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        cursor_path.write_text(json.dumps({"advisory_ids": ["VULN-001"], "version": 1}), encoding="utf-8")
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        both_advisories = [_advisory(vuln_id="VULN-001"), _advisory(vuln_id="VULN-002")]

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", return_value={"results": []}):
                with patch("scanner.extract_advisories", return_value=both_advisories):
                    assert fresh_poller.main() == 0

        events = _events(capsys)
        assert len(events) == 1
        assert events[0]["advisory_id"] == "VULN-002"


class TestFailureWithoutCursorAdvance:
    def test_scanner_failure_does_not_update_cursor(self, tmp_home, fresh_poller, monkeypatch, capsys):
        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        cursor_path.write_text(json.dumps({"advisory_ids": ["VULN-001"], "version": 1}), encoding="utf-8")
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", side_effect=RuntimeError("scanner failed")):
                assert fresh_poller.main() == 2

        events = _events(capsys)
        assert events == []

        cursor_data = json.loads(cursor_path.read_text())
        assert cursor_data["advisory_ids"] == ["VULN-001"]

    def test_scanner_failure_emits_to_stderr(self, tmp_home, fresh_poller, monkeypatch, capsys):
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", side_effect=RuntimeError("transport error")):
                assert fresh_poller.main() == 2

        captured = capsys.readouterr()
        assert "transport error" in captured.err

    def test_missing_mimir_home_uses_fallback(self, tmp_home, fresh_poller, monkeypatch, capsys):
        monkeypatch.delenv("MIMIR_HOME", raising=False)
        sys.modules.pop("poller", None)

        import poller
        monkeypatch.setattr(poller, "_cursor_path", lambda: None)

        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", return_value={"results": []}):
                with patch("scanner.extract_advisories", return_value=[_advisory()]):
                    assert poller.main() == 0

        events = _events(capsys)
        assert len(events) == 1


class TestCursorAtomicity:
    def test_cursor_update_is_atomic(self, tmp_home, fresh_poller, monkeypatch, capsys):
        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        cursor_path.write_text(json.dumps({"advisory_ids": [], "version": 1}), encoding="utf-8")
        (tmp_home.parent / "uv.lock").write_text("", encoding="utf-8")

        advisories = [_advisory(vuln_id="NEW-001")]

        with patch("scanner.find_lockfiles", return_value=[tmp_home.parent / "uv.lock"]):
            with patch("scanner.run_osv_scanner", return_value={"results": []}):
                with patch("scanner.extract_advisories", return_value=advisories):
                    assert fresh_poller.main() == 0

        temp_file = tmp_home / "dependency-advisory-cursor.json.tmp"
        assert not temp_file.exists()


class TestNoLockfiles:
    def test_no_lockfiles_exits_zero_silently(self, tmp_home, fresh_poller, monkeypatch, capsys):
        with patch("scanner.find_lockfiles", return_value=[]):
            assert fresh_poller.main() == 0

        events = _events(capsys)
        assert events == []

        cursor_path = tmp_home / "dependency-advisory-cursor.json"
        assert cursor_path.exists()
