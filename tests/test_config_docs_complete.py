"""Completeness contract for ``docs/configuration.md``.

A static (AST) scan of the mimir core runtime finds every environment variable
read, and asserts each is either documented in ``docs/configuration.md`` or on
the explicit exclusion allowlist below. This keeps the doc's "complete" claim
honest and makes it a regression: a new flag added without a doc row fails CI.

Motivation: #1046 / #1047 proposed rebuilding capabilities that already existed
behind undocumented flags. The doc closes that discoverability gap; this test
keeps it closed.

What the scanner discovers (the enforced surface):

- ``os.environ.get("X")`` / ``os.environ["X"]`` / ``os.getenv("X")``;
- any helper whose name contains ``env`` (``_env``, ``_env_int``,
  ``_resolve_env_int``, …) called with the name as first argument;
- **indirect reads via a module-level string constant** — e.g.
  ``_WEBHOOK_ENV = "MIMIR_WATCHDOG_WEBHOOK_URL"`` then
  ``os.environ.get(_WEBHOOK_ENV)``. The constant is resolved to its value.

Out of scope (documented boundary): env names computed at runtime from
non-constant values — a function parameter, an imported constant from another
module, an f-string, or a name pulled from config/skill specs. Those are
plumbing (`_env(name)` inside the helper itself, credential/skill-spec readers),
not operator config flags, and are not part of the "complete" guarantee.

Scope: ``mimir/`` core runtime, excluding ``tests/`` and ``optional-skills/``
(standalone skill subprocesses with their own env contract). No import of
mimir: pure AST, so it can't be broken by import-time side effects.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIMIR = REPO / "mimir"
DOC = REPO / "docs" / "configuration.md"

# Env reads that are intentionally NOT operator configuration flags. Each has a
# reason; ``test_allowlist_entries_are_actually_read`` keeps this list honest.
ALLOWLIST = {
    "HOME",               # OS-standard; read only as a path fallback.
    "CODEX_HOME",         # Codex CLI's own config dir; read to locate auth.
    "CLAUDE_CONFIG_DIR",  # Claude Code CLI's own config dir.
    "CHAINLINK_BIN",      # Path override for the external chainlink CLI binary.
    "WORKLINK_RUN_BIN",   # Path override for the external worklink runner.
    "MIMIR_SPAWN_DEPTH",  # Harness-set on child processes to track spawn depth;
                          # read to enforce the recursion cap, not operator-set.
}

_ENV_ACCESSOR_HINT = "env"  # substring identifying env-reading helper callees


def _module_str_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` (and annotated) string constants."""
    consts: dict[str, str] = {}
    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for t in targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = value.value
    return consts


def _resolve_arg(arg: ast.expr, consts: dict[str, str]) -> str | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)  # module-level string constant, else None
    return None


def _env_names_from_source(src: str) -> set[str]:
    """Every env-var name read in ``src`` (literal or module-constant-backed)."""
    names: set[str] = set()
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return names
    consts = _module_str_constants(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            is_env_helper = _ENV_ACCESSOR_HINT in callee.lower()
            is_environ_get = (
                callee == "get"
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
            )
            if (is_env_helper or is_environ_get) and node.args:
                n = _resolve_arg(node.args[0], consts)
                if n:
                    names.add(n)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
        ):
            n = _resolve_arg(node.slice, consts)
            if n:
                names.add(n)
    return names


def _env_names_in(path: Path) -> set[str]:
    try:
        return _env_names_from_source(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return set()


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
    """Backtick names in the doc's TABLE ROWS (prose mentions don't count)."""
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


def test_scanner_resolves_constant_backed_indirect_reads():
    """Regression: an undocumented env read via a module-level constant is
    DISCOVERED (would fail the completeness test), not silently skipped."""
    src = (
        "import os\n"
        '_FAKE_ENV = "MIMIR_UNDOCUMENTED_FIXTURE_XYZ"\n'
        "def f():\n"
        "    return os.environ.get(_FAKE_ENV, '')\n"
    )
    assert "MIMIR_UNDOCUMENTED_FIXTURE_XYZ" in _env_names_from_source(src)
    # ...and via an env-named helper with a constant arg:
    src2 = (
        "OTHER = \"MIMIR_UNDOCUMENTED_FIXTURE_ABC\"\n"
        "def _env_int(name, default): ...\n"
        "x = _env_int(OTHER, 0)\n"
    )
    assert "MIMIR_UNDOCUMENTED_FIXTURE_ABC" in _env_names_from_source(src2)


def test_scanner_covers_the_known_indirect_reads():
    """The specific constant-backed reads called out in review are covered."""
    scanned = _scan_core()
    for name in ("MIMIR_WATCHDOG_WEBHOOK_URL", "MIMIR_DEFAULTS_UPGRADE_AUTO_SUBMIT_CLEAN"):
        assert name in scanned, f"{name} (a constant-backed read) not discovered"
