"""Tests for ``mimir.redaction`` — token-shaped secret masking before durable
logs. #499 closed the drift where AWS keys and JSON OAuth-token value forms
(which the sibling templates/git/pre-commit hook catches) passed through
``redact_text`` unredacted into events.jsonl.
"""

from __future__ import annotations

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


def test_existing_anthropic_and_bearer_patterns_unbroken() -> None:
    assert redact_text("sk-ant-abc123def456ghi789") == "[REDACTED]"
    assert redact_text("Authorization: Bearer abcdef123456") == (
        "Authorization: Bearer [REDACTED]"
    )


def test_benign_text_passes_through() -> None:
    text = "the quick brown fox jumps over 13 lazy dogs"
    assert redact_text(text) == text
