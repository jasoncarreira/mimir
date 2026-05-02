"""Probe → channel-message adaptation.

A LongMemEval question has shape::

    {
      "question_id": "qa_30__simple_user_info",
      "question": "What's my favorite color?",
      "question_date": "2023/06/01 (Thu) 14:23",
      "haystack_sessions": [...],
      "haystack_dates": [...],
      "haystack_session_ids": [...],
      "answer": "blue",
    }

The integration runner needs to:
1. Ingest the haystack into saga (atoms with backdated created_at).
2. Drive the question through mimir's /event endpoint as a channel
   message — the channel_id is ``bench-<question_id>`` so BenchBridge
   prefixes match.
3. Capture the agent's reply from BenchBridge's stdout/StringIO stream.
4. Score against the gold answer using saga's existing judge.

This module owns the probe → /event payload mapping. It's intentionally
small — most of the work is in `runner.py`, which orchestrates the
ingest → query → score loop.
"""

from __future__ import annotations

from typing import Any


def question_to_event(question: dict[str, Any]) -> dict[str, Any]:
    """Build the JSON body for ``POST /event`` from a LongMemEval question.

    The trigger is ``user_message`` so mimir's pre-message hook fires
    (saga query, contextual rewrite if enabled, etc.). channel_id is
    ``bench-<question_id>`` so BenchBridge handles outbound and the
    reactions log scopes per-question. Includes ``content_meta`` with
    the question's reference date so the agent can pass it into saga's
    temporal pathway via ``hybrid_retrieve(reference_date=...)`` if a
    future hook reads it.
    """
    qid = question["question_id"]
    return {
        "trigger": "user_message",
        "channel_id": f"bench-{qid}",
        "content": question["question"],
        "content_meta": {
            "question_id": qid,
            "question_date": question.get("question_date"),
            "reference_date_iso": question.get("question_date"),
        },
    }


def channel_id_for(question_id: str) -> str:
    """Stable channel-id naming. BenchBridge.prefixes = ('bench',) so
    anything starting with 'bench-' routes here."""
    return f"bench-{question_id}"
