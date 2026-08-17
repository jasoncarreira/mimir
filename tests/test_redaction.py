"""Tests for ``mimir.redaction`` — token-shaped secret masking before durable
logs. #499 closed the drift where AWS keys and JSON OAuth-token value forms
(which the sibling templates/git/pre-commit hook catches) passed through
``redact_text`` unredacted into events.jsonl.
"""

from __future__ import annotations

import pytest

from mimir.redaction import redact_payload, redact_text


# ─── #499: AWS keys + JSON OAuth-token forms ───────────────────────────


def test_redacts_aws_access_key_id() -> None:
    assert redact_text("AKIAIOSFODNN7EXAMPLE") == "[REDACTED]"
    # STS temp keys (ASIA) too.
    assert "[REDACTED]" in redact_text("creds: ASIAABCDEFGHIJKLMNOP done")
    assert "ASIAABCDEFGHIJKLMNOP" not in redact_text("creds: ASIAABCDEFGHIJKLMNOP")


def test_redacts_aws_secret_access_key_envform() -> None:
    out = redact_text("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX")
    assert out == "AWS_SECRET_ACCESS_KEY=[REDACTED]"
    out2 = redact_text("export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in out2
    assert "[REDACTED]" in out2


def test_redacts_json_oauth_token_values() -> None:
    for key in ("access_token", "refresh_token", "client_secret"):
        payload = f'{{"{key}": "s3cr3t-value-abcdef123456"}}'
        out = redact_text(payload)
        assert "s3cr3t-value-abcdef123456" not in out
        # Key name + JSON structure preserved; only the value masked.
        assert f'"{key}": "[REDACTED]"' in out


def test_redact_payload_masks_nested_aws_key() -> None:
    payload = {"cmd": "AWS_SECRET_ACCESS_KEY=abcd1234EFGH/ijkl run",
               "note": "AKIAIOSFODNN7EXAMPLE in stderr",
               "args": ["export", "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"]}
    out = redact_payload(payload)
    assert "AKIAIOSFODNN7EXAMPLE" not in str(out)
    assert "abcd1234EFGH/ijkl" not in str(out)


# ─── existing patterns still hold (no regression) ──────────────────────


FAKE_SECRET = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("> X-API-Key: " + FAKE_SECRET, "> X-API-Key: [REDACTED]"),
        ("MIMIR_API_KEY: " + FAKE_SECRET, "MIMIR_API_KEY: [REDACTED]"),
        (
            '{"VOYAGE_API_KEY": "pa-' + FAKE_SECRET + '"}',
            '{"VOYAGE_API_KEY": "[REDACTED]"}',
        ),
        ("MIMIR_API_KEY=" + FAKE_SECRET, "MIMIR_API_KEY=[REDACTED]"),
    ],
)
def test_redacts_credential_key_value_forms(text: str, expected: str) -> None:
    assert redact_text(text) == expected


# Named corpus reviewed for false positives when broadening key/value delimiters.
FALSE_POSITIVE_CORPUS = (
    "ordinary prose: this explanation should remain readable",
    "2026-08-15 12:00:00 INFO server started: ready",
    'config = {"timeout": 30, "retries": 2}',
    "headers = {'Content-Type': 'application/json'}",
    "https://x-access-token:synthetic-secret@github.com/owner/repo",
)


@pytest.mark.parametrize("text", FALSE_POSITIVE_CORPUS)
def test_key_value_false_positive_corpus_passes_through(text: str) -> None:
    assert redact_text(text) == text


# A passphrase is a credential and contains whitespace. Stopping the value at
# the first space masks one word and persists the rest, which reads as redacted.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"password": "correct horse battery staple"}', '{"password": "[REDACTED]"}'),
        ("password: 'my secret pass phrase'", "password: '[REDACTED]'"),
        ('API_KEY: "abc def ghi"', 'API_KEY: "[REDACTED]"'),
    ],
)
def test_quoted_credential_is_masked_through_the_closing_quote(
    text: str, expected: str
) -> None:
    assert redact_text(text) == expected


# An embedded quote is part of the credential, not the end of it. Stopping at
# the first quote character masks the opening fragment and persists the rest,
# which reads as redacted. JSON and Python-repr escape with a backslash; YAML
# doubles the single quote.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"password":"correct \\"horse\\" battery staple"}',
            '{"password":"[REDACTED]"}',
        ),
        ("password: 'can''t share this'", "password: '[REDACTED]'"),
        ('{"api_key":"ends with \\\\"}', '{"api_key":"[REDACTED]"}'),
        # YAML escapes a physical line break with a backslash; the value
        # continues onto the next line and must be masked whole.
        (
            'password: "first-part\\\n  second-part"',
            'password: "[REDACTED]"',
        ),
    ],
)
def test_quoted_credential_survives_embedded_quote_escapes(
    text: str, expected: str
) -> None:
    assert redact_text(text) == expected


# A match must reach a real closing delimiter. Without that requirement an
# unterminated quote consumes the rest of the input, erasing durable context —
# a worse outcome than not matching, because the record of what happened is
# what disappears.
@pytest.mark.parametrize(
    "text",
    [
        'password: "abc\nnext: must-survive\n',
        "password: 'abc\nnext: must-survive\n",
        # Valid YAML whose value ends in a LITERAL backslash: a backslash is not
        # an escape inside a single-quoted scalar, so the quote that follows is
        # the real closing delimiter and the next line is not part of the value.
        "password: 'ends in \\'\nnext: must-survive\n",
    ],
)
def test_unterminated_or_literal_backslash_quote_does_not_eat_context(
    text: str,
) -> None:
    out = redact_text(text)

    assert "next: must-survive" in out


# A single-quoted value holding no backslash reads the same under both
# grammars, so it is masked. ``''`` is YAML's escape and is unambiguous too.
@pytest.mark.parametrize(
    ("text", "fragments"),
    [
        ("password: 'my secret pass phrase'", ["secret", "phrase"]),
        ("password: 'can''t share this'", ["share", "this"]),
        ("{'password': 'correct horse battery staple'}", ["horse", "staple"]),
    ],
)
def test_unambiguous_single_quoted_values_are_masked(
    text: str, fragments: list[str]
) -> None:
    out = redact_text(text)

    for fragment in fragments:
        assert fragment not in out
    assert "[REDACTED]" in out


# A backslash is exactly where the two grammars disagree, so the value is left
# ALONE rather than guessed at. This is an honest miss: it must never mask part
# of the credential, and must never disturb the surrounding record. Preferring
# either reading produced five successive defects, in both directions.
@pytest.mark.parametrize(
    "text",
    [
        # Python-repr: the apostrophe is escaped and the value continues.
        "password: 'has \\' and \" both'",
        "{'password': 'has \\' and \" both'}",
        # YAML: the backslash is literal and the value ends at that apostrophe.
        "password: 'ends in \\'",
        "{'password': 'ends in \\', 'next': 'must-survive'}",
        "password: 'ends in \\' # note 'quoted'",
        "password: 'ends in \\'\nnext: must-survive\n",
        # A literal backslash immediately before YAML's doubled-quote escape.
        "password: 'alpha\\''omega-tail'\nnext: must-survive\n",
    ],
)
def test_ambiguous_single_quoted_values_are_declined_untouched(text: str) -> None:
    out = redact_text(text)

    assert out == text, "an ambiguous value must be left exactly as it was"
    assert "[REDACTED]" not in out


# Key quoting and value quoting are independent grammar choices. Coupling them
# left the standard Python dict repr — a single-quoted key with a double-quoted
# value — entirely unmasked, which is among the most common shapes in a durable
# log because it is what ``repr`` produces for a mapping.
@pytest.mark.parametrize(
    "text",
    [
        '{\'password\': "correct horse battery staple"}',
        '"password": \'correct horse battery staple\'',
        '{"password": "correct horse battery staple"}',
        "{'password': 'correct horse battery staple'}",
        'password: "correct horse battery staple"',
        "password: 'correct horse battery staple'",
    ],
)
def test_key_and_value_quote_styles_are_independent(text: str) -> None:
    out = redact_text(text)

    for fragment in ("correct", "horse", "battery", "staple"):
        assert fragment not in out
    assert "[REDACTED]" in out


# The block indicator is NOT a value. Masking it would print
# ``password: [REDACTED]`` directly above an untouched body, presenting an
# unredacted credential as though it had been scrubbed. Multiline block scalars
# are out of scope here; declining the indicator keeps the miss honest.
@pytest.mark.parametrize("indicator", ["|", ">", "|-", ">-", "|+", "|2"])
def test_block_scalar_indicator_is_not_reported_as_redacted(indicator: str) -> None:
    secret = "hh" + "-secret-body"
    text = f"password: {indicator}\n  {secret}\n"

    out = redact_text(text)

    assert out == text, "the indicator must not be masked as if it were a value"
    assert "[REDACTED]" not in out


def test_existing_anthropic_and_bearer_patterns_unbroken() -> None:
    assert redact_text("sk-ant-abc123def456ghi789") == "[REDACTED]"
    assert redact_text("Authorization: Bearer abcdef123456") == (
        "Authorization: Bearer [REDACTED]"
    )


def test_benign_text_passes_through() -> None:
    text = "the quick brown fox jumps over 13 lazy dogs"
    assert redact_text(text) == text
