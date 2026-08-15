from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "assert_installed_acp", ROOT / ".github" / "assert_installed_acp.py"
)
assert SPEC and SPEC.loader
assert_installed_acp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assert_installed_acp)


def test_native_fixture_uses_unique_pth_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site-packages"
    site.mkdir()
    monkeypatch.setattr(
        assert_installed_acp.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=f"{site}\n"),
    )

    assert_installed_acp._install_native_fixture(
        Path("python"), tmp_path, {}, tmp_path / "ssh"
    )
    assert_installed_acp._install_native_fixture(Path("python"), tmp_path, {})

    modules = sorted(site.glob("_mimir_acp_ci_fixture_*.py"))
    loaders = sorted(site.glob("_mimir_acp_ci_fixture_*.pth"))
    assert len(modules) == len(loaders) == 2
    assert {path.stem for path in modules} == {path.stem for path in loaders}
    assert {path.read_text() for path in loaders} == {
        f"import {path.stem}\n" for path in loaders
    }
    assert not (site / "sitecustomize.py").exists()
    assert sum("SSH_PATH" in path.read_text() for path in modules) == 1


def test_fixture_cleanup_preserves_failure_and_adds_captured_output(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    source = (
        "import pathlib,sys,time; "
        f"pathlib.Path({str(ready)!r}).write_text(''); "
        "sys.stdout.write('server-out\\n'); sys.stdout.flush(); "
        "sys.stderr.write('server-error\\n'); sys.stderr.flush(); "
        "time.sleep(60)"
    )
    original = ValueError("child failed")

    with pytest.raises(ValueError) as caught:
        with assert_installed_acp._fixture_server(
            [sys.executable, "-c", source],
            cwd=tmp_path,
            env=dict(assert_installed_acp.os.environ),
            stop_on_success=False,
        ):
            for _ in range(100):
                if ready.exists():
                    break
                time.sleep(0.01)
            raise original

    assert caught.value is original
    diagnostics = "\n".join(getattr(caught.value, "__notes__", []))
    assert "fixture server stdout: b'server-out\\n'" in diagnostics
    assert "fixture server stderr: b'server-error\\n'" in diagnostics


def test_fixture_cleanup_reports_captured_client_output(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError) as caught:
        with assert_installed_acp._fixture_server(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            env=dict(assert_installed_acp.os.environ),
            stop_on_success=False,
        ):
            raise subprocess.CalledProcessError(
                1, ["mimir", "acp"], output=b"client-out", stderr=b"client-error"
            )

    diagnostics = "\n".join(getattr(caught.value, "__notes__", []))
    assert "client return code: 1" in diagnostics
    assert "client stdout: b'client-out'" in diagnostics
    assert "client stderr: b'client-error'" in diagnostics


def test_fixture_server_failure_reports_captured_output(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as caught:
        with assert_installed_acp._fixture_server(
            [
                sys.executable,
                "-c",
                "import sys; print('out'); print('error', file=sys.stderr); raise SystemExit(7)",
            ],
            cwd=tmp_path,
            env=dict(assert_installed_acp.os.environ),
            stop_on_success=False,
        ):
            pass

    message = str(caught.value)
    assert "fixture server return code: 7" in message
    assert "fixture server stdout: b'out\\n'" in message
    assert "fixture server stderr: b'error\\n'" in message
