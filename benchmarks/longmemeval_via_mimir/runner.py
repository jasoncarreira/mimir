"""LongMemEval through mimir's BenchBridge dispatch path (v0.5 §3).

Boots mimir in-process with `_InProcessSaga` and a `BenchBridge` outbound,
ingests each LongMemEval question's haystack into a per-question saga
SQLite database, then drives the question through ``POST /event`` so
mimir's pre-message hook (saga query + contextual rewrite +
session_boundaries surfacing) and post-message hook (mark_contributions)
both fire. The agent's reply is captured from the BenchBridge stream
and written to a hypotheses JSONL the existing saga evaluator scores.

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
# Match msam_bench.toml: effectively unlimited.
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
# v0.5 §2 mimir-prod overrides — same as the default saga.toml.
enable_contextual_rewrite = true
two_tier_enabled = true
enable_missing_ref_pivot = true
enable_confidence_gating = true
default_min_confidence_tier = "low"

[retrieval_v2]
enable_query_expansion = true

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
    saga.toml uses msam_bench.toml's effectively-unlimited cap. LLM
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


async def _run_one_question(
    *,
    question: dict[str, Any],
    dispatcher: Any,
    bench_bridge: Any,
    bench_stream: io.StringIO,
    aiohttp_app: Any,
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
    """
    from aiohttp.test_utils import TestClient, TestServer
    from saga.benchmarks.longmemeval.ingest import ingest_question

    qid = question["question_id"]

    # Per-question saga DB (replicates saga's bench isolation).
    work_dir = Path(os.environ.get("SAGA_DATA_DIR", "."))
    db_path = work_dir / f"q_{qid}.db"
    _switch_saga_db(db_path)
    ingest_question(question)

    # Snapshot turn-log size + bench stream position so we can read just
    # the new content after this question's turn finishes.
    turns_pos_before = turns_log.stat().st_size if turns_log.exists() else 0
    stream_pos_before = bench_stream.tell()

    async with TestClient(TestServer(aiohttp_app)) as client:
        body = question_to_event(question)
        resp = await client.post("/event", json=body)
        if resp.status != 200:
            return None
        await dispatcher.drain()

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

    # Build the bench app.
    from mimir.cli import setup_home as _setup_home
    _setup_home(home)
    # Overwrite saga.toml with bench-friendly settings — default mimir
    # caps storage at 1M tokens (fine for daily, fatal for LongMemEval).
    _write_bench_saga_toml(home)
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
    for q in dataset:
        try:
            rec = await _run_one_question(
                question=q,
                dispatcher=dispatcher,
                bench_bridge=bench_bridge,
                bench_stream=bench_stream,
                aiohttp_app=app,
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
