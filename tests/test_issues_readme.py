"""chainlink #31 #39: setup_home seeds memory/issues/README.md.

Phase 4 deferred this from PR #63. The directory is created by
setup_home but unseeded — anyone reading the bare directory listing
gets no signal about what belongs there. This README seeds the layer
with a one-paragraph "what goes here / what doesn't" pointer that
mirrors the canonical rule in memory/core/60-filing-rules.md.

Cosmetic severity per chainlink #39: the routing rules already live
in 60-filing-rules.md and the per-file desc-line pattern already
populates memory/INDEX.md for filed entries. The README is purely
for the human or future-mimir reading the bare directory.
"""

from __future__ import annotations

from pathlib import Path

from mimir.cli import DEFAULT_ISSUES_README, setup_home


def test_setup_writes_issues_readme(tmp_path: Path):
    home = tmp_path / "agent"
    status = setup_home(home)

    readme = home / "memory" / "issues" / "README.md"
    assert readme.is_file()

    body = readme.read_text()
    # Core convention: first line is the desc comment INDEX.md reads.
    assert body.splitlines()[0].startswith("<!-- desc:")

    # Status report mentions the file when newly created.
    files = status["files_created"]
    assert "memory/issues/README.md" in files


def test_setup_issues_readme_idempotent(tmp_path: Path):
    """Operator edits to memory/issues/README.md (e.g., agent-specific
    layer additions) must not be clobbered on re-run. Mirrors the
    filing-rules idempotency contract."""
    home = tmp_path / "agent"
    setup_home(home)

    readme = home / "memory" / "issues" / "README.md"
    user_body = "<!-- desc: customized -->\n# Issues (operator edit)\n"
    readme.write_text(user_body)

    setup_home(home)
    assert readme.read_text() == user_body  # not clobbered


def test_default_issues_readme_starts_with_desc_comment():
    assert DEFAULT_ISSUES_README.startswith("<!-- desc:")


def test_default_issues_readme_points_at_sibling_layers():
    """README's load-bearing job is to route a future-mimir who
    misfiles toward the right layer. Concept synthesis →
    state/wiki/concepts/, long-form → state/wiki/topics/,
    channel-scoped → memory/channels/. Guard against accidental
    edits dropping any of those pointers."""
    body = DEFAULT_ISSUES_README
    assert "state/wiki/concepts/" in body
    assert "state/wiki/topics/" in body
    assert "memory/channels/" in body
    # And points at the canonical rubric for the full table.
    assert "memory/core/60-filing-rules.md" in body
