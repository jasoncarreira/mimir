"""Output equivalence and bounded-cost regression for live credential scanning."""

from __future__ import annotations

import itertools
import random
import re
import time

import pytest

from mimir import turn_event_redaction as redaction
from tests.redaction_corpus import SECRET_TEXT_CORPUS


# Frozen pre-#1652 grammar and substitution loop, not the optimized dispatcher.
_OLD_CREDENTIAL = re.compile(
    r"(?i)(['\"]?[A-Za-z0-9_.:-]*(?:token|api[_-]?key|secret|password|authorization)['\"]?\s*[:=]\s*)"
    r"(?:['\"][^'\"]*['\"]|[^,\s}]+)"
)


def _legacy_scrub_text(text: str) -> str:
    for index, pattern in enumerate(redaction._SECRET_PATTERNS):
        if index == 1:
            pattern = _OLD_CREDENTIAL
        replacement = r"\1[redacted]" if pattern.groups == 2 else "[redacted]"
        text = pattern.sub(replacement, text)
    return redaction._PATH_PATTERN.sub("[path]", text)


@pytest.mark.parametrize("text", SECRET_TEXT_CORPUS)
def test_existing_corpus_equivalence(text: str) -> None:
    assert redaction.scrub_text(text) == _legacy_scrub_text(text)


def test_key_value_corpus_equivalence() -> None:
    # Same key/value parity corpus as test_redaction, plus authorization and
    # Unicode IGNORECASE spellings that an ASCII literal prefilter would miss.
    for key, template, secret in itertools.product(
        ("X-API-Key", "MIMIR_API_KEY", "VOYAGE_API_KEY", "TAVILY_API_KEY",
         "password", "authorization", "toKen", "pa\u017f\u017fword", "ap\u0131key",
         "AP\u0130KEY", "to\u212aen"),
        ("> {key}: {secret}", "{key}: {secret}", '{{"{key}": "{secret}"}}',
         "{{'{key}': '{secret}'}}", "{key}={secret}"),
        ("fake-value-one", "fake-value-two"),
    ):
        text = template.format(key=key, secret=secret) + "\nother credential: ghp_fakeDecoy123"
        assert redaction.scrub_text(text) == _legacy_scrub_text(text)
        assert secret not in redaction.scrub_text(text)


def test_adversarial_equivalence() -> None:
    for prefix, key, separator, value in itertools.product(
        ("", "'", '"', "prefix.:--", "\u0130", "\u212a", " /", "token=first, "),
        ("token", "secret" * 12, "authorization", "token_not_a_key"),
        ("=", ":", "' : ", '"\n=\t', " "),
        ("", "value", "'two words'", '"mismatched\'', "'unterminated",
         "one:token=two", "'one'token=two", "one,token=two", "}"),
    ):
        text = prefix + key + separator + value
        assert redaction.scrub_text(text) == _legacy_scrub_text(text), repr(text)

    rng = random.Random(1652)
    fragments = ("token", "secret", "api-key", "authorization", "foo", "=", ":",
                 "'", '"', " ", "\n", ",", "}", "-", "\u0130", "\u017f")
    for _ in range(2000):
        text = "".join(rng.choices(fragments, k=20))
        assert redaction.scrub_text(text) == _legacy_scrub_text(text), repr(text)


def test_jwt_and_entropy_adversarial_equivalence() -> None:
    for header, payload, signature, suffix in itertools.product(
        ("eyJabcdefgh", "eyJabcdefgh-eyJabcdefgh", "xeyJabcdefgh", "\u0130-eyJabcdefgh"),
        ("abcd", "abc", "abcd-", "eyJabcdefgh"),
        ("abcdefgh", "abcdefg", "abcdefgh---", "eyJabcdefgh"),
        ("", ".eyJabcdefgh.abcd.abcdefgh", "-eyJabcdefgh.abcd.abcdefgh", "\u0130"),
    ):
        text = header + "." + payload + "." + signature + suffix
        assert redaction.scrub_text(text) == _legacy_scrub_text(text), repr(text)

    for prefix, blob, suffix in itertools.product(
        ("", "\u0130", "-", "\u0130-", "A0 "),
        ("a" * 39, "a" * 40, "a-" * 30, "A0" + "a" * 38,
         "a" * 40 + "---", "-" * 40, "a" * 40 + "/" + "a" * 40),
        ("", " A0", " A\u0660", "\nA0", "\rA0", "\u0130", "-A0"),
    ):
        text = prefix + blob + suffix
        assert redaction.scrub_text(text) == _legacy_scrub_text(text), repr(text)


@pytest.mark.parametrize("fragment", [
    "a", "0123456789abcdef", "token", "ordinary prose ", "abc_.:-",
    "a" * 40 + "-", "eyJabcdefgh-",
])
@pytest.mark.parametrize("size", [8192, 65536])
def test_long_run_cost(fragment: str, size: int) -> None:
    text = (fragment * (size // len(fragment) + 1))[:size]
    # CPU time excludes scheduling delays under loaded xdist CI. 150 ms allows
    # ample headroom for a linear 64 KiB scan, but is >10x below the reported
    # 1.62 s legacy 8 KiB case. No timing ratios or ambient process state.
    start = time.process_time()
    result = redaction.scrub_text(text)
    elapsed = time.process_time() - start
    assert result == text
    assert elapsed < 0.150, f"{size} characters took {elapsed:.3f}s CPU"
