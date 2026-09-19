from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence


_BASE = ["uv", "run"]
_FULL = ["--extra", "dev", "--extra", "bench", "pytest", "-q", "-n", "6"]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        print("usage: run_worklink_incident_verification.py", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parents[1]
    parent_env = dict(os.environ)
    runs = [
        ([*_BASE, "pytest", "-q", "--tb=short"], dict(parent_env)),
        ([*_BASE, *_FULL], {k: v for k, v in parent_env.items() if k != "MIMIR_ACCESS_CONTROL_ENFORCED"}),
        ([*_BASE, *_FULL], {**parent_env, "MIMIR_ACCESS_CONTROL_ENFORCED": "1"}),
    ]
    first_failure = 0
    for command, child_env in runs:
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=child_env,
                shell=False,
                check=False,
            )
        except OSError as exc:
            print(f"could not launch verification command {command!r}: {exc}", file=sys.stderr)
            code = 127
        else:
            code = completed.returncode
            if code < 0:
                code = 128 + abs(code)
        if code and not first_failure:
            first_failure = code
    return first_failure


if __name__ == "__main__":
    raise SystemExit(main())
