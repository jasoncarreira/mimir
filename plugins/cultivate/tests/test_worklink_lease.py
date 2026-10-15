"""Compatibility and security checks at the Worklink lease extraction boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import asyncio
from types import SimpleNamespace

import pytest

from mimir_cultivate.worklink import checkout, claims, continuation, merge_closure, poller_hooks
from mimir_cultivate.worklink.retained_scope import derive_retained_factory_scope
from mimir_cultivate.worklink import retained_scope
from mimir.models import RetainedFactoryScope
from mimir.worklink.dispatch_failures import dispatch_failure_state_dir


def test_checkout_capability_requires_issued_directory(tmp_path: Path) -> None:
    issued = tmp_path / "checkout"
    issued.mkdir()
    unissued_fd = os.open(issued, os.O_RDONLY)
    try:
        with pytest.raises(TypeError, match="issued by the checkout factory"):
            checkout.CheckoutAuthorization(issued, 1949, 1, unissued_fd)
    finally:
        os.close(unissued_fd)

    fd = os.open(issued, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    with checkout._mint_checkout_authorization(issued, 1949, 1, fd) as authorization:
        authorization.verify(issued)
        issued.rename(tmp_path / "old")
        issued.symlink_to(tmp_path / "old", target_is_directory=True)
        with pytest.raises(ValueError, match="does not match"):
            authorization.verify(issued)


def test_claim_reset_preserves_wire_format_and_bounded_budget() -> None:
    record = claims.ClaimRecord(1949, 1, "worker", datetime(2026, 10, 1, tzinfo=UTC))
    assert record.to_comment() == "WORKLINK_CLAIM " + json.dumps({
        "issue_id": 1949, "attempt": 1, "agent_id": "worker",
        "claimed_at": "2026-10-01T00:00:00+00:00", "heartbeat_at": None,
    }, sort_keys=True)
    comments = [record.to_comment(), claims.CLAIM_RESET_PREFIX + '{"reason":"repair"}']
    client = claims.ChainlinkClaims(agent_id="worker")
    assert client.next_attempt(comments) == 2
    assert client.attempts_used(comments) == 0
    assert claims.claim_records_from_comments(comments) == [record]


def test_shutdown_abort_forgives_claim_without_reusing_ordinal() -> None:
    claimed_at = datetime(2026, 10, 1, tzinfo=UTC)
    record = claims.ClaimRecord(1949, 1, "worker", claimed_at)
    aborted = claims.ShutdownAbortRecord(1949, 1, "worker", claimed_at, claimed_at)
    client = claims.ChainlinkClaims(agent_id="worker")
    comments = [record.to_comment(), aborted.to_comment()]
    assert client.attempts_used(comments) == 0
    assert client.next_attempt(comments) == 2


def test_continuation_delivery_key_keeps_existing_identity(tmp_path: Path) -> None:
    action = continuation.WorklinkContinuationAction(tmp_path / "key.json", "key", 1949, None, 1)
    assert action.delivery_key == "worklink-continuation:key"
    assert continuation.continuation_sidecar_path(tmp_path, "key") == (
        tmp_path / "state" / "worklink" / "continuations" / "key.json"
    )


def test_merge_closure_rejects_ambiguous_completion() -> None:
    assert merge_closure.parse_completion_reference(
        "Closes chainlink #1949.\n\nImplemented.", expected_issue_id=1949,
    ).qualifies
    assert not merge_closure.parse_completion_reference(
        "Closes chainlink #1949.\n\nStill incomplete.", expected_issue_id=1949,
    ).qualifies


def test_poller_relevance_refuses_stale_occurrence(tmp_path: Path) -> None:
    state = tmp_path / poller_hooks.STATE_FILE
    state.write_text(json.dumps({"issues": {"1949": {
        "active": True, "signature": "original", "occurrence_id": "first",
    }}}), encoding="utf-8")
    event = SimpleNamespace(
        extra={"items": [{
            "issue_id": 1949, "error_signature": "original", "failure_occurrence_id": "second",
        }]},
    )
    assert asyncio.run(poller_hooks.recovery_relevance_check(tmp_path)(event)) is False


def test_retained_scope_refuses_untrusted_service() -> None:
    result = derive_retained_factory_scope(
        SimpleNamespace(service_principal="untrusted"),
        SimpleNamespace(canonical="poller:worklink-ready-queue", trigger="poller", authority_profile="github"),
    )
    assert result.scope is None
    assert result.refusal_reason == "service is not the Worklink ready queue"


def test_retained_effect_lease_accepts_only_current_dead_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = RetainedFactoryScope(
        issue_id=1949, signature="failure", occurrence_id="occurrence",
        run_id="run", attempt=1, session="session", repository="owner/repo",
        branch="branch", sandbox=str(tmp_path / "sandbox"),
    )
    record = SimpleNamespace(**{
        key: getattr(scope, key) for key in (
            "issue_id", "run_id", "attempt", "session", "repository", "branch", "sandbox"
        )
    })

    @contextmanager
    def acquired(*_args):
        yield True

    monkeypatch.setattr(retained_scope, "factory_checkout_interlock", acquired)
    monkeypatch.setattr(retained_scope, "factory_issue_resource_lock", acquired)
    monkeypatch.setattr(retained_scope, "current_failure_record", lambda *_: {
        "signature": scope.signature, "occurrence_id": scope.occurrence_id,
    })
    monkeypatch.setattr(retained_scope, "load_factory_records_for_issue", lambda *_: [record])
    monkeypatch.setattr(retained_scope, "factory_process_is_alive", lambda _: False)
    monkeypatch.setattr(retained_scope, "factory_process_is_verified_dead", lambda _: True)
    monkeypatch.setattr(retained_scope, "_factory_session_lock_is_fresh", lambda _: False)

    with retained_scope.retained_factory_effect_lease(tmp_path, scope) as result:
        assert result.scope is scope
    monkeypatch.setattr(retained_scope, "_factory_session_lock_is_fresh", lambda _: True)
    with retained_scope.retained_factory_effect_lease(tmp_path, scope) as result:
        assert result.scope is None
        assert result.refusal_reason == "factory session lock is fresh"


def test_poller_receipt_keeps_only_live_unnotified_incident(tmp_path: Path) -> None:
    root = dispatch_failure_state_dir(tmp_path)
    root.mkdir(parents=True)
    state = {"version": 1, "issues": {"1949": {
        "issue_id": 1949, "active": True, "signature": "failure",
        "occurrence_id": "occurrence", "notified_signatures": [],
    }}}
    (root / poller_hooks.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    receipts = []
    poller_hooks.report_live_delivery_receipts(root, tmp_path, receipts.append)
    key = hashlib.sha256(b"worklink-run-failure:1949:failure:occurrence").hexdigest()
    assert receipts == [{key}]
    state["issues"]["1949"]["notified_signatures"] = ["failure"]
    (root / poller_hooks.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    poller_hooks.report_live_delivery_receipts(root, tmp_path, receipts.append)
    assert receipts[-1] == set()
