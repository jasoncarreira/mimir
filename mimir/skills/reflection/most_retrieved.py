"""Reflection skill helper: fetch top-N most-retrieved atoms from MSAM.

Invoked via Bash from the reflection skill's SKILL.md. Not exposed as
an MCP tool — this query fires once a week from the reflection turn,
so paying the agent's permanent toolspace cost on every turn would be
the wrong tradeoff. Bundling it as a script keeps the toolspace tight
without losing reflection-time access.

Usage:
    uv run python -m mimir.skills.reflection.most_retrieved \\
        --days 7 --count 20 --contributed-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mimir.config import Config
from mimir.msam_client import MsamClient


async def _amain() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=7,
                   help="window in days (default 7)")
    p.add_argument("--count", type=int, default=10,
                   help="top-K atoms to return (default 10)")
    p.add_argument("--channel", default=None,
                   help="filter to atoms tagged with this channel id")
    p.add_argument(
        "--contributed-only", action="store_true",
        help="count only retrievals where access_log.contributed=1 — "
             "atoms that earned their keep, not just got pulled in",
    )
    args = p.parse_args()

    cfg = Config.from_env()
    client = MsamClient(endpoint=cfg.msam_endpoint, api_key=cfg.msam_api_key or None)
    try:
        atoms = await client.most_retrieved_atoms(
            days=args.days,
            count=args.count,
            channel_id=args.channel,
            contributed_only=args.contributed_only,
        )
    finally:
        await client.close()
    json.dump(atoms, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
