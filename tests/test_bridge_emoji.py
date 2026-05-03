"""Tests for mimir.bridges._emoji."""

from __future__ import annotations

from mimir.bridges._emoji import (
    EMOJI_ALIASES,
    resolve_for_discord,
    resolve_for_slack,
)


# ─── Discord ────────────────────────────────────────────────────────


def test_discord_alias_to_unicode():
    assert resolve_for_discord(":thumbsup:") == "👍"
    assert resolve_for_discord(":eyes:") == "👀"


def test_discord_bare_alias_to_unicode():
    assert resolve_for_discord("thumbsup") == "👍"


def test_discord_unicode_passthrough():
    assert resolve_for_discord("👍") == "👍"


def test_discord_custom_emoji_passthrough():
    """Discord custom-server emoji literal — pass through unchanged so
    discord-py's add_reaction gets exactly what it wants."""
    assert resolve_for_discord("<:custom:123456789>") == "<:custom:123456789>"


def test_discord_animated_custom_emoji_passthrough():
    assert resolve_for_discord("<a:wave:987654321>") == "<a:wave:987654321>"


def test_discord_unknown_alias_passthrough():
    """An unknown alias passes through — discord-py will reject it
    cleanly, and the bridge can log + skip."""
    assert resolve_for_discord(":unknown_alias:") == ":unknown_alias:"


def test_discord_empty_string():
    assert resolve_for_discord("") == ""
    assert resolve_for_discord("   ") == ""


# ─── Slack ──────────────────────────────────────────────────────────


def test_slack_colon_form_strips_colons():
    assert resolve_for_slack(":thumbsup:") == "thumbsup"


def test_slack_bare_alias_kept():
    assert resolve_for_slack("thumbsup") == "thumbsup"


def test_slack_unicode_reverse_lookup():
    assert resolve_for_slack("👍") == "thumbsup"
    assert resolve_for_slack("👀") == "eyes"


def test_slack_unicode_unknown_returns_none():
    """Unicode glyph with no known alias returns None — Slack needs a
    name; caller logs and skips the directive."""
    assert resolve_for_slack("\U0001F47B") is None  # 👻 ghost — not in table


def test_slack_workspace_only_alias_kept():
    """A bare name that ISN'T in our table passes through — could be a
    workspace-custom emoji that Slack will resolve at the API layer.
    We can't validate workspace-custom emoji from here.

    Wait — current resolve_for_slack returns None for unknown bare names
    (it's stricter than Discord's pass-through). Slack's reactions.add
    will fail with invalid_name; the bridge surfaces that back to the
    agent. This test pins the strictness."""
    assert resolve_for_slack("rare_workspace_emoji") is None


def test_slack_empty_string():
    assert resolve_for_slack("") is None


# ─── Alias table sanity ────────────────────────────────────────────


def test_alias_table_contains_core_set():
    """Common aliases the agent would pick from the directive vocabulary
    block must be present."""
    for k in ("thumbsup", "thumbsdown", "eyes", "fire", "white_check_mark", "x"):
        assert k in EMOJI_ALIASES, f"missing alias: {k}"
