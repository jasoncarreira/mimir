"""Probe → channel-message adaptation for the file_search autopass A/B harness.

Mirrors ``benchmarks/longmemeval_via_mimir/route.py`` but for a simpler
probe shape — there's no haystack to ingest, just a user message to
post against the live memory/ + state/ checkout the indexer already
sees.

A probe has the form::

    {
      "text": "fixture 'aiohttp_server' not found, what do I do?",
      "expected_target": "memory/issues/pytest-aiohttp-dev-extras.md",
      "shape": "fingerprinted-error",
    }

For each probe we generate a stable channel id (``bench-fsap-<idx>-<arm>``)
so BenchBridge handles outbound and the per-arm result JSONL stays
scoped to the arm's run.
"""

from __future__ import annotations

from typing import Any


def probe_to_event(probe: dict[str, Any], channel_id: str) -> dict[str, Any]:
    """Build the JSON body for ``POST /event`` from a probe row.

    Trigger is ``user_message`` so mimir's pre-message hook fires
    (saga query, contextual rewrite when enabled, AND the file_search
    autopass block when the arm has it on).
    """
    return {
        "trigger": "user_message",
        "channel_id": channel_id,
        "content": probe["text"],
        "extra": {
            "probe_index": probe.get("_index"),
            "shape": probe.get("shape"),
            "expected_target": probe.get("expected_target"),
        },
    }


def channel_id_for(arm: str, idx: int) -> str:
    """Stable channel-id naming. BenchBridge.prefixes = ('bench',) so
    anything starting with 'bench-' routes through the bench bridge
    rather than DiscordBridge/SlackBridge.

    Per-arm channels keep per-channel chat-history buffers from
    leaking the autopass-on prompt into the autopass-off run.
    """
    return f"bench-fsap-{arm}-{idx:03d}"
