from __future__ import annotations

from pathlib import Path

from mimir.worklink.backends.feature_factory import FACTORY_VERSION


ROOT = Path(__file__).resolve().parents[1]
FACTORY_DOCS = (
    "docs/code-building-pipeline.md",
    "docs/configuration.md",
    "docs/internal/WORKLINK.md",
)


def test_current_factory_documentation_matches_runtime_version() -> None:
    assert FACTORY_VERSION == "0.7.2"
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
