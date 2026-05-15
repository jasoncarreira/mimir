"""Schema/shape tests for the chainlink #140 (Sub B) probe set."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from benchmarks.file_search_autopass_ab.runner import load_probes


PROBES_PATH = (
    Path(__file__).resolve().parent.parent
    / "benchmarks"
    / "file_search_autopass_ab"
    / "probes.yaml"
)

EXPECTED_SHAPES = {
    "fingerprinted-error",
    "concept-lookup",
    "recent-decision",
    "procedural",
}


def test_probes_yaml_loads_and_has_exactly_30():
    probes = load_probes(PROBES_PATH)
    assert len(probes) == 30, (
        f"expected exactly 30 probes per chainlink #140 spawn brief; "
        f"got {len(probes)}"
    )


def test_each_probe_has_required_keys():
    probes = load_probes(PROBES_PATH)
    for p in probes:
        assert isinstance(p.get("text"), str) and p["text"].strip(), p
        assert isinstance(p.get("expected_target"), str) and p["expected_target"].strip(), p
        assert p.get("shape") in EXPECTED_SHAPES, p
        assert isinstance(p.get("_index"), int) and p["_index"] >= 1, p


def test_all_four_shapes_represented():
    probes = load_probes(PROBES_PATH)
    shapes = {p["shape"] for p in probes}
    missing = EXPECTED_SHAPES - shapes
    assert not missing, f"missing shapes: {missing}"


def test_shape_distribution_roughly_even():
    """Brief says aim for ~7-8 of each shape across the 30 probes."""
    probes = load_probes(PROBES_PATH)
    counts = Counter(p["shape"] for p in probes)
    for shape in EXPECTED_SHAPES:
        assert 5 <= counts[shape] <= 10, (
            f"shape {shape} count={counts[shape]} outside the 5-10 band; "
            f"full distribution: {dict(counts)}"
        )


def test_probe_indices_are_unique_and_one_indexed():
    probes = load_probes(PROBES_PATH)
    indices = [p["_index"] for p in probes]
    assert indices == list(range(1, len(probes) + 1)), (
        "expected probe _index values 1..N in order"
    )


def test_load_probes_rejects_invalid_shape(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "probes:\n"
        "  - text: hi\n"
        "    expected_target: foo\n"
        "    shape: nonsense\n"
    )
    with pytest.raises(ValueError, match="invalid shape"):
        load_probes(bad)


def test_load_probes_rejects_missing_key(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "probes:\n"
        "  - text: hi\n"
        "    shape: procedural\n"
    )
    with pytest.raises(ValueError, match="missing required key"):
        load_probes(bad)
