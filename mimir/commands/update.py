"""Update subcommand — ``mimir update [--check] [--apply] [--pre]``.

Extracted from ``mimir.cli`` (Phase 3, chainlink #240).
Checks PyPI for a newer mimir release and optionally installs it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def add_argparse(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Register ``mimir update`` subcommand.  Returns the created parser."""
    update_p = sub.add_parser(
        "update",
        help="Check PyPI for a newer mimir release; with --apply, install it via pip.",
    )
    update_p.add_argument(
        "--check",
        action="store_true",
        help=(
            "Print status only; exit non-zero when an update is available "
            "(useful in scripts / CI)."
        ),
    )
    update_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "If a newer version is on PyPI, run `python -m pip install --upgrade mimir`. "
            "You must restart the agent (e.g., `docker compose restart`) to engage "
            "the new code."
        ),
    )
    update_p.add_argument(
        "--pre",
        action="store_true",
        help="Include pre-release versions (alpha / beta / rc / dev) when checking.",
    )
    return update_p


def dispatch(args: argparse.Namespace) -> int:
    """``mimir update`` — PyPI version-check + optional install.

    Three modes:
    - default: print current vs latest, exit 0 either way
    - ``--check``: same status print, but exit 1 if an update is
      available (useful in scripts / CI gates)
    - ``--apply``: if newer version available, run
      ``python -m pip install --upgrade mimir``. After install,
      print a clear "restart agent" message — the running process
      doesn't auto-pick up the new install.

    ``--pre`` flag opts into pre-release surfacing (alpha / beta / rc
    / dev). Default excludes them — stable releases only.
    """
    from ..version_check import check_for_update, _pypi_package_name

    pkg = _pypi_package_name()
    result = check_for_update(include_prereleases=args.pre)

    if result.error_msg:
        print(f"Update check failed: {result.error_msg}", file=sys.stderr)
        print(f"Current installed: {result.current}", file=sys.stderr)
        # Exit 0 — failure isn't actionable; treat as "no signal".
        # --check should NOT report "update available" on a failure.
        return 0

    print(f"Current installed: {result.current}")
    print(f"Latest on PyPI:    {result.latest}")
    if not result.is_newer:
        print("Status: up to date")
        return 0

    print("Status: UPDATE AVAILABLE")
    if not args.apply:
        if args.check:
            # CI / script flow — non-zero so callers can act.
            return 1
        print()
        print("To install:  mimir update --apply")
        print("After install, restart the agent:")
        print("  docker compose restart   # containerized deployments")
        print("  (or restart the mimir process by hand for bare-metal)")
        return 0

    # --apply path. Use python -m pip directly so this works regardless
    # of whether the operator's environment uses pip, uv, or pipx for
    # the original install — they all expose ``python -m pip``. The
    # package name is whatever ``MIMIR_PYPI_PACKAGE_NAME`` resolves to
    # (defaults to ``mimir`` for now; will be the real published name
    # once the open-source release is on PyPI).
    print()
    print(f"Installing {pkg} {result.latest} via python -m pip...")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", pkg],
            check=False,
        )
    except OSError as exc:
        print(f"pip invocation failed: {exc}", file=sys.stderr)
        return 2
    if completed.returncode != 0:
        print(
            f"pip exited {completed.returncode}; install did not succeed.",
            file=sys.stderr,
        )
        return completed.returncode
    print()
    print(f"Installed {pkg} {result.latest}.")
    print()
    print("IMPORTANT: the running agent process is still on the OLD")
    print("version until you restart it. To engage the new code:")
    print("  docker compose restart   # containerized deployments")
    print("  (or restart the mimir process by hand for bare-metal)")
    return 0
