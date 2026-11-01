"""OTLP credential carrier classification at subprocess boundaries."""

from __future__ import annotations

import pytest

from mimir.otel_credentials import filter_otlp_env, is_sensitive_otlp_env, sensitive_otlp_endpoint


@pytest.mark.parametrize("prefix", ["OTEL_EXPORTER_OTLP_", "OTEL_EXPORTER_OTLP_TRACES_", "OTEL_EXPORTER_OTLP_METRICS_", "OTEL_EXPORTER_OTLP_LOGS_"])
@pytest.mark.parametrize("suffix", ["HEADERS", "CLIENT_KEY", "CLIENT_CERTIFICATE", "CERTIFICATE"])
def test_always_sensitive_carriers(prefix: str, suffix: str) -> None:
    assert is_sensitive_otlp_env((prefix + suffix).lower(), "harmless-looking")


@pytest.mark.parametrize("value", [
    "https://user:pass@example.test/v1", "https://user%40example.test:pass@example.test",
    "https://example.test/path?token=plain", "https://example.test/path?",
    "https://example.test/path#section", "not-a-url", "ftp://example.test",
    "https://[broken", "https://example.test:bad", "https://example.test/has space",
])
def test_sensitive_endpoints(value: str) -> None:
    assert sensitive_otlp_endpoint(value)
    for name in ("MIMIR_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"):
        assert is_sensitive_otlp_env(name.lower(), value)


def test_filter_preserves_ordinary_endpoints_and_unrelated_settings() -> None:
    env = {
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://example.test/base/",
        "MIMIR_OTLP_ENDPOINT": "http://localhost:4318",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT": "5",
        "OTHER_PROVIDER_KEY": "keep-provider",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "plain-canary",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://user:plain-canary@example.test",
        "OTEL_EXPORTER_OTLP_FUTURE_HEADERS": "not-in-the-closed-set",
    }
    assert filter_otlp_env(env) == {name: value for name, value in env.items() if name not in {
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "OTEL_EXPORTER_OTLP_ENDPOINT",
    }}
    assert env["OTEL_EXPORTER_OTLP_LOGS_HEADERS"] == "plain-canary"
