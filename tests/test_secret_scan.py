"""Tests for mimir.secret_scan (commit-time secret detection)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mimir.redaction import redact_text
from mimir.secret_scan import contains_secret


CREDENTIAL_CORPUS = [
    pytest.param("ghp_" + "a" * 36, id="github-classic-pat"),
    pytest.param("gho_" + "b" * 36, id="github-oauth"),
    pytest.param("ghu_" + "c" * 36, id="github-user-to-server"),
    pytest.param("ghs_" + "d" * 36, id="github-app-installation"),
    pytest.param("ghr_" + "e" * 36, id="github-refresh"),
    pytest.param("github_pat_" + "A" * 60, id="github-fine-grained-pat"),
    pytest.param("sk-ant-" + "A1b2" * 6, id="anthropic"),
    pytest.param("sk-proj-" + "B" * 24, id="openai-project"),
    pytest.param("sk-" + "C" * 24, id="openai-classic"),
    pytest.param("Authorization: Bearer " + "z" * 30, id="bearer"),
    pytest.param("AKIA" + "A" * 16, id="aws-access-key"),
    pytest.param("ASIA" + "0" * 16, id="aws-sts-access-key"),
    pytest.param(
        "AWS_SECRET_ACCESS_KEY=" + "aB0/" * 10,
        id="aws-secret-access-key-assignment",
    ),
    pytest.param(
        'config = {"refresh_token": "' + "x" * 30 + '"}',
        id="oauth-refresh-json",
    ),
    pytest.param(
        'config = {"access_token": "' + "y" * 30 + '"}',
        id="oauth-access-json",
    ),
    pytest.param(
        'config = {"client_secret": "' + "z" * 30 + '"}',
        id="oauth-client-secret-json",
    ),
    pytest.param("xoxb-" + "A" * 24, id="slack-bot"),
    pytest.param("xoxp-" + "B" * 24, id="slack-user"),
    pytest.param("xoxa-" + "C" * 24, id="slack-app"),
    pytest.param("xoxs-" + "D" * 24, id="slack-config"),
    pytest.param("xoxr-" + "E" * 24, id="slack-refresh"),
    pytest.param("M" * 24 + "." + "n" * 6 + "." + "o" * 27, id="discord-bot"),
]


@pytest.mark.parametrize(
    "text",
    CREDENTIAL_CORPUS,
)
def test_contains_secret_matches_high_signal_shapes(text: str) -> None:
    assert contains_secret(text) is True


@pytest.mark.parametrize("text", CREDENTIAL_CORPUS)
def test_refusal_gate_is_not_weaker_than_redactor_for_credential_corpus(
    text: str,
) -> None:
    assert redact_text(text) != text
    assert contains_secret(text) is True


@pytest.mark.parametrize("text", CREDENTIAL_CORPUS)
def test_pre_commit_template_matches_credential_corpus(
    text: str, tmp_path: Path
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sample = tmp_path / "sample.txt"
    sample.write_text(text + "\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "sample.txt"], check=True)

    hook = Path(__file__).parents[1] / "mimir" / "templates" / "git" / "pre-commit"
    result = subprocess.run(
        [str(hook)], cwd=tmp_path, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0, text
    assert "refusing to commit content" in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        # Low-signal / placeholder shapes the log redactor would flag but the
        # commit-refusal gate must NOT (they block benign generated content).
        "token=YOUR_TOKEN_HERE",
        "password=changeme",
        "api_key=<set-me>",
        "export TOKEN=$MY_TOKEN",
        # Short / non-credential lookalikes below the length floors.
        "ghp_short",
        "sk-foo-bar",  # wiki-slug shape (has hyphens; not base62 body)
        "AKIA123",  # too short
        "just some normal prose about tokens and passwords",
        "def verify_AWS_SECRET_ACCESS_KEY_assignment(value): return bool(value)",
        "VGhpcyBpcyBhIGFuIG9yZGluYXJ5IGJhc2U2NCBibG9iLg==",
        "c5fcc4810123456789abcdef0123456789abcdef",
        "123e4567-e89b-12d3-a456-426614174000",
    ],
)
def test_contains_secret_ignores_benign_corpus(text: str) -> None:
    assert contains_secret(text) is False
