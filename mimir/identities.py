"""Identity reconciliation (FUTURE_WORK §6.1).

Operator-managed alias map at ``<home>/state/identities.yaml`` that
collapses platform-specific author ids onto a single canonical
identity. Used by ``MessageBuffer.cross_author_messages`` so a turn
for Alice on Slack pulls her Discord public history (and vice versa).

Schema (full example):

    people:
      - canonical: alice                    # short id used as the matching key
        display_name: Alice Smith           # optional; for prompt rendering
        aliases:
          - slack-U123ABC                   # Slack user id (xoxb users.list)
          - discord-456789                  # Discord numeric user id
          - bsky:alice.bsky.social          # Bluesky handle
          - email:alice@example.com         # email address
        notes: Eng team lead                # optional; surfaces in prompt

Alias prefix convention (informational — the resolver treats every alias
as an opaque string, so the convention is for human readability):
- ``slack-<user_id>``         hyphen separator (id is alphanumeric)
- ``discord-<numeric_id>``    hyphen separator (id is numeric)
- ``bsky:<handle>``           colon — handle contains dots
- ``email:<address>``         colon — address contains @ and dots

Design tenets:
- **Resolver-less callers behave identically to today.** A None resolver
  (file missing, deployment without identities.yaml) makes ``resolve``
  return its input unchanged. Every code path that uses the resolver
  must tolerate ``None``.
- **Liberal on read.** Malformed entries log a warning and skip; the
  rest of the file still parses. One bad row doesn't break the resolver.
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
        if not isinstance(people, list):
            log.warning(
                "identities.yaml: expected top-level 'people' list, got %s — "
                "treating as empty",
                type(people).__name__,
            )
            self._alias_map = {}
            self._display_names = {}
            self._identities = {}
            return 0

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

        self._alias_map = alias_map
        self._display_names = display_names
        self._identities = identities
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
