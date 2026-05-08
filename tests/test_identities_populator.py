"""Bridge populator merger + scrapers (chainlink #40 Phase D / #44).

The merger has the most surface area — idempotency, operator-set field
preservation, fresh-file synthesis, malformed-input tolerance — and is
covered exhaustively here. The scraper functions are tested with
duck-typed mock clients (no live Discord/Slack imports required).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest
import yaml

from mimir.identities import IdentityResolver
from mimir.identities_populator import (
    merge_into_yaml,
    populate_all,
    populate_from_discord,
    populate_from_slack,
)


def _state_yaml(home: Path) -> Path:
    return home / "state" / "identities.yaml"


def _write(home: Path, body: str) -> None:
    p = _state_yaml(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(body), encoding="utf-8")


def _read_doc(home: Path) -> dict:
    text = _state_yaml(home).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


# ---------------------------------------------------------------------------
# merge_into_yaml — fresh file, idempotency, operator-preservation.
# ---------------------------------------------------------------------------


def test_merge_into_fresh_yaml_creates_both_sections(tmp_path: Path):
    counts = merge_into_yaml(
        tmp_path,
        people=[
            {"aliases": ["discord-1"], "display_name": "Alice"},
        ],
        channels=[
            {"canonical": "discord-1500", "display_name": "#general", "kind": "public"},
        ],
    )
    assert counts["people_added"] == 1
    assert counts["channels_added"] == 1
    assert counts["people_updated"] == 0
    assert counts["channels_updated"] == 0
    doc = _read_doc(tmp_path)
    assert any(p["canonical"] == "discord-1" for p in doc["people"])
    assert any(c["canonical"] == "discord-1500" for c in doc["channels"])


def test_merge_idempotent_on_rerun(tmp_path: Path):
    """Running the same populator output twice yields zero deltas the
    second time — the headline idempotency contract."""
    payload = {
        "people": [{"aliases": ["discord-1"], "display_name": "Alice"}],
        "channels": [
            {
                "canonical": "discord-1500",
                "display_name": "#general",
                "kind": "public",
            }
        ],
    }
    counts1 = merge_into_yaml(
        tmp_path, people=payload["people"], channels=payload["channels"]
    )
    assert counts1["people_added"] == 1
    counts2 = merge_into_yaml(
        tmp_path, people=payload["people"], channels=payload["channels"]
    )
    assert counts2["people_added"] == 0
    assert counts2["people_updated"] == 0
    assert counts2["channels_added"] == 0
    assert counts2["channels_updated"] == 0


def test_merge_preserves_operator_set_display_name(tmp_path: Path):
    """If the operator hand-edited a display_name to something different
    from what the bridge reports, we never overwrite. (e.g. operator
    sets canonical=jason, populator sees display_name=Jason Carreira; if
    the operator changed display_name to 'jason' the populator value
    must not clobber it.)"""
    _write(
        tmp_path,
        """\
        people:
          - canonical: jason
            display_name: jason
            aliases:
              - discord-238367217903730690
            notes: Operator-set notes
        """,
    )
    counts = merge_into_yaml(
        tmp_path,
        people=[
            {
                "aliases": ["discord-238367217903730690"],
                "display_name": "Jason Carreira",  # would-be overwrite
                "notes": "Discord member",  # would-be overwrite
            }
        ],
        channels=[],
    )
    # Same alias → matched; nothing to add/update because every field is
    # already set.
    assert counts["people_added"] == 0
    assert counts["people_updated"] == 0
    doc = _read_doc(tmp_path)
    jason = next(p for p in doc["people"] if p["canonical"] == "jason")
    assert jason["display_name"] == "jason"
    assert jason["notes"] == "Operator-set notes"


def test_merge_fills_blank_fields_only(tmp_path: Path):
    """Operator left display_name blank — the populator fills it in.
    Other operator-set fields stay untouched."""
    _write(
        tmp_path,
        """\
        people:
          - canonical: jason
            aliases: [discord-238367217903730690]
            notes: Operator-set notes
        """,
    )
    counts = merge_into_yaml(
        tmp_path,
        people=[
            {
                "aliases": ["discord-238367217903730690"],
                "display_name": "Jason Carreira",
                "notes": "Different notes",  # ignored — operator already set
            }
        ],
        channels=[],
    )
    assert counts["people_updated"] == 1
    doc = _read_doc(tmp_path)
    jason = next(p for p in doc["people"] if p["canonical"] == "jason")
    assert jason["display_name"] == "Jason Carreira"  # filled
    assert jason["notes"] == "Operator-set notes"  # preserved


def test_merge_adds_new_alias_to_existing_canonical(tmp_path: Path):
    """Cross-platform identity merge — operator set jason with a discord
    alias; populator brings in his slack alias. The new alias is added
    without disturbing the canonical or display_name."""
    _write(
        tmp_path,
        """\
        people:
          - canonical: jason
            display_name: Jason Carreira
            aliases: [discord-238367217903730690]
        """,
    )
    counts = merge_into_yaml(
        tmp_path,
        people=[
            {
                "aliases": ["discord-238367217903730690", "slack-U999"],
                "display_name": "Jason C",
            }
        ],
        channels=[],
    )
    assert counts["people_updated"] == 1
    doc = _read_doc(tmp_path)
    jason = next(p for p in doc["people"] if p["canonical"] == "jason")
    assert "slack-U999" in jason["aliases"]
    # Display name preserved.
    assert jason["display_name"] == "Jason Carreira"


def test_merge_channel_fills_only_blank_fields(tmp_path: Path):
    _write(
        tmp_path,
        """\
        channels:
          - canonical: discord-1500
            display_name: jason-mimir
        """,
    )
    counts = merge_into_yaml(
        tmp_path,
        people=[],
        channels=[
            {
                "canonical": "discord-1500",
                "display_name": "#general",  # would-be overwrite
                "kind": "public",  # not set — should fill
                "notes": "Discord guild: mimir-test",  # not set — should fill
            }
        ],
    )
    assert counts["channels_updated"] == 1
    doc = _read_doc(tmp_path)
    ch = next(c for c in doc["channels"] if c["canonical"] == "discord-1500")
    assert ch["display_name"] == "jason-mimir"  # preserved
    assert ch["kind"] == "public"  # filled
    assert ch["notes"] == "Discord guild: mimir-test"  # filled


def test_merge_dry_run_does_not_write(tmp_path: Path):
    counts = merge_into_yaml(
        tmp_path,
        people=[{"aliases": ["discord-1"]}],
        channels=[{"canonical": "discord-1500", "kind": "public"}],
        dry_run=True,
    )
    assert counts["people_added"] == 1
    assert counts["channels_added"] == 1
    # File never written.
    assert not _state_yaml(tmp_path).is_file()


def test_merge_no_op_does_not_bump_mtime(tmp_path: Path):
    """When nothing changes, the YAML file is left untouched (no mtime
    flap that would trip a file-watch reloader)."""
    _write(
        tmp_path,
        """\
        people:
          - canonical: jason
            aliases: [discord-238367217903730690]
        """,
    )
    yaml_path = _state_yaml(tmp_path)
    original_mtime = yaml_path.stat().st_mtime_ns
    # Sleep is fragile — just confirm the file content didn't change.
    original_content = yaml_path.read_bytes()
    counts = merge_into_yaml(
        tmp_path,
        people=[{"aliases": ["discord-238367217903730690"]}],
        channels=[],
    )
    assert counts["people_updated"] == 0
    # Content identical; we don't strictly assert mtime equality (some
    # filesystems update atime on read), only that bytes didn't change.
    assert yaml_path.read_bytes() == original_content
    # mtime sanity-check: re-stat shouldn't show a write.
    assert yaml_path.stat().st_mtime_ns == original_mtime


def test_merge_synthesizes_canonical_from_first_alias(tmp_path: Path):
    """No canonical given — first alias doubles as canonical until the
    operator merges cross-platform identities by hand."""
    counts = merge_into_yaml(
        tmp_path,
        people=[{"aliases": ["discord-12345"]}],
        channels=[],
    )
    assert counts["people_added"] == 1
    doc = _read_doc(tmp_path)
    p = doc["people"][0]
    assert p["canonical"] == "discord-12345"
    assert p["aliases"] == ["discord-12345"]


def test_merge_skips_malformed_inputs(tmp_path: Path):
    """Liberal-on-read posture mirrors IdentityResolver — bad rows
    are dropped, valid rows still process."""
    counts = merge_into_yaml(
        tmp_path,
        people=[
            "not a dict",  # type: ignore[list-item]
            {"aliases": []},  # no aliases
            {"aliases": [""]},  # empty alias
            {"aliases": ["discord-good"]},
        ],
        channels=[
            "not a dict",  # type: ignore[list-item]
            {"canonical": ""},  # empty canonical
            {"canonical": "discord-1500", "kind": "public"},
        ],
    )
    assert counts["people_added"] == 1
    assert counts["channels_added"] == 1


def test_merge_strips_whitespace(tmp_path: Path):
    counts = merge_into_yaml(
        tmp_path,
        people=[{"aliases": ["  discord-1  "], "display_name": "  Alice  "}],
        channels=[
            {
                "canonical": "  discord-1500  ",
                "display_name": "  #general  ",
                "kind": "public",
            }
        ],
    )
    assert counts["people_added"] == 1
    assert counts["channels_added"] == 1
    doc = _read_doc(tmp_path)
    p = doc["people"][0]
    assert p["aliases"] == ["discord-1"]
    assert p["display_name"] == "Alice"
    ch = doc["channels"][0]
    assert ch["canonical"] == "discord-1500"
    assert ch["display_name"] == "#general"


def test_merge_preserves_existing_unrelated_entries(tmp_path: Path):
    """Operator has alice + bob; populator brings in carol. Both
    existing entries survive."""
    _write(
        tmp_path,
        """\
        people:
          - canonical: alice
            aliases: [slack-A]
          - canonical: bob
            aliases: [slack-B]
        """,
    )
    counts = merge_into_yaml(
        tmp_path,
        people=[{"aliases": ["slack-C"], "display_name": "Carol"}],
        channels=[],
    )
    assert counts["people_added"] == 1
    doc = _read_doc(tmp_path)
    canonicals = {p["canonical"] for p in doc["people"]}
    assert canonicals == {"alice", "bob", "slack-C"}


def test_merge_round_trips_through_identity_resolver(tmp_path: Path):
    """End-to-end: merger output is a valid identities.yaml that the
    IdentityResolver can load. Confirms the YAML shape matches the
    loader's expectations."""
    merge_into_yaml(
        tmp_path,
        people=[
            {"aliases": ["discord-1"], "display_name": "Alice"},
        ],
        channels=[
            {"canonical": "discord-1500", "display_name": "#general", "kind": "public"},
        ],
    )
    r = IdentityResolver(home=tmp_path)
    r.reload()
    assert r.resolve("discord-1") == "discord-1"
    assert r.display_name("discord-1") == "Alice"
    assert r.channel_count() == 1
    assert r.channel_display_name("discord-1500") == "#general"


# ---------------------------------------------------------------------------
# populate_from_discord — duck-typed Discord client.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_populate_from_discord_returns_members_and_channels():
    member = SimpleNamespace(
        id=12345, global_name=None, display_name="Alice", name="alice123"
    )
    text_channel = SimpleNamespace(id=1500, name="general")
    guild = SimpleNamespace(
        name="mimir-test", members=[member], text_channels=[text_channel]
    )
    client = SimpleNamespace(guilds=[guild], is_closed=lambda: False)
    bridge = SimpleNamespace(_client=client)

    people, channels = await populate_from_discord(bridge)
    assert len(people) == 1
    assert people[0]["canonical"] == "discord-12345"
    assert people[0]["display_name"] == "Alice"
    assert len(channels) == 1
    assert channels[0]["canonical"] == "discord-1500"
    assert channels[0]["display_name"] == "#general"
    assert channels[0]["kind"] == "public"
    assert "mimir-test" in channels[0]["notes"]


@pytest.mark.asyncio
async def test_populate_from_discord_empty_when_disconnected():
    bridge = SimpleNamespace(_client=None)
    people, channels = await populate_from_discord(bridge)
    assert people == []
    assert channels == []


@pytest.mark.asyncio
async def test_populate_from_discord_skips_member_without_id():
    member = SimpleNamespace(id=None, name="ghost")
    guild = SimpleNamespace(
        name="g", members=[member], text_channels=[]
    )
    client = SimpleNamespace(guilds=[guild], is_closed=lambda: False)
    bridge = SimpleNamespace(_client=client)
    people, _ = await populate_from_discord(bridge)
    assert people == []


@pytest.mark.asyncio
async def test_populate_from_discord_prefers_global_name():
    """``global_name`` is Discord's canonical display field (post-username
    refresh); fall back to display_name → name."""
    member = SimpleNamespace(
        id=1, global_name="Alice Smith", display_name="al", name="alice"
    )
    guild = SimpleNamespace(name="g", members=[member], text_channels=[])
    bridge = SimpleNamespace(
        _client=SimpleNamespace(guilds=[guild], is_closed=lambda: False)
    )
    people, _ = await populate_from_discord(bridge)
    assert people[0]["display_name"] == "Alice Smith"


# ---------------------------------------------------------------------------
# populate_from_slack — duck-typed Slack AsyncWebClient.
# ---------------------------------------------------------------------------


class _FakeSlackClient:
    """Mock AsyncWebClient. Supports paginated users_list /
    conversations_list and (optional) error injection."""

    def __init__(
        self,
        users_pages: list[dict] | None = None,
        channels_pages: list[dict] | None = None,
        users_error: Exception | None = None,
        channels_error: Exception | None = None,
    ):
        self.users_pages = users_pages or []
        self.channels_pages = channels_pages or []
        self.users_error = users_error
        self.channels_error = channels_error
        self.users_calls = 0
        self.channels_calls = 0

    async def users_list(self, **kwargs):
        if self.users_error is not None:
            raise self.users_error
        idx = self.users_calls
        self.users_calls += 1
        if idx >= len(self.users_pages):
            return {"members": [], "response_metadata": {"next_cursor": ""}}
        return self.users_pages[idx]

    async def conversations_list(self, **kwargs):
        if self.channels_error is not None:
            raise self.channels_error
        idx = self.channels_calls
        self.channels_calls += 1
        if idx >= len(self.channels_pages):
            return {"channels": [], "response_metadata": {"next_cursor": ""}}
        return self.channels_pages[idx]


@pytest.mark.asyncio
async def test_populate_from_slack_returns_users_and_channels():
    client = _FakeSlackClient(
        users_pages=[
            {
                "members": [
                    {
                        "id": "U123",
                        "real_name": "Alice",
                        "profile": {"display_name": "alice"},
                        "deleted": False,
                    },
                    {
                        "id": "U456",
                        "real_name": "Bot",
                        "profile": {"display_name": ""},
                        "is_bot": True,
                    },
                ],
                "response_metadata": {"next_cursor": ""},
            }
        ],
        channels_pages=[
            {
                "channels": [
                    {
                        "id": "C100",
                        "name": "general",
                        "topic": {"value": "team chatter"},
                        "is_private": False,
                    },
                    {
                        "id": "G200",
                        "name": "private-eng",
                        "is_private": True,
                    },
                ],
                "response_metadata": {"next_cursor": ""},
            }
        ],
    )
    bridge = SimpleNamespace(_app=SimpleNamespace(client=client))
    people, channels = await populate_from_slack(bridge)
    assert {p["canonical"] for p in people} == {"slack-U123", "slack-U456"}
    bot = next(p for p in people if p["canonical"] == "slack-U456")
    assert bot["notes"] == "Slack bot account"
    assert {c["canonical"] for c in channels} == {"slack-C100", "slack-G200"}
    public = next(c for c in channels if c["canonical"] == "slack-C100")
    assert public["kind"] == "public"
    assert public["display_name"] == "#general"
    assert "team chatter" in public["notes"]
    private = next(c for c in channels if c["canonical"] == "slack-G200")
    assert private["kind"] == "private"


@pytest.mark.asyncio
async def test_populate_from_slack_paginates_via_cursor():
    client = _FakeSlackClient(
        users_pages=[
            {
                "members": [{"id": "U1", "name": "alice"}],
                "response_metadata": {"next_cursor": "page2"},
            },
            {
                "members": [{"id": "U2", "name": "bob"}],
                "response_metadata": {"next_cursor": ""},
            },
        ],
    )
    bridge = SimpleNamespace(_app=SimpleNamespace(client=client))
    people, _ = await populate_from_slack(bridge)
    # Both pages consumed.
    assert {p["canonical"] for p in people} == {"slack-U1", "slack-U2"}
    assert client.users_calls == 2


@pytest.mark.asyncio
async def test_populate_from_slack_skips_deleted_users():
    client = _FakeSlackClient(
        users_pages=[
            {
                "members": [
                    {"id": "U1", "name": "active"},
                    {"id": "U2", "name": "ghost", "deleted": True},
                ],
                "response_metadata": {"next_cursor": ""},
            }
        ],
    )
    bridge = SimpleNamespace(_app=SimpleNamespace(client=client))
    people, _ = await populate_from_slack(bridge)
    assert {p["canonical"] for p in people} == {"slack-U1"}


@pytest.mark.asyncio
async def test_populate_from_slack_swallows_api_error():
    """Permissions hiccup, network blip, or SDK-side unexpected response:
    log + return empty for the failed half. Don't fail-loud — populator
    runs are best-effort and the next scheduled run picks up where this
    one left off. The OTHER half still tries (users error doesn't block
    conversations)."""
    client = _FakeSlackClient(
        users_error=RuntimeError("missing_scope"),
        channels_pages=[
            {
                "channels": [{"id": "C1", "name": "still-works"}],
                "response_metadata": {"next_cursor": ""},
            }
        ],
    )
    bridge = SimpleNamespace(_app=SimpleNamespace(client=client))
    people, channels = await populate_from_slack(bridge)
    assert people == []  # users half failed
    assert channels and channels[0]["canonical"] == "slack-C1"


@pytest.mark.asyncio
async def test_populate_from_slack_empty_when_no_app():
    bridge = SimpleNamespace(_app=None)
    people, channels = await populate_from_slack(bridge)
    assert people == []
    assert channels == []


# ---------------------------------------------------------------------------
# populate_all — orchestrator.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_populate_all_with_no_bridges_is_noop(tmp_path: Path):
    counts = await populate_all(tmp_path)
    assert counts["people_added"] == 0
    assert counts["channels_added"] == 0


@pytest.mark.asyncio
async def test_populate_all_combines_discord_and_slack(tmp_path: Path):
    discord_member = SimpleNamespace(
        id=12345, global_name=None, display_name="Alice", name="alice"
    )
    discord_channel = SimpleNamespace(id=1500, name="general")
    discord_bridge = SimpleNamespace(
        _client=SimpleNamespace(
            guilds=[
                SimpleNamespace(
                    name="g", members=[discord_member],
                    text_channels=[discord_channel],
                )
            ],
            is_closed=lambda: False,
        )
    )
    slack_client = _FakeSlackClient(
        users_pages=[
            {
                "members": [{"id": "U1", "real_name": "Bob"}],
                "response_metadata": {"next_cursor": ""},
            }
        ],
        channels_pages=[
            {
                "channels": [{"id": "C1", "name": "ops"}],
                "response_metadata": {"next_cursor": ""},
            }
        ],
    )
    slack_bridge = SimpleNamespace(_app=SimpleNamespace(client=slack_client))

    counts = await populate_all(
        tmp_path,
        discord_bridge=discord_bridge,
        slack_bridge=slack_bridge,
    )
    assert counts["people_added"] == 2
    assert counts["channels_added"] == 2
    doc = _read_doc(tmp_path)
    canonicals = {p["canonical"] for p in doc["people"]}
    assert canonicals == {"discord-12345", "slack-U1"}


@pytest.mark.asyncio
async def test_populate_all_dry_run_does_not_write(tmp_path: Path):
    discord_bridge = SimpleNamespace(
        _client=SimpleNamespace(
            guilds=[
                SimpleNamespace(
                    name="g",
                    members=[
                        SimpleNamespace(id=1, name="a", global_name="A", display_name="A")
                    ],
                    text_channels=[],
                )
            ],
            is_closed=lambda: False,
        )
    )
    counts = await populate_all(
        tmp_path, discord_bridge=discord_bridge, dry_run=True
    )
    assert counts["people_added"] == 1
    assert not _state_yaml(tmp_path).is_file()
