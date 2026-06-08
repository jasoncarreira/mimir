"""GEPA adapter for the commitments-extraction pilot (chainlink #404, Path A).

Runs a candidate **system prompt** through the same model path as
``mimir.commitments.extractor`` (saga's ``call_llm`` + ``_parse_extraction_json``),
scores the extracted commitment texts with the reference-free
:mod:`evals.commitments_extraction.metrics`, and exposes each example's ASI as
gepa reflective data.

Scope: only ``EXTRACTION_SYSTEM`` is evolved — that's where the v4
self-containment rubric lives, and keeping ``USER_TEMPLATE`` fixed means gepa
can't break its ``.format()`` placeholders.

``gepa`` is imported lazily (inside ``evaluate``), so this module and the
metrics can be exercised without the optional ``gepa`` extra installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from . import metrics

#: Committed SYNTHETIC fixture — for unit tests + offline demo. NOT the real
#: eval corpus. Real evaluation reads in-home session turns via
#: :func:`load_turns_corpus`, which are never committed (privacy: real session
#: text carries Discord content / PII / operational detail).
SYNTHETIC_CORPUS_PATH = Path(__file__).parent / "synthetic_corpus.jsonl"

#: The single component gepa evolves. seed_candidate = {COMPONENT_SYSTEM: EXTRACTION_SYSTEM}.
COMPONENT_SYSTEM = "system"


@dataclass
class Example:
    id: str
    split: str
    source_text: str
    notes: str = ""


def load_corpus(path: Path = SYNTHETIC_CORPUS_PATH, *, split: str | None = None) -> list[Example]:
    """Load the committed SYNTHETIC fixture, optionally filtered to a split.

    For tests and offline demo. The real evaluation uses
    :func:`load_turns_corpus` against the agent's in-home turn log.
    """
    out: list[Example] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        d = json.loads(raw)
        if split is not None and d.get("split") != split:
            continue
        out.append(
            Example(
                id=d["id"],
                split=d.get("split", ""),
                source_text=d["source_text"],
                notes=d.get("notes", ""),
            )
        )
    return out


# Min source length to bother extracting — mirrors the extractor's MIN_OUTPUT_LEN
# and the v4 eval recipe ("output >= 100 chars").
_MIN_SOURCE_CHARS = 100


def _holdout_bucket(turn_id: str, holdout_every: int) -> bool:
    """Deterministic, process-stable holdout assignment (no PYTHONHASHSEED
    dependence, no RNG) — same turn always lands in the same split."""
    digest = hashlib.sha1(turn_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % holdout_every == 0


def load_turns_corpus(
    home: Path,
    *,
    split: str | None = None,
    limit: int | None = None,
    holdout_every: int = 4,
) -> list[Example]:
    """Load REAL session-end syntheses from the agent's in-home turn log.

    Reads ``<home>/logs/turns.jsonl``, keeps ``trigger == "saga_session_end"``
    turns whose ``output`` is >= 100 chars (the extractor's actual input, and
    the v4 eval recipe), and returns them as examples. Splits are assigned by a
    stable hash of ``turn_id`` (~1/``holdout_every`` to holdout).

    **Privacy:** these examples contain real session content (Discord text, PII,
    operational detail). They are read at run time on the deployment and MUST
    NOT be committed to the framework repo. ``home`` is the agent's bind-mounted
    home, which is git-ignored from this repo.
    """
    turns_path = home / "logs" / "turns.jsonl"
    rows: list[dict] = []
    for raw in turns_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if d.get("trigger") != "saga_session_end":
            continue
        output = d.get("output")
        if not isinstance(output, str) or len(output) < _MIN_SOURCE_CHARS:
            continue
        rows.append(d)

    # Most-recent `limit` (turns.jsonl is chronological).
    if limit is not None and limit >= 0:
        rows = rows[-limit:]

    out: list[Example] = []
    for d in rows:
        tid = str(d.get("turn_id") or d.get("saga_session_id") or len(out))
        sp = "holdout" if _holdout_bucket(tid, holdout_every) else "train"
        if split is not None and sp != split:
            continue
        out.append(
            Example(
                id=tid,
                split=sp,
                source_text=d["output"],
                notes="real saga_session_end turn (in-home; never committed)",
            )
        )
    return out


#: extract_fn(system_prompt, source_text) -> list[commitment text strings]
ExtractFn = Callable[[str, str], Awaitable[list[str]]]


def make_extract_fn(model: str | None = None) -> ExtractFn:
    """Default extractor: render the fixed ``USER_TEMPLATE`` with the source,
    call the configured model via saga's ``call_llm`` (same path the real
    extractor uses), and return the extracted commitment texts. Lazy imports so
    this module loads without saga/model config present.
    """

    async def _extract(system_prompt: str, source_text: str) -> list[str]:
        from mimir.commitments.extractor import USER_TEMPLATE, _parse_extraction_json
        from mimir.saga._config_io import resolve_llm_config
        from mimir.saga._llm import call_llm

        user_msg = USER_TEMPLATE.format(
            channel_id="(pilot)", ts="", saga_session_id="(pilot)", output=source_text,
        )
        cfg = dict(resolve_llm_config("commitments"))
        if model:
            cfg["model"] = model.split(":", 1)[-1]
        raw = await call_llm(
            cfg, prompt=user_msg, system=system_prompt, temperature=0.0, max_tokens=2000,
        )
        parsed = _parse_extraction_json(raw) or {}
        texts: list[str] = []
        for item in parsed.get("commitments") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                texts.append(item["text"].strip())
        return texts

    return _extract


class CommitmentsAdapter:
    """gepa adapter (duck-typed: gepa only needs ``evaluate`` +
    ``make_reflective_dataset``).

    ``baseline_counts`` maps example id -> the baseline prompt's extraction
    count, anchoring the per-example volume penalty (see ``run_pilot``).
    """

    def __init__(
        self,
        examples: list[Example],
        baseline_counts: dict[str, int],
        extract_fn: ExtractFn,
    ) -> None:
        self.examples = list(examples)
        self.baseline_counts = dict(baseline_counts)
        self.extract_fn = extract_fn

    def evaluate(self, batch, candidate, capture_traces=False):
        from gepa.core.adapter import EvaluationBatch

        system_prompt = candidate.get(COMPONENT_SYSTEM, "")

        async def _run_all():
            return await asyncio.gather(
                *[self.extract_fn(system_prompt, ex.source_text) for ex in batch]
            )

        texts_per_ex = asyncio.run(_run_all())

        outputs: list = []
        scores: list[float] = []
        trajectories: list[dict] = []
        for ex, texts in zip(batch, texts_per_ex):
            ev = metrics.score_extraction(
                ex.source_text,
                texts,
                baseline_count=self.baseline_counts.get(ex.id, len(texts)),
            )
            outputs.append(texts)
            scores.append(ev.score)
            if capture_traces:
                trajectories.append(
                    {"example_id": ex.id, "source": ex.source_text, "texts": texts, "asi": ev.asi}
                )
        return EvaluationBatch(
            outputs=outputs, scores=scores, trajectories=trajectories if capture_traces else None
        )

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        trajs = eval_batch.trajectories or []
        records: dict[str, list[dict]] = {}
        for comp in components_to_update:
            records[comp] = [
                {
                    "Inputs": traj["source"],
                    "Generated Outputs": json.dumps(traj["texts"], ensure_ascii=False),
                    "Feedback": traj["asi"],
                }
                for traj in trajs
            ]
        return records
