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


# The block-scalar indicator is not the value. Masking it prints a redacted
# line directly above an untouched secret, so the log reads as safe when it
# is not — the failure mode this pins is worse than a plain miss.
@pytest.mark.parametrize("indicator", ["|", ">", "|-", ">-", "|+", "|2"])
def test_yaml_block_scalar_body_is_masked_not_the_indicator(indicator: str) -> None:
    secret = "s3cr3t" + "-value-here"
    out = redact_text(f"password: {indicator}\n  {secret}\n")

    assert secret not in out
    assert "[REDACTED]" in out
    # The indicator survives so the structure stays readable.
    assert out.startswith(f"password: {indicator}")


# Blank lines are part of a block scalar, and a header may carry a comment.
# Ending the body at the first blank line masks the opening fragment and leaves
# the rest — the log then reads as scrubbed while holding half the credential.
@pytest.mark.parametrize(
    "header",
    ["password: |", "password: >-", "password: | # supplied externally"],
)
@pytest.mark.parametrize("gap", ["\n", "\n\n", "\n   \n"])
def test_yaml_block_scalar_masks_every_fragment_across_blank_lines(
    header: str, gap: str
) -> None:
    first = "aa" + "-secret-fragment"
    second = "bb" + "-secret-fragment"
    out = redact_text(f"{header}\n  {first}\n{gap}  {second}\n")

    assert first not in out
    assert second not in out


# The body may also BEGIN after blank lines. Requiring the first body line to
# be indented and non-blank misses such a scalar entirely — the block rule does
# not fire and the bare-value guard correctly declines the indicator, so the
# credential is left wholly unredacted.
@pytest.mark.parametrize("lead", ["\n", "\n\n", "   \n", "\t\n"])
def test_yaml_block_scalar_body_may_begin_after_blank_lines(lead: str) -> None:
    secret = "dd" + "-secret-fragment"
    out = redact_text(f"password: |\n{lead}  {secret}\n")

    assert secret not in out
    assert "[REDACTED]" in out


# Sweep the block-scalar grammar rather than adding one case per report: three
# rounds of review found three separate holes in this one pattern, each a legal
# YAML shape the previous fix had not considered.
@pytest.mark.parametrize(
    "header",
    [
        "password: |", "password: >", "password: |-", "password: >+",
        "password: |2", "password: | # supplied externally",
        "PASSWORD: |", '"api_key": |',
    ],
)
@pytest.mark.parametrize("lead", ["", "\n", "\n\n", "   \n"])
@pytest.mark.parametrize("gap", ["", "\n", "  \n"])
def test_yaml_block_scalar_grammar_sweep_leaks_no_fragment(
    header: str, lead: str, gap: str
) -> None:
    first = "ee" + "-frag-one"
    second = "ff" + "-frag-two"
    out = redact_text(f"{header}\n{lead}  {first}\n{gap}  {second}\n")

    assert first not in out
    assert second not in out


def test_yaml_block_scalar_leaves_the_following_key_intact() -> None:
    secret = "cc" + "-secret-fragment"
    out = redact_text(f"password: |\n  {secret}\n\nnextkey: plain\n")

    assert secret not in out
    assert out.endswith("\nnextkey: plain\n")


def test_existing_anthropic_and_bearer_patterns_unbroken() -> None:
    assert redact_text("sk-ant-abc123def456ghi789") == "[REDACTED]"
    assert redact_text("Authorization: Bearer abcdef123456") == (
        "Authorization: Bearer [REDACTED]"
    )


def test_benign_text_passes_through() -> None:
    text = "the quick brown fox jumps over 13 lazy dogs"
    assert redact_text(text) == text
