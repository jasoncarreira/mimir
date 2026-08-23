"""Tests for ``mimir.redaction`` — token-shaped secret masking before durable
logs. #499 closed the drift where AWS keys and JSON OAuth-token value forms
(which the sibling templates/git/pre-commit hook catches) passed through
``redact_text`` unredacted into events.jsonl.
"""

from __future__ import annotations

import re

import pytest

from mimir.contained_execution import SensitiveMaterialScrubber
from mimir.redaction import (
    _COLON_CREDENTIAL_PATTERNS,
    _TOKEN_PATTERNS,
    _mask_block_scalar_lines,
    redact_payload,
    redact_text,
)
from mimir.turn_event_redaction import scrub_text
from tests.redaction_corpus import (
    BARE_SECRET_TEXT_CORPUS,
    FAKE_SECRET,
    SECRET_TEXT_CORPUS,
)


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


def test_durable_redaction_is_superset_of_ephemeral_redaction() -> None:
    for text in SECRET_TEXT_CORPUS:
        ephemeral_masked = scrub_text(text) != text
        durable_masked = redact_text(text) != text

        assert not ephemeral_masked or durable_masked, (
            f"ephemeral path masked {text!r}, but durable path did not"
        )


@pytest.mark.parametrize(
    ("text", "preserved_prefix"),
    [
        pytest.param(BARE_SECRET_TEXT_CORPUS[0], "", id="bare-xapp"),
        pytest.param(BARE_SECRET_TEXT_CORPUS[1], "", id="bare-tvly"),
        pytest.param(BARE_SECRET_TEXT_CORPUS[2], "", id="bare-pa"),
        pytest.param(BARE_SECRET_TEXT_CORPUS[3], "", id="bare-aiza"),
        pytest.param(
            BARE_SECRET_TEXT_CORPUS[4],
            "abcdefghijklmnopqrstuvwxyz.",
            id="bare-long-header-jwt",
        ),
    ],
)
def test_bare_secret_shapes_are_masked_by_durable_and_live_redactors(
    text: str, preserved_prefix: str
) -> None:
    durable = redact_text(text)
    live = scrub_text(text)

    assert durable != text
    assert live != text
    assert text not in durable
    assert text not in live
    assert durable.startswith(preserved_prefix)
    assert live.startswith(preserved_prefix)


def test_39_character_google_api_key_is_masked() -> None:
    google_key = BARE_SECRET_TEXT_CORPUS[3]

    assert len(google_key) == 39
    assert redact_text(google_key) == "[REDACTED]"
    assert scrub_text(google_key) == "[redacted]"


def test_shared_corpus_pins_contained_scrubber_registration_asymmetry() -> None:
    scrubber = SensitiveMaterialScrubber(home="", checkout=None)

    for text in BARE_SECRET_TEXT_CORPUS:
        assert redact_text(text) != text
        assert scrub_text(text) != text
        assert scrubber.scrub_text(text) == text

        scrubber.add_scalar(text)
        assert scrubber.scrub_text(text) != text


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


_LEGACY_COLON_PATTERNS = (
    re.compile(
        r"(?i)(?<![A-Za-z0-9_./-])"
        r"(['\"]?[A-Za-z0-9_.-]*(?:token|api[_-]?key|password|passwd|secret)"
        r"['\"]?[ \t]*:[ \t]*\")((?:\\[\s\S]|[^\"\\\n])*)(?=\")"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z0-9_./-])"
        r"(['\"]?[A-Za-z0-9_.-]*(?:token|api[_-]?key|password|passwd|secret)"
        r"['\"]?[ \t]*:[ \t]*')((?:''|[^'\\\n])*)(?=')"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z0-9_./-])"
        r"(['\"]?[A-Za-z0-9_.-]*(?:token|api[_-]?key|password|passwd|secret)"
        r"['\"]?[ \t]*:[ \t]*)"
        r"(?![#!&*]|[|>][-+0-9]*(?:\s|$))([^\s\"',&}]+)"
    ),
)


def _legacy_redact_text(text: str) -> str:
    if not text:
        return text
    out = _mask_block_scalar_lines(text)
    for pattern in _TOKEN_PATTERNS:
        if pattern is _COLON_CREDENTIAL_PATTERNS[0]:
            for legacy_pattern in _LEGACY_COLON_PATTERNS:
                out = legacy_pattern.sub(r"\1[REDACTED]", out)
        elif (
            pattern is _COLON_CREDENTIAL_PATTERNS[1]
            or pattern is _COLON_CREDENTIAL_PATTERNS[2]
        ):
            continue
        elif pattern.groups == 2:
            out = pattern.sub(r"\1[REDACTED]", out)
        else:
            out = pattern.sub("[REDACTED]", out)
    return out


# This corpus deliberately crosses the productions above rather than merely
# asserting that representative secrets disappeared. It pins byte-identical
# output against the three scans replaced by the optimized rule.
COLON_RULE_EQUIVALENCE_CORPUS = (
    "> X-API-Key: " + FAKE_SECRET,
    "MIMIR_API_KEY: " + FAKE_SECRET,
    '{"VOYAGE_API_KEY": "pa-' + FAKE_SECRET + '"}',
    "password: 'my secret pass phrase'",
    'API_KEY: "abc def ghi"',
    '{"password":"correct \\"horse\\" battery staple"}',
    "password: 'can''t share this'",
    '{"api_key":"ends with \\\\"}',
    'password: "first-part\\\n  second-part"',
    'password: "abc\nnext: must-survive\n',
    "password: 'abc\nnext: must-survive\n",
    "password: 'has \\' and \" both'",
    "{'password': 'ends in \\', 'next': 'must-survive'}",
    "password: 'alpha\\''omega-tail'\nnext: must-survive\n",
    "password: bare-value, api_key: second-value",
    '{"password": "first", "api_key": \'second\', "token": third}',
    'token:xtoken:"second-secret"',
    'prefixx"token":"second-secret"',
    "ſpassword: second-secret",
    "pa\u017f\u017fword: second-secret",
    "ap\u0131_key: second-secret",
    "\u017fecret: second-secret",
    "prefix password: # retained\n  !!str\n  |\n    secret-body\n",
    "password: |\n  secret-body\nfollowing: keep\n",
    *FALSE_POSITIVE_CORPUS,
)


@pytest.mark.parametrize("text", COLON_RULE_EQUIVALENCE_CORPUS)
def test_anchored_colon_rules_are_byte_identical_to_legacy_rules(text: str) -> None:
    assert redact_text(text) == _legacy_redact_text(text)


def test_anchored_colon_rules_preserve_two_capture_dispatch() -> None:
    assert all(
        _TOKEN_PATTERNS.count(pattern) == 1
        for pattern in _COLON_CREDENTIAL_PATTERNS
    )
    assert all(pattern.groups == 2 for pattern in _COLON_CREDENTIAL_PATTERNS)
    assert all(pattern.groups in {0, 2} for pattern in _TOKEN_PATTERNS)


@pytest.mark.parametrize(
    "text",
    ["\ud800", "before \udfff after", "password: \ud800value", 'api_key: "\ud800"'],
)
def test_redact_text_is_total_on_surrogate_input(text: str) -> None:
    assert redact_text(text) == _legacy_redact_text(text)


def _wrapped_block(
    header: str,
    secret: str,
    *,
    leading_blank: bool = False,
    internal_blank: bool = False,
    explicit_key: bool = False,
) -> str:
    lines = ["ancestor:", "  items:"]
    if explicit_key:
        lines.extend(["    - ? password", f"      : {header}"])
    else:
        lines.append(f"    - {header}")
    if leading_blank:
        lines.append("")
    lines.append(f"        {secret}-first")
    if internal_blank:
        lines.append("")
    lines.extend(
        [
            f"        {secret}-second",
            "      sibling: keep-sibling",
            "    - keep-following-item",
            "  ancestor_sibling: keep-ancestor",
        ]
    )
    return "\n".join(lines) + "\n"


BLOCK_SCALAR_PRODUCTIONS = (
    pytest.param("password: |", {}, id="plain-literal"),
    pytest.param("password: >", {}, id="plain-folded"),
    pytest.param("password: |-", {}, id="strip-chomping"),
    pytest.param("password: |+", {}, id="keep-chomping"),
    pytest.param("password: |2", {}, id="explicit-indentation"),
    pytest.param("password: | # operator note", {}, id="header-comment"),
    pytest.param("password: |", {"leading_blank": True}, id="leading-blank"),
    pytest.param("password: >", {"internal_blank": True}, id="internal-blank"),
    pytest.param('"a:password": |', {}, id="quoted-key-with-colon"),
    pytest.param("password: &value_anchor !!str |", {}, id="anchor-then-tag"),
    pytest.param("password: !!str &value_anchor >", {}, id="tag-then-anchor"),
    # Alias-plus-properties is not valid YAML, but it occurs in arbitrary log
    # text and pins the non-document scanner rather than the parser path.
    pytest.param("password: *value_alias !!str |", {}, id="alias-then-tag-fallback"),
    pytest.param("|", {"explicit_key": True}, id="explicit-mapping-key"),
    pytest.param(
        "&explicit_anchor !!str >+ # note",
        {"explicit_key": True},
        id="explicit-key-with-properties",
    ),
)


@pytest.mark.parametrize(("header", "options"), BLOCK_SCALAR_PRODUCTIONS)
def test_block_scalar_productions_mask_body_and_preserve_structure(
    header: str, options: dict[str, bool]
) -> None:
    secret = "s3cr3t" + "-value"
    text = _wrapped_block(header, secret, **options)

    out = redact_text(text)

    for fragment in (secret, secret + "-first", secret + "-second"):
        assert fragment not in out
    assert "sibling: keep-sibling" in out
    assert "- keep-following-item" in out
    assert "ancestor:" in out
    assert "items:" in out
    assert "ancestor_sibling: keep-ancestor" in out
    assert header in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize("style", ["|", ">"])
@pytest.mark.parametrize("modifier", ["", "-", "+", "2", "2-"])
@pytest.mark.parametrize("key", ["password", "service_token", '"a:api_key"'])
def test_block_scalar_grammar_sweep_masks_without_overconsuming(
    style: str, modifier: str, key: str
) -> None:
    """Cross productions instead of testing only previously reported inputs."""
    secret = "s3cr3t" + "-sweep-value"
    header = f"{key}: !!str &sweep {style}{modifier} # retained"
    text = _wrapped_block(header, secret, internal_blank=True)

    out = redact_text(text)

    assert secret not in out
    assert header in out
    for context in (
        "sibling: keep-sibling",
        "- keep-following-item",
        "ancestor:",
        "items:",
        "ancestor_sibling: keep-ancestor",
    ):
        assert context in out


def test_nested_mapping_and_sequence_boundaries_survive_redaction() -> None:
    secret = "s3cr3t" + "-nested-value"
    text = (
        "ancestor:\n"
        "  items:\n"
        "    - config:\n"
        "        layer:\n"
        "          password: |\n"
        f"            {secret}\n"
        "          sibling: keep-deep-sibling\n"
        "      outer_sibling: keep-outer-sibling\n"
        "    - - password: >\n"
        f"          {secret}\n"
        "        sibling: keep-sequence-sibling\n"
        "      - keep-next-inner-item\n"
        "    - keep-next-outer-item\n"
        "  ancestor_sibling: keep-ancestor\n"
    )

    out = redact_text(text)

    assert secret not in out
    for context in (
        "ancestor:",
        "items:",
        "config:",
        "layer:",
        "sibling: keep-deep-sibling",
        "outer_sibling: keep-outer-sibling",
        "sibling: keep-sequence-sibling",
        "- keep-next-inner-item",
        "- keep-next-outer-item",
        "ancestor_sibling: keep-ancestor",
    ):
        assert context in out


def test_log_text_scanner_associates_explicit_key_with_later_indicator() -> None:
    secret = "s3cr3t" + "-explicit-log-value"
    yaml_fragment = _wrapped_block("!!str |", secret, explicit_key=True)
    text = "subprocess output follows (not YAML)\n" + yaml_fragment

    out = redact_text(text)

    assert secret not in out
    for context in (
        "subprocess output follows (not YAML)",
        "? password",
        ": !!str |",
        "sibling: keep-sibling",
        "- keep-following-item",
        "ancestor:",
        "ancestor_sibling: keep-ancestor",
    ):
        assert context in out


@pytest.mark.parametrize("log_prefix", ["", "subprocess output (not YAML)\n"])
def test_block_indicator_and_properties_may_start_on_later_lines(
    log_prefix: str,
) -> None:
    secret = "s3cr3t" + "-multiline-node-value"
    text = log_prefix + (
        "ancestor:\n"
        "  password: !!str\n"
        "    &value_anchor\n"
        "    | # retained\n"
        f"      {secret}\n"
        "  sibling: keep-sibling\n"
        "following: keep-following\n"
    )

    out = redact_text(text)

    assert secret not in out
    assert "password: !!str" in out
    assert "&value_anchor" in out
    assert "| # retained" in out
    assert "password: [REDACTED]" not in out
    assert "sibling: keep-sibling" in out
    assert "following: keep-following" in out
    assert "ancestor:" in out


@pytest.mark.parametrize(
    "fragment",
    [
        "password: # retained\n  !!str\n  |\n    {secret}\n",
        "? password\n: # retained\n  !!str\n  &value_anchor\n  >-\n    {secret}\n",
    ],
    ids=["implicit-comment", "explicit-comment-and-properties"],
)
def test_fallback_tracks_deferred_value_after_separator_comment(fragment: str) -> None:
    secret = "s3cr3t" + "-deferred-value"
    text = (
        "subprocess output (not YAML)\n"
        "ancestor:\n"
        + fragment.format(secret=secret)
        + "sibling: keep-sibling\n"
        "- keep-following-item\n"
        "ancestor_sibling: keep-ancestor\n"
    )

    out = redact_text(text)

    assert secret not in out
    assert "# retained" in out
    assert "password: [REDACTED]" not in out
    assert "sibling: keep-sibling" in out
    assert "- keep-following-item" in out
    assert "ancestor:" in out
    assert "ancestor_sibling: keep-ancestor" in out


def test_trailing_blank_line_and_following_key_keep_their_lines() -> None:
    secret = "s3cr3t" + "-trailing-value"
    text = f"password: |\n  {secret}\n\nfollowing: own-line\n"

    out = redact_text(text)

    assert secret not in out
    assert out == "password: |\n  [REDACTED]\n\nfollowing: own-line\n"


def test_block_scalar_redaction_preserves_crlf() -> None:
    secret = "s3cr3t" + "-crlf-value"
    text = _wrapped_block("password: >+ # note", secret, internal_blank=True)
    text = text.replace("\n", "\r\n")

    out = redact_text(text)

    assert secret not in out
    assert "\r\n" in out
    assert out.replace("\r\n", "").find("\n") == -1
    assert "sibling: keep-sibling\r\n" in out
    assert "- keep-following-item\r\n" in out
    assert "ancestor_sibling: keep-ancestor\r\n" in out


@pytest.mark.parametrize("indicator", ["|", ">", "|-", ">-", "|+", "|2"])
def test_block_scalar_indicator_is_not_reported_as_redacted(indicator: str) -> None:
    secret = "hh" + "-secret-body"
    text = f"password: {indicator}\n  {secret}\n"

    out = redact_text(text)

    assert secret not in out
    assert f"password: {indicator}" in out
    assert f"password: [REDACTED]" not in out
    assert "  [REDACTED]" in out


@pytest.mark.parametrize(
    "text",
    [
        "prose uses | as a separator and > as an arrow",
        "comparison: x > y | fallback",
        "password: | is discussed inline, not followed by an indented body",
    ],
)
def test_prose_containing_block_indicator_characters_is_untouched(text: str) -> None:
    assert redact_text(text) == text


def test_existing_anthropic_and_bearer_patterns_unbroken() -> None:
    assert redact_text("sk-ant-abc123def456ghi789") == "[REDACTED]"
    assert redact_text("Authorization: Bearer abcdef123456") == (
        "Authorization: Bearer [REDACTED]"
    )


def test_benign_text_passes_through() -> None:
    text = "the quick brown fox jumps over 13 lazy dogs"
    assert redact_text(text) == text
