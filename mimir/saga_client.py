"""Saga client — Protocol with one in-process and one HTTP implementation.

Mimir interacts with the memory store through a unified ``SagaClient``
interface. Two implementations:

- ``SagaStore`` (in ``mimir.saga.client``) — the in-process
  retrieval/consolidation engine. Default for empty/localhost endpoints;
  same process, same SQLite directory, no HTTP loop. ``make_saga_client``
  constructs and returns one (wrapped in ``RecordingSagaClient``).
- ``_HttpSaga`` — the original aiohttp client against saga's FastAPI
  server. Used when ``SAGA_ENDPOINT`` is set to a non-localhost URL,
  i.e. an external saga deployment. Kept intact so multi-agent shared-
  saga setups still work.

The factory ``make_saga_client(config)`` selects the implementation based
on ``config.saga_endpoint``. mimir's call sites (``agent.py``, ``sagatools.py``,
``scheduler.py``, ``server.py``) work with either — they all do
``await client.<method>(...)``.

Errors from either implementation surface as ``SagaError``. The HTTP
client adds ``status``/``body`` for 4xx/5xx debugging; the in-process
client raises ``SagaError`` only for genuine logic errors (DB unreachable,
schema mismatch) since there's no transport layer to fail.

Long inputs to ``query`` are clamped client-side regardless of implementation.
saga's ``_fts5_query`` builds an OR-joined FTS5 expression from query tokens;
SQLite FTS5 caps expression depth at 1000, and a probe with several hundred
distinct tokens (common with Bluesky transcripts) blows that up. The cap
below keeps queries within FTS5's limits without hurting retrieval quality
(embedding-based retrieval doesn't care about token count past the embedder's
own truncation).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

import aiohttp

log = logging.getLogger(__name__)

# Hard cap on the keyword-token count we let saga build FTS5 expressions from.
# SQLite FTS5 trees are capped at depth 1000; a token cap of 64 keeps us well
# under that even with future saga internal-nesting changes. The cap is also
# essentially free in retrieval quality — beyond ~20 terms BM25 is dominated
# by a few salient words.
_MAX_QUERY_TOKENS = 64
# Backstop if the input has so few whitespace separators that even after
# truncation it would still blow up saga (e.g. one giant URL).
_MAX_QUERY_CHARS = 1500

# Retry policy for transient saga failures (5xx, ClientError, TimeoutError)
# in the HTTP client. 4xx is permanent and never retried. Total wait:
# 0.2 + 0.4 + 0.8 = 1.4s across 4 attempts, which covers a typical sidecar
# restart without hanging the agent. Past that, surface the error and let
# the caller log+continue.
#
# The two constants are loosely coupled: ``_MAX_RETRIES`` controls how many
# retry attempts fire, ``_RETRY_DELAYS_S`` provides the per-attempt sleep.
# Today they line up (3 retries, 3 delays) so ``_RETRY_DELAYS_S[attempt]``
# is always in-bounds. A future tuner who bumps ``_MAX_RETRIES`` past
# ``len(_RETRY_DELAYS_S)`` would IndexError mid-retry; the call sites use
# ``_retry_delay()`` to clamp the lookup defensively.
_MAX_RETRIES = 3
_RETRY_DELAYS_S = (0.2, 0.4, 0.8)


def _retry_delay(attempt: int) -> float:
    """Return the sleep duration for ``attempt`` (0-indexed), clamped to
    the last entry in ``_RETRY_DELAYS_S`` so the lookup is index-safe
    when ``_MAX_RETRIES`` is tuned past the tuple's length."""
    return _RETRY_DELAYS_S[min(attempt, len(_RETRY_DELAYS_S) - 1)]


class SagaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# ─── Protocol ────────────────────────────────────────────────────


@runtime_checkable
class SagaClient(Protocol):
    """The eight-method surface mimir uses against saga.

    Both ``SagaStore`` and ``_HttpSaga`` implement this. Most call
    sites accept ``SagaClient | None`` — None disables the integration
    (e.g., when running mimir without saga at all).
    """

    async def query(
        self, query: str, *, top_k: int = 12, mode: str = "task",
        token_budget: int = 500, session_id: str | None = None,
        min_confidence_tier: str | None = None,
        context: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]: ...

    async def store(
        self, content: str, *, stream: str | None = None,
        profile: str | None = None, source_type: str = "api",
        use_llm_annotate: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def feedback(
        self, atom_ids: list[str], response_text: str, *,
        session_id: str | None = None, feedback: str | None = None,
    ) -> dict[str, Any]: ...

    async def outcome(
        self, atom_ids: list[str], feedback: str, *,
        session_id: str | None = None, query: str | None = None,
    ) -> dict[str, Any]: ...

    async def end_session(
        self, session_id: str, summary: str, *,
        topics_discussed: list[str] | None = None,
        decisions_made: list[str] | None = None,
        unfinished: list[str] | None = None,
        emotional_state: str | None = None,
        closed_since: list[str] | None = None,
        channel_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def consolidate(
        self, *, dry_run: bool = False, max_clusters: int | None = None,
        extra_canonical_subjects: list[str] | None = None,
    ) -> dict[str, Any]: ...

    async def forget(
        self, *,
        dry_run: bool = True,
        min_retrievals: int | None = None,
        contribution_threshold: float | None = None,
        contradiction_threshold: float | None = None,
        confidence_floor: float | None = None,
        grace_days: int | None = None,
    ) -> dict[str, Any]: ...

    async def recent_session_boundaries(
        self, *, channel_id: str | None = None, count: int = 3,
    ) -> list[dict[str, Any]]: ...

    async def most_retrieved_atoms(
        self, *, days: int = 7, count: int = 10,
        channel_id: str | None = None, contributed_only: bool = False,
        trend: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def health(self) -> bool: ...
    async def close(self) -> None: ...


# ─── HTTP implementation (legacy / external-saga path) ───────────


class _HttpSaga:
    """Original aiohttp client — used when SAGA_ENDPOINT is set to a
    non-localhost URL, i.e. an external saga deployment is expected.

    This is the unchanged v0.4 ``SagaClient`` body, just renamed."""

    def __init__(
        self, endpoint: str, api_key: str | None = None, timeout_s: float = 30.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        # CR#9: serialize lazy init. Two concurrent first-call turns
        # both saw ``self._session is None`` and both constructed a
        # ``ClientSession``, with the loser's session leaking and
        # producing aiohttp deprecation warnings in production logs.
        # Mostly a multi-deployment edge case (default is ``SagaStore``)
        # but the lock is cheap and the failure mode is silent.
        self._session_lock = asyncio.Lock()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        # Cheap read first — once the session is up the common path
        # avoids the lock entirely.
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session
            headers: dict[str, str] = {}
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            self._session = aiohttp.ClientSession(timeout=self._timeout, headers=headers)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def health(self) -> bool:
        try:
            sess = await self._ensure_session()
            async with sess.get(f"{self._endpoint}/v1/health") as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
        except Exception:  # noqa: BLE001
            return False

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        sess = await self._ensure_session()
        url = f"{self._endpoint}{path}"
        # CR2 (memory & retrieval) fix: track the last exception
        # alongside the last 5xx body so the trailing "exhausted" raise
        # carries the original cause. Pre-fix, the trailing raise only
        # populated ``last_status`` / ``last_body`` from the 5xx path,
        # so a ClientError-driven exhaustion would lose the original
        # exception entirely. Today the trailing raise is unreachable
        # (every loop branch returns / raises / continues), but a
        # future edit dropping a ``continue`` would fall through; this
        # makes that future regression diagnostic-friendly.
        last_status: int | None = None
        last_body: str | None = None
        last_exc: BaseException | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with sess.post(url, json=body) as resp:
                    text = await resp.text()
                    if resp.status >= 500:
                        last_status = resp.status
                        last_body = text
                        if attempt < _MAX_RETRIES:
                            await asyncio.sleep(_retry_delay(attempt))
                            continue
                        # PR #112 re-review consistency: explicit ``from
                        # None`` matches the trailing ``raise ... from
                        # last_exc`` style. There's no enclosing
                        # exception to chain (we're inside the ``try``
                        # body, not the ``except``); ``from None``
                        # makes the absence-of-cause explicit rather
                        # than implicit-via-default.
                        raise SagaError(
                            f"SAGA {path} returned {resp.status} after {attempt + 1} attempts",
                            status=resp.status, body=text,
                        ) from None
                    if resp.status >= 400:
                        raise SagaError(
                            f"SAGA {path} returned {resp.status}",
                            status=resp.status, body=text,
                        ) from None
                    try:
                        return json.loads(text)
                    except ValueError as exc:
                        raise SagaError(f"SAGA {path} returned non-JSON body: {exc}") from exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                raise SagaError(
                    f"SAGA {path} failed after {attempt + 1} attempts: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        raise SagaError(
            f"SAGA {path} retry loop exhausted",
            status=last_status, body=last_body,
        ) from last_exc

    async def _get_or_empty(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """**Fast degrade-to-empty for prompt-assembly GETs (CR2-#11).**

        Asymmetric to ``_post`` by design: no retries, log + return
        ``{}`` on ANY failure (4xx/5xx, ClientError, timeout, non-JSON).
        Used by the prompt-assembly path — ``recent_session_boundaries``,
        ``most_retrieved_atoms`` — where adding ``_post``'s 1.4s retry
        backoff would block the prompt build on every transient blip.
        That's exactly the failure mode the agent's
        ``_assemble_session_summaries`` fallback (PR #96) is fighting:
        a SAGA outage at prompt-assembly time should degrade to local-
        mirror data, not stall the turn.

        Caller contract: empty dict means "no data this time, fall
        back if you have a fallback." Callers that need fallback
        semantics MUST handle the empty-result case explicitly —
        ``_assemble_session_summaries`` reads from the local mirror
        when this returns ``{}``.

        If a future endpoint genuinely needs retries (e.g. saga adds a
        ``/v1/expensive_lookup`` that's idempotent and rare enough to
        justify backoff), add a separate ``_get_with_retries`` rather
        than turning this asymmetry on for the existing callers — the
        prompt-assembly path is the load-bearing constraint.
        """
        try:
            sess = await self._ensure_session()
            async with sess.get(f"{self._endpoint}{path}", params=params) as resp:
                if resp.status >= 400:
                    log.warning("SAGA %s returned %d; degrading to empty", path, resp.status)
                    return {}
                text = await resp.text()
                try:
                    return json.loads(text)
                except ValueError:
                    log.warning("SAGA %s returned non-JSON; degrading to empty", path)
                    return {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("SAGA %s failed: %s", path, exc)
            return {}

    async def query(
        self, query: str, *, top_k: int = 12, mode: str = "task",
        token_budget: int = 500, session_id: str | None = None,
        min_confidence_tier: str | None = None,
        context: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": _clamp_query(query), "top_k": top_k,
            "mode": mode, "token_budget": token_budget,
        }
        if session_id:
            body["session_id"] = session_id
        if min_confidence_tier:
            body["min_confidence_tier"] = min_confidence_tier
        if context:
            body["context"] = context
        return await self._post("/v1/query", body)

    async def store(
        self, content: str, *, stream: str | None = None,
        profile: str | None = None, source_type: str = "api",
        use_llm_annotate: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "content": content, "source_type": source_type,
            "use_llm_annotate": use_llm_annotate,
        }
        if stream:
            body["stream"] = stream
        if profile:
            body["profile"] = profile
        if metadata:
            body["metadata"] = metadata
        return await self._post("/v1/store", body)

    async def feedback(
        self, atom_ids: list[str], response_text: str, *,
        session_id: str | None = None, feedback: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"atom_ids": atom_ids, "response_text": response_text}
        if session_id:
            body["session_id"] = session_id
        if feedback:
            body["feedback"] = feedback
        return await self._post("/v1/feedback", body)

    async def outcome(
        self, atom_ids: list[str], feedback: str, *,
        session_id: str | None = None, query: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"atom_ids": atom_ids, "feedback": feedback}
        if session_id:
            body["session_id"] = session_id
        if query:
            body["query"] = query
        return await self._post("/v1/outcome", body)

    async def end_session(
        self, session_id: str, summary: str, *,
        topics_discussed: list[str] | None = None,
        decisions_made: list[str] | None = None,
        unfinished: list[str] | None = None,
        emotional_state: str | None = None,
        closed_since: list[str] | None = None,
        channel_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"session_id": session_id, "summary": summary}
        if topics_discussed:
            body["topics_discussed"] = topics_discussed
        if decisions_made:
            body["decisions_made"] = decisions_made
        if unfinished:
            body["unfinished"] = unfinished
        if emotional_state:
            body["emotional_state"] = emotional_state
        if closed_since:
            body["closed_since"] = closed_since
        if channel_id:
            body["channel_id"] = channel_id
        return await self._post("/v1/sessions/end", body)

    async def consolidate(
        self, *, dry_run: bool = False, max_clusters: int | None = None,
        extra_canonical_subjects: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"dry_run": dry_run}
        if max_clusters is not None:
            body["max_clusters"] = max_clusters
        if extra_canonical_subjects:
            body["extra_canonical_subjects"] = list(extra_canonical_subjects)
        return await self._post("/v1/consolidate", body)

    async def forget(
        self, *,
        dry_run: bool = True,
        min_retrievals: int | None = None,
        contribution_threshold: float | None = None,
        contradiction_threshold: float | None = None,
        confidence_floor: float | None = None,
        grace_days: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"dry_run": dry_run}
        if min_retrievals is not None:
            body["min_retrievals"] = min_retrievals
        if contribution_threshold is not None:
            body["contribution_threshold"] = contribution_threshold
        if contradiction_threshold is not None:
            body["contradiction_threshold"] = contradiction_threshold
        if confidence_floor is not None:
            body["confidence_floor"] = confidence_floor
        if grace_days is not None:
            body["grace_days"] = grace_days
        return await self._post("/v1/forget", body)

    async def recent_session_boundaries(
        self, *, channel_id: str | None = None, count: int = 3,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"count": count}
        if channel_id:
            params["channel"] = channel_id
        data = await self._get_or_empty("/v1/sessions/recent", params)
        return data.get("sessions") or []

    async def most_retrieved_atoms(
        self, *, days: int = 7, count: int = 10,
        channel_id: str | None = None, contributed_only: bool = False,
        trend: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "days": days, "count": count,
            "contributed_only": "true" if contributed_only else "false",
        }
        if channel_id:
            params["channel"] = channel_id
        if trend:
            params["trend"] = trend
        data = await self._get_or_empty("/v1/atoms/most_retrieved", params)
        return data.get("atoms") or []


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
    # ``mark_contributions`` method on ``SagaStore`` or
    # ``_HttpSaga`` either; adding it to this set would AttributeError
    # at runtime.
    _RECORDED_METHODS = frozenset({
        "query", "store", "feedback", "outcome", "end_session",
        "consolidate", "forget",
    })

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
            "end_session", self._inner.end_session, args, kwargs,
        )

    async def consolidate(self, *args, **kwargs):
        return await self._call(
            "consolidate", self._inner.consolidate, args, kwargs,
        )

    async def forget(self, *args, **kwargs):
        return await self._call("forget", self._inner.forget, args, kwargs)

    async def _call(
        self, call_type: str, fn, args: tuple, kwargs: dict,
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
                    ctx.saga_calls.append(SagaCallRecord(
                        call_type=call_type,
                        args=_summarize_args(call_type, args, kwargs),
                        result=_summarize_result(call_type, result, error),
                        latency_ms=elapsed_ms,
                        error=error,
                        t_ms=t_ms,
                    ))
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
            "summary": _trunc(
                args[1] if len(args) > 1 else kwargs.get("summary", "")
            ),
            "topics_discussed": kwargs.get("topics_discussed") or [],
            "decisions_made_count": len(kwargs.get("decisions_made") or []),
            "unfinished_count": len(kwargs.get("unfinished") or []),
        }
    # Default: shallow copy with truncation. Covers outcome / consolidate /
    # decay / forget — the per-call shapes are operator-tunable enough
    # that hardcoded summarizers would drift faster than the dict shape
    # itself. The 200-char string truncation bounds size regardless.
    return {"args": [_trunc(a) for a in args], **{k: _trunc(v) for k, v in kwargs.items()}}


def _summarize_result(
    call_type: str, result: Any, error: str | None,
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
            for a in (result.get(k) or []):
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
    endpoint: str | None = None,
    api_key: str | None = None,
    *,
    db_path: "Path | None" = None,
    embedding_dim: int | None = None,
    timeout_s: float = 30.0,
    record_calls: bool = True,
) -> SagaClient:
    """Pick the right implementation based on ``endpoint``.

    - Empty/unset, ``localhost``, or ``127.0.0.1`` → in-process
      ``SagaStore`` (the mimir.saga clean-room rewrite of saga's
      retrieval/consolidation engine). ``db_path`` defaults to
      ``$MIMIR_HOME/.mimir/saga.db``; pass explicitly to override
      (tests, alternative DB layouts).
    - Anything else → ``_HttpSaga(endpoint, api_key, timeout_s)``
      (kept for operators running a separate saga HTTP server; this
      path will be retired once the in-process backend covers all
      production use cases).

    ``record_calls`` (default True): wrap the underlying client in
    ``RecordingSagaClient`` so each call appends a ``SagaCallRecord``
    to the active ``TurnContext.saga_calls``. Set False for tests
    that want to inspect the bare client without recording overhead.
    """
    import os
    from pathlib import Path
    if not endpoint or _is_localhost(endpoint):
        from .saga.client import SagaStore
        resolved_db: Path
        if db_path is not None:
            resolved_db = Path(db_path)
        else:
            home = os.environ.get("MIMIR_HOME")
            if not home:
                raise RuntimeError(
                    "make_saga_client(): MIMIR_HOME not set and db_path "
                    "not supplied — cannot resolve in-process SagaStore "
                    "db path. Set MIMIR_HOME or pass db_path explicitly."
                )
            resolved_db = Path(home) / ".mimir" / "saga.db"
        resolved_db.parent.mkdir(parents=True, exist_ok=True)
        inner: SagaClient = SagaStore(
            db_path=resolved_db, embedding_dim=embedding_dim,
        )
    else:
        inner = _HttpSaga(
            endpoint=endpoint, api_key=api_key, timeout_s=timeout_s,
        )
    if record_calls:
        return RecordingSagaClient(inner)  # type: ignore[return-value]
    return inner


def _is_localhost(endpoint: str) -> bool:
    """True if endpoint is an obvious localhost URL.

    Catches the default ``http://localhost:3002`` mimir setup writes when
    nothing's configured — that case should resolve to in-process, not
    HTTP-to-localhost (which would fail when no saga server is running).
    """
    e = endpoint.lower().strip()
    if not e:
        return True
    # Allow URL or bare host. We don't care about the port — saga server
    # at localhost:* is still a "this same machine" deployment, which the
    # operator hasn't explicitly opted out of in-process for.
    for marker in ("://localhost", "://127.0.0.1", "://0.0.0.0", "://[::1]"):
        if marker in e:
            return True
    return False


# ─── Helpers ─────────────────────────────────────────────────────


def _format_atom(a: dict[str, Any]) -> dict[str, Any]:
    """Mirror saga/saga/server.py::_format_atom — the per-atom shape
    mimir's pre-message hook + sagatools consume.

    Source of truth for the canonical shape lives in saga's server.py.
    Drift here will be caught by the v0.5 §3 integration bench."""
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


def _clamp_query(text: str) -> str:
    """Trim a /v1/query input to fit FTS5's expression-tree limit.

    Strategy: keep the first ``_MAX_QUERY_TOKENS`` whitespace-separated
    tokens; if the resulting string is still too long (e.g. one massive
    URL with no whitespace), truncate at ``_MAX_QUERY_CHARS``."""
    if not text:
        return text
    tokens = text.split()
    if len(tokens) > _MAX_QUERY_TOKENS:
        text = " ".join(tokens[:_MAX_QUERY_TOKENS])
    if len(text) > _MAX_QUERY_CHARS:
        text = text[:_MAX_QUERY_CHARS]
    return text
