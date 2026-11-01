"""OTLP carrier redaction at actual event and turn sinks."""

from __future__ import annotations

import json

import pytest

from mimir.event_logger import EventLogger
from mimir.models import TurnRecord
from mimir.redaction import redact_payload, redact_text
from mimir.turn_logger import TurnLogger


@pytest.mark.parametrize("name,value", [
    ("OTEL_EXPORTER_OTLP_HEADERS", "plain-header-canary"),
    ("otel_exporter_otlp_traces_client_key", "plain-key-canary"),
    ("OTEL_EXPORTER_OTLP_METRICS_CLIENT_CERTIFICATE", "plain-cert-canary"),
    ("OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE", "plain-location-canary"),
    ("MIMIR_OTLP_ENDPOINT", "not-a-url-canary"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.test/?plain-query-canary"),
    ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://user:plain-traces@example.test/v1/traces"),
    ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "https://example.test/v1/metrics?plain-metrics"),
    ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "https://example.test/v1/logs#plain-logs"),
])
def test_keyed_payload_and_text(name: str, value: str) -> None:
    assert redact_payload({name: value, "ordinary": "keep-ordinary"}) == {
        name: "[REDACTED]", "ordinary": "keep-ordinary",
    }
    assert value not in redact_text(f'{name}="{value}" and {name}: "{value}"')


def test_embedded_url_and_nonsecret_endpoint() -> None:
    text = (
        "connect https://user:plain-userinfo@example.test/v1/traces then "
        "https://example.test/v1/metrics?plain-query and "
        "https://example.test/v1/logs#plain-fragment; keep https://example.test/base/"
    )
    safe = redact_text(text)
    for secret in ("plain-userinfo", "plain-query", "plain-fragment"):
        assert secret not in safe
    assert "keep https://example.test/base/" in safe
    assert "https://user:plain-git@github.com/org/repo?view=1" in redact_text(
        "https://user:plain-git@github.com/org/repo?view=1"
    )
    assert redact_text("https://github.com/org/repo?next=/v1/traces") == (
        "https://github.com/org/repo?next=/v1/traces"
    )
    assert redact_payload({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://example.test/base/"}) == {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://example.test/base/",
    }


def test_contextual_generic_endpoint_without_blanket_url_redaction() -> None:
    text = (
        "OTEL_EXPORTER_OTLP_ENDPOINT -> https://user:plain-base@example.test/custom/base; "
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT -> https://example.test/custom?plain-base-query; "
        "unrelated https://user:plain-git@github.com/org/repo?view=1 "
        "and https://example.test/search?plain-other-query"
    )
    safe = redact_text(text)
    assert "plain-base" not in safe
    assert "plain-base-query" not in safe
    assert "https://user:plain-git@github.com/org/repo?view=1" in safe
    assert "https://example.test/search?plain-other-query" in safe


@pytest.mark.asyncio
async def test_event_and_turn_durable_sinks(tmp_path) -> None:
    payload = {
        "otel_exporter_otlp_traces_headers": "plain-header-canary",
        "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE": "plain-cert-canary",
        "MIMIR_OTLP_ENDPOINT": "https://user:plain-userinfo@example.test",
        "text": "visit https://example.test/v1/traces?plain-query; keep https://example.test/base/",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://example.test/base/",
        "nested": json.dumps({"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://user:plain-nested@example.test/base"}),
        "diagnostic": (
            "OTEL_EXPORTER_OTLP_ENDPOINT -> https://user:plain-generic@example.test/custom/base; "
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT -> https://example.test/custom?plain-generic-query; "
            "unrelated https://user:plain-git@github.com/org/repo?view=1 "
            "and https://example.test/search?plain-other-query"
        ),
    }
    events_path = tmp_path / "events.jsonl"
    await EventLogger(events_path, "session").log("otel_test", details=payload)
    turns_path = tmp_path / "turns.jsonl"
    turn = TurnRecord(
        ts="2026-05-15T12:00:00Z", turn_id="turn", session_id="session",
        saga_session_id=None, trigger="user_message", channel_id="channel",
        input=json.dumps(payload), events=[{"type": "tool_result", "content": payload}],
    )
    await TurnLogger(turns_path).write(turn)
    for path in (events_path, turns_path):
        raw = path.read_text()
        for secret in ("plain-header-canary", "plain-cert-canary", "plain-userinfo", "plain-query", "plain-nested", "plain-generic", "plain-generic-query"):
            assert secret not in raw
        persisted = json.loads(raw)
        details = persisted["details"] if path == events_path else persisted["events"][0]["content"]
        assert details["otel_exporter_otlp_traces_headers"] == "[REDACTED]"
        assert details["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://example.test/base/"
        assert "plain-nested" not in details["nested"]
        assert "https://user:plain-git@github.com/org/repo?view=1" in details["diagnostic"]
        assert "https://example.test/search?plain-other-query" in details["diagnostic"]
        if path == turns_path:
            assert "plain-generic" not in persisted["input"]


@pytest.mark.parametrize("name", [
    "MIMIR_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
])
def test_ordinary_endpoint_remains_readable(name: str) -> None:
    endpoint = "https://collector.example.test:4318/v1/traces"
    assert redact_payload({name: endpoint}) == {name: endpoint}
    assert redact_text(f'{name}="{endpoint}"') == f'{name}="{endpoint}"'
