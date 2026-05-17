"""Sanity-check the probe set for chainlink #141 Slice 2.

These tests don't touch the retrieval stack — they just validate
``probes.json``'s shape so a typo or missing field surfaces before
a 15-minute end-to-end run. Mirrors
``tests/test_file_search_autopass_ab_probes.py`` (chainlink #140).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROBES = Path(__file__).resolve().parents[1] / \
    "benchmarks" / "file_search_colbert_recency" / "probes.json"


def _load() -> list[dict]:
    return json.loads(PROBES.read_text())["probes"]


def test_probes_file_exists() -> None:
    assert PROBES.is_file(), f"missing probe set: {PROBES}"


def test_probes_count_in_target_range() -> None:
    probes = _load()
    # Brief targets ~45-50; we shipped 49.
    assert 45 <= len(probes) <= 60, (
        f"probe count out of range: {len(probes)}"
    )


def test_each_probe_has_required_fields() -> None:
    for p in _load():
        for key in ("id", "query", "expected_paths", "category"):
            assert key in p, f"probe {p.get('id')} missing {key!r}"
        assert isinstance(p["expected_paths"], list)
        assert p["expected_paths"], f"probe {p['id']} has empty expected_paths"
        assert all(isinstance(s, str) and s for s in p["expected_paths"])


def test_probe_ids_unique_and_sequential() -> None:
    ids = [p["id"] for p in _load()]
    assert len(ids) == len(set(ids)), "duplicate probe id"
    assert ids == sorted(ids), "probe ids should be sorted"


def test_categories_in_allowed_set() -> None:
    allowed = {"path-citation", "colbert-favorable", "rare-token"}
    for p in _load():
        assert p["category"] in allowed, (
            f"probe {p['id']} has unknown category {p['category']!r}"
        )


def test_category_distribution_reasonable() -> None:
    """We promised ~30 path-citation (carried from #140) and 15-20
    new ColBERT-favorable / rare-token probes. Loose bounds to keep
    the harness easy to refine without breaking the test."""
    counts: dict[str, int] = {}
    for p in _load():
        counts[p["category"]] = counts.get(p["category"], 0) + 1
    assert counts.get("path-citation", 0) >= 25, counts
    assert counts.get("colbert-favorable", 0) >= 8, counts
    assert counts.get("rare-token", 0) >= 5, counts
