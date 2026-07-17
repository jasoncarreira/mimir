"""Saga client — the in-process ``SagaClient`` mimir uses for memory.

Mimir interacts with the memory store through a unified ``SagaClient``
interface, backed by ``SagaStore`` (in ``mimir.saga.client``) — the
in-process retrieval/consolidation engine. Same process, same SQLite
directory, no HTTP loop. ``make_saga_client`` constructs one (wrapped in
``RecordingSagaClient``) and returns it.

mimir's call sites (``agent.py``, ``sagatools.py``, ``scheduler.py``,
``server.py``) all do ``await client.<method>(...)``.

Errors surface as ``SagaError`` — raised only for genuine logic errors
(DB unreachable, schema mismatch), since the in-process store has no
transport layer to fail.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


class SagaError(RuntimeError):
    def __init__(
        self, message: str, status: int | None = None, body: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# ─── Protocol ────────────────────────────────────────────────────


@runtime_checkable
class SagaClient(Protocol):
    """The eight-method surface mimir uses against saga.

    ``SagaStore`` implements this. Most call
    sites accept ``SagaClient | None`` — None disables the integration
    (e.g., when running mimir without saga at all).
    """

    async def query(
        self,
        query: str,
        *,
        top_k: int = 12,
        mode: str = "task",
        token_budget: int = 500,
        session_id: str | None = None,
        min_confidence_tier: str | None = None,
        context: list[dict[str, str]] | None = None,
        extra_atom_ranked_pathways: Mapping[str, Iterable[str]] | None = None,
        rrf_pathway_weights: Mapping[str, float] | None = None,
        enable_session_boundary_rrf: bool | None = None,
        session_boundary_limit: int | None = None,
        session_boundary_alpha: float | None = None,
        session_boundary_weight: float | None = None,
        session_boundary_atoms_per_session: int | None = None,
    ) -> dict[str, Any]: ...

    async def store(
        self,
        content: str,
        *,
        stream: str | None = None,
        profile: str | None = None,
        source_type: str = "api",
        use_llm_annotate: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def feedback(
        self,
        atom_ids: list[str],
        response_text: str,
        *,
        session_id: str | None = None,
        feedback: str | None = None,
        auth_context: Any = None,
    ) -> dict[str, Any]: ...

    async def outcome(
        self,
        atom_ids: list[str],
        feedback: str,
        *,
        session_id: str | None = None,
        query: str | None = None,
        auth_context: Any = None,
    ) -> dict[str, Any]: ...

    async def end_session(
        self,
        session_id: str,
        summary: str,
        *,
        topics_discussed: list[str] | None = None,
        decisions_made: list[str] | None = None,
        unfinished: list[str] | None = None,
        emotional_state: str | None = None,
        closed_since: list[str] | None = None,
        channel_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def consolidate(
        self,
        *,
        dry_run: bool = False,
        max_clusters: int | None = None,
        extra_canonical_subjects: list[str] | None = None,
    ) -> dict[str, Any]: ...

    async def forget(
        self,
        *,
        dry_run: bool = True,
        min_retrievals: int | None = None,
        contribution_threshold: float | None = None,
        contradiction_threshold: float | None = None,
        confidence_floor: float | None = None,
        grace_days: int | None = None,
        auth_context: Any = None,
    ) -> dict[str, Any]: ...

    async def rebuild_index_if_needed(self) -> bool: ...

    async def recent_session_boundaries(
        self,
        *,
        channel_id: str | None = None,
        count: int = 3,
        auth_context: Any = None,
    ) -> list[dict[str, Any]]: ...

    async def most_retrieved_atoms(
        self,
        *,
        days: int = 7,
        count: int = 10,
        channel_id: str | None = None,
        contributed_only: bool = False,
        trend: str | None = None,
        auth_context: Any = None,
    ) -> list[dict[str, Any]]: ...

    async def health(self) -> bool: ...
    async def close(self) -> None: ...


# ─── Factory ─────────────────────────────────────────────────────


class RecordingSagaClient:
    """Transparent wrapper that appends a ``SagaCallRecord`` to the
    current ``TurnContext.saga_calls`` on every method invocation.

    The recording is a side-effect only — args + result pass through
    unchanged. When no current turn is registered (saga calls from
    consolidation cron, decay sweeps, etc.), the wrapper silently
    skips the append; only turn-scoped calls produce records.

    Records carry compact arg/result summaries (strings truncated to
    200 chars) so turns.jsonl row size stays bounded. Full saga
    detail still goes to events.jsonl via the existing
    ``saga_query_ctx_resolution`` / ``saga_store_ctx_resolution`` /
    etc. events — these records are the inline view, not a
    replacement.

    Errors during a saga call produce a record with ``error`` set and
    re-raise so callers see the original exception. Errors during
    the recording itself (e.g. TurnContext shape drift) are swallowed
    — observability must never break the agent loop.
    """

    # Methods we wrap. Anything not in this list passes through
    # __getattr__ unchanged (e.g. private helpers, future additions
    # that don't need recording).
    #
    # Note: ``mark_contributions`` is intentionally NOT here — mimir
    # uses ``feedback()`` for the credit-pass call (see
    # ``agent.py:_post_message_hook``, line 1859). There's no
    # ``mark_contributions`` method on ``SagaStore``; adding it to this
    # set would AttributeError at runtime.
    _RECORDED_METHODS = frozenset(
        {
            "query",
            "store",
            "feedback",
            "outcome",
            "end_session",
            "consolidate",
            "consolidate_skill_memories",
            "forget",
        }
    )

    def __init__(self, inner: SagaClient) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        """Default-passthrough — for anything not in
        ``_RECORDED_METHODS`` (e.g. ``recent_session_boundaries``,
        ``most_retrieved_atoms``, private helpers on the wrapped
        impl), forward to ``self._inner`` unchanged. Recorded methods
        are defined explicitly below."""
        return getattr(self._inner, name)

    async def query(self, *args, **kwargs):
        return await self._call("query", self._inner.query, args, kwargs)

    async def store(self, *args, **kwargs):
        return await self._call("store", self._inner.store, args, kwargs)

    async def feedback(self, *args, **kwargs):
        return await self._call("feedback", self._inner.feedback, args, kwargs)

    async def outcome(self, *args, **kwargs):
        return await self._call("outcome", self._inner.outcome, args, kwargs)

    async def end_session(self, *args, **kwargs):
        return await self._call(
            "end_session",
            self._inner.end_session,
            args,
            kwargs,
        )

    async def consolidate(self, *args, **kwargs):
        return await self._call(
            "consolidate",
            self._inner.consolidate,
            args,
            kwargs,
        )

    async def consolidate_skill_memories(self, *args, **kwargs):
        # #266: per-skill dedup pass. Intentionally NOT on the SagaClient
        # Protocol — it's implemented by the embedded ``SagaStore`` only.
        # A wrapped client without the method raises AttributeError; the
        # scheduler's saga-consolidate job catches that and degrades to a
        # "skipped" marker (see scheduler.py).
        return await self._call(
            "consolidate_skill_memories",
            self._inner.consolidate_skill_memories,
            args,
            kwargs,
        )

    async def forget(self, *args, **kwargs):
        return await self._call("forget", self._inner.forget, args, kwargs)

    async def _call(
        self,
        call_type: str,
        fn,
        args: tuple,
        kwargs: dict,
    ):
        """Common dispatch — time the call, capture args + result,
        append to TurnContext.saga_calls, re-raise any exception."""
        import time as _time
        from .models import SagaCallRecord

        started = _time.monotonic()
        error: str | None = None
        result: Any = None
        try:
            result = await fn(*args, **kwargs)
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            elapsed_ms = (_time.monotonic() - started) * 1000.0
            try:
                ctx = _resolve_turn_ctx(kwargs)
                if ctx is not None:
                    # ``t_ms`` is start-relative, not finish-relative, so
                    # the call appears on the timeline at the moment it
                    # KICKED OFF (matches how operators read tool_call
                    # events). Without a turn ctx (consolidation cron,
                    # decay sweeps), t_ms stays None — the record still
                    # carries latency_ms for diagnostics.
                    t_ms: float | None = None
                    ctx_started = getattr(ctx, "started_at", None)
                    if ctx_started is not None:
                        t_ms = (started - ctx_started) * 1000.0
                    ctx.saga_calls.append(
                        SagaCallRecord(
                            call_type=call_type,
                            args=_summarize_args(call_type, args, kwargs),
                            result=_summarize_result(call_type, result, error),
                            latency_ms=elapsed_ms,
                            error=error,
                            t_ms=t_ms,
                        )
                    )
            except Exception:  # noqa: BLE001
                # Observability must never break the loop. Swallow.
                pass


def _resolve_turn_ctx(kwargs: dict):
    """Resolve the active TurnContext via the three-level chain that
    works across MCP-dispatched + in-process saga calls.

    The MCP tools (``saga_query`` / ``saga_store`` / ``saga_feedback``
    / ``saga_end_session``) run on a fresh task forked by the SDK's
    control-protocol handler. That task captured the
    ``_current_turn`` contextvar at fork time (= ``None``) and never
    sees later ``set()`` calls in ``run_turn`` (see
    ``mimir/_context.py`` module docstring + chainlink #23 design).
    A bare ``get_current_turn()`` lookup from this wrapper returns
    ``None`` on every model-driven saga call — exactly the most
    important calls to record.

    ``_context.resolve_active_ctx`` is the canonical three-level
    chain that survives the task-fork boundary:
    1. ``kwargs["session_id"]`` → match against
       ``ctx.saga_session_id`` in ``_active_turns`` (multi-channel
       safe; required because the SDK forks per-call)
    2. ``get_only_active_turn()`` — works in single-channel
       deployments where the dispatcher serializes turns
    3. ``get_current_turn()`` — works for direct-handler-call paths
       (mimir's pre/post-message hooks, sagatools tests)

    Returns ``None`` if all three miss (saga calls from consolidation
    cron, decay sweeps, etc., have no active turn — that's by design).
    """
    from ._context import resolve_active_ctx

    ctx, _resolution = resolve_active_ctx(kwargs or {})
    return ctx


# Per-call-type arg/result summarizers. Truncate strings to 200 chars
# so turns.jsonl row size stays bounded; full detail lives in
# events.jsonl. Keep keys stable — turn viewer renders them.

_TRUNC_CHARS = 200


def _trunc(s: Any) -> Any:
    if isinstance(s, str) and len(s) > _TRUNC_CHARS:
        return s[: _TRUNC_CHARS - 1] + "…"
    return s


def _summarize_args(call_type: str, args: tuple, kwargs: dict) -> dict:
    """Compact, bounded summary of the call's input. Each call_type
    has a known signature, so this is a positional-vs-kwargs unify."""
    if call_type == "query":
        return {
            "query": _trunc(args[0] if args else kwargs.get("query", "")),
            "top_k": kwargs.get("top_k"),
            "mode": kwargs.get("mode"),
            "min_confidence_tier": kwargs.get("min_confidence_tier"),
            "session_id": kwargs.get("session_id"),
            "context_present": bool(kwargs.get("context")),
        }
    if call_type == "store":
        return {
            "content": _trunc(args[0] if args else kwargs.get("content", "")),
            "stream": kwargs.get("stream"),
            "profile": kwargs.get("profile"),
            "source_type": kwargs.get("source_type"),
        }
    if call_type == "feedback":
        atom_ids = args[0] if args else kwargs.get("atom_ids", [])
        return {
            "atom_ids": list(atom_ids) if atom_ids else [],
            "response_text": _trunc(
                args[1] if len(args) > 1 else kwargs.get("response_text", "")
            ),
            "feedback": kwargs.get("feedback"),
        }
    if call_type == "outcome":
        atom_ids = args[0] if args else kwargs.get("atom_ids", [])
        return {
            "atom_ids": list(atom_ids) if atom_ids else [],
            "feedback": args[1] if len(args) > 1 else kwargs.get("feedback"),
            "query": _trunc(kwargs.get("query") or ""),
        }
    if call_type == "end_session":
        return {
            "session_id": args[0] if args else kwargs.get("session_id"),
            "summary": _trunc(args[1] if len(args) > 1 else kwargs.get("summary", "")),
            "topics_discussed": kwargs.get("topics_discussed") or [],
            "decisions_made_count": len(kwargs.get("decisions_made") or []),
            "unfinished_count": len(kwargs.get("unfinished") or []),
        }
    # Default: shallow copy with truncation. Covers outcome / consolidate /
    # decay / forget — the per-call shapes are operator-tunable enough
    # that hardcoded summarizers would drift faster than the dict shape
    # itself. The 200-char string truncation bounds size regardless.
    return {
        "args": [_trunc(a) for a in args],
        **{k: _trunc(v) for k, v in kwargs.items()},
    }


def _summarize_result(
    call_type: str,
    result: Any,
    error: str | None,
) -> dict:
    """Compact, bounded summary of the call's output."""
    if error is not None:
        return {"ok": False}
    if not isinstance(result, dict):
        # Some clients return list (e.g. recent_session_boundaries —
        # not in _RECORDED_METHODS so this is a safety net).
        return {"ok": True, "type": type(result).__name__}
    if call_type == "query":
        # saga returns {atoms: [...]} or {observations, raws, triples}.
        atom_ids = []
        for k in ("atoms", "observations", "raws"):
            for a in result.get(k) or []:
                if isinstance(a, dict) and a.get("id"):
                    atom_ids.append(a["id"])
        return {
            "ok": True,
            "atom_ids": atom_ids[:50],  # bounded
            "atom_count": len(atom_ids),
            "rewritten_query": _trunc(result.get("rewritten_query") or ""),
        }
    if call_type == "store":
        return {
            "ok": True,
            "atom_id": result.get("atom_id") or result.get("id"),
        }
    if call_type == "feedback":
        return {
            "ok": True,
            "marked": result.get("marked"),
            "total": result.get("total"),
        }
    # Default: just status.
    return {"ok": True}


def make_saga_client(
    *,
    db_path: Path | None = None,
    embedding_dim: int | None = None,
    record_calls: bool = True,
) -> SagaClient:
    """Build the in-process saga client (``SagaStore``).

    saga runs in-process — the ``mimir.saga`` clean-room rewrite of saga's
    retrieval/consolidation engine. ``db_path`` defaults to
    ``$MIMIR_HOME/.mimir/saga.db``; pass explicitly to override (tests,
    alternative DB layouts).

    ``record_calls`` (default True): wrap the client in
    ``RecordingSagaClient`` so each call appends a ``SagaCallRecord`` to the
    active ``TurnContext.saga_calls``. Set False for tests that want the bare
    client without recording overhead.
    """
    import os
    from .saga.client import SagaStore

    if db_path is not None:
        resolved_db = Path(db_path)
    else:
        home = os.environ.get("MIMIR_HOME")
        if not home:
            raise RuntimeError(
                "make_saga_client(): MIMIR_HOME not set and db_path not "
                "supplied — cannot resolve the in-process SagaStore db path. "
                "Set MIMIR_HOME or pass db_path explicitly."
            )
        resolved_db = Path(home) / ".mimir" / "saga.db"
    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    inner: SagaClient = SagaStore(db_path=resolved_db, embedding_dim=embedding_dim)
    if record_calls:
        return RecordingSagaClient(inner)  # type: ignore[return-value]
    return inner


# ─── Helpers ─────────────────────────────────────────────────────


def _format_atom(a: dict[str, Any]) -> dict[str, Any]:
    """Per-atom shape mimir's pre-message hook + sagatools consume.

    Source of truth for the canonical shape: this function. Historically
    mirrored from the out-of-process saga server's ``_format_atom``; now
    that the runtime is in-process under ``mimir/saga/``, this IS the
    canonical shape. The integration bench at
    ``benchmarks/longmemeval_via_mimir/`` exercises it end-to-end."""
    topics = a.get("topics", [])
    if isinstance(topics, str):
        try:
            topics = json.loads(topics)
        except (json.JSONDecodeError, TypeError):
            topics = []

    metadata = a.get("metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}

    return {
        "id": a.get("id"),
        "content": a.get("content", ""),
        "stream": a.get("stream", "semantic"),
        "similarity": round(a.get("_similarity", 0), 3),
        "score": round(a.get("_combined_score", a.get("_activation", 0)), 3),
        "confidence_tier": a.get("_confidence_tier", "unknown"),
        "topics": topics,
        "metadata": metadata,
        "source_type": a.get("source_type", "unknown"),
    }
