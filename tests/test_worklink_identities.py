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


def test_worklink_imports_do_not_resolve_system_accounts() -> None:
    result = _run_identity_import(
        """
import grp
import pwd

pwd.getpwnam = lambda name: (_ for _ in ()).throw(AssertionError(name))
grp.getgrnam = lambda name: (_ for _ in ()).throw(AssertionError(name))

import mimir.contained_checkout
import mimir.project_tests
import mimir.worklink.checkout
import mimir.worklink.worker_exec
print("safe")
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "safe"


def test_system_account_resolution_is_cached_and_shared_by_consumers() -> None:
    result = _run_identity_import(
        """
import grp
import json
import pwd
from types import SimpleNamespace

calls = []
uids = {"mimir": 42001, "worklink": 42002}
def user(name):
    calls.append(["user", name])
    return SimpleNamespace(pw_uid=uids[name])
def group(name):
    calls.append(["group", name])
    return SimpleNamespace(gr_gid={"worklink": 42003}[name])
pwd.getpwnam = user
grp.getgrnam = group

import mimir.contained_checkout as handoff
import mimir.worklink.checkout as checkout
import mimir.worklink.identities as identities
import mimir.worklink.worker_exec as executor

values = [
    identities.get_identities(),
    handoff.get_identities(),
    checkout.get_identities(),
    executor.get_identities(),
]
print(json.dumps({
    "values": [[value.mimir_uid, value.worklink_uid, value.worklink_gid] for value in values],
    "same": all(value is values[0] for value in values),
    "calls": calls,
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
        "values": [[42001, 42002, 42003]] * 4,
        "same": True,
        "calls": [["user", "mimir"], ["user", "worklink"], ["group", "worklink"]],
    }


@pytest.mark.parametrize(
    ("missing_kind", "missing_name", "message"),
    [
        ("account", "mimir", "required account 'mimir' is missing"),
        ("account", "worklink", "required account 'worklink' is missing"),
        ("group", "worklink", "required group 'worklink' is missing"),
    ],
)
def test_missing_required_identity_fails_on_first_containment_use(
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
from mimir.worklink.identities import get_identities
get_identities()
"""
    )

    assert result.returncode != 0
    assert message in result.stderr
