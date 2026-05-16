"""LongMemEval runner that ingests + retrieves via mimir.saga.MemoryClient.

Pipeline per question:

  1. Fresh per-question DB → ``MemoryClient(db_path=...)``.
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
    """Construct a MemoryClient with a fresh per-question DB.

    Wires the bench-tuned P12 synonym dict (DEFAULT_LONGMEMEVAL_SYNONYMS)
    so the FTS5 keyword pathway gets the same query expansion saga's
    canonical bench used. RRF fusion is on by default in recall.py.

    ``embedding_dim=None`` (the default) lets ``MemoryClient`` auto-
    detect the dimension from the first embedding row on first
    ``query()``. Hardcoding the wrong dim is a silent FAISS-killer:
    ``VectorIndex.build_from_db`` filters rows where stored dim
    doesn't match ``self.dimension``, so OpenAI 1536d embeddings
    against a 1024-dim index produced an empty FAISS index in the
    earlier 73.4% run — recall fell back to keyword-only.
    """
    from mimir.saga.client import MemoryClient
    from mimir.saga.fts import DEFAULT_LONGMEMEVAL_SYNONYMS
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return MemoryClient(
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


# ─── Reader (reused from saga) ───────────────────────────────────────


def _read(question: str, question_date: str, retrieved: dict) -> dict:
    """Call saga's reader. The reader prompt and provider plumbing are
    independent of which memory backend produced the retrieved atoms,
    so reuse is the right call here — keeps the reader factor constant
    when comparing saga-baseline vs memory-baseline numbers."""
    from saga.benchmarks.longmemeval.harness import read
    return read(question, question_date, retrieved)


# ─── Per-question runner ─────────────────────────────────────────────


async def _run_one(
    *,
    q: dict,
    work_dir: Path,
    keep_db: bool,
    consolidate_enabled: bool,
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

    try:
        # Ingest
        t0 = time.time()
        ingest_stats = await _ingest_question(client, q)
        metrics["ingest_s"] = round(time.time() - t0, 2)
        metrics["n_atoms_ingested"] = ingest_stats["ingested"]

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
                # MemoryClient.consolidate didn't return these keys, so
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
        retrieved = await client.query(
            q["question"],
            top_k=RETRIEVAL_TOP_K,
            reference_date=ref_date,
        )
        metrics["retrieve_s"] = round(time.time() - t0, 2)
        metrics["n_observations"] = len(retrieved.get("observations", []))
        metrics["n_raws"] = len(retrieved.get("raws", []))
        metrics["n_atoms_retrieved"] = (
            metrics["n_observations"] + metrics["n_raws"]
        )

        # Reader
        t0 = time.time()
        reader = await asyncio.to_thread(
            _read, q["question"], q["question_date"], retrieved,
        )
        metrics["read_s"] = round(time.time() - t0, 2)
        metrics["reader_prompt_tokens"] = reader.get("reader_prompt_tokens")
        metrics["reader_completion_tokens"] = reader.get(
            "reader_completion_tokens",
        )

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
    if args.limit:
        dataset = dataset[: args.limit]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(args.work_dir) if args.work_dir else (output_dir / "work")
    work_dir.mkdir(parents=True, exist_ok=True)

    out_path = output_dir / f"hypotheses_{args.run_tag}.jsonl"
    met_path = output_dir / f"metrics_{args.run_tag}.jsonl"
    done = _load_done(out_path) if args.resume else set()
    mode = "a" if args.resume and done else "w"
    out_f = out_path.open(mode, buffering=1)
    met_f = met_path.open(mode, buffering=1)

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
        )
        if err is not None:
            errors += 1
        if record is not None:
            out_f.write(json.dumps(record) + "\n")
        met_f.write(json.dumps(metrics) + "\n")
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
            "LongMemEval through mimir.saga.MemoryClient — bypasses "
            "saga entirely. Parallel to longmemeval_via_mimir."
        ),
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="cap number of questions (default: all 500)",
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
        "--saga-config", default=None,
        help="override SAGA_CONFIG path. Default: bench_saga.toml in "
             "this directory (OpenAI text-embedding-3-small + "
             "gpt-5.4-nano consolidation LLM).",
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
