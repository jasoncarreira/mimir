"""Per-user web API key issuance + resolution (github #726 auth core)."""

from __future__ import annotations

from pathlib import Path

import yaml

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
    k2 = issue_web_key(tmp_path, "bob", roles=["user"])
    assert k1 != k2
    r = _resolver(tmp_path)
    assert r.resolve_web_key(k1) is None  # old key dead
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
    k2 = issue_web_key(tmp_path, "dave")
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
