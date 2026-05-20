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


# Production-bridge env vars that ``mimir/server.py:build_app`` consults.
# Kept as a module-level tuple so the regression test can import the same
# canonical list rather than duplicating it (drift between this list and
# the test would silently weaken the test's coverage).
_BENCH_PRODUCTION_BRIDGE_ENV_VARS: tuple[str, ...] = (
    "DISCORD_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
)


def _suppress_production_bridges_in_env() -> None:
    """Clear DISCORD/SLACK tokens so ``build_app`` skips live bridge registration.

    The bench dispatches through ``BenchBridge`` ONLY — but
    ``mimir/server.py:build_app`` registers ``DiscordBridge`` / ``SlackBridge``
    if their tokens are set in the env. When mimir launches this bench as a
    subagent, the tokens are inherited from the parent shell, so the
    bench-mimir connects to the same Discord bot account and starts
    receiving + replying to live Discord messages — burning bench budget on
    chat, polluting the bench's ``chat_history.jsonl``, and shifting the
    prompt cache between LongMemEval questions.

    Set the tokens to empty BEFORE ``Config.from_env()`` so the cleared
    values propagate into the Config used by build_app.

    Unconditional set (not ``setdefault``) — even if the operator has them
    in their .env, bench mode should NEVER touch live chat. The regression
    guard for this contract lives at
    ``tests/test_bench_runner.py::test_suppress_production_bridges_in_env_blocks_live_bridge_registration``
    (chainlink #119).
    """
    for _var in _BENCH_PRODUCTION_BRIDGE_ENV_VARS:
        os.environ[_var] = ""


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


_BENCH_TOP_K = 20  # match the RETRIEVAL_TOP_K saga's longmemeval bench used


def _install_saga_bench_overrides() -> None:
    """Bench-only monkey-patch: ``mimir.saga_client._InProcessSaga.query``
    bumps any caller-supplied ``top_k`` to ``_BENCH_TOP_K`` (20). saga's
    bench used RETRIEVAL_TOP_K=20; mimir's pre-message hook hardcodes
    12. The agent gets more retrieved-atom context this way — same
    shape the Minimax reader had in saga's run_eval.

    Idempotent. Scoped to the bench process; production mimir is
    untouched.

    Historical: the prior `saga.core.hybrid_retrieve` reference-date
    patch was removed alongside the vendored saga deletion. mimir's
    SagaStore.query has its own reference_date threading; the old
    patch was a no-op against the actual mimir.saga call chain.
    """
    from mimir import saga_client as _sc
    if not getattr(_sc._InProcessSaga, "_bench_patched", False):
        _orig_query = _sc._InProcessSaga.query

        async def _patched_query(self, query, *, top_k=12, **kwargs):
            return await _orig_query(self, query, top_k=_BENCH_TOP_K, **kwargs)

        _sc._InProcessSaga.query = _patched_query
        _sc._InProcessSaga._bench_patched = True


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

    NOTE: This runner no longer performs per-question ingest +
    consolidation. Those steps used to go through the vendored
    ``saga.core`` / ``saga.benchmarks.longmemeval.ingest`` paths which
    set state on modules outside mimir.saga's call chain (effectively
    no-ops for the actual retrieval path) and were removed alongside
    the vendored saga deletion. For a working modern bench, use
    ``benchmarks.longmemeval_via_memory.runner`` (SagaStore-direct) or
    the in-progress ``runner_memory.py`` (BenchBridge backed by
    ``mimir.saga.SagaStore``).
    """
    qid = question["question_id"]
    import time as _time

    t_ingest = 0.0
    t_consolidate = 0.0
    n_clusters = 0
    stats: dict[str, Any] = {}

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
    # Suppress production bridges in bench mode. See
    # ``_suppress_production_bridges_in_env`` (defined at module scope) for
    # the full failure-mode history. Must run BEFORE ``Config.from_env()``
    # so the cleared tokens propagate into the Config used by build_app.
    _suppress_production_bridges_in_env()
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
    # patches _InProcessSaga.query (top_k bump to 20 to match saga's
    # bench). The historical saga.core.hybrid_retrieve patch was removed
    # alongside the vendored saga deletion.
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


_DEPRECATED_MSG = """\
benchmarks.longmemeval_via_mimir.runner is unsupported post saga-decoupling.

This runner used to drive per-question ingest + consolidation through the
vendored saga.core engine. After the vendored saga deletion, those steps
were no-ops against the actual mimir.saga retrieval path; the runner is
now retained only for the dispatch scaffolding that
``tests/test_bench_via_mimir.py`` exercises (route / score / hypothesis
extraction helpers).

For a working LongMemEval run:
  - SagaStore-direct: benchmarks.longmemeval_via_memory.runner
  - mimir BenchBridge (in-progress):
    benchmarks.longmemeval_via_mimir.runner_memory

Pass ``--allow-deprecated`` to bypass this guard (the dispatch loop
will still run but ingest no atoms; useful only for harness debugging).
"""


def main() -> None:
    if "--allow-deprecated" not in sys.argv:
        print(_DEPRECATED_MSG, file=sys.stderr)
        sys.exit(2)
    sys.argv = [a for a in sys.argv if a != "--allow-deprecated"]
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
