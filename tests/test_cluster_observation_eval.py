from __future__ import annotations

from evals.cluster_observation.adapter import (
    COMPONENT_RICH_PROMPT,
    ClusterObservationAdapter,
    Example,
    render_candidate_prompt,
)
from evals.cluster_observation.metrics import score_candidate, score_prompt_candidate


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


def test_calibration_filters_regex_artifacts_without_hiding_real_unsupported_ids():
    ex = {
        "example_id": "calibration-artifacts",
        "source_cluster": {
            "evidence_atom_ids": ["a1", "a2"],
            "atoms": [
                {
                    "atom_id": "a1",
                    "content": (
                        "arXiv:2606.13141 V-RAGBench / CARVE was judged relevant on "
                        "2026-06-12; evaluate prompt/latency cost and downstream answer/action quality."
                    ),
                },
                {
                    "atom_id": "a2",
                    "content": "Voyage-4-lite uses 1024d embeddings and OpenAI uses 1536d embeddings.",
                },
            ],
        },
        "evaluator_annotations": {
            "required_identifiers_dates_numbers_names": {
                "identifiers": ["arXiv:2606.13141", "V-RAGBench", "CARVE", "2026-06-12"],
                "dates": ["2026-06-12"],
                "numbers": ["2606.13141", "2026", "06", "12", "1024d", "1536d"],
                "names": ["V-RAGBench", "CARVE", "Voyage"],
            }
        },
        "strata": {"identifier_dense": True},
    }

    ev = score_candidate(
        ex,
        _raw(
            "Several arXiv papers cover memory/retrieval and prompt/latency tradeoffs. "
            "The V-RAGBench / CARVE item stayed relevant on 2026-06-12; "
            "Voyage used 1024 dimensions and OpenAI used +1536 dimensions."
        ),
    )

    assert ev.asi["hard_fail"] is None
    high_spans = [u["candidate_span"] for u in ev.asi["support"]["unsupported_high_severity"]]
    assert "arXiv papers" not in high_spans
    assert "/retrieval" not in high_spans
    assert "1024" not in high_spans
    assert "1536" not in high_spans

    pp = score_candidate(
        {
            "example_id": "pp",
            "source_cluster": {
                "atoms": [{"atom_id": "a1", "content": "Arm A scored 0.904 and Arm B scored 0.88."}],
            },
        },
        _raw("Arm A led by a 2.4 percentage-point margin."),
    )
    assert pp.asi["support"]["unsupported_high_severity"] == []

    bad = score_candidate(ex, _raw("PR #999 changed the V-RAGBench result."))
    assert bad.asi["hard_fail"] == "unsupported_high_severity_claim"
    assert "PR #999" in [u["candidate_span"] for u in bad.asi["support"]["unsupported_high_severity"]]


def test_structural_indices_and_identifier_number_components_are_ignored():
    ex = {
        "example_id": "indices",
        "source_cluster": {
            "evidence_atom_ids": ["a1"],
            "atoms": [
                {
                    "atom_id": "a1",
                    "content": "arXiv:2606.20280 ELVA was reviewed on 2026-06-12.",
                }
            ],
        },
        "evaluator_annotations": {
            "required_identifiers_dates_numbers_names": {
                "identifiers": ["arXiv:2606.20280"],
                "dates": ["2026-06-12"],
                "numbers": ["2606.20280", "2026", "06", "12"],
                "names": ["ELVA"],
            }
        },
        "strata": {"identifier_dense": True},
    }

    ev = score_candidate(ex, _raw("[1] arXiv:2606.20280 ELVA was reviewed on 2026-06-12."))

    assert ev.asi["hard_fail"] is None
    assert ev.asi["support"]["unsupported_high_severity"] == []
    assert ev.asi["symbolic_retention"]["required_count"] == 3


def test_identifier_dense_hard_gate_spares_primary_id_covered_summary():
    ex = {
        "example_id": "primary-id-covered",
        "source_cluster": {
            "evidence_atom_ids": ["a1", "a2"],
            "atoms": [
                {"atom_id": "a1", "content": "arXiv:2606.10000 AlphaBench introduced an agent memory benchmark."},
                {"atom_id": "a2", "content": "arXiv:2606.20000 BetaBench added retrieval hard negatives."},
            ],
        },
        "evaluator_annotations": {
            "required_identifiers_dates_numbers_names": {
                "identifiers": [
                    "arXiv:2606.10000",
                    "AlphaBench",
                    "BetaBench",
                    "/concepts/agent-memory",
                    "/retrieval/hard-negative",
                    "/benchmark/corpus",
                    "/source/modal/time",
                    "/saga/observation",
                    "/cluster/evidence",
                    "/wiki/page",
                    "/graph/path",
                    "/eval/holdout",
                    "/memory/index",
                ],
                "numbers": ["2606.10000", "2606.20000"],
                "names": ["AlphaBench", "BetaBench"],
            }
        },
        "strata": {"identifier_dense": True},
    }

    ev = score_candidate(
        ex,
        _raw("arXiv:2606.10000 AlphaBench introduced an agent memory benchmark."),
    )

    assert ev.asi["symbolic_retention"]["score"] < 0.20
    assert ev.asi["coverage"]["score"] >= 0.50
    assert ev.asi["hard_fail"] is None


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


def test_prompt_overfit_regularizer_penalizes_corpus_specific_glossaries():
    generic = """You are consolidating {n} atoms.

OBSERVATION:
<one or two sentences>

TRIPLES:
(subject, predicate, object)
[OR exactly: NONE]

CONTRADICTIONS:
[OR exactly: NONE]

Preserve identifiers from the atoms, but do not invent them.
Atoms:
{indexed_atoms}
"""
    overfit = generic + """

KNOWN ARXIV IDS:
- arXiv:2606.06003
- arXiv:2606.05901
- arXiv:2606.11945
- arXiv:2606.11350
- arXiv:2606.13550
- arXiv:2606.13141
- arXiv:2606.16494
- arXiv:2606.17910

KNOWN COMPACT ALIASES:
- state/wiki/concepts/cost-aware-evidence-selection.md -> cost-aware-evidence.md
- state/wiki/concepts/admem-advanced-memory-task-solving-agents.md -> admem_memory_agents

DOMAIN-SPECIFIC GUIDANCE:
- OMAGR
- TrafficOmni-RAG
- ScoreGate
- MRAgent
- MARDoc
- MemDreamer
- HiMPO
- EvoArena
- StreamMemBench
- ReGrad
""" + ("\nAdditional domain-specific instruction." * 250)

    generic_score = score_prompt_candidate(generic)
    overfit_score = score_prompt_candidate(overfit)

    assert generic_score["penalty"] == 0.0
    assert overfit_score["penalty"] >= 0.30
    assert "hardcoded_identifiers" in overfit_score["signals"]
    assert "corpus_glossary_section" in overfit_score["signals"]
    assert overfit_score["counts"]["arxiv_ids"] == 8


def test_adapter_applies_prompt_overfit_penalty_to_scores_and_asi():
    ex = Example(id="ex-1", split="train", data=_example())

    async def synth(prompt: str, example: Example) -> str:
        return _raw("Chainlink #614 fixed SAGA retrieval on 2026-06-12 for Muninn.")

    prompt = "Atoms:\n{indexed_atoms}\nKNOWN ARXIV IDS:\n" + "\n".join(
        f"- arXiv:2606.{i:05d}" for i in range(20)
    )
    adapter = ClusterObservationAdapter([ex], synth)
    batch = adapter.evaluate([ex], {COMPONENT_RICH_PROMPT: prompt}, capture_traces=True)

    penalty = batch.trajectories[0]["asi"]["prompt_overfit"]["penalty"]
    assert penalty > 0.0
    assert batch.scores[0] == 0.0
    assert batch.trajectories[0]["asi"]["prompt_overfit"]["pass"] is False
    assert batch.trajectories[0]["asi"]["score_breakdown"]["prompt_overfit_gate_passed"] is False
    assert "hardcoded_identifiers" in batch.trajectories[0]["asi"]["prompt_overfit"]["signals"]


def test_prompt_overfit_gate_fails_undelimited_corpus_literals():
    prompt = """You are consolidating {n} atoms.

Known identifiers from the pilot corpus:
- arXiv:2606.13141
- PR #843
- state/wiki/concepts/cost-aware-evidence-selection.md

Atoms:
{indexed_atoms}
"""

    score = score_prompt_candidate(prompt)

    assert score["pass"] is False
    assert score["gate"]["passed"] is False
    assert "hardcoded_arxiv_ids" in score["gate"]["hard_fail_reasons"]
    assert "hardcoded_pr_or_issue_ids" in score["gate"]["hard_fail_reasons"]
    assert "hardcoded_paths" in score["gate"]["hard_fail_reasons"]


def test_prompt_overfit_gate_ignores_deliberately_frozen_example_blocks():
    prompt = """You are consolidating {n} atoms.
Preserve identifiers from source atoms; do not preload pilot-corpus IDs.

BEGIN FROZEN EXAMPLE
Input atom: arXiv:2606.13141 is about state/wiki/concepts/example.md and PR #843.
Output observation: arXiv:2606.13141 covered the example.
END FROZEN EXAMPLE

Atoms:
{indexed_atoms}
"""

    score = score_prompt_candidate(prompt)

    assert score["pass"] is True
    assert score["frozen_example_blocks"] == 1
    assert score["counts"]["arxiv_ids"] == 0
    assert score["counts"]["issue_ids"] == 0
    assert score["counts"]["path_literals"] == 0


def test_adapter_zeroes_score_when_prompt_overfit_gate_fails():
    ex = Example(id="ex-1", split="train", data=_example())

    async def synth(prompt: str, example: Example) -> str:
        return _raw("Chainlink #614 fixed SAGA retrieval on 2026-06-12 for Muninn.")

    prompt = "Atoms:\n{indexed_atoms}\nKNOWN ARXIV IDS:\n- arXiv:2606.13141\n"
    adapter = ClusterObservationAdapter([ex], synth)
    batch = adapter.evaluate([ex], {COMPONENT_RICH_PROMPT: prompt}, capture_traces=True)

    assert batch.scores == [0.0]
    asi = batch.trajectories[0]["asi"]
    assert asi["prompt_overfit"]["pass"] is False
    assert asi["score_breakdown"]["prompt_overfit_gate_passed"] is False


def test_meta_cluster_wrapper_is_hard_gate():
    ev = score_candidate(
        _example(),
        _raw("These 2 atoms document that Chainlink #614 fixed SAGA retrieval on 2026-06-12."),
    )

    assert ev.score == 0.0
    assert ev.asi["hard_fail"] == "meta_cluster_wrapper"
    assert ev.asi["quality"]["meta_cluster_wrapper"]["hits"]


def test_direct_observation_does_not_trip_meta_cluster_wrapper_gate():
    ev = score_candidate(
        _example(),
        _raw("Chainlink #614 fixed SAGA retrieval on 2026-06-12 for Muninn."),
    )

    assert ev.asi["hard_fail"] is None
    assert ev.asi["quality"]["meta_cluster_wrapper"]["hits"] == []
