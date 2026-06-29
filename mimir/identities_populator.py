"""Bridge populators for ``state/identities.yaml`` (chainlink #40 Phase D).

Daily-cadence (operator-scheduled) scrape of bridge metadata that
fills in the ``people:`` and ``channels:`` sections of
``state/identities.yaml`` so the cross-channel content surfacing
(chainlink #43, Phase C) and the identity-lookup skill (chainlink
#42, Phase B) have a populated registry to read.

Idempotency contract — re-running on a fresh YAML is a no-op once
populated:

- **People.** Match by alias (``discord-<id>`` / ``slack-<id>``).
  If the alias is already mapped to a canonical, the existing entry
  is preserved verbatim — operator-set fields (``display_name``,
  ``notes``, custom aliases) are never overwritten. If the alias is
  unknown, a new entry is created with ``canonical=<alias>`` (the
  alias itself doubles as the canonical until an operator merges
  cross-platform identities by hand).
- **Channels.** Match by canonical (``discord-<channel_id>`` /
  ``slack-<channel_id>``). New channels get a full record (display
  name + kind + populator notes); existing channels only have
  *missing* fields filled (a blank ``display_name`` populates,
  but a non-empty one is preserved).

Operator-overridable rule: the populator never *overwrites* an
already-set string field. It only fills blanks.

This module imports bridge types lazily so non-bridge deployments
(benchmark, web stub) don't pay the import cost. The bridges
themselves don't need to know about this module — populators
read the public client surface (``DiscordBridge._client.guilds``,
``SlackBridge._app.client.{users_list, conversations_list}``) the
same way fetch_history does.
"""

from __future__ import annotations

import functools
import logging
import os
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

import yaml

from .event_logger import log_event, log_event_sync
from .identities import WEB_KEY_ALIAS_PREFIX, hash_web_key

log = logging.getLogger(__name__)

PairingRequestStatus = Literal["changed", "unchanged", "capped"]


# ---------------------------------------------------------------------------
# YAML merge — the heart of the idempotency contract.
# ---------------------------------------------------------------------------


def _extract_header(text: str) -> str:
    """Return the leading comment block of a YAML file, verbatim.

    "Leading comment block" = every line from the start of the file up
    to (but not including) the first non-blank, non-comment line. Blank
    lines that precede or sit between comment lines are kept; the
    trailing blank that conventionally separates a header from the
    document body is also kept (so the round-tripped file has the same
    visual shape).

    This is the closest PyYAML-only round-trip we can do: top-of-file
    comments — which is where ``identities.yaml``'s schema doc lives —
    survive a populator write. Comments *inside* document entries
    (``aliases:`` lists, per-record ``# DO NOT remove`` annotations)
    do NOT survive — see ``_load_yaml`` docstring.
    """
    header_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped == "" or stripped == "\n":
            header_lines.append(line)
            continue
        break
    return "".join(header_lines)


def _load_yaml(path: Path) -> tuple[dict[str, Any], str]:
    """Read identities.yaml; return ``(doc, header_text)``.

    ``doc`` is the parsed YAML mapping (empty dict for missing /
    non-mapping files). ``header_text`` is the leading comment block —
    every line from the start of the file through the last consecutive
    comment / blank line before the first document content. The header
    is preserved verbatim and prepended on write back, so the
    operator's schema documentation at the top of ``identities.yaml``
    survives a populator run.

    Limitations (PyYAML only does so much):

    - Top-of-file comments survive. Operators editing the schema header
      can rely on it.
    - Comments *inside* document entries (e.g. an inline
      ``# DO NOT remove`` next to a specific alias) are dropped on
      write. If that ever becomes load-bearing, the right escalation
      is ``ruamel.yaml`` round-trip mode (carries inline comments) —
      a new dependency, deferred until a real use case shows up.
    - Treats missing / unparseable / non-mapping files as empty so a
      fresh deployment starts clean.
    """
    if not path.is_file():
        return {}, ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("identities.yaml read failed: %s — treating as empty", exc)
        return {}, ""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        log.warning("identities.yaml parse failed: %s — refusing to overwrite", exc)
        # Returning a sentinel telling the caller to abort (preserve the
        # operator's broken-but-recoverable file rather than nuke it).
        raise
    header = _extract_header(text)
    if not isinstance(doc, dict):
        return {}, header
    return doc, header


def _strip_value(v: Any) -> Any:
    """If ``v`` is a string, strip whitespace; else return unchanged."""
    return v.strip() if isinstance(v, str) else v


def _find_person(
    people: Iterable[Any], author_or_canonical: str
) -> dict[str, Any] | None:
    for entry in people:
        if not isinstance(entry, dict):
            continue
        canonical = entry.get("canonical")
        if isinstance(canonical, str) and canonical.strip() == author_or_canonical:
            return entry
        if any(
            isinstance(alias, str) and alias.strip() == author_or_canonical
            for alias in (entry.get("aliases") or [])
        ):
            return entry
    return None


# All in-process writers of ``state/identities.yaml`` share this lock so the
# read → mutate → write is atomic across the live first-contact DM capture
# (``capture_dm_channel``) and the scheduled populator (``merge_into_yaml``) —
# otherwise they lost-update each other. Unique temp files (below) additionally
# remove the shared-``.tmp`` rename race. RLock in case a future caller nests;
# today neither writer calls the other.
_IDENTITIES_WRITE_LOCK = threading.RLock()


def _serialized_identities_write(fn):
    """Hold ``_IDENTITIES_WRITE_LOCK`` for the whole call — decorate every
    function that does a read-modify-write of ``state/identities.yaml``."""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _IDENTITIES_WRITE_LOCK:
            return fn(*args, **kwargs)
    return _wrapper


def _atomic_write_identities(yaml_path: Path, header: str, doc: dict) -> None:
    """Write ``header + safe_dump(doc)`` to ``yaml_path`` via a UNIQUE temp
    file + atomic rename, so concurrent writers never share (and clobber) a
    fixed ``.tmp`` path. Caller must hold ``_IDENTITIES_WRITE_LOCK``."""
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=1_000)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(yaml_path.parent), prefix=".identities-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(header + body)
        os.replace(tmp_name, yaml_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _default_web_key() -> str:
    """A URL-safe random web API key (~256 bits). Shown once, never stored raw."""
    return secrets.token_urlsafe(32)


@_serialized_identities_write
def issue_web_key(
    home: Path,
    canonical: str,
    *,
    roles: Sequence[str] | None = None,
    key_factory: Callable[[], str] | None = None,
) -> str:
    """Mint (or rotate) a per-user web API key for ``canonical`` and return the
    RAW key — the only moment it is ever recoverable. The caller shows it once
    and distributes it out-of-band; only ``webkey:<sha256>`` is persisted.

    Rotation-safe: any existing ``webkey:`` alias on the person is dropped
    first, so re-issuing immediately invalidates the prior key. Creates the
    person entry if absent. ``roles`` (e.g. ``["user"]`` / ``["admin"]``), when
    given, sets ``access.roles`` so a fresh user is usable in one step; omit to
    leave existing access untouched. Atomic + header-preserving."""
    raw_key = (key_factory or _default_web_key)()
    alias = hash_web_key(raw_key)
    yaml_path = home / "state" / "identities.yaml"
    doc, header = _load_yaml(yaml_path)
    people = doc.get("people")
    if not isinstance(people, list):
        people = []
        doc["people"] = people
    entry = _find_person(people, canonical)
    if entry is None:
        entry = {"canonical": canonical, "aliases": []}
        people.append(entry)
    aliases = entry.get("aliases")
    if not isinstance(aliases, list):
        aliases = []
        entry["aliases"] = aliases
    # Drop any prior web key (rotate): the old key stops resolving immediately.
    aliases[:] = [
        a for a in aliases
        if not (isinstance(a, str) and a.startswith(WEB_KEY_ALIAS_PREFIX))
    ]
    aliases.append(alias)
    if roles is not None:
        entry["access"] = {"roles": [str(role) for role in roles]}
    _atomic_write_identities(yaml_path, header, doc)
    try:
        log_event_sync(
            "identity_web_key_issued",
            canonical=canonical,
            roles=list(roles) if roles is not None else None,
            rotated=True,
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail a key operation
        pass
    return raw_key


@_serialized_identities_write
def revoke_web_key(home: Path, canonical: str) -> bool:
    """Drop ``canonical``'s web key alias so the key stops resolving.

    Returns True if a key was removed, False if the person is unknown or had
    none. Access roles are left intact (revoke a key ≠ deauthorize the person)."""
    yaml_path = home / "state" / "identities.yaml"
    doc, header = _load_yaml(yaml_path)
    people = doc.get("people")
    if not isinstance(people, list):
        return False
    entry = _find_person(people, canonical)
    if entry is None:
        return False
    aliases = entry.get("aliases")
    if not isinstance(aliases, list):
        return False
    before = len(aliases)
    aliases[:] = [
        a for a in aliases
        if not (isinstance(a, str) and a.startswith(WEB_KEY_ALIAS_PREFIX))
    ]
    if len(aliases) == before:
        return False
    _atomic_write_identities(yaml_path, header, doc)
    try:
        log_event_sync("identity_web_key_revoked", canonical=canonical)
    except Exception:  # noqa: BLE001 — telemetry must never fail a key operation
        pass
    return True


@_serialized_identities_write
def set_user_prefs(home: Path, canonical: str, prefs: Mapping[str, Any]) -> bool:
    """Merge web/UI preferences into ``canonical``'s identities.yaml entry.

    Existing aliases, roles, display fields, and unrelated prefs are preserved.
    Returns True when the file changed. Unknown canonicals are not created here —
    preference writes are tied to an authenticated existing identity.
    """
    key = (canonical or "").strip()
    if not key or not isinstance(prefs, Mapping):
        return False

    yaml_path = home / "state" / "identities.yaml"
    doc, header = _load_yaml(yaml_path)
    people = doc.get("people")
    if not isinstance(people, list):
        return False
    entry = _find_person(people, key)
    if entry is None:
        return False

    current = entry.get("prefs")
    if not isinstance(current, dict):
        current = {}
    merged = dict(current)
    changed = False
    for pref_key, pref_value in prefs.items():
        if not isinstance(pref_key, str) or not pref_key.strip():
            continue
        clean_key = pref_key.strip()
        if pref_value is None:
            if clean_key in merged:
                merged.pop(clean_key, None)
                changed = True
        elif merged.get(clean_key) != pref_value:
            merged[clean_key] = pref_value
            changed = True

    if not changed:
        return False
    if merged:
        entry["prefs"] = merged
    else:
        entry.pop("prefs", None)
    _atomic_write_identities(yaml_path, header, doc)
    return True


@_serialized_identities_write
def capture_dm_channel(
    home: Path, author: str, platform: str, dm_channel_id: str
) -> bool:
    """Record a user's DM channel into ``state/identities.yaml`` on first
    contact. Mirrors ``merge_into_yaml``'s match-by-alias + fill-blank
    posture, and shares ``_IDENTITIES_WRITE_LOCK`` with it so the two writers
    can't lost-update each other when the scheduled populator runs
    concurrently.

    Args:
        home: mimir home; YAML lives at ``<home>/state/identities.yaml``.
        author: the platform-prefixed inbound id (e.g. ``slack-U05ABC``,
            ``discord-456789``) — the same value as ``AgentEvent.author``.
        platform: ``"slack"`` / ``"discord"`` (the ``dm_channels`` key).
        dm_channel_id: the mimir DM channel id (``dm-slack-D…`` /
            ``dm-discord-…``) resolved from the bridge.

    Finds the person whose ``aliases`` include ``author`` (or creates a
    new entry keyed by ``author``), then sets ``dm_channels[platform]``
    only if it isn't already set — an existing value (operator- or
    previously-captured) is never overwritten. Atomic, header-preserving,
    unique-temp write; a no-op (no mtime bump) when nothing changed. Returns
    True iff it wrote. Best-effort: callers should not fail a turn on a
    False/raise.
    """
    author = (author or "").strip()
    platform = (platform or "").strip()
    dm_channel_id = (dm_channel_id or "").strip()
    if not (author and platform and dm_channel_id):
        return False

    yaml_path = home / "state" / "identities.yaml"
    doc, header = _load_yaml(yaml_path)

    existing_people = doc.get("people")
    if not isinstance(existing_people, list):
        existing_people = []

    match: dict[str, Any] | None = None
    for entry in existing_people:
        if not isinstance(entry, dict):
            continue
        if any(
            isinstance(a, str) and a.strip() == author
            for a in (entry.get("aliases") or [])
        ):
            match = entry
            break

    changed = False
    if match is None:
        # Brand-new person — canonical defaults to the inbound id, same as
        # the populator does for an unknown alias (operator can merge later).
        match = {"canonical": author, "aliases": [author]}
        existing_people.append(match)
        changed = True

    dm = match.get("dm_channels")
    if not isinstance(dm, dict):
        dm = {}
    if not dm.get(platform):
        dm[platform] = dm_channel_id
        match["dm_channels"] = dm
        changed = True
    elif dm.get(platform) != dm_channel_id:
        # Already captured a different DM channel for this platform — leave
        # it (stable per user; operator authority). Log the drift only.
        log.info(
            "capture_dm_channel: %s already has %s DM %r; not overwriting with %r",
            match.get("canonical", author),
            platform,
            dm.get(platform),
            dm_channel_id,
        )

    if not changed:
        return False

    doc["people"] = existing_people
    _atomic_write_identities(yaml_path, header, doc)
    log.info(
        "captured DM channel for %s on %s: %s",
        match.get("canonical", author), platform, dm_channel_id,
    )
    return True


@_serialized_identities_write
def request_dm_pairing(
    home: Path,
    author: str,
    platform: str,
    dm_channel_id: str,
    *,
    author_display: str | None = None,
    max_pending: int | None = None,
) -> bool:
    """Create or refresh an operator-reviewable pending DM pairing.

    This is deliberately not an approval path: it records the canonical alias,
    captured DM channel, display name if blank, and ``pairing.status=pending``
    only when the identity is not already allowlisted. It never writes
    ``access.roles``, so an unknown DMer cannot self-authorize by triggering
    first contact.
    """
    return request_pairing(
        home,
        author,
        platform,
        channel_id=dm_channel_id,
        author_display=author_display,
        is_dm=True,
        max_pending=max_pending,
    )


@_serialized_identities_write
def request_pairing(
    home: Path,
    author: str,
    platform: str,
    *,
    channel_id: str,
    author_display: str | None = None,
    is_dm: bool = False,
    max_pending: int | None = None,
) -> bool:
    """Create or refresh an operator-reviewable pending pairing.

    Returns True only on a first pending transition or metadata write. New
    pending identities are capped by ``max_pending`` to keep distinct-author
    spam from growing ``identities.yaml`` without bound. Existing identities
    may still be updated so operator-authored entries remain repairable.
    """
    return request_pairing_status(
        home,
        author,
        platform,
        channel_id=channel_id,
        author_display=author_display,
        is_dm=is_dm,
        max_pending=max_pending,
    ) == "changed"


@_serialized_identities_write
def request_pairing_status(
    home: Path,
    author: str,
    platform: str,
    *,
    channel_id: str,
    author_display: str | None = None,
    is_dm: bool = False,
    max_pending: int | None = None,
) -> PairingRequestStatus:
    """Create/refresh a pending pairing and report capped drops explicitly.

    ``request_pairing`` preserves the historical bool API for callers that only
    need to know whether YAML changed. Server-side notification needs to
    distinguish "unchanged duplicate" from "new contact dropped because the
    pending cap is full", so this status API keeps that signal observable.
    """
    author = (author or "").strip()
    platform = (platform or "").strip()
    channel_id = (channel_id or "").strip()
    display = (author_display or "").strip()
    if not (author and platform and channel_id):
        return "unchanged"

    yaml_path = home / "state" / "identities.yaml"
    doc, header = _load_yaml(yaml_path)
    people = doc.get("people")
    if not isinstance(people, list):
        people = []

    match = _find_person(people, author)
    changed = False
    if match is None:
        if max_pending is not None and max_pending >= 0:
            pending_count = 0
            for entry in people:
                if not isinstance(entry, dict):
                    continue
                pairing = entry.get("pairing")
                if isinstance(pairing, dict) and pairing.get("status") == "pending":
                    pending_count += 1
            if pending_count >= max_pending:
                return "capped"
        match = {"canonical": author, "aliases": [author]}
        if display:
            match["display_name"] = display
        people.append(match)
        changed = True

    aliases = match.get("aliases")
    if not isinstance(aliases, list):
        aliases = []
    alias_set = {
        alias.strip() for alias in aliases if isinstance(alias, str) and alias.strip()
    }
    if author not in alias_set:
        aliases.append(author)
        match["aliases"] = aliases
        changed = True

    if display and not match.get("display_name"):
        match["display_name"] = display
        changed = True

    if is_dm:
        dm = match.get("dm_channels")
        if not isinstance(dm, dict):
            dm = {}
        if not dm.get(platform):
            dm[platform] = channel_id
            match["dm_channels"] = dm
            changed = True

    access = match.get("access")
    roles: list[Any] = []
    if isinstance(access, dict):
        raw_roles = access.get("roles") or []
        if isinstance(raw_roles, list):
            roles = raw_roles
        elif isinstance(raw_roles, str):
            roles = [raw_roles]
    if any(isinstance(role, str) and role.strip() for role in roles):
        if changed:
            doc["people"] = people
            _atomic_write_identities(yaml_path, header, doc)
            return "changed"
        return "unchanged"

    pairing = match.get("pairing")
    if not isinstance(pairing, dict):
        pairing = {}
    requested_at = datetime.now(timezone.utc).isoformat()
    pending = {
        "status": "pending",
        "requested_at": requested_at,
        "platform": platform,
        "author": author,
        "channel": channel_id,
        "delivery": "dm" if is_dm else "public_shared_channel",
    }
    if is_dm:
        pending["dm_channel"] = channel_id
    if pairing.get("status") != "pending":
        pairing.update(pending)
        match["pairing"] = pairing
        changed = True
    else:
        # Keep the first requested_at for audit stability; refresh only facts
        # that can be corrected by the bridge layer.
        for key in ("platform", "author", "channel", "delivery", "dm_channel"):
            if key not in pending:
                continue
            if pairing.get(key) != pending[key]:
                pairing[key] = pending[key]
                changed = True
        match["pairing"] = pairing

    if not changed:
        return "unchanged"
    doc["people"] = people
    _atomic_write_identities(yaml_path, header, doc)
    return "changed"


@_serialized_identities_write
def approve_pairing(
    home: Path,
    author_or_canonical: str,
    *,
    roles: Iterable[str] = ("user",),
) -> bool:
    """Approve a pending identity by granting canonical-level access roles.

    The operator supplies an existing alias or canonical id. The write is
    scoped to ``access.roles`` and ``pairing.status``; display names, notes,
    aliases, DM channels, and other operator-authored fields are preserved.
    """
    key = (author_or_canonical or "").strip()
    clean_roles = [
        role.strip()
        for role in roles
        if isinstance(role, str) and role.strip() in {"user", "admin"}
    ]
    if not key or not clean_roles:
        return False

    yaml_path = home / "state" / "identities.yaml"
    doc, header = _load_yaml(yaml_path)
    people = doc.get("people")
    if not isinstance(people, list):
        return False
    match = _find_person(people, key)
    if match is None:
        return False

    changed = False
    access = match.get("access")
    if not isinstance(access, dict):
        access = {}
    if access.get("roles") != clean_roles:
        access["roles"] = clean_roles
        match["access"] = access
        changed = True

    pairing = match.get("pairing")
    if isinstance(pairing, dict) and pairing.get("status") != "approved":
        pairing["status"] = "approved"
        pairing["approved_at"] = datetime.now(timezone.utc).isoformat()
        match["pairing"] = pairing
        changed = True

    if not changed:
        return False
    doc["people"] = people
    _atomic_write_identities(yaml_path, header, doc)
    return True


@_serialized_identities_write
def merge_into_yaml(
    home: Path,
    *,
    people: Iterable[dict[str, Any]],
    channels: Iterable[dict[str, Any]],
    dry_run: bool = False,
) -> dict[str, int]:
    """Merge populator output into ``<home>/state/identities.yaml``.

    Args:
        home: mimir home directory; YAML lives at ``<home>/state/identities.yaml``.
        people: iterable of dicts with at least ``aliases`` (list[str]).
            Optional: ``canonical``, ``display_name``, ``notes``.
        channels: iterable of dicts with at least ``canonical``. Optional:
            ``display_name``, ``kind``, ``aliases``, ``notes``.
        dry_run: when True, the merger computes the merged shape and
            returns counts but does NOT write to disk. Useful for a
            pre-flight ``mimir populate-identities --dry-run``.

    Returns:
        Dict of counts: ``{"people_added", "people_updated",
        "channels_added", "channels_updated", "people_total",
        "channels_total"}``. ``_added`` covers brand-new canonicals;
        ``_updated`` covers existing canonicals that gained aliases or
        had blank fields filled.

    Idempotency: re-running with the same populator output yields
    ``_added == 0`` and ``_updated == 0`` after the first run.
    """
    yaml_path = home / "state" / "identities.yaml"
    doc, header = _load_yaml(yaml_path)

    # ---- people ---------------------------------------------------------
    existing_people = doc.get("people")
    if not isinstance(existing_people, list):
        existing_people = []
    # Build alias → entry index for fast lookup. Last-wins on dupes (matches
    # IdentityResolver behavior).
    alias_to_entry: dict[str, dict[str, Any]] = {}
    for entry in existing_people:
        if not isinstance(entry, dict):
            continue
        for alias in entry.get("aliases") or []:
            if isinstance(alias, str) and alias.strip():
                alias_to_entry[alias.strip()] = entry

    people_added = 0
    people_updated = 0
    for incoming in people:
        if not isinstance(incoming, dict):
            continue
        in_aliases = [
            _strip_value(a)
            for a in (incoming.get("aliases") or [])
            if isinstance(a, str) and a.strip()
        ]
        if not in_aliases:
            continue
        # Find the first existing entry that already has any of these
        # aliases. If multiple match, we trust the first hit (operator's
        # earlier merge of cross-platform identities is authoritative).
        match: dict[str, Any] | None = None
        for alias in in_aliases:
            if alias in alias_to_entry:
                match = alias_to_entry[alias]
                break

        if match is None:
            # Brand new — synthesize an entry. Canonical defaults to the
            # incoming canonical or the first alias.
            canonical = (
                _strip_value(incoming.get("canonical")) or in_aliases[0]
            )
            new_entry: dict[str, Any] = {
                "canonical": canonical,
                "aliases": list(in_aliases),
            }
            display = _strip_value(incoming.get("display_name"))
            if display:
                new_entry["display_name"] = display
            notes = _strip_value(incoming.get("notes"))
            if notes:
                new_entry["notes"] = notes
            existing_people.append(new_entry)
            for alias in in_aliases:
                alias_to_entry[alias] = new_entry
            people_added += 1
        else:
            # Existing canonical — only ADD missing aliases and only FILL
            # missing display_name / notes. Never overwrite.
            changed = False
            current_aliases = match.get("aliases") or []
            if not isinstance(current_aliases, list):
                current_aliases = []
            current_set = {
                a.strip()
                for a in current_aliases
                if isinstance(a, str) and a.strip()
            }
            for alias in in_aliases:
                if alias not in current_set:
                    current_aliases.append(alias)
                    current_set.add(alias)
                    alias_to_entry[alias] = match
                    changed = True
            match["aliases"] = current_aliases

            in_display = _strip_value(incoming.get("display_name"))
            if in_display and not _strip_value(match.get("display_name")):
                match["display_name"] = in_display
                changed = True
            in_notes = _strip_value(incoming.get("notes"))
            if in_notes and not _strip_value(match.get("notes")):
                match["notes"] = in_notes
                changed = True

            if changed:
                people_updated += 1

    # ---- channels -------------------------------------------------------
    existing_channels = doc.get("channels")
    if not isinstance(existing_channels, list):
        existing_channels = []
    by_canonical: dict[str, dict[str, Any]] = {}
    for entry in existing_channels:
        if not isinstance(entry, dict):
            continue
        canonical = _strip_value(entry.get("canonical"))
        if isinstance(canonical, str) and canonical:
            by_canonical[canonical] = entry

    channels_added = 0
    channels_updated = 0
    for incoming in channels:
        if not isinstance(incoming, dict):
            continue
        canonical = _strip_value(incoming.get("canonical"))
        if not canonical or not isinstance(canonical, str):
            continue
        existing = by_canonical.get(canonical)
        if existing is None:
            # Brand new channel — full populator record.
            new_entry: dict[str, Any] = {"canonical": canonical}
            for field in ("display_name", "kind", "notes"):
                v = _strip_value(incoming.get(field))
                if v:
                    new_entry[field] = v
            in_aliases = [
                _strip_value(a)
                for a in (incoming.get("aliases") or [])
                if isinstance(a, str) and a.strip()
            ]
            if in_aliases:
                new_entry["aliases"] = in_aliases
            existing_channels.append(new_entry)
            by_canonical[canonical] = new_entry
            channels_added += 1
        else:
            # Existing — fill missing fields only.
            changed = False
            for field in ("display_name", "kind", "notes"):
                in_v = _strip_value(incoming.get(field))
                if in_v and not _strip_value(existing.get(field)):
                    existing[field] = in_v
                    changed = True
            in_aliases = [
                _strip_value(a)
                for a in (incoming.get("aliases") or [])
                if isinstance(a, str) and a.strip()
            ]
            if in_aliases:
                current = existing.get("aliases") or []
                if not isinstance(current, list):
                    current = []
                current_set = {
                    a.strip()
                    for a in current
                    if isinstance(a, str) and a.strip()
                }
                for alias in in_aliases:
                    if alias not in current_set:
                        current.append(alias)
                        current_set.add(alias)
                        changed = True
                existing["aliases"] = current
            if changed:
                channels_updated += 1

    # ---- write back -----------------------------------------------------
    if not dry_run:
        # Only write if there's actual content to keep — and only when
        # something changed (avoid bumping mtime on a pure no-op run,
        # which can falsely trip file-watch reloaders).
        if people_added or people_updated or channels_added or channels_updated:
            doc["people"] = existing_people
            doc["channels"] = existing_channels
            # Atomic, header-preserving, unique-temp write under the shared
            # ``_IDENTITIES_WRITE_LOCK`` (held by this function's decorator) so
            # a concurrent ``capture_dm_channel`` can't lost-update us and we
            # can't clobber its just-captured dm_channels. The header prepend
            # keeps the operator's schema doc-comment; inline / mid-document
            # comments are NOT preserved — see _load_yaml docstring.
            _atomic_write_identities(yaml_path, header, doc)

    return {
        "people_added": people_added,
        "people_updated": people_updated,
        "people_total": len(existing_people),
        "channels_added": channels_added,
        "channels_updated": channels_updated,
        "channels_total": len(existing_channels),
    }


# ---------------------------------------------------------------------------
# Bridge-side scrapers.
#
# These read the public client surfaces of each bridge. They never
# write to identities.yaml directly; they return raw dicts that
# ``merge_into_yaml`` consumes. That separation keeps the merger
# testable in isolation and lets bridge tests mock just the client
# surface without dragging the YAML round-trip in.
# ---------------------------------------------------------------------------


async def populate_from_discord(
    bridge: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scrape Discord guilds for members + text channels.

    Returns ``(people, channels)`` ready for ``merge_into_yaml``.

    Empty lists if the bridge isn't connected — populator runs are
    best-effort and never raise on a transient connection state.

    Discord member coverage depends on the ``members`` intent being
    enabled and the cache being warm. We use ``guild.members`` (cached
    list) rather than ``fetch_members()`` (paginated API call) to keep
    the populator fast on large guilds; gaps backfill on the next run
    once the cache catches up.

    Best-effort error handling matches the Slack side: if a guild's
    member or channel iteration raises mid-loop (cache miss, transient
    discord.py exception), the failure is logged + skipped at the
    guild level so the rest of the guilds still contribute. Populator
    runs shouldn't fail-loud and block the next scheduled tick.
    """
    client = getattr(bridge, "_client", None)
    if client is None or getattr(client, "is_closed", lambda: False)():
        return [], []

    people: list[dict[str, Any]] = []
    channels: list[dict[str, Any]] = []

    # Guilds are an iterable of ``discord.Guild`` (or test doubles).
    try:
        guilds = list(getattr(client, "guilds", []) or [])
    except Exception as exc:  # noqa: BLE001 — best-effort scheduled job
        log.warning("populate_from_discord guilds enumeration failed: %s", exc)
        return [], []

    for guild in guilds:
        guild_name = getattr(guild, "name", None)
        # Members. Wrap the per-guild iteration so a single bad guild
        # doesn't take down the orchestrator.
        try:
            for member in getattr(guild, "members", []) or []:
                mid = getattr(member, "id", None)
                if mid is None:
                    continue
                alias = f"discord-{mid}"
                display = (
                    getattr(member, "global_name", None)
                    or getattr(member, "display_name", None)
                    or getattr(member, "name", None)
                )
                entry: dict[str, Any] = {
                    "canonical": alias,
                    "aliases": [alias],
                }
                if display:
                    entry["display_name"] = str(display)
                # Mirror the Slack populator's bot annotation so
                # downstream consumers can spot bot accounts without
                # re-querying the bridge.
                if getattr(member, "bot", False):
                    entry["notes"] = "Discord bot account"
                people.append(entry)
        except Exception as exc:  # noqa: BLE001 — best-effort scheduled job
            log.warning(
                "populate_from_discord members iteration failed for guild "
                "%r: %s",
                guild_name, exc,
            )

        # Text channels (skip voice / stage / forum — those don't carry
        # user-readable message streams that mimir surfaces). Threads
        # are intentionally omitted — they're transient and would bloat
        # the registry.
        try:
            for channel in getattr(guild, "text_channels", []) or []:
                cid = getattr(channel, "id", None)
                if cid is None:
                    continue
                cname = getattr(channel, "name", None)
                entry = {
                    "canonical": f"discord-{cid}",
                    "kind": "public",
                }
                if cname:
                    entry["display_name"] = (
                        f"#{cname}" if guild_name is None else f"#{cname}"
                    )
                if guild_name:
                    entry["notes"] = f"Discord guild: {guild_name}"
                channels.append(entry)
        except Exception as exc:  # noqa: BLE001 — best-effort scheduled job
            log.warning(
                "populate_from_discord text_channels iteration failed for "
                "guild %r: %s",
                guild_name, exc,
            )

    return people, channels


async def populate_from_slack(
    bridge: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scrape Slack workspace for users + conversations.

    Returns ``(people, channels)`` ready for ``merge_into_yaml``.

    Empty lists when the bridge isn't initialized. SlackApiError is
    swallowed (logged) — populator runs shouldn't fail-loud on a
    permissions hiccup; the next scheduled run can pick up where this
    one left off.

    Pagination: Slack's ``users.list`` and ``conversations.list`` both
    return paginated results via ``response_metadata.next_cursor``. We
    follow cursors to completion. Workspaces with thousands of users
    will trip rate limits; the slack_sdk client retries automatically
    with exponential backoff (default behavior).
    """
    app = getattr(bridge, "_app", None)
    if app is None:
        return [], []
    client = getattr(app, "client", None)
    if client is None:
        return [], []

    people: list[dict[str, Any]] = []
    channels: list[dict[str, Any]] = []

    # ---- users.list ----
    # Populator runs are best-effort: a permissions hiccup, network
    # blip, or SDK-side unexpected response shouldn't fail-loud and
    # block the next scheduled run. Broad except is intentional;
    # log + skip the failed half (the other half still tries).
    cursor: str | None = None
    while True:
        try:
            kwargs: dict[str, Any] = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await client.users_list(**kwargs)
        except Exception as exc:  # noqa: BLE001 — best-effort scheduled job
            log.warning("populate_from_slack users_list failed: %s", exc)
            # CR2 (memory & retrieval) fix: emit a structured event so
            # the operator can see partial pagination. Pre-fix, page 3
            # of 10 failing left pages 1-2 partial; merge_into_yaml
            # didn't know it got partial data; YAML write "looked
            # complete." Idempotency saves the next run, but operator
            # visibility was zero.
            await log_event(
                "populator_partial_pagination",
                source="slack",
                resource="users",
                error=f"{type(exc).__name__}: {exc}",
                items_seen=len(people),
            )
            break
        # Successfully read this page.
        members = resp.get("members") or []
        for m in members:
            if not isinstance(m, dict):
                continue
            uid = m.get("id")
            if not uid:
                continue
            if m.get("deleted"):
                continue
            alias = f"slack-{uid}"
            profile = m.get("profile") or {}
            display = (
                profile.get("display_name")
                or m.get("real_name")
                or profile.get("real_name")
                or m.get("name")
            )
            entry: dict[str, Any] = {
                "canonical": alias,
                "aliases": [alias],
            }
            if display:
                entry["display_name"] = str(display)
            if m.get("is_bot"):
                entry["notes"] = "Slack bot account"
            people.append(entry)
        meta = resp.get("response_metadata") or {}
        next_cursor = meta.get("next_cursor") or ""
        if not next_cursor:
            break
        cursor = next_cursor

    # ---- conversations.list ----
    cursor = None
    while True:
        try:
            kwargs = {
                "limit": 200,
                # Public + private group channels. DMs (im/mpim) are
                # excluded — they're per-user surfaces and shouldn't be
                # registered in the cross-channel registry. Per-message
                # privacy gating still applies via _is_private_channel,
                # but populating dm-* channel records would just bloat
                # the registry without surfacing in the prompt.
                "types": "public_channel,private_channel",
                "exclude_archived": True,
            }
            if cursor:
                kwargs["cursor"] = cursor
            resp = await client.conversations_list(**kwargs)
        except Exception as exc:  # noqa: BLE001 — best-effort scheduled job
            log.warning(
                "populate_from_slack conversations_list failed: %s", exc
            )
            await log_event(
                "populator_partial_pagination",
                source="slack",
                resource="channels",
                error=f"{type(exc).__name__}: {exc}",
                items_seen=len(channels),
            )
            break
        chs = resp.get("channels") or []
        for c in chs:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if not cid:
                continue
            cname = c.get("name")
            entry = {
                "canonical": f"slack-{cid}",
                "kind": "public" if not c.get("is_private") else "private",
            }
            if cname:
                entry["display_name"] = f"#{cname}"
            topic = (c.get("topic") or {}).get("value")
            if topic:
                entry["notes"] = f"Slack topic: {topic}"
            channels.append(entry)
        meta = resp.get("response_metadata") or {}
        next_cursor = meta.get("next_cursor") or ""
        if not next_cursor:
            break
        cursor = next_cursor

    return people, channels


async def populate_all(
    home: Path,
    *,
    discord_bridge: Any | None = None,
    slack_bridge: Any | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run all available populators and merge results into identities.yaml.

    Bridges that aren't passed in (or that aren't connected) contribute
    nothing — running with no bridges is a no-op. Combined return
    follows ``merge_into_yaml`` shape.
    """
    all_people: list[dict[str, Any]] = []
    all_channels: list[dict[str, Any]] = []

    if discord_bridge is not None:
        d_people, d_channels = await populate_from_discord(discord_bridge)
        all_people.extend(d_people)
        all_channels.extend(d_channels)

    if slack_bridge is not None:
        s_people, s_channels = await populate_from_slack(slack_bridge)
        all_people.extend(s_people)
        all_channels.extend(s_channels)

    return merge_into_yaml(
        home,
        people=all_people,
        channels=all_channels,
        dry_run=dry_run,
    )
