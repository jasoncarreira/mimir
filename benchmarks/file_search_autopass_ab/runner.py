"""A/B harness driver for chainlink #140 (Sub B of #138).

Spawns an in-process mimir twice — once with
``MIMIR_FILE_SEARCH_AUTOPASS_ENABLED=1`` and once with it disabled —
runs the full probe set through ``POST /event`` on each, and writes
per-arm result JSONL to ``results/file_search_autopass_ab/<tag>/``.
Then computes the metric/per-probe markdown report.

Mirrors ``benchmarks/longmemeval_via_mimir/runner.py`` for in-process
boot + BenchBridge dispatch. The probe set is small and there's no
haystack to ingest, so the per-probe loop is much simpler than
longmemeval's.

Usage::

    uv run python -m benchmarks.file_search_autopass_ab.runner \\
        --run-tag smoke \\
        --probes benchmarks/file_search_autopass_ab/probes.yaml \\
        --output-dir results/file_search_autopass_ab/ \\
        --limit 3

Drop ``--limit`` for the full 30-probe end-to-end run.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .route import channel_id_for, probe_to_event
from .score import (
    ProbeResult,
    extract_metrics_from_turn,
    load_results,
    render_markdown,
    summarise,
    write_results,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="benchmarks.file_search_autopass_ab.runner",
        description=(
            "Run the file_search autopass A/B harness for chainlink #140. "
            "Drives mimir against the probe set twice with the autopass "
            "env var flipped on then off, captures per-turn metrics from "
            "turns.jsonl, and writes a markdown comparison report."
        ),
    )
    p.add_argument("--run-tag", required=True,
                   help="identifier for output filenames (e.g. smoke, full30)")
    p.add_argument("--probes", type=Path,
                   default=Path(__file__).parent / "probes.yaml",
                   help="path to probes.yaml (default: alongside this module)")
    p.add_argument("--output-dir", type=Path,
                   default=Path("results/file_search_autopass_ab/"),
                   help="root output dir; per-run subdir is <output-dir>/<run-tag>")
    p.add_argument("--limit", type=int, default=None,
                   help="only run the first N probes (smoke testing)")
    p.add_argument("--mimir-home", type=Path, default=None,
                   help="MIMIR_HOME for the bench (defaults to a per-run "
                        "tmpdir under output-dir)")
    p.add_argument(
        "--report", type=Path, default=None,
        help="if set, write the markdown report to this path; otherwise "
             "the report lands at <output-dir>/<run-tag>/report.md",
    )
    p.add_argument(
        "--arm", choices=["on", "off", "both"], default="both",
        help="which arm to run (default: both). Useful for resumption.",
    )
    return p.parse_args(argv)


def load_probes(path: Path) -> list[dict[str, Any]]:
    """Load probes.yaml. Returns a list of probe dicts with a 1-indexed
    ``_index`` injected for stable per-probe identity across arms.

    Yaml lib: PyYAML is in mimir's dep tree (used by saga config). Falls
    back to a small inline parser if it's somehow missing, but in
    practice it's there.
    """
    import yaml  # type: ignore[import-untyped]
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    raw = data.get("probes") or []
    out: list[dict[str, Any]] = []
    for i, p in enumerate(raw, start=1):
        if not isinstance(p, dict):
            raise ValueError(f"probes[{i}] is not a mapping: {p!r}")
        for key in ("text", "expected_target", "shape"):
            if key not in p:
                raise ValueError(f"probes[{i}] missing required key {key!r}: {p!r}")
        if p["shape"] not in {
            "fingerprinted-error", "concept-lookup",
            "recent-decision", "procedural",
        }:
            raise ValueError(
                f"probes[{i}] invalid shape {p['shape']!r}; "
                "must be one of fingerprinted-error/concept-lookup/"
                "recent-decision/procedural"
            )
        p_copy = dict(p)
        p_copy["_index"] = i
        out.append(p_copy)
    return out


# ----------------------------------------------------------------------
# Arm setup
# ----------------------------------------------------------------------


def _suppress_live_bridges() -> None:
    """Strip live-chat tokens from the environment so the bench app
    can't accidentally connect to real Discord/Slack.

    Mirrors the equivalent block in ``benchmarks/longmemeval_via_mimir/runner.py``
    (see ``memory/issues/bench-runner-live-bridge-leak.md``). Even if
    the operator's ``.env`` exports these, bench mode should never
    register the live bridges. Set BEFORE ``Config.from_env()`` runs.

    Also clears ``MIMIR_API_KEY`` — the bench POSTs to ``/event``
    without an ``X-API-Key`` header (it's in-process; auth is a
    network-side concern). When the operator's env exports an API
    key, the bench-side auth middleware would 401 every probe.
    Empty key disables the middleware (dev-mode pass-through).
    """
    for var in (
        "DISCORD_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
        "MIMIR_API_KEY",
    ):
        os.environ[var] = ""


def _configure_arm_env(arm: str) -> None:
    """Flip ``MIMIR_FILE_SEARCH_AUTOPASS_ENABLED`` for this arm.

    ``arm == "on"`` → ``"1"``. ``arm == "off"`` → ``"0"``. We set both
    branches explicitly (rather than unsetting for off) so an operator's
    inherited env can't poison the off-arm.
    """
    if arm == "on":
        os.environ["MIMIR_FILE_SEARCH_AUTOPASS_ENABLED"] = "1"
    else:
        os.environ["MIMIR_FILE_SEARCH_AUTOPASS_ENABLED"] = "0"


# ----------------------------------------------------------------------
# Per-probe dispatch
# ----------------------------------------------------------------------


def _tail_turn_for_channel(
    turns_log: Path,
    channel_id: str,
    byte_offset: int,
) -> dict[str, Any] | None:
    """Read the most-recent ``TurnRecord`` for ``channel_id`` appended
    after ``byte_offset``. Returns the parsed dict or None when no
    matching record is present yet."""
    if not turns_log.exists():
        return None
    try:
        with turns_log.open("rb") as f:
            f.seek(byte_offset)
            tail = f.read()
    except OSError:
        return None
    if not tail:
        return None
    chosen: dict[str, Any] | None = None
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
        chosen = rec
    return chosen


async def _run_one_probe(
    *,
    probe: dict[str, Any],
    arm: str,
    dispatcher: Any,
    client: Any,
    turns_log: Path,
) -> ProbeResult:
    """Drive one probe through ``/event``, wait for the turn to finish,
    extract the seven metrics from the resulting ``TurnRecord``."""
    idx = probe["_index"]
    channel_id = channel_id_for(arm, idx)
    body = probe_to_event(probe, channel_id)

    turns_pos_before = turns_log.stat().st_size if turns_log.exists() else 0

    started = time.monotonic()
    try:
        resp = await client.post("/event", json=body)
    except Exception as exc:  # noqa: BLE001 — keep going across probes
        return ProbeResult(
            probe_index=idx,
            probe_text=probe["text"],
            expected_target=probe["expected_target"],
            shape=probe["shape"],
            arm=arm,
            captured=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    if resp.status != 200:
        text = await resp.text()
        return ProbeResult(
            probe_index=idx,
            probe_text=probe["text"],
            expected_target=probe["expected_target"],
            shape=probe["shape"],
            arm=arm,
            captured=False,
            error=f"/event POST returned {resp.status}: {text[:300]}",
        )

    # Wait for this channel's queue to drain — same pattern as the
    # longmemeval runner. dispatcher.drain() would close workers; we
    # only want the channel's pending events to complete.
    queue = dispatcher._queues.get(channel_id)
    if queue is not None:
        await queue.join()
    duration_s = time.monotonic() - started

    rec = _tail_turn_for_channel(turns_log, channel_id, turns_pos_before)
    if rec is None:
        return ProbeResult(
            probe_index=idx,
            probe_text=probe["text"],
            expected_target=probe["expected_target"],
            shape=probe["shape"],
            arm=arm,
            captured=False,
            duration_ms=int(duration_s * 1000),
            error="no turn record appended to turns.jsonl for this channel",
        )

    metrics = extract_metrics_from_turn(
        rec, expected_target=probe["expected_target"],
    )
    return ProbeResult(
        probe_index=idx,
        probe_text=probe["text"],
        expected_target=probe["expected_target"],
        shape=probe["shape"],
        arm=arm,
        file_search_count=metrics["file_search_count"],
        grep_glob_count=metrics["grep_glob_count"],
        read_count=metrics["read_count"],
        total_tool_calls=metrics["total_tool_calls"],
        duration_ms=metrics["duration_ms"] or int(duration_s * 1000),
        total_cost_usd=metrics["total_cost_usd"],
        hit=metrics["hit"],
        reply=metrics["reply"],
        captured=True,
    )


# ----------------------------------------------------------------------
# Arm runner
# ----------------------------------------------------------------------


async def _run_arm(
    *,
    arm: str,
    probes: list[dict[str, Any]],
    home: Path,
    output_path: Path,
) -> list[ProbeResult]:
    """Boot mimir in-process with the arm's env, run every probe, return
    per-probe results.

    Each arm gets a fresh ``home/.mimir/saga.db`` (we delete it on
    arm-entry) so the autopass-on prompt cache doesn't leak into the
    autopass-off run.
    """
    # Configure arm-specific env BEFORE Config.from_env() reads it.
    _configure_arm_env(arm)
    _suppress_live_bridges()

    os.environ["MIMIR_HOME"] = str(home)
    os.environ.setdefault("SAGA_DATA_DIR", str(home / ".mimir"))
    (home / ".mimir").mkdir(parents=True, exist_ok=True)
    # Quiesce the session-end synthesis turn for the bench — same
    # rationale as the longmemeval runner: harmless if it fires, but
    # pure wasted work for an A/B run.
    os.environ.setdefault("MIMIR_SAGA_SESSION_IDLE_MINUTES", "9999")

    from mimir.cli import setup_home as _setup_home
    _setup_home(home)
    from mimir.config import Config
    cfg = Config.from_env()
    cfg = replace(cfg, home=home)

    from mimir import server as mimir_server
    bench_stream = io.StringIO()
    app = mimir_server.build_app(cfg)
    dispatcher = app["dispatcher"]
    channels = app["channels"]
    bench_bridge = next(
        (b for b in channels.bridges() if getattr(b, "name", None) == "bench"),
        None,
    )
    assert bench_bridge is not None, "BenchBridge missing — server.build_app changed?"
    bench_bridge.stream = bench_stream

    turns_log = home / "logs" / "turns.jsonl"
    results: list[ProbeResult] = []

    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        for probe in probes:
            print(
                f"  [arm={arm}] probe {probe['_index']}/{len(probes)}: "
                f"{probe['text'][:80]}...",
                file=sys.stderr, flush=True,
            )
            try:
                rec = await _run_one_probe(
                    probe=probe,
                    arm=arm,
                    dispatcher=dispatcher,
                    client=client,
                    turns_log=turns_log,
                )
            except Exception as exc:  # noqa: BLE001
                rec = ProbeResult(
                    probe_index=probe["_index"],
                    probe_text=probe["text"],
                    expected_target=probe["expected_target"],
                    shape=probe["shape"],
                    arm=arm,
                    captured=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(rec)

    write_results(output_path, results)
    print(
        f"  arm={arm}: wrote {len(results)} probe results to {output_path}",
        file=sys.stderr,
    )
    return results


# ----------------------------------------------------------------------
# Top-level orchestration
# ----------------------------------------------------------------------


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    probes = load_probes(args.probes)
    if args.limit:
        probes = probes[: args.limit]
    if not probes:
        print("no probes loaded — check --probes path", file=sys.stderr)
        return 2

    run_dir = args.output_dir / args.run_tag
    run_dir.mkdir(parents=True, exist_ok=True)
    on_path = run_dir / "arm_on.jsonl"
    off_path = run_dir / "arm_off.jsonl"

    home = args.mimir_home or (run_dir / "mimir_home")
    home.mkdir(parents=True, exist_ok=True)

    on_results: list[ProbeResult] = []
    off_results: list[ProbeResult] = []

    # Run the arms sequentially. Two separate boots is the simplest way
    # to guarantee zero prompt-cache cross-contamination between arms;
    # in-process boot is cheap (sub-second) so the cost is negligible.
    if args.arm in ("on", "both"):
        on_results = await _run_arm(
            arm="on", probes=probes, home=home, output_path=on_path,
        )
    elif on_path.exists():
        on_results = load_results(on_path)

    if args.arm in ("off", "both"):
        off_results = await _run_arm(
            arm="off", probes=probes, home=home, output_path=off_path,
        )
    elif off_path.exists():
        off_results = load_results(off_path)

    if not on_results or not off_results:
        print(
            "Only one arm produced results; skipping comparison rendering. "
            f"Re-run with --arm both to fill in. (on={len(on_results)}, "
            f"off={len(off_results)})",
            file=sys.stderr,
        )
        return 0

    summary = summarise(on_results, off_results)
    report_path = args.report or (run_dir / "report.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_markdown(summary, run_tag=args.run_tag), encoding="utf-8",
    )
    print(f"report written to {report_path}", file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
