"""No shipped surface may claim a provider is incompatible with enforcement.

Every provider supports ``MIMIR_ACCESS_CONTROL_ENFORCED``. The ``claude-code:``
startup refusal was removed when #910 plumbed the per-turn ``AuthContext`` across
the Claude Code SDK hook boundary.

Why this is a test rather than a review habit: the claim was added in one PR and
invalidated by a sibling PR about two hours later, and it then took **three**
rounds to find every copy — a console warning, a ``.env`` template comment, two
docstrings, and finally two sentences in ``SPEC.md``. Each round was a hand-run
grep over the files someone happened to think of, and each round missed a
surface. An executable scan does not have that failure mode.

If a provider ever genuinely becomes enforcement-incompatible again, delete this
test in the same change that reintroduces the constraint.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Assertions that a provider is refused/blocked under enforcement.
_STALE_CLAIM = re.compile(
    # Deliberately broad on the verb. The five copies found so far each phrased
    # it differently — "refused at startup", "cannot be used with", "cannot be
    # combined with" — and a narrow pattern is how three of them survived a
    # hand-run grep.
    r"claude.code.{0,200}?(refus|cannot be|can't be|can not be|incompatible|not compatible|unsupported)"
    r".{0,80}?(enforc|ACCESS_CONTROL)"
    r"|(enforc|ACCESS_CONTROL).{0,200}?claude.code.{0,80}?"
    r"(refus|cannot be|can't be|can not be|incompatible|not compatible|unsupported)",
    re.IGNORECASE | re.DOTALL,
)

# Operator-facing surfaces: shipped source, the top-level docs, and the docs
# tree that pyproject force-includes into the wheel as mimir/bundled_docs/.
# ``tests/`` is excluded because tests legitimately quote the strings, and
# ``bundled_docs`` because it is a build-time copy of ``docs/``.
_SCANNED = (
    sorted((_ROOT / "mimir").rglob("*.py"))
    + sorted((_ROOT / "docs").rglob("*.md"))
    + [_ROOT / "README.md", _ROOT / "SPEC.md", _ROOT / ".env.example"]
)


def _surfaces():
    for path in _SCANNED:
        if not path.is_file():
            continue
        rel = path.relative_to(_ROOT).as_posix()
        if "bundled_docs" in rel or rel.startswith("tests/"):
            continue
        yield rel, path


def test_no_shipped_surface_claims_claude_code_blocks_enforcement():
    offenders: list[str] = []
    for rel, path in _surfaces():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        for match in _STALE_CLAIM.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel}:{line}: {match.group(0)[:110]!r}")

    assert not offenders, (
        "shipped surfaces still claim claude-code is incompatible with "
        "MIMIR_ACCESS_CONTROL_ENFORCED (removed by #910):\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_actually_covers_the_surfaces_that_regressed():
    """Guard the guard: a scan over the wrong file set passes vacuously.

    Each path listed here previously carried a copy of the stale claim, so if
    the scan stops reaching one of them this test fails rather than the suite
    going quietly green.
    """
    covered = {rel for rel, _ in _surfaces()}
    for required in (
        "mimir/commands/setup.py",       # console warning + .env template
        "mimir/config.py",               # model_spec docstring
        "mimir/model_registry.py",       # DEFAULT_MODEL_SPEC docstring
        "SPEC.md",                       # §4.2 model paragraph
        "docs/authorization.md",         # enablement runbook
        "docs/configuration.md",         # env-var reference
        "README.md",                     # authorization paragraph
    ):
        assert required in covered, f"scan no longer covers {required}"
