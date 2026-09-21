"""Typed, diagnostic-only provenance for Worklink failure text.

Diagnostic provenance describes where text came from.  It never grants control
authority: identities, paths, commands, and resume arguments must be obtained
from the corresponding server-held records.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

from ..redaction import redact_text

MAX_DIAGNOSTIC_TEXT = 4000


class DiagnosticProvenance(StrEnum):
    SERVER_FIXED = "server_fixed"
    SERVER_STRUCTURAL = "server_structural"
    RETAINED_OUTPUT = "retained_output"
    EXTERNAL_ACTIVE_INGEST = "external_active_ingest"
    LEGACY_UNKNOWN = "legacy_unknown"


class DiagnosticAuthority(StrEnum):
    DIAGNOSTIC_ONLY = "diagnostic_only"


class DiagnosticProducer(StrEnum):
    """Closed audit tags.  Tags deliberately have no effect on provenance."""

    WORKLINK_CONTROL = "worklink_control"
    WORKLINK_AUTONOMY = "worklink_autonomy"
    FACTORY_PROCESS = "factory_process"
    OPENCODE_PROCESS = "opencode_process"
    GIT_PROCESS = "git_process"
    REPOSITORY_TEST = "repository_test"
    WORKER_PROCESS = "worker_process"
    CHAINLINK_PROCESS = "chainlink_process"
    FORGE = "forge"
    EVIDENCE = "evidence"
    CHECKOUT = "checkout"


_PROVENANCE_RANK = {
    DiagnosticProvenance.SERVER_FIXED: 0,
    DiagnosticProvenance.SERVER_STRUCTURAL: 1,
    DiagnosticProvenance.RETAINED_OUTPUT: 2,
    DiagnosticProvenance.EXTERNAL_ACTIVE_INGEST: 3,
    DiagnosticProvenance.LEGACY_UNKNOWN: 4,
}


@dataclass(frozen=True)
class RetainedOutputCapture:
    """Server-held evidence that retained output was independently generated.

    A producer tag, process identity, or matching bytes is intentionally not
    accepted in place of this evidence.  Callers recording a capture must state
    whether active ingest influenced it; dependent output cannot be promoted.
    """

    reference: str
    active_ingest_dependency: bool

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise ValueError("retained capture reference must be non-empty")
        if type(self.active_ingest_dependency) is not bool:
            raise ValueError("retained capture dependency must be boolean")


@dataclass(frozen=True)
class DiagnosticEnvelope:
    text: str
    provenance: DiagnosticProvenance
    authority: DiagnosticAuthority = DiagnosticAuthority.DIAGNOSTIC_ONLY
    producer_tag: DiagnosticProducer | None = None
    retained_source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("diagnostic text must be a string")
        if not isinstance(self.provenance, DiagnosticProvenance):
            raise TypeError("diagnostic provenance must be a closed class")
        if self.authority is not DiagnosticAuthority.DIAGNOSTIC_ONLY:
            raise ValueError("diagnostics may only have diagnostic_only authority")
        if self.producer_tag is not None and not isinstance(
            self.producer_tag, DiagnosticProducer
        ):
            raise TypeError("diagnostic producer must be a closed audit tag")
        if self.retained_source is not None and (
            not isinstance(self.retained_source, str)
            or not self.retained_source.strip()
        ):
            raise ValueError("retained source must be a non-empty string")
        if self.provenance is DiagnosticProvenance.RETAINED_OUTPUT:
            if self.retained_source is None:
                raise ValueError("retained output requires server-held capture evidence")
        elif self.retained_source is not None:
            raise ValueError("only retained output may carry a retained source")

    @property
    def active_ingest(self) -> bool:
        return self.provenance in {
            DiagnosticProvenance.EXTERNAL_ACTIVE_INGEST,
            DiagnosticProvenance.LEGACY_UNKNOWN,
        }

    @property
    def informational(self) -> bool:
        return not self.active_ingest

    def to_dict(self) -> dict[str, object]:
        """Return the exact durable v1 envelope shape."""
        return {
            "version": 1,
            "text": self.text,
            "provenance": self.provenance.value,
            "authority": self.authority.value,
            "producer_tag": self.producer_tag.value if self.producer_tag else None,
            "retained_source": self.retained_source,
        }


def retained_capture(
    reference: str, *, active_ingest_dependency: bool
) -> RetainedOutputCapture:
    """Record the server's dependency finding for one retained capture."""
    return RetainedOutputCapture(reference, active_ingest_dependency)


def _bounded_text(text: str, *, limit: int = MAX_DIAGNOSTIC_TEXT) -> str:
    if type(limit) is not int or limit < 0:
        raise ValueError("diagnostic text limit must be a non-negative integer")
    return redact_text(text)[:limit]


def server_fixed(
    text: str, *, producer_tag: DiagnosticProducer | None = None
) -> DiagnosticEnvelope:
    """Mint literal server-authored diagnostic text.

    This entry point is for literal/template text in server code.  Data-bearing
    values must use ``server_structural``, ``external_active_ingest``, or
    composition with their existing envelope.
    """
    return DiagnosticEnvelope(
        _bounded_text(text), DiagnosticProvenance.SERVER_FIXED,
        producer_tag=producer_tag,
    )


def server_structural(
    text: str, *, producer_tag: DiagnosticProducer | None = None
) -> DiagnosticEnvelope:
    """Mint text containing only validated closed structural values."""
    return DiagnosticEnvelope(
        _bounded_text(text), DiagnosticProvenance.SERVER_STRUCTURAL,
        producer_tag=producer_tag,
    )


def retained_output(
    text: str,
    *,
    capture: RetainedOutputCapture,
    producer_tag: DiagnosticProducer | None = None,
) -> DiagnosticEnvelope:
    """Mint eligible independently generated retained output.

    Eligibility depends only on a server-held capture record.  Process identity,
    locality, and byte equality are intentionally absent from this interface.
    """
    if not isinstance(capture, RetainedOutputCapture):
        raise TypeError("retained output requires server-held capture evidence")
    if capture.active_ingest_dependency:
        raise ValueError("active-ingest-dependent output is not retained-output eligible")
    return DiagnosticEnvelope(
        _bounded_text(text),
        DiagnosticProvenance.RETAINED_OUTPUT,
        producer_tag=producer_tag,
        retained_source=capture.reference,
    )


def external_active_ingest(
    text: str, *, producer_tag: DiagnosticProducer | None = None
) -> DiagnosticEnvelope:
    return DiagnosticEnvelope(
        _bounded_text(text), DiagnosticProvenance.EXTERNAL_ACTIVE_INGEST,
        producer_tag=producer_tag,
    )


def legacy_unknown(
    value: BaseException | str | object,
    *,
    producer_tag: DiagnosticProducer | None = None,
) -> DiagnosticEnvelope:
    if isinstance(value, BaseException):
        text = f"{type(value).__name__}: {value}"
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    return DiagnosticEnvelope(
        _bounded_text(text), DiagnosticProvenance.LEGACY_UNKNOWN,
        producer_tag=producer_tag,
    )


def normalize_diagnostic(
    value: DiagnosticEnvelope | BaseException | str,
) -> DiagnosticEnvelope:
    """Preserve typed input and fail closed for all bare legacy values."""
    if isinstance(value, DiagnosticEnvelope):
        return value
    return legacy_unknown(value)


def transform_diagnostic(
    value: DiagnosticEnvelope,
    *,
    final_line: bool = False,
    limit: int = MAX_DIAGNOSTIC_TEXT,
) -> DiagnosticEnvelope:
    """Redact/bound an envelope without changing its provenance."""
    if not isinstance(value, DiagnosticEnvelope):
        raise TypeError("diagnostic transformation requires an envelope")
    text = value.text
    if final_line:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = lines[-1] if lines else "Worklink run failed"
    return DiagnosticEnvelope(
        _bounded_text(text, limit=limit),
        value.provenance,
        authority=value.authority,
        producer_tag=value.producer_tag,
        retained_source=value.retained_source,
    )


def least_trusted_provenance(
    values: Iterable[DiagnosticEnvelope],
) -> DiagnosticProvenance:
    envelopes = tuple(values)
    if not envelopes:
        return DiagnosticProvenance.SERVER_FIXED
    return max(envelopes, key=lambda value: _PROVENANCE_RANK[value.provenance]).provenance


def compose_diagnostics(
    *values: DiagnosticEnvelope,
    separator: str = "",
) -> DiagnosticEnvelope:
    """Compose text using the least-trusted provenance of every component."""
    if not all(isinstance(value, DiagnosticEnvelope) for value in values):
        raise TypeError("diagnostic composition requires typed envelopes")
    provenance = least_trusted_provenance(values)
    producer_tags = {value.producer_tag for value in values}
    producer_tag = producer_tags.pop() if len(producer_tags) == 1 else None
    retained_sources = {
        value.retained_source for value in values if value.retained_source is not None
    }
    retained_source = None
    if provenance is DiagnosticProvenance.RETAINED_OUTPUT:
        if len(retained_sources) != 1:
            # Multiple captures cannot be represented as one attestation.  The
            # text stays usable only after an independent aggregate capture.
            provenance = DiagnosticProvenance.EXTERNAL_ACTIVE_INGEST
        else:
            retained_source = retained_sources.pop()
    return DiagnosticEnvelope(
        _bounded_text(separator.join(value.text for value in values)),
        provenance,
        producer_tag=producer_tag,
        retained_source=retained_source,
    )


def with_least_trusted_provenance(
    text: str, *values: DiagnosticEnvelope
) -> DiagnosticEnvelope:
    """Apply composition provenance while retaining an already-rendered value."""
    if not values:
        return server_fixed(text)
    composed = compose_diagnostics(*values)
    return DiagnosticEnvelope(
        _bounded_text(text),
        composed.provenance,
        producer_tag=composed.producer_tag,
        retained_source=composed.retained_source,
    )


def decode_persisted_diagnostic(value: object) -> DiagnosticEnvelope:
    """Strictly decode an envelope read from protected server persistence.

    Any legacy, extra-field, malformed, or unknown shape becomes active-ingest
    ``legacy_unknown``.  The display text is retained where it is safely
    recoverable, but no provenance claim from a malformed object survives.
    """
    fallback_text = ""
    if isinstance(value, Mapping) and isinstance(value.get("text"), str):
        fallback_text = value["text"]
    elif isinstance(value, str):
        fallback_text = value
    if not isinstance(value, Mapping) or set(value) != {
        "version", "text", "provenance", "authority", "producer_tag",
        "retained_source",
    }:
        return legacy_unknown(fallback_text)
    try:
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or type(value["text"]) is not str
        ):
            raise ValueError
        provenance = DiagnosticProvenance(value["provenance"])
        authority = DiagnosticAuthority(value["authority"])
        raw_producer = value["producer_tag"]
        producer = None if raw_producer is None else DiagnosticProducer(raw_producer)
        retained_source = value["retained_source"]
        if retained_source is not None and type(retained_source) is not str:
            raise ValueError
        return DiagnosticEnvelope(
            _bounded_text(value["text"]),
            provenance,
            authority=authority,
            producer_tag=producer,
            retained_source=retained_source,
        )
    except (KeyError, TypeError, ValueError):
        return legacy_unknown(fallback_text)


def decode_untrusted_diagnostic(value: object) -> DiagnosticEnvelope:
    """Decode poller/model/subprocess data without accepting self-attestation."""
    if isinstance(value, Mapping) and isinstance(value.get("text"), str):
        return legacy_unknown(value["text"])
    return legacy_unknown(value)
