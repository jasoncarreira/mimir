"""Verify release and direct wheels contain the same runtime files."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from zipfile import ZipFile


REQUIRED_MEMBERS = (
    "mimir/web_auth.js",
    "mimir/bundled_docs/.env.example",
)
REACT_DIST_PREFIX = "mimir/react_app/dist/"


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
