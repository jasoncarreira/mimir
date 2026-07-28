"""Every optional-skill poller must start the way production starts it.

``pollers.json`` launches these as ``python3 poller.py`` — a subprocess whose
``sys.path[0]`` is the INSTALLED skill directory (``<home>/skills/<name>``), run
by whatever ``python3`` is on PATH rather than the mimir venv interpreter.
``PYTHONPATH`` cannot rescue it: that variable sits in ``mimir/pollers.py``'s
``_PROCESS_CONTROL_ENV_DENY`` and is deliberately withheld from poller
subprocesses as a process-hijack vector, alongside ``LD_PRELOAD``.

So a poller importing ``mimir`` at module scope must repair ``sys.path`` itself
(``_ensure_mimir_import_path``). #1231 added ``from mimir.pollers import ...`` to
the github poller without that repair; it then died with
``ModuleNotFoundError: No module named 'mimir'`` every cycle for hours — no PR
reviews, no changes-requested reconciliation — while its own unit tests stayed
green, because pytest has ``mimir`` importable and the production entrypoint does
not.

Two things make this test actually test that, both learned by being wrong:

1. **The interpreter must not already have ``mimir``.** A first version used
   ``sys.executable``; the venv has mimir installed editable, so it passed with
   the repair deleted. Worthless, caught by mutation.
2. **The poller must run from an INSTALLED COPY, not the source tree.** A second
   version ran each poller from ``mimir/optional-skills/<name>/``, where the
   helper's script-relative ``Path(__file__).parents[3]`` candidate resolves to
   the repo root and always succeeds. Deleting the ``MIMIR_SOURCE_DIR`` and
   ``/workspace/mimir`` candidates left the suite fully green, so the test
   asserted nothing about deployment portability. Found in review of #1233 by
   mutation, not by the test.

Copying the skill to a temp directory reproduces the installed shape: no
``parents[3]`` shortcut, so the deployment locators are the only way through.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SKILL_ROOT = _ROOT / "mimir" / "optional-skills"
_SKILLS = sorted(p for p in _SKILL_ROOT.iterdir() if (p / "poller.py").is_file())

_IMPORT_SHIM = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('poller_under_test', 'poller.py')\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "try:\n"
    "    spec.loader.exec_module(mod)\n"
    "except (ImportError, ModuleNotFoundError) as exc:\n"
    "    print('IMPORT-FAILURE: ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)\n"
    "    raise SystemExit(3)\n"
    "except SystemExit:\n"
    "    pass\n"
    "except Exception:\n"
    "    pass\n"  # runtime errors are out of scope; only import wiring matters
)


def _interpreter_without_mimir() -> str | None:
    """An interpreter that cannot import ``mimir`` — production's situation."""
    seen: set[str] = set()
    for candidate in ("/usr/bin/python3", shutil.which("python3"), shutil.which("python")):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        probe = subprocess.run(
            [candidate, "-c", "import mimir"],
            capture_output=True, text=True, timeout=60, cwd="/",
        )
        if probe.returncode != 0:
            return candidate
    return None


def _manifest_pass_env(skill: Path) -> set[str]:
    """Env names the skill's own manifest declares — the only ones production passes."""
    manifest = skill / "pollers.json"
    if not manifest.is_file():
        return set()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    names: set[str] = set()
    for poller in data.get("pollers") or []:
        names.update(poller.get("pass_env") or [])
    return names


@pytest.mark.parametrize("skill", _SKILLS, ids=lambda p: p.name)
def test_installed_poller_entrypoint_can_resolve_mimir(skill: Path, tmp_path: Path):
    interpreter = _interpreter_without_mimir()
    if interpreter is None:
        pytest.fail(
            "no interpreter available that lacks 'mimir' on its path, so this test "
            "cannot reproduce how pollers.json launches a poller. Do not delete it — "
            "fix the environment or the interpreter discovery."
        )

    # Reproduce the installed shape: <tmp>/skills/<name>/, far from the checkout,
    # so the helper's script-relative parents[3] candidate cannot resolve.
    installed = tmp_path / "skills" / skill.name
    shutil.copytree(skill, installed)
    assert not (installed.parents[2] / "mimir" / "__init__.py").is_file(), (
        "the temp install must not sit beside a checkout, or parents[3] would "
        "resolve and this test would go back to proving nothing"
    )

    # Only manifest-declared env, exactly as the runner passes it. MIMIR_SOURCE_DIR
    # reaches the subprocess only if the skill's own pollers.json declares it — which
    # is the portability half of this check.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "STATE_DIR": str(installed)}
    declared = _manifest_pass_env(skill)
    if "MIMIR_SOURCE_DIR" in declared:
        env["MIMIR_SOURCE_DIR"] = str(_ROOT)
    if "MIMIR_HOME" in declared:
        env["MIMIR_HOME"] = str(tmp_path)

    proc = subprocess.run(
        [interpreter, "-c", _IMPORT_SHIM],
        cwd=str(installed), env=env, capture_output=True, text=True, timeout=180,
    )

    # Only the mimir-resolution class is in scope. A probe interpreter may predate
    # the project's floor (macOS ships 3.9; `datetime.UTC` and `enum.StrEnum` are
    # 3.11+), and those ImportErrors say nothing about the sys.path repair.
    assert "No module named 'mimir" not in proc.stderr, (
        f"{skill.name}: an INSTALLED copy cannot resolve `mimir` using only the env "
        f"its own pollers.json declares. A poller importing mimir at module scope "
        f"needs _ensure_mimir_import_path(), AND its manifest must pass "
        f"MIMIR_SOURCE_DIR so the helper can locate the checkout outside the "
        f"mimirbot-specific /workspace/mimir layout.\n{proc.stderr[-900:]}"
    )
