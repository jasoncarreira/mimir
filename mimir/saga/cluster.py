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


EmbeddingRow = tuple[str, bytes, int, str | None, str | None, str | None]


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


def _sum_rows(matrix):
    """Reduce NumPy products with the running Python's float-sum semantics.

    Python 3.12+ uses compensated float summation; neither NumPy's cumsum
    nor its pairwise sum is equivalent at exact cosine thresholds. tolist()
    supplies builtin floats to sum (not NumPy scalars), keeping multiplication
    vectorized and the coordinate reduction in builtin code.
    """
    import numpy as np

    return np.asarray([sum(row.tolist()) for row in matrix], dtype=np.float64)


def fetch_embedding_rows(
    conn: sqlite3.Connection,
    atoms: list[dict],
) -> list[EmbeddingRow]:
    """Fetch embedding/ACL snapshots under the caller's connection lock.

    Rows are (atom_id, vec, dim, owner_principal, origin_domain, visibility).
    This is an internal clustering input, not an authorized read API. Callers
    must select eligible atoms and must not reuse snapshots across ACL changes.
    No decoding or CPU clustering is performed here.
    """
    if not atoms:
        return []
    atom_ids = [a["id"] for a in atoms]
    placeholders = ",".join(["?"] * len(atom_ids))
    return conn.execute(
        f"SELECT e.atom_id, e.vec, e.dim, a.owner_principal, "
        f"a.origin_domain, a.visibility "
        f"FROM embeddings e JOIN atoms a ON a.id = e.atom_id "
        f"WHERE e.atom_id IN ({placeholders})",
        atom_ids,
    ).fetchall()


def cluster_by_similarity(
    conn: sqlite3.Connection,
    atoms: list[dict],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    scope_acl: bool = False,
    embedding_rows: list[EmbeddingRow] | None = None,
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
    To release the connection lock before CPU work, fetch rows with
    ``fetch_embedding_rows(conn, atoms)`` under that lock, then pass them as
    ``embedding_rows`` here outside it. A supplied list (even empty) prevents
    all connection access. Rows must be trusted embedding/ACL snapshots from
    that helper, not caller-supplied ownership claims.
    """
    if not atoms:
        return []

    import numpy as np

    rows = fetch_embedding_rows(conn, atoms) if embedding_rows is None else embedding_rows
    vec_by_atom: dict[str, np.ndarray] = {}
    acl_by_atom: dict[str, tuple[str, str | None, str]] = {}
    for atom_id, vec_bytes, dim, owner, domain, visibility in rows:
        # Missing ownership data cannot establish a safe cluster boundary.
        if scope_acl and (not owner or not visibility):
            continue
        try:
            if not isinstance(dim, int) or dim < 0 or len(vec_bytes) != dim * 4:
                continue
            vec_by_atom[atom_id] = np.frombuffer(vec_bytes, dtype=np.float32).astype(np.float64)
            if owner and visibility:
                acl_by_atom[atom_id] = (owner, domain, visibility)
        except (TypeError, ValueError):
            continue  # malformed; skip atom

    # Batch coordinate products in NumPy, but match _cosine's builtin float
    # reductions and math.sqrt on every supported Python version. Reassociation
    # can change exact threshold boundaries and first-cluster ties.
    import math
    ids_by_dim: dict[int, list[str]] = {}
    for atom_id, vec in vec_by_atom.items():
        ids_by_dim.setdefault(len(vec), []).append(atom_id)
    matrices = {}
    norms = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        for dim, ids in ids_by_dim.items():
            mat = np.vstack([vec_by_atom[atom_id] for atom_id in ids])
            matrices[dim] = mat
            norms[dim] = np.asarray(
                [math.sqrt(total) for total in _sum_rows(mat * mat)],
                dtype=np.float64,
            )

    # Greedy single-pass.
    cluster_atoms: list[list[dict]] = []
    cluster_acls: list[tuple[str, str | None, str]] = []
    for atom in atoms:
        vec = vec_by_atom.get(atom["id"])
        acl = acl_by_atom.get(atom["id"])
        if vec is None or (scope_acl and acl is None):
            continue  # no embedding/ACL; can't safely cluster
        dim = len(vec)
        scores = np.zeros(len(ids_by_dim[dim]))
        if dim:
            with np.errstate(invalid="ignore", divide="ignore"):
                norm = math.sqrt(sum((vec * vec).tolist()))
                dots = _sum_rows(matrices[dim] * vec)
                np.divide(
                    dots, norms[dim] * norm, out=scores,
                    where=(norms[dim] != 0.0) & (norm != 0.0),
                )
        sim_by_atom = dict(zip(ids_by_dim[dim], scores.tolist()))
        best_idx = -1
        best_sim = -1.0
        for i, members in enumerate(cluster_atoms):
            if scope_acl and cluster_acls[i] != acl:
                continue
            # Different dimensions and zero vectors contribute zero, but still
            # count in the denominator. Do not normalize a cluster centroid.
            sim = sum(sim_by_atom.get(a["id"], 0.0) for a in members) / len(members)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_idx >= 0 and best_sim >= threshold:
            cluster_atoms[best_idx].append(atom)
        else:
            cluster_atoms.append([atom])
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
