"""Tests for the dependency-advisory-watch osv-scanner wrapper."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import scanner


def _report(*, name="aiohttp", version="3.9.0", vuln_id="TEST-001"):
    return {
        "results": [{
            "packages": [{
                "package": {"name": name, "version": version, "ecosystem": "PyPI"},
                "vulnerabilities": [{
                    "id": vuln_id,
                    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/7.5"}],
                    "affected": [{
                        "package": {"name": name},
                        "ranges": [{"events": [{"introduced": "0"}, {"fixed": "3.9.1"}]}],
                    }],
                }],
            }],
        }],
    }


def test_finds_only_supported_root_lockfiles(tmp_path):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    nested = tmp_path / ".worklink"
    nested.mkdir()
    (nested / "uv.lock").write_text("", encoding="utf-8")
    assert [path.name for path in scanner.find_lockfiles(tmp_path)] == [
        "uv.lock", "package-lock.json"
    ]


def test_osv_scanner_findings_exit_one_is_success(tmp_path):
    lockfile = tmp_path / "package-lock.json"
    lockfile.write_text("{}", encoding="utf-8")
    completed = subprocess.CompletedProcess([], 1, json.dumps(_report()), "")
    with patch("scanner.subprocess.run", return_value=completed) as run:
        assert scanner.run_osv_scanner(lockfile) == _report()
    assert run.call_args.args[0] == [
        scanner.OSV_SCANNER, "scan", "--format", "json", "--lockfile", str(lockfile)
    ]


def test_osv_scanner_real_failure_is_not_clean(tmp_path, monkeypatch, capsys):
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text("", encoding="utf-8")
    monkeypatch.setattr(scanner, "ROOT_DIR", tmp_path)
    completed = subprocess.CompletedProcess([], 2, "", "transport failed")
    with patch("scanner.subprocess.run", return_value=completed):
        assert scanner.run_scan() == 2
    assert "transport failed" in capsys.readouterr().err


def test_invalid_scanner_json_is_not_clean(tmp_path, monkeypatch):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(scanner, "ROOT_DIR", tmp_path)
    completed = subprocess.CompletedProcess([], 0, "not-json", "")
    with patch("scanner.subprocess.run", return_value=completed):
        assert scanner.run_scan() == 2


def test_extracts_normalized_advisory():
    assert scanner.extract_advisories(_report())[0].to_dict() == {
        "package": "aiohttp",
        "current_version": "3.9.0",
        "affected_range": ">=0,<3.9.1",
        "severity": "7.5",
        "advisory_url": "https://osv.dev/vulnerability/TEST-001",
        "remediation_version": "3.9.1",
    }


def test_scan_output_is_deterministic_and_deduplicated(tmp_path, monkeypatch, capsys):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scanner, "ROOT_DIR", tmp_path)
    reports = [_report(name="z-pkg", vuln_id="Z"), _report(name="a-pkg", vuln_id="A")]
    with patch("scanner.run_osv_scanner", side_effect=reports):
        assert scanner.run_scan() == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["package"] for row in rows] == ["a-pkg", "z-pkg"]


def test_clean_scan_emits_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scanner, "ROOT_DIR", tmp_path)
    assert scanner.run_scan() == 0
    assert capsys.readouterr().out == ""
