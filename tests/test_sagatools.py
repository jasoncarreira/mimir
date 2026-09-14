from __future__ import annotations

import pytest

from mimir.sagatools import (
    _ATOM_CONTENT_CAP,
    _format_atoms,
    _format_saga_payload,
    _format_triples,
)


@pytest.mark.parametrize("shape", ["atoms", "observations", "raws", "legacy"])
@pytest.mark.parametrize("integrities", [("untrusted", "trusted", None), (None, None, None)])
def test_payload_preserves_order_without_trust_grouping(shape, integrities):
    atoms = []
    triples = []
    for i, integrity in enumerate(integrities):
        atom = {"content": f"memory {i}", "score": 0.9 - i * 0.1}
        triple = {"subject": f"subject {i}", "predicate": "is", "object": "here"}
        if integrity is not None:
            atom["integrity"] = integrity
            triple["integrity"] = integrity
        atoms.append(atom)
        triples.append(triple)
    if shape == "legacy":
        payload = {"_raw_atoms": atoms[:1], "sections": {"memories": atoms[1:]}}
    else:
        payload = {shape: atoms}
    payload["triples"] = triples

    rendered = _format_saga_payload(payload)

    assert "Trusted-origin" not in rendered
    assert "Untrusted-origin" not in rendered
    assert rendered == (
        "- [atom (0.900)] memory 0\n"
        "- [atom (0.800)] memory 1\n"
        "- [atom (0.700)] memory 2\n\n"
        "- (subject 0, is, here)\n"
        "- (subject 1, is, here)\n"
        "- (subject 2, is, here)"
    )


def test_atom_formatting_and_provenance():
    assert _format_atoms([{
        "memory_type": "observation",
        "confidence_tier": "high",
        "similarity": 0.87654,
        "content": "  first\nsecond  ",
        "origin_trigger": "user",
        "origin_ref": " chat\n42 ",
        "captured_at": "2026-09-14T12:00:00Z",
        "metadata": {"origin_ref": "ignored"},
    }]) == (
        "- [observation/high (0.877)]"
        " [trigger=user; ref=chat 42; captured=2026-09-14T12:00:00Z] first second"
    )
    assert _format_atoms([{"content": "x" * (_ATOM_CONTENT_CAP + 1)}]) == (
        "- [atom] " + "x" * _ATOM_CONTENT_CAP + "\u2026"
    )


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        ({}, ""),
        ({"valid_from": "2026-01-01T12:00:00Z"}, " [valid 2026-01-01 \u2192 present]"),
        ({"valid_until": "2026-09-14"}, " [valid \u2192 2026-09-14]"),
        (
            {"valid_from": "2026-01-01", "valid_until": "2026-09-14"},
            " [valid 2026-01-01 \u2192 2026-09-14]",
        ),
    ],
)
def test_triple_formatting_and_provenance(dates, expected):
    assert _format_triples([{
        "subject": "Alice",
        "predicate": "lives_in",
        "object": "Paris",
        "confidence": 0.876,
        "origin_trigger": "user",
        "origin_ref": "chat:42",
        "captured_at": "2026-09-14",
        **dates,
    }]) == (
        f"- (Alice, lives_in, Paris){expected} (conf 0.88)"
        " [trigger=user; ref=chat:42; captured=2026-09-14]"
    )
    assert _format_triples([{"confidence": 1.0}]) == "- (?, ?, ?)"


def test_empty_rendering():
    assert _format_atoms([]) == "(no atoms)"
    assert _format_triples([]) == ""
    assert _format_saga_payload({}) == "(no atoms)"
