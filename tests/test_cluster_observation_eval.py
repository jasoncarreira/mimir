from __future__ import annotations

from evals.cluster_observation.adapter import (
    COMPONENT_RICH_PROMPT,
    ClusterObservationAdapter,
    Example,
    render_candidate_prompt,
)
from evals.cluster_observation.metrics import score_candidate


def _example() -> dict:
    return {
        "example_id": "ex-1",
        "source_cluster": {
            "evidence_atom_ids": ["a1", "a2"],
            "atoms": [
                {"atom_id": "a1", "content": "Chainlink #614 fixed SAGA retrieval on 2026-06-12."},
                {"atom_id": "a2", "content": "Muninn kept the SAGA corpus for validation."},
            ],
        },
        "retrieval_probes": [{"query": "Chainlink #614"}],
        "evaluator_annotations": {
            "required_identifiers_dates_numbers_names": {
                "identifiers": ["Chainlink #614", "SAGA", "Muninn"],
                "dates": ["2026-06-12"],
                "numbers": ["614"],
                "names": ["Muninn"],
            }
        },
        "strata": {"identifier_dense": True},
    }


def _raw(obs: str, triples: str = "NONE", contradictions: str = "NONE") -> str:
    return f"OBSERVATION:\n{obs}\n\nTRIPLES:\n{triples}\n\nCONTRADICTIONS:\n{contradictions}\n"


def test_identifier_retention_miss_is_actionable():
    ev = score_candidate(_example(), _raw("A retrieval fix happened."))

    assert ev.score == 0.0
    assert ev.asi["hard_fail"] == "identifier_dense_symbolic_collapse"
    assert "Chainlink #614" in ev.asi["symbolic_retention"]["missing"]["identifiers"]
    assert "2026-06-12" in ev.asi["symbolic_retention"]["missing"]["dates"]


def test_unsupported_claim_miss_is_actionable():
    ev = score_candidate(
        _example(),
        _raw("Chainlink #614 fixed SAGA retrieval on 2026-06-12 and PR #999 deployed it."),
    )

    assert ev.score == 0.0
    assert ev.asi["hard_fail"] == "unsupported_high_severity_claim"
    spans = [u["candidate_span"] for u in ev.asi["support"]["unsupported_high_severity"]]
    assert "PR #999" in spans


def test_parser_format_failure_is_hard_gate():
    ev = score_candidate(
        _example(),
        "OBSERVATION:\nChainlink #614 fixed SAGA retrieval.\nTRIPLES:\nNONE\nCONTRADICTIONS:\nNONE",
    )

    assert ev.score == 0.0
    assert ev.asi["hard_fail"] == "parser_compatibility"
    assert any("swallowed" in err for err in ev.asi["parser"]["errors"])


def test_retrieval_geometry_regression_is_reported():
    ex = _example()

    def embed(text: str):
        if "Chainlink #614" in text:
            return (1.0, 0.0)
        if "retrieval fix" in text or "fix happened" in text:
            return (0.0, 1.0)
        return (1.0, 0.0)

    good = score_candidate(ex, _raw("Chainlink #614 fixed SAGA retrieval on 2026-06-12 for Muninn."), embedding_fn=embed)
    bad = score_candidate(ex, _raw("Muninn saw that a retrieval fix happened."), embedding_fn=embed)

    assert not bad.asi["retrieval_geometry"]["skipped"]
    assert bad.asi["retrieval_geometry"]["score"] < good.asi["retrieval_geometry"]["score"]


def test_adapter_evaluates_raw_outputs_and_returns_reflective_asi():
    ex = Example(id="ex-1", split="train", data=_example())

    async def synth(prompt: str, example: Example) -> str:
        assert "[1] Chainlink #614" in render_candidate_prompt(prompt, example)
        return _raw("Chainlink #614 fixed SAGA retrieval on 2026-06-12 for Muninn.")

    adapter = ClusterObservationAdapter([ex], synth)
    batch = adapter.evaluate([ex], {COMPONENT_RICH_PROMPT: "Atoms:\n{indexed_atoms}"}, capture_traces=True)

    assert len(batch.scores) == 1
    assert batch.scores[0] > 0.0
    reflective = adapter.make_reflective_dataset({COMPONENT_RICH_PROMPT: "x"}, batch, [COMPONENT_RICH_PROMPT])
    assert "symbolic_retention" in reflective[COMPONENT_RICH_PROMPT][0]["Feedback"]
