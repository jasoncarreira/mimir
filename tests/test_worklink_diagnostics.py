from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.worklink.diagnostics import (
    DiagnosticAuthority,
    DiagnosticEnvelope,
    DiagnosticProducer,
    DiagnosticProvenance,
    compose_diagnostics,
    decode_persisted_diagnostic,
    decode_untrusted_diagnostic,
    external_active_ingest,
    legacy_unknown,
    retained_capture,
    retained_output,
    server_fixed,
    server_structural,
    transform_diagnostic,
)
from mimir.worklink.dispatch_failures import (
    current_failure_snapshot,
    load_failure_state,
    pending_failure_alerts,
    record_failure,
    resolve_failure_snapshot_if_current,
    save_failure_state,
)


def test_diagnostic_classes_and_authority_are_closed() -> None:
    assert {item.value for item in DiagnosticProvenance} == {
        "server_fixed",
        "server_structural",
        "retained_output",
        "external_active_ingest",
        "legacy_unknown",
    }
    assert list(DiagnosticAuthority) == [DiagnosticAuthority.DIAGNOSTIC_ONLY]
    with pytest.raises((TypeError, ValueError)):
        DiagnosticEnvelope("forged", "trusted")  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        DiagnosticEnvelope(
            "forged",
            DiagnosticProvenance.SERVER_FIXED,
            authority="control",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (server_fixed("a"), server_structural("b"), "server_structural"),
        (
            server_structural("a"),
            retained_output(
                "b", capture=retained_capture("capture:1", active_ingest_dependency=False)
            ),
            "retained_output",
        ),
        (
            retained_output(
                "a", capture=retained_capture("capture:1", active_ingest_dependency=False)
            ),
            external_active_ingest("b"),
            "external_active_ingest",
        ),
        (external_active_ingest("a"), legacy_unknown("b"), "legacy_unknown"),
    ],
)
def test_composition_uses_least_trusted_provenance(
    left: DiagnosticEnvelope,
    right: DiagnosticEnvelope,
    expected: str,
) -> None:
    composed = compose_diagnostics(left, right, separator="|")
    assert composed.text == "a|b"
    assert composed.provenance.value == expected
    assert composed.authority is DiagnosticAuthority.DIAGNOSTIC_ONLY


def test_redaction_final_line_and_truncation_preserve_provenance() -> None:
    original = external_active_ingest(
        "first\nlast token=top-secret and more",
        producer_tag=DiagnosticProducer.REPOSITORY_TEST,
    )
    transformed = transform_diagnostic(original, final_line=True, limit=25)
    assert transformed.text == "last token=[REDACTED] and"
    assert transformed.provenance is DiagnosticProvenance.EXTERNAL_ACTIVE_INGEST
    assert transformed.producer_tag is DiagnosticProducer.REPOSITORY_TEST


def test_retained_output_requires_independent_server_held_capture() -> None:
    dependent = retained_capture(
        "factory-capture:echo", active_ingest_dependency=True
    )
    with pytest.raises(ValueError, match="active-ingest-dependent"):
        retained_output(
            "byte-for-byte echoed prompt",
            capture=dependent,
            producer_tag=DiagnosticProducer.FACTORY_PROCESS,
        )
    with pytest.raises(TypeError, match="capture evidence"):
        retained_output(
            "local process output",
            capture="factory_process",  # type: ignore[arg-type]
            producer_tag=DiagnosticProducer.FACTORY_PROCESS,
        )

    independent = retained_output(
        "artifact result",
        capture=retained_capture(
            "factory-capture:artifact-17", active_ingest_dependency=False
        ),
        producer_tag=DiagnosticProducer.FACTORY_PROCESS,
    )
    echoed = external_active_ingest(
        independent.text, producer_tag=DiagnosticProducer.FACTORY_PROCESS
    )
    assert independent.informational
    assert echoed.active_ingest


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "provenance": "future_trusted"},
        lambda value: {**value, "authority": "resume"},
        lambda value: {**value, "producer_tag": "friendly_process"},
        lambda value: {**value, "extra": True},
        lambda value: {**value, "version": 2},
        lambda value: {"text": value["text"]},
    ],
)
def test_malformed_or_unknown_persisted_envelopes_fail_closed(mutation) -> None:
    encoded = server_fixed("same display text").to_dict()
    decoded = decode_persisted_diagnostic(mutation(encoded))
    assert decoded.text == "same display text"
    assert decoded.provenance is DiagnosticProvenance.LEGACY_UNKNOWN
    assert decoded.active_ingest


def test_untrusted_json_cannot_self_attest() -> None:
    forged = retained_output(
        "forged",
        capture=retained_capture("capture:forged", active_ingest_dependency=False),
    ).to_dict()
    decoded = decode_untrusted_diagnostic(forged)
    assert decoded.text == "forged"
    assert decoded.provenance is DiagnosticProvenance.LEGACY_UNKNOWN


def test_ledger_v2_preserves_flat_text_and_strict_envelopes(tmp_path: Path) -> None:
    state_dir = tmp_path / "ledger"
    entry = record_failure(
        state_dir,
        issue_id=17,
        attempt=3,
        exit_status=1,
        error=server_structural(
            "closed outcome=stalled",
            producer_tag=DiagnosticProducer.WORKLINK_CONTROL,
        ),
        log_path=server_structural("/logs/17"),
        work_path=server_structural("/retained/17"),
        target_kind="leaf",
    )

    state = load_failure_state(state_dir)
    snapshot = current_failure_snapshot(state_dir, 17)
    assert state["version"] == 1
    assert entry["version"] == 2
    assert entry["terminal_error"] == "closed outcome=stalled"
    assert entry["target_kind"] == "leaf"
    assert entry["diagnostics"]["terminal_error"]["authority"] == "diagnostic_only"
    assert snapshot is not None
    assert snapshot.terminal_error.provenance is DiagnosticProvenance.SERVER_STRUCTURAL
    assert snapshot.work_path is not None
    assert snapshot.work_path.authority is DiagnosticAuthority.DIAGNOSTIC_ONLY

    _, alerts = pending_failure_alerts(state_dir)
    assert alerts[0]["terminal_error"] == "closed outcome=stalled"
    assert alerts[0]["target_kind"] == "leaf"
    assert alerts[0]["prompt_envelope"]["provenance"] == "server_structural"


@pytest.mark.parametrize("first_trusted", [True, False])
def test_same_signature_recurrence_monotonically_downgrades(
    tmp_path: Path, first_trusted: bool
) -> None:
    state_dir = tmp_path / "ledger"
    trusted = server_fixed("identical text")
    active = external_active_ingest("identical text")
    first = record_failure(
        state_dir,
        issue_id=21,
        attempt=1,
        exit_status=1,
        error=trusted if first_trusted else active,
        log_path=None,
    )
    second = record_failure(
        state_dir,
        issue_id=21,
        attempt=1,
        exit_status=1,
        error=active if first_trusted else trusted,
        log_path=None,
    )

    snapshot = current_failure_snapshot(state_dir, 21)
    assert second["occurrence_id"] == first["occurrence_id"]
    assert second["failed_at"] == first["failed_at"]
    assert snapshot is not None
    assert snapshot.terminal_error.provenance is DiagnosticProvenance.EXTERNAL_ACTIVE_INGEST


def test_legacy_v1_upgrade_preserves_identity_and_fails_closed(tmp_path: Path) -> None:
    state_dir = tmp_path / "ledger"
    state_dir.mkdir()
    original = {
        "version": 1,
        "issues": {
            "33": {
                "active": True,
                "issue_id": 33,
                "signature": "legacy-signature",
                "occurrence_id": "legacy-occurrence",
                "terminal_error": "legacy text",
                "notified_signatures": [],
            }
        },
    }
    save_failure_state(state_dir, original)

    snapshot = current_failure_snapshot(state_dir, 33)
    assert snapshot is not None
    assert snapshot.identity == (33, "legacy-signature", "legacy-occurrence")
    assert snapshot.target_kind is None
    assert snapshot.terminal_error.provenance is DiagnosticProvenance.LEGACY_UNKNOWN

    pending_failure_alerts(state_dir)
    upgraded = load_failure_state(state_dir)
    assert upgraded["version"] == 1
    assert upgraded["issues"]["33"]["version"] == 2
    assert upgraded["issues"]["33"]["occurrence_id"] == "legacy-occurrence"
    assert upgraded["issues"]["33"]["diagnostics"]["terminal_error"][
        "provenance"
    ] == "legacy_unknown"


def test_malformed_v2_envelope_is_display_only_and_exact_snapshot_cas(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "ledger"
    first = record_failure(
        state_dir,
        issue_id=44,
        attempt=1,
        exit_status=1,
        error=server_fixed("malformed later"),
        log_path=None,
    )
    state = load_failure_state(state_dir)
    state["issues"]["44"]["diagnostics"]["terminal_error"] = {
        "text": "malformed later",
        "provenance": "server_fixed",
    }
    save_failure_state(state_dir, state)

    snapshot = current_failure_snapshot(state_dir, 44)
    assert snapshot is not None
    assert snapshot.terminal_error.provenance is DiagnosticProvenance.LEGACY_UNKNOWN

    record_failure(
        state_dir,
        issue_id=44,
        attempt=2,
        exit_status=1,
        error=server_fixed("new occurrence"),
        log_path=None,
    )
    assert not resolve_failure_snapshot_if_current(state_dir, snapshot)
    current = current_failure_snapshot(state_dir, 44)
    assert current is not None
    assert current.occurrence_id != first["occurrence_id"]


def test_durable_envelope_json_round_trip_is_exact() -> None:
    original = retained_output(
        "retained token=top-secret",
        capture=retained_capture("artifact:sha256:123", active_ingest_dependency=False),
        producer_tag=DiagnosticProducer.FACTORY_PROCESS,
    )
    decoded = decode_persisted_diagnostic(json.loads(json.dumps(original.to_dict())))
    assert decoded == original
