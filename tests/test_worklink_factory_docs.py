from __future__ import annotations

from pathlib import Path

from mimir.worklink.backends.feature_factory import (
    FACTORY_VERSION,
    _DEFAULT_FACTORY_MAX_RETRIES,
    _MAX_FACTORY_MAX_RETRIES,
)


ROOT = Path(__file__).resolve().parents[1]
FACTORY_DOCS = (
    "docs/code-building-pipeline.md",
    "docs/configuration.md",
    "docs/internal/WORKLINK.md",
)


def test_current_factory_documentation_matches_runtime_version() -> None:
    assert FACTORY_VERSION == "0.8.0"
    for relative_path in FACTORY_DOCS:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert text.count(f"`feature-factory@{FACTORY_VERSION}`") == 1, relative_path
        assert text.count(f"`opencode-feature-factory@{FACTORY_VERSION}`") == 1, relative_path
        assert "`feature-factory@0.7.0`" not in text, relative_path
        assert "`opencode-feature-factory@0.7.0`" not in text, relative_path


def test_current_factory_admission_claims_match_runtime_version() -> None:
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    worklink = (ROOT / "docs/internal/WORKLINK.md").read_text(encoding="utf-8")

    assert f"Package-bound feature-factory {FACTORY_VERSION} launcher" in configuration
    assert configuration.count(f"no {FACTORY_VERSION} runtime consumes it.") == 4
    assert (
        "`defaults.trusted_test_retries` is **retired**, not an operator setting in\n"
        f"{FACTORY_VERSION}."
    ) in configuration
    assert f"ship in\n{FACTORY_VERSION}." in configuration
    assert f"package/adapter {FACTORY_VERSION} verification" in worklink


def test_factory_launch_contract_is_identical_in_bounded_documentation() -> None:
    assert FACTORY_DOCS == (
        "docs/code-building-pipeline.md",
        "docs/configuration.md",
        "docs/internal/WORKLINK.md",
    )
    launch = (
        f'--command feature " --autonomous --max-retries '
        f'{_DEFAULT_FACTORY_MAX_RETRIES} <issue>"'
    )
    retry = (
        f"`MIMIR_FACTORY_MAX_RETRIES` defaults to `{_DEFAULT_FACTORY_MAX_RETRIES}`, "
        f"accepts exactly ASCII `[0-9]+` in range `1..{_MAX_FACTORY_MAX_RETRIES}`, "
        f"and falls back to `{_DEFAULT_FACTORY_MAX_RETRIES}` for absent or invalid values."
    )
    staging = (
        "feature-factory 0.7.5 stages the workflow inside the run directory; "
        "exact token `--auto` is never passed."
    )
    base = (
        "Worklink's base selects the checkout start point and PR target; it is not factory "
        "`--base`, which is never passed."
    )

    for relative_path in FACTORY_DOCS:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        assert launch in normalized, relative_path
        assert retry in normalized, relative_path
        assert staging in normalized, relative_path
        assert base in normalized, relative_path


def test_factory_identity_preflight_contract_is_identical_in_bounded_documentation() -> None:
    git_identity = (
        "Before first factory dispatch only, after checkout creation and before process launch, "
        "Worklink reads the effective checkout `git config --get user.name` and `git config "
        "--get user.email`. Both must be nonblank. The child receives `GIT_AUTHOR_NAME`, "
        "`GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL`; Worklink never "
        "writes sandbox Git identity configuration."
    )
    publication = (
        "Worklink reads the nonblank `publishing_identity` from "
        "`MIMIR_FACTORY_PUBLISHING_IDENTITY` when that variable is set, otherwise from the "
        "trusted controller checkout's `.factory.json`. A set but blank or non-string "
        "override fails instead of falling back. For GitHub publication Worklink verifies the credential "
        "this process is already bound to, `GITHUB_TOKEN`, rather than selecting among "
        "candidates: `GH_TOKEN` is a child-only alias for `gh`, and verifying a second "
        "credential in a process that already verified one is refused by the forge identity "
        "memo before `/user` is ever reached. That token's owner is compared against the "
        "selected identity before dispatch, then both child aliases are normalized to it. "
        "`GH_TOKEN` and `GITHUB_TOKEN` set to different values fail dispatch as an operator "
        "ambiguity rather than one being preferred, and a missing `GITHUB_TOKEN` fails naming "
        "that variable - in both cases without disclosing values."
    )

    for relative_path in FACTORY_DOCS:
        normalized = " ".join((ROOT / relative_path).read_text(encoding="utf-8").split())
        assert git_identity in normalized, relative_path
        assert publication in normalized, relative_path


def test_internal_factory_status_contract_matches_runtime_binding() -> None:
    worklink = (ROOT / "docs/internal/WORKLINK.md").read_text(encoding="utf-8")

    assert "additive top-level fields are ignored" in worklink
    assert (
        "`issue_key`, `pr_base`, `lock_session`, and `pr_url` are string-or-null"
        in worklink
    )
    assert "`validator` and `terminal_result` are object-or-null" in worklink
    assert "uses the run ID as identity; the issue key is optional display enrichment" in worklink
    assert "A null PR base is\n  allowed during recovery" in worklink
    assert "a populated base must always match the record" in worklink
    assert (
        "Completed\n  publication verification requires a non-null matching PR base before evidence"
        in worklink
    )
