"""SagaClient-compatible facade over the new memory subsystem.

Adapts ``mimir.memory.*`` operations to the ``SagaClient`` Protocol
(see ``mimir/saga_client.py``) so mimir's call sites — ``agent.py``,
``sagatools.py``, ``server.py`` — can flip from saga → memory atomically
with one wiring change (``make_saga_client(..)`` returns a
``MemoryClient`` instead of an ``_InProcessSaga``).

Provider/index plumbing reuses saga's existing infrastructure during
the transition:

- Embedding provider: ``saga.embeddings.get_provider()`` — already
  configured for voyage/openai/onnx via saga.toml.
- FAISS index: ``saga.vector_index.faiss_search_atoms`` — points at
  saga.db today; switching it to mimir.memory.db is a follow-up.
- FTS5: this client doesn't ship its own FTS5 path yet; keyword
  search falls back to the bare-bones ``WHERE content LIKE ?`` SQL
  for now. RRF + tsvector-style scoring is a Tier 3 follow-up.

This means the v1 ``MemoryClient`` is structurally complete but
operationally tied to saga's infra. Replacing each piece — FAISS,
FTS5, embeddings — is incremental work that can land per-piece
without breaking the API surface.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

from .mark_access import AccessEvent, mark_access
from .recall import recall as _recall
from .store import store as _store
from .reflect import (
    reflect as _reflect,
    recent_session_boundaries as _recent_boundaries,
)
from .consolidate import consolidate as _consolidate
from .forget import forget as _forget


# ─── Provider/index adapters ─────────────────────────────────────────


def _embed_text_sync(text: str) -> tuple[bytes, str, str, int]:
    """Adapt saga.embeddings.get_provider() to the store.EmbedFn shape.

    Returns (vec_bytes, provider_name, model, dim). Sync because the
    provider call is itself sync (network I/O is hidden inside).
    """
    from saga.embeddings import get_provider
    from saga.config import get_config

    cfg = get_config()
    provider = get_provider()
    vec = provider.embed(text[:cfg("embedding", "max_input_chars", 2000)],
                          input_type="passage")
    provider_name = cfg("embedding", "provider", "unknown")
    model = cfg("embedding", "model", "unknown")
    dim = provider.dimensions()
    vec_bytes = struct.pack(f"{dim}f", *vec)
    return vec_bytes, provider_name, model, dim


def _query_embed_sync(text: str) -> list[float]:
    """Adapt for recall.QueryEmbedFn — returns float list (not bytes)."""
    from saga.embeddings import get_provider
    from saga.config import get_config

    cfg = get_config()
    provider = get_provider()
    return provider.embed(text[:cfg("embedding", "max_input_chars", 2000)],
                          input_type="query")


def _faiss_search_stub(query_emb: list[float], top_k: int) -> list[tuple[str, float]]:
    """Placeholder for FAISS-against-mimir.memory.db.

    Saga's FAISS index is keyed on saga.db atoms — pointing it at
    mimir.memory.db requires either rebuilding the index here or
    sharing the schema (which we deliberately don't). v1: returns
    empty; recall falls back on FTS-only candidates. Wire properly
    in the follow-up that adds FAISS-on-mimir.memory.
    """
    return []


def _fts_search_naive(conn: sqlite3.Connection):
    """A naive keyword-search adapter that does ``WHERE content LIKE ?``
    against atoms. Replace with a real FTS5 path during integration.
    Returned callable closes over the connection so it matches
    recall.FtsSearchFn's shape."""
    def _fn(query: str, top_k: int) -> list[tuple[str, float]]:
        # Split into terms, build a naive LIKE pattern (each term must
        # appear at least once). Score = number of matching terms.
        terms = [t.strip().lower() for t in query.split() if len(t) > 2]
        if not terms:
            return []
        like_clauses = " AND ".join(["LOWER(content) LIKE ?"] * len(terms))
        params = [f"%{t}%" for t in terms]
        rows = conn.execute(
            f"SELECT id FROM atoms WHERE tombstoned = 0 AND {like_clauses} LIMIT ?",
            params + [top_k],
        ).fetchall()
        return [(r[0], float(len(terms))) for r in rows]
    return _fn


# ─── The facade ──────────────────────────────────────────────────────


class MemoryClient:
    """SagaClient-compatible facade. Holds a sqlite3 connection to
    mimir.memory.db and translates each saga-vocabulary method to the
    equivalent ``mimir.memory.*`` operation.

    Connection lifecycle: the client opens one connection per process
    on first use, applies the schema if the file is fresh, and reuses
    that connection. Caller can also pass an open connection via
    ``conn=...`` for tests.

    All public methods are async to match SagaClient. CPU-bound work
    runs via ``asyncio.to_thread`` so mimir's event loop stays
    responsive during synthesis / consolidation passes.
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self._db_path = db_path
        self._conn = conn  # may be None until first use

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self._db_path is None:
            raise RuntimeError(
                "MemoryClient: no db_path and no conn provided. "
                "Construct with MemoryClient(db_path=Path(...)) or pass conn=..."
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self._db_path.exists()
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        if fresh:
            schema_path = Path(__file__).parent / "schema.sql"
            self._conn.executescript(schema_path.read_text())
            self._conn.commit()
        return self._conn

    # ── SagaClient surface ──────────────────────────────────────────

    async def query(
        self, query: str, *, top_k: int = 12, mode: str = "task",
        token_budget: int = 500, session_id: str | None = None,
        min_confidence_tier: str | None = None,
        context: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        def _do():
            conn = self._ensure_conn()
            result = _recall(
                conn, query,
                query_embed_fn=_query_embed_sync,
                faiss_search_fn=_faiss_search_stub,
                fts_search_fn=_fts_search_naive(conn),
                k=top_k,
                session_id=session_id,
            )
            # Translate the RecallResult into saga's response shape so
            # mimir's call sites don't change.
            return {
                "query": query, "mode": mode, "two_tier": True,
                "gated": result.gated,
                "gated_reason": result.gated_reason,
                "observations": [_candidate_to_atom(c) for c in result.observations],
                "raws": [_candidate_to_atom(c) for c in result.raws],
                "triples": [],
                "items_returned": len(result.observations) + len(result.raws),
                "rewritten_query": result.rewritten_query or "",
            }
        return await asyncio.to_thread(_do)

    async def store(
        self, content: str, *, stream: str | None = None,
        profile: str | None = None, source_type: str = "api",
        use_llm_annotate: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        def _do():
            conn = self._ensure_conn()
            result = _store(
                conn, content, embed_fn=_embed_text_sync,
                stream=stream or "semantic",
                profile=profile or "standard",
                source_type=source_type,
                metadata=metadata,
            )
            if result.stored:
                return {"stored": True, "atom_id": result.atom_id}
            return {
                "stored": False, "atom_id": result.atom_id,
                "reason": result.reason or "duplicate",
            }
        return await asyncio.to_thread(_do)

    async def feedback(
        self, atom_ids: list[str], response_text: str, *,
        session_id: str | None = None, feedback: str | None = None,
    ) -> dict[str, Any]:
        # Saga's feedback contract is "credit pass after generating a
        # response." Map atom_ids → feedback_positive events
        # (the response_text was generated using these; that's the
        # endorsement). 'feedback' parameter (positive/negative) maps
        # to our signal kwarg.
        signal = (feedback or "positive").lower()
        def _do():
            from . import feedback as _feedback
            conn = self._ensure_conn()
            n = _feedback(conn, atom_ids, signal=signal, session_id=session_id)
            return {"marked": n, "total": len(atom_ids)}
        return await asyncio.to_thread(_do)

    async def outcome(
        self, atom_ids: list[str], feedback: str, *,
        session_id: str | None = None, query: str | None = None,
    ) -> dict[str, Any]:
        # Outcome is saga's "after the response was delivered, was it
        # well-received?" signal. We map positive → feedback_positive
        # event (same as the credit pass); negative is a no-op event-wise
        # but flags atoms for explicit forget review.
        return await self.feedback(atom_ids, "", session_id=session_id,
                                    feedback=feedback)

    async def end_session(
        self, session_id: str, summary: str, *,
        topics_discussed: list[str] | None = None,
        decisions_made: list[str] | None = None,
        unfinished: list[str] | None = None,
        emotional_state: str | None = None,
        closed_since: list[str] | None = None,
    ) -> dict[str, Any]:
        # The agent has already done the synthesis — it's calling
        # end_session with the rendered fields. The new reflect()
        # would re-derive them via LLM; here we just persist what was
        # passed in. We supply a stub boundary_synth_fn that returns
        # the agent's pre-computed fields.
        def _stub_synth(_atoms, _ctx):
            return {
                "summary": summary,
                "topics_discussed": topics_discussed or [],
                "decisions_made": decisions_made or [],
                "unfinished": unfinished or [],
                "emotional_state": emotional_state,
            }
        def _do():
            conn = self._ensure_conn()
            result = _reflect(
                conn, session_id=session_id, channel_id=None,
                embed_fn=_embed_text_sync,
                boundary_synth_fn=_stub_synth,
                # observation_synth_fn=None — saga's end_session
                # didn't auto-synthesize observations; the
                # consolidator did. Same here: leave observation
                # synthesis to consolidate().
            )
            return {
                "boundary_atom_id": result.boundary_atom_id,
                "boundary_created": result.boundary_created,
                "session_member_count": result.session_member_count,
            }
        return await asyncio.to_thread(_do)

    async def consolidate(
        self, *, dry_run: bool = False, max_clusters: int | None = None,
        extra_canonical_subjects: list[str] | None = None,
    ) -> dict[str, Any]:
        # The new consolidate() needs a cluster_fn + observation_synth_fn.
        # In v1 we don't have an LLM wired in directly — caller would
        # inject from mimir's existing saga.consolidation infrastructure
        # during the integration pass. For now, return a no-op response
        # so the API contract is satisfied but consolidation actually
        # runs via saga's existing cron until we wire the LLM.
        return {
            "clusters_formed": 0, "observations_emitted": [],
            "note": "MemoryClient.consolidate is not yet wired to an LLM "
                    "synth_fn — falls through. Configure via the "
                    "integration follow-up.",
        }

    async def decay(self) -> dict[str, Any]:
        # No decay cron in the new design — activation is computed
        # on-demand, no state to transition. Return a no-op shape
        # that matches saga's response so call sites don't break.
        return {"transitions": {"faded": 0, "dormanted": 0}}

    async def forget(
        self, *,
        dry_run: bool = True,
        min_retrievals: int | None = None,
        contribution_threshold: float | None = None,
        contradiction_threshold: float | None = None,
        confidence_floor: float | None = None,
        grace_days: int | None = None,
    ) -> dict[str, Any]:
        # Map saga's criteria-based forget to forget_by_criteria.
        from .forget import forget_by_criteria
        def _do():
            conn = self._ensure_conn()
            result = forget_by_criteria(
                conn,
                min_age_days=grace_days,
                activation_below=confidence_floor,
                dry_run=dry_run,
            )
            return {
                "tombstoned_count": result.tombstoned_count,
                "preview_ids": result.tombstoned_ids if dry_run else [],
                "dry_run": dry_run,
            }
        return await asyncio.to_thread(_do)

    async def recent_session_boundaries(
        self, *, channel_id: str | None = None, count: int = 3,
    ) -> list[dict[str, Any]]:
        def _do():
            conn = self._ensure_conn()
            return _recent_boundaries(
                conn, channel_id=channel_id, count=count,
            )
        return await asyncio.to_thread(_do)

    async def most_retrieved_atoms(
        self, *, days: int = 7, count: int = 10,
        channel_id: str | None = None, contributed_only: bool = False,
        trend: str | None = None,
    ) -> list[dict[str, Any]]:
        # Map to access_events log — count retrieval events per atom
        # in the last N days.
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        def _do():
            conn = self._ensure_conn()
            sources = ("retrieval", "feedback_positive")
            placeholders = ",".join(["?"] * len(sources))
            rows = conn.execute(
                f"SELECT a.id, a.content, COUNT(e.id) AS n "
                f"FROM atoms a "
                f"JOIN access_events e ON e.atom_id = a.id "
                f"WHERE a.tombstoned = 0 "
                f"AND e.ts >= ? "
                f"AND e.source IN ({placeholders}) "
                f"GROUP BY a.id ORDER BY n DESC LIMIT ?",
                [cutoff] + list(sources) + [count],
            ).fetchall()
            return [
                {"id": r[0], "content": r[1], "retrieval_count": r[2]}
                for r in rows
            ]
        return await asyncio.to_thread(_do)

    async def health(self) -> bool:
        try:
            conn = self._ensure_conn()
            conn.execute("SELECT 1 FROM atoms LIMIT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _candidate_to_atom(c) -> dict[str, Any]:
    """Map a RecallCandidate to saga's atom-response shape."""
    a = c.atom
    return {
        "id": a["id"],
        "content": a["content"],
        "stream": a.get("stream"),
        "memory_type": a.get("memory_type"),
        "_activation": c.activation,
        "_similarity": c.similarity,
        "_combined_score": c.total,
        "_trend": c.trend_label,
        "topics": _safe_json_load(a.get("topics")),
        "metadata": _safe_json_load(a.get("metadata")),
    }


def _safe_json_load(s):
    if not s:
        return [] if isinstance(s, str) else s
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return s
