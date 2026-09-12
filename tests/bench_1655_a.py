"""Reproduce DEFECT A timings: uv run python tests/bench_1655_a.py.

Loads only the baseline session-search method from git, not atom search.
Reports cold-after-invalidation median (7 runs) and Python allocation peak.
"""
from __future__ import annotations

import argparse
import ast
import statistics
import struct
import subprocess
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import faiss
import mimir.saga.client as client
from mimir.models import AuthContext
from mimir.saga.vector_index import VectorIndex

faiss.omp_set_num_threads(1)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline", default="0a6384fdb90ec4ef4e4d3b17fea30df119754ea2")
args = parser.parse_args()
source = subprocess.check_output([
    "git", "-c", f"safe.directory={Path.cwd()}", "show", f"{args.baseline}:mimir/saga/client.py",
], text=True)
tree = ast.parse(source)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SagaStore")
method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_search_sessions_with_conn")
namespace = dict(vars(client))
exec(compile(ast.Module(body=[method], type_ignores=[]), "baseline", "exec"), namespace)
baseline = namespace[method.name]
scope = AuthContext(principal="alice", canonical_principal="user:alice", roles=("user",),
                    event_ingress="test", trigger="test", channel_id=None,
                    interactivity=None, enforcement_enabled=True)
dimension = 1024
blob = struct.pack(f"{dimension}f", *([1.0] + [0.0] * (dimension - 1)))
query = [1.0] + [0.0] * (dimension - 1)
original_search = VectorIndex.search
for size in (1000, 10000, 30000):
    with tempfile.TemporaryDirectory() as directory:
        store = client.SagaStore(db_path=Path(directory) / "bench.db", embedding_dim=dimension)
        store._sessions_embedding_dim = dimension
        conn = store._ensure_conn()
        conn.executemany(
            "INSERT INTO sessions (id, started_at, ended_at, embedding, embedding_dim, owner_principal, visibility) VALUES (?, ?, ?, ?, ?, ?, 'private')",
            ((f"s-{i:06d}", "2026-01-01", "2026-01-01" if i < 500 else "2026-09-01",
              blob, dimension, "user:alice" if i < 500 else "user:bob") for i in range(size)),
        )
        conn.commit()
        for name, fn in (("before", baseline), ("after", client.SagaStore._search_sessions_with_conn)):
            work = []

            def search(index, vector, top_k=10):
                work.append((index.total_vectors, top_k))
                return original_search(index, vector, top_k=top_k)

            def run():
                # end_session invalidates this flag after every rotation.
                store._sessions_index_built = False
                return fn(store, conn, "q", query_emb=query, auth_context=scope)

            with patch.object(VectorIndex, "search", search):
                run()
                times = []
                for _ in range(7):
                    start = time.perf_counter()
                    result = run()
                    times.append((time.perf_counter() - start) * 1000)
                tracemalloc.start()
                run()
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            assert len(result) == 10 and all(r["similarity_score"] == 1.0 for r in result)
            print(f"{name} rows={size} dim={dimension} cold_median_ms={statistics.median(times):.3f} python_peak_mib={peak / 2**20:.2f} indexed/top_k={work[-1]}")
        conn.close()
