"""Per-channel + global message buffer (SPEC §5.4).

On startup ``chat_history.jsonl`` is replayed into two ``deque``s:
- ``message_history_all`` (bounded, default 500)
- ``message_history_by_channel[channel_id]`` (each bounded, default 250)

New messages append to both. Eviction is ``deque.maxlen`` only — same model
as open-strix. The on-disk file grows unbounded by default.

``recent_activity()`` produces the chronological merge that goes into the
turn prompt under ``## Recent activity``: within-channel last N + cross-channel
same-author last M (DM channels excluded from the cross pull — privacy rule).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from .pollers import POLLER_CHANNEL_PREFIX
from .scheduler import SCHEDULER_CHANNEL_PREFIX

#: Channel-id prefixes that identify synthetic per-tick channels (no
#: narrative continuity across turns — each turn has a discrete job).
#: Used by :meth:`MessageBuffer.assemble_recent_activity` to skip the
#: within-channel pull for these channels. Chainlink #78 (2026-05-11)
#: extended via PR #127 review (poller:* added 2026-05-11).
SYNTHETIC_CHANNEL_PREFIXES = (SCHEDULER_CHANNEL_PREFIX, POLLER_CHANNEL_PREFIX)

log = logging.getLogger(__name__)

MessageKind = Literal["user_message", "assistant_message", "system_note"]


@dataclass
class Message:
    ts: str
    msg_id: str | None
    channel_id: str
    author: str | None
    author_display: str | None
    kind: MessageKind
    content: str
    thread_id: str | None = None
    # Origin tag for the Recent-activity allowlist. None on legacy records;
    # set to the inbound AgentEvent.source on new records (SPEC §5.4).
    source: str | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "msg_id": self.msg_id,
            "channel_id": self.channel_id,
            "author": self.author,
            "author_display": self.author_display,
            "kind": self.kind,
            "content": self.content,
            "thread_id": self.thread_id,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(
            ts=data.get("ts", ""),
            msg_id=data.get("msg_id"),
            channel_id=data.get("channel_id", ""),
            author=data.get("author"),
            author_display=data.get("author_display"),
            kind=data.get("kind", "user_message"),
            content=data.get("content", ""),
            thread_id=data.get("thread_id"),
            source=data.get("source"),
        )


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _is_private_channel(channel_id: str) -> bool:
    """SPEC §5.4 privacy rule — any ``dm-*`` channel is private."""
    return channel_id.startswith("dm-")


@dataclass
class MessageBuffer:
    """In-memory deques + JSONL append. One instance per process.

    ``resolver`` (FUTURE_WORK §6.1) is an optional ``IdentityResolver``
    that maps platform-prefixed author ids to a canonical for cross-
    channel / cross-platform pull. ``None`` (the default) makes
    ``cross_author_messages`` fall back to direct equality on
    ``msg.author`` — same behavior as before identity reconciliation
    landed.
    """

    history_path: Path
    global_max: int = 500
    per_channel_max: int = 250
    resolver: object | None = None  # IdentityResolver | None — typed loosely to
    # avoid a hard import dep in this module (history.py loads early).
    cross_platform_pull: bool = True
    _all: deque[Message] = field(default_factory=deque)
    _by_channel: dict[str, deque[Message]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._all = deque(maxlen=self.global_max)
        # Per-channel deques are created lazily on first message for that channel.

    def replay(self) -> int:
        """Read ``chat_history.jsonl`` from disk and rehydrate the deques.

        Returns the number of messages loaded. Idempotent — replaying twice
        just re-overwrites with the same tail (deques are bounded).
        """
        self._all = deque(maxlen=self.global_max)
        self._by_channel = {}
        if not self.history_path.is_file():
            return 0
        loaded = 0
        try:
            with self.history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = Message.from_dict(data)
                    self._append_in_memory(msg)
                    loaded += 1
        except OSError as exc:
            log.warning("chat_history.jsonl replay failed: %s", exc)
        return loaded

    def _append_in_memory(self, msg: Message) -> None:
        self._all.append(msg)
        ch = self._by_channel.get(msg.channel_id)
        if ch is None:
            ch = deque(maxlen=self.per_channel_max)
            self._by_channel[msg.channel_id] = ch
        ch.append(msg)

    async def append(self, msg: Message) -> None:
        """Append to disk + both deques.

        No lock: the in-memory deque mutation is single-threaded under
        asyncio (synchronous, no awaits inside ``_append_in_memory``),
        and the disk write goes through ``asyncio.to_thread``. Each
        ``_append_disk`` call opens the file with ``"a"`` and writes one
        JSON line — POSIX guarantees ``O_APPEND`` writes are atomic at
        the kernel level for a single ``write(2)`` syscall, so concurrent
        appends from different threads can't corrupt or interleave at the
        byte level. They may land out of call order on disk (whichever
        thread wins the inode-lock race), but each line is whole.

        Removing the lock — which previously held *across* the
        ``await asyncio.to_thread(...)`` — lets concurrent callers
        actually fan out to separate threads instead of serializing
        through one. See CR#17.

        Note: caller-visible flush ordering is preserved by keeping the
        ``await``; on return, this message's disk write has completed.
        Tests and graceful-shutdown paths depend on that. A more
        aggressive fix would make the write fire-and-forget with a
        bounded queue — deferred (would need bg-task tracking on the
        buffer instance plus test-side flush hooks)."""
        self._append_in_memory(msg)
        await asyncio.to_thread(self._append_disk, msg)

    def _append_disk(self, msg: Message) -> None:
        """Append one JSONL record. **Interleave-atomicity, not
        durability** (CR2 memory & retrieval clarification).

        ``open("a")`` + ``write(...)`` produces a single ``write(2)``
        per call thanks to Python's text-mode buffering inside the
        ``with`` block, and POSIX guarantees ``O_APPEND`` writes are
        atomic at the kernel level — so concurrent appends from
        different threads can't corrupt or interleave at the byte
        level (each line is whole). They may land out of call order
        on disk (whichever thread wins the inode-lock race), but the
        file stays valid JSONL.

        However: this is NOT a durability guarantee. The ``with``
        block's ``close()`` flushes Python's buffer to the OS page
        cache; we do NOT call ``os.fsync()``. A crash between
        write-return and OS flush loses the recent records that were
        in the page cache. ``chat_history.jsonl`` is operator-
        readable conversational state — informational, not load-
        bearing — so the durability trade-off (perf cost on every
        append vs. losing the last few records on crash) lands on
        skipping fsync. If a future use makes this load-bearing,
        either ``os.fsync(f.fileno())`` here or rely on the writer
        to call sync explicitly.
        """
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg.to_dict(), ensure_ascii=True, default=str) + "\n")
        except OSError as exc:
            log.warning("chat_history.jsonl append failed: %s", exc)

    def make_message(
        self,
        *,
        channel_id: str,
        kind: MessageKind,
        content: str,
        author: str | None = None,
        author_display: str | None = None,
        msg_id: str | None = None,
        thread_id: str | None = None,
        ts: str | None = None,
        source: str | None = None,
    ) -> Message:
        return Message(
            ts=ts or _utc_now_iso(),
            msg_id=msg_id,
            channel_id=channel_id,
            author=author,
            author_display=author_display,
            kind=kind,
            content=content,
            thread_id=thread_id,
            source=source,
        )

    # ---- read paths --------------------------------------------------------

    def channel_count(self, channel_id: str) -> int:
        ch = self._by_channel.get(channel_id)
        return len(ch) if ch is not None else 0

    def total_count(self) -> int:
        return len(self._all)

    def recent_for_channel(
        self,
        channel_id: str,
        limit: int,
        *,
        source_allowlist: frozenset[str] | None = None,
    ) -> list[Message]:
        """Last ``limit`` recent messages, scoped per the Phase C
        cross-channel-content rule (chainlink #40 / #43):

        - **Public-channel target.** Pool is *all public channels'*
          most-recent activity, ranked by recency. The current channel
          contributes naturally; quiet channels are still represented
          by their tail. Diverges from the prior "current-channel queue
          with global fallback when empty" rule — operator-confirmed
          dumb-recency global pool.
        - **DM target.** Pool stays the global ``self._all`` window,
          which the runtime still gates per-message via
          ``_is_private_channel(msg.channel_id)`` upstream of any
          cross-channel render. Behaviorally unchanged from before.

        Privacy: ``_is_private_channel(msg.channel_id)`` excludes any
        message whose channel id is a DM when the target channel is
        public — so a bot replying in ``#eng`` can never see its own DM
        transcripts as "Recent activity," regardless of whether
        ``#eng`` itself has prior history. The DM-into-public leak path
        is closed at the source the same way it was before Phase C.

        ``limit=0`` returns nothing (used to disable Recent activity in
        benchmarks; bare slicing with ``[-0:]`` would return the full
        list).

        ``source_allowlist`` filters the candidate pool by
        ``Message.source``. ``None`` means no filter; a frozenset means
        "only these sources". Messages with ``source=None`` are
        excluded when an allowlist is set.
        """
        if limit <= 0:
            return []
        if _is_private_channel(channel_id):
            # DM target — pool is everything (its own DM + public). The
            # runtime's outbound-render layer is responsible for any
            # further DM-vs-public scoping.
            pool = list(self._all)
        else:
            # Public target — pool is all public-channel messages, no
            # per-channel preference. The current channel's tail will
            # naturally dominate when it's busy; quieter peers
            # contribute when they have recent activity.
            pool = [
                m for m in self._all if not _is_private_channel(m.channel_id)
            ]
        if source_allowlist is not None:
            pool = [m for m in pool if m.source in source_allowlist]
        return pool[-limit:]

    def cross_author_messages(
        self,
        *,
        author: str,
        exclude_channel: str,
        limit: int,
        within_hours: int,
        source_allowlist: frozenset[str] | None = None,
    ) -> list[Message]:
        """Last ``limit`` messages by ``author`` on channels other than
        ``exclude_channel``, within the time window. DMs always excluded.

        Author matching goes through the optional ``IdentityResolver``
        (FUTURE_WORK §6.1) — so ``slack-U123ABC`` and ``discord-456789``
        both resolve to ``alice`` and surface together when alice's
        identity is mapped in ``state/identities.yaml``. Without a
        resolver (or for unknown ids), comparison falls back to direct
        equality.

        ``source_allowlist`` filters by ``Message.source`` (same semantics as
        ``recent_for_channel``)."""
        if not author or limit <= 0:
            return []
        if not self.cross_platform_pull:
            # Operator opted out of cross-platform pull (e.g. compliance);
            # fall back to direct equality, no resolver consulted.
            target_canonical = author
            resolve = None
        else:
            target_canonical = self._resolve(author)
            resolve = self._resolve
        cutoff = datetime.now(tz=timezone.utc).timestamp() - within_hours * 3600
        out: list[Message] = []
        # Walk newest-first for cheap early exit.
        for msg in reversed(self._all):
            if len(out) >= limit:
                break
            if msg.channel_id == exclude_channel:
                continue
            if _is_private_channel(msg.channel_id):
                continue
            msg_canonical = resolve(msg.author) if resolve else msg.author
            if msg_canonical != target_canonical:
                continue
            if source_allowlist is not None and msg.source not in source_allowlist:
                continue
            try:
                msg_ts = datetime.fromisoformat(msg.ts.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                continue
            if msg_ts < cutoff:
                # Older than the window — and since we're walking newest-first,
                # everything else is older too.
                break
            out.append(msg)
        out.reverse()
        return out

    def _resolve(self, author: str | None) -> str | None:
        """Map ``author`` through the resolver if one is wired; else return
        unchanged. Falls through to direct equality when no resolver."""
        if self.resolver is None or author is None:
            return author
        return self.resolver.resolve(author)

    def assemble_recent_activity(
        self,
        *,
        channel_id: str,
        author: str | None,
        recent_per_channel: int,
        recent_author_cross: int,
        cross_hours: int,
        source_allowlist: frozenset[str] | None = None,
    ) -> list[Message]:
        """Merge within-channel + cross-channel author streams, chronological.

        Cross-channel pull is skipped when ``author`` is None (e.g. scheduled
        ticks have no inbound author).

        Within-channel pull is **also** skipped when ``channel_id`` is a
        synthetic per-tick channel — ``scheduler:*`` (heartbeat, reflect,
        saga-consolidate, introspection-report) or ``poller:*`` (each
        registered poller's emitted-event channel). See
        :data:`SYNTHETIC_CHANNEL_PREFIXES`. Those channels only ever
        hold prior assistant scheduled-tick or poller-event replies —
        no narrative continuity, no useful prior context. Each
        synthetic tick has a specific job (heartbeat picks ONE backlog
        item; reflect looks at agent behavior across the week; a
        poller turn responds to one discrete external event) that
        doesn't benefit from chat-tail context, and cross-tick
        assistant-reply tail just inflates the prompt without
        informing the work. Chainlink #78 (2026-05-11); ``poller:``
        added via PR #127 review (same review-thread).

        ``source_allowlist`` (SPEC §5.4) keeps benchmark / API / scheduler
        events out of the prompt by default — only "real conversation"
        sources participate. Mirrors open-strix's hard-coded
        ``{"discord","web","stdin"}`` filter (``app.py:734``).
        """
        if channel_id.startswith(SYNTHETIC_CHANNEL_PREFIXES):
            # Synthetic scheduler:* or poller:* channel — no useful
            # prior context. See docstring above for rationale.
            within: list[Message] = []
        else:
            within = self.recent_for_channel(
                channel_id, recent_per_channel, source_allowlist=source_allowlist
            )
        cross: list[Message] = []
        if author:
            # Cross-pull is one-directional: DM messages are excluded by
            # ``cross_author_messages`` itself (source-side filter on
            # ``_is_private_channel(msg.channel_id)``). The target channel
            # being a DM does NOT block the pull — surfacing Alice's #eng
            # context inside her private DM with the bot is just useful
            # context, not a privacy leak. The leak would be the other
            # direction (DM content into a public channel), and that's
            # already prevented at the source.
            cross = self.cross_author_messages(
                author=author,
                exclude_channel=channel_id,
                limit=recent_author_cross,
                within_hours=cross_hours,
                source_allowlist=source_allowlist,
            )

        # Merge by ts (string ISO compares lexicographically).
        merged = sorted(within + cross, key=lambda m: m.ts)

        # De-dup on (channel_id, msg_id, ts) so the same message doesn't
        # appear twice if a cross-pull happens to overlap (unlikely but cheap).
        seen: set[tuple] = set()
        unique: list[Message] = []
        for m in merged:
            key = (m.channel_id, m.msg_id, m.ts)
            if key in seen:
                continue
            seen.add(key)
            unique.append(m)
        return unique


def render_recent_activity(
    messages: Iterable[Message],
    *,
    max_chars: int = 0,
    resolver: object | None = None,
) -> str:
    """Render messages as ``[<ts> <channel>] <author>: <content>`` lines.

    ``max_chars`` (>0) caps each individual message's content; longer bodies
    are truncated with ``…[truncated]`` (same convention as ``turn_logger``'s
    tool-result cap). The per-message cap protects against a single huge
    inbound (e.g. a 500-post bluesky seed transcript) blowing the model's
    context when prior messages are included via the SPEC §5.4 deque pull.

    ``resolver`` (FUTURE_WORK §6.1) — when present, lookups happen on
    both axes:

    - **Author side.** If the message's author has an identity record,
      render the record's ``display_name`` instead of the per-message
      ``author_display``. Alice on Slack and Alice on Discord render
      with the same name.
    - **Channel side** (chainlink #40 Phase C). If the message's
      ``channel_id`` is registered, render
      ``<display_name> (<channel_id>)`` so the agent reads
      ``[ts jason-mimir (discord-1500…)]`` instead of bare opaque ids.
      The id stays in the line so the agent can still target the
      channel by id when needed. Channels not registered fall through
      to the bare id (existing format).

    The "Known identities" preamble (built separately by
    ``render_identity_context``) surfaces canonicals + aliases so the
    agent connects the dots between the rendered name and the platform
    ids.
    """
    lines: list[str] = []
    for m in messages:
        ts_short = m.ts[:16] if m.ts else ""
        author = None
        if resolver is not None and m.author and m.kind != "assistant_message":
            author = resolver.display_name(m.author)
        if not author:
            author = m.author_display or m.author or (
                "(assistant)" if m.kind == "assistant_message" else "(system)"
            )
        if m.kind == "assistant_message":
            author = "(assistant)"
        content = m.content or ""
        if max_chars > 0 and len(content) > max_chars:
            content = content[:max_chars] + "…[truncated]"
        # Surface msg_id when present so the agent can target older
        # messages with ``<react message="<id>" />``. Skipped when the
        # record has no id (legacy entries, system_notes).
        id_part = f" id={m.msg_id}" if m.msg_id else ""
        # Phase C channel-side resolution: if the resolver knows this
        # channel, prefix its display_name. ``getattr`` guards against
        # legacy resolvers without the channel API.
        channel_field = m.channel_id
        if resolver is not None:
            channel_lookup = getattr(resolver, "channel_display_name", None)
            if callable(channel_lookup):
                display = channel_lookup(m.channel_id)
                if display:
                    channel_field = f"{display} ({m.channel_id})"
        lines.append(f"[{ts_short} {channel_field}{id_part}] {author}: {content}")
    return "\n".join(lines)


def render_identity_context(
    messages: Iterable[Message],
    event_author: str | None,
    resolver: object | None,
) -> str | None:
    """Build a 'Known identities' block for the turn prompt (FUTURE_WORK §6.1).

    Lists every identity record matching an author in ``messages`` or the
    inbound ``event_author``, deduplicated by canonical. Format per line:

        - **<canonical>** — <display_name> (<notes>) · aliases: <a1, a2, ...>

    Display name, notes, and aliases are each only included when present.
    Returns ``None`` when no resolver is wired or no author maps to a
    record — the caller drops the section entirely in that case.
    """
    if resolver is None:
        return None

    candidate_authors: list[str] = []
    if event_author:
        candidate_authors.append(event_author)
    for m in messages or []:
        if m.author and m.kind != "assistant_message":
            candidate_authors.append(m.author)

    # Dedupe on canonical so cross-platform messages from one identity
    # produce a single entry.
    seen: dict[str, object] = {}
    by_canonical = {i.canonical: i for i in resolver.all_identities()}
    for author in candidate_authors:
        canonical = resolver.resolve(author)
        if canonical is None or canonical == author:
            # No identity record (resolver fell through unchanged).
            continue
        identity = by_canonical.get(canonical)
        if identity is None:
            continue
        seen[canonical] = identity

    if not seen:
        return None

    lines: list[str] = []
    for canonical, identity in seen.items():
        parts = [f"- **{canonical}**"]
        if getattr(identity, "display_name", None):
            parts.append(f" — {identity.display_name}")
        if getattr(identity, "notes", None):
            parts.append(f" ({identity.notes})")
        aliases = getattr(identity, "aliases", None) or []
        if aliases:
            parts.append(f" · aliases: {', '.join(aliases)}")
        lines.append("".join(parts))
    return "\n".join(lines)
