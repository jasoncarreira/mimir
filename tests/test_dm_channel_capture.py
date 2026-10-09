"""First-contact DM-channel capture + the list_channels tool.

Covers the feature that auto-records a user's DM channel into
``state/identities.yaml`` on first contact per bridge, the resolver
accessor that reads it back, and the read-only ``list_channels`` tool.
"""

from __future__ import annotations

import json
import argparse
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from mimir.identities import IdentityResolver
from mimir import identities_populator as _pop
from mimir.identities_populator import capture_dm_channel, merge_into_yaml
from mimir.identities_populator import approve_pairing, request_dm_pairing
from mimir.identities_populator import request_pairing, request_pairing_status
from mimir.identities_populator import (
    PairingCodeLockedError, approve_pairing_code, request_pairing_with_code,
)
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
        "    access: {roles: [user, admin]}\n"
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
    assert alice["access"] == {"roles": ["user", "admin"]}
    assert alice["dm_channels"]["slack"] == "dm-slack-D07XYZ"

    resolver = IdentityResolver(home=home)
    resolver.reload()
    assert resolver.access_dict("slack-U05ABC") == {"roles": ["user", "admin"]}
    assert resolver.access_dict("discord-456") == {"roles": ["user", "admin"]}
    assert resolver.is_authorized("slack-U05ABC") is True


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


def test_both_identities_writers_share_one_lock() -> None:
    # The capture writer and the scheduled populator must coordinate through
    # the SAME lock, or they lost-update identities.yaml (mimir-carreira #710).
    assert hasattr(_pop, "_IDENTITIES_WRITE_LOCK")
    assert hasattr(capture_dm_channel, "__wrapped__")  # decorated = lock-serialized
    assert hasattr(merge_into_yaml, "__wrapped__")


def test_populator_merge_preserves_captured_dm_channels(tmp_path: Path) -> None:
    """A scheduled populate must not erase a just-captured dm_channels entry,
    and capture must not erase populator fields — the cross-writer coordination
    plus dm_channels being preserved through merge_into_yaml's in-place fill."""
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)

    # First contact captures a DM channel (creates the person entry).
    assert capture_dm_channel(home, "slack-U05ABC", "slack", "dm-slack-D1") is True

    # The daily populator later runs, matching the same person by alias and
    # adding a cross-platform alias + display_name.
    merge_into_yaml(
        home,
        people=[{"aliases": ["slack-U05ABC", "discord-456"], "display_name": "Alice"}],
        channels=[],
    )

    person = next(
        p for p in _read(home)["people"] if "slack-U05ABC" in (p.get("aliases") or [])
    )
    # Populator additions landed...
    assert "discord-456" in person["aliases"]
    assert person["display_name"] == "Alice"
    # ...and the captured DM channel survived (no lost update).
    assert person["dm_channels"]["slack"] == "dm-slack-D1"


def test_request_dm_pairing_creates_pending_identity_without_roles(
    tmp_path: Path,
) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)

    assert request_dm_pairing(
        home,
        "slack-U05ABC",
        "slack",
        "dm-slack-D07XYZ",
        author_display="Alice",
    ) is True

    person = _read(home)["people"][0]
    assert person["canonical"] == "slack-U05ABC"
    assert person["display_name"] == "Alice"
    assert person["dm_channels"] == {"slack": "dm-slack-D07XYZ"}
    assert person["pairing"]["status"] == "pending"
    assert "access" not in person

    resolver = IdentityResolver(home=home)
    resolver.reload()
    assert resolver.is_authorized("slack-U05ABC") is False


def test_request_public_pairing_creates_pending_identity_without_dm_or_roles(
    tmp_path: Path,
) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)

    assert request_pairing(
        home,
        "slack-U05ABC",
        "slack",
        channel_id="slack-C07XYZ",
        author_display="Alice",
        is_dm=False,
    ) is True
    assert request_pairing(
        home,
        "slack-U05ABC",
        "slack",
        channel_id="slack-C07XYZ",
        author_display="Alice",
        is_dm=False,
    ) is False

    person = _read(home)["people"][0]
    assert person["canonical"] == "slack-U05ABC"
    assert person["display_name"] == "Alice"
    assert "dm_channels" not in person
    assert person["pairing"]["status"] == "pending"
    assert person["pairing"]["channel"] == "slack-C07XYZ"
    assert person["pairing"]["delivery"] == "public_shared_channel"
    assert "access" not in person

    resolver = IdentityResolver(home=home)
    resolver.reload()
    assert resolver.is_authorized("slack-U05ABC") is False


def test_request_pairing_bounds_new_pending_identity_growth(
    tmp_path: Path,
) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)

    assert request_pairing(
        home,
        "slack-U1",
        "slack",
        channel_id="dm-slack-D1",
        is_dm=True,
        max_pending=1,
    ) is True
    assert request_pairing(
        home,
        "slack-U2",
        "slack",
        channel_id="dm-slack-D2",
        is_dm=True,
        max_pending=1,
    ) is False
    assert request_pairing_status(
        home,
        "slack-U2",
        "slack",
        channel_id="dm-slack-D2",
        is_dm=True,
        max_pending=1,
    ) == "capped"

    people = _read(home)["people"]
    assert [p["canonical"] for p in people] == ["slack-U1"]
    assert people[0]["pairing"]["status"] == "pending"


def test_approve_pairing_preserves_operator_fields_and_allowlists_canonical(
    tmp_path: Path,
) -> None:
    home = tmp_path / "agent"
    (home / "state").mkdir(parents=True)
    (home / "state" / "identities.yaml").write_text(
        "people:\n"
        "  - canonical: alice\n"
        "    display_name: Alice Smith\n"
        "    aliases: [slack-U05ABC, discord-456]\n"
        "    notes: operator-authored\n"
        "    dm_channels: {slack: dm-slack-D07XYZ}\n"
        "    pairing: {status: pending, requested_at: '2026-01-01T00:00:00Z'}\n",
        encoding="utf-8",
    )

    assert approve_pairing(home, "slack-U05ABC") is True

    alice = _read(home)["people"][0]
    assert alice["canonical"] == "alice"
    assert alice["display_name"] == "Alice Smith"
    assert alice["aliases"] == ["slack-U05ABC", "discord-456"]
    assert alice["notes"] == "operator-authored"
    assert alice["dm_channels"] == {"slack": "dm-slack-D07XYZ"}
    assert alice["pairing"]["status"] == "approved"
    assert alice["access"] == {"roles": ["user"]}

    resolver = IdentityResolver(home=home)
    resolver.reload()
    assert resolver.is_authorized("slack-U05ABC") is True
    assert resolver.resolve("discord-456") == "alice"


def _clock(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 10, 9, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(_pop, "datetime", Clock)
    return Clock


def test_pairing_codes_are_hashed_rate_limited_and_dm_only(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    home = tmp_path / "agent"
    kwargs = dict(channel_id="dm-discord-100", is_dm=True, max_pending=1)
    status, first = request_pairing_with_code(home, "discord-1", "discord", **kwargs)
    assert status == "changed" and re.fullmatch(r"[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}", first)
    raw = (home / "state" / "identities.yaml").read_text()
    assert first not in raw
    pairing = _read(home)["people"][0]["pairing"]
    assert len(bytes.fromhex(pairing["code_salt"])) == 16
    assert pairing["code_hash"] == hashlib.sha256(
        bytes.fromhex(pairing["code_salt"]) + first.encode()
    ).hexdigest()
    assert datetime.fromisoformat(pairing["code_expires_at"]) == clock.current + timedelta(hours=1)
    clock.current += timedelta(minutes=9)
    assert request_pairing_with_code(home, "discord-1", "discord", **kwargs) == ("unchanged", None)
    assert request_pairing_with_code(home, "discord-2", "discord", channel_id="dm-discord-200", is_dm=True, max_pending=1) == ("capped", None)
    assert len(_read(home)["people"]) == 1
    clock.current += timedelta(minutes=1)
    status, second = request_pairing_with_code(home, "discord-1", "discord", **kwargs)
    assert status == "changed" and second != first
    assert _read(home)["people"][0]["pairing"]["code_hash"] != pairing["code_hash"]
    assert second not in (home / "state" / "identities.yaml").read_text()
    assert request_pairing_with_code(home, "slack-U1", "slack", channel_id="slack-C1", is_dm=False) == ("changed", None)
    assert "code_hash" not in _read(home)["people"][1]["pairing"]


def test_authorized_sender_does_not_receive_a_pairing_code(tmp_path):
    home = tmp_path / "agent"
    assert request_pairing_with_code(home, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True)[1]
    assert approve_pairing(home, "slack-U1")
    assert request_pairing_with_code(home, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True) == ("unchanged", None)
    assert "code_hash" not in _read(home)["people"][0]["pairing"]


def test_approved_pairing_with_stale_code_cannot_be_approved_again(tmp_path):
    home = tmp_path / "agent"
    _, code = request_pairing_with_code(home, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True)
    doc = _read(home)
    doc["people"][0]["pairing"]["status"] = "approved"
    (home / "state" / "identities.yaml").write_text(yaml.safe_dump(doc))
    assert not approve_pairing_code(home, code)
    assert "access" not in _read(home)["people"][0]


def test_malformed_lockout_state_refuses_even_valid_code(tmp_path):
    home = tmp_path / "agent"
    _, code = request_pairing_with_code(home, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True)
    (home / "state" / "pairing_lockout.json").write_text('{"failed_attempts": "unknown"}')
    with pytest.raises(PairingCodeLockedError, match="locked"):
        approve_pairing_code(home, code)
    assert "access" not in _read(home)["people"][0]


def test_pairing_code_approval_lockout_expiration_and_canonical_bypass(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    home = tmp_path / "agent"
    _, code = request_pairing_with_code(home, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True)
    assert code is not None
    calls = []
    compare = _pop.secrets.compare_digest

    def observed(a, b):
        calls.append((a, b))
        return compare(a, b)

    monkeypatch.setattr(_pop.secrets, "compare_digest", observed)
    for n in range(5):
        assert not approve_pairing_code(home, "ZZZZZZZZ")
        assert json.loads((home / "state" / "pairing_lockout.json").read_text())["failed_attempts"] == n + 1
    assert len(calls) == 5
    with pytest.raises(PairingCodeLockedError, match="locked"):
        approve_pairing_code(home, code)
    assert len(calls) == 5  # even a valid code cannot reach lookup during lockout
    clock.current += timedelta(minutes=10)
    _, code = request_pairing_with_code(home, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True)
    clock.current += timedelta(minutes=50)
    assert approve_pairing_code(home, "  ".join(code.lower()))
    person = _read(home)["people"][0]
    assert person["access"]["roles"] == ["user"]
    assert person["pairing"]["status"] == "approved"
    assert not {"code_hash", "code_salt", "code_expires_at"} & person["pairing"].keys()
    assert json.loads((home / "state" / "pairing_lockout.json").read_text())["failed_attempts"] == 0
    assert not approve_pairing_code(home, code)  # single use

    _, expired = request_pairing_with_code(home, "discord-2", "discord", channel_id="dm-discord-2", is_dm=True)
    clock.current += timedelta(hours=1)
    assert not approve_pairing_code(home, expired)
    assert _read(home)["people"][1]["pairing"]["status"] == "pending"
    assert approve_pairing(home, "discord-2", roles=["user", "admin"])
    assert _read(home)["people"][1]["access"]["roles"] == ["user", "admin"]
    assert "code_hash" not in _read(home)["people"][1]["pairing"]


def test_pairing_code_cli_and_mutual_exclusion(tmp_path, capsys):
    from mimir.commands import identities as cmd

    _, code = request_pairing_with_code(tmp_path, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    id_parser = cmd.add_argparse(sub)

    def run(*args):
        return cmd.dispatch(parser.parse_args(["identities", "approve-pairing", "--home", str(tmp_path), *args]), id_parser)

    assert run("--code", code, "--admin") == 0
    assert _read(tmp_path)["people"][0]["access"]["roles"] == ["user", "admin"]
    assert code not in capsys.readouterr().out
    assert run("--code", code) == 1
    assert "invalid or expired" in capsys.readouterr().err
    assert run("slack-U1", "--code", code) == 1
    assert "either an identity or --code" in capsys.readouterr().err
    assert run() == 1
    assert run("slack-U1") == 0  # canonical form retains existing role semantics
    _, other = request_pairing_with_code(tmp_path, "slack-U2", "slack", channel_id="dm-slack-D2", is_dm=True)
    assert run("slack-U2") == 0
    assert other not in capsys.readouterr().out

    for _ in range(5):
        assert run("--code", "ZZZZZZZZ") == 1
    capsys.readouterr()
    assert run("--code", "ABCDEF23") == 1
    assert "locked" in capsys.readouterr().err
    _, third = request_pairing_with_code(tmp_path, "slack-U3", "slack", channel_id="dm-slack-D3", is_dm=True)
    assert run("slack-U3") == 0  # canonical form bypasses code lockout
    assert third not in capsys.readouterr().out


@pytest.mark.parametrize("platform,channel", [
    ("slack", "dm-slack-G123"), ("discord", "discord-123"),
    ("slack", "slack-C123"), ("discord", "dm-unknown"),
])
def test_shared_channels_never_mint_pairing_codes(tmp_path, platform, channel):
    _, code = request_pairing_with_code(tmp_path, f"{platform}-1", platform,
                                       channel_id=channel, is_dm=True)
    assert code is None
    assert "code_hash" not in _read(tmp_path)["people"][0]["pairing"]


def test_status_only_pairing_never_consumes_a_code(tmp_path):
    request_pairing_status(tmp_path, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True)
    assert "code_hash" not in _read(tmp_path)["people"][0]["pairing"]
    assert request_pairing_with_code(tmp_path, "slack-U1", "slack", channel_id="dm-slack-D1", is_dm=True)[1]


def test_delivery_cleanup_cannot_erase_a_newer_code(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    kwargs = dict(channel_id="dm-slack-D1", is_dm=True)
    _, first = request_pairing_with_code(tmp_path, "slack-U1", "slack", **kwargs)
    clock.current += timedelta(minutes=10)
    _, second = request_pairing_with_code(tmp_path, "slack-U1", "slack", **kwargs)
    assert not _pop.prepare_pairing_code_delivery(tmp_path, "slack-U1", first, failed=True)
    assert not _pop.prepare_pairing_code_delivery(tmp_path, "slack-U1", first)
    assert approve_pairing_code(tmp_path, second)
    assert not _pop.prepare_pairing_code_delivery(tmp_path, "slack-U1", second)


@pytest.mark.parametrize("operation", ["mint", "guess"])
def test_identity_transactions_serialize_across_processes(tmp_path, operation):
    import subprocess
    import sys
    import select

    request_pairing_with_code(tmp_path, "slack-U0", "slack", channel_id="dm-slack-D0", is_dm=True)
    script = '''
import sys
from pathlib import Path
from mimir import identities_populator as pop
home, operation, actor = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
original = pop._load_yaml
def load(path):
    result = original(path)
    print("loaded", flush=True)
    if actor == "1":
        sys.stdin.readline()
    return result
pop._load_yaml = load
print("started", flush=True)
if operation == "mint":
    pop.request_pairing_with_code(home, "slack-U" + actor, "slack",
                                 channel_id="dm-slack-D" + actor, is_dm=True)
else:
    pop.approve_pairing_code(home, "ZZZZZZZZ")
'''
    first = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), operation, "1"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    second = None
    try:
        assert select.select([first.stdout], [], [], 15)[0]
        assert first.stdout.readline().strip() == b"started"
        assert select.select([first.stdout], [], [], 15)[0]
        assert first.stdout.readline().strip() == b"loaded"
        second = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), operation, "2"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        assert select.select([second.stdout], [], [], 15)[0]
        assert second.stdout.readline().strip() == b"started"
        # The second process cannot load a stale snapshot while the first
        # transaction is paused after reading but before writing.
        assert not select.select([second.stdout], [], [], 0.2)[0]
        _, error = first.communicate(b"continue\n", timeout=15)
        assert first.returncode == 0, error
        output, error = second.communicate(timeout=15)
        assert second.returncode == 0, error
        assert output.strip() == b"loaded"
        if operation == "mint":
            assert {p["canonical"] for p in _read(tmp_path)["people"]} == {"slack-U0", "slack-U1", "slack-U2"}
            assert all("code_hash" in p["pairing"] for p in _read(tmp_path)["people"])
        else:
            state = json.loads((tmp_path / "state" / "pairing_lockout.json").read_text())
            assert state["failed_attempts"] == 2
    finally:
        for child in (first, second):
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
