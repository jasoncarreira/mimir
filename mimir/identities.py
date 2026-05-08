"""Identity reconciliation (FUTURE_WORK §6.1).

Operator-managed alias map at ``<home>/state/identities.yaml`` that
collapses platform-specific author ids onto a single canonical
identity. Used by ``MessageBuffer.cross_author_messages`` so a turn
for Alice on Slack pulls her Discord public history (and vice versa).

The same file also carries a parallel ``channels:`` section — canonical
channel id → display name + kind + aliases. Used by Phase C
cross-channel rendering and the Phase B identity-lookup skill (chainlink
#40).

Schema (full example)::

    people:
      - canonical: alice                    # short id used as the matching key
        display_name: Alice Smith           # optional; for prompt rendering
        aliases:
          - slack-U123ABC                   # Slack user id (xoxb users.list)
          - discord-456789                  # Discord numeric user id
          - bsky:alice.bsky.social          # Bluesky handle
          - email:alice@example.com         # email address
        notes: Eng team lead                # optional; surfaces in prompt

    channels:
      - canonical: discord-1500672382166110321
        display_name: jason-mimir
        kind: public                        # public | dm | guild-meta
        aliases: []                         # optional; rare for channels
        notes: Primary operator channel     # optional

Alias prefix convention (informational — the resolver treats every alias
as an opaque string, so the convention is for human readability):
- ``slack-<user_id>``         hyphen separator (id is alphanumeric)
- ``discord-<numeric_id>``    hyphen separator (id is numeric)
- ``bsky:<handle>``           colon — handle contains dots
- ``email:<address>``         colon — address contains @ and dots

Channel ``kind`` enumerates how the channel surfaces in cross-channel
pulls: ``public`` channels participate, ``dm`` channels never do
(redundant with ``_is_private_channel`` — channel ``kind`` is for
operator labelling and the lookup skill, not gating), and
``guild-meta`` is non-message infrastructure (server-wide audit logs,
threads index) that's listed for completeness but never appears as a
message channel. Unknown values are accepted and stored verbatim so
new bridge kinds don't require a code change.

Design tenets:
- **Resolver-less callers behave identically to today.** A None resolver
  (file missing, deployment without identities.yaml) makes ``resolve``
  return its input unchanged. Every code path that uses the resolver
  must tolerate ``None``.
- **Liberal on read.** Malformed entries log a warning and skip; the
  rest of the file still parses. One bad row doesn't break the resolver.
- **Backwards-compatible schema growth.** ``channels:`` is optional;
  files with only ``people:`` continue to load identically. People-only
  callers don't need to know channels exist.
- **System-facing, not agent-facing.** Operator and CLI write this
  file. The agent doesn't have a tool to mutate it. Identity lookups
  happen inside the runtime — the agent just sees the cross-channel
  pull "work" (or not) on its prompt.

Privacy layering: the DM rule (§5.4) wins regardless of identity
resolution. ``cross_author_messages`` already filters
``_is_private_channel(msg.channel_id)`` *before* checking authors —
identity reconciliation never lifts DM content into a non-DM channel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass
class Identity:
    """One canonical identity and its platform aliases."""

    canonical: str
    display_name: str | None = None
    aliases: list[str] = field(default_factory=list)
    notes: str | None = None


@dataclass
class Channel:
    """One canonical channel and its (rare) aliases.

    ``kind`` is the operator-set label — ``public``, ``dm``,
    ``guild-meta``, or any string the operator wants. Unknown values
    are passed through verbatim so new bridge kinds don't require a
    code change. Privacy gating still goes through
    ``_is_private_channel`` regardless of ``kind``; this field is for
    labelling and the Phase B lookup skill.
    """

    canonical: str
    display_name: str | None = None
    kind: str | None = None
    aliases: list[str] = field(default_factory=list)
    notes: str | None = None


class IdentityResolver:
    """Loads ``<home>/state/identities.yaml`` and answers
    alias → canonical queries.

    Construct once at server startup. Call ``reload()`` to re-read the
    file on demand (e.g. after the operator edits it). ``resolve`` and
    ``display_name`` are zero-allocation lookups against an in-memory
    dict — safe to call on every cross-channel pull.
    """

    def __init__(self, home: Path) -> None:
        self._yaml_path = home / "state" / "identities.yaml"
        self._alias_map: dict[str, str] = {}
        self._display_names: dict[str, str] = {}  # canonical → display_name
        self._identities: dict[str, Identity] = {}  # canonical → Identity
        # Channels (chainlink #40 Phase A — backwards-compat: empty when
        # the YAML has no ``channels:`` section).
        self._channel_alias_map: dict[str, str] = {}  # alias → canonical
        self._channel_display_names: dict[str, str] = {}
        self._channels: dict[str, Channel] = {}

    def reload(self) -> int:
        """Re-read the YAML file. Returns the number of aliases loaded.

        Missing file → resolver is empty (not an error). Unparseable YAML
        → logs a warning and leaves the existing state in place (better to
        keep working with a stale-but-valid map than to nuke it).
        """
        if not self._yaml_path.is_file():
            self._alias_map = {}
            self._display_names = {}
            self._identities = {}
            self._channel_alias_map = {}
            self._channel_display_names = {}
            self._channels = {}
            return 0

        try:
            text = self._yaml_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("identities.yaml read failed: %s — keeping prior state", exc)
            return len(self._alias_map)

        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            log.warning(
                "identities.yaml parse failed: %s — keeping prior state", exc
            )
            return len(self._alias_map)

        people = doc.get("people") if isinstance(doc, dict) else None
        if people is None:
            # File present but no 'people:' key — treat as empty people
            # list so a channels-only file is valid.
            people = []
        elif not isinstance(people, list):
            log.warning(
                "identities.yaml: expected top-level 'people' list, got %s — "
                "treating as empty",
                type(people).__name__,
            )
            people = []

        alias_map: dict[str, str] = {}
        display_names: dict[str, str] = {}
        identities: dict[str, Identity] = {}

        for raw in people:
            if not isinstance(raw, dict):
                log.warning("identities.yaml: skipping non-dict entry: %r", raw)
                continue
            canonical = raw.get("canonical")
            if not isinstance(canonical, str) or not canonical.strip():
                log.warning(
                    "identities.yaml: skipping entry without 'canonical' field: %r",
                    raw,
                )
                continue
            canonical = canonical.strip()

            display_name = raw.get("display_name")
            if display_name is not None and not isinstance(display_name, str):
                log.warning(
                    "identities.yaml: %s display_name is not a string, ignoring",
                    canonical,
                )
                display_name = None

            raw_aliases = raw.get("aliases") or []
            if not isinstance(raw_aliases, list):
                log.warning(
                    "identities.yaml: %s aliases is not a list, ignoring",
                    canonical,
                )
                raw_aliases = []

            aliases: list[str] = []
            for alias in raw_aliases:
                if not isinstance(alias, str) or not alias.strip():
                    log.warning(
                        "identities.yaml: %s — skipping non-string/empty alias: %r",
                        canonical,
                        alias,
                    )
                    continue
                alias = alias.strip()
                if alias in alias_map and alias_map[alias] != canonical:
                    log.warning(
                        "identities.yaml: alias %r already maps to %r, "
                        "overwriting with %r (last-wins)",
                        alias,
                        alias_map[alias],
                        canonical,
                    )
                alias_map[alias] = canonical
                aliases.append(alias)

            notes = raw.get("notes")
            if notes is not None and not isinstance(notes, str):
                notes = None

            identities[canonical] = Identity(
                canonical=canonical,
                display_name=display_name,
                aliases=aliases,
                notes=notes,
            )
            if display_name:
                display_names[canonical] = display_name

        # Channels section — independent parse, identical liberal-on-read
        # posture. Missing/non-list = empty (backwards-compat).
        channels_raw = doc.get("channels") if isinstance(doc, dict) else None
        if channels_raw is None:
            channels_raw = []
        elif not isinstance(channels_raw, list):
            log.warning(
                "identities.yaml: expected top-level 'channels' list, got "
                "%s — treating as empty",
                type(channels_raw).__name__,
            )
            channels_raw = []

        channel_alias_map: dict[str, str] = {}
        channel_display_names: dict[str, str] = {}
        channels: dict[str, Channel] = {}

        for raw in channels_raw:
            if not isinstance(raw, dict):
                log.warning(
                    "identities.yaml: skipping non-dict channel entry: %r", raw
                )
                continue
            canonical = raw.get("canonical")
            if not isinstance(canonical, str) or not canonical.strip():
                log.warning(
                    "identities.yaml: skipping channel without 'canonical' "
                    "field: %r",
                    raw,
                )
                continue
            canonical = canonical.strip()

            display_name = raw.get("display_name")
            if display_name is not None and not isinstance(display_name, str):
                log.warning(
                    "identities.yaml: channel %s display_name is not a string,"
                    " ignoring",
                    canonical,
                )
                display_name = None

            kind = raw.get("kind")
            if kind is not None and not isinstance(kind, str):
                log.warning(
                    "identities.yaml: channel %s kind is not a string, "
                    "ignoring",
                    canonical,
                )
                kind = None

            raw_aliases = raw.get("aliases") or []
            if not isinstance(raw_aliases, list):
                log.warning(
                    "identities.yaml: channel %s aliases is not a list, "
                    "ignoring",
                    canonical,
                )
                raw_aliases = []

            ch_aliases: list[str] = []
            # Canonical id is its own alias so resolve_channel(canonical)
            # round-trips. Operators rarely need extra aliases for
            # channels, but the shape mirrors people: for symmetry.
            channel_alias_map[canonical] = canonical
            for alias in raw_aliases:
                if not isinstance(alias, str) or not alias.strip():
                    log.warning(
                        "identities.yaml: channel %s — skipping non-string/"
                        "empty alias: %r",
                        canonical,
                        alias,
                    )
                    continue
                alias = alias.strip()
                if (
                    alias in channel_alias_map
                    and channel_alias_map[alias] != canonical
                ):
                    log.warning(
                        "identities.yaml: channel alias %r already maps to "
                        "%r, overwriting with %r (last-wins)",
                        alias,
                        channel_alias_map[alias],
                        canonical,
                    )
                channel_alias_map[alias] = canonical
                ch_aliases.append(alias)

            notes = raw.get("notes")
            if notes is not None and not isinstance(notes, str):
                notes = None

            channels[canonical] = Channel(
                canonical=canonical,
                display_name=display_name,
                kind=kind,
                aliases=ch_aliases,
                notes=notes,
            )
            if display_name:
                channel_display_names[canonical] = display_name

        self._alias_map = alias_map
        self._display_names = display_names
        self._identities = identities
        self._channel_alias_map = channel_alias_map
        self._channel_display_names = channel_display_names
        self._channels = channels
        return len(self._alias_map)

    def resolve(self, author: str | None) -> str | None:
        """Map ``author`` (a platform-prefixed id) to canonical. Unknown
        ids fall through unchanged. ``None`` stays ``None``."""
        if author is None:
            return None
        return self._alias_map.get(author, author)

    def display_name(self, author: str | None) -> str | None:
        """Return the display name for ``author``'s canonical, or
        ``None`` if the alias is unknown or has no display name set."""
        if author is None:
            return None
        canonical = self._alias_map.get(author, author)
        return self._display_names.get(canonical)

    def all_identities(self) -> list[Identity]:
        """All loaded identities. Order is YAML file order."""
        return list(self._identities.values())

    def alias_count(self) -> int:
        return len(self._alias_map)

    # Channel-side accessors (chainlink #40 Phase A). Mirror the
    # people-side shape: ``resolve_channel`` falls through unknown ids
    # unchanged so callers don't need to special-case "not in the
    # registry yet."

    def resolve_channel(self, channel_id: str | None) -> str | None:
        """Map ``channel_id`` (any alias) to its canonical id. Unknown
        ids fall through unchanged. ``None`` stays ``None``."""
        if channel_id is None:
            return None
        return self._channel_alias_map.get(channel_id, channel_id)

    def channel_display_name(self, channel_id: str | None) -> str | None:
        """Return the display name for ``channel_id``'s canonical, or
        ``None`` if the id is unknown or has no display name set."""
        if channel_id is None:
            return None
        canonical = self._channel_alias_map.get(channel_id, channel_id)
        return self._channel_display_names.get(canonical)

    def channel(self, channel_id: str | None) -> Channel | None:
        """Return the full ``Channel`` record for ``channel_id`` (via
        any alias), or ``None`` if not registered."""
        if channel_id is None:
            return None
        canonical = self._channel_alias_map.get(channel_id)
        if canonical is None:
            return None
        return self._channels.get(canonical)

    def all_channels(self) -> list[Channel]:
        """All loaded channels. Order is YAML file order."""
        return list(self._channels.values())

    def channel_count(self) -> int:
        return len(self._channels)
