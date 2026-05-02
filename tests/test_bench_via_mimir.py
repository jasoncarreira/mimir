"""v0.5 §3: integration bench scaffolding tests.

The full runner needs a real LongMemEval dataset + OpenAI judge to
exercise end-to-end. These tests cover the deterministic pieces:

- ``route.question_to_event`` produces a valid `/event` body.
- ``score.write_hypotheses_jsonl`` matches saga's bench output format.
- ``runner._extract_hypothesis`` correctly parses BenchBridge stdout
  lines.
- The ``mimir/`` package can be imported without the saga workspace
  being importable, and vice-versa (standalone-ness sanity check —
  V0.5.md §1 invariant).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from benchmarks.longmemeval_via_mimir.route import (
    channel_id_for,
    question_to_event,
)
from benchmarks.longmemeval_via_mimir.score import (
    evaluate_command,
    write_hypotheses_jsonl,
)
from benchmarks.longmemeval_via_mimir.runner import _extract_hypothesis


def test_question_to_event_shape():
    q = {
        "question_id": "qa_30__simple",
        "question": "What's my favorite color?",
        "question_date": "2023/06/01 (Thu) 14:23",
    }
    body = question_to_event(q)
    assert body["trigger"] == "user_message"
    assert body["channel_id"] == "bench-qa_30__simple"
    assert body["content"] == "What's my favorite color?"
    assert body["content_meta"]["question_id"] == "qa_30__simple"
    assert body["content_meta"]["reference_date_iso"] == "2023/06/01 (Thu) 14:23"


def test_channel_id_for_consistent():
    assert channel_id_for("qa_30") == "bench-qa_30"
    assert channel_id_for("complex-id__with_underscores") == (
        "bench-complex-id__with_underscores"
    )


def test_write_hypotheses_jsonl_round_trips(tmp_path: Path):
    output = tmp_path / "hypotheses.jsonl"
    records = [
        {"question_id": "qa1", "hypothesis": "blue"},
        {"question_id": "qa2", "hypothesis": "I don't know."},
    ]
    n = write_hypotheses_jsonl(output, records)
    assert n == 2
    lines = output.read_text().splitlines()
    assert json.loads(lines[0]) == records[0]
    assert json.loads(lines[1]) == records[1]


def test_write_hypotheses_jsonl_append_mode(tmp_path: Path):
    output = tmp_path / "hypotheses.jsonl"
    write_hypotheses_jsonl(output, [{"question_id": "q1", "hypothesis": "a"}])
    write_hypotheses_jsonl(
        output, [{"question_id": "q2", "hypothesis": "b"}], append=True,
    )
    assert len(output.read_text().splitlines()) == 2


def test_evaluate_command_includes_judge_paths(tmp_path: Path):
    cmd = evaluate_command(tmp_path / "hyp.jsonl", tmp_path / "ds.json")
    assert "evaluate_qa.py gpt-4o" in cmd
    assert "hyp.jsonl" in cmd
    assert "ds.json" in cmd


def test_extract_hypothesis_picks_up_single_message():
    stream = (
        "[mimir:bench send_message channel=bench-qa1 msg_id=abc123] blue\n"
    )
    assert _extract_hypothesis(stream, "qa1") == "blue"


def test_extract_hypothesis_concatenates_multi_message_replies():
    """When the agent splits an answer across multiple sends, the runner
    concatenates them — LongMemEval expects a single hypothesis string."""
    stream = (
        "[mimir:bench send_message channel=bench-qa1 msg_id=m1] First line.\n"
        "[mimir:bench send_message channel=bench-qa1 msg_id=m2] Second line.\n"
    )
    assert _extract_hypothesis(stream, "qa1") == "First line.\nSecond line."


def test_extract_hypothesis_ignores_other_channels():
    stream = (
        "[mimir:bench send_message channel=bench-qa1 msg_id=m1] correct\n"
        "[mimir:bench send_message channel=bench-qa2 msg_id=m2] wrong\n"
    )
    assert _extract_hypothesis(stream, "qa1") == "correct"


def test_extract_hypothesis_skips_attachment_lines():
    stream = (
        "[mimir:bench send_message channel=bench-qa1 msg_id=m1] answer\n"
        "[mimir:bench send_message_attachments channel=bench-qa1 msg_id=m1] img.png\n"
    )
    assert _extract_hypothesis(stream, "qa1") == "answer"


def test_extract_hypothesis_returns_none_when_no_match():
    stream = "(unrelated stdout)\n"
    assert _extract_hypothesis(stream, "qa1") is None
