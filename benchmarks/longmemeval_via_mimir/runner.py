"""LongMemEval through mimir's BenchBridge dispatch path (v0.5 §3).

Boots mimir in-process with `_InProcessSaga` and a `BenchBridge` outbound,
ingests each LongMemEval question's haystack into a per-question saga
SQLite database, runs consolidation (so observations exist for the
two-tier retrieval pathway to surface), then drives the question through
``POST /event`` so mimir's pre-message hook (saga query +
session_boundaries surfacing) and post-message hook
(mark_contributions) both fire. The agent's reply is read from
``turns.jsonl`` and written to a hypotheses JSONL the existing saga
evaluator scores.

Note on contextual rewrite: it's enabled in the bench saga.toml but
won't fire on these probes — mimir's pre-message hook passes
``context=`` from the channel's chat buffer, which is empty for a
fresh bench channel. To exercise rewrite end-to-end we'd need
multi-turn probes (LongMemEval doesn't have those).

Usage:
    cd mimir
    uv run python -m benchmarks.longmemeval_via_mimir.runner \\
        --limit 5 \\
        --run-tag mimir_v0_5_smoke \\
        --output-dir results/longmemeval_via_mimir/

For a full 500-question run, drop ``--limit`` (and budget several hours
plus an OpenAI API key for the judge).

This file is intentionally a *driver*. Heavy lifting (atom storage,
retrieval, scoring) reuses saga's bench infrastructure. The reason for
this harness existing AT ALL is the cache/contextual-rewrite/credit-pass
interactions that are invisible to saga-direct benches — see V0.5.md §3.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .route import channel_id_for, question_to_event
from .score import evaluate_command, write_hypotheses_jsonl


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="benchmarks.longmemeval_via_mimir.runner",
        description=(
            "Run LongMemEval through mimir's BenchBridge dispatch path. "
            "Captures cache, contextual rewrite, and credit-pass effects "
            "that are invisible to saga-direct benches."
        ),
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="number of questions to run; default=all 500",
    )
    p.add_argument(
        "--run-tag", required=True,
        help="identifier for the output filename (e.g. mimir_v0_5_smoke)",
    )
    p.add_argument(
        "--output-dir", default="results/longmemeval_via_mimir/",
        help="directory for hypothesis JSONL output",
    )
    p.add_argument(
        "--dataset-path", default=None,
        help="override the LongMemEval dataset JSON path (defaults to "
             "saga.benchmarks.longmemeval.config.DATASET_PATH)",
    )
    p.add_argument(
        "--keep-dbs", action="store_true",
        help="keep per-question SQLite databases after the run "
             "(useful for offline inspection)",
    )
    p.add_argument(
        "--mimir-home", default=None,
        help="MIMIR_HOME for the bench agent (defaults to a per-run "
             "tmpdir under the output dir)",
    )
    p.add_argument(
        "--saga-config", default=None,
        help="path to a saga.toml to use instead of the bench-mode "
             "default. Lets two benches run side-by-side with different "
             "saga configs (mirrors saga's own run_eval --config knob); "
             "pair with distinct --run-tag and --mimir-home so per-DB / "
             "per-home / output paths don't collide.",
    )
    return p.parse_args(argv)


def _resolve_dataset(args: argparse.Namespace) -> Path:
    if args.dataset_path:
        return Path(args.dataset_path)
    from saga.benchmarks.longmemeval.config import DATASET_PATH
    return DATASET_PATH


_BENCH_SAGA_TOML_TEMPLATE = """\
# saga.toml for the integration bench. Overwrites the default
# mimir setup writes so token budgets don't refuse LongMemEval
# haystacks (single-question haystacks push thousands of atoms,
# well past the 1M-token production cap).

[storage]
db_path = "{db_path}"
metrics_db_path = "{metrics_db_path}"
# Match saga_bench.toml: effectively unlimited.
token_budget_ceiling = 100000000
auto_compact_threshold_pct = 99
refuse_threshold_pct = 100
db_busy_timeout_ms = 5000

[embedding]
provider = "openai"
url = "https://api.openai.com/v1/embeddings"
model = "text-embedding-3-small"
dimensions = 1536
api_key_env = "OPENAI_API_KEY"

[llm]
# Bench LLM. Default claude_code (Max OAuth, free, slow) — flip to
# openai_compat + gpt-5.4-nano via SAGA_BENCH_LLM_PROVIDER if you
# want direct bench parity against the saga_p30_canon_v4 baseline.
provider = "{llm_provider}"
model = "{llm_model}"
{llm_extra}
timeout_seconds = 120

[retrieval]
# v0.5 §2 mimir-prod overrides — same as the default saga.toml, with
# one bench-specific tweak: contextual_rewrite is OFF. LongMemEval is
# single-turn so the rewrite gate never fires (channel chat buffer is
# empty when the bench POSTs /event), but leaving it on misleads
# anyone reading the bench config; explicit-off documents the intent.
enable_contextual_rewrite = false
two_tier_enabled = true
enable_missing_ref_pivot = true
enable_confidence_gating = true
default_min_confidence_tier = "low"

[retrieval_v2]
# P12 synonym expansion on the FTS5/keyword pathway. The flag is a
# no-op without a populated [query_expansion.synonyms] block, so the
# block below ships with the bench config rather than living in a
# separate operator-managed file.
enable_query_expansion = true

[query_expansion.synonyms]
# Starter synonym dict tuned for LongMemEval's question patterns
# (profession/home/schedule/relationship/preference/transit). The
# semantic pathway already handles synonyms via embedding similarity,
# so this only affects the keyword (FTS5) leg of hybrid retrieval.
# Add entries here, not in operator-managed configs — the bench is
# meant to be reproducible from this file alone.
profession = ["job", "career", "work", "occupation", "employed"]
home = ["hometown", "residence", "lives", "address", "neighborhood"]
schedule = ["routine", "calendar", "plan", "appointment", "meeting"]
family = ["spouse", "wife", "husband", "partner", "children", "kids", "parent", "mom", "dad", "sibling"]
preference = ["like", "favorite", "prefer", "enjoy", "love"]
commute = ["drive", "travel", "transit", "ride", "route"]
school = ["college", "university", "graduated", "degree", "studied", "education"]

[triples]
enable_extraction = true

[consolidation]
enabled = true
enable_llm = true

[server]
api_key = ""
"""


def _write_bench_saga_toml(home: Path) -> None:
    """Overwrite ``<home>/saga.toml`` with bench-friendly settings.

    The default saga.toml mimir setup writes caps storage at 1M tokens —
    fine for daily use, fatal for LongMemEval haystacks. This bench
    saga.toml uses saga_bench.toml's effectively-unlimited cap. LLM
    config respects ``SAGA_BENCH_LLM_PROVIDER`` (default ``claude_code``
    for free Max OAuth; set to ``openai_compat`` to use gpt-5.4-nano for
    bench parity).
    """
    saga_dir = home / ".mimir"
    saga_dir.mkdir(parents=True, exist_ok=True)

    provider = os.environ.get("SAGA_BENCH_LLM_PROVIDER", "claude_code").strip().lower()
    if provider == "openai_compat":
        model = os.environ.get("SAGA_BENCH_LLM_MODEL", "gpt-5.4-nano")
        extra = (
            'url = "https://api.openai.com/v1/chat/completions"\n'
            'api_key_env = "OPENAI_API_KEY"'
        )
    elif provider == "anthropic":
        model = os.environ.get("SAGA_BENCH_LLM_MODEL", "claude-haiku-4-5")
        extra = 'api_key_env = "ANTHROPIC_API_KEY"'
    else:
        provider = "claude_code"
        model = os.environ.get("SAGA_BENCH_LLM_MODEL", "claude-haiku-4-5")
        extra = ""

    body = _BENCH_SAGA_TOML_TEMPLATE.format(
        db_path=saga_dir / "saga.db",
        metrics_db_path=saga_dir / "saga_metrics.db",
        llm_provider=provider,
        llm_model=model,
        llm_extra=extra,
    )
    (home / "saga.toml").write_text(body)


_BENCH_REFERENCE_DATE = None  # set per-question in _run_one_question
_BENCH_TOP_K = 20  # match saga.benchmarks.longmemeval.config.RETRIEVAL_TOP_K


def _install_saga_bench_overrides() -> None:
    """Bench-only monkey-patches that match saga's run_eval.py shape:

    1. ``saga.core.hybrid_retrieve`` injects per-question
       ``reference_date`` (parsed from ``q["question_date"]``) when the
       caller doesn't supply one. mimir's pre-message hook calls
       ``saga.query()`` without ref_date — production gets ``now()``,
       which is correct for live agents but wrong for a 2023-dated
       LongMemEval haystack run from 2026 (every "2 weeks ago" probe
       computes against the wrong "now"). saga's run_eval parses the
       question_date and threads it through; we replicate that here
       without touching the SagaClient API.

    2. ``mimir.saga_client._InProcessSaga.query`` bumps any caller-
       supplied ``top_k`` to ``_BENCH_TOP_K`` (20). saga's bench used
       RETRIEVAL_TOP_K=20; mimir's pre-message hook hardcodes 12. The
       agent gets more retrieved-atom context this way — same shape the
       Minimax reader had in saga's run_eval.

    Both patches are idempotent — calling this twice is safe. Both are
    scoped to the bench process; production mimir is untouched.
    """
    import saga.core as _saga_core
    if not getattr(_saga_core, "_bench_patched", False):
        _orig_retrieve = _saga_core.hybrid_retrieve

        async def _patched_retrieve(*args, reference_date=None, **kwargs):
            if reference_date is None and _BENCH_REFERENCE_DATE is not None:
                reference_date = _BENCH_REFERENCE_DATE
            return await _orig_retrieve(*args, reference_date=reference_date, **kwargs)

        _saga_core.hybrid_retrieve = _patched_retrieve
        _saga_core._bench_patched = True

    from mimir import saga_client as _sc
    if not getattr(_sc._InProcessSaga, "_bench_patched", False):
        _orig_query = _sc._InProcessSaga.query

        async def _patched_query(self, query, *, top_k=12, **kwargs):
            return await _orig_query(self, query, top_k=_BENCH_TOP_K, **kwargs)

        _sc._InProcessSaga.query = _patched_query
        _sc._InProcessSaga._bench_patched = True


def _parse_question_date(question: dict) -> Any:
    from datetime import datetime, timezone
    try:
        return datetime.strptime(
            question["question_date"], "%Y/%m/%d (%a) %H:%M",
        ).replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, TypeError):
        return None


def _switch_saga_db(db_path: Path) -> None:
    """Point the in-process saga at a fresh SQLite file for this question.

    Mirrors saga.benchmarks.longmemeval.run_eval._switch_db; saga's bench
    tooling is the source of truth for the per-question DB lifecycle. We
    repeat it here because the integration runner doesn't go through
    run_eval — mimir's BenchBridge owns the dispatch loop, and it would
    be confusing to import the saga runner just for this one helper."""
    import saga.core
    import saga.triples
    saga.core.DB_PATH = db_path
    saga.triples.DB_PATH = db_path
    if db_path.exists():
        db_path.unlink()
    from saga.core import get_db, run_migrations
    conn = get_db()
    conn.close()
    run_migrations()
    from saga.triples import init_triples_schema
    init_triples_schema()
    # Reset FAISS singletons so they're rebuilt against the fresh DB.
    # saga.vector_index._atoms_index is module-level; without reset, the
    # index built for question N persists into N+1 (and grows unbounded
    # via on_atom_stored hooks during the next ingest). Cross-question
    # cluster lookups still produce the right answer because atom_map
    # filters out N-1's IDs, but memory + search work grows linearly
    # with question count for no benefit.
    from saga.vector_index import reset_indexes
    reset_indexes()


async def _run_one_question(
    *,
    question: dict[str, Any],
    dispatcher: Any,
    bench_bridge: Any,
    bench_stream: io.StringIO,
    client: Any,
    turns_log: Path,
) -> dict[str, Any] | None:
    """Drive a single LongMemEval question through mimir's dispatcher.

    Returns the hypothesis record `{"question_id", "hypothesis"}` or None
    on failure (logged + skipped).

    Hypothesis source: the turn record's ``output`` field in
    ``turns.jsonl``. The agent's text reply for a default Q→A turn lands
    there, NOT in BenchBridge — BenchBridge only captures outbound when
    the agent explicitly calls the ``send_message`` tool, which it
    doesn't for a normal user_message reply. We still pass through the
    bench_stream as a secondary check in case the agent did use
    send_message.

    The aiohttp ``client`` is created ONCE for the whole run and reused
    across questions. Re-creating it per question (the previous design)
    triggered ``app.on_startup`` / ``on_cleanup`` each iteration,
    starting and tearing down the dispatcher worker — only the LAST
    question's turn actually completed because the worker shut down
    between iterations.
    """
    from saga.benchmarks.longmemeval.ingest import ingest_question

    qid = question["question_id"]
    import time as _time

    # Per-question saga DB (replicates saga's bench isolation).
    work_dir = Path(os.environ.get("SAGA_DATA_DIR", "."))
    db_path = work_dir / f"q_{qid}.db"
    _switch_saga_db(db_path)
    # Anchor saga's temporal pathway to the question's contemporaneous
    # date; otherwise temporal-reasoning probes ("2 weeks ago", "last
    # spring") compute against system clock and miss every time on a
    # 2023-haystack-from-2026 bench.
    global _BENCH_REFERENCE_DATE
    _BENCH_REFERENCE_DATE = _parse_question_date(question)

    t_phase = _time.time()
    stats = ingest_question(question)
    t_ingest = _time.time() - t_phase

    # Consolidate. saga's run_eval.py runs this between ingest and
    # retrieve so the observation-bonus + two-tier observations have
    # material to surface; without it, the agent only sees raw atoms.
    from saga.config import get_config as _saga_get_config
    t_consolidate = 0.0
    n_clusters = 0
    if _saga_get_config()('consolidation', 'enabled', False):
        from saga.consolidation import ConsolidationEngine
        t_phase = _time.time()
        try:
            cresult = await ConsolidationEngine().consolidate() or {}
            n_clusters = cresult.get("clusters_consolidated", 0) if isinstance(cresult, dict) else 0
        except Exception as exc:  # noqa: BLE001 — don't kill the run
            print(f"  consolidation failed for {qid}: {exc}", file=sys.stderr)
        t_consolidate = _time.time() - t_phase

    # Snapshot turn-log size + bench stream position so we can read just
    # the new content after this question's turn finishes.
    turns_pos_before = turns_log.stat().st_size if turns_log.exists() else 0
    stream_pos_before = bench_stream.tell()

    body = question_to_event(question)
    channel_id = body["channel_id"]
    t_phase = _time.time()
    resp = await client.post("/event", json=body)
    if resp.status != 200:
        text = await resp.text()
        print(
            f"  /event POST for {qid} returned {resp.status}: {text[:300]}",
            file=sys.stderr,
        )
        return None
    # Wait for *this question's* turn to finish without closing the
    # dispatcher. dispatcher.drain() sets _closed=True and cancels
    # workers, breaking subsequent /event POSTs (queue_full_or_closed).
    # Per-channel queue.join() blocks until the worker calls task_done()
    # — i.e., until the agent finishes this turn.
    queue = dispatcher._queues.get(channel_id)
    if queue is not None:
        await queue.join()
    t_agent = _time.time() - t_phase

    n_atoms = stats.get("ingested", 0) if isinstance(stats, dict) else 0
    print(
        f"  [{qid}] ingest={t_ingest:.1f}s ({n_atoms} atoms) "
        f"consolidate={t_consolidate:.1f}s (n={n_clusters}) "
        f"agent={t_agent:.1f}s "
        f"total={t_ingest + t_consolidate + t_agent:.1f}s",
        file=sys.stderr, flush=True,
    )

    # Primary hypothesis source: turns.jsonl output for the just-run turn.
    hypothesis = _extract_hypothesis_from_turns(
        turns_log, channel_id_for(qid), turns_pos_before,
    )
    if hypothesis is None:
        # Fallback: scrape send_message-routed output from BenchBridge.
        bench_stream.seek(stream_pos_before)
        new_output = bench_stream.read()
        hypothesis = _extract_hypothesis(new_output, qid)
    if hypothesis is None:
        return None
    return {"question_id": qid, "hypothesis": hypothesis}


def _extract_hypothesis_from_turns(
    turns_log: Path, channel_id: str, byte_offset: int,
) -> str | None:
    """Read turn records appended after ``byte_offset``, return the
    ``output`` of the most recent turn whose channel_id matches.

    Scoped to *new* records (post-offset) so re-running the same channel
    later doesn't pick up a stale prior turn.
    """
    if not turns_log.exists():
        return None
    with turns_log.open("rb") as f:
        f.seek(byte_offset)
        tail = f.read()
    if not tail:
        return None
    out: str | None = None
    for line in tail.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("channel_id") != channel_id:
            continue
        # Take the latest matching record's output.
        out_text = rec.get("output")
        if out_text:
            out = str(out_text).strip()
    return out


def _extract_hypothesis(stream_text: str, question_id: str) -> str | None:
    """BenchBridge writes lines like::

        [mimir:bench send_message channel=bench-<qid> msg_id=<m>] <text>

    We capture *every* outbound line for this channel and concatenate
    them — agents sometimes send multi-line answers as separate messages.
    """
    needle = f"channel={channel_id_for(question_id)} "
    pieces: list[str] = []
    for line in stream_text.splitlines():
        if needle not in line:
            continue
        if "send_message_attachments" in line:
            continue
        marker = "] "
        idx = line.find(marker)
        if idx < 0:
            continue
        pieces.append(line[idx + len(marker):])
    if not pieces:
        return None
    return "\n".join(pieces).strip()


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    dataset_path = _resolve_dataset(args)
    if not dataset_path.exists():
        print(f"dataset not found: {dataset_path}", file=sys.stderr)
        return 2
    dataset = json.loads(dataset_path.read_text())
    if args.limit:
        dataset = dataset[: args.limit]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    hypotheses_path = output_dir / f"hypotheses_{args.run_tag}.jsonl"

    # Mimir home for this run.
    if args.mimir_home:
        home = Path(args.mimir_home)
    else:
        home = output_dir / f"mimir_home_{args.run_tag}"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["MIMIR_HOME"] = str(home)
    os.environ.setdefault("SAGA_DATA_DIR", str(home / ".mimir"))
    (home / ".mimir").mkdir(parents=True, exist_ok=True)
    # Disable saga_session_end firing during the bench. Default idle
    # is 10 minutes; SessionManager flushes ended sessions on a timer
    # and any channel that's been quiet long enough triggers a
    # synthesis turn (extra LLM call, writes a session_boundary atom).
    # Harmless for hypothesis correctness — by the time it fires the
    # user_message turn has already produced its answer — but pure
    # wasted work for the bench. Saga's own bench TOMLs disabled the
    # equivalent (`enable_session_boundaries = false`); we do the
    # mimir-side equivalent here. Operator can override.
    os.environ.setdefault("MIMIR_SAGA_SESSION_IDLE_MINUTES", "9999")

    # Build the bench app.
    from mimir.cli import setup_home as _setup_home
    _setup_home(home)
    # Saga config selection:
    # - If --saga-config given: point SAGA_CONFIG at it (overrides
    #   the per-home saga.toml mimir/server.py would otherwise pick up).
    # - Else: overwrite <home>/saga.toml with bench-friendly settings
    #   (token_budget_ceiling 100M etc.) so haystack ingest doesn't
    #   refuse atoms.
    if args.saga_config:
        custom_path = Path(args.saga_config).resolve()
        if not custom_path.exists():
            print(f"--saga-config not found: {custom_path}", file=sys.stderr)
            return 2
        os.environ["SAGA_CONFIG"] = str(custom_path)
    else:
        _write_bench_saga_toml(home)

    # Install bench-only monkey-patches AFTER saga and mimir are
    # importable but BEFORE the app builds. _install_saga_bench_overrides
    # patches saga.core.hybrid_retrieve (per-question reference_date)
    # and _InProcessSaga.query (top_k bump to 20 to match saga's bench).
    _install_saga_bench_overrides()
    from mimir.config import Config
    cfg = Config.from_env()
    cfg = replace(cfg, home=home)

    from mimir import server as mimir_server
    bench_stream = io.StringIO()

    app = mimir_server.build_app(cfg)
    dispatcher = app["dispatcher"]
    # Re-target BenchBridge at our StringIO so we can scrape the agent's
    # outbound. The default BenchBridge writes to sys.stdout (which the
    # external runner harness scrapes); for in-process bench we capture
    # to memory.
    channels = app["channels"]
    bench_bridge = next(
        (b for b in channels.bridges() if getattr(b, "name", None) == "bench"),
        None,
    )
    assert bench_bridge is not None, "BenchBridge missing — server.build_app changed?"
    bench_bridge.stream = bench_stream

    written: list[dict] = []
    failed: list[str] = []
    turns_log = home / "logs" / "turns.jsonl"

    # Open ONE TestClient for the entire run. Each enter/exit fires
    # app.on_startup/on_cleanup, which starts and shuts down the
    # dispatcher worker; per-question scoping silently lost everything
    # except the last question's turn.
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        for q in dataset:
            try:
                rec = await _run_one_question(
                    question=q,
                    dispatcher=dispatcher,
                    bench_bridge=bench_bridge,
                    bench_stream=bench_stream,
                    client=client,
                    turns_log=turns_log,
                )
            except Exception as exc:  # noqa: BLE001 — keep going on per-question crashes
                print(f"  question {q['question_id']} crashed: {exc}", file=sys.stderr)
                failed.append(q["question_id"])
                continue
            if rec is None:
                failed.append(q["question_id"])
                continue
            written.append(rec)

    n = write_hypotheses_jsonl(hypotheses_path, written)
    print(f"wrote {n} hypotheses to {hypotheses_path}")
    if failed:
        print(f"  {len(failed)} questions failed (no hypothesis captured)")
    print(f"to score, run:\n  {evaluate_command(hypotheses_path, dataset_path)}")
    return 0 if not failed else 1


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
