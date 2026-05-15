"""Turn-level lifecycle hooks — post-cutover stub.

Pre-cutover this exposed registration points around the SDK Agent's
11-phase run_turn (CR#15). The deepagents-backed Agent.run_turn in
mimir/agent.py:Agent.run_turn is a much shorter linear method, and
the hook points (pre-message memory inject, post-message credit
pass) are inlined there directly.

Phase D will revisit whether this surface is needed for production
extensibility; for now it's a no-op stub so legacy imports don't
break.
"""
from __future__ import annotations

from typing import Any


def register_turn_hook(*args, **kwargs) -> None:
    """No-op post-cutover."""
    return None


def turn_hooks_registry() -> dict:
    return {}
