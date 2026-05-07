"""chainlink #31 Phase 4: setup_home seeds memory/core/60-filing-rules.md.

The 60- block codifies where things go in memory/ and state/ — severity
rubric, layer-by-layer rules, two filing questions (Q1 operator-decision,
Q2 operational-gotcha), misfiling table. setup_home must seed it
alongside the other core blocks (00, 20, 30, 40, 50) so fresh agents
start with the rules instead of accumulating drift then having to
codify retroactively.
"""

from __future__ import annotations

from pathlib import Path

from mimir.cli import DEFAULT_FILING_RULES, setup_home


def test_setup_writes_filing_rules(tmp_path: Path):
    home = tmp_path / "agent"
    status = setup_home(home)

    rules = home / "memory" / "core" / "60-filing-rules.md"
    assert rules.is_file()

    body = rules.read_text()
    # Core block convention: first line is the desc comment INDEX.md reads.
    assert body.splitlines()[0].startswith("<!-- desc:")

    # Status report mentions the file when newly created.
    files = status["files_created"]
    assert "memory/core/60-filing-rules.md" in files


def test_setup_filing_rules_idempotent(tmp_path: Path):
    """Operator edits to 60-filing-rules.md (e.g., agent-specific layer
    additions) must not be clobbered on re-run. Mirrors the
    heartbeat-patterns idempotency contract."""
    home = tmp_path / "agent"
    setup_home(home)

    rules = home / "memory" / "core" / "60-filing-rules.md"
    user_body = "<!-- desc: customized -->\n# Filing Rules (operator edit)\n"
    rules.write_text(user_body)

    setup_home(home)
    assert rules.read_text() == user_body  # not clobbered


def test_default_filing_rules_starts_with_desc_comment():
    assert DEFAULT_FILING_RULES.startswith("<!-- desc:")


def test_default_filing_rules_has_loadbearing_sections():
    """Guard against accidental edits dropping the load-bearing parts:
    the severity rubric (cosmetic / drift-amplifier / system-breaking),
    the two filing questions, and the misfiling table. If any of these
    headers disappear, future-mimir reading the block won't find the
    rule that points them at the right layer."""
    body = DEFAULT_FILING_RULES
    assert "## Severity rubric" in body
    assert "**cosmetic**" in body
    assert "**drift-amplifier**" in body
    assert "**system-breaking**" in body
    assert "## Two filing questions" in body
    # Q1 and Q2 are the binary-question shortcuts; both must be present.
    assert 'Am I asking the operator to make a decision?' in body
    assert 'an operational issue I might hit' in body
    assert "## Misfiling table" in body
