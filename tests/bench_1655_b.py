"""DEFECT B: uv run --locked python tests/bench_1655_b.py.

Only fixture data in an in-memory SQLite DB is written. The baseline factory is
AST-extracted from git; both factories use current, identical dependencies and
VectorIndex.search. Timings exclude setup, embedding, and instrumentation.
Python allocation is a separate tracemalloc peak, not RSS or native FAISS memory.
The precomputed lane returns an existing ranked list without copying it, isolating
SQL authorization and Python filtering from search/result materialization.
"""
from __future__ import annotations

import argparse
import ast
import gc
import hashlib
import inspect
import json
import platform
import sqlite3
import statistics
import subprocess
import time
import tracemalloc
from pathlib import Path

import faiss
import numpy as np

import mimir.saga.client as client
from mimir.models import AuthContext
from mimir.saga.ownership import SagaReadAuthorization
from mimir.saga.vector_index import VectorIndex


BASELINE = "0a6384fdb90ec4ef4e4d3b17fea30df119754ea2"
ROOT = Path(__file__).resolve().parents[1]


def load_baseline(revision: str):
    source = subprocess.check_output(
        ["git", "-c", f"safe.directory={ROOT}", "show",
         f"{revision}:mimir/saga/client.py"], cwd=ROOT, text=True,
    )
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == "_make_faiss_search_fn")
    namespace = dict(vars(client))
    exec(compile(ast.Module(body=[node], type_ignores=[]),
                 f"{revision}:mimir/saga/client.py", "exec"), namespace)
    return namespace[node.name]


class RankedIndex:
    """Search cost deliberately excluded; the precomputed list is borrowed."""

    def __init__(self, candidates: list[tuple[str, float]]):
        self.candidates = candidates
        self.total_vectors = len(candidates)

    def search(self, query, *, top_k):
        assert top_k == self.total_vectors
        return self.candidates


def benchmark(size: int, dimension: int, repeats: int, allocation_runs: int,
              baseline_revision: str = BASELINE) -> list[dict]:
    if size < 12 or dimension < 2 or repeats < 1 or allocation_runs < 1:
        raise ValueError("Need size >= 12, dimension >= 2 and positive run counts")
    faiss.omp_set_num_threads(1)
    factories = {"before": load_baseline(baseline_revision),
                 "after": client._make_faiss_search_fn}
    context = AuthContext(
        principal="alice", canonical_principal="user:alice", roles=("user",),
        event_ingress="test", trigger="test", channel_id=None,
        interactivity=None, enforcement_enabled=True,
    )
    conn = sqlite3.connect(":memory:")
    records = []
    try:
        conn.executescript((ROOT / "mimir/saga/schema.sql").read_text())
        index = VectorIndex(dimension=dimension)
        index.build_from_db(conn)
        # Empty build + public incremental add keeps FlatIP even above 50k.
        # Unit vectors with strictly decreasing first coordinate give a known
        # ranking, including at the authorized/hidden boundary, without ties.
        ids = [f"atom-{i:09d}" for i in range(size)]
        matrix = np.zeros((size, dimension), dtype=np.float32)
        matrix[:, 0] = np.linspace(0.9, 0.1, size, dtype=np.float32)
        matrix[:, 1] = np.sqrt(1.0 - matrix[:, 0] ** 2)
        for atom_id, vector in zip(ids, matrix):
            index.add(atom_id, vector.tobytes())
        del matrix
        assert isinstance(index._index, faiss.IndexFlatIP)
        assert index.total_vectors == size
        conn.executemany(
            "INSERT INTO atoms (id, content, content_hash, created_at, "
            "owner_principal, visibility) VALUES (?, 'benchmark', ?, "
            "'2026-01-01', 'user:alice', 'private')",
            ((atom_id, atom_id) for atom_id in ids),
        )
        query = [1.0] + [0.0] * (dimension - 1)
        ranked = index.search(query, top_k=size)
        assert [item[0] for item in ranked] == ids
        assert all(a[1] > b[1] for a, b in zip(ranked, ranked[1:]))
        for scenario in ("all_authorized", "buried_12"):
            if scenario == "buried_12":
                conn.execute("UPDATE atoms SET owner_principal='user:bob' WHERE id < ?",
                             (ids[-12],))
            # Full production schema/indexes, with optimizer statistics. Without
            # statistics a different plan can dominate these batch measurements.
            conn.execute("ANALYZE")
            conn.commit()
            expected = ranked[:12] if scenario == "all_authorized" else ranked[-12:]
            for lane, search_index in (("real_faiss", index),
                                       ("precomputed", RankedIndex(ranked))):
                functions = {
                    name: factory(search_index, conn, read_authorization=
                                  SagaReadAuthorization(context, "query"))
                    for name, factory in factories.items()
                }
                samples = {name: [] for name in functions}
                peaks = {name: [] for name in functions}
                for fn in functions.values():
                    assert fn(query, 12) == expected
                # Alternate order to reduce systematic cache/order bias. GC is
                # enabled; collect outside the timed region before every call.
                for repeat in range(repeats):
                    order = list(functions) if repeat % 2 == 0 else list(reversed(functions))
                    for name in order:
                        gc.collect()
                        start = time.perf_counter()
                        result = functions[name](query, 12)
                        samples[name].append((time.perf_counter() - start) * 1000)
                        assert result == expected
                for repeat in range(allocation_runs):
                    for name in functions:
                        gc.collect()
                        tracemalloc.start()
                        try:
                            result = functions[name](query, 12)
                            peaks[name].append(tracemalloc.get_traced_memory()[1])
                        finally:
                            tracemalloc.stop()
                        assert result == expected
                for name, fn in functions.items():
                    sql_count = 0
                    first_sql = None

                    def trace(sql):
                        nonlocal sql_count, first_sql
                        if sql.startswith("SELECT a.id FROM atoms"):
                            sql_count += 1
                            if first_sql is None:
                                first_sql = sql

                    conn.set_trace_callback(trace)
                    try:
                        assert fn(query, 12) == expected
                    finally:
                        conn.set_trace_callback(None)
                    plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + first_sql)]
                    record = dict(
                        scenario=scenario, lane=lane, implementation=name,
                        atoms=size, dimension=dimension, top_k=12,
                        authorized=size if scenario == "all_authorized" else 12,
                        elapsed_ms=samples[name],
                        median_ms=statistics.median(samples[name]),
                        timed_total_ms=sum(samples[name]),
                        python_peak_bytes=peaks[name],
                        median_python_peak_mib=statistics.median(peaks[name]) / 2**20,
                        sql_statements=sql_count, sql_plan=plan,
                        result_ids=[item[0] for item in expected],
                    )
                    records.append(record)
                    print(json.dumps(record), flush=True)
    finally:
        conn.close()
    return records


def test_benchmark_smoke() -> None:
    """Validate both measurement lanes and exact authorized top-12 on a small DB."""
    records = benchmark(1024, 32, 1, 1)
    assert len(records) == 8
    for row in records:
        expected_queries = 4 if (row["implementation"] == "after"
                                 and row["scenario"] == "buried_12") else 1
        assert row["sql_statements"] == expected_queries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--atoms", type=int, default=200_000)
    parser.add_argument("--dimension", type=int, default=32)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--allocation-runs", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(dict(
        python=platform.python_version(), platform=platform.platform(),
        numpy=np.__version__, faiss=faiss.__version__, sqlite=sqlite3.sqlite_version,
        faiss_threads=1, baseline=args.baseline, sqlite_storage="memory",
        schema="production schema.sql; ANALYZE after scenario setup",
        current_factory_sha256=hashlib.sha256(
            inspect.getsource(client._make_faiss_search_fn).encode()).hexdigest(),
        repeats=args.repeats, allocation_runs=args.allocation_runs,
    )), flush=True)
    start = time.perf_counter()
    benchmark(args.atoms, args.dimension, args.repeats, args.allocation_runs, args.baseline)
    print(json.dumps({"benchmark_wall_seconds_including_setup": time.perf_counter() - start}))


if __name__ == "__main__":
    main()
