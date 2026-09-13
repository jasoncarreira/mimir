from __future__ import annotations

import math
import random
import sqlite3
import struct
from concurrent.futures import ThreadPoolExecutor

import pytest

from mimir.saga.cluster import (
    _mean_cosine,
    cluster_by_similarity,
    fetch_embedding_rows,
    make_default_cluster_fn,
)


def _row(atom_id, vec, owner="alice", domain=None, visibility="private"):
    return (atom_id, struct.pack(f"{len(vec)}f", *vec), len(vec), owner, domain, visibility)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE atoms (id TEXT PRIMARY KEY, owner_principal TEXT, "
        "origin_domain TEXT, visibility TEXT);"
        "CREATE TABLE embeddings (atom_id TEXT PRIMARY KEY, vec BLOB, dim INTEGER);"
    )
    yield connection
    connection.close()


def _insert(conn, rows):
    for atom_id, blob, dim, owner, domain, visibility in rows:
        conn.execute("INSERT INTO atoms VALUES (?, ?, ?, ?)", (atom_id, owner, domain, visibility))
        conn.execute("INSERT INTO embeddings VALUES (?, ?, ?)", (atom_id, blob, dim))


def _reference(atoms, rows, threshold, scope_acl=False):
    """The original scalar greedy algorithm, including its -1 sentinel."""
    vectors = {}
    acls = {}
    for atom_id, blob, dim, owner, domain, visibility in rows:
        if scope_acl and (not owner or not visibility):
            continue
        try:
            vectors[atom_id] = list(struct.unpack(f"{dim}f", blob))
        except struct.error:
            continue
        if owner and visibility:
            acls[atom_id] = (owner, domain, visibility)
    clusters = []
    for atom in atoms:
        atom_id = atom["id"]
        if atom_id not in vectors or (scope_acl and atom_id not in acls):
            continue
        best_idx, best_sim = -1, -1.0
        for i, members in enumerate(clusters):
            if scope_acl and acls[members[0]["id"]] != acls[atom_id]:
                continue
            sim = _mean_cosine(vectors[atom_id], [vectors[a["id"]] for a in members])
            if sim > best_sim:
                best_idx, best_sim = i, sim
        if best_idx >= 0 and best_sim >= threshold:
            clusters[best_idx].append(atom)
        else:
            clusters.append([atom])
    return clusters


@pytest.mark.parametrize("scope_acl", [False, True])
@pytest.mark.parametrize("threshold", [-1.0, -0.2, 0.0, 0.3, 0.8, 1.0, 1.1])
def test_matches_scalar_greedy(conn, scope_acl, threshold):
    rng = random.Random(1701)
    rows = [
        _row(str(i), [rng.uniform(-10, 10) for _ in range(rng.choice([3, 7, 64]))],
             owner=rng.choice(["alice", "bob"]), domain=rng.choice([None, "room"]))
        for i in range(60)
    ]
    rows += [_row("zero", [0, 0, 0]), _row("empty", [])]
    atoms = [{"id": row[0]} for row in rows] + [{"id": "missing"}]
    rng.shuffle(atoms)
    atoms.append(atoms[0])  # Duplicate inputs keep their weight and position.
    _insert(conn, rows)
    expected = _reference(atoms, rows, threshold, scope_acl)
    assert cluster_by_similarity(conn, atoms, threshold=threshold, scope_acl=scope_acl) == expected
    assert cluster_by_similarity(
        conn, atoms, threshold=threshold, scope_acl=scope_acl,
        embedding_rows=fetch_embedding_rows(conn, atoms),
    ) == expected


@pytest.mark.parametrize("sum_mode", ["native", "compensated"])
@pytest.mark.parametrize("direction", [-math.inf, None, math.inf])
def test_exact_float64_threshold(conn, direction, sum_mode, monkeypatch):
    if sum_mode == "compensated":
        # Exercise non-left-to-right reduction even on Python 3.11. fsum is
        # not a Python 3.12 sum emulator; both oracle and implementation must
        # honor the selected scalar reduction rather than hard-code cumsum.
        monkeypatch.setattr("mimir.saga.cluster.sum", math.fsum, raising=False)
    rng = random.Random(1701)
    rows = [_row(str(i), [rng.random() for _ in range(1536)]) for i in range(3)]
    atoms = [{"id": row[0]} for row in rows]
    vectors = [list(struct.unpack("1536f", row[1])) for row in rows]
    threshold = _mean_cosine(vectors[1], [vectors[0]])
    if direction is not None:
        threshold = math.nextafter(threshold, direction)
    assert cluster_by_similarity(conn, atoms, threshold=threshold, embedding_rows=rows) == _reference(
        atoms, rows, threshold,
    )


@pytest.mark.parametrize(
    "vectors,threshold,expected",
    [
        ([[1, 0], [0, 1], [1, 1]], 0.7, [[0, 2], [1]]),  # First tie wins.
        ([[1, 0], [0.6, 0.8], [0, 1]], 0.5, [[0, 1], [2]]),  # Mean, not centroid cosine.
        ([[1], [-1]], -1.0, [[0], [1]]),  # Strictly greater than -1 sentinel.
        ([[1], [1, 0], [1]], 0.0, [[0, 1, 2]]),
        ([[0], [1]], 0.0, [[0, 1]]),
        ([[], [1]], 0.0, [[0, 1]]),
    ],
)
def test_greedy_edge_cases(conn, vectors, threshold, expected):
    rows = [_row(str(i), vec) for i, vec in enumerate(vectors)]
    atoms = [{"id": row[0]} for row in rows]
    clusters = cluster_by_similarity(conn, atoms, threshold=threshold, embedding_rows=rows)
    assert clusters == [[atoms[i] for i in members] for members in expected]


def test_malformed_and_nonfinite(conn):
    rows = [
        _row("valid", [1, 0]), _row("nan", [math.nan, 1]),
        _row("inf", [math.inf, 0]), _row("zero", [0, 0]),
        ("short", b"\x00", 2, "alice", None, "private"),
        ("long", b"\x00" * 12, 2, "alice", None, "private"),
        ("negative", b"", -1, "alice", None, "private"),
    ]
    atoms = [{"id": row[0]} for row in rows]
    assert cluster_by_similarity(conn, atoms, threshold=0, embedding_rows=rows) == _reference(
        atoms, rows, 0,
    )
    malformed = [("null", None, 2, "alice", None, "private"),
                 ("dim", b"", None, "alice", None, "private")]
    assert cluster_by_similarity(conn, [{"id": r[0]} for r in malformed], embedding_rows=malformed) == []


def test_acl_uses_stored_boundary_and_fails_closed(conn):
    rows = [
        _row("a", [1], "alice", "room", "private"),
        _row("b", [1], "bob", "room", "private"),
        _row("c", [1], "alice", "other", "private"),
        _row("d", [1], "alice", "room", "shared"),
        _row("e", [1], None), _row("f", [1], visibility=""),
        _row("g", [1], "alice", "room", "private"),
    ]
    _insert(conn, rows)
    atoms = [{"id": r[0], "owner_principal": "forged", "visibility": "shared"} for r in rows]
    expected = [[atoms[0], atoms[6]], [atoms[1]], [atoms[2]], [atoms[3]]]
    for preloaded in (None, fetch_embedding_rows(conn, atoms)):
        assert cluster_by_similarity(conn, atoms, scope_acl=True, embedding_rows=preloaded) == expected
        assert cluster_by_similarity(conn, atoms, embedding_rows=preloaded) == [atoms]
    assert make_default_cluster_fn(conn, scope_acl=True)(atoms) == expected


def test_preloaded_rows_need_no_connection_access(conn):
    rows = [_row("a", [1]), _row("b", [1]), _row("unrequested", [1])]
    _insert(conn, rows)
    atoms = [{"id": "b"}, {"id": "a"}]
    fetched = fetch_embedding_rows(conn, atoms)
    assert {r[0] for r in fetched} == {"a", "b"}
    assert fetch_embedding_rows(conn, []) == []
    # SQLite's default thread guard rejects access from this CPU worker.
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(cluster_by_similarity, conn, atoms, embedding_rows=fetched).result() == [atoms]
        assert pool.submit(cluster_by_similarity, conn, atoms, embedding_rows=[]).result() == []
    conn.close()
    assert cluster_by_similarity(conn, atoms, embedding_rows=fetched) == [atoms]
    assert cluster_by_similarity(conn, []) == []
