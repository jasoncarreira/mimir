"""Turn-lifecycle hook chain (re-introduced after PR #181).

The SDK-era code (pre-deepagents) had a hook chain — abstract
``TurnHook`` base with ``pre_query`` / ``on_message`` / ``post_query``
/ ``finalize`` methods, fired by ``fire_pre_query`` / ``fire_finalize``
helpers with per-hook exception isolation. PR #181 (deepagents
migration) replaced the chain with inlined logic in ``agent.py`` —
the inlined approach was simpler but lost the extension point that
made it cheap to register agent-specific post-turn logic (commitment
extraction, wiki backlinks, etc.) without touching the agent loop.

This module restores the abstraction. Goals:

* **Extension point for multi-agent deployments** — Muninnbot can
  register its own ``TurnHook`` subclasses (e.g., a custom finalize
  for its own knowledge-graph updates) without modifying ``agent.py``.
* **Testability** — each hook is unit-testable in isolation;
  pre-deepagents, the inlined logic could only be exercised via
  full-turn integration tests.
* **Visible failures** — a hook that raises emits a
  ``turn_hook_failed`` event in ``events.jsonl`` (the surface PR
  #210 deferred here). Pre-fix, hook exceptions only surfaced in
  the container log.

Lifecycle stages (call order during ``Agent.run_turn``):

  pre_query   — after TurnContext set up, before LLM invocation.
  post_query  — after LLM completes, before TurnRecord is written.
  finalize    — after TurnRecord written. Best place for post-turn
                work (extraction, wiki updates, etc.) that shouldn't
                block the user-visible reply path.

All three stages (``pre_query``, ``post_query``, and ``finalize``)
are now wired into ``agent.py``. ``pre_query`` fires after
TurnContext setup and inbound buffer append, before memory-block
assembly. ``post_query`` fires after ``agent.astream`` completes
and result fields are derived, before the TurnRecord is written.
``finalize`` fires after the TurnRecord is written to
``turns.jsonl``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable

from .event_logger import log_event

if TYPE_CHECKING:  # pragma: no cover
    from .models import AgentEvent, TurnContext, TurnRecord

log = logging.getLogger(__name__)


class TurnHook:
    """Abstract base for turn lifecycle hooks.

    Subclasses override the methods they care about; unoverridden
    methods are no-ops. Register the subclass on ``Agent`` via the
    constructor's ``turn_hooks=`` parameter or ``Agent.add_hook(...)``.

    Exception isolation: a hook that raises does NOT crash the turn
    or prevent subsequent hooks from running. The exception is
    logged and emitted as a ``turn_hook_failed`` event in
    ``events.jsonl`` (see ``fire_hooks``).
    """

    async def pre_query(self, ctx: "TurnContext", event: "AgentEvent") -> None:
        """Fired after TurnContext is set up, before LLM invocation.

        Use for: setting up turn-local state, recording turn-start
        metrics, modifying the prompt assembly inputs via mutation
        on ``ctx``. Avoid heavy I/O — this is on the critical path
        of the user-visible reply.
        """
        return None

    async def post_query(
        self, ctx: "TurnContext", event: "AgentEvent",
        messages: list, output: str,
    ) -> None:
        """Fired after LLM completes, before TurnRecord is written.

        Use for: cleanup that needs the model output but should run
        before the audit record is finalized. Most operational logic
        should go in ``finalize`` instead — that runs after the
        record is durable, so a failure here doesn't lose the turn.
        """
        return None

    async def finalize(
        self, ctx: "TurnContext", event: "AgentEvent",
        record: "TurnRecord",
    ) -> None:
        """Fired after the TurnRecord is written to ``turns.jsonl``.

        Best place for post-turn work that:
          - Reads the finalized record (e.g. extraction over output)
          - Shouldn't delay the user-visible reply (already sent)
          - Can fail without losing the audit trail (record persisted)
        """
        return None


async def fire_hooks(
    stage: str,
    hooks: Iterable[TurnHook],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Fire all hooks for a given stage with per-hook exception
    isolation.

    ``stage`` is the method name on TurnHook (``"pre_query"`` /
    ``"post_query"`` / ``"finalize"``). Hooks run in registration
    order; an exception in any single hook is caught, logged, and
    emitted as a ``turn_hook_failed`` event — subsequent hooks
    still run.

    Event-emission failures are themselves swallowed to keep one
    misbehaving hook (or a logging issue) from short-circuiting the
    chain entirely.
    """
    for hook in hooks:
        method = getattr(hook, stage, None)
        if method is None:
            # Hook doesn't implement this stage — skip silently.
            # (Defensive: subclasses inherit no-op stubs from
            # ``TurnHook``, so a strictly-conforming hook never hits
            # this branch. Guards against duck-typed hook objects
            # that don't subclass ``TurnHook`` at all.)
            continue
        try:
            await method(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            hook_name = type(hook).__name__
            log.exception(
                "turn hook %r raised in stage=%s",
                hook_name, stage,
            )
            try:
                # Use module-level ``log_event`` so tests can
                # monkeypatch ``mimir.turn_hooks.log_event``. Lazy
                # re-import would shadow the patch.
                await log_event(
                    "turn_hook_failed",
                    hook=hook_name,
                    stage=stage,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:  # noqa: BLE001
                # Don't let event logging itself short-circuit the
                # rest of the hook chain.
                pass
            # Continue to the next hook — isolation invariant.


# ── Built-in hooks (migrated from agent.py inlined logic) ───────────


class CommitmentExtractionHook(TurnHook):
    """Migrated from ``Agent._maybe_extract_commitments`` (inlined in
    ``agent.py:1029``). Extracts commitments from a saga_session_end
    synthesis output and persists net-new records to the
    ``CommitmentsStore``.

    Best-effort throughout: every failure path logs + returns; the
    synthesis turn's own record is unaffected.

    Events emitted:
    * ``commitments_extracted`` on ≥1 added record (carries count,
      skipped_dedupe, prompt_version)
    * ``commitments_extraction_no_op`` with reason in
      {short_output, llm_returned_zero, all_dedupe_skipped} when the
      path ran but added nothing — distinguishable in backtests from
      "skipped extraction entirely."

    Suppressed when:
    * ``ctx.trigger != "saga_session_end"``
    * The hook was constructed with ``commitments_store=None``
    * ``record.output`` is empty
    """

    def __init__(self, commitments_store: Any) -> None:
        self._store = commitments_store

    async def finalize(
        self, ctx: "TurnContext", event: "AgentEvent",
        record: "TurnRecord",
    ) -> None:
        if ctx.trigger != "saga_session_end":
            return
        if self._store is None:
            return
        output = getattr(record, "output", "") or ""
        if not output:
            return

        # Lazy imports keep the hook module load-light. ``log_event`` is
        # NOT lazy-imported — module-level so tests can monkeypatch
        # ``mimir.turn_hooks.log_event``.
        from .commitments.extractor import (
            EXTRACTION_PROMPT_VERSION,
            MIN_OUTPUT_LEN,
            extract_commitments,
        )
        from .history import SYNTHETIC_CHANNEL_PREFIXES

        # Synthetic channels (``scheduler:*`` / ``poller:*``) are never
        # delivery targets for the commitment poller or prompt-block
        # surfacing. If a saga_session_end fires on a synthetic
        # channel, nullify the channel before passing to the
        # extractor so the resulting records are unbound and surface
        # cross-channel as intended. Log events keep ``ctx.channel_id``
        # for observability (origin channel), not for binding.
        effective_channel_id = ctx.channel_id
        if (
            effective_channel_id
            and effective_channel_id.startswith(SYNTHETIC_CHANNEL_PREFIXES)
        ):
            effective_channel_id = None

        if len(output) < MIN_OUTPUT_LEN:
            await log_event(
                "commitments_extraction_no_op",
                reason="short_output",
                output_len=len(output),
                channel_id=ctx.channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )
            return

        try:
            extracted = await extract_commitments(
                output,
                channel_id=effective_channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "commitment extraction failed for turn %s; skipping",
                ctx.turn_id,
            )
            return

        if not extracted:
            await log_event(
                "commitments_extraction_no_op",
                reason="llm_returned_zero",
                channel_id=ctx.channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )
            return

        # Snapshot active dedupe keys once — N×|JSONL| in the prior
        # find_by_dedupe_key-per-record shape, N+|JSONL| this way.
        # ``current_state`` returns active records only, matching the
        # find_by_dedupe_key semantics.
        state = self._store.current_state()
        existing_keys = {
            r.dedupe_key
            for r in state.values()
            if r.dedupe_key and not r.is_terminal()
        }

        added = 0
        skipped_dedupe = 0
        for rec in extracted:
            if rec.dedupe_key in existing_keys:
                skipped_dedupe += 1
                continue
            try:
                await self._store.add(rec)
                added += 1
                existing_keys.add(rec.dedupe_key)
            except Exception:  # noqa: BLE001
                log.exception(
                    "commitments store.add failed for record %s", rec.id,
                )

        if added > 0:
            await log_event(
                "commitments_extracted",
                count=added,
                skipped_dedupe=skipped_dedupe,
                channel_id=ctx.channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )
        else:
            await log_event(
                "commitments_extraction_no_op",
                reason="all_dedupe_skipped",
                extracted_count=len(extracted),
                skipped_dedupe=skipped_dedupe,
                channel_id=ctx.channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )


__all__ = ["TurnHook", "fire_hooks", "CommitmentExtractionHook"]
