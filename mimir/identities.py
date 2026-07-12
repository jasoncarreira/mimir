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
        access:                             # optional; canonical-level access metadata
          roles: [user]                     # explicit allowlist; omit/empty = unauthorized
        notes: Eng team lead                # optional; surfaces in prompt

    channels:
      - canonical: discord-100000000000000002
        display_name: ops-room
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

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

#: Alias prefix for a person's hashed web API key (per-user auth, github #726).
#: The raw key is NEVER stored — only ``webkey:<sha256hex>`` goes in the
#: person's ``aliases:``, so identities.yaml at rest carries no usable secret.
#: The web auth middleware hashes the incoming ``X-API-Key`` and resolves the
#: resulting ``webkey:`` alias through the same map as every other alias.
WEB_KEY_ALIAS_PREFIX = "webkey:"


def hash_web_key(raw_key: str) -> str:
    """Hash a raw web API key into its ``identities.yaml`` alias form.

    SHA-256 hex. Used both by the auth middleware (to look up the presented
    key) and by the key-mint CLI (to write the alias). Lookup is by hash, so
    the raw secret is never string-compared against stored material."""
    return WEB_KEY_ALIAS_PREFIX + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AccessMetadata:
    """Authorization metadata attached to a canonical identity.

    The resolver treats this as canonical-level state: Slack, Discord,
    email, and other aliases all resolve to the same identity record and
    therefore see the same access metadata.
    """

    roles: tuple[str, ...] = ()
    is_service: bool = False

    def as_dict(self) -> dict[str, object]:
        out = {"roles": list(self.roles)}
        if self.is_service:
            out["is_service"] = True
        return out

    @property
    def is_authorized(self) -> bool:
        # USER-tier inbound access must be explicit. Service metadata describes
        # identity type; it does not make an external service a human user.
        return "user" in self.roles or "admin" in self.roles

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


_KNOWN_ACCESS_VALUES = {"user", "admin", "service"}


@dataclass
class Identity:
    """One canonical identity and its platform aliases."""

    canonical: str
    display_name: str | None = None
    aliases: list[str] = field(default_factory=list)
    notes: str | None = None
    access: AccessMetadata = field(default_factory=AccessMetadata)
    # User-facing web preferences. Kept deliberately generic so new frontend
    # preferences can ride the same identities.yaml field without schema churn.
    prefs: dict[str, object] = field(default_factory=dict)
    # Captured DM channels, keyed by platform (e.g. {"slack": "dm-slack-D…",
    # "discord": "dm-discord-…"}). Auto-populated on first contact per bridge
    # (see ``capture_dm_channel`` in identities_populator) so the agent can
    # reach this person directly without the operator pre-configuring it.
    dm_channels: dict[str, str] = field(default_factory=dict)


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

    @staticmethod
    def _parse_access(raw: object, canonical: str) -> AccessMetadata:
        """Parse optional per-canonical access metadata.

        Bad or unfamiliar shapes fall back to the non-privileged default
        rather than breaking identity loading or accidentally granting admin
        access. Supported shape:

        ``access: {roles: [user|admin]}``
        """
        if raw is None:
            return AccessMetadata()
        if not isinstance(raw, dict):
            log.warning(
                "identities.yaml: %s access is not a map, using default access",
                canonical,
            )
            return AccessMetadata()

        roles: list[str] = []
        malformed = False
        raw_roles = raw.get("roles")
        if raw_roles is None and isinstance(raw.get("role"), str):
            raw_roles = [raw.get("role")]

        if raw_roles is None:
            roles = []
        elif isinstance(raw_roles, list):
            for role in raw_roles:
                if isinstance(role, str) and role.strip() in _KNOWN_ACCESS_VALUES:
                    roles.append(role.strip())
                else:
                    malformed = True
                    log.warning(
                        "identities.yaml: %s — skipping malformed access role: %r",
                        canonical,
                        role,
                    )
        elif isinstance(raw_roles, str) and raw_roles.strip() in _KNOWN_ACCESS_VALUES:
            roles = [raw_roles.strip()]
        else:
            log.warning(
                "identities.yaml: %s access.roles is malformed, using default role",
                canonical,
            )
            malformed = True

        if malformed:
            log.warning(
                "identities.yaml: %s access.roles contained invalid values, "
                "using default-deny access",
                canonical,
            )
            return AccessMetadata()

        # Parse is_service flag (chainlink #864)
        is_service = False
        raw_is_service = raw.get("is_service")
        if isinstance(raw_is_service, bool):
            is_service = raw_is_service

        return AccessMetadata(roles=tuple(roles), is_service=is_service)

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

            access = self._parse_access(raw.get("access"), canonical)

            raw_prefs = raw.get("prefs") or {}
            prefs: dict[str, object] = raw_prefs if isinstance(raw_prefs, dict) else {}
            if raw_prefs and not isinstance(raw_prefs, dict):
                log.warning(
                    "identities.yaml: %s prefs is not a map, ignoring",
                    canonical,
                )

            # dm_channels: platform → mimir channel_id. Liberal-on-read —
            # a malformed map is dropped, not fatal.
            raw_dm = raw.get("dm_channels") or {}
            dm_channels: dict[str, str] = {}
            if isinstance(raw_dm, dict):
                for platform, cid in raw_dm.items():
                    if (
                        isinstance(platform, str)
                        and isinstance(cid, str)
                        and platform.strip()
                        and cid.strip()
                    ):
                        dm_channels[platform.strip()] = cid.strip()
            elif raw_dm:
                log.warning(
                    "identities.yaml: %s dm_channels is not a map, ignoring",
                    canonical,
                )

            identities[canonical] = Identity(
                canonical=canonical,
                display_name=display_name,
                aliases=aliases,
                notes=notes,
                access=access,
                prefs=dict(prefs),
                dm_channels=dm_channels,
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

        # CR2 (memory & retrieval) deferred-fix note: the 6 attribute
        # reassignments below are not atomic relative to each other —
        # a concurrent reader (e.g. ``display_name(author)`` reads
        # ``_alias_map`` then ``_display_names``) can straddle the
        # reassignment and see new ``_alias_map`` against old
        # ``_display_names``. In practice this race is unreachable on
        # the asyncio loop (no awaits between the assigns) and only
        # the scheduler.py:942 worker-thread reload path could trigger
        # it — but that path constructs a *new* IdentityResolver,
        # so it doesn't share state with the agent's main resolver.
        #
        # The clean fix is to bundle into an immutable holder (frozen
        # dataclass) and swap once, but it's a 12-site read-path
        # refactor for a race that hasn't bitten yet. Recording the
        # known residual risk here so the next reviewer doesn't
        # re-derive it from scratch.
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

    def dm_channels(self, author: str | None) -> dict[str, str]:
        """Captured DM channels (platform → channel_id) for ``author``'s
        canonical. ``{}`` when the alias is unknown or none are captured."""
        if author is None:
            return {}
        canonical = self._alias_map.get(author, author)
        ident = self._identities.get(canonical)
        return dict(ident.dm_channels) if ident else {}

    def access_metadata(self, author: str | None) -> AccessMetadata:
        """Access metadata for ``author``'s canonical identity.

        Unknown authors and malformed/missing YAML metadata receive the
        fail-closed default: no roles, therefore unauthorized.
        """
        if author is None:
            return AccessMetadata()
        canonical = self._alias_map.get(author, author)
        ident = self._identities.get(canonical)
        return ident.access if ident else AccessMetadata()

    def has_web_keys(self) -> bool:
        """True if any identity carries a ``webkey:`` alias.

        The auth middleware uses this to fail safe: if per-user keys are
        configured, the web gate activates even when the ``MIMIR_API_KEY``
        master key is unset — so adding users can't leave the server
        unintentionally wide open."""
        return any(alias.startswith(WEB_KEY_ALIAS_PREFIX) for alias in self._alias_map)

    def resolve_web_key(self, raw_key: str | None) -> Identity | None:
        """Resolve a raw web API key to its identity record, or ``None``.

        Hashes the presented key and looks up the ``webkey:<hash>`` alias —
        the same alias map every other platform id flows through. Returns the
        full :class:`Identity` (carrying ``access.roles``) so the caller can
        both attribute the request and authorize it; ``None`` for an unknown
        or empty key. Authorization is the caller's job: a resolved identity
        with no roles is still unauthorized (see ``access.is_authorized``)."""
        if not raw_key:
            return None
        canonical = self._alias_map.get(hash_web_key(raw_key))
        if canonical is None:
            return None
        return self._identities.get(canonical)

    def identity(self, author: str | None) -> Identity | None:
        """Return ``author``'s canonical identity record, if known.

        Unlike ``resolve()``, unknown ids do not fall through as synthetic
        identities. This lets policy callers distinguish an allowlisted person,
        a known-but-unprivileged person, and a completely unknown author.
        """
        if author is None:
            return None
        canonical = self._alias_map.get(author, author)
        return self._identities.get(canonical)

    def access_dict(self, author: str | None) -> dict[str, object]:
        """Dict form of :meth:`access_metadata` for JSON/tool callers."""
        return self.access_metadata(author).as_dict()

    def is_authorized(self, author: str | None) -> bool:
        """Whether ``author`` has an explicit non-empty role grant.

        Identity presence is not authorization: unknown authors and known
        auto-populated identities without roles both return False.
        """
        return self.access_metadata(author).is_authorized

    def is_admin(self, author: str | None) -> bool:
        """Whether ``author`` has the explicit ``admin`` role."""
        return self.access_metadata(author).is_admin

    def dm_channel(self, author: str | None, platform: str | None = None) -> str | None:
        """The captured DM ``channel_id`` for ``author`` on ``platform``
        (``"slack"`` / ``"discord"``). With no platform, returns the sole
        captured DM if exactly one is known, else ``None``."""
        chans = self.dm_channels(author)
        if not chans:
            return None
        if platform:
            return chans.get(platform)
        if len(chans) == 1:
            return next(iter(chans.values()))
        return None

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
