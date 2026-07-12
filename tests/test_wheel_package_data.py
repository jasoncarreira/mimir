"""Regression guard for wheel packaging (chainlink #290).

The wheel's ``[tool.hatch.build].include`` list is an allowlist: any
runtime data file not matched by a pattern silently drops from the
published wheel. That shipped a string of broken fresh-install bugs in
0.2.0/0.2.1 — saga's ``schema.sql`` (read on fresh-DB init), the default
prompt templates, the scheduler template, the credential manifest, and
some skills' support files (tmux ``.sh``, chainlink ``.fragment``) were
all absent from the wheel even though the installed package reads them at
runtime.

These tests assert (a) the critical data files still exist in the source
tree and (b) the include config actually covers them, using a faithful
translation of the git-wildmatch globs hatchling uses. Pure stdlib
(``tomllib`` + ``re``) so it runs anywhere pytest does — no build step and
no ``uv`` / ``hatchling`` / ``pathspec`` dependency.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Data files the INSTALLED package reads at runtime — each MUST ship in the
# wheel. Add new runtime data files here as they're introduced; keeping this
# list current is the whole point of the guard.
CRITICAL_RUNTIME_DATA = (
    "mimir/saga/schema.sql",  # SagaStore executescript()s this on fresh-DB init
    "mimir/scheduler_template.yaml",  # seed_scheduler's default scheduler
    "mimir/credentials.yaml",  # cred_verify._PACKAGE_MANIFEST, loaded at import
    "mimir/prompt_templates/heartbeat.md",  # seed_prompts default tick prompts
    "mimir/prompt_templates/reflect.md",
    "mimir/prompt_templates/issues-audit.md",
    "mimir/prompt_templates/commitments-review.md",
    "mimir/prompt_templates/upgrade.md",  # version-triggered defaults reconciliation
    "mimir/prompt_templates/worklink-order.md",  # Worklink operator-run prompt
    "mimir/prompt_templates/decompose.md",  # Worklink planner/decomposer prompt
    "mimir/memory_templates/core/00-identity.md",  # setup core-memory defaults
    "mimir/memory_templates/core/05-non-goals.md",
    "mimir/memory_templates/core/06-action-boundaries.md",
    "mimir/memory_templates/core/20-vsm-terms.md",
    "mimir/memory_templates/core/30-reflection-policy.md",
    "mimir/memory_templates/core/40-learned-behaviors.md",
    "mimir/memory_templates/core/50-heartbeat-patterns.md",
    "mimir/memory_templates/core/60-filing-rules.md",
    "mimir/skills/tmux/scripts/find-sessions.sh",  # tmux skill support scripts
    "mimir/skills/tmux/scripts/wait-for-text.sh",
    "mimir/skills/chainlink/dockerfile.fragment",  # chainlink scaffold fragment
    "mimir/optional-skills/chainlink-orchestrator/SKILL.md",  # Worklink planner skill (opt-in)
    "mimir/optional-skills/chainlink-orchestrator/poller.py",  # ready-queue poller (#444)
    "mimir/optional-skills/chainlink-orchestrator/pollers.json",
    "mimir/optional-skills/dependency-advisory-watch/SKILL.md",  # OSV dependency advisory poller (opt-in)
    "mimir/optional-skills/dependency-advisory-watch/poller.py",  # poller entry point
    "mimir/optional-skills/dependency-advisory-watch/pollers.json",  # poller manifest
    "mimir/optional-skills/dependency-advisory-watch/scanner.py",  # OSV scanning logic
    "mimir/optional-skills/dependency-advisory-watch/dockerfile.fragment",  # pinned osv-scanner installer
)


def _gitwildmatch_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate the git-wildmatch glob subset hatchling uses into an
    anchored full-path regex.

    Handles the three constructs our include/exclude patterns use:
    ``**/`` (zero or more whole path segments), ``*`` (anything but ``/``
    within a segment), and ``?`` (one non-``/`` char). Everything else is a
    literal. Faithful enough for the patterns pyproject.toml actually uses;
    ``test_gitwildmatch_to_regex_semantics`` pins the behavior.
    """
    out = ["^"]
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")  # zero or more full directory segments
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")  # within a single path segment
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _build_include_exclude() -> tuple[list[str], list[str]]:
    cfg = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    build = cfg["tool"]["hatch"]["build"]
    return build.get("include", []), build.get("exclude", [])


def test_gitwildmatch_to_regex_semantics() -> None:
    # ``**`` crosses path separators; ``*`` does not.
    r = _gitwildmatch_to_regex("mimir/**/*.py")
    assert r.match("mimir/server.py")  # zero intermediate dirs
    assert r.match("mimir/saga/client.py")  # nested
    assert not r.match("mimir/server.pyc")  # extension is matched exactly

    r = _gitwildmatch_to_regex("mimir/saga/*.sql")
    assert r.match("mimir/saga/schema.sql")
    assert not r.match("mimir/saga/sub/x.sql")  # ``*`` stops at ``/``

    r = _gitwildmatch_to_regex("mimir/skills/**/*")
    assert r.match("mimir/skills/tmux/scripts/find-sessions.sh")
    assert r.match("mimir/skills/chainlink/dockerfile.fragment")
    assert not r.match("mimir/saga/schema.sql")  # outside skills/

    r = _gitwildmatch_to_regex("mimir/scheduler_template.yaml")
    assert r.match("mimir/scheduler_template.yaml")
    assert not r.match("mimir/scheduler_template-yaml")  # ``.`` is a literal


def test_critical_runtime_data_files_exist_in_source() -> None:
    missing = [rel for rel in CRITICAL_RUNTIME_DATA if not (REPO_ROOT / rel).is_file()]
    assert not missing, f"runtime data files absent from the source tree: {missing}"


def test_wheel_include_config_covers_critical_runtime_data() -> None:
    include, exclude = _build_include_exclude()
    inc = [_gitwildmatch_to_regex(p) for p in include]
    exc = [_gitwildmatch_to_regex(p) for p in exclude]
    uncovered = [
        rel
        for rel in CRITICAL_RUNTIME_DATA
        if not any(r.match(rel) for r in inc) or any(r.match(rel) for r in exc)
    ]
    assert not uncovered, (
        "these runtime data files are NOT packaged by "
        f"[tool.hatch.build].include (or are killed by exclude): {uncovered}. "
        "Add a matching include glob in pyproject.toml (see chainlink #290)."
    )


# ---- Reference docs shipped via force-include (mimir/doc_seed.py) ----------
# The operator docs live at the repo root but must ALSO ship in the wheel under
# mimir/bundled_docs/ so `mimir setup` can seed them into <home>/docs/. They're
# packaged via [tool.hatch.build.targets.wheel.force-include], not the include
# allowlist above.

def _force_include() -> dict:
    cfg = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return (
        cfg["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})
    )


def test_docs_force_included_into_wheel() -> None:
    fi = _force_include()
    # Sources map under mimir/bundled_docs/ so doc_seed.source_root() finds them.
    assert fi.get("docs") == "mimir/bundled_docs/docs"
    assert fi.get("README.md") == "mimir/bundled_docs/README.md"
    assert fi.get(".env.example") == "mimir/bundled_docs/.env.example"
    # And the sources exist in the tree.
    for rel in ("docs/configuration.md", "README.md", ".env.example"):
        assert (REPO_ROOT / rel).is_file(), f"seed source missing: {rel}"


def test_doc_seed_enumerates_operator_docs_excluding_internal() -> None:
    from mimir import doc_seed

    root = doc_seed.source_root()
    assert root is not None
    rels = {rel for rel, _src in doc_seed._seed_items(root)}
    assert "docs/configuration.md" in rels
    assert "docs/README.md" in rels
    assert "docs/.env.example" in rels
    # docs/internal/ is never seeded into the home
    assert not any(r.startswith("docs/internal") or "internal" in r for r in rels)
