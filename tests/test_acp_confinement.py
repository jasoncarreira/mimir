from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mimir.acp import confinement


MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")


def test_unavailable_platform_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(confinement.sys, "platform", "unsupported")
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
    result = run_confined(["/bin/cat", str(approved)], cwd, [approved])
    assert result.returncode == 0, result.stderr
    for denied in (outside / "denied", cwd / "escape", Path("/etc/hosts")):
        # Redirect contents even if confinement regresses: never print hosts data.
        result = run_confined(
            ["/bin/sh", "-c", f"/bin/cat {shlex.quote(str(denied))} >/dev/null"],
            cwd, [approved],
        )
        assert result.returncode != 0, f"unexpected read permission: {denied}"
    result = run_confined(
        ["/bin/sh", "-c", f"echo denied > {shlex.quote(str(outside / 'new'))}"], cwd,
    )
    assert result.returncode != 0
    assert not (outside / "new").exists()


@MACOS
def test_directory_addition_is_literal_not_subtree(fixture_scope):
    cwd, outside = fixture_scope
    result = run_confined(["/bin/cat", str(outside / "denied")], cwd, [outside])
    assert result.returncode != 0


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
        [sys.executable, "-c", script], cwd=cwd, approved_paths=[approved],
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
    approved.unlink()
    approved.symlink_to(outside / "denied")
    result = run_confined(["/bin/cat", str(approved)], cwd, [approved])
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


@pytest.fixture
def apparmor(monkeypatch, tmp_path):
    backend = confinement.AppArmorBackend()
    enabled = tmp_path / "enabled"
    enabled.write_text("Y\n")
    tool = tmp_path / "tool"
    tool.write_text("")
    tool.chmod(0o700)
    monkeypatch.setattr(confinement.AppArmorBackend, "enabled", enabled)
    for name in ("parser", "executable", "interpreter"):
        monkeypatch.setattr(confinement.AppArmorBackend, name, tool)
    monkeypatch.setattr(confinement, "_backend", lambda: backend)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = argv[2] + " (enforce)\n" if "-p" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(confinement.subprocess, "run", run)
    return backend, calls, run


@pytest.mark.parametrize("platform, expected", [
    ("linux", confinement.AppArmorBackend), ("darwin", confinement.SeatbeltBackend),
])
def test_backend_platform_selection(monkeypatch, platform, expected):
    monkeypatch.setattr(confinement.sys, "platform", platform)
    assert isinstance(confinement._backend(), expected)


@pytest.mark.parametrize("suffix", [
    '"', "'", " ", "\t", "\n", "\r", "*", "?", "[x]", "{a,b}",
    "\\", "@{HOME}", "#", ",", "\x00", "\udcff", "\u00a0",
])
@pytest.mark.parametrize("source", ["cwd", "approved", "scratch"])
def test_apparmor_refuses_path_syntax(suffix, source):
    values = {"cwd": Path("/session"), "approved": Path("/approved"), "scratch": Path("/scratch")}
    values[source] = Path("/bad" + suffix)
    with pytest.raises(confinement.ConfinementUnavailable, match="represent"):
        confinement.apparmor_profile(values["cwd"], [values["approved"]], [values["scratch"]])


@pytest.mark.parametrize("path", ["/", "relative", "/a/../b"])
def test_apparmor_requires_frozen_nonroot_path(path):
    with pytest.raises(confinement.ConfinementUnavailable):
        confinement.apparmor_profile(Path(path))


def test_apparmor_pure_exact_scope(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("synthesis must not inspect files or start processes")
    with monkeypatch.context() as patch:
        for name in ("resolve", "stat", "read_text", "is_dir", "is_file"):
            patch.setattr(Path, name, forbidden)
        patch.setattr(subprocess, "run", forbidden)
        profile = confinement.apparmor_profile(Path("/session"), [Path("/outside/approved")], [Path("/scratch")])
    assert profile == confinement.apparmor_profile(Path("/session"), [Path("/outside/approved")], [Path("/scratch")])
    writable = {line.strip() for line in profile.splitlines() if " rw" in line}
    assert writable == {
        "/dev/null rw,", "/session rwk,", "/session/ rw,", "/session/** rwk,",
        "/outside/approved rwk,", "/outside/approved/ rw,", "/outside/approved/** rwk,",
        "/scratch rwk,", "/scratch/ rw,", "/scratch/** rwk,",
    }
    assert "ux," not in profile and "px," not in profile and "change_profile" not in profile
    assert "complain" not in profile
    assert "  deny /scratch w," in profile
    assert "  deny /scratch/ w," in profile


@pytest.mark.parametrize("enabled", ["N", "", "yes", "Y (complain)", "\udcff"])
def test_apparmor_lsm_must_be_enabled(apparmor, tmp_path, enabled):
    backend, calls, _ = apparmor
    backend.enabled.write_bytes(enabled.encode("utf-8", errors="surrogateescape"))
    with pytest.raises(confinement.BackendUnavailable):
        backend.prepare(["/bin/true"], cwd=tmp_path)
    assert not calls


@pytest.mark.parametrize("missing", ["enabled", "parser", "executable", "interpreter"])
def test_apparmor_missing_prerequisite(apparmor, tmp_path, monkeypatch, missing):
    backend, calls, _ = apparmor
    monkeypatch.setattr(backend, missing, tmp_path / "missing")
    with pytest.raises(confinement.BackendUnavailable):
        backend.prepare(["/bin/true"], cwd=tmp_path)
    assert not calls


def test_apparmor_executable_permission_required(apparmor, tmp_path):
    backend, calls, _ = apparmor
    backend.parser.chmod(0o600)
    with pytest.raises(confinement.BackendUnavailable):
        backend.prepare(["/bin/true"], cwd=tmp_path)
    assert not calls


@pytest.mark.parametrize("name", ["parser", "executable", "interpreter"])
def test_apparmor_tool_must_be_regular_file(apparmor, monkeypatch, tmp_path, name):
    backend, calls, _ = apparmor
    monkeypatch.setattr(backend, name, tmp_path)
    with pytest.raises(confinement.BackendUnavailable):
        backend.prepare(["/bin/true"], cwd=tmp_path)
    assert not calls


@pytest.mark.parametrize("path", [Path('/bad"'), Path('/bad\udcff')])
def test_apparmor_bad_scope_never_uses_existing_risk_consent(apparmor, tmp_path, path):
    with pytest.raises(confinement.ConfinementUnavailable, match="represent"):
        confinement.prepare_command(["/bin/true"], cwd=tmp_path,
                                    approved_paths=[path], allow_unconfined=True)
    assert not apparmor[1]


def test_apparmor_prepares_only_after_load_and_transition(apparmor, tmp_path):
    backend, calls, _ = apparmor
    approved = tmp_path / "approved"
    prepared = backend.prepare(["/bin/sh", "-c", "exit 37"], cwd=tmp_path, approved_paths=[approved])
    assert prepared.execution_mode == "confined"
    assert len(calls) == 3
    assert calls[0][0][-1] == "--skip-kernel-load"
    assert calls[1][0][-1] == "--replace"
    assert calls[0][1]["input"] == calls[1][1]["input"] == confinement.apparmor_profile(tmp_path, [approved])
    assert all("--config-file=/dev/null" in argv and "--skip-cache" in argv and "--Werror" in argv
               for argv, _ in calls[:2])
    assert all(kwargs["close_fds"] and kwargs["timeout"] == 15 for _, kwargs in calls)
    assert prepared.argv == (*calls[2][0], "/bin/sh", "-c", "exit 37")
    assert calls[2][0][3:9] == ("--", str(backend.interpreter), "-I", "-S", "-c", confinement._APPARMOR_LAUNCH)
    assert prepared.env["TMPDIR"] == str(tmp_path)
    assert not any(str(approved) in arg for argv, _ in calls[:2] for arg in argv)


@pytest.mark.parametrize("code, label", [
    (0, "unconfined"), (0, "{name} (complain)"), (0, "other (enforce)"),
    (1, "{name} (enforce)"), (0, ""), (0, "{name}//other (enforce)"),
])
def test_apparmor_transition_not_inferred_from_successful_load(apparmor, monkeypatch, tmp_path, code, label):
    backend, calls, run = apparmor
    def bad_probe(argv, **kwargs):
        result = run(argv, **kwargs)
        if "-p" in argv:
            return subprocess.CompletedProcess(argv, code, label.format(name=argv[2]), "")
        return result
    monkeypatch.setattr(subprocess, "run", bad_probe)
    with pytest.raises(confinement.BackendUnavailable, match="transition"):
        confinement.prepare_command(["/bin/true"], cwd=tmp_path)
    assert len(calls) == 3
    prepared = confinement.prepare_command(["/bin/true"], cwd=tmp_path, allow_unconfined=True)
    assert prepared.execution_mode == "unconfined" and prepared.argv == ("/bin/true",)


@pytest.mark.parametrize("phase", [0, 1, 2])
@pytest.mark.parametrize("failure", ["status", "oserror", "timeout"])
def test_apparmor_setup_failures_never_silently_execute(apparmor, monkeypatch, tmp_path, phase, failure):
    backend, calls, run = apparmor
    def fail(argv, **kwargs):
        if len(calls) == phase:
            calls.append((argv, kwargs))
            if failure == "oserror":
                raise OSError("fixture")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 15)
            return subprocess.CompletedProcess(argv, 1, "", "fixture")
        return run(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(confinement.ConfinementUnavailable):
        confinement.prepare_command(["/bin/true"], cwd=tmp_path)
    assert len(calls) == phase + 1
    if phase == 0 and failure == "status":
        calls.clear()
        with pytest.raises(confinement.ConfinementUnavailable, match="compilation"):
            confinement.prepare_command(["/bin/true"], cwd=tmp_path, allow_unconfined=True)


@pytest.mark.parametrize("kind", ["cwd", "scratch"])
def test_apparmor_changed_identity_is_not_backend_absence(apparmor, tmp_path, kind):
    backend, calls, _ = apparmor
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    kwargs = {"cwd": alias} if kind == "cwd" else {"cwd": tmp_path, "scratch_paths": [alias]}
    with pytest.raises(confinement.ConfinementUnavailable, match="identity"):
        confinement.prepare_command(["/bin/true"], allow_unconfined=True, **kwargs)
    assert not calls


@pytest.mark.parametrize("kind", ["cwd", "scratch"])
def test_apparmor_missing_directory_is_not_backend_absence(apparmor, tmp_path, kind):
    missing = tmp_path / "missing"
    kwargs = {"cwd": missing} if kind == "cwd" else {"cwd": tmp_path, "scratch_paths": [missing]}
    with pytest.raises(confinement.ConfinementUnavailable, match="identity"):
        confinement.prepare_command(["/bin/true"], allow_unconfined=True, **kwargs)


def test_apparmor_empty_command(apparmor, tmp_path):
    with pytest.raises(ValueError, match="argv"):
        apparmor[0].prepare([], cwd=tmp_path)


@pytest.mark.asyncio
async def test_apparmor_rejected_candidate_is_never_loaded_or_launched(apparmor, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from mimir.acp import hosted

    backend, calls, _ = apparmor
    monkeypatch.setattr(confinement.sys, "platform", "linux")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(candidate, target_is_directory=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    validated = []
    synthesize = confinement.apparmor_profile
    def synth(cwd, approved_paths=(), scratch_paths=()):
        approved_paths = tuple(approved_paths)
        validated.append(approved_paths)
        return synthesize(cwd, approved_paths, scratch_paths)
    monkeypatch.setattr(confinement, "apparmor_profile", synth)
    async def reject(*args):
        assert validated[-1] == (candidate.resolve(),)
        assert all(str(candidate) not in kwargs.get("input", "") for _, kwargs in calls)
        return False
    provider = hosted.HostedHandsProvider(request_scope_permission=AsyncMock(side_effect=reject))
    provider.bind_session("s", cwd)
    session = provider._sessions["s"]
    result = await provider.request_scope(session, str(alias))
    assert not result["approved"]
    provider._request_scope_permission.assert_awaited_once_with("s", str(candidate.resolve()))
    assert candidate.resolve() not in session.scope.approved
    # Exercise the actual execution callsite, stopping at process creation.
    spawn = AsyncMock(side_effect=OSError("fixture: do not run emulated AppArmor"))
    monkeypatch.setattr(hosted.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(hosted.HostedMcpError, match="fixture"):
        await provider._confined_shell(session, "true")
    spawn.assert_awaited_once()
    assert all(str(candidate) not in kwargs.get("input", "") for _, kwargs in calls)
    assert validated[-1] == ()
    await provider.close()


def test_apparmor_validation_loads_nothing(apparmor, monkeypatch, tmp_path):
    monkeypatch.setattr(confinement.sys, "platform", "linux")
    confinement.validate_scope(cwd=tmp_path, candidate_paths=[tmp_path / "candidate"])
    assert not apparmor[1]


@pytest.mark.parametrize("label", ["unconfined", "intended (complain)", "other (enforce)", "intended (enforce)"])
def test_apparmor_actual_launcher_checks_label_before_exec(label, tmp_path):
    # Exercise the actual launcher source in an isolated child, emulating only
    # the kernel label file. This is not an AppArmor enforcement test.
    marker = tmp_path / "ran"
    script = f"""import builtins, io, sys
real_open = builtins.open
builtins.open = lambda path, *a, **kw: io.StringIO({label!r}) if path == '/proc/self/attr/current' else real_open(path, *a, **kw)
sys.argv = ['launcher', 'intended', sys.executable, '-c', {f'from pathlib import Path; Path({str(marker)!r}).touch(); raise SystemExit(37)'!r}]
exec({confinement._APPARMOR_LAUNCH!r})
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script], capture_output=True, timeout=15)
    assert marker.exists() is (label == "intended (enforce)")
    assert result.returncode == (37 if label == "intended (enforce)" else 1)
