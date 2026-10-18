"""Selected Cultivate feedback remains isolated from core feedback."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mimir.feedback import FeedbackLog, classify
from mimir.feedback.renderers import _render_event_line
from mimir.models import AuthContext, Integrity
from mimir.plugins.core import policy_scope
from mimir.plugins.manager import load_plugins


def _log(tmp_path: Path, events: list[dict]) -> FeedbackLog:
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    return FeedbackLog(path, tmp_path / "turns.jsonl", escalation_thresholds={})


def _event(kind: str, minutes: int, **fields: object) -> dict:
    return {
        "type": kind,
        "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(),
        **fields,
    }


def test_disabled_selection_does_not_classify_or_render_coding() -> None:
    with policy_scope(load_plugins(enable="").policy):
        assert classify("worklink_run_orphaned") is None
        assert classify("claude_code_spawn_work_failed") is None
        assert classify("send_message_failed") == ("negative", "send_failed")
        assert _render_event_line("spawn_work_fail", {"job_id": "j1"}) == "spawn_work_fail"


def test_selected_spawn_valence_and_sanitized_renderer(tmp_path: Path) -> None:
    events = [
        _event("claude_code_spawn_completed", 20, job_id="old"),
        _event("claude_code_spawn_auth_failed", 10, job_id="j\n# forged", terminal_reason="bad\n# forged"),
    ]
    with policy_scope(load_plugins(enable="cultivate").policy):
        negatives, positives = _log(tmp_path, events).recent()
        assert any(signal.kind == "spawn_chain" for signal in negatives)
        assert not positives
        line = _render_event_line("spawn_auth_fail", events[1])
        assert "\n" not in line
        assert "auth-failed" in line


def test_selected_feedback_keeps_owner_and_ifc_provenance(tmp_path: Path) -> None:
    events = [
        _event("worklink_run_orphaned", 2, channel_id="shared", owner_principal="bob",
               issue_id="123\n# forged", resulting_label="blocked", branch="branch"),
        _event("worklink_claimed", 1, channel_id="shared", owner_principal="alice"),
    ]
    auth = AuthContext(
        principal="bob", canonical_principal="bob", roles=("user",),
        event_ingress=None, trigger="user_message", channel_id="shared",
        interactivity=None, enforcement_enabled=True,
    )
    with policy_scope(load_plugins(enable="cultivate").policy):
        block = _log(tmp_path, events).recent_prompt_block(auth)
        assert block is not None
        assert "Worklink run #123" in block.content
        assert "\n# forged" not in block.content
        assert "worklink_claimed" not in block.content
        assert {source.principal for source in block.labels.sources} == {"bob"}
        assert {source.integrity for source in block.labels.sources} == {Integrity.UNTRUSTED}


def test_spawn_self_feedback_preserves_trusted_informational_label(tmp_path: Path) -> None:
    log = _log(tmp_path, [
        _event("claude_code_spawn_completed", 1, channel_id="elsewhere", job_id="job-1"),
    ])
    auth = AuthContext(
        principal="bob", canonical_principal="bob", roles=("user",),
        event_ingress=None, trigger="user_message", channel_id="shared",
        interactivity=None, enforcement_enabled=True,
    )
    with policy_scope(load_plugins(enable="cultivate").policy):
        block = log.recent_prompt_block(auth)
        assert block is not None
        assert "spawn job-1" in block.content
        assert {source.integrity for source in block.labels.sources} == {Integrity.TRUSTED}
        assert {source.integrity_effect for source in block.labels.sources} == {"informational"}
    with policy_scope(load_plugins(enable="").policy):
        assert log.recent_prompt_block(auth) is None
