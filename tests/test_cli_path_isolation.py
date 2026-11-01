"""The default suite must not inherit host coding executables."""

from __future__ import annotations

import shutil

import pytest


@pytest.mark.parametrize("name", (
    "opencode", "codex", "factory.js", "feature-factory", "chainlink", "gh",
))
def test_host_coding_cli_is_not_on_path(name: str) -> None:
    assert shutil.which(name) is None


def test_real_opencode_admission_requires_explicit_executable() -> None:
    pytest.importorskip("mimir_cultivate")
    from mimir_cultivate.opencode import opencode_available

    assert not opencode_available()
