"""First-contact DM-channel capture + the list_channels tool.

Covers the feature that auto-records a user's DM channel into
``state/identities.yaml`` on first contact per bridge, the resolver
accessor that reads it back, and the read-only ``list_channels`` tool.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mimir.identities import IdentityResolver
from mimir.identities_populator import capture_dm_channel
from mimir.bridges.bench import BenchBridge
from mimir.tools.registry import (
    list_channels,
    set_channel_registry,
    set_identity_resolver,
)


def _read(home: Path) -> dict:
    return yaml.safe_load((home / "state" / "identities.yaml").read_text(encoding="utf-8"))


def test_capture_creates_new_person_on_fresh_home(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)

    assert capture_dm_channel(home, "slack-U05ABC", "slack", "dm-slack-D07XYZ") is True

    people = _read(home)["people"]
    assert len(people) == 1
    p = people[0]
    # Unknown alias → new entry keyed by the inbound id (operator merges later).
    assert p["canonical"] == "slack-U05ABC"
    assert "slack-U05ABC" in p["aliases"]
    assert p["dm_channels"] == {"slack": "dm-slack-D07XYZ"}


def test_capture_fills_existing_person_preserving_operator_fields_and_header(
    tmp_path: Path,
) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)
    yaml_path = home / "state" / "identities.yaml"
    yaml_path.write_text(
        "# operator schema header — keep me\n"
        "people:\n"
        "  - canonical: alice\n"
        "    display_name: Alice Smith\n"
        "    aliases: [slack-U05ABC, discord-456]\n"
        "    notes: eng lead\n",
        encoding="utf-8",
    )

    assert capture_dm_channel(home, "slack-U05ABC", "slack", "dm-slack-D07XYZ") is True

    text = yaml_path.read_text(encoding="utf-8")
    assert text.startswith("# operator schema header — keep me")  # header preserved
    alice = _read(home)["people"][0]
    # Match-by-alias hit the existing entry; operator fields untouched.
    assert alice["canonical"] == "alice"
    assert alice["display_name"] == "Alice Smith"
    assert alice["notes"] == "eng lead"
    assert alice["dm_channels"]["slack"] == "dm-slack-D07XYZ"


def test_capture_is_fill_blank_and_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)

    assert capture_dm_channel(home, "slack-U05ABC", "slack", "dm-slack-D1") is True
    # Same value again → no change.
    assert capture_dm_channel(home, "slack-U05ABC", "slack", "dm-slack-D1") is False
    # A *different* value never overwrites the captured one.
    assert capture_dm_channel(home, "slack-U05ABC", "slack", "dm-slack-OTHER") is False

    assert _read(home)["people"][0]["dm_channels"]["slack"] == "dm-slack-D1"


def test_capture_multi_platform_on_a_merged_person(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)
    (home / "state" / "identities.yaml").write_text(
        "people:\n"
        "  - canonical: alice\n"
        "    aliases: [slack-U05ABC, discord-456]\n",
        encoding="utf-8",
    )

    assert capture_dm_channel(home, "slack-U05ABC", "slack", "dm-slack-D1") is True
    assert capture_dm_channel(home, "discord-456", "discord", "dm-discord-789") is True

    alice = _read(home)["people"][0]
    assert alice["dm_channels"] == {"slack": "dm-slack-D1", "discord": "dm-discord-789"}


def test_capture_rejects_empty_args(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)
    assert capture_dm_channel(home, "", "slack", "dm-slack-D1") is False
    assert capture_dm_channel(home, "slack-U1", "", "dm-slack-D1") is False
    assert capture_dm_channel(home, "slack-U1", "slack", "") is False
    assert not (home / "state" / "identities.yaml").exists()


def test_resolver_dm_channel_accessor_round_trips(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)
    capture_dm_channel(home, "slack-U05ABC", "slack", "dm-slack-D07XYZ")

    resolver = IdentityResolver(home=home)
    resolver.reload()

    # Resolves through the alias to the captured DM channel.
    assert resolver.dm_channel("slack-U05ABC", "slack") == "dm-slack-D07XYZ"
    assert resolver.dm_channels("slack-U05ABC") == {"slack": "dm-slack-D07XYZ"}
    # Sole-DM convenience (no platform) + unknown platform.
    assert resolver.dm_channel("slack-U05ABC") == "dm-slack-D07XYZ"
    assert resolver.dm_channel("slack-U05ABC", "discord") is None
    assert resolver.dm_channel("nobody-here") is None


class _FakeRegistry:
    def __init__(self, prefixes: list[str]) -> None:
        self._prefixes = prefixes

    def prefixes(self) -> list[str]:
        return list(self._prefixes)


@pytest.mark.asyncio
async def test_list_channels_tool(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)
    (home / "state" / "identities.yaml").write_text(
        "channels:\n"
        "  - canonical: discord-100\n"
        "    display_name: ops-room\n"
        "    kind: public\n"
        "people:\n"
        "  - canonical: alice\n"
        "    display_name: Alice\n"
        "    aliases: [slack-U1]\n"
        "    dm_channels: {slack: dm-slack-D1, discord: dm-discord-2}\n",
        encoding="utf-8",
    )
    resolver = IdentityResolver(home=home)
    resolver.reload()
    set_identity_resolver(resolver)
    set_channel_registry(
        _FakeRegistry(["dm-slack-", "slack-", "dm-discord-", "discord-", "web-"])
    )
    try:
        out = json.loads(await list_channels.ainvoke({}))
        assert any(c["channel_id"] == "discord-100" for c in out["channels"])
        dms = {(d["person"], d["platform"]): d["channel_id"] for d in out["dms"]}
        assert dms[("alice", "slack")] == "dm-slack-D1"
        assert dms[("alice", "discord")] == "dm-discord-2"
        assert "slack-" in out["live_prefixes"] and "discord-" in out["live_prefixes"]

        # platform filter → slack only
        slack = json.loads(await list_channels.ainvoke({"platform": "slack"}))
        assert slack["platform"] == "slack"
        assert all(d["platform"] == "slack" for d in slack["dms"])
        assert all(
            c["channel_id"].startswith(("slack-", "dm-slack-")) for c in slack["channels"]
        )
        assert "discord-100" not in [c["channel_id"] for c in slack["channels"]]
        assert "discord-" not in slack["live_prefixes"]
        assert "dm-slack-" in slack["live_prefixes"]
    finally:
        set_identity_resolver(None)
        set_channel_registry(None)


@pytest.mark.asyncio
async def test_bridge_base_resolve_dm_channel_defaults_none(tmp_path: Path) -> None:
    # BenchBridge inherits the base no-op default (no DM concept).
    bench = BenchBridge(home=tmp_path)
    assert await bench.resolve_dm_channel("U1") is None
