"""Per-user web API key issuance + resolution (github #726 auth core)."""

from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from mimir.identities import WEB_KEY_ALIAS_PREFIX, IdentityResolver, hash_web_key
from mimir.identities_populator import issue_web_key, revoke_web_key


def _resolver(home: Path) -> IdentityResolver:
    r = IdentityResolver(home)
    r.reload()
    return r


def _people(home: Path) -> list:
    doc = yaml.safe_load((home / "state" / "identities.yaml").read_text()) or {}
    return doc.get("people") or []


def test_issue_and_resolve_round_trip(tmp_path: Path) -> None:
    key = issue_web_key(tmp_path, "alice", roles=["user"])
    r = _resolver(tmp_path)
    ident = r.resolve_web_key(key)
    assert ident is not None
    assert ident.canonical == "alice"
    assert ident.access.is_authorized
    assert not ident.access.is_admin


def test_unknown_and_empty_keys_resolve_to_none(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice", roles=["user"])
    r = _resolver(tmp_path)
    assert r.resolve_web_key("not-a-real-key") is None
    assert r.resolve_web_key("") is None
    assert r.resolve_web_key(None) is None


def test_raw_key_never_stored_only_hash(tmp_path: Path) -> None:
    key = issue_web_key(tmp_path, "alice", roles=["user"])
    text = (tmp_path / "state" / "identities.yaml").read_text()
    assert key not in text  # raw key must never hit disk
    assert hash_web_key(key) in text  # the webkey:<hash> alias is present


def test_admin_role(tmp_path: Path) -> None:
    key = issue_web_key(tmp_path, "ops", roles=["admin"])
    ident = _resolver(tmp_path).resolve_web_key(key)
    assert ident is not None and ident.access.is_admin


def test_rotate_invalidates_old_key(tmp_path: Path) -> None:
    k1 = issue_web_key(tmp_path, "bob", roles=["user"])
    other = issue_web_key(tmp_path, "bob", label="acp")
    k2 = issue_web_key(tmp_path, "bob", roles=["user"], rotate=True)
    assert k1 != k2
    r = _resolver(tmp_path)
    assert r.resolve_web_key(k1) is None  # old key dead
    assert r.resolve_web_key(other) is None
    assert (r.resolve_web_key(k2) or None) and r.resolve_web_key(k2).canonical == "bob"
    bob = next(p for p in _people(tmp_path) if p["canonical"] == "bob")
    webkeys = [a for a in bob["aliases"] if a.startswith(WEB_KEY_ALIAS_PREFIX)]
    assert len(webkeys) == 1  # exactly one — no accumulation


def test_revoke_drops_key_but_keeps_roles(tmp_path: Path) -> None:
    key = issue_web_key(tmp_path, "carol", roles=["user"])
    assert _resolver(tmp_path).resolve_web_key(key) is not None
    assert revoke_web_key(tmp_path, "carol") is True
    r = _resolver(tmp_path)
    assert r.resolve_web_key(key) is None  # key gone
    assert r.identity("carol").access.is_authorized  # roles intact
    # idempotent: nothing left to revoke
    assert revoke_web_key(tmp_path, "carol") is False
    assert revoke_web_key(tmp_path, "nobody") is False


def test_issue_preserves_existing_person_fields(tmp_path: Path) -> None:
    k1 = issue_web_key(tmp_path, "dave", roles=["user"])
    # Operator adds fields after the first issue.
    p = tmp_path / "state" / "identities.yaml"
    doc = yaml.safe_load(p.read_text())
    dave = next(x for x in doc["people"] if x["canonical"] == "dave")
    dave["display_name"] = "Dave"
    dave["aliases"].append("slack-U999")
    p.write_text(yaml.safe_dump(doc))
    # Rotate (no roles arg → leave access untouched).
    k2 = issue_web_key(tmp_path, "dave", rotate=True)
    r = _resolver(tmp_path)
    ident = r.resolve_web_key(k2)
    assert ident is not None and ident.canonical == "dave"
    assert ident.display_name == "Dave"  # preserved
    assert "slack-U999" in ident.aliases  # preserved
    assert ident.access.is_authorized  # untouched
    assert r.resolve_web_key(k1) is None  # old key dead


def test_issue_key_factory_injection(tmp_path: Path) -> None:
    # Deterministic key for the test; proves the hash, not the raw, is stored.
    key = issue_web_key(tmp_path, "ed", roles=["user"], key_factory=lambda: "fixed-key-123")
    assert key == "fixed-key-123"
    assert _resolver(tmp_path).resolve_web_key("fixed-key-123").canonical == "ed"


def test_second_key_does_not_invalidate_first(tmp_path: Path) -> None:
    first = issue_web_key(tmp_path, "alice", roles=["user", "admin"], label="web")
    resolver = _resolver(tmp_path)
    second = issue_web_key(tmp_path, "alice", label="acp")
    for key in (first, second):
        identity = resolver.resolve_web_key(key)
        assert identity is not None and identity.canonical == "alice"
        assert identity.access.roles == ("user", "admin")
    assert set(resolver.identity("alice").web_key_labels.values()) == {"web", "acp"}
    assert resolver.has_web_keys()
    assert revoke_web_key(tmp_path, "alice", label="acp", allow_last=False)
    assert resolver.resolve_web_key(second) is None
    assert resolver.resolve_web_key(first).canonical == "alice"
    assert resolver.has_web_keys()
    assert not revoke_web_key(tmp_path, "alice", label="unknown")
    assert resolver.resolve_web_key(first).canonical == "alice"
    assert revoke_web_key(tmp_path, "alice", label="web")
    assert resolver.resolve_web_key(first) is None
    assert not resolver.has_web_keys()
    assert resolver.web_gate_latched()


def test_labels_and_header_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state" / "identities.yaml"
    path.parent.mkdir()
    header = "# Operator identity registry\n# Preserve this documentation.\n\n"
    aliases = [hash_web_key("old-web"), hash_web_key("old-acp")]
    path.write_text(header + yaml.safe_dump({"people": [{
        "canonical": "alice", "aliases": aliases + ["email:alice@example.com"],
    }]}))
    assert _resolver(tmp_path).identity("alice").web_key_labels == dict(zip(aliases, ["legacy-1", "legacy-2"]))
    issue_web_key(tmp_path, "alice", label="phone")
    assert revoke_web_key(tmp_path, "alice", label="legacy-1")
    assert path.read_text().startswith(header)
    identity = _resolver(tmp_path).identity("alice")
    assert set(identity.web_key_labels.values()) == {"legacy-2", "phone"}
    assert _resolver(tmp_path).resolve_web_key("old-acp").canonical == "alice"
    assert _people(tmp_path)[0]["web_key_labels"] == identity.web_key_labels


@pytest.mark.parametrize("canonical", ["alice", "bob"])
def test_duplicate_key_hash_rejected_without_write(tmp_path: Path, canonical: str) -> None:
    key = issue_web_key(tmp_path, "alice", label="web")
    path = tmp_path / "state" / "identities.yaml"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="already assigned"):
        issue_web_key(tmp_path, canonical, key_factory=lambda: key, rotate=True)
    assert path.read_bytes() == before
    assert _resolver(tmp_path).resolve_web_key(key).canonical == "alice"


def test_duplicate_metadata_labels_keep_revocation_slots_distinct(tmp_path: Path) -> None:
    path = tmp_path / "state" / "identities.yaml"
    path.parent.mkdir()
    keys = ["first", "second", "third"]
    aliases = [hash_web_key(key) for key in keys]
    path.write_text(yaml.safe_dump({"people": [{
        "canonical": "alice", "aliases": aliases,
        "web_key_labels": dict(zip(aliases, ["legacy-1", " legacy-1 ", "legacy-1"])),
    }]}))
    resolver = _resolver(tmp_path)
    expected = dict(zip(aliases, ["legacy-1", "legacy-2", "legacy-3"]))
    assert resolver.identity("alice").web_key_labels == expected
    assert revoke_web_key(tmp_path, "alice", label="legacy-2")
    assert resolver.resolve_web_key(keys[1]) is None
    for key in (keys[0], keys[2]):
        assert resolver.resolve_web_key(key).canonical == "alice"
    del expected[aliases[1]]
    assert resolver.identity("alice").web_key_labels == expected
    assert _people(tmp_path)[0]["web_key_labels"] == expected


def test_duplicate_label_rejected_without_write(tmp_path: Path) -> None:
    key = issue_web_key(tmp_path, "alice", label="web")
    path = tmp_path / "state" / "identities.yaml"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="label already exists"):
        issue_web_key(tmp_path, "alice", label=" web ", roles=["admin"])
    assert path.read_bytes() == before
    assert _resolver(tmp_path).resolve_web_key(key).canonical == "alice"


@pytest.mark.parametrize("operation", [issue_web_key, revoke_web_key])
@pytest.mark.parametrize("label", ["", "  ", 1, False, [], {}])
def test_invalid_label_rejected_without_write(tmp_path: Path, operation, label) -> None:
    issue_web_key(tmp_path, "alice")
    path = tmp_path / "state" / "identities.yaml"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="label must"):
        operation(tmp_path, "alice", label=label)
    assert path.read_bytes() == before


def test_invalid_rotation_rejected_without_write(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice")
    path = tmp_path / "state" / "identities.yaml"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="rotate must"):
        issue_web_key(tmp_path, "alice", rotate="false")
    assert path.read_bytes() == before


@pytest.mark.parametrize("warm", [False, True])
def test_ambiguous_hash_fails_closed_without_logging_material(tmp_path: Path, caplog, warm: bool) -> None:
    key = issue_web_key(tmp_path, "alice", roles=["user"])
    resolver = _resolver(tmp_path) if warm else IdentityResolver(tmp_path)
    path = tmp_path / "state" / "identities.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["people"].append({"canonical": "bob", "aliases": [hash_web_key(key)], "access": {"roles": ["admin"]}})
    path.write_text(yaml.safe_dump(doc))
    resolver.reload()
    assert resolver.resolve_web_key(key) is None
    assert resolver.web_gate_latched()
    assert "multiple identities" in caplog.text
    assert key not in caplog.text
    assert hash_web_key(key).removeprefix(WEB_KEY_ALIAS_PREFIX) not in caplog.text


def test_key_events_contain_no_material(tmp_path: Path, monkeypatch, caplog) -> None:
    events = []
    monkeypatch.setattr("mimir.identities_populator.log_event_sync", lambda *args, **kwargs: events.append((args, kwargs)))
    first = issue_web_key(tmp_path, "alice", label="web")
    second = issue_web_key(tmp_path, "alice", label="acp")
    revoke_web_key(tmp_path, "alice", label="acp")
    third = issue_web_key(tmp_path, "alice", rotate=True)
    assert [event[1].get("rotated") for event in events] == [False, False, None, True]
    blob = repr(events) + caplog.text
    for key in (first, second, third):
        assert key not in blob
        assert hash_web_key(key).removeprefix(WEB_KEY_ALIAS_PREFIX) not in blob


def test_failed_atomic_write_preserves_keys_and_labels(tmp_path: Path, monkeypatch) -> None:
    key = issue_web_key(tmp_path, "alice", label="web")
    path = tmp_path / "state" / "identities.yaml"
    before = path.read_bytes()

    def fail_replace(source, destination):
        assert Path(source).parent == path.parent
        assert Path(destination) == path
        raise OSError("simulated rename failure")

    monkeypatch.setattr("mimir.identities_populator.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        issue_web_key(tmp_path, "alice", label="acp")
    assert path.read_bytes() == before
    assert list(path.parent.glob(".identities-*.tmp")) == []
    assert _resolver(tmp_path).resolve_web_key(key).web_key_labels == {hash_web_key(key): "web"}
