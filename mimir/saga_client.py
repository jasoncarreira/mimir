"""Async HTTP client for the SAGA REST API (SPEC §4.1, §9.3).

SAGA is the Python/FastAPI service at ``/Users/jcarreira/projects/odin/saga/``,
default port 3001 (config) — Mimir points at ``SAGA_ENDPOINT`` (default
``http://localhost:3002`` per SPEC §14, override per-deployment).

This client wraps just the endpoints Mimir needs:
- ``query``               — pre-message hit retrieval
- ``store``               — explicit atom store (rare; SAGA auto-extracts)
- ``feedback``            — mark_contributions for response credit
- ``outcome``             — explicit per-atom feedback (used by saga_feedback)
- ``end_session``         — write a session_boundary atom
- ``consolidate``         — weekly maintenance pass

Errors are surfaced as ``SagaError``; transient HTTP failures don't crash
the agent — caller decides how to log/retry. ``ClientSession`` is created
lazily and reused so we don't pay TCP setup per call.

Long inputs to ``query`` are clamped client-side. SAGA's ``_fts5_query``
constructs an OR-joined FTS5 expression from the input tokens; SQLite's
FTS5 caps expression depth at 1000, and a probe with several hundred
distinct tokens (common with Bluesky transcripts) produces a 500 from
the server. The cap below keeps queries within FTS5's limits while
preserving semantic-search quality (embedding-based retrieval doesn't
care about token count past the embedder's own truncation).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# Hard cap on the keyword-token count we let SAGA build FTS5 expressions from.
# SQLite FTS5 trees are capped at depth 1000; a token cap of 64 keeps us well
# under that even with future SAGA internal-nesting changes. The cap is also
# essentially free in retrieval quality — beyond ~20 terms BM25 is dominated
# by a few salient words.
_MAX_QUERY_TOKENS = 64
# Backstop if the input has so few whitespace separators that even after
# truncation it would still blow up SAGA (e.g. one giant URL).
_MAX_QUERY_CHARS = 1500

# Retry policy for transient SAGA failures (5xx, ClientError, TimeoutError).
# 4xx is permanent and never retried. Total wait: 0.2 + 0.4 + 0.8 = 1.4s
# across 4 attempts, which covers a typical sidecar restart without hanging
# the agent. Past that, surface the error and let the caller log+continue.
_MAX_RETRIES = 3
_RETRY_DELAYS_S = (0.2, 0.4, 0.8)


class SagaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class SagaClient:
    """One client per process. ``close()`` releases the underlying session.
    All methods are coroutines and never block the event loop."""

    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
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
        """True when SAGA responds 200 to /v1/health. Used by the integration
        smoke and by callers that want to skip SAGA gracefully when down."""
        try:
            sess = await self._ensure_session()
            async with sess.get(f"{self._endpoint}/v1/health") as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
        except Exception:  # noqa: BLE001
            return False

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST with exponential-backoff retry on transient failures.

        Retries: ``aiohttp.ClientError``, ``asyncio.TimeoutError``, and 5xx
        responses are retried up to ``_MAX_RETRIES`` times with delays
        ``[0.2s, 0.4s, 0.8s]``. 4xx responses are permanent — no retry.

        The SAGA sidecar restarts in <1s; the backoff window covers a typical
        restart without making the agent wait too long. Past 1.4s of total
        backoff, the failure is surfaced and the caller decides what to do.
        """
        sess = await self._ensure_session()
        url = f"{self._endpoint}{path}"
        last_exc: Exception | None = None
        last_status: int | None = None
        last_body: str | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with sess.post(url, json=body) as resp:
                    text = await resp.text()
                    if resp.status >= 500:
                        last_status = resp.status
                        last_body = text
                        if attempt < _MAX_RETRIES:
                            await asyncio.sleep(_RETRY_DELAYS_S[attempt])
                            continue
                        raise SagaError(
                            f"SAGA {path} returned {resp.status} after {attempt + 1} attempts",
                            status=resp.status,
                            body=text,
                        )
                    if resp.status >= 400:
                        # 4xx is permanent — bad request, auth failure, etc.
                        raise SagaError(
                            f"SAGA {path} returned {resp.status}",
                            status=resp.status,
                            body=text,
                        )
                    try:
                        return await _parse_json(text)
                    except ValueError as exc:
                        raise SagaError(f"SAGA {path} returned non-JSON body: {exc}") from exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAYS_S[attempt])
                    continue
                raise SagaError(
                    f"SAGA {path} failed after {attempt + 1} attempts: {type(exc).__name__}: {exc}"
                ) from exc
        # Unreachable — the loop either returns or raises every iteration.
        raise SagaError(
            f"SAGA {path} retry loop exhausted",
            status=last_status,
            body=last_body,
        )

    # ---- public API -----------------------------------------------------

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
    ) -> dict[str, Any]:
        """POST /v1/query. Returns the full response dict including raw atoms.

        ``session_id`` (any stable string per conversation) lets SAGA scope
        the access_log rows it writes for this retrieve, so a later
        ``feedback(session_id=...)`` call can credit *every* retrieval in
        the session window — not just the globally-most-recent row per
        atom (SAGA commit e88d458, schema migration 10).

        ``context`` is the prior conversation, most recent last. When SAGA
        has ``[retrieval] enable_contextual_rewrite = true`` and ``context``
        is non-empty, SAGA rewrites short / referential queries
        ("yes, look for that") into self-contained form before retrieval.
        Each entry is ``{"role": "user"|"assistant", "content": str}``;
        SAGA uses the last 10, truncating each content to 400 chars.
        Safe to always pass — flag-off and rewrite failures fall through
        to the original query.

        ``min_confidence_tier`` (``"none" | "low" | "medium" | "high"``) is
        the per-atom floor SAGA applies before returning. ``None`` lets
        SAGA use its ``[retrieval].default_min_confidence_tier`` config
        (today: ``"low"`` — drops sub-0.15 noise). Pass ``"medium"`` /
        ``"high"`` for high-stakes probes where a wrong answer is worse
        than no answer.

        Long inputs are clamped to ``_MAX_QUERY_TOKENS`` whitespace-separated
        tokens (and a backstop char limit) to avoid SQLite FTS5's 1000-deep
        expression-tree limit on SAGA's keyword path."""
        body: dict[str, Any] = {
            "query": _clamp_query(query),
            "top_k": top_k,
            "mode": mode,
            "token_budget": token_budget,
        }
        if session_id:
            body["session_id"] = session_id
        if min_confidence_tier:
            body["min_confidence_tier"] = min_confidence_tier
        if context:
            body["context"] = context
        return await self._post("/v1/query", body)

    async def store(
        self,
        content: str,
        *,
        stream: str | None = None,
        profile: str | None = None,
        source_type: str = "api",
        use_llm_annotate: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /v1/store. ``stream`` maps to SAGA's stream taxonomy
        (semantic/episodic/observation/...); leaving it None lets SAGA decide."""
        body: dict[str, Any] = {
            "content": content,
            "source_type": source_type,
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
        self,
        atom_ids: list[str],
        response_text: str,
        *,
        session_id: str | None = None,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/feedback — passive contribution credit (mark_contributions)."""
        body: dict[str, Any] = {"atom_ids": atom_ids, "response_text": response_text}
        if session_id:
            body["session_id"] = session_id
        if feedback:
            body["feedback"] = feedback
        return await self._post("/v1/feedback", body)

    async def outcome(
        self,
        atom_ids: list[str],
        feedback: str,
        *,
        session_id: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/outcome — explicit per-atom feedback. ``feedback`` must be
        one of SAGA's enum values: positive/negative/neutral/silence."""
        body: dict[str, Any] = {"atom_ids": atom_ids, "feedback": feedback}
        if session_id:
            body["session_id"] = session_id
        if query:
            body["query"] = query
        return await self._post("/v1/outcome", body)

    async def end_session(
        self,
        session_id: str,
        summary: str,
        *,
        topics_discussed: list[str] | None = None,
        decisions_made: list[str] | None = None,
        unfinished: list[str] | None = None,
        emotional_state: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/sessions/end. Returns ``{atom_id, session_id}``."""
        body: dict[str, Any] = {"session_id": session_id, "summary": summary}
        if topics_discussed:
            body["topics_discussed"] = topics_discussed
        if decisions_made:
            body["decisions_made"] = decisions_made
        if unfinished:
            body["unfinished"] = unfinished
        if emotional_state:
            body["emotional_state"] = emotional_state
        return await self._post("/v1/sessions/end", body)

    async def consolidate(self, *, dry_run: bool = False, max_clusters: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"dry_run": dry_run}
        if max_clusters is not None:
            body["max_clusters"] = max_clusters
        return await self._post("/v1/consolidate", body)

    async def recent_session_boundaries(
        self,
        *,
        channel_id: str | None = None,
        count: int = 3,
    ) -> list[dict[str, Any]]:
        """GET /v1/sessions/recent — chronological recall of session
        boundary atoms (most recent first), optionally filtered by
        channel. Distinct from /v1/query: query ranks by similarity,
        this ranks by recency.

        Best-effort: returns ``[]`` on any failure (4xx, 5xx, network)
        so the prompt-assembly path doesn't crash if SAGA is briefly
        down. The caller logs at warning."""
        params: dict[str, Any] = {"count": count}
        if channel_id:
            params["channel"] = channel_id
        data = await self._get_or_empty("/v1/sessions/recent", params)
        # SAGA commit 5545a6e ships {"sessions": [...]}.
        return data.get("sessions") or []

    async def most_retrieved_atoms(
        self,
        *,
        days: int = 7,
        count: int = 10,
        channel_id: str | None = None,
        contributed_only: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /v1/atoms/most_retrieved — top-N atoms by retrieval
        count over the last ``days`` days.

        Used by the reflection skill to identify atoms worth promoting
        to core memory (high-value content the agent retrieves and
        uses repeatedly), and as input for "what has the agent been
        thinking about lately?" pre-message context.

        ``contributed_only=True`` restricts the count to retrievals
        where ``access_log.contributed=1`` — atoms whose content
        actually showed up in the agent's reply per the credit-pass.
        Use this for promotion candidates; pass ``False`` to also see
        atoms that got pulled in but weren't used.

        Best-effort: returns ``[]`` on any failure."""
        params: dict[str, Any] = {
            "days": days,
            "count": count,
            "contributed_only": "true" if contributed_only else "false",
        }
        if channel_id:
            params["channel"] = channel_id
        data = await self._get_or_empty("/v1/atoms/most_retrieved", params)
        return data.get("atoms") or []

    async def _get_or_empty(
        self, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """GET that returns ``{}`` (instead of raising) on any failure.
        Used by the v0.4 reflection / prompt-time helpers where missing
        data degrades gracefully into an empty section."""
        try:
            sess = await self._ensure_session()
            async with sess.get(f"{self._endpoint}{path}", params=params) as resp:
                if resp.status >= 400:
                    log.warning(
                        "SAGA %s returned %d; degrading to empty",
                        path, resp.status,
                    )
                    return {}
                text = await resp.text()
                try:
                    return await _parse_json(text)
                except ValueError:
                    log.warning("SAGA %s returned non-JSON; degrading to empty", path)
                    return {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("SAGA %s failed: %s", path, exc)
            return {}


async def _parse_json(text: str) -> dict[str, Any]:
    return json.loads(text)


def _clamp_query(text: str) -> str:
    """Trim a /v1/query input to fit FTS5's expression-tree limit.

    Strategy: keep the first ``_MAX_QUERY_TOKENS`` whitespace-separated
    tokens; if the resulting string is still too long (e.g. one massive
    URL with no whitespace), truncate at ``_MAX_QUERY_CHARS``. The
    semantic embedder SAGA uses already truncates its own input, so we
    don't try to be cleverer than the upstream tokenizer here.
    """
    if not text:
        return text
    tokens = text.split()
    if len(tokens) > _MAX_QUERY_TOKENS:
        text = " ".join(tokens[:_MAX_QUERY_TOKENS])
    if len(text) > _MAX_QUERY_CHARS:
        text = text[:_MAX_QUERY_CHARS]
    return text
