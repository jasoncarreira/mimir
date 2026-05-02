"""Reflection skill helper: fetch top-N most-retrieved atoms from SAGA.

Invoked from the reflection skill's SKILL.md via the `mimir reflection
most-retrieved` CLI subcommand (see ``mimir/cli.py``). Not exposed as
an MCP tool — this query fires once a week from the reflection turn,
so paying the agent's permanent toolspace cost on every turn would be
the wrong tradeoff. Bundling as a CLI subcommand keeps the toolspace
tight while keeping the script reachable without cwd / PATH gymnastics
(``mimir`` is on PATH wherever the agent was launched from).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mimir.config import Config
from mimir.saga_client import make_saga_client


def add_argparse(p: argparse.ArgumentParser) -> None:
    """Wire flags onto ``p``. Shared between standalone module-mode (rare,
    development-only) and the ``mimir reflection most-retrieved`` CLI."""
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


async def run(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    client = make_saga_client(endpoint=cfg.saga_endpoint, api_key=cfg.saga_api_key or None)
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


async def _amain() -> int:
    p = argparse.ArgumentParser(
        description="Top-N most-retrieved SAGA atoms over a recent window."
    )
    add_argparse(p)
    return await run(p.parse_args())


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
