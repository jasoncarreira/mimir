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
    active_failure_identities,
    autonomous_dispatch_block_reason,
    current_failure_snapshot,
    load_failure_state,
    mark_failure_notified,
    pending_failure_alerts,
    record_failure,
    record_success,
    resolve_failure_if_current,
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
        lambda value: {**value, "version": True},
        lambda value: {**value, "version": 1.0},
        lambda value: {**value, "version": "1"},
        lambda value: {**value, "version": None},
        lambda value: {"text": value["text"]},
    ],
)
def test_malformed_or_unknown_persisted_envelopes_fail_closed(mutation) -> None:
    encoded = server_fixed("same display text").to_dict()
    decoded = decode_persisted_diagnostic(mutation(encoded))
    assert decoded.text == "same display text"
    assert decoded.provenance is DiagnosticProvenance.LEGACY_UNKNOWN
    assert decoded.active_ingest


@pytest.mark.parametrize(
    ("raw_text", "canonical_text"),
    [
        ("token=top-secret", "token=[REDACTED]"),
        ("x" * 4001, "x" * 4000),
    ],
)
def test_noncanonical_persisted_envelope_text_cannot_retain_trust(
    raw_text: str, canonical_text: str
) -> None:
    encoded = server_fixed("canonical").to_dict()
    encoded["text"] = raw_text

    decoded = decode_persisted_diagnostic(encoded)

    assert decoded.text == canonical_text
    assert decoded.provenance is DiagnosticProvenance.LEGACY_UNKNOWN


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


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 2])
def test_ledger_version_requires_exact_integer_one(
    tmp_path: Path, version: object
) -> None:
    state_dir = tmp_path / "ledger"
    state_dir.mkdir()
    save_failure_state(state_dir, {"version": version, "issues": {}})

    with pytest.raises(ValueError, match="unsupported ledger version"):
        current_failure_snapshot(state_dir, 1)
    with pytest.raises(OSError, match="unsupported ledger version"):
        pending_failure_alerts(state_dir)


@pytest.mark.parametrize("version", [True, 2.0, "2", None])
def test_incident_version_lookalikes_cannot_attest_diagnostics(
    tmp_path: Path, version: object
) -> None:
    state_dir = tmp_path / "ledger"
    record_failure(
        state_dir,
        issue_id=18,
        attempt=1,
        exit_status=1,
        error=server_fixed("trusted text"),
        log_path=None,
    )
    state = load_failure_state(state_dir)
    state["issues"]["18"]["version"] = version
    save_failure_state(state_dir, state)

    snapshot = current_failure_snapshot(state_dir, 18)
    assert snapshot is not None
    assert snapshot.terminal_error.text == "trusted text"
    assert snapshot.terminal_error.provenance is DiagnosticProvenance.LEGACY_UNKNOWN


@pytest.mark.parametrize(
    "field",
    [
        "terminal_error",
        "preservation_error",
        "log_path",
        "preserved_ref",
        "run_id",
        "work_path",
        "transcript_path",
    ],
)
@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "null",
        "malformed",
        "unknown",
        "mismatch",
        "redaction_equivalent",
        "bound_equivalent",
    ],
)
def test_every_flat_diagnostic_is_reconciled_with_its_envelope(
    tmp_path: Path, field: str, defect: str
) -> None:
    state_dir = tmp_path / f"ledger-{field}-{defect}"
    record_failure(
        state_dir,
        issue_id=19,
        attempt=1,
        exit_status=1,
        error=server_structural("terminal"),
        log_path=server_structural("log"),
        preserved_ref=server_structural("ref"),
        preservation_error=server_structural("preservation"),
        run_id=server_structural("run"),
        work_path=server_structural("work"),
        transcript_path=server_structural("transcript"),
        target_kind="factory",
    )
    state = load_failure_state(state_dir)
    entry = state["issues"]["19"]
    envelope = entry["diagnostics"][field]
    if defect == "missing":
        del entry["diagnostics"][field]
    elif defect == "null":
        entry["diagnostics"][field] = None
    elif defect == "malformed":
        entry["diagnostics"][field] = {"text": entry[field]}
    elif defect == "unknown":
        entry["diagnostics"][field] = {**envelope, "provenance": "future_trusted"}
    elif defect == "mismatch":
        entry["diagnostics"][field] = {**envelope, "text": "different text"}
    elif defect == "redaction_equivalent":
        entry[field] = "token=[REDACTED]"
        entry["diagnostics"][field] = {**envelope, "text": "token=top-secret"}
    else:
        entry[field] = "x" * 4000
        entry["diagnostics"][field] = {**envelope, "text": "x" * 4001}
    save_failure_state(state_dir, state)

    snapshot = current_failure_snapshot(state_dir, 19)
    assert snapshot is not None
    reconciled = getattr(snapshot, field)
    assert reconciled is not None
    assert reconciled.text == entry[field]
    assert reconciled.provenance is DiagnosticProvenance.LEGACY_UNKNOWN

    _, alerts = pending_failure_alerts(state_dir)
    assert alerts[0]["diagnostic_envelopes"][field]["provenance"] == "legacy_unknown"
    assert alerts[0]["prompt_envelope"]["provenance"] == "legacy_unknown"


@pytest.mark.parametrize(
    "field",
    [
        "terminal_error",
        "preservation_error",
        "log_path",
        "preserved_ref",
        "run_id",
        "work_path",
        "transcript_path",
    ],
)
@pytest.mark.parametrize("defect", ["missing", "null", "malformed"])
def test_invalid_flat_diagnostic_cannot_retain_envelope_trust(
    tmp_path: Path, field: str, defect: str
) -> None:
    state_dir = tmp_path / f"ledger-flat-{field}-{defect}"
    record_failure(
        state_dir,
        issue_id=20,
        attempt=1,
        exit_status=1,
        error=server_fixed("terminal"),
        log_path=server_fixed("log"),
        preserved_ref=server_fixed("ref"),
        preservation_error=server_fixed("preservation"),
        run_id=server_fixed("run"),
        work_path=server_fixed("work"),
        transcript_path=server_fixed("transcript"),
        target_kind="factory",
    )
    state = load_failure_state(state_dir)
    entry = state["issues"]["20"]
    if defect == "missing":
        del entry[field]
    elif defect == "null":
        entry[field] = None
    else:
        entry[field] = ["malformed"]
    save_failure_state(state_dir, state)

    snapshot = current_failure_snapshot(state_dir, 20)
    assert snapshot is not None
    reconciled = getattr(snapshot, field)
    assert reconciled is not None
    assert reconciled.provenance is DiagnosticProvenance.LEGACY_UNKNOWN
    _, alerts = pending_failure_alerts(state_dir)
    assert alerts[0]["diagnostic_envelopes"][field]["provenance"] == "legacy_unknown"
    assert alerts[0]["prompt_envelope"]["provenance"] == "legacy_unknown"


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


@pytest.mark.parametrize(
    ("map_key", "embedded_issue_id", "requested_issue_id"),
    [
        ("41", 42, 41),
        ("042", 42, 42),
        ("0", 0, 41),
        ("-1", -1, 41),
        ("true", True, 41),
    ],
)
def test_cross_identity_incident_records_fail_closed_on_every_read_and_write(
    tmp_path: Path,
    map_key: str,
    embedded_issue_id: object,
    requested_issue_id: int,
) -> None:
    state_dir = tmp_path / "ledger"
    entry = record_failure(
        state_dir,
        issue_id=41,
        attempt=1,
        exit_status=1,
        error=server_fixed("failure"),
        log_path=None,
    )
    state = load_failure_state(state_dir)
    malformed = state["issues"].pop("41")
    malformed["issue_id"] = embedded_issue_id
    state["issues"][map_key] = malformed
    save_failure_state(state_dir, state)

    with pytest.raises(ValueError, match="invalid issue identity"):
        current_failure_snapshot(state_dir, requested_issue_id)
    with pytest.raises(ValueError, match="invalid issue identity"):
        active_failure_identities(state_dir, limit=10)
    with pytest.raises(OSError, match="invalid issue identity"):
        pending_failure_alerts(state_dir)
    assert "invalid issue identity" in (
        autonomous_dispatch_block_reason(state_dir, requested_issue_id) or ""
    )
    with pytest.raises(OSError, match="invalid issue identity"):
        mark_failure_notified(
            state_dir, requested_issue_id, entry["signature"], entry["occurrence_id"]
        )
    with pytest.raises(OSError, match="invalid issue identity"):
        record_success(state_dir, requested_issue_id)
    with pytest.raises(OSError, match="invalid issue identity"):
        resolve_failure_if_current(
            state_dir, requested_issue_id, entry["signature"], entry["occurrence_id"]
        )
    assert load_failure_state(state_dir)["issues"][map_key]["active"] is True


@pytest.mark.parametrize("issue_id", [True, 0, -1, 41.0, "41"])
def test_requested_and_resolution_issue_identity_must_be_a_positive_integer(
    tmp_path: Path, issue_id: object
) -> None:
    state_dir = tmp_path / "ledger"
    entry = record_failure(
        state_dir,
        issue_id=41,
        attempt=1,
        exit_status=1,
        error=server_fixed("failure"),
        log_path=None,
    )

    with pytest.raises(ValueError, match="invalid issue identity"):
        current_failure_snapshot(state_dir, issue_id)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid issue identity"):
        resolve_failure_if_current(
            state_dir,
            issue_id,  # type: ignore[arg-type]
            entry["signature"],
            entry["occurrence_id"],
        )
    assert current_failure_snapshot(state_dir, 41) is not None


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
