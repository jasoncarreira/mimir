"""Verify release and direct wheels contain the same runtime files."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from zipfile import ZipFile


REQUIRED_MEMBERS = (
    "mimir/web_auth.js",
    "mimir/bundled_docs/.env.example",
    "mimir/acp/__init__.py",
    "mimir/acp/__main__.py",
    "mimir/acp/agent.py",
    "mimir/acp/bootstrap.py",
    "mimir/acp/credentials.py",
    "mimir/acp/daemon.py",
    "mimir/acp/host.py",
    "mimir/acp/hands_contract.py",
    "mimir/acp/profiles.py",
    "mimir/acp/proxy.py",
    "mimir/acp/relay.py",
    "mimir/acp/ssh.py",
    "mimir/acp/bridge.py",
    "mimir/acp/journal.py",
    "mimir/acp/session_store.py",
    "mimir/acp/updates.py",
    "mimir/acp/sdk.py",
    "mimir/acp/stdio.py",
    "mimir/acp/transport.py",
    "mimir/bundled_docs/docs/acp.md",
)
REACT_DIST_PREFIX = "mimir/react_app/dist/"
ACP_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "mimir" / "acp"


def acp_source_members() -> set[str]:
    return {f"mimir/acp/{path.name}" for path in ACP_SOURCE_ROOT.glob("*.py")}


def wheel_members(path: Path) -> Counter[str]:
    with ZipFile(path) as wheel:
        return Counter(wheel.namelist())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdist-wheel", type=Path, required=True)
    parser.add_argument("--direct-wheel", type=Path, required=True)
    args = parser.parse_args()

    sdist_members = wheel_members(args.sdist_wheel)
    direct_members = wheel_members(args.direct_wheel)

    required_acp = {name for name in REQUIRED_MEMBERS if name.startswith("mimir/acp/")}
    actual_acp = acp_source_members()
    if required_acp != actual_acp:
        raise SystemExit(
            "ACP source and required wheel inventories differ: "
            f"missing-required={sorted(actual_acp - required_acp)}, "
            f"unexpected-required={sorted(required_acp - actual_acp)}"
        )
    built_acp = {name for name in sdist_members if name.startswith("mimir/acp/") and name.endswith(".py")}
    if built_acp != actual_acp:
        raise SystemExit(
            "ACP source and built wheel inventories differ: "
            f"missing-built={sorted(actual_acp - built_acp)}, "
            f"unexpected-built={sorted(built_acp - actual_acp)}"
        )

    missing = [name for name in REQUIRED_MEMBERS if name not in sdist_members]
    if not any(
        name.startswith(REACT_DIST_PREFIX) and not name.endswith("/")
        for name in sdist_members
    ):
        missing.append(f"{REACT_DIST_PREFIX}*")
    if missing:
        raise SystemExit(
            f"{args.sdist_wheel} is missing required wheel members: {missing}"
        )

    if sdist_members != direct_members:
        only_sdist = sorted((sdist_members - direct_members).elements())
        only_direct = sorted((direct_members - sdist_members).elements())
        raise SystemExit(
            "wheel member lists differ by build path: "
            f"only in sdist-built={only_sdist}; only in direct={only_direct}"
        )

    print(
        f"verified {len(sdist_members)} identical wheel members, including "
        f"{', '.join(REQUIRED_MEMBERS)} and {REACT_DIST_PREFIX}*"
    )


if __name__ == "__main__":
    main()
