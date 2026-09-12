"""Standalone, stdlib-only issue #1668 benchmark; no agent initialization.

Run: uv run --no-project python benchmarks/issue1668_io.py --runs 7
Fixtures/results default to /tmp/opencode; override with --fixture-dir.
Baseline and working-tree
methods are AST-compiled unchanged. A minimal wiki module supplies the exact
source-declared executor for the snapshot wrapper's relative import.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import concurrent.futures
import fcntl
import hashlib
import inspect
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import types
from contextlib import contextmanager
from contextvars import copy_context
from functools import partial
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "a91b825772f92d4d3df7d688a82d77b4694278d6"
WORK = []


class TimedExecutor(concurrent.futures.ThreadPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):
        def timed():
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                WORK.append((threading.current_thread().name,
                             (time.perf_counter() - start) * 1000))
        return super().submit(timed)


def source(path, baseline=False):
    if baseline:
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={ROOT}", "show", f"{BASELINE}:{path}"],
            cwd=ROOT, text=True,
        )
    return (ROOT / path).read_text()


def extract(text, class_name, names, namespace):
    tree = ast.parse(text)
    body = tree.body if class_name is None else next(
        node.body for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    selected = []
    for node in body:
        name = getattr(node, "name", None)
        if isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", None)
        if name in names:
            selected.append(node)
    assert len(selected) == len(names), (names, selected)
    module = ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0,
    ), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<extracted-source>", "exec"), namespace)
    return {name: namespace[name] for name in names}


async def measure(call):
    ticks = []

    async def heartbeat():
        while True:
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.001)

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.01)
    ticks.clear()
    ticks.append(time.perf_counter())
    WORK.clear()
    start = time.perf_counter()
    outcome = "ok"
    try:
        result = call()
        if inspect.isawaitable(result):
            await result
    except TimeoutError:
        outcome = "timeout"
    elapsed = (time.perf_counter() - start) * 1000
    workers = list(WORK)
    # Let a heartbeat overdue because of synchronous blocking actually fire.
    await asyncio.sleep(0.005)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return {"wall_ms": elapsed,
            "worker_ms": sum(ms for _, ms in workers),
            "threads": sorted({name for name, _ in workers}),
            "worst_gap_ms": max(b - a for a, b in zip(ticks, ticks[1:])) * 1000,
            "outcome": outcome}


async def main(args):
    namespace = dict(globals(), __package__="mimir",
                     ThreadPoolExecutor=TimedExecutor,
                     concurrent=types.SimpleNamespace(futures=types.SimpleNamespace(
                         ThreadPoolExecutor=TimedExecutor)))
    wiki = types.ModuleType("mimir.wiki_backlinks")
    wiki.__dict__.update(extract(source("mimir/wiki_backlinks.py"), None,
                                {"_BACKLINKS_EXECUTOR"}, namespace))
    sys.modules["mimir.wiki_backlinks"] = wiki
    agents = []
    stores = []
    for baseline in (True, False):
        ns = namespace.copy()
        methods = {"_snapshot_wiki_mtimes", "_skill_catalog_fingerprint",
                   "_WIKI_GENERATED_OUTPUTS"}
        if not baseline:
            methods.add("_snapshot_wiki_mtimes_async")
        agents.append(type("BenchAgent", (), extract(
            source("mimir/agent.py", baseline), "Agent", methods, ns))())
        ns = namespace.copy()
        text = source("mimir/commitments/store.py", baseline)
        constants = {"_WRITE_LOCK_TIMEOUT_SECS", "COMMITMENTS_JSONL_SCHEMA_VERSION"}
        if not baseline:
            constants |= {"_STORE_EXECUTOR", "run_store_io"}
        extract(text, None, constants, ns)
        stores.append(type("BenchStore", (), extract(text, "CommitmentsStore", {
            "_writer_lock", "_append_line_sync", "_append",
        }, ns))())

    args.fixture_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="issue1668-", dir=args.fixture_dir) as tmp:
        home = Path(tmp)
        paragraph = (
            "## Operational notes\n\nValidate the deployment configuration before "
            "changing service ownership. Record the observed latency, rollback "
            "criteria, and links to [[incident-response]] and [[release-checklist]]. "
            "Use a read-only inspection first; retain command output and verify "
            "the expected state after the change.\n\n"
            "```sh\nservice-control inspect --format json\n```\n\n"
        )
        sizes = {}
        for kind, count, low, span in (("wiki", args.pages, 4, 29),
                                       ("skills", args.skills, 4, 21)):
            total = 0
            for i in range(count):
                path = (home / "state/wiki" / f"topic-{i % 50:02}" / f"page-{i:05}.md"
                        if kind == "wiki" else
                        home / "skills" / f"group-{i % 20:02}" / f"skill-{i:05}" / "SKILL.md")
                path.parent.mkdir(parents=True, exist_ok=True)
                length = (low + i % span) * 1024
                header = f"---\nname: {kind}-{i}\ndescription: Operational guide {i}\n---\n\n"
                data = (header + paragraph * (length // len(paragraph) + 1))[:length]
                path.write_text(data)
                total += length
            sizes[kind] = {"files": count, "bytes": total,
                           "min_KiB": low, "max_KiB": low + span - 1}
        for agent in agents:
            agent._config = types.SimpleNamespace(home=home)
            agent._skill_fingerprint_executor = None
            agent._skill_fingerprint_cache = None
        skill_sources = [str(home / "skills")]
        old, new = agents
        assert old._snapshot_wiki_mtimes() == await new._snapshot_wiki_mtimes_async()
        assert len(old._snapshot_wiki_mtimes()) == args.pages
        expected = old._skill_catalog_fingerprint(skill_sources)
        assert expected == await new._skill_catalog_fingerprint(skill_sources)
        reads = []
        read_bytes = Path.read_bytes

        def counted(path):
            reads.append(str(path))
            return read_bytes(path)

        with patch.object(Path, "read_bytes", counted):
            for _ in range(args.runs):
                assert await new._skill_catalog_fingerprint(skill_sources) == expected
        assert not reads, reads

        def event(i):
            return {"type": "commitment_added", "id": f"c-{i:010x}",
                    "ts_unix": 1789000000, "v": 1, "record": {
                        "id": f"c-{i:010x}", "channel_id": "bench-ops",
                        "owner_principal": "user:benchmark", "status": "pending",
                        "text": "Review release readiness and publish the rollback checklist.",
                        "suggested_reminder": "Please verify the release checklist with the on-call engineer.",
                        "due_window_start_unix": 1789100000,
                        "due_window_end_unix": 1789200000}}

        for i, store in enumerate(stores):
            store.path = home / f"commitments-{i}.jsonl"
            store._lock = asyncio.Lock()
            with store.path.open("w") as f:
                for j in range(200):
                    f.write(json.dumps(event(j)) + "\n")
                f.flush()
                os.fsync(f.fileno())
        sizes["store"] = {"initial_events": 200, "initial_bytes": stores[0].path.stat().st_size,
                          "append_bytes": len(json.dumps(event(1000))) + 1}

        async def cold_hash():
            new._skill_fingerprint_cache = None
            assert await new._skill_catalog_fingerprint(skill_sources) == expected

        cases = {
            "A_before_sync": old._snapshot_wiki_mtimes,
            "A_after_awaited": new._snapshot_wiki_mtimes_async,
            "B_before_sync": lambda: old._skill_catalog_fingerprint(skill_sources),
            "B_after_cache_miss": cold_hash,
            "B_after_warm": lambda: new._skill_catalog_fingerprint(skill_sources),
            "D_before_sync": lambda: stores[0]._append(event(1000)),
            "D_after_awaited": lambda: stores[1]._append(event(1000)),
        }
        results = {name: [] for name in cases}
        for _ in range(args.runs):
            for name, call in cases.items():
                results[name].append(await measure(call))
        for i, label in enumerate(("before_sync", "after_awaited")):
            name = f"D_contention_{label}"
            results[name] = []
            store = stores[i]
            for _ in range(args.runs):
                ready = threading.Event()

                def hold():
                    with store.path.with_suffix(".jsonl.lock").open("a") as lock:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                        ready.set()
                        time.sleep(0.250)
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

                holder = threading.Thread(target=hold)
                # Start contention after heartbeat's pre-roll, inside measurement.
                async def contended():
                    holder.start()
                    ready.wait()
                    await store._append(event(1001))

                results[name].append(await measure(contended))
                holder.join()
        report = {"python": sys.version, "platform": platform.platform(),
                  "head": subprocess.check_output([
                      "git", "-c", f"safe.directory={ROOT}", "rev-parse", "HEAD"
                  ], cwd=ROOT, text=True).strip(),
                  "source_sha256": {p: hashlib.sha256(source(p).encode()).hexdigest()
                                    for p in ("mimir/agent.py", "mimir/commitments/store.py")},
                  "filesystem": subprocess.check_output(["df", "-T", tmp], text=True),
                  "fixtures": sizes, "runs": args.runs, "warm_content_reads": len(reads),
                  "summary": {}, "raw": results}
        for name, rows in results.items():
            report["summary"][name] = {
                "median_wall_ms": statistics.median(r["wall_ms"] for r in rows),
                "median_worker_ms": statistics.median(r["worker_ms"] for r in rows),
                "max_gap_ms": max(r["worst_gap_ms"] for r in rows),
                "timeouts": sum(r["outcome"] == "timeout" for r in rows),
            }
        output = args.fixture_dir / "issue1668-results.json"
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "raw"}, indent=2))
        print(f"Raw timings: {output}")
    new._skill_fingerprint_executor.shutdown()
    wiki._BACKLINKS_EXECUTOR.shutdown()
    stores[1]._append.__func__.__globals__["_STORE_EXECUTOR"].shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--pages", type=int, default=5000)
    parser.add_argument("--skills", type=int, default=2000)
    parser.add_argument("--fixture-dir", type=Path, default=Path("/tmp/opencode"))
    args = parser.parse_args()
    if args.runs < 5:
        parser.error("at least five runs are required")
    asyncio.run(main(args))
