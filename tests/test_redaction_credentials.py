"""Coverage and false-positive evidence for the durable/live credential boundary."""

from __future__ import annotations

import base64
import re
import statistics
import time

import pytest

from mimir import redaction, turn_event_redaction
from mimir.shared_redaction_patterns import LOG_SECRET_PATTERNS


@pytest.mark.parametrize("key", [
    "authorization", "credential", "credentials", "key", "private_key", "PRIVATE-KEY",
])
@pytest.mark.parametrize("template", [
    "{key}: {value}", "{key}={value}", '{"{key}": "{value}"}',
    "{key}: |\n  {value}\nstatus: ready",
])
@pytest.mark.parametrize("long", [False, True])
def test_credential_keys(key: str, template: str, long: bool) -> None:
    value = "Synthetic9" * 8 if long else "test-value"
    # Replace rather than format: the JSON template has literal braces.
    text = template.replace("{key}", key).replace("{value}", value)
    expected = text.replace(value, "[REDACTED]")
    assert redaction.redact_text(text) == expected
    assert redaction.redact_payload({"output": [text]}) == {"output": [expected]}
    assert redaction.redact_payload({key: value}) == {key: "[REDACTED]"}
    assert turn_event_redaction.scrub_value({key: value}) == {key: "[redacted]"}
    if "|" not in text:
        assert value not in turn_event_redaction.scrub_text(text)


@pytest.mark.parametrize("shape", [
    "basic", "bearer", "xoxe", "glpat", "stripe", "short-openai",
    "pem", "rsa", "ec", "encrypted", "openssh", "truncated-openssh",
])
def test_new_secret_shapes(shape: str, monkeypatch: pytest.MonkeyPatch) -> None:
    body = "Synthetic9" * 8
    if shape in {"basic", "bearer"}:
        value = base64.b64encode(b"test-user:test-passphrase").decode()
        text = "Authorization: " + shape.title() + " " + value
    elif shape in {"xoxe", "glpat", "stripe", "short-openai"}:
        prefix = {"xoxe": "xoxe-", "glpat": "glpat-", "stripe": "sk_live_",
                  "short-openai": "sk-"}[shape]
        value = prefix + ("Abcd1234" if shape == "short-openai" else body)
        text = value
    else:
        label = {"pem": "", "rsa": "RSA ", "ec": "EC ",
                 "encrypted": "ENCRYPTED ", "openssh": "OPENSSH ",
                 "truncated-openssh": "OPENSSH "}[shape] + "PRIVATE KEY"
        value = body
        text = "-----BEGIN " + label + "-----\n" + body
        if shape != "truncated-openssh":
            text += "\n-----END " + label + "-----"
    assert value in text
    assert value not in redaction.redact_text(text)
    assert "[REDACTED]" in redaction.redact_text(text)
    assert value not in str(redaction.redact_payload({"output": [text]}))
    assert value not in turn_event_redaction.scrub_text(text)
    assert "[redacted]" in turn_event_redaction.scrub_text(text)

    # The size limit changes only YAML parsing, never the regex coverage.
    def no_parser(*args: object) -> None:
        pytest.fail("oversized output must use the indentation scanner")

    monkeypatch.setattr(redaction.yaml, "compose_all", no_parser)
    large = "# " + "z" * redaction.MAX_YAML_REDACTION_CHARS + "\n"
    large += "private_key: |\n  block-secret\noutput: ready\n" + text
    result = redaction.redact_text(large)
    assert value not in result
    assert "block-secret" not in result
    assert "output: ready" in result


def benign_log_rows() -> list[str]:
    return [
        "INFO image data:image/png;base64," + base64.b64encode(bytes(range(96))).decode(),
        "INFO checkout commit=" + "abcdef0123456789" * 2 + "abcdef01" + " Build 7",
        "INFO request_id=" + "550e8400" + "-e29b-41d4-a716-446655440000",
        "INFO artifact=/workspace/Build7/" + "diagnostic_archive/" * 4 + "output.json",
        "INFO server started port=8080",
        "INFO tool=shell_exec exit_code=0 duration_ms=123",
        "INFO HTTP GET /health status=200",
        "INFO retry attempt=2 delay_ms=500",
    ]


def test_entropy_false_positive_measurement() -> None:
    # Frozen SSE fallback, including its line-wide rather than blob-local
    # lookaheads. This reproducible synthetic sample is not production telemetry.
    old_entropy = re.compile(
        r"\b(?=[A-Za-z0-9_+/=-]{40,}\b)(?=.*[A-Z])(?=.*[a-z])(?=.*\d)"
        r"[A-Za-z0-9_+/=-]+\b"
    )
    rows = benign_log_rows()
    assert [i for i, row in enumerate(rows) if old_entropy.search(row)] == [0, 1, 3]
    for row in rows:
        assert redaction.redact_text(row) == row
        assert redaction.redact_payload({"output": row}) == {"output": row}
        # Live path hiding is intentional and separate from credential masking.
        assert "[redacted]" not in turn_event_redaction.scrub_text(row)


@pytest.mark.parametrize("text", [
    "monkey: banana", "hockey=puck", "key_count: 3", "dedupe_key: release-7",
    "benign-key: retained",
    "Basic authentication failed", "basic operation completed",
])
def test_key_word_boundary(text: str) -> None:
    assert redaction.redact_text(text) == text
    assert turn_event_redaction.scrub_text(text) == text


def test_benign_structured_keys() -> None:
    value = {"key_count": 3, "keyboard": "attached", "keys": ["name", "status"],
             "dedupe_key": "release-7", "benign-key": "retained"}
    assert redaction.redact_payload(value) == value
    assert turn_event_redaction.scrub_value(value) == value


@pytest.mark.parametrize("fragment", [
    "monkey", "key", "private_key", "credential", "authorization",
])
def test_repeated_key_candidates_have_bounded_cost(fragment: str) -> None:
    texts = {
        size: (fragment * (size // len(fragment) + 1))[:size]
        for size in (65536, 131072, 262144)
    }
    # Warm both paths before measuring; neither input contains YAML block
    # indicators, so doubling does not switch between parser and scanner paths.
    for text in texts.values():
        assert redaction.redact_text(text) == text

    samples: dict[int, list[float]] = {size: [] for size in texts}
    for trial in range(5):
        # Alternate order and take medians to reduce sensitivity to transient
        # runner noise. Time only redaction, not input construction or assertions.
        sizes = list(texts) if trial % 2 == 0 else list(reversed(texts))
        for size in sizes:
            start = time.process_time()
            result = redaction.redact_text(texts[size])
            elapsed = time.process_time() - start
            assert result == texts[size]
            samples[size].append(elapsed)

    t_1x = statistics.median(samples[65536])
    t_4x = statistics.median(samples[262144])
    # Across this 4x size span, linear and quadratic work grow ~4x and ~16x.
    # Their geometric midpoint (8x) tolerates up to 2x multiplicative noise in
    # either direction while still discriminating the two complexity classes.
    assert t_4x < t_1x * 8, (fragment, t_1x, t_4x)


def test_shared_pattern_registration() -> None:
    for pattern in LOG_SECRET_PATTERNS:
        assert pattern in redaction._TOKEN_PATTERNS
    assert turn_event_redaction.LOG_SECRET_PATTERNS is LOG_SECRET_PATTERNS
