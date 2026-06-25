"""LongMemEval runner that ingests + retrieves via mimir.saga.SagaStore.

Pipeline per question:

  1. Fresh per-question DB → ``SagaStore(db_path=...)``.
  2. Ingest every haystack turn as an atom (sync ``store()`` so we
     can backdate ``created_at`` to the session date afterward).
  3. ``consolidate()`` runs the cross-session pass: clustering + LLM-
     backed observation synthesis. Saga's ``run_eval.py`` runs the
     equivalent here ("clusters consolidated"); we mirror so the
     two-tier retrieval pathway has material.
  4. ``query()`` retrieves up to ``RETRIEVAL_TOP_K`` atoms in the
     two-tier shape ``{"observations": [...], "raws": [...]}``.
  5. Saga's existing reader prompt (``harness.read``) consumes the
     retrieved atoms and produces a hypothesis. Reader is provider-
     agnostic — works against the same OpenAI-compat / anthropic
     endpoints saga's bench used.
  6. Hypothesis lands in ``hypotheses_<run_tag>.jsonl``; per-question
     metrics land in ``metrics_<run_tag>.jsonl``.

The reader is reused from ``saga.benchmarks.longmemeval.harness`` to
keep apples-to-apples comparability with the saga baseline; only the
storage/retrieval backend changes between the two runners.

Usage:
    uv run python -m benchmarks.longmemeval_via_memory.runner \\
        --limit 5 --run-tag memory_v2_smoke

    # full 500
    uv run python -m benchmarks.longmemeval_via_memory.runner \\
        --run-tag memory_v2_full
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ─── Bench knobs ─────────────────────────────────────────────────────


# Match saga's bench knob so two-tier numbers are directly comparable.
RETRIEVAL_TOP_K = 20

# Ingest batch size for store(). Each store() call is ~1 embedding +
# 1 atom insert + 1 access_event; batching at this level mostly
# amortizes the embedding-provider network cost (voyage / openai batch
# both prefer >32 inputs per request).
INGEST_BATCH_SIZE = 256


# ─── Per-question DB ─────────────────────────────────────────────────


def _make_client(db_path: Path, *, embedding_dim: int | None = None):
    """Construct a SagaStore with a fresh per-question DB.

    Wires the bench-tuned P12 synonym dict (DEFAULT_LONGMEMEVAL_SYNONYMS)
    so the FTS5 keyword pathway gets the same query expansion saga's
    canonical bench used. RRF fusion is on by default in recall.py.

    ``embedding_dim=None`` (the default) lets ``SagaStore`` auto-
    detect the dimension from the first embedding row on first
    ``query()``. Hardcoding the wrong dim is a silent FAISS-killer:
    ``VectorIndex.build_from_db`` filters rows where stored dim
    doesn't match ``self.dimension``, so OpenAI 1536d embeddings
    against a 1024-dim index produced an empty FAISS index in the
    earlier 73.4% run — recall fell back to keyword-only.
    """
    from mimir.saga.client import SagaStore
    from mimir.saga.fts import DEFAULT_LONGMEMEVAL_SYNONYMS
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SagaStore(
        db_path=db_path,
        agent_id="longmemeval",
        embedding_dim=embedding_dim,
        synonyms=DEFAULT_LONGMEMEVAL_SYNONYMS,
    )


def _batch_embed_texts(texts: list[str]) -> list[tuple[bytes, str, str, int]]:
    """Batch-embed via the saga provider's ``batch_embed`` API (or fall
    back to per-text). Returns parallel list of
    ``(vec_bytes, provider, model, dim)`` tuples matching the shape
    ``mimir.saga.store.EmbedFn`` would produce.
    """
    import struct as _struct
    from mimir.saga.embeddings import get_provider
    from mimir.saga._config_io import get_config

    cfg = get_config()
    provider = get_provider()
    max_chars = cfg("embedding", "max_input_chars", 2000)
    provider_name = cfg("embedding", "provider", "unknown")
    model = cfg("embedding", "model", "unknown")
    dim = provider.dimensions()
    batch_size = cfg("embedding", "batch_size", 256)

    truncated = [t[:max_chars] for t in texts]
    vecs: list[list[float]] = []
    if hasattr(provider, "batch_embed"):
        for i in range(0, len(truncated), batch_size):
            chunk = truncated[i : i + batch_size]
            vecs.extend(provider.batch_embed(chunk, input_type="passage"))
    else:
        for t in truncated:
            vecs.append(provider.embed(t, input_type="passage"))

    out: list[tuple[bytes, str, str, int]] = []
    for v in vecs:
        vec_bytes = _struct.pack(f"{dim}f", *v)
        out.append((vec_bytes, provider_name, model, dim))
    return out


def _parse_question_date(question_date: str):
    """Parse LongMemEval's question_date ('YYYY/MM/DD (Day) HH:MM') into
    a UTC datetime. Passed as ``reference_date`` to recall() so the
    Petrov OL activation computes ages against the haystack's timeline,
    not wall-clock 2026. Without this every 2023-dated atom looks
    "3 years old" and activation cratering destroys temporal probes."""
    from datetime import datetime, timezone
    try:
        return datetime.strptime(
            question_date, "%Y/%m/%d (%a) %H:%M",
        ).replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, TypeError):
        return None


# ─── Ingest ──────────────────────────────────────────────────────────


def _parse_session_date(raw: str) -> str:
    """Parse a haystack session date into UTC ISO. Same parser saga
    uses — keeps temporal-anchor probes aligned with the haystack."""
    clean = raw.split("(")[0].strip() + " " + raw.rsplit(" ", 1)[-1]
    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(clean, fmt).replace(
                tzinfo=timezone.utc,
            ).isoformat()
        except ValueError:
            continue
    return datetime.now(timezone.utc).isoformat()


def _format_turn(date_iso: str, role: str, content: str) -> str:
    """Same atom-text shape saga uses; preserved so the reader prompt
    can lean on date prefixes for temporal probes."""
    date_tag = date_iso[:10]
    return f"[{date_tag} {role}] {content.strip()}"


def _iter_turns(q: dict):
    for sid, sdate, turns in zip(
        q["haystack_session_ids"],
        q["haystack_dates"],
        q["haystack_sessions"],
    ):
        iso = _parse_session_date(sdate)
        for i, turn in enumerate(turns):
            role = turn.get("role", "user")
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            yield {
                "session_id": sid,
                "session_date_iso": iso,
                "turn_index": i,
                "role": role,
                "has_answer": bool(turn.get("has_answer")),
                "text_for_atom": _format_turn(iso, role, content),
            }


def _iter_haystack_sessions(q: dict):
    for sid, sdate in zip(q["haystack_session_ids"], q["haystack_dates"]):
        yield {
            "session_id": sid,
            "session_date_iso": _parse_session_date(sdate),
        }


async def _ingest_question(client, q: dict) -> dict:
    """Ingest every haystack turn as a raw atom. Backdates
    ``created_at`` to the session date so temporal-reasoning probes
    work against the haystack's timeline rather than wall-clock now.

    Stream choice mirrors saga: user turns → episodic, assistant turns
    → semantic. Affects activation thresholds and the consolidation
    cluster grouping.

    Batches embedding calls (256 atoms per provider request) so the
    OpenAI / Voyage round trip cost amortizes — single-call ingest
    was ~250s/q on text-embedding-3-small with ~500 atoms; batched
    drops to ~30s/q.
    """
    turns = list(_iter_turns(q))
    if not turns:
        return {"ingested": 0, "total_turns": 0}

    # Batch-embed all turn texts up front via the provider's
    # batch_embed API (falls back to per-text if not available).
    embeddings = await asyncio.to_thread(_batch_embed_texts,
                                          [t["text_for_atom"] for t in turns])

    # Store atoms with the pre-computed embeddings.
    ingested = 0
    stored_ids: list[tuple[str, str]] = []
    for t, emb in zip(turns, embeddings):
        stream = "episodic" if t["role"] == "user" else "semantic"
        r = await client.store(
            t["text_for_atom"],
            stream=stream,
            source_type="longmemeval",
            session_id=t["session_id"],
            metadata={
                "session_id": t["session_id"],
                "session_date": t["session_date_iso"],
                "turn_index": t["turn_index"],
                "role": t["role"],
                "has_answer": t["has_answer"],
            },
            precomputed_embedding=emb,
        )
        if r.get("stored"):
            ingested += 1
        stored_ids.append((r["atom_id"], t["session_date_iso"]))

    # Backdate created_at on each atom to the session's date. Saga does
    # the same in ingest.py — without it, every atom looks "just now"
    # and temporal probes ("2 weeks ago") fail.
    def _backdate():
        conn = client._ensure_conn()
        conn.executemany(
            "UPDATE atoms SET created_at = ? WHERE id = ?",
            [(iso, aid) for aid, iso in stored_ids],
        )
        conn.commit()
    await asyncio.to_thread(_backdate)

    return {
        "ingested": ingested,
        "total_turns": len(turns),
    }


# ─── Generated session boundaries ───────────────────────────────────


async def _write_generated_session_boundaries(client, q: dict) -> dict:
    """Bench-only LongMemEval session reflection path.

    Synthesizes structured boundary fields with Saga's boundary prompt and
    persists them as real ``sessions`` rows through ``SagaStore.end_session``.
    This is separate from the closed PR #878 deterministic-summary rendering
    design: this path writes searchable session rows, then promotes atoms from
    matched sessions through retrieval rather than rendering summaries to the
    reader.
    """
    from mimir.saga.synthesize import make_async_boundary_synth_fn

    synth_boundary = make_async_boundary_synth_fn()
    written = 0
    total = 0

    for session in _iter_haystack_sessions(q):
        total += 1
        sid = session["session_id"]
        session_date_iso = session["session_date_iso"]

        def _load_atoms():
            conn = client._ensure_conn()
            cols = (
                "id", "content", "stream", "memory_type",
                "source_type", "created_at", "topics", "metadata",
            )
            rows = conn.execute(
                f"SELECT {', '.join(cols)} FROM atoms "
                "WHERE session_id = ? AND tombstoned = 0 "
                "ORDER BY created_at, rowid",
                (sid,),
            ).fetchall()
            return [dict(zip(cols, row)) for row in rows]

        atoms = await asyncio.to_thread(_load_atoms)
        fields = await synth_boundary(atoms, None)
        result = await client.end_session(
            sid,
            fields.get("summary") or "",
            topics_discussed=fields.get("topics_discussed") or [],
            decisions_made=fields.get("decisions_made") or [],
            unfinished=fields.get("unfinished") or [],
            emotional_state=fields.get("emotional_state"),
            channel_id="longmemeval",
        )
        if result.get("session_summary_written"):
            written += 1

        def _backdate_session():
            conn = client._ensure_conn()
            conn.execute(
                """
                UPDATE sessions
                   SET started_at = ?,
                       ended_at = ?,
                       reflected_at = ?
                 WHERE id = ?
                """,
                (session_date_iso, session_date_iso, session_date_iso, sid),
            )
            conn.commit()

        await asyncio.to_thread(_backdate_session)

    return {"session_boundaries_total": total, "session_boundaries_written": written}


# ─── Session-boundary atom promotion ────────────────────────────────


async def _session_boundary_rrf_pathway(
    client,
    question: str,
    *,
    limit: int = 3,
    alpha: float = 0.7,
    atoms_per_session: int = 30,
    weight: float = 0.5,
) -> tuple[list[str], dict[str, Any]]:
    """Resolve session-boundary matches into atom ids for an RRF lane.

    The boundary text is used only for session search. The reader never
    receives a rendered session-summary block; matched sessions promote
    their own atoms via ``extra_atom_ranked_pathways``.
    """
    matched_sessions = await client.search_sessions(
        question,
        alpha=alpha,
        limit=limit,
    )
    cap = max(0, int(atoms_per_session))

    def _load_atom_ids_for_sessions() -> dict[str, list[str]]:
        conn = client._ensure_conn()
        by_session: dict[str, list[str]] = {}
        for session in matched_sessions:
            sid = session.get("session_id")
            if not sid or cap <= 0:
                by_session[str(sid)] = []
                continue
            rows = conn.execute(
                """
                SELECT id
                  FROM atoms
                 WHERE session_id = ?
                   AND tombstoned = 0
                 ORDER BY created_at, rowid
                 LIMIT ?
                """,
                (sid, cap),
            ).fetchall()
            by_session[str(sid)] = [row[0] for row in rows]
        return by_session

    atoms_by_session = await asyncio.to_thread(_load_atom_ids_for_sessions)
    atom_ids: list[str] = []
    seen: set[str] = set()
    for session in matched_sessions:
        sid = session.get("session_id")
        for atom_id in atoms_by_session.get(str(sid), []):
            if atom_id not in seen:
                atom_ids.append(atom_id)
                seen.add(atom_id)

    debug = {
        "session_boundary_rrf_enabled": True,
        "session_boundary_limit": limit,
        "session_boundary_alpha": alpha,
        "session_boundary_weight": weight,
        "session_boundary_atoms_per_session": cap,
        "session_boundary_matched_sessions": [
            {
                "session_id": s.get("session_id"),
                "similarity_score": s.get("similarity_score"),
                "recency_score": s.get("recency_score"),
                "blended_score": s.get("blended_score"),
            }
            for s in matched_sessions
        ],
        "session_boundary_atoms_by_session": atoms_by_session,
        "session_boundary_atom_candidates": len(atom_ids),
    }
    return atom_ids, debug


# ─── Reader (reused from saga) ───────────────────────────────────────


def _read(question: str, question_date: str, retrieved: dict) -> dict:
    """Call saga's reader. The reader prompt and provider plumbing are
    independent of which memory backend produced the retrieved atoms,
    so reuse is the right call here — keeps the reader factor constant
    when comparing saga-baseline vs memory-baseline numbers."""
    from saga.benchmarks.longmemeval.harness import read
    return read(question, question_date, retrieved)


def _read_with_prompt(question: str, question_date: str, retrieved: dict) -> tuple[dict, list[dict]]:
    """Call saga's reader and return the exact reader messages used."""
    from saga.benchmarks.longmemeval.harness import build_prompt, call_reader

    # Mirrors saga.benchmarks.longmemeval.harness.read(); keep in sync so
    # prompt-capture smoke artifacts inspect the same reader path scored runs use.
    messages = build_prompt(question, question_date, retrieved)
    result = call_reader(messages)
    return {
        "hypothesis": result["text"],
        "reader_latency_ms": result["latency_ms"],
        "reader_prompt_tokens": result["prompt_tokens"],
        "reader_completion_tokens": result["completion_tokens"],
        "reader_model": result["model"],
    }, messages


def _parse_question_types(raw: str | None) -> list[str]:
    if raw is None:
        return []
    requested: list[str] = []
    for part in raw.split(","):
        category = part.strip()
        if category and category not in requested:
            requested.append(category)
    return requested


def _category_counts(dataset: list[dict], categories: list[str]) -> dict[str, int]:
    counts = {category: 0 for category in categories}
    for item in dataset:
        category = item.get("question_type")
        if category in counts:
            counts[category] += 1
    return counts


def _filter_question_types(dataset: list[dict], raw_question_types: str | None) -> list[dict]:
    requested = _parse_question_types(raw_question_types)
    if not requested:
        return dataset

    valid = sorted({
        item.get("question_type")
        for item in dataset
        if isinstance(item.get("question_type"), str)
    })
    unknown = sorted(set(requested) - set(valid))
    if unknown:
        raise ValueError(
            "unknown --question-types value(s): "
            f"{', '.join(unknown)}. Valid dataset question types: {', '.join(valid)}"
        )

    requested_set = set(requested)
    filtered = [
        item for item in dataset
        if item.get("question_type") in requested_set
    ]
    counts = _category_counts(filtered, requested)
    print(
        "Selected question types: "
        + ", ".join(f"{category}={counts[category]}" for category in requested),
        file=sys.stderr,
    )
    return filtered


# ─── Per-question runner ─────────────────────────────────────────────


async def _run_one(
    *,
    q: dict,
    work_dir: Path,
    keep_db: bool,
    consolidate_enabled: bool,
    session_boundary_treatment: str = "none",
    session_boundary_rrf_lane: bool = False,
    session_boundary_limit: int = 3,
    session_boundary_alpha: float = 0.7,
    session_boundary_weight: float = 0.5,
    session_boundary_atoms_per_session: int = 30,
    capture_reader_prompt: bool = False,
) -> tuple[dict | None, dict, str | None]:
    """Returns (hypothesis_record, metrics, error_str)."""
    qid = q["question_id"]
    db_path = work_dir / f"q_{qid}.db"
    err: str | None = None

    client = _make_client(db_path)

    metrics: dict[str, Any] = {
        "question_id": qid,
        "question_type": q.get("question_type"),
    }
    record: dict | None = None
    reader_prompt_messages: list[dict] | None = None

    try:
        # Ingest
        t0 = time.time()
        ingest_stats = await _ingest_question(client, q)
        metrics["ingest_s"] = round(time.time() - t0, 2)
        metrics["n_atoms_ingested"] = ingest_stats["ingested"]

        # Optional generated session-boundary lane. This writes real sessions
        # rows for search_sessions(), but the retrieved session summaries are
        # not rendered to the reader in this leaf.
        t0 = time.time()
        if session_boundary_treatment == "generated" or session_boundary_rrf_lane:
            boundary_stats = await _write_generated_session_boundaries(client, q)
        else:
            boundary_stats = {
                "session_boundaries_total": 0,
                "session_boundaries_written": 0,
            }
        metrics["session_boundaries_s"] = round(time.time() - t0, 2)
        metrics.update(boundary_stats)
        try:
            def _session_index_counts():
                conn = client._ensure_conn()
                row = conn.execute(
                    """
                    SELECT COUNT(*),
                           SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END)
                      FROM sessions
                    """
                ).fetchone()
                return int(row[0] or 0), int(row[1] or 0)

            indexed_sessions, embedded_sessions = await asyncio.to_thread(
                _session_index_counts,
            )
            metrics["session_boundary_indexed_sessions"] = indexed_sessions
            metrics["session_boundary_embedded_sessions"] = embedded_sessions
        except Exception:
            metrics["session_boundary_indexed_sessions"] = 0
            metrics["session_boundary_embedded_sessions"] = 0

        # Consolidate (LLM-backed; cross-session pass)
        n_clusters = 0
        n_triples = 0
        n_contra = 0
        n_supersedes_from_contra = 0
        t0 = time.time()
        if consolidate_enabled:
            try:
                cresult = await client.consolidate()
                n_clusters = cresult.get("clusters_consolidated", 0)
                # P42 + contradiction telemetry (Tier 3). Older versions of
                # SagaStore.consolidate didn't return these keys, so
                # .get(key, 0) keeps the runner compatible with pre-Tier 3
                # builds.
                n_triples = cresult.get("triples_stored", 0)
                n_contra = cresult.get("contradicts_stored", 0)
                n_supersedes_from_contra = cresult.get(
                    "supersedes_from_contradictions", 0,
                )
            except Exception as ce:
                # Don't kill the bench on consolidation failure; it
                # affects two-tier numbers but raws still rank.
                print(
                    f"  consolidation error on {qid}: {ce}", file=sys.stderr,
                )
        metrics["consolidate_s"] = round(time.time() - t0, 2)
        metrics["clusters_consolidated"] = n_clusters
        metrics["triples_stored"] = n_triples
        metrics["contradicts_stored"] = n_contra
        metrics["supersedes_from_contradictions"] = n_supersedes_from_contra

        # World-state row count + cumulative triples (counts ALL triples
        # in this question's DB, not just the ones added this call —
        # accumulating triple count across multiple consolidate passes
        # for cross-session DBs would otherwise be invisible). One
        # cheap COUNT(*) post-consolidate.
        def _post_consolidate_counts():
            conn = client._ensure_conn()
            t = conn.execute(
                "SELECT COUNT(*) FROM triples WHERE tombstoned = 0"
            ).fetchone()[0]
            w = conn.execute("SELECT COUNT(*) FROM world_state").fetchone()[0]
            return t, w
        try:
            n_triples_total, n_world_state = await asyncio.to_thread(
                _post_consolidate_counts,
            )
            metrics["triples_total"] = n_triples_total
            metrics["world_state_rows"] = n_world_state
        except Exception:
            # Pre-Tier-3 DBs lack the triples/world_state tables — skip
            # the counters rather than crash. The .get fallbacks above
            # already cover the consolidate response shape.
            pass

        # Query (two-tier shape — saga's reader handles either)
        t0 = time.time()
        ref_date = _parse_question_date(q["question_date"])
        extra_atom_ranked_pathways = None
        rrf_pathway_weights = None
        if session_boundary_rrf_lane:
            boundary_atom_ids, boundary_debug = await _session_boundary_rrf_pathway(
                client,
                q["question"],
                limit=session_boundary_limit,
                alpha=session_boundary_alpha,
                atoms_per_session=session_boundary_atoms_per_session,
                weight=session_boundary_weight,
            )
            extra_atom_ranked_pathways = {"session_boundary": boundary_atom_ids}
            rrf_pathway_weights = {"session_boundary": session_boundary_weight}
            metrics.update(boundary_debug)
        else:
            metrics.update({
                "session_boundary_rrf_enabled": False,
                "session_boundary_limit": session_boundary_limit,
                "session_boundary_alpha": session_boundary_alpha,
                "session_boundary_weight": session_boundary_weight,
                "session_boundary_atoms_per_session": session_boundary_atoms_per_session,
                "session_boundary_matched_sessions": [],
                "session_boundary_atoms_by_session": {},
                "session_boundary_atom_candidates": 0,
            })
        retrieved = await client.query(
            q["question"],
            top_k=RETRIEVAL_TOP_K,
            reference_date=ref_date,
            extra_atom_ranked_pathways=extra_atom_ranked_pathways,
            rrf_pathway_weights=rrf_pathway_weights,
        )
        metrics["retrieve_s"] = round(time.time() - t0, 2)
        metrics["n_observations"] = len(retrieved.get("observations", []))
        metrics["n_raws"] = len(retrieved.get("raws", []))
        metrics["n_atoms_retrieved"] = (
            metrics["n_observations"] + metrics["n_raws"]
        )

        # Reader
        t0 = time.time()
        if capture_reader_prompt:
            reader, reader_prompt_messages = await asyncio.to_thread(
                _read_with_prompt, q["question"], q["question_date"], retrieved,
            )
        else:
            reader = await asyncio.to_thread(
                _read, q["question"], q["question_date"], retrieved,
            )
        metrics["read_s"] = round(time.time() - t0, 2)
        metrics["reader_prompt_tokens"] = reader.get("reader_prompt_tokens")
        metrics["reader_completion_tokens"] = reader.get(
            "reader_completion_tokens",
        )
        if capture_reader_prompt:
            metrics["_reader_prompt_messages"] = reader_prompt_messages

        record = {"question_id": qid, "hypothesis": reader["hypothesis"]}
    except Exception as e:
        err = str(e)
        traceback.print_exc()
        metrics["error"] = err
    finally:
        await client.close()
        if not keep_db and db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                pass

    return record, metrics, err


# ─── Top-level driver ────────────────────────────────────────────────


def _load_done(output_path: Path) -> set[str]:
    done: set[str] = set()
    if not output_path.exists():
        return done
    for line in output_path.read_text().splitlines():
        try:
            done.add(json.loads(line)["question_id"])
        except Exception:
            continue
    return done


async def _amain(args) -> int:
    # Point saga's config loader at our bench saga.toml BEFORE any
    # saga imports trigger config resolution. This sets the
    # consolidation LLM to gpt-5.4-nano and the embedding provider
    # to OpenAI text-embedding-3-small (canonical bench setup).
    # Override by setting SAGA_CONFIG in the env before running.
    if not os.environ.get("SAGA_CONFIG"):
        default_cfg = Path(__file__).parent / "bench_saga.toml"
        if args.saga_config:
            os.environ["SAGA_CONFIG"] = str(Path(args.saga_config).resolve())
        else:
            os.environ["SAGA_CONFIG"] = str(default_cfg)

    # Load .env from repo root so OPENAI_API_KEY / MINIMAX_API_KEY land
    # in os.environ before any provider code reads them.
    repo_root = Path(__file__).resolve().parents[2]
    env_path = repo_root / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value

    # Resolve dataset path — defaults to saga's bench DATASET_PATH so
    # this runner doesn't ship its own copy. Set --dataset to point
    # elsewhere.
    if args.dataset:
        dataset_path = Path(args.dataset)
    else:
        from saga.benchmarks.longmemeval.config import DATASET_PATH
        dataset_path = DATASET_PATH
    if not dataset_path.exists():
        print(f"dataset not found: {dataset_path}", file=sys.stderr)
        return 2
    dataset = json.loads(dataset_path.read_text())
    try:
        dataset = _filter_question_types(dataset, args.question_types)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.limit:
        dataset = dataset[: args.limit]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(args.work_dir) if args.work_dir else (output_dir / "work")
    work_dir.mkdir(parents=True, exist_ok=True)

    out_path = output_dir / f"hypotheses_{args.run_tag}.jsonl"
    met_path = output_dir / f"metrics_{args.run_tag}.jsonl"
    debug_path = Path(args.retrieval_debug_jsonl) if args.retrieval_debug_jsonl else None
    if debug_path is not None:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done(out_path) if args.resume else set()
    mode = "a" if args.resume and done else "w"
    out_f = out_path.open(mode, buffering=1)
    met_f = met_path.open(mode, buffering=1)
    debug_f = debug_path.open(mode, buffering=1) if debug_path else None

    t_start = time.time()
    n_processed = 0
    errors = 0

    for i, q in enumerate(dataset):
        qid = q["question_id"]
        if qid in done:
            continue
        record, metrics, err = await _run_one(
            q=q,
            work_dir=work_dir,
            keep_db=args.keep_dbs,
            consolidate_enabled=not args.no_consolidate,
            session_boundary_treatment=args.session_boundary_treatment,
            session_boundary_rrf_lane=args.session_boundary_rrf_lane,
            session_boundary_limit=args.session_boundary_limit,
            session_boundary_alpha=args.session_boundary_alpha,
            session_boundary_weight=args.session_boundary_weight,
            session_boundary_atoms_per_session=args.session_boundary_atoms_per_session,
            capture_reader_prompt=args.capture_reader_prompt,
        )
        if err is not None:
            errors += 1
        if record is not None:
            out_f.write(json.dumps(record) + "\n")
        reader_prompt_messages = metrics.pop("_reader_prompt_messages", None)
        met_f.write(json.dumps(metrics) + "\n")
        if debug_f is not None:
            debug_record = {
                "question_id": qid,
                "question_type": q.get("question_type"),
                "session_boundary_rrf_enabled": metrics.get(
                    "session_boundary_rrf_enabled",
                ),
                "session_boundaries_written": metrics.get(
                    "session_boundaries_written",
                ),
                "session_boundary_indexed_sessions": metrics.get(
                    "session_boundary_indexed_sessions",
                ),
                "session_boundary_matched_sessions": metrics.get(
                    "session_boundary_matched_sessions",
                ),
                "session_boundary_atoms_by_session": metrics.get(
                    "session_boundary_atoms_by_session",
                ),
                "session_boundary_atom_candidates": metrics.get(
                    "session_boundary_atom_candidates",
                ),
                "session_boundary_weight": metrics.get(
                    "session_boundary_weight",
                ),
                "session_boundary_limit": metrics.get(
                    "session_boundary_limit",
                ),
                "session_boundary_alpha": metrics.get(
                    "session_boundary_alpha",
                ),
                "session_boundary_atoms_per_session": metrics.get(
                    "session_boundary_atoms_per_session",
                ),
            }
            if args.capture_reader_prompt:
                debug_record["reader_prompt_messages"] = reader_prompt_messages
            debug_f.write(json.dumps(debug_record) + "\n")
        n_processed += 1

        elapsed = time.time() - t_start
        print(
            f"[{i+1}/{len(dataset)}] {qid} ({q.get('question_type')}) "
            f"ingest={metrics.get('ingest_s', 0)}s "
            f"cons={metrics.get('consolidate_s', 0)}s"
            f"(n={metrics.get('clusters_consolidated', 0)}) "
            f"retrieve={metrics.get('retrieve_s', 0)}s "
            f"read={metrics.get('read_s', 0)}s "
            f"atoms={metrics.get('n_atoms_ingested', 0)}/"
            f"{metrics.get('n_observations', 0)}obs+"
            f"{metrics.get('n_raws', 0)}raws "
            f"elapsed={elapsed:.0f}s",
            flush=True,
        )

    out_f.close()
    met_f.close()
    if debug_f is not None:
        debug_f.close()
    print(
        f"\nDone. Processed {n_processed}, errors {errors}. "
        f"Total {(time.time() - t_start) / 60:.1f} min. "
        f"Hypotheses: {out_path}"
    )
    return 0 if errors == 0 else 1


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="benchmarks.longmemeval_via_memory.runner",
        description=(
            "LongMemEval through mimir.saga.SagaStore — bypasses "
            "saga entirely. Parallel to longmemeval_via_mimir."
        ),
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="cap number of questions (default: all 500)",
    )
    ap.add_argument(
        "--question-types", default=None,
        help=(
            "comma-separated LongMemEval question_type values to run "
            "(filter is applied before --limit)"
        ),
    )
    ap.add_argument(
        "--run-tag", required=True,
        help="identifier for the output filename "
             "(e.g. memory_v2_smoke)",
    )
    ap.add_argument(
        "--output-dir", default="results/longmemeval_via_memory/",
        help="directory for hypotheses + metrics JSONL output",
    )
    ap.add_argument(
        "--work-dir", default=None,
        help="directory for per-question SQLite files "
             "(default: <output-dir>/work)",
    )
    ap.add_argument(
        "--dataset", default=None,
        help="override the LongMemEval dataset JSON path",
    )
    ap.add_argument(
        "--resume", action="store_true",
        help="skip questions already present in the hypotheses file",
    )
    ap.add_argument(
        "--keep-dbs", action="store_true",
        help="don't delete per-question SQLite files (useful for "
             "post-hoc inspection / cluster spelunking)",
    )
    ap.add_argument(
        "--no-consolidate", action="store_true",
        help="skip the consolidate() pass — produces a raws-only baseline "
             "to compare against the two-tier number",
    )
    ap.add_argument(
        "--session-boundary-treatment",
        choices=("none", "generated"),
        default="none",
        help=(
            "bench-only session boundary mode. 'generated' synthesizes "
            "structured fields with Saga's boundary LLM prompt and writes "
            "real sessions rows; default 'none' preserves the previous "
            "via-memory behavior."
        ),
    )
    ap.add_argument(
        "--session-boundary-rrf-lane",
        action="store_true",
        help=(
            "enable bench-only session-boundary atom promotion: search "
            "sessions, expand matched sessions to atoms by atoms.session_id, "
            "and add them as a 'session_boundary' RRF pathway."
        ),
    )
    ap.add_argument(
        "--session-boundary-limit",
        type=int,
        default=3,
        help="number of sessions to retrieve for the boundary RRF lane",
    )
    ap.add_argument(
        "--session-boundary-alpha",
        type=float,
        default=0.7,
        help="semantic-vs-recency alpha for SagaStore.search_sessions",
    )
    ap.add_argument(
        "--session-boundary-weight",
        type=float,
        default=0.5,
        help="RRF pathway weight for promoted session-boundary atoms",
    )
    ap.add_argument(
        "--session-boundary-atoms-per-session",
        type=int,
        default=30,
        help="maximum atoms promoted from each matched session",
    )
    ap.add_argument(
        "--retrieval-debug-jsonl",
        default=None,
        help=(
            "optional JSONL path with per-question session-boundary "
            "retrieval debug details for smoke inspection"
        ),
    )
    ap.add_argument(
        "--capture-reader-prompt",
        action="store_true",
        help=(
            "include exact reader prompt messages in --retrieval-debug-jsonl. "
            "Use only for small smoke runs; full-slice prompt capture is noisy."
        ),
    )
    ap.add_argument(
        "--saga-config", default=None,
        help="override SAGA_CONFIG path. Default: bench_saga.toml in "
             "this directory (OpenAI text-embedding-3-small + "
             "gpt-5.4-nano consolidation LLM).",
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
