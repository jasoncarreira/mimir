"""Scoring — thin wrapper around saga's existing LongMemEval judge.

saga ships an evaluator harness that pipes hypothesis JSONL files to
LongMemEval's upstream ``evaluate_qa.py`` (gpt-4o judge). The integration
runner produces hypotheses in the same shape::

    {"question_id": "qa_30__simple_user_info", "hypothesis": "blue"}

This module just exposes the JSONL writer + a pointer to saga's scoring
docs so the integration bench's score command reuses the same pipeline
as the saga-only bench. Keeps numbers comparable across both views of
the same retrieval improvement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


def write_hypotheses_jsonl(
    output_path: Path,
    records: Iterable[dict],
    *,
    append: bool = False,
) -> int:
    """Write hypothesis records, one per line. Returns the count written.

    Each record must have ``question_id`` and ``hypothesis`` keys. The
    file format matches saga's bench output so the same evaluate_qa.py
    invocation works on both.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    n = 0
    with output_path.open(mode, encoding="utf-8") as f:
        for r in records:
            assert "question_id" in r and "hypothesis" in r, (
                f"hypothesis record missing required fields: {r}"
            )
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def evaluate_command(hypotheses_path: Path, dataset_path: Path) -> str:
    """Return the shell command an operator runs to score hypotheses.

    We don't invoke the judge here — the upstream evaluator lives in
    saga's external/longmemeval and needs OpenAI credentials. This
    helper just produces the canonical command string so docs / runner
    output stay in sync.
    """
    return (
        f"cd saga/external/longmemeval/src/evaluation && "
        f"python evaluate_qa.py gpt-4o "
        f"{hypotheses_path.resolve()} "
        f"{dataset_path.resolve()}"
    )
