"""Cosine-similarity-based agglomerative clustering for reflect.

Default clusterer for reflect's observation synthesis. Greedy single-
pass: walk atoms in arbitrary order, for each new atom either join an
existing cluster (if mean cosine similarity to cluster members exceeds
threshold) or start a new cluster.

Tradeoffs vs alternatives:

- **vs k-means**: k-means needs a pre-set k. We don't know how many
  observation-worthy clusters a session has; greedy single-pass figures
  it out.
- **vs hierarchical full-linkage**: full-linkage is O(n²) and produces
  better-shaped clusters, but a session's raws are usually ≤100 atoms
  and the quality gain isn't worth the perf cost. Single-pass is O(n·c)
  where c is the cluster count.
- **vs entity-based** (Hindsight): entity clustering groups by extracted
  named entities ("Alice", "PR #157"). Better quality for fact-heavy
  domains but requires an NER pass per atom. Tier 3 stretch.

Threshold default 0.6 — empirically tuned during saga's bench iteration
against LongMemEval-S. Lower threshold → larger clusters with more
heterogeneous atoms (observation synthesis has to abstract more).
Higher → tighter clusters that may miss conceptually-related atoms
phrased differently.
"""

from __future__ import annotations

import sqlite3
import struct
from typing import Callable


# Default threshold for OpenAI text-embedding-3-small (1536d) /
# saga's canonical bench. Calibrated against LongMemEval-S via the
# threshold sweep in `benchmarks/longmemeval_via_memory/threshold_sweep.py`:
# 0.80 produces ~12 clusters/question with mean intra-cluster cohesion
# 0.84 — tight enough that observation synthesis has on-topic evidence,
# small enough that the eligible set fits inside any reasonable cap.
# Below 0.70 the clusters become kitchen-sink (cohesion < 0.76) and
# the 20-cluster cap silently drops 40-50 candidates per question
# (bench v1 ran at 0.60 and hit this — see 73.4% baseline metrics).
# Voyage's 1024d distributions are tighter, so a higher value may be
# appropriate when switching providers.
DEFAULT_SIMILARITY_THRESHOLD = 0.80

# Floor on cluster size that triggers a similarity check. Below this
# (e.g. one-atom clusters), every new atom is considered for join.
MIN_CLUSTER_FOR_THRESHOLD = 1


def _unpack_vec(vec_bytes: bytes, dim: int) -> list[float]:
    """Unpack the raw float32 bytes stored in embeddings.vec."""
    return list(struct.unpack(f"{dim}f", vec_bytes))


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 for zero-norm inputs (defensive)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _mean_cosine(vec: list[float], cluster_vecs: list[list[float]]) -> float:
    """Mean cosine of ``vec`` against each member of the cluster."""
    if not cluster_vecs:
        return 0.0
    return sum(_cosine(vec, v) for v in cluster_vecs) / len(cluster_vecs)


def cluster_by_similarity(
    conn: sqlite3.Connection,
    atoms: list[dict],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    scope_acl: bool = False,
) -> list[list[dict]]:
    """Greedy single-pass agglomerative clustering.

    For each atom (in input order):
    1. Compute mean cosine similarity vs each existing cluster's members
    2. If best-matching cluster's similarity ≥ threshold, join it
    3. Otherwise, start a new cluster

    Returns the final cluster list, ordered by creation (oldest first).
    Atoms with no embedding row are silently skipped (shouldn't happen
    if store() was used; defensive).

    Caller: reflect() passes this as the ``cluster_fn`` injection.
    """
    if not atoms:
        return []

    # Bulk-fetch embeddings for all input atoms in one query.
    atom_ids = [a["id"] for a in atoms]
    placeholders = ",".join(["?"] * len(atom_ids))
    rows = conn.execute(
        f"SELECT e.atom_id, e.vec, e.dim, a.owner_principal, "
        f"a.origin_domain, a.visibility "
        f"FROM embeddings e JOIN atoms a ON a.id = e.atom_id "
        f"WHERE e.atom_id IN ({placeholders})",
        atom_ids,
    ).fetchall()
    vec_by_atom: dict[str, list[float]] = {}
    acl_by_atom: dict[str, tuple[str, str | None, str]] = {}
    for atom_id, vec_bytes, dim, owner, domain, visibility in rows:
        # Missing ownership data cannot establish a safe cluster boundary.
        if scope_acl and (not owner or not visibility):
            continue
        try:
            vec_by_atom[atom_id] = _unpack_vec(vec_bytes, dim)
            if owner and visibility:
                acl_by_atom[atom_id] = (owner, domain, visibility)
        except struct.error:
            continue  # malformed; skip atom

    # Greedy single-pass.
    cluster_atoms: list[list[dict]] = []
    cluster_vecs: list[list[list[float]]] = []
    cluster_acls: list[tuple[str, str | None, str]] = []
    for atom in atoms:
        vec = vec_by_atom.get(atom["id"])
        acl = acl_by_atom.get(atom["id"])
        if vec is None or (scope_acl and acl is None):
            continue  # no embedding/ACL; can't safely cluster
        best_idx = -1
        best_sim = -1.0
        for i, vecs in enumerate(cluster_vecs):
            if scope_acl and cluster_acls[i] != acl:
                continue
            sim = _mean_cosine(vec, vecs)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_idx >= 0 and best_sim >= threshold:
            cluster_atoms[best_idx].append(atom)
            cluster_vecs[best_idx].append(vec)
        else:
            cluster_atoms.append([atom])
            cluster_vecs.append([vec])
            cluster_acls.append(acl or ("", None, ""))
    return cluster_atoms


def make_default_cluster_fn(
    conn: sqlite3.Connection,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    *,
    scope_acl: bool = False,
) -> Callable[[list[dict]], list[list[dict]]]:
    """Bind a clusterer to a specific connection + threshold.
    Returns a callable matching reflect.ClusterFn (atoms → clusters).

    Use:
        from reflect import reflect
        from cluster import make_default_cluster_fn
        reflect(conn, sid, ..., cluster_fn=make_default_cluster_fn(conn))
    """
    def _fn(atoms: list[dict]) -> list[list[dict]]:
        return cluster_by_similarity(
            conn, atoms, threshold=threshold, scope_acl=scope_acl,
        )
    return _fn
