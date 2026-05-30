"""Tests for the expanded ``_redact`` token-pattern coverage
(pre-OSS hardening, review item #8)."""

from __future__ import annotations

import pytest

from mimir.git_bootstrap import _redact


# ─── github PAT (preserved from original behavior) ───────────────────────


def test_redact_github_classic_pat():
    out = _redact("error: token ghp_abcdef0123456789ABCDEF was rejected")
    assert "ghp_" not in out
    assert "REDACTED" in out


def test_redact_github_fine_grained_pat():
    out = _redact("token github_pat_11ABCDEFG_xyz0123 returned 401")
    assert "github_pat_" not in out
    assert "REDACTED" in out


# ─── anthropic ──────────────────────────────────────────────────────────


def test_redact_anthropic_api_key():
    out = _redact("ANTHROPIC_API_KEY=sk-ant-api03-AbCdEf12_3456-789xyz_long")
    assert "sk-ant-api03-" not in out
    assert "REDACTED" in out


# ─── slack ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", ["xoxb", "xoxp", "xoxa", "xoxs", "xoxr"])
def test_redact_slack_tokens(prefix: str) -> None:
    out = _redact(f"SLACK_BOT_TOKEN={prefix}-1234-5678-AbCdEf-GhIjKlMn")
    assert prefix + "-" not in out
    assert "REDACTED" in out


# ─── openai-style ───────────────────────────────────────────────────────


def test_redact_openai_secret_key():
    """Plain ``sk-`` (not ``sk-ant-``) — OpenAI shape."""
    out = _redact("OPENAI_API_KEY=sk-proj_AbCdEfGh1234567890_ijKlMnOpQrSt")
    assert "sk-proj_" not in out
    assert "REDACTED" in out


# ─── bearer header (preserves prefix) ────────────────────────────────────


def test_redact_bearer_preserves_prefix():
    out = _redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")
    assert "Bearer [REDACTED]" in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_redact_bearer_case_insensitive():
    out = _redact("authorization: bearer abc123def456ghi789")
    assert "REDACTED" in out
    assert "abc123def456ghi789" not in out


# ─── token= / api_key= / password= value-shapes ──────────────────────────


@pytest.mark.parametrize(
    "prefix",
    ["token=", "api_key=", "api-key=", "apikey=", "password=", "passwd=", "secret="],
)
def test_redact_keyvalue_shapes(prefix: str) -> None:
    """The ``key=value`` shape preserves the key, masks the value."""
    src = f"failed: {prefix}supersecretvalue while connecting"
    out = _redact(src)
    assert "supersecretvalue" not in out
    assert prefix in out  # prefix kept
    assert "REDACTED" in out


# ─── empty / no-match passthroughs ───────────────────────────────────────


def test_redact_empty_returns_empty():
    assert _redact("") == ""


def test_redact_none_returns_none():
    """The function preserves the falsy passthrough for ``None``-ish."""
    # _redact's guard treats falsy text as a passthrough.
    assert _redact(None) is None  # type: ignore[arg-type]


def test_redact_innocent_text_unchanged():
    src = "git push to origin/main succeeded after 3 retries"
    assert _redact(src) == src


# ─── env-style dump (the canonical failure mode the review flagged) ──────


def test_redact_env_dump_with_multiple_tokens():
    """The "env | grep TOKEN" failure mode — a single output blob with
    several token shapes. All should be masked."""
    env_dump = (
        "ANTHROPIC_API_KEY=sk-ant-api03-LongValueABCDEFGhijklmn_xyz\n"
        "SLACK_BOT_TOKEN=xoxb-1234-AbCdEfGh_ijklmn\n"
        "GITHUB_TOKEN=ghp_AbCdEf0123456789klMnOpQr\n"
        "DATABASE_URL=postgres://user:secret-pw@host/db\n"
    )
    out = _redact(env_dump)
    assert "sk-ant-api03-" not in out
    assert "xoxb-1234-" not in out
    assert "ghp_AbCdEf" not in out


# ─── chainlink #237: paired redact + pre-commit hook alphabet test ──


def test_redact_and_pre_commit_hook_agree_on_openai_project_key(tmp_path):
    """chainlink #237: the OpenAI ``sk-proj_…`` shape uses an underscore.
    The redact regex catches it; pre-fix the pre-commit hook's
    ``sk-[A-Za-z0-9]{40,}`` pattern (no underscore in the alphabet)
    silently allowed it through.

    The contract is "if redact masks it, the hook must refuse the
    commit." Pair the same input against both layers.
    """
    import subprocess
    from pathlib import Path

    # Same value the existing redact test uses.
    candidate = "OPENAI_API_KEY=sk-proj_AbCdEfGh1234567890_ijKlMnOpQrSt"

    # Layer 1: redact masks the secret.
    redacted = _redact(candidate)
    assert "sk-proj_" not in redacted, (
        f"redact didn't mask sk-proj_; chainlink #237 regression: {redacted}"
    )

    # Layer 2: the pre-commit hook refuses the commit.
    # Build a tiny git repo, install the hook, stage the candidate, run.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
    )

    # Install the bundled hook.
    hook_src = (
        Path(__file__).resolve().parent.parent
        / "mimir" / "templates" / "git" / "pre-commit"
    )
    hook_dst = repo / ".git" / "hooks" / "pre-commit"
    hook_dst.write_text(hook_src.read_text())
    hook_dst.chmod(0o755)

    # Stage a file containing the candidate value on a +line.
    bad = repo / "config.txt"
    bad.write_text(candidate + "\n")
    subprocess.run(["git", "-C", str(repo), "add", "config.txt"], check=True)

    # Commit should fail (non-zero exit from the hook).
    result = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "should be refused"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"pre-commit hook accepted sk-proj_ key; chainlink #237 regression. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # The refusal message should mention the matching pattern.
    assert "sk-" in (result.stdout + result.stderr).lower() or \
        "refusing" in (result.stdout + result.stderr).lower()


def test_pre_commit_hook_still_catches_traditional_sk_ant_keys(tmp_path):
    """Sanity: the chainlink #237 alphabet change must not break the
    existing sk-ant-… catch."""
    import subprocess
    from pathlib import Path

    candidate = "ANTHROPIC_API_KEY=sk-ant-api03-LongValueABCDEFGhijklmn_xyz"

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)

    hook_src = (
        Path(__file__).resolve().parent.parent
        / "mimir" / "templates" / "git" / "pre-commit"
    )
    hook_dst = repo / ".git" / "hooks" / "pre-commit"
    hook_dst.write_text(hook_src.read_text())
    hook_dst.chmod(0o755)

    bad = repo / "config.txt"
    bad.write_text(candidate + "\n")
    subprocess.run(["git", "-C", str(repo), "add", "config.txt"], check=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "should be refused"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_pre_commit_hook_allows_wiki_slug_shaped_sk_strings(tmp_path):
    """Regression: wiki slugs like ``sk-hyphen-false-positive`` and
    ``sk-approach-to-cybernetics-1961`` must NOT trigger the hook.

    The old ``sk-[A-Za-z0-9_\\-]{20,}`` pattern included hyphens in the
    alphabet, so any 20+ char string starting with ``sk-`` was refused.
    The fixed pattern ``sk-[A-Za-z0-9]{20,}`` is base62 only — hyphens in
    the body disqualify the match.  This test pins that invariant.
    """
    import subprocess
    from pathlib import Path

    # Two real slugs that triggered the bug in the wild (2026-05-29).
    innocent_lines = (
        "see also [[sk-hyphen-false-positive]] and [[sk-approach-to-cybernetics-1961]]\n"
        "- Last completed: 2026-05-29 23:00 -- 0 stale-open-closes (sk-foo-bar-baz-qux-quux stuff)\n"
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)

    hook_src = (
        Path(__file__).resolve().parent.parent
        / "mimir" / "templates" / "git" / "pre-commit"
    )
    hook_dst = repo / ".git" / "hooks" / "pre-commit"
    hook_dst.write_text(hook_src.read_text())
    hook_dst.chmod(0o755)

    ok = repo / "notes.md"
    ok.write_text(innocent_lines)
    subprocess.run(["git", "-C", str(repo), "add", "notes.md"], check=True)
    # Commit must SUCCEED — wiki slugs are not secrets.
    subprocess.run(
        ["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True
    )
    result = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "innocent content"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"pre-commit hook falsely refused wiki-slug content (regression from "
        f"sk-[A-Za-z0-9_\\-]{{20,}} over-broad pattern). "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
