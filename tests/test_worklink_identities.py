from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_identity_import(script: str, *, environment: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, **(environment or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_system_account_resolution_is_shared_by_executor_and_handoff() -> None:
    result = _run_identity_import(
        """
import grp
import json
import pwd
from types import SimpleNamespace

uids = {"mimir": 42001, "worklink": 42002}
pwd.getpwnam = lambda name: SimpleNamespace(pw_uid=uids[name])
grp.getgrnam = lambda name: SimpleNamespace(gr_gid={"worklink": 42003}[name])

import mimir.contained_checkout as handoff
import mimir.worklink.checkout as checkout
import mimir.worklink.worker_exec as executor

print(json.dumps({
    "executor": [executor.MIMIR_UID, executor.WORKLINK_UID, executor.WORKLINK_GID],
    "checkout": [checkout.MIMIR_UID, checkout.WORKLINK_GID],
    "handoff": [handoff.MIMIR_UID, handoff.WORKLINK_GID],
}))
""",
        environment={
            "MIMIR_UID": "7",
            "WORKLINK_UID": "8",
            "WORKLINK_GID": "9",
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "executor": [42001, 42002, 42003],
        "checkout": [42001, 42003],
        "handoff": [42001, 42003],
    }


@pytest.mark.parametrize(
    ("missing_kind", "missing_name", "message"),
    [
        ("account", "mimir", "required account 'mimir' is missing"),
        ("account", "worklink", "required account 'worklink' is missing"),
        ("group", "worklink", "required group 'worklink' is missing"),
    ],
)
def test_missing_required_identity_fails_during_import(
    missing_kind: str, missing_name: str, message: str
) -> None:
    result = _run_identity_import(
        f"""
import grp
import pwd
from types import SimpleNamespace

def user(name):
    if {missing_kind!r} == "account" and name == {missing_name!r}:
        raise KeyError(name)
    return SimpleNamespace(pw_uid=42001 if name == "mimir" else 42002)

def group(name):
    if {missing_kind!r} == "group" and name == {missing_name!r}:
        raise KeyError(name)
    return SimpleNamespace(gr_gid=42003)

pwd.getpwnam = user
grp.getgrnam = group
import mimir.worklink.worker_exec
"""
    )

    assert result.returncode != 0
    assert message in result.stderr
