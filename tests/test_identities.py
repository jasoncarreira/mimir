"""IdentityResolver — YAML loading + alias resolution (FUTURE_WORK §6.1)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from mimir.identities import IdentityResolver


def _write_identities(tmp_path: Path, body: str) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "identities.yaml").write_text(dedent(body), encoding="utf-8")
    r = IdentityResolver(home=tmp_path)
    r.reload()
    return r


def test_missing_file_yields_empty_resolver(tmp_path: Path):
    r = IdentityResolver(home=tmp_path)
    loaded = r.reload()
    assert loaded == 0
    assert r.resolve("anything") == "anything"
    assert r.resolve(None) is None
    assert r.display_name("anything") is None
    assert r.alias_count() == 0


def test_loads_single_identity(tmp_path: Path):
    r = _write_identities(
        tmp_path,
        """\
        people:
          - canonical: alice
            display_name: Alice Smith
            aliases:
              - slack-U123ABC
              - discord-456789
            notes: Eng team lead
        """,
    )
    assert r.alias_count() == 2
    assert r.resolve("slack-U123ABC") == "alice"
    assert r.resolve("discord-456789") == "alice"
    assert r.display_name("slack-U123ABC") == "Alice Smith"
    assert r.display_name("discord-456789") == "Alice Smith"
    identities = r.all_identities()
    assert len(identities) == 1
    assert identities[0].notes == "Eng team lead"


def test_resolve_falls_through_unknown(tmp_path: Path):
    r = _write_identities(
        tmp_path,
        """\
        people:
          - canonical: alice
            aliases: [slack-U123]
        """,
    )
    # Known alias resolves.
    assert r.resolve("slack-U123") == "alice"
    # Unknown alias falls through to itself.
    assert r.resolve("slack-UNOPE") == "slack-UNOPE"
    # None stays None.
    assert r.resolve(None) is None


def test_cross_platform_canonical(tmp_path: Path):
    """Alice on slack and Alice on discord both resolve to the same
    canonical — this is the headline use case."""
    r = _write_identities(
        tmp_path,
        """\
        people:
          - canonical: alice
            aliases: [slack-U123, discord-456]
          - canonical: bob
            aliases: [slack-U777]
        """,
    )
    assert r.resolve("slack-U123") == r.resolve("discord-456") == "alice"
    assert r.resolve("slack-U777") == "bob"
    # Different canonicals don't collide.
    assert r.resolve("alice") == "alice"  # canonical itself, not in alias_map
    assert r.resolve("bob") == "bob"


def test_malformed_entry_skipped_others_load(tmp_path: Path):
    r = _write_identities(
        tmp_path,
        """\
        people:
          - canonical: alice
            aliases: [slack-U123]
          - aliases: [discord-orphan]      # missing canonical, skip
          - canonical: ""                  # empty canonical, skip
            aliases: [slack-empty]
          - "not a dict"                    # not a dict, skip
          - canonical: bob
            aliases: [slack-U777, discord-999]
        """,
    )
    # alice + bob loaded; the malformed entries are gone.
    assert r.alias_count() == 3
    assert r.resolve("slack-U123") == "alice"
    assert r.resolve("slack-U777") == "bob"
    assert r.resolve("discord-999") == "bob"
    # The orphan alias from the skipped entry didn't make it in.
    assert r.resolve("discord-orphan") == "discord-orphan"


def test_unparseable_yaml_keeps_prior_state(tmp_path: Path):
    """Unparseable YAML on reload shouldn't nuke the existing alias_map.
    Better to keep stale-but-valid mappings than to lose all identity
    information when someone fat-fingers an edit."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text(
        dedent(
            """\
            people:
              - canonical: alice
                aliases: [slack-U123]
            """
        )
    )
    r = IdentityResolver(home=tmp_path)
    r.reload()
    assert r.resolve("slack-U123") == "alice"

    # Now break the file mid-edit (unbalanced quotes).
    (state / "identities.yaml").write_text("people:\n  - canonical: 'bob\n")
    loaded = r.reload()
    # Loaded count reflects still-valid prior state.
    assert loaded == 1
    assert r.resolve("slack-U123") == "alice"  # unchanged


def test_duplicate_alias_last_wins(tmp_path: Path):
    """Two canonicals claiming the same alias — last wins, but a warning
    fires (verified out-of-band; here we just confirm the behavior is
    deterministic)."""
    r = _write_identities(
        tmp_path,
        """\
        people:
          - canonical: alice
            aliases: [shared-alias-x]
          - canonical: bob
            aliases: [shared-alias-x]
        """,
    )
    assert r.resolve("shared-alias-x") == "bob"


def test_reload_picks_up_changes(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    yaml_path = state / "identities.yaml"
    yaml_path.write_text(
        dedent(
            """\
            people:
              - canonical: alice
                aliases: [slack-U123]
            """
        )
    )
    r = IdentityResolver(home=tmp_path)
    r.reload()
    assert r.resolve("slack-U123") == "alice"
    assert r.resolve("discord-456") == "discord-456"  # not yet known

    # Add a discord alias to alice.
    yaml_path.write_text(
        dedent(
            """\
            people:
              - canonical: alice
                aliases: [slack-U123, discord-456]
            """
        )
    )
    r.reload()
    assert r.resolve("discord-456") == "alice"


def test_top_level_not_a_dict_treated_empty(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "identities.yaml").write_text("- just a list\n- not a dict\n")
    r = IdentityResolver(home=tmp_path)
    loaded = r.reload()
    assert loaded == 0
    assert r.resolve("anything") == "anything"


def test_aliases_field_must_be_list(tmp_path: Path):
    """If aliases is a string instead of a list, that identity loads
    with no aliases (the canonical is registered but unreachable via
    alias lookup)."""
    r = _write_identities(
        tmp_path,
        """\
        people:
          - canonical: alice
            aliases: "slack-U123"   # string, not a list — invalid
        """,
    )
    # No aliases registered for alice.
    assert r.resolve("slack-U123") == "slack-U123"
    # But the identity itself is loaded.
    assert any(i.canonical == "alice" for i in r.all_identities())


def test_strips_whitespace_on_canonical_and_aliases(tmp_path: Path):
    r = _write_identities(
        tmp_path,
        """\
        people:
          - canonical: "  alice  "
            aliases:
              - "  slack-U123  "
        """,
    )
    assert r.resolve("slack-U123") == "alice"


def test_email_works_as_alias(tmp_path: Path):
    """Email addresses are valid aliases — the resolver treats every
    alias as opaque, so ``email:user@example.com`` Just Works. Useful
    when an EmailBridge lands and inbound events arrive with
    ``author = email:alice@example.com``, or when the operator wants
    to record an email as a known identifier for cross-reference."""
    r = _write_identities(
        tmp_path,
        """\
        people:
          - canonical: alice
            display_name: Alice Smith
            aliases:
              - slack-U123ABC
              - email:alice@example.com
              - discord-456789
        """,
    )
    assert r.resolve("email:alice@example.com") == "alice"
    assert r.display_name("email:alice@example.com") == "Alice Smith"
    # All three aliases collapse to the same canonical.
    assert (
        r.resolve("slack-U123ABC")
        == r.resolve("email:alice@example.com")
        == r.resolve("discord-456789")
        == "alice"
    )
    # Multiple emails per person — should also Just Work.
    r2 = _write_identities(
        tmp_path,
        """\
        people:
          - canonical: alice
            aliases:
              - email:alice@work.example.com
              - email:alice@personal.example.com
        """,
    )
    assert r2.resolve("email:alice@work.example.com") == "alice"
    assert r2.resolve("email:alice@personal.example.com") == "alice"
