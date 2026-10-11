"""Assert a checkout attestation with its refusal reason on failure."""

from __future__ import annotations

from mimir.access_control import pr_checkout_lease_attestation


def assert_lease_attested(auth: object, scope: object, lease: object) -> None:
    verdict = pr_checkout_lease_attestation(auth, scope, lease)
    assert verdict.ok, f"lease attestation refused: {verdict.reason}"
