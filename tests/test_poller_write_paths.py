"""Shipped and home-installed poller agent-write destinations."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mimir.poller_write_paths import refused_poller_write_paths
from mimir.pollers import discover_pollers


def test_shipped_poller_prompts_and_declarations_have_no_vetoed_writes(tmp_path: Path):
    root = Path(__file__).resolve().parents[1] / "mimir"
    failures = []
    for skills in (root / "skills", root / "optional-skills"):
        for manifest in sorted(skills.rglob("pollers.json")):
            for entry in json.loads(manifest.read_text(encoding="utf-8"))["pollers"]:
                for path in refused_poller_write_paths(manifest.parent, entry, tmp_path):
                    failures.append(f"{entry['name']}: {path} ({manifest})")
    assert not failures, "\n".join(failures)


def test_vetoed_prompt_and_declaration_warn_for_home_poller(
    tmp_path: Path, caplog,
):
    skill = tmp_path / "skills" / "example"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "poller.py").write_text(
        'print({"prompt": "Write draft to state/pollers/example/drafts/note.md"})\n',
        encoding="utf-8",
    )
    entry = {
        "name": "example", "command": "python3 scripts/poller.py", "cron": "* * * * *",
        "agent_write_paths": ["$STATE_DIR/notes.md"],
    }
    (skill / "pollers.json").write_text(json.dumps({"pollers": [entry]}), encoding="utf-8")
    assert refused_poller_write_paths(skill, entry, tmp_path) == (
        "state/pollers/example/drafts/note.md", "state/pollers/example/notes.md",
    )
    with caplog.at_level(logging.WARNING, logger="mimir.pollers"):
        assert len(discover_pollers(tmp_path / "skills", state_root=tmp_path / "state" / "pollers")) == 1
    assert "poller=example path=state/pollers/example/drafts/note.md" in caplog.text
    assert "poller=example path=state/pollers/example/notes.md" in caplog.text


def test_scratch_and_proposal_destinations_are_not_reported(tmp_path: Path):
    entry = {"name": "example", "agent_write_paths": ["scratch/turns/this-turn/draft.md"]}
    assert refused_poller_write_paths(tmp_path, entry, tmp_path) == ()


def test_interpolated_poller_drafts_directory_is_detected(tmp_path: Path):
    script = tmp_path / "scripts"
    script.mkdir()
    (script / "poller.py").write_text(
        'STATE_DIR = None\nDRAFTS_DIR = STATE_DIR / "drafts"\n'
        'prompt = f"Write your draft to {DRAFTS_DIR}/report.md"\n',
        encoding="utf-8",
    )
    entry = {"name": "example", "command": "python3 scripts/poller.py"}
    assert refused_poller_write_paths(tmp_path, entry, tmp_path) == (
        "state/pollers/example/drafts/report.md",
    )
