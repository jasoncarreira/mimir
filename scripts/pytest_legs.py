#!/usr/bin/env python3
"""Run named pytest legs, one child process each, under the CI environment.

Usage: ``pytest_legs.py LEG [LEG ...]`` where each LEG is ``SELECTION:PATH[,PATH...]``
or ``skills:all``. SELECTION is one of:

* ``core``: ``MIMIR_PLUGINS`` unset, enforcement unset
* ``cultivate``: ``MIMIR_PLUGINS=cultivate``, enforcement unset
* ``core-enforced``: ``MIMIR_PLUGINS`` unset, ``MIMIR_ACCESS_CONTROL_ENFORCED=1``
* ``cultivate-enforced``: ``MIMIR_PLUGINS=cultivate``, ``MIMIR_ACCESS_CONTROL_ENFORCED=1``

``skills:all`` mirrors the CI loop: one process per optional-skill test directory that
contains a ``test_*.py``, with plugins and enforcement unset.

Legs run serially and the first nonzero exit is returned (pytest's exit 5, "no tests
collected", counts as a failure). A malformed leg, an unknown selection, or a path that
is missing or outside the repository exits 64 before anything runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

EXIT_USAGE = 64
ROOT = Path(__file__).resolve().parent.parent
SKILL_TEST_GLOBS = (
    "mimir/optional-skills/*/tests",
    "plugins/cultivate/mimir_cultivate/defaults/skills/*/tests",
)
# selection -> (MIMIR_PLUGINS value or None, enforced)
SELECTIONS: Mapping[str, tuple[str | None, bool]] = {
    "core": (None, False),
    "cultivate": ("cultivate", False),
    "core-enforced": (None, True),
    "cultivate-enforced": ("cultivate", True),
}
_PLUGINS = "MIMIR_PLUGINS"
_ENFORCED = "MIMIR_ACCESS_CONTROL_ENFORCED"


class LegError(ValueError):
    """A leg argument that cannot be run."""


@dataclass(frozen=True)
class Leg:
    selection: str
    paths: tuple[str, ...]


def parse_leg(text: str, root: Path) -> Leg:
    selection, separator, rest = text.partition(":")
    if not separator:
        raise LegError(f"malformed leg {text!r}: expected SELECTION:PATH[,PATH...]")
    if selection == "skills":
        if rest != "all":
            raise LegError(f"malformed leg {text!r}: only skills:all is defined")
        return Leg("skills", ())
    if selection not in SELECTIONS:
        raise LegError(f"unknown selection {selection!r} in leg {text!r}")
    names = rest.split(",")
    if not rest or any(not name for name in names):
        raise LegError(f"malformed leg {text!r}: empty path")
    resolved_root = root.resolve()
    for name in names:
        if name.startswith("-"):
            raise LegError(f"malformed leg {text!r}: path {name!r} looks like an option")
        target = (resolved_root / name).resolve()
        if not target.is_relative_to(resolved_root):
            raise LegError(f"path {name!r} is outside the repository")
        if not target.exists():
            raise LegError(f"path {name!r} does not exist")
    return Leg(selection, tuple(names))


def leg_environment(selection: str, base: Mapping[str, str]) -> dict[str, str]:
    env = {key: value for key, value in base.items() if key not in {_PLUGINS, _ENFORCED}}
    if selection == "skills":
        return env
    plugins, enforced = SELECTIONS[selection]
    if plugins is not None:
        env[_PLUGINS] = plugins
    if enforced:
        env[_ENFORCED] = "1"
    return env


def skill_test_dirs(root: Path) -> list[str]:
    found = []
    for pattern in SKILL_TEST_GLOBS:
        for directory in sorted(root.glob(pattern)):
            if directory.is_dir() and any(directory.glob("test_*.py")):
                found.append(directory.relative_to(root).as_posix())
    return found


def _exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def run_leg(
    leg: Leg, root: Path, base_env: Mapping[str, str],
    runner: Callable[..., "subprocess.CompletedProcess[bytes]"],
) -> int:
    env = leg_environment(leg.selection, base_env)
    groups = [(path,) for path in skill_test_dirs(root)] if leg.selection == "skills" else [leg.paths]
    if not groups:
        print("pytest_legs: no optional-skills test directories matched", file=sys.stderr)
        return 1
    for paths in groups:
        print(f"== {leg.selection}: {' '.join(paths)} ==", flush=True)
        completed = runner(
            [sys.executable, "-m", "pytest", "-q", "--tb=short", *paths], cwd=root, env=env,
        )
        status = _exit_status(completed.returncode)
        if status != 0:
            return status
    return 0


def main(
    argv: Sequence[str] | None = None, *, root: Path = ROOT,
    base_env: Mapping[str, str] | None = None,
    runner: Callable[..., "subprocess.CompletedProcess[bytes]"] = subprocess.run,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__, file=sys.stderr)
        return EXIT_USAGE
    try:
        legs = [parse_leg(arg, root) for arg in args]
    except LegError as exc:
        print(f"pytest_legs: {exc}", file=sys.stderr)
        return EXIT_USAGE
    environment = os.environ if base_env is None else base_env
    for leg in legs:
        status = run_leg(leg, root, environment, runner)
        if status != 0:
            return status
    return 0


if __name__ == "__main__":
    sys.exit(main())
