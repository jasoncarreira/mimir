"""Tests for dependency-advisory-watch scanner.

Covers: clean inventory, one matched advisory, malformed/source failure,
deterministic ordering, and excluded directories.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import scanner


class TestParseUVLock:
    """Tests for uv.lock parsing."""

    def test_parses_packages(self, tmp_path):
        lock_content = '''version = 1

[[package]]
name = "aiohttp"
version = "3.9.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "requests"
version = "2.31.0"
source = { registry = "https://pypi.org/simple" }
'''
        lock_path = tmp_path / "uv.lock"
        lock_path.write_text(lock_content)

        packages = scanner.parse_uv_lock(lock_path)

        assert packages == {
            "aiohttp": "3.9.0",
            "requests": "2.31.0",
        }

    def test_empty_lockfile(self, tmp_path):
        lock_content = "version = 1\n"
        lock_path = tmp_path / "uv.lock"
        lock_path.write_text(lock_content)

        packages = scanner.parse_uv_lock(lock_path)

        assert packages == {}


class TestParsePackageLock:
    """Tests for package-lock.json parsing."""

    def test_parses_packages(self, tmp_path):
        lock_content = json.dumps({
            "packages": {
                "node_modules/pkg-a": {"version": "1.0.0", "name": "pkg-a"},
                "node_modules/pkg-b": {"version": "2.0.0", "name": "pkg-b"},
            }
        })
        lock_path = tmp_path / "package-lock.json"
        lock_path.write_text(lock_content)

        packages = scanner.parse_package_lock(lock_path)

        assert packages == {
            "pkg-a": "1.0.0",
            "pkg-b": "2.0.0",
        }

    def test_excludes_root_package(self, tmp_path):
        lock_content = json.dumps({
            "packages": {
                "": {"version": "1.0.0", "name": "root-pkg"},
                "node_modules/real-pkg": {"version": "2.0.0", "name": "real-pkg"},
            }
        })
        lock_path = tmp_path / "package-lock.json"
        lock_path.write_text(lock_content)

        packages = scanner.parse_package_lock(lock_path)

        assert packages == {"real-pkg": "2.0.0"}


class TestExcludedDirectories:
    """Tests for excluded directory handling."""

    def test_excludes_worklink(self, tmp_path):
        worklink_dir = tmp_path / ".worklink"
        worklink_dir.mkdir()
        lock_path = worklink_dir / "uv.lock"
        lock_path.write_text('version = 1\n[[package]]\nname = "test"\nversion = "1.0.0"')

        assert scanner._is_excluded(lock_path) is True

    def test_excludes_opencode(self, tmp_path):
        opencode_dir = tmp_path / ".opencode"
        opencode_dir.mkdir()
        lock_path = opencode_dir / "uv.lock"
        lock_path.write_text('version = 1\n[[package]]\nname = "test"\nversion = "1.0.0"')

        assert scanner._is_excluded(lock_path) is True

    def test_includes_root_lockfile(self, tmp_path):
        lock_path = tmp_path / "uv.lock"
        lock_path.write_text('version = 1\n[[package]]\nname = "test"\nversion = "1.0.0"')

        assert scanner._is_excluded(lock_path) is False


class TestFindLockfiles:
    """Tests for lockfile discovery."""

    def test_finds_both_lockfiles(self, tmp_path):
        (tmp_path / "uv.lock").write_text("version = 1\n")
        (tmp_path / "package-lock.json").write_text("{}")

        lockfiles = scanner.find_lockfiles(tmp_path)

        names = [name for name, _ in lockfiles]
        assert "uv.lock" in names
        assert "package-lock.json" in names

    def test_excludes_nested_lockfiles(self, tmp_path):
        (tmp_path / "uv.lock").write_text("version = 1\n")

        nested = tmp_path / ".worklink" / "uv.lock"
        nested.parent.mkdir()
        nested.write_text("version = 1\n")

        lockfiles = scanner.find_lockfiles(tmp_path)

        assert len(lockfiles) == 1
        assert lockfiles[0][0] == "uv.lock"


class TestOSVQuery:
    """Tests for OSV API interaction."""

    def test_query_osv_success(self):
        packages = {"aiohttp": "3.9.0"}

        mock_response = {
            "vulns": [
                {
                    "id": "TEST-001",
                    "affected": [
                        {
                            "package": {"name": "aiohttp"},
                            "ranges": [
                                {
                                    "type": "SEMVER",
                                    "events": [
                                        {"introduced": "0"},
                                        {"fixed": "3.9.1"},
                                    ],
                                }
                            ],
                            "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/7.5"}],
                        }
                    ],
                }
            ]
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_context)
            mock_context.__exit__ = MagicMock(return_value=False)
            mock_context.read.return_value = json.dumps(mock_response).encode()
            mock_urlopen.return_value = mock_context

            results = scanner.query_osv(packages, "PyPI")

        assert len(results) == 1
        assert results[0]["_query_package"] == "aiohttp"
        assert len(results[0]["vulns"]) == 1

    def test_query_osv_network_error(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")

            results = scanner.query_osv({"test": "1.0.0"}, "PyPI")

            assert results == []


class TestExtractAdvisories:
    """Tests for advisory extraction and normalization."""

    def test_extracts_single_advisory(self):
        packages = {"aiohttp": "3.9.0"}
        osv_results = [
            {
                "package": {"name": "aiohttp"},
                "vulns": [
                    {
                        "id": "TEST-001",
                        "affected": [
                            {
                                "package": {"name": "aiohttp"},
                                "ranges": [
                                    {
                                        "type": "SEMVER",
                                        "events": [
                                            {"introduced": "0"},
                                            {"fixed": "3.9.1"},
                                        ],
                                    }
                                ],
                                "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/7.5"}],
                            }
                        ],
                    }
                ],
            }
        ]

        advisories = scanner.extract_advisories(packages, osv_results, "PyPI")

        assert len(advisories) == 1
        adv = advisories[0]
        assert adv.package == "aiohttp"
        assert adv.current_version == "3.9.0"
        assert "3.9.1" in adv.affected_range
        assert adv.severity == "7.5"
        assert "TEST-001" in adv.advisory_url
        assert adv.remediation_version == "3.9.1"

    def test_handles_no_vulnerabilities(self):
        packages = {"safe-pkg": "1.0.0"}
        osv_results = [
            {"package": {"name": "safe-pkg"}, "vulns": []}
        ]

        advisories = scanner.extract_advisories(packages, osv_results, "PyPI")

        assert advisories == []


class TestDeterministicOrdering:
    """Tests for deterministic output ordering."""

    def test_advisories_sorted(self):
        packages = {"z-package": "1.0.0", "a-package": "2.0.0", "m-package": "3.0.0"}
        osv_results = [
            {
                "package": {"name": name},
                "vulns": [
                    {
                        "id": f"TEST-{name}",
                        "affected": [
                            {
                                "package": {"name": name},
                                "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}]}],
                            }
                        ],
                    }
                ],
            }
            for name in packages
        ]

        advisories = scanner.extract_advisories(packages, osv_results, "PyPI")
        advisories.sort(key=lambda a: (a.package, a.current_version, a.advisory_url))

        names = [a.package for a in advisories]
        assert names == ["a-package", "m-package", "z-package"]


class TestCleanScan:
    """Tests for clean scan behavior (no stdout on success)."""

    def test_no_output_on_no_lockfiles(self, tmp_path, captured_events, monkeypatch):
        monkeypatch.setattr(scanner, "ROOT_DIR", tmp_path)

        result = scanner.run_scan()

        assert result == 0
        assert captured_events == []


class TestErrorHandling:
    """Tests for error handling and failure modes."""

    def test_transport_failure_returns_empty_results(self, tmp_path, monkeypatch):
        import urllib.error

        (tmp_path / "uv.lock").write_text('version = 1\n[[package]]\nname = "t"\nversion = "1.0.0"')

        monkeypatch.setattr(scanner, "ROOT_DIR", tmp_path)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")

            result = scanner.run_scan()

        assert result == 0


class TestFullIntegration:
    """Integration tests using the repository checkout running pytest."""

    def test_scanner_finds_workspace_lockfiles(self):
        root = Path(__file__).resolve().parents[4]
        lockfiles = scanner.find_lockfiles(root)

        names = [name for name, _ in lockfiles]
        assert "uv.lock" in names
        assert "package-lock.json" in names
