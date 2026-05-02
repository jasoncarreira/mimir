"""Saga client — Protocol with two implementations (v0.5 §2).

Mimir interacts with saga's atom store through a unified ``SagaClient``
interface. Two implementations:

- ``_InProcessSaga`` — direct calls into ``saga.core`` via
  ``asyncio.to_thread``. Default since v0.5 §2: saga and mimir live in
  the same workspace, same process, same SQLite directory. No HTTP loop.
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
_MAX_RETRIES = 3
_RETRY_DELAYS_S = (0.2, 0.4, 0.8)


class SagaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# ─── Protocol ────────────────────────────────────────────────────


@runtime_checkable
class SagaClient(Protocol):
    """The eight-method surface mimir uses against saga.

    Both ``_InProcessSaga`` and ``_HttpSaga`` implement this. Most call
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
    ) -> dict[str, Any]: ...

    async def consolidate(
        self, *, dry_run: bool = False, max_clusters: int | None = None,
    ) -> dict[str, Any]: ...

    async def recent_session_boundaries(
        self, *, channel_id: str | None = None, count: int = 3,
    ) -> list[dict[str, Any]]: ...

    async def most_retrieved_atoms(
        self, *, days: int = 7, count: int = 10,
        channel_id: str | None = None, contributed_only: bool = False,
    ) -> list[dict[str, Any]]: ...

    async def health(self) -> bool: ...
    async def close(self) -> None: ...


# ─── In-process implementation ───────────────────────────────────


class _InProcessSaga:
    """Direct calls into saga.core via ``asyncio.to_thread``.

    saga's hot-path functions are sync (def, not async def) — embeddings,
    FTS5, vector ops are CPU-bound and hold the GIL while running. We wrap
    each call in ``to_thread`` so mimir's event loop stays responsive while
    saga does the heavy work. saga's FastAPI server does the same thing
    via uvicorn's executor; we make it explicit at the boundary.

    The response shape mirrors what saga's ``/v1/<endpoint>`` route handlers
    return, so mimir's call sites don't care which client they're using.
    Atom formatting (the ``_format_atom`` shape with id/content/similarity/
    score/confidence_tier/topics/metadata/source_type) is reproduced here
    because saga doesn't currently expose a service-layer function for it;
    the server handler is the source of truth (see ``saga/saga/server.py``
    ``api_query`` for the canonical implementation). If they drift, the
    integration bench (v0.5 §3) catches it.
    """

    def __init__(self) -> None:
        self._healthy: bool | None = None

    async def _ensure_ready(self) -> None:
        """Boot-time check: import saga + run get_stats. Surfaces config
        issues (missing DB, embedding model fails to load) immediately
        rather than at first query."""
        if self._healthy is not None:
            return
        try:
            await asyncio.to_thread(self._sync_stats)
            self._healthy = True
        except Exception as exc:
            self._healthy = False
            log.warning("in-process saga health check failed: %s", exc)
            raise SagaError(f"saga health check failed: {exc}") from exc

    @staticmethod
    def _sync_stats() -> dict[str, Any]:
        from saga.core import get_stats
        return get_stats()

    async def health(self) -> bool:
        try:
            await asyncio.to_thread(self._sync_stats)
            return True
        except Exception as exc:
            log.warning("in-process saga health probe failed: %s", exc)
            return False

    async def close(self) -> None:
        # Nothing to release — saga uses per-call SQLite connections.
        return

    async def query(
        self, query: str, *, top_k: int = 12, mode: str = "task",
        token_budget: int = 500, session_id: str | None = None,
        min_confidence_tier: str | None = None,
        context: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_ready()
        clamped = _clamp_query(query)
        try:
            return await asyncio.to_thread(
                self._sync_query,
                clamped, top_k, mode, token_budget,
                session_id, min_confidence_tier, context,
            )
        except SagaError:
            raise
        except Exception as exc:
            # Preserve the HTTP client's contract: transport-layer or
            # backend failures surface as SagaError. The agent's pre-message
            # hook catches SagaError and degrades gracefully (logs + skips
            # auto-fetch). RuntimeError from missing embedding API key,
            # SQLite OperationalError, etc. all fold into this path.
            raise SagaError(f"in-process saga query failed: {exc}") from exc

    @staticmethod
    def _sync_query(
        query: str, top_k: int, mode: str, token_budget: int,
        session_id: str | None, min_confidence_tier: str | None,
        context: list[dict[str, str]] | None,
    ) -> dict[str, Any]:
        # Mirrors saga/saga/server.py::api_query (two-tier branch — the
        # default since [retrieval].two_tier_enabled = true is the v0.5
        # canonical setting).
        import time
        from saga.core import hybrid_retrieve
        from saga.config import get_config
        cfg = get_config()
        t0 = time.time()
        result = hybrid_retrieve(
            query, mode=mode, top_k=top_k,
            two_tier=True, context=context, session_id=session_id,
        )
        obs = result.get("observations", []) or []
        raws = result.get("raws", []) or []

        gated_reason = None
        if cfg('retrieval', 'enable_confidence_gating', True):
            floor = (
                min_confidence_tier
                or cfg('retrieval', 'default_min_confidence_tier', 'low')
            )
            tier_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
            floor_rank = tier_rank.get(floor, 1)

            def _passes(a: dict) -> bool:
                t = a.get("_confidence_tier", "none")
                return tier_rank.get(t, 0) >= floor_rank

            obs_before, raws_before = len(obs), len(raws)
            obs = [o for o in obs if _passes(o)]
            raws = [r for r in raws if _passes(r)]
            obs_dropped = obs_before - len(obs)
            raws_dropped = raws_before - len(raws)
            if obs_dropped or raws_dropped:
                gated_reason = (
                    f"floor={floor}: dropped {obs_dropped} obs and "
                    f"{raws_dropped} raws below threshold"
                )

        return {
            "query": query, "mode": mode, "two_tier": True,
            "gated": gated_reason is not None,
            "gated_reason": gated_reason,
            "observations": [_format_atom(o) for o in obs],
            "raws": [_format_atom(r) for r in raws],
            "triples": [],
            "items_returned": len(obs) + len(raws),
            "latency_ms": round((time.time() - t0) * 1000, 2),
        }

    async def store(
        self, content: str, *, stream: str | None = None,
        profile: str | None = None, source_type: str = "api",
        use_llm_annotate: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_ready()
        try:
            return await asyncio.to_thread(
                self._sync_store, content, stream, profile,
                source_type, use_llm_annotate, metadata,
            )
        except SagaError:
            raise
        except Exception as exc:
            raise SagaError(f"in-process saga store failed: {exc}") from exc

    @staticmethod
    def _sync_store(
        content: str, stream: str | None, profile: str | None,
        source_type: str, use_llm_annotate: bool,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from saga.annotate import smart_annotate, classify_stream, classify_profile
        from saga.core import store_atom

        actual_stream = stream or classify_stream(content)
        actual_profile = profile or classify_profile(content)
        annotations = smart_annotate(content, use_llm=use_llm_annotate)
        result = store_atom(
            content=content, stream=actual_stream, profile=actual_profile,
            **annotations, source_type=source_type, metadata=metadata,
        )
        if isinstance(result, tuple):
            atom_id, reason = result
        else:
            atom_id = result
            reason = "duplicate content" if result is None else None

        if atom_id is None:
            return {
                "stored": False, "atom_id": None,
                "stream": actual_stream, "profile": actual_profile,
                "annotations": annotations,
                "triples_extracted": 0, "reason": reason,
            }
        return {
            "stored": True, "atom_id": atom_id,
            "stream": actual_stream, "profile": actual_profile,
            "annotations": annotations,
            "triples_extracted": 0,
        }

    async def feedback(
        self, atom_ids: list[str], response_text: str, *,
        session_id: str | None = None, feedback: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_ready()

        def _do() -> dict[str, Any]:
            from saga.core import mark_contributions
            return mark_contributions(atom_ids, response_text, session_id) or {}

        try:
            return await asyncio.to_thread(_do)
        except SagaError:
            raise
        except Exception as exc:
            raise SagaError(f"in-process saga feedback failed: {exc}") from exc

    async def outcome(
        self, atom_ids: list[str], feedback: str, *,
        session_id: str | None = None, query: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_ready()

        def _do() -> dict[str, Any]:
            from saga.core import record_outcome
            return record_outcome(atom_ids, feedback, session_id, query) or {}

        try:
            return await asyncio.to_thread(_do)
        except SagaError:
            raise
        except Exception as exc:
            raise SagaError(f"in-process saga outcome failed: {exc}") from exc

    async def end_session(
        self, session_id: str, summary: str, *,
        topics_discussed: list[str] | None = None,
        decisions_made: list[str] | None = None,
        unfinished: list[str] | None = None,
        emotional_state: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_ready()

        def _do() -> dict[str, Any]:
            from saga.core import store_session_boundary
            atom_id = store_session_boundary(
                session_id=session_id, summary=summary,
                topics_discussed=topics_discussed,
                decisions_made=decisions_made,
                unfinished=unfinished,
                emotional_state=emotional_state,
            )
            return {"atom_id": atom_id, "session_id": session_id, "channel": None}

        try:
            return await asyncio.to_thread(_do)
        except SagaError:
            raise
        except Exception as exc:
            raise SagaError(f"in-process saga end_session failed: {exc}") from exc

    async def consolidate(
        self, *, dry_run: bool = False, max_clusters: int | None = None,
    ) -> dict[str, Any]:
        await self._ensure_ready()

        def _do() -> dict[str, Any]:
            from saga.consolidation import ConsolidationEngine
            engine = ConsolidationEngine()
            kwargs: dict[str, Any] = {"dry_run": dry_run}
            if max_clusters is not None:
                kwargs["max_clusters"] = max_clusters
            result = engine.consolidate(**kwargs) or {}
            # mimir's scheduler logs the result; defensive flat dict.
            if not isinstance(result, dict):
                return {"result": result}
            return result

        try:
            return await asyncio.to_thread(_do)
        except SagaError:
            raise
        except Exception as exc:
            raise SagaError(f"in-process saga consolidate failed: {exc}") from exc

    async def recent_session_boundaries(
        self, *, channel_id: str | None = None, count: int = 3,
    ) -> list[dict[str, Any]]:
        await self._ensure_ready()

        def _do() -> list[dict[str, Any]]:
            from saga.core import get_last_sessions
            return get_last_sessions(count=count, channel=channel_id) or []

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:  # noqa: BLE001 — best-effort parity with HTTP client.
            log.warning("in-process recent_session_boundaries failed: %s", exc)
            return []

    async def most_retrieved_atoms(
        self, *, days: int = 7, count: int = 10,
        channel_id: str | None = None, contributed_only: bool = False,
    ) -> list[dict[str, Any]]:
        await self._ensure_ready()

        def _do() -> list[dict[str, Any]]:
            from saga.core import get_most_retrieved
            return get_most_retrieved(
                days=days, count=count,
                channel=channel_id,
                contributed_only=contributed_only,
            ) or []

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:  # noqa: BLE001
            log.warning("in-process most_retrieved_atoms failed: %s", exc)
            return []


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
                            status=resp.status, body=text,
                        )
                    if resp.status >= 400:
                        raise SagaError(
                            f"SAGA {path} returned {resp.status}",
                            status=resp.status, body=text,
                        )
                    try:
                        return json.loads(text)
                    except ValueError as exc:
                        raise SagaError(f"SAGA {path} returned non-JSON body: {exc}") from exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAYS_S[attempt])
                    continue
                raise SagaError(
                    f"SAGA {path} failed after {attempt + 1} attempts: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        raise SagaError(
            f"SAGA {path} retry loop exhausted",
            status=last_status, body=last_body,
        )

    async def _get_or_empty(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
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
        return await self._post("/v1/sessions/end", body)

    async def consolidate(
        self, *, dry_run: bool = False, max_clusters: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"dry_run": dry_run}
        if max_clusters is not None:
            body["max_clusters"] = max_clusters
        return await self._post("/v1/consolidate", body)

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
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "days": days, "count": count,
            "contributed_only": "true" if contributed_only else "false",
        }
        if channel_id:
            params["channel"] = channel_id
        data = await self._get_or_empty("/v1/atoms/most_retrieved", params)
        return data.get("atoms") or []


# ─── Factory ─────────────────────────────────────────────────────


def make_saga_client(
    endpoint: str | None = None,
    api_key: str | None = None,
    *,
    timeout_s: float = 30.0,
) -> SagaClient:
    """Pick the right implementation based on ``endpoint``.

    - Empty/unset, ``localhost``, or ``127.0.0.1`` → ``_InProcessSaga``.
    - Anything else → ``_HttpSaga(endpoint, api_key, timeout_s)``.

    saga still installs as a workspace member regardless, so importing
    saga.core works in either case. The HTTP path is just there for
    operators who explicitly want a separate saga deployment (shared
    saga across multiple agents, scaling, dev pointing at staging).
    """
    if not endpoint or _is_localhost(endpoint):
        return _InProcessSaga()
    return _HttpSaga(endpoint=endpoint, api_key=api_key, timeout_s=timeout_s)


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
