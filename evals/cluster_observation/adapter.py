"""GEPA adapter for SAGA cluster→observation prompt optimization.

The adapter evolves only the rich consolidation prompt text. It renders a
candidate prompt against exported source clusters, calls an injected synthesis
function, and scores each raw response with :mod:`evals.cluster_observation.metrics`.

Default model execution is intentionally lazy and optional: tests can pass a
stub ``synth_fn``; live pilots can pass a real callable or use
``make_saga_synth_fn()`` to route through SAGA's configured consolidation LLM.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

from . import metrics

COMPONENT_RICH_PROMPT = "rich_prompt"
DEFAULT_CORPUS = Path("/mimir-home/state/evals/gepa-cluster-observation/mimir-saga-trace-corpus.jsonl")


@dataclass
class Example:
    id: str
    split: str
    data: dict


def load_corpus(path: Path = DEFAULT_CORPUS, *, split: str | None = None) -> list[Example]:
    """Load an exported cluster-observation JSONL corpus.

    Splits are deterministic and simple for the first pilot: every fourth
    non-blank row is holdout unless the row already carries a ``split`` field.
    """
    out: list[Example] = []
    if not path.exists():
        return out
    row_idx = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        data = json.loads(raw)
        ex_split = str(data.get("split") or ("holdout" if row_idx % 4 == 0 else "train"))
        if split is None or ex_split == split:
            out.append(Example(id=str(data.get("example_id") or f"row-{row_idx}"), split=ex_split, data=data))
        row_idx += 1
    return out


SynthFn = Callable[[str, Example], Awaitable[str]]


def render_candidate_prompt(candidate_prompt: str, example: Example) -> str:
    """Render source atoms into a GEPA-evolved prompt without ``str.format``.

    GEPA mutates the candidate text freely, so literal braces in examples or
    JSON snippets must remain inert.  Only the small set of fixed production
    placeholders is replaced, and ``{indexed_atoms}`` is mandatory so candidates
    cannot be scored against a contentless prompt.
    """

    atoms = example.data.get("source_cluster", {}).get("atoms", [])
    indexed_atoms = "\n".join(
        f"[{i + 1}] {atom.get('content', '')}"
        for i, atom in enumerate(atoms)
        if isinstance(atom, Mapping)
    )
    if "{indexed_atoms}" not in candidate_prompt:
        raise ValueError("candidate prompt must contain {indexed_atoms}")
    replacements = {
        "{n}": str(len(atoms)),
        "{indexed_atoms}": indexed_atoms,
        "{prior_block}": "",
        "{vocab_block}": "",
    }
    rendered = candidate_prompt
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered


def make_saga_synth_fn(*, llm_config: dict | None = None, max_tokens: int = 1500) -> SynthFn:
    """Build a raw-response synthesizer using SAGA's consolidation LLM path."""

    async def _synth(candidate_prompt: str, example: Example) -> str:
        from mimir.saga._config_io import resolve_llm_config
        from mimir.saga._llm import call_llm

        cfg = llm_config or resolve_llm_config("consolidation")
        prompt = render_candidate_prompt(candidate_prompt, example)
        return await call_llm(cfg, prompt=prompt, max_tokens=max_tokens, temperature=0.0, system=None)

    return _synth


class ClusterObservationAdapter:
    """Duck-typed GEPA adapter returning scores and ASI-rich traces."""

    # GEPA 0.1.x probes this concrete attribute on duck-typed adapters.
    propose_new_texts = None

    def __init__(
        self,
        examples: Sequence[Example],
        synth_fn: SynthFn,
        *,
        embedding_fn: metrics.EmbeddingFn | None = None,
    ) -> None:
        self.examples = list(examples)
        self.synth_fn = synth_fn
        self.embedding_fn = embedding_fn

    def evaluate(self, batch, candidate, capture_traces=False):
        try:
            from gepa.core.adapter import EvaluationBatch
        except Exception:  # pragma: no cover - exercised when optional gepa is absent
            class EvaluationBatch:  # minimal shape used by tests/offline smoke runs
                def __init__(self, outputs, scores, trajectories=None):
                    self.outputs = outputs
                    self.scores = scores
                    self.trajectories = trajectories

        prompt = candidate.get(COMPONENT_RICH_PROMPT, "")
        prompt_overfit = metrics.score_prompt_candidate(prompt)
        examples = [ex if isinstance(ex, Example) else self._coerce_example(ex) for ex in batch]

        async def _run_all() -> list[str]:
            hard_fail_reasons = prompt_overfit.get("gate", {}).get("hard_fail_reasons", [])
            if (
                not prompt_overfit.get("pass", True)
                and "missing_indexed_atoms_placeholder" in hard_fail_reasons
            ):
                return [
                    "OBSERVATION:\n\nTRIPLES:\nNONE\n\nCONTRADICTIONS:\nNONE\n"
                    for _ in examples
                ]
            return await asyncio.gather(*[self.synth_fn(prompt, ex) for ex in examples])

        raw_outputs = asyncio.run(_run_all())
        scores: list[float] = []
        trajectories: list[dict] = []
        for ex, raw in zip(examples, raw_outputs):
            ev = metrics.score_candidate(ex.data, raw, embedding_fn=self.embedding_fn)
            raw_score = ev.score
            prompt_gate_passed = bool(prompt_overfit.get("pass", True))
            adjusted_score = 0.0 if not prompt_gate_passed else max(
                0.0, raw_score - float(prompt_overfit["penalty"])
            )
            ev.asi.setdefault("score_breakdown", {})["raw_output_score"] = raw_score
            ev.asi["score_breakdown"]["prompt_overfit_penalty"] = prompt_overfit["penalty"]
            ev.asi["score_breakdown"]["prompt_overfit_gate_passed"] = prompt_gate_passed
            ev.asi["prompt_overfit"] = prompt_overfit
            scores.append(adjusted_score)
            if capture_traces:
                trajectories.append(
                    {
                        "example_id": ex.id,
                        "source_atom_ids": ex.data.get("source_cluster", {}).get("evidence_atom_ids", []),
                        "raw_output": raw,
                        "asi": ev.asi,
                    }
                )
        return EvaluationBatch(
            outputs=raw_outputs,
            scores=scores,
            trajectories=trajectories if capture_traces else None,
        )

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        trajs = eval_batch.trajectories or []
        records: dict[str, list[dict]] = {}
        for comp in components_to_update:
            records[comp] = [
                {
                    "Inputs": {
                        "example_id": traj["example_id"],
                        "source_atom_ids": traj.get("source_atom_ids", []),
                    },
                    "Generated Outputs": traj["raw_output"],
                    "Feedback": json.dumps(traj["asi"], ensure_ascii=False, indent=2),
                }
                for traj in trajs
            ]
        return records

    @staticmethod
    def _coerce_example(value) -> Example:
        if isinstance(value, Mapping):
            data = dict(value)
            return Example(id=str(data.get("example_id") or "example"), split=str(data.get("split") or ""), data=data)
        raise TypeError(f"expected Example or mapping, got {type(value).__name__}")
