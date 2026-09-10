"""Syntax checks catch compilation failures that block Hands without proving enforcement."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from mimir.acp.confinement import apparmor_profile
from mimir.acp.execution_scope import ScopeApproval


@pytest.fixture(params=[
    pytest.param(("/work/session", [
        ScopeApproval(Path("/approved/tree"), recursive=True),
        ScopeApproval(Path("/approved/file.txt"), recursive=False),
        ScopeApproval(Path("/other/tree"), recursive=True),
    ], [], ["/work/session/** rwk,", "/approved/tree/** rwk,",
            "/approved/file.txt rwk,", "/other/tree/** rwk,"]), id="several-approved-paths"),
    # Path normalizes repeated separators before synthesis, as in production.
    pytest.param(("/work///a+b-c.d//project", [], [],
                  ["/work/a+b-c.d/project/** rwk,"]), id="punctuation-repeated-separators"),
    pytest.param(("/" + "/".join(["nested"] * 40), [], [],
                  ["/" + "/".join(["nested"] * 40) + "/** rwk,"]), id="deeply-nested"),
    pytest.param(("/session", [], [], ["/session rwk,", "/session/ rw,",
                                          "/session/** rwk,"]), id="root-depth"),
    pytest.param(("/session", [], [Path("/session/capture"), Path("/scratch")], [
        "/session/capture/** rwk,", "deny /session/capture w,", "deny /session/capture/ w,",
        "/scratch/** rwk,", "deny /scratch w,", "deny /scratch/ w,",
    ]), id="scratch-rules"),
    pytest.param(("/session", [], [], [
        "/** ix,", "/usr/bin/** mr,", "/bin/** mr,", "/usr/lib/** mr,",
        "/usr/lib64/** mr,", "/lib/** mr,", "/lib64/** mr,", "/usr/local/lib/** mr,",
        "/etc/ld.so.cache r,", "/dev/null rw,", "/dev/urandom r,", "/dev/random r,",
        "/proc/*/attr/current r,",
    ]), id="runtime-read-allowances"),
])
def generated_profile(request):
    cwd, approved, scratch, expected = request.param
    return apparmor_profile(Path(cwd), approved, scratch), expected


def test_generated_profile_shape(generated_profile):
    profile, expected = generated_profile
    assert set(expected) <= {line.strip() for line in profile.splitlines()}


@pytest.fixture(scope="module")
def parser_command():
    parser = shutil.which("apparmor_parser")
    if parser is None:
        # Administrative binaries need not be on an unprivileged user's PATH.
        for directory in ("/usr/sbin", "/sbin"):
            parser = shutil.which("apparmor_parser", path=directory)
            if parser is not None:
                break
    if parser is None:
        pytest.skip("apparmor_parser is not installed; real AppArmor syntax checking requires it")
    help_result = subprocess.run(
        [parser, "--help"], capture_output=True, text=True, timeout=15,
        env={**os.environ, "LC_ALL": "C"},
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--skip-kernel-load" in help_result.stdout + help_result.stderr
    # Ignore host configuration and caches; never load or replace a kernel profile.
    return [parser, "--config-file=/dev/null", "--skip-cache", "--skip-kernel-load"]


def test_parser_accepts_generated_profile(generated_profile, parser_command):
    profile, _ = generated_profile
    result = subprocess.run(
        parser_command, input=profile, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_parser_rejects_unterminated_rule(parser_command):
    profile = apparmor_profile(Path("/session"))
    assert "  /session/** rwk,\n" in profile
    corrupted = profile.replace("  /session/** rwk,\n", "  /session/** rwk\n")
    assert corrupted != profile
    result = subprocess.run(
        parser_command, input=corrupted, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode != 0, "parser accepted a rule with its trailing comma removed"
