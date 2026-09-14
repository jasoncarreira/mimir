"""Trusted startup prompt seeding."""

from __future__ import annotations

import pytest

from mimir import access_control, prompt_templates


def test_seed_prompts_preserves_existing_templates(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "other-home"))
    existing = home / "prompts" / "heartbeat.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("operator content\n")

    statuses = prompt_templates.seed_prompts(home)

    assert statuses[existing.name] == "present"
    assert existing.read_text() == "operator content\n"
    for name, status in statuses.items():
        if status == "created":
            assert (home / "prompts" / name).read_text() == prompt_templates.bundled_defaults()[name]
    assert not (home / ".mimir/file-integrity.json").exists()


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_seed_prompts_reports_failed_trusted_write(tmp_path, monkeypatch, error):
    def fail(home, destination, content):
        raise error("cannot record trusted bytes")

    monkeypatch.setattr(access_control, "write_framework_file", fail)
    assert set(prompt_templates.seed_prompts(tmp_path).values()) == {"skipped"}
    assert not list((tmp_path / "prompts").glob("*.md"))
