"""``.factory.json`` declares how this repository turns a run input into a work item.

The factory dropped its built-in GitHub grammar (upstream
``opencode-feature-factory#213``), so reference intake exists only where a
repository declares it. That makes the parsing contract load-bearing: an input
that resolves when it should not silently starts a run against the wrong work
item, and an input that fails to resolve silently becomes a free-text feature
request. Both failures are quiet, which is why they are pinned here.

Three defects have already shipped in this one command and each was a variant of
"input silently becomes the wrong work item":

* ``gh issue view`` also resolves pull requests, because GitHub serves issues and
  PRs from ``/issues/{n}`` — so a PR number became a work item.
* ``tr -d '[:space:]'`` stripped *all* whitespace, so ``13 89`` collapsed to
  ``1389``.
* Both ``sed`` stages were line-oriented, so ``"add dark mode\\n1389"`` had its
  prose line dropped and resolved as issue 1389.

``gh`` is stubbed on ``PATH`` so these assertions need no network and no
credentials. The stub records its argv, which lets a rejection be asserted as
"``gh`` was never invoked" rather than merely "stdout was empty" — the stronger
claim, and the one that distinguishes a parse rejection from a lookup that
happened and returned nothing.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DECLARATION = REPO_ROOT / ".factory.json"


def _resolve_command() -> str:
    return json.loads(DECLARATION.read_text())["resolve"]


def _stub_gh(tmp_path: Path, *, kind: str = "issue", api_rc: int = 0) -> tuple[Path, Path]:
    """Write a ``gh`` stub onto PATH. Returns (bindir, argv-log).

    ``kind`` drives the ``.pull_request`` probe so the pull-request refusal can be
    exercised without a live PR; ``api_rc`` drives the not-found path.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gh-argv.log"
    stub = bindir / "gh"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        "  api)\n"
        f"    [ {api_rc} -eq 0 ] || exit {api_rc}\n"
        f'    printf "%s\\n" "{kind}"\n'
        "    ;;\n"
        "  issue)\n"
        # Shape only matters insofar as the caller emits it verbatim.
        '    printf \'{"run_id":"STUB","number":0,"title":"stub"}\\n\'\n'
        "    ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return bindir, log


def _run(command: str, factory_input: str, bindir: Path) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["FACTORY_INPUT"] = factory_input
    return subprocess.run(
        ["sh", "-c", command], capture_output=True, text=True, env=env, cwd=REPO_ROOT
    )


def test_declaration_is_valid_json_with_exactly_the_expected_keys() -> None:
    data = json.loads(DECLARATION.read_text())
    # Exact, not a subset: an unrecognized key here means the factory would either refuse the
    # declaration outright or act on something nobody reviewed. `bootstrap` joined the set when
    # this repository opted in to feature-factory #248, so a sandbox installs its dependencies
    # before any gate instead of discovering they are absent.
    assert set(data) == {"resolve", "verify", "publish", "publishing_identity", "bootstrap", "pr_draft"}
    # The declaration must not carry a credential; it may only reference the
    # environment the factory already inherits.
    blob = DECLARATION.read_text()
    for secret_marker in ("ghp_", "gho_", "github_pat_", "GH_TOKEN="):
        assert secret_marker not in blob


@pytest.mark.parametrize(
    "factory_input",
    [
        "1389",
        "#1389",
        "https://github.com/jasoncarreira/mimir/issues/1389",
        "  1389  ",
        "\n1389\n",
    ],
)
def test_whole_input_references_resolve(tmp_path: Path, factory_input: str) -> None:
    bindir, log = _stub_gh(tmp_path)
    proc = _run(_resolve_command(), factory_input, bindir)
    assert proc.returncode == 0, proc.stderr
    assert "STUB" in proc.stdout, f"expected a resolved work item, got {proc.stdout!r}"
    assert "1389" in log.read_text(), "gh should have been asked about issue 1389"


@pytest.mark.parametrize(
    "factory_input",
    [
        "add a dark mode toggle",
        "13 89",
        # Regression: both sed stages were line-oriented, so the prose line was
        # dropped and the numeric line resolved on its own.
        "add dark mode\n1389",
        "1389\nand also 1390",
        "",
        "   ",
        "issue 1389 please",
        "https://github.com/other/repo/issues/1389",
    ],
)
def test_non_references_are_rejected_without_any_lookup(
    tmp_path: Path, factory_input: str
) -> None:
    bindir, log = _stub_gh(tmp_path)
    proc = _run(_resolve_command(), factory_input, bindir)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"expected empty stdout, got {proc.stdout!r}"
    assert not log.exists(), (
        "gh must not be consulted for something that is not a reference; "
        f"stub was called with: {log.read_text() if log.exists() else ''!r}"
    )


def test_pull_request_numbers_are_refused(tmp_path: Path) -> None:
    """GitHub serves issues and PRs from /issues/{n}, so a PR number parses as a
    reference. It must not become a work item."""
    bindir, log = _stub_gh(tmp_path, kind="pr")
    proc = _run(_resolve_command(), "1388", bindir)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"a pull request must not resolve, got {proc.stdout!r}"
    # The probe must actually have run — this is a refusal, not a parse failure.
    assert "api" in log.read_text()


def test_unresolvable_number_errors_rather_than_becoming_free_text(tmp_path: Path) -> None:
    """A number-shaped input that does not exist is a typo, not free text. It must
    fail loudly instead of silently starting a new-feature run."""
    bindir, _ = _stub_gh(tmp_path, api_rc=1)
    proc = _run(_resolve_command(), "999999", bindir)
    assert proc.returncode != 0, "an unresolvable reference must not exit zero"
    assert proc.stdout == ""
