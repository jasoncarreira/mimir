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
        assert f"feature-factory@{FACTORY_VERSION}" in text, relative_path
        assert f"opencode-feature-factory@{FACTORY_VERSION}" in text, relative_path
        assert "feature-factory@0.7.0" not in text, relative_path
        assert "opencode-feature-factory@0.7.0" not in text, relative_path


def test_current_factory_admission_claims_match_runtime_version() -> None:
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    worklink = (ROOT / "docs/internal/WORKLINK.md").read_text(encoding="utf-8")

    assert f"Package-bound feature-factory {FACTORY_VERSION} launcher" in configuration
    assert f"ship in\n{FACTORY_VERSION}." in configuration
    assert f"package/adapter {FACTORY_VERSION} verification" in worklink
