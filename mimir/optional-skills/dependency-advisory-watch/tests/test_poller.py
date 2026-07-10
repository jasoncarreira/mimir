from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def fresh_poller(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MIMIR_SCAN_ROOT", str(tmp_path))
    monkeypatch.setenv("POLLER_NAME", "dependency-advisory-watch")
    sys.modules.pop("poller", None)
    return importlib.import_module("poller")


def _events(capsys) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _all_lines(capsys) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _write_uv_lock(root: Path, packages: list[dict[str, str]]) -> None:
    lines = ["version = 1", "revision = 3"]
    for pkg in packages:
        lines.append("")
        lines.append("[[package]]")
        lines.append(f'name = "{pkg["name"]}"')
        lines.append(f'version = "{pkg["version"]}"')
        lines.append('source = { registry = "https://pypi.org/simple" }')
    content = "\n".join(lines)
    (root / "uv.lock").write_text(content, encoding="utf-8")


def _write_package_lock(root: Path, packages: list[dict[str, str]]) -> None:
    pkg_obj = {}
    for pkg in packages:
        key = f"node_modules/{pkg['name']}"
        pkg_obj[key] = {
            "version": pkg["version"],
            "name": pkg["name"],
        }
    content = json.dumps({"packages": pkg_obj}, separators=(",", ":"))
    (root / "package-lock.json").write_text(content, encoding="utf-8")


class FakeResponse:
    def __init__(self, status: int, data: dict) -> None:
        self._status = status
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @property
    def status(self) -> int:
        return self._status

    def read(self) -> bytes:
        return json.dumps(self._data).encode("utf-8")


def _make_vuln_response(vulns: list[dict]) -> FakeResponse:
    return FakeResponse(200, {"vulns": vulns})


@pytest.fixture
def mock_urllib(monkeypatch):
    mock = MagicMock()
    mock.urlopen.return_value = _make_vuln_response([])
    with patch.dict("sys.modules", {"urllib.request": mock, "urllib.error": MagicMock()}):
        yield mock


def test_no_lockfiles_exits_zero_silently(fresh_poller, capsys):
    assert fresh_poller.main() == 0
    assert _events(capsys) == []


def test_uv_lock_inventory_clean(fresh_poller, capsys):
    root = Path(sys.modules["os"].environ["MIMIR_SCAN_ROOT"])
    _write_uv_lock(root, [{"name": "aiohttp", "version": "3.13.5"}])

    with patch("urllib.request.urlopen", create=True) as mock_urlopen:
        mock_urlopen.return_value = _make_vuln_response([])

        assert fresh_poller.main() == 0
        assert _events(capsys) == []


def test_package_lock_inventory_clean(fresh_poller, capsys):
    root = Path(sys.modules["os"].environ["MIMIR_SCAN_ROOT"])
    _write_package_lock(root, [{"name": "react", "version": "19.2.3"}])

    with patch("urllib.request.urlopen", create=True) as mock_urlopen:
        mock_urlopen.return_value = _make_vuln_response([])

        assert fresh_poller.main() == 0
        assert _events(capsys) == []


def test_one_matched_advisory(fresh_poller, capsys):
    root = Path(sys.modules["os"].environ["MIMIR_SCAN_ROOT"])
    _write_uv_lock(root, [{"name": "aiohttp", "version": "3.13.5"}])

    with patch("urllib.request.urlopen", create=True) as mock_urlopen:
        mock_urlopen.return_value = _make_vuln_response(
            [
                {
                    "id": "PYSEC-2024-001",
                    "severity": [{"score": "9.8", "type": "CVSS_V3"}],
                    "affected": [
                        {
                            "ranges": [
                                {
                                    "type": "SEMVER",
                                    "events": [{"introduced": "0"}, {"fixed": "3.11.1"}],
                                }
                            ]
                        }
                    ],
                }
            ]
        )

        assert fresh_poller.main() == 0
        events = _events(capsys)
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == "dependency_advisory"
        assert ev["package_name"] == "aiohttp"
        assert ev["package_version"] == "3.13.5"
        assert ev["ecosystem"] == "PyPI"
        assert ev["severity"] == "9.8"
        assert "PYSEC-2024-001" in ev["advisory_url"]
        assert ev["remediation_version"] == "3.11.1"


def test_malformed_lockfile_emits_signal(fresh_poller, capsys):
    root = Path(sys.modules["os"].environ["MIMIR_SCAN_ROOT"])
    (root / "uv.lock").write_text("invalid content", encoding="utf-8")

    assert fresh_poller.main() == 1
    events = _all_lines(capsys)
    assert any(e.get("signal") == "dependency_advisory_inventory_failed" for e in events)


def test_osv_query_failure_emits_signal(fresh_poller, capsys):
    root = Path(sys.modules["os"].environ["MIMIR_SCAN_ROOT"])
    _write_uv_lock(root, [{"name": "aiohttp", "version": "3.13.5"}])

    with patch("urllib.request.urlopen", create=True) as mock_urlopen:
        mock_urlopen.side_effect = Exception("network error")

        assert fresh_poller.main() == 1
        events = _all_lines(capsys)
        assert any(e.get("signal") == "dependency_advisory_osv_query_failed" for e in events)


def test_deterministic_ordering(fresh_poller, capsys):
    root = Path(sys.modules["os"].environ["MIMIR_SCAN_ROOT"])
    _write_uv_lock(
        root,
        [
            {"name": "requests", "version": "2.31.0"},
            {"name": "aiohttp", "version": "3.13.5"},
            {"name": "charset-normalizer", "version": "3.3.2"},
        ],
    )

    with patch("urllib.request.urlopen", create=True) as mock_urlopen:
        mock_urlopen.return_value = _make_vuln_response(
            [
                {
                    "id": "PYSEC-2024-001",
                    "affected": [{"ranges": [{"type": "SEMVER", "events": [{"fixed": "3.11.1"}]}]}],
                },
            ]
        )

        assert fresh_poller.main() == 0
        events = _events(capsys)

        assert len(events) == 3
        package_names = [e["package_name"] for e in events]
        assert package_names == sorted(package_names)


def test_scoped_directory_excluded(monkeypatch, capsys, tmp_path: Path):
    worklink_dir = tmp_path / ".worklink" / "nested"
    worklink_dir.mkdir(parents=True)
    _write_uv_lock(worklink_dir, [{"name": "aiohttp", "version": "3.13.5"}])

    monkeypatch.setenv("MIMIR_SCAN_ROOT", str(worklink_dir))
    monkeypatch.setenv("POLLER_NAME", "dependency-advisory-watch")
    sys.modules.pop("poller", None)

    import poller

    result = poller.main()
    assert result == 0
    events = _all_lines(capsys)
    assert any(e.get("signal") == "dependency_advisory_scan_skipped" for e in events)


def test_production_subprocess_no_vulnerabilities(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _write_uv_lock(root, [{"name": "nonexistent-package-xyz", "version": "1.0.0"}])

    skill_dir = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        ["python3", "poller.py"],
        cwd=skill_dir,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "MIMIR_SCAN_ROOT": str(root)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
