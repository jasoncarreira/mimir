from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mimir.acp import confinement
from mimir.acp.execution_scope import ScopeApproval


MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")


def test_unavailable_platform_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(confinement.sys, "platform", "linux")
    with pytest.raises(confinement.ConfinementUnavailable, match="unavailable"):
        confinement.prepare_command(["/bin/sh", "-c", "true"], cwd=tmp_path)


def test_missing_backend_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(confinement.SeatbeltBackend, "executable", tmp_path / "missing")
    with pytest.raises(confinement.ConfinementUnavailable, match="sandbox-exec"):
        confinement.SeatbeltBackend().prepare(["/bin/true"], cwd=tmp_path)


def test_environment_does_not_trust_injected_runtime(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/untrusted.dylib")
    for name in ("ANTHROPIC_API_KEY", "MIMIR_TEST_SECRET", "SSH_AUTH_SOCK", "BASH_ENV", "ENV", "LD_PRELOAD"):
        monkeypatch.setenv(name, "must-not-inherit")
    env = confinement._environment()
    assert "must-not-inherit" not in env.values()
    assert "DYLD_INSERT_LIBRARIES" not in env
    assert env["PYTHONPATH"] != "/untrusted"
    assert env["PYTHONNOUSERSITE"] == "1"


@pytest.fixture
def fixture_scope(tmp_path):
    tmp_path = tmp_path.resolve()
    cwd = tmp_path / 'cwd "quoted" café'
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (cwd / "inside").write_text("fixture")
    (outside / "approved").write_text("fixture")
    (outside / "denied").write_text("fixture")
    (cwd / "escape").symlink_to(outside / "denied")
    return cwd, outside


def run_confined(argv, cwd, approved=()):
    prepared = confinement.prepare_command(argv, cwd=cwd, approved_paths=approved)
    return subprocess.run(
        prepared.argv, cwd=cwd, env=prepared.env, text=True,
        capture_output=True, timeout=15,
    )


@MACOS
def test_shell_scope_exact_file_and_symlink_escape(fixture_scope):
    cwd, outside = fixture_scope
    command = "echo local > created; /bin/cat inside >/dev/null"
    result = run_confined(["/bin/sh", "-c", command], cwd)
    assert result.returncode == 0, result.stderr
    assert (cwd / "created").read_text() == "local\n"
    approved = outside / "approved"
    approval = ScopeApproval(approved, recursive=False)
    result = run_confined(["/bin/cat", str(approved)], cwd, [approval])
    assert result.returncode == 0, result.stderr
    for denied in (outside / "denied", cwd / "escape", Path("/etc/hosts")):
        # Redirect contents even if confinement regresses: never print hosts data.
        result = run_confined(
            ["/bin/sh", "-c", f"/bin/cat {shlex.quote(str(denied))} >/dev/null"],
            cwd, [approval],
        )
        assert result.returncode != 0, f"unexpected read permission: {denied}"
    result = run_confined(
        ["/bin/sh", "-c", f"echo denied > {shlex.quote(str(outside / 'new'))}"], cwd,
    )
    assert result.returncode != 0
    assert not (outside / "new").exists()


@MACOS
def test_directory_addition_grants_subtree_without_escape(fixture_scope):
    cwd, outside = fixture_scope
    directory = outside / "approved-tree"
    nested = directory / "nested"
    nested.mkdir(parents=True)
    existing = nested / "existing"
    existing.write_text("fixture")
    sibling = outside / "approved-tree-sibling"
    sibling.mkdir()
    (sibling / "denied").write_text("fixture")
    escape = nested / "escape"
    escape.symlink_to(outside)
    approval = ScopeApproval(directory, recursive=True)
    created = nested / "new" / "created"
    script = (
        "from pathlib import Path; "
        f"existing = Path({str(existing)!r}); "
        "assert existing.read_text() == 'fixture'; "
        "existing.write_text('updated'); "
        f"created = Path({str(created)!r}); "
        "created.parent.mkdir(); created.write_text('created')"
    )
    result = run_confined([sys.executable, "-c", script], cwd, [approval])
    assert result.returncode == 0, result.stderr
    assert existing.read_text() == "updated"
    assert created.read_text() == "created"
    for denied in (outside / "denied", sibling / "denied", escape / "denied"):
        result = run_confined(["/bin/cat", str(denied)], cwd, [approval])
        assert result.returncode != 0, f"unexpected read permission: {denied}"
        result = run_confined(
            ["/bin/sh", "-c", f"echo changed > {shlex.quote(str(denied))}"],
            cwd, [approval],
        )
        assert result.returncode != 0, f"unexpected write permission: {denied}"
        assert denied.read_text() == "fixture"
    for denied in (outside / "new", sibling / "new", escape / "new"):
        result = run_confined(
            ["/bin/sh", "-c", f"echo denied > {shlex.quote(str(denied))}"],
            cwd, [approval],
        )
        assert result.returncode != 0, f"unexpected create permission: {denied}"
        assert not denied.exists()


@pytest.mark.parametrize("recursive", [False, True], ids=["file-literal", "directory-subpath"])
def test_seatbelt_profile_exact_writable_scope(monkeypatch, fixture_scope, recursive):
    cwd, outside = fixture_scope
    approved = outside / "approved"
    if recursive:
        approved.unlink()
        approved.mkdir()
        (approved / "escape").symlink_to(outside / "denied")
    approval = ScopeApproval(approved, recursive=recursive)
    monkeypatch.setattr(confinement.SeatbeltBackend, "executable", Path(sys.executable))
    prepared = confinement.SeatbeltBackend().prepare(
        ["/bin/true"], cwd=cwd, approved_paths=[approval],
    )
    profile = prepared.argv[2]
    filters = sorted([
        f'(subpath {json.dumps(str(cwd), ensure_ascii=False)})',
        f'({"subpath" if recursive else "literal"} {json.dumps(str(approved), ensure_ascii=False)})',
    ])
    # Exact writable rules exclude parent trees, prefix siblings and escape targets.
    assert [line for line in profile.splitlines() if "file-write" in line] == [
        "(allow file-read* file-write* " + " ".join(filters) + ")",
        '(allow file-write* (literal "/dev/null"))',
    ]


@pytest.mark.parametrize("replacement", ["directory", "symlink-to-directory", "symlink-to-file"])
def test_seatbelt_profile_preserves_file_snapshot(monkeypatch, fixture_scope, replacement):
    cwd, outside = fixture_scope
    approved = outside / "approved"
    assert approved.is_file()
    approval = ScopeApproval(approved.resolve(), recursive=False)
    approved.unlink()
    if replacement == "directory":
        approved.mkdir()
    elif replacement == "symlink-to-directory":
        approved.symlink_to(outside, target_is_directory=True)
    else:
        approved.symlink_to(outside / "denied")
    monkeypatch.setattr(confinement.SeatbeltBackend, "executable", Path(sys.executable))
    prepared = confinement.SeatbeltBackend().prepare(
        ["/bin/true"], cwd=cwd, approved_paths=[approval],
    )
    filters = sorted([
        f'(subpath {json.dumps(str(cwd), ensure_ascii=False)})',
        f'(literal {json.dumps(str(approval.path), ensure_ascii=False)})',
    ])
    assert [line for line in prepared.argv[2].splitlines() if "file-write" in line] == [
        "(allow file-read* file-write* " + " ".join(filters) + ")",
        '(allow file-write* (literal "/dev/null"))',
    ]


@MACOS
def test_persistent_python_native_scope(fixture_scope):
    cwd, outside = fixture_scope
    approved = outside / "approved"
    script = """
import ctypes, json, sys
libc = ctypes.CDLL(None)
count = 0
for line in sys.stdin:
    count += 1
    path = json.loads(line)
    fd = libc.open(path.encode(), 0)
    allowed = fd >= 0
    if allowed:
        libc.close(fd)
    print(json.dumps({'request': count, 'allowed': allowed}), flush=True)
"""
    prepared = confinement.prepare_command(
        [sys.executable, "-c", script], cwd=cwd,
        approved_paths=[ScopeApproval(approved, recursive=False)],
    )
    paths = [cwd / "inside", approved, outside / "denied", cwd / "escape", Path("/etc/hosts")]
    result = subprocess.run(
        prepared.argv, env=prepared.env, cwd=cwd,
        input="".join(json.dumps(str(path)) + "\n" for path in paths),
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        {"request": i + 1, "allowed": allowed}
        for i, allowed in enumerate((True, True, False, False, False))
    ]
    result = run_confined(
        [sys.executable, "-c", "from pathlib import Path; Path('written').write_text('fixture')"], cwd,
    )
    assert result.returncode == 0, result.stderr
    assert (cwd / "written").read_text() == "fixture"


@MACOS
def test_module_runtime_and_private_scratch(fixture_scope, tmp_path):
    cwd, outside = fixture_scope
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    prepared = confinement.prepare_command(
        [sys.executable, "-c", "import mimir.acp.python_kernel; from pathlib import Path; "
         f"Path({str(scratch / 'out')!r}).write_text('fixture')"],
        cwd=cwd, scratch_paths=[scratch],
    )
    result = subprocess.run(prepared.argv, env=prepared.env, cwd=cwd, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert (scratch / "out").read_text() == "fixture"
    # A runtime scratch grant must not imply access to another sibling directory.
    assert run_confined(["/bin/cat", str(outside / "denied")], cwd).returncode != 0


@MACOS
def test_suppressed_denials_are_not_observable(fixture_scope):
    cwd, outside = fixture_scope
    result = run_confined(
        ["/bin/sh", "-c", f"/bin/cat {shlex.quote(str(outside / 'denied'))} 2>/dev/null || true"], cwd,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@MACOS
def test_invalid_profile_does_not_execute(tmp_path):
    marker = tmp_path / "executed"
    result = subprocess.run(
        [str(confinement.SeatbeltBackend.executable), "-p", "(version 1)(unknown-operation)",
         "/usr/bin/touch", str(marker)], capture_output=True, timeout=15,
    )
    assert result.returncode != 0
    assert not marker.exists()


@MACOS
def test_replaced_approved_file_does_not_grant_new_symlink_target(fixture_scope):
    cwd, outside = fixture_scope
    approved = (outside / "approved").resolve()
    approval = ScopeApproval(approved, recursive=False)
    approved.unlink()
    approved.symlink_to(outside / "denied")
    result = run_confined(["/bin/cat", str(approved)], cwd, [approval])
    assert result.returncode != 0


@MACOS
def test_replaced_session_cwd_fails_closed(fixture_scope):
    cwd, outside = fixture_scope
    frozen_cwd = cwd.resolve()
    cwd.rename(cwd.with_name("original-cwd"))
    cwd.symlink_to(outside, target_is_directory=True)
    with pytest.raises(confinement.ConfinementUnavailable, match="changed identity"):
        confinement.prepare_command(["/bin/cat", "denied"], cwd=frozen_cwd)


@MACOS
def test_replaced_scratch_directory_fails_closed(fixture_scope, tmp_path):
    cwd, outside = fixture_scope
    scratch = (tmp_path / "scratch").resolve()
    scratch.mkdir()
    scratch.rmdir()
    scratch.symlink_to(outside, target_is_directory=True)
    with pytest.raises(confinement.ConfinementUnavailable, match="scratch directory changed identity"):
        confinement.prepare_command(["/bin/cat", str(outside / "denied")], cwd=cwd, scratch_paths=[scratch])


@MACOS
def test_scratch_root_cannot_be_removed_by_child(fixture_scope, tmp_path):
    cwd, _ = fixture_scope
    scratch = (tmp_path / "scratch").resolve()
    scratch.mkdir()
    prepared = confinement.prepare_command(
        [sys.executable, "-c", f"import os; os.rmdir({str(scratch)!r})"],
        cwd=cwd, scratch_paths=[scratch],
    )
    result = subprocess.run(prepared.argv, env=prepared.env, cwd=cwd, capture_output=True, timeout=15)
    assert result.returncode != 0
    assert scratch.is_dir()
