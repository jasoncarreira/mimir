from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.cli import main
from mimir.index import build_memory_index, build_state_index, build_wiki_index
from mimir.memory_doctor import run_doctor


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_clean_home(home: Path) -> None:
    _write(
        home / "memory" / "core" / "00-identity.md",
        "<!-- desc: identity -->\n# Identity\n" + "x" * 250,
    )
    _write(
        home / "memory" / "issues" / "one.md",
        "<!-- desc: one issue -->\n# One\nbody",
    )
    _write(home / "memory" / "learnings-pending.md", "<!-- desc: pending learnings -->\n")
    _write(home / "memory" / "INDEX.md", build_memory_index(home))


def _find(report, *, layer: str, check: str, path: str | None = None):
    matches = [
        f for f in report.findings
        if f.layer == layer and f.check == check and (path is None or f.path == path)
    ]
    assert matches
    return matches[0]


def test_missing_core_desc_header_is_reported(tmp_path: Path) -> None:
    _write(tmp_path / "memory" / "core" / "00-identity.md", "# Identity\n" + "x" * 250)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="core",
        check="desc-header",
        path="memory/core/00-identity.md",
    )
    assert finding.severity == "warning"
    assert report.severity_counts["warning"] >= 1
    core = next(s for s in report.sections if s.name == "core")
    assert core.metrics["missing_desc_headers"] == 1


def test_real_channel_over_cap_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "memory" / "channels" / "discord-1" / "notes.md", "x" * 8_300)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="channels",
        check="over-cap",
        path="memory/channels/discord-1",
    )
    assert finding.severity == "warning"
    channels = next(s for s in report.sections if s.name == "channels")
    assert channels.metrics["cap_bytes"] == 8_192
    assert channels.metrics["over_cap_directories"] == 1


def test_synthetic_channel_non_injection_note(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "memory" / "channels" / "scheduler:heartbeat" / "notes.md", "x" * 9_000)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="channels",
        check="synthetic-non-injection",
        path="memory/channels/scheduler:heartbeat",
    )
    assert finding.severity == "info"
    assert not [f for f in report.findings if f.check == "over-cap"]


def test_stale_memory_index_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "memory" / "topic.md", "<!-- desc: new topic -->\nbody")

    report = run_doctor(tmp_path)

    finding = _find(report, layer="index", check="stale", path="memory/INDEX.md")
    assert finding.severity == "warning"
    index = next(s for s in report.sections if s.name == "index")
    assert index.metrics["stale"] == 1


def test_stale_state_index_is_reported_without_rewriting(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "raw" / "one.md", "<!-- desc: one -->\nbody")
    _write(tmp_path / "state" / "INDEX.md", build_state_index(tmp_path))
    before = (tmp_path / "state" / "INDEX.md").read_text(encoding="utf-8")
    _write(tmp_path / "state" / "raw" / "two.md", "<!-- desc: two -->\nbody")

    report = run_doctor(tmp_path)

    finding = _find(report, layer="state", check="index-stale", path="state/INDEX.md")
    assert finding.severity == "warning"
    assert (tmp_path / "state" / "INDEX.md").read_text(encoding="utf-8") == before
    state = next(s for s in report.sections if s.name == "state")
    assert state.metrics["index_stale"] == 1


def test_stale_wiki_index_is_reported_without_rewriting(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "wiki" / "entities" / "alice.md", "<!-- desc: Alice -->\n[[bob]]")
    _write(tmp_path / "state" / "wiki" / "entities" / "bob.md", "<!-- desc: Bob -->\n[[alice]]")
    _write(tmp_path / "state" / "wiki" / "index.md", build_wiki_index(tmp_path))
    before = (tmp_path / "state" / "wiki" / "index.md").read_text(encoding="utf-8")
    _write(tmp_path / "state" / "wiki" / "entities" / "carol.md", "<!-- desc: Carol -->\n[[alice]]")

    report = run_doctor(tmp_path)

    finding = _find(report, layer="wiki", check="index-stale", path="state/wiki/index.md")
    assert finding.severity == "warning"
    assert (tmp_path / "state" / "wiki" / "index.md").read_text(encoding="utf-8") == before
    wiki = next(s for s in report.sections if s.name == "wiki")
    assert wiki.metrics["index_stale"] == 1


def test_wiki_orphan_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "wiki" / "concepts" / "lonely.md", "<!-- desc: Lonely -->\n# Lonely")
    _write(tmp_path / "state" / "wiki" / "index.md", build_wiki_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="wiki",
        check="orphan",
        path="state/wiki/concepts/lonely.md",
    )
    assert finding.severity == "warning"
    wiki = next(s for s in report.sections if s.name == "wiki")
    assert wiki.metrics["orphans"] == 1


def test_wiki_dangling_link_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "wiki" / "topics" / "source.md", "<!-- desc: Source -->\n[[ghost]]")
    _write(tmp_path / "state" / "wiki" / "topics" / "target.md", "<!-- desc: Target -->\n[[source]]")
    _write(tmp_path / "state" / "wiki" / "index.md", build_wiki_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="wiki",
        check="dangling-link",
        path="state/wiki/topics/source.md",
    )
    assert finding.severity == "warning"
    assert "[[ghost]]" in finding.message
    wiki = next(s for s in report.sections if s.name == "wiki")
    assert wiki.metrics["dangling_links"] == 1


def test_unexpected_top_level_state_markdown_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "state" / "voice-drafts.md", "<!-- desc: draft -->\nbody")
    _write(tmp_path / "state" / "INDEX.md", build_state_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="state",
        check="top-level-md",
        path="state/voice-drafts.md",
    )
    assert finding.severity == "warning"
    state = next(s for s in report.sections if s.name == "state")
    assert state.metrics["unexpected_top_level_md_files"] == 1


def test_overgrown_learnings_pending_is_reported(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(
        tmp_path / "memory" / "learnings-pending.md",
        "\n".join(f"- item {i}" for i in range(220)),
    )
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="learnings-pending",
        check="overgrown",
        path="memory/learnings-pending.md",
    )
    assert finding.severity == "warning"
    section = next(s for s in report.sections if s.name == "learnings-pending")
    assert section.metrics["lines"] == 220
    assert section.metrics["overgrown"] == 1


def test_duplicate_issue_note_detection(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    body = "<!-- desc: duplicate incident -->\n# Incident\nsame facts"
    _write(tmp_path / "memory" / "issues" / "a.md", body)
    _write(tmp_path / "memory" / "issues" / "b.md", body)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    report = run_doctor(tmp_path)

    finding = _find(
        report,
        layer="issues",
        check="obvious-duplicate",
        path="memory/issues/b.md",
    )
    assert finding.severity == "warning"
    issues = next(s for s in report.sections if s.name == "issues")
    assert issues.metrics["duplicate_files"] == 1


def test_json_output_is_stable(tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)
    _write(tmp_path / "memory" / "core" / "10-no-desc.md", "# Missing\n" + "x" * 250)
    _write(tmp_path / "memory" / "INDEX.md", build_memory_index(tmp_path))

    payload = run_doctor(tmp_path).to_json()

    assert list(payload) == ["status", "severity_counts", "sections", "findings"]
    assert payload["status"] == "warning"
    assert list(payload["severity_counts"]) == ["error", "warning", "info"]
    assert {"layer", "check", "severity", "path", "message", "suggestion"} == set(
        payload["findings"][0]
    )
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload


def test_memory_doctor_cli_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _seed_clean_home(tmp_path)

    main(["memory", "doctor", "--home", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["severity_counts"] == {"error": 0, "warning": 0, "info": 0}
