"""Completeness contract for ``docs/configuration.md``.

A static (AST) scan of the mimir core runtime finds every environment variable
read, and asserts each is either documented in ``docs/configuration.md`` or on
the explicit exclusion allowlist below. This keeps the doc's "complete" claim
honest and makes it a regression: a new flag added without a doc row fails CI.

Motivation: #1046 / #1047 proposed rebuilding capabilities that already existed
behind undocumented flags. The doc closes that discoverability gap; this test
keeps it closed.

Scope: ``mimir/`` core runtime, excluding ``tests/`` and ``optional-skills/``
(standalone skill subprocesses with their own env contract — listed in the doc
for convenience but not enforced here). No import of mimir: pure AST, so it
can't be broken by import-time side effects.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIMIR = REPO / "mimir"
DOC = REPO / "docs" / "configuration.md"

# Env reads that are intentionally NOT mimir configuration flags. Each has a
# reason; ``test_allowlist_entries_are_actually_read`` keeps this list from
# rotting.
ALLOWLIST = {
    "HOME",              # OS-standard; read only as a path fallback.
    "CODEX_HOME",        # Codex CLI's own config dir; mimir reads it to locate auth.
    "CLAUDE_CONFIG_DIR",  # Claude Code CLI's own config dir.
    "CHAINLINK_BIN",     # Path override for the external chainlink CLI binary.
    "WORKLINK_RUN_BIN",  # Path override for the external worklink runner binary.
}


def _env_names_in(path: Path) -> set[str]:
    """Every string literal read as an env var in ``path`` (multi-line safe)."""
    names: set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return names

    def first_str(call: ast.Call) -> str | None:
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(
            call.args[0].value, str
        ):
            return call.args[0].value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            # Any env-reading helper: os.getenv, config _env/_env_int/...,
            # _resolve_env_int, etc. — keyed on "env" in the callee name.
            if "env" in callee.lower():
                n = first_str(node)
                if n:
                    names.add(n)
            # os.environ.get("X")
            if (
                callee == "get"
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
            ):
                n = first_str(node)
                if n:
                    names.add(n)
        # os.environ["X"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
        ):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
    return names


def _scan_core() -> set[str]:
    found: set[str] = set()
    for path in MIMIR.rglob("*.py"):
        parts = path.relative_to(MIMIR.parent).parts
        if "tests" in parts or parts[1:2] == ("optional-skills",):
            continue
        found |= _env_names_in(path)
    # Keep env-var-looking names only (uppercase with an underscore, or HOME).
    return {n for n in found if n.isupper() and ("_" in n or n == "HOME")}


def _documented_names() -> set[str]:
    """Backtick-wrapped names in the doc's TABLE ROWS (prose mentions don't count)."""
    names: set[str] = set()
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("|"):
            names |= set(re.findall(r"`([A-Z][A-Z0-9_]+)`", line))
    return names


def test_every_core_env_var_is_documented():
    missing = sorted(_scan_core() - _documented_names() - ALLOWLIST)
    assert not missing, (
        "Environment variables read by mimir core runtime but absent from "
        "docs/configuration.md. Add a table row there, or add it to ALLOWLIST "
        f"in this test if it is not a mimir config flag: {missing}"
    )


def test_allowlist_entries_are_actually_read():
    stale = sorted(ALLOWLIST - _scan_core())
    assert not stale, (
        f"ALLOWLIST entries no longer read in mimir core — remove them: {stale}"
    )
