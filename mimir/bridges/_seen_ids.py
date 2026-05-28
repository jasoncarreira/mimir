"""Per-bridge bounded LRU of seen inbound message IDs (chainlink #232).

Slack Socket Mode is documented to redeliver events on ACK loss; Discord
gateway's resume protocol redelivers around disconnects. Without dedup,
the same operator message arriving twice burns one agent turn per
redelivery — quota waste plus side-effect divergence for tools that
mutate state.

The cache is bounded (default 1000 entries) and LRU — the oldest entry
falls out when the cap is hit. Per-bridge instances live for the bridge
process lifetime; the bridge supervisor's restart will reset the cache,
which is fine: a redelivery that crosses a restart is already a
boundary the protocol layer should have ACK'd.

Scope: only the Slack and Discord bridges integrate this cache. The
WebChat bridge generates its ``source_id`` via ``uuid.uuid4().hex[:12]``
at enqueue time so redelivery isn't structurally possible — no cache
needed. Bench / poller-injected events also bypass this bridge-side
check; if cross-source dedup is ever needed, lift the check into
``mimir.dispatcher``.
"""

from __future__ import annotations

import collections
import logging

log = logging.getLogger(__name__)


class SeenIdCache:
    """Bounded LRU of inbound ``source_id`` values seen by one bridge.

    Not thread-safe — bridges run inside the asyncio event loop, and a
    single coroutine handles all inbound events sequentially. If a
    future bridge runs inbound handlers across threads, wrap the
    add_if_new call in a lock.
    """

    def __init__(self, *, maxlen: int = 1000) -> None:
        if maxlen < 1:
            raise ValueError(f"maxlen must be positive (got {maxlen})")
        self._seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._maxlen = maxlen

    def add_if_new(self, source_id: str) -> bool:
        """Record *source_id*; return True if it was new, False if seen.

        On a hit, the existing entry is moved to the end of the LRU so
        a "hot" duplicate (e.g. a runaway redelivery loop) doesn't get
        aged out and silently start passing again.
        """
        if not source_id:
            # Caller passed an empty id — treat as "new" so the bridge
            # still forwards the message. A bridge without a source_id
            # can't be deduped anyway; this is the safe default.
            return True
        if source_id in self._seen:
            self._seen.move_to_end(source_id)
            return False
        self._seen[source_id] = None
        if len(self._seen) > self._maxlen:
            self._seen.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, source_id: str) -> bool:
        return source_id in self._seen
