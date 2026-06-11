#!/usr/bin/env python3
"""Probe chainlink lock race behavior for WORKLINK claim decisions.

Creates disposable git remotes and chainlink trackers, then races two
independent clones/agents against `chainlink locks claim <issue>`.
The probe is intentionally black-box: it shells out to the installed
`chainlink` binary rather than importing implementation details.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Run:
    code: int
    stdout: str
    stderr: str


def run(cmd: list[str], cwd: Path, *, check: bool = True, timeout: int = 60) -> Run:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    result = Run(proc.returncode, proc.stdout, proc.stderr)
    if check and result.code != 0:
        raise RuntimeError(
            f"command failed ({result.code}) in {cwd}: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def git(cmd: list[str], cwd: Path, *, check: bool = True) -> Run:
    return run(["git", *cmd], cwd, check=check)


def chainlink(binary: str, args: list[str], cwd: Path, *, check: bool = True) -> Run:
    return run([binary, *args], cwd, check=check)


def extract_first_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in output: {text!r}")
    return json.loads(text[start : end + 1])


def init_remote_case(root: Path, binary: str, name: str) -> tuple[Path, Path]:
    case = root / name
    remote = case / "remote.git"
    seed = case / "seed"
    case.mkdir(parents=True)
    git(["init", "--bare", "--initial-branch", "main", str(remote)], case)
    git(["clone", str(remote), str(seed)], case)
    git(["config", "user.email", "probe@example.invalid"], seed)
    git(["config", "user.name", "chainlink lock probe"], seed)
    chainlink(binary, ["init"], seed)
    create = chainlink(
        binary,
        ["issue", "create", "lock race probe", "--priority", "low", "--label", "worklink"],
        seed,
    )
    match = re.search(r"#(\d+)", create.stdout)
    if not match:
        raise RuntimeError(f"could not parse created issue id from {create.stdout!r}")
    issue_id = match.group(1)
    if issue_id != "1":
        raise RuntimeError(f"fresh probe expected issue #1, got #{issue_id}")
    git(["add", "-A"], seed)
    git(["commit", "-m", "init chainlink probe"], seed)
    git(["push", "origin", "main"], seed)
    return case, remote


def clone_agent(case: Path, remote: Path, binary: str, name: str) -> Path:
    clone = case / name
    git(["clone", str(remote), str(clone)], case)
    git(["config", "user.email", f"{name}@example.invalid"], clone)
    git(["config", "user.name", name], clone)
    chainlink(binary, ["agent", "init", f"probe-{name}", "--description", name], clone)
    return clone


def race_once(root: Path, binary: str, idx: int) -> dict[str, Any]:
    case, remote = init_remote_case(root, binary, f"race-{idx:02d}")
    a = clone_agent(case, remote, binary, "a")
    b = clone_agent(case, remote, binary, "b")

    procs: dict[str, subprocess.Popen[str]] = {}
    for name, cwd, branch in (("a", a, "probe-a"), ("b", b, "probe-b")):
        procs[name] = subprocess.Popen(
            [binary, "locks", "claim", "1", "--branch", branch],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    results: dict[str, Run] = {}
    for name, proc in procs.items():
        try:
            stdout, stderr = proc.communicate(timeout=60)
            code = proc.returncode if proc.returncode is not None else -999
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            code = -999
            stderr = stderr + "\nTIMEOUT waiting for chainlink locks claim"
        results[name] = Run(code, stdout, stderr)

    audit = case / "audit"
    git(["clone", str(remote), str(audit)], case)
    chainlink(binary, ["agent", "init", "probe-audit", "--description", "audit"], audit)
    locks_raw = chainlink(binary, ["locks", "list", "--json"], audit, check=False)
    locks = extract_first_json_object(locks_raw.stdout + locks_raw.stderr)
    lock = locks.get("locks", {}).get("1")

    claimed = [name for name, result in results.items() if result.code == 0 and "Claimed lock" in result.stdout]
    already = [name for name, result in results.items() if result.code == 0 and "already hold" in result.stdout]
    ok = len(claimed) == 1 and not already and lock is not None and lock.get("agent_id") == f"probe-{claimed[0]}"
    return {
        "trial": idx,
        "ok": ok,
        "claimed": claimed,
        "already": already,
        "lock": lock,
        "results": {
            name: {"code": r.code, "stdout": r.stdout.strip(), "stderr_tail": r.stderr.strip().splitlines()[-4:]}
            for name, r in results.items()
        },
    }


def stale_steal_probe(root: Path, binary: str) -> dict[str, Any]:
    case, remote = init_remote_case(root, binary, "stale-steal")
    holder = clone_agent(case, remote, binary, "holder")
    thief = clone_agent(case, remote, binary, "thief")
    claim = chainlink(binary, ["locks", "claim", "1", "--branch", "holder-branch"], holder, check=False)
    check = chainlink(binary, ["locks", "check", "1"], thief, check=False)
    steal = chainlink(binary, ["locks", "steal", "1"], thief, check=False)
    after = chainlink(binary, ["locks", "list", "--json"], thief, check=False)
    locks = extract_first_json_object(after.stdout + after.stderr)
    return {
        "claim": {"code": claim.code, "stdout": claim.stdout.strip(), "stderr_tail": claim.stderr.strip().splitlines()[-4:]},
        "check": {"code": check.code, "stdout": check.stdout.strip(), "stderr_tail": check.stderr.strip().splitlines()[-4:]},
        "steal": {"code": steal.code, "stdout": steal.stdout.strip(), "stderr_tail": steal.stderr.strip().splitlines()[-4:]},
        "locks": locks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--chainlink", default=os.environ.get("CHAINLINK", "chainlink"))
    parser.add_argument("--keep", action="store_true", help="keep the temporary probe directory")
    args = parser.parse_args()

    binary = shutil.which(args.chainlink)
    if not binary:
        print(f"chainlink binary not found: {args.chainlink}", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="chainlink-lock-probe-"))
    try:
        races = [race_once(tmp, binary, i) for i in range(1, args.trials + 1)]
        stale = stale_steal_probe(tmp, binary)
        double_claims = [r for r in races if len(r["claimed"]) > 1 or r["already"]]
        failures = [r for r in races if not r["ok"]]
        report = {
            "chainlink": run([binary, "--version"], tmp, check=False).stdout.strip(),
            "trials": args.trials,
            "ok_trials": sum(1 for r in races if r["ok"]),
            "double_claims": len(double_claims),
            "failures": failures,
            "sample_trials": races[:3],
            "stale_steal_probe": stale,
            "tempdir": str(tmp) if args.keep else None,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if not failures and not double_claims else 1
    finally:
        if args.keep:
            print(f"kept tempdir: {tmp}", file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
