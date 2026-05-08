"""Generic asyncio-pool primitive shared between saga and mimir.

Both saga's ``_AsyncClaudePool`` and mimir's ``ClientPool`` follow the
same shape: a bounded pool of asyncio-connected clients, condition-
guarded with lazy loop binding. They differ in their *policy*
(fingerprint-keyed-drain vs single-fingerprint-recycle, what counts
toward "in-flight", how grow interleaves with the lock), but they
share the bookkeeping skeleton:

- A max-size cap that's validated at construction.
- An ``_idle`` stack of returned instances.
- A lazy-bound ``asyncio.Condition`` so module import doesn't require
  a running event loop, but ``acquire``/``release`` get a real
  condition on the calling loop.

This module is the canonical home for that skeleton — extracted in
chainlink #46 (Phase 2 of #20) once both pools became asyncio-native
and the threading-vs-asyncio gap that blocked sharing previously
disappeared. See ``state/spec/chainlink-20-saga-async-native-plan.md``.

Hosted in saga because saga is the lower-level package: mimir already
depends on saga, but saga doesn't depend on mimir, so having mimir
import the primitive from saga keeps the dependency arrow pointing
the right way.

Not thread-safe — assumes a single asyncio event loop, which is the
runtime model for both consumers."""

from __future__ import annotations

import asyncio
from typing import Generic, TypeVar

T = TypeVar("T")


class BoundedAsyncPool(Generic[T]):
    """Bookkeeping base for a bounded asyncio pool of T.

    Subclasses implement their own ``acquire``/``release`` semantics
    on top of this skeleton — the policy differences (fingerprint
    drain, recycle-after-N, async vs sync construction) don't fit a
    single generic acquire loop without callback gymnastics, so we
    share only the structurally-identical pieces.

    Subclasses typically:
    - call ``super().__init__(max_size)``
    - add their own size tracking (``_size`` counter, or
      ``len(_idle) + len(_in_flight)``, etc.)
    - add their own state (in-flight set, current fingerprint, recycle
      threshold, etc.)
    - implement ``acquire`` and ``release`` using the inherited
      ``_idle`` stack and ``_condition()``.
    """

    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        self._idle: list[T] = []
        self._cond: "asyncio.Condition | None" = None

    @property
    def max_size(self) -> int:
        return self._max_size

    def _condition(self) -> asyncio.Condition:
        """Lazy-bind the condition to the running loop. Module import
        shouldn't require an event loop."""
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond
