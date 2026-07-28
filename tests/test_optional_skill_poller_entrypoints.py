"""Every optional-skill poller must start the way production starts it.

``pollers.json`` launches these as ``python3 poller.py`` — a subprocess with the
SKILL directory as ``sys.path[0]``, run by whatever ``python3`` is on PATH rather
than the mimir venv interpreter. ``PYTHONPATH`` cannot rescue it: that variable
sits in ``mimir/pollers.py``'s ``_PROCESS_CONTROL_ENV_DENY`` and is deliberately
withheld from poller subprocesses as a process-hijack vector.

So a poller that imports ``mimir`` at module scope must repair ``sys.path``
itself first (``_ensure_mimir_import_path``). #1231 added
``from mimir.pollers import ...`` to the github poller without that repair; the
poller then died with ``ModuleNotFoundError: No module named 'mimir'`` on every
cycle for hours — no PR reviews, no changes-requested reconciliation — while its
own unit tests stayed green, because pytest has ``mimir`` importable and the
production entrypoint does not.

The test must therefore run an interpreter that CANNOT already import ``mimir``.
Using ``sys.executable`` is worthless here: the venv has mimir installed
editable, so the check passes with the repair deleted. That was verified by
mutation, not assumed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SKILLS = sorted(
    p for p in (_ROOT / "mimir" / "optional-skills").iterdir()
    if (p / "poller.py").is_file()
)


def _interpreter_without_mimir() -> str | None:
    """An interpreter that cannot import ``mimir`` — i.e. production's situation."""
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


@pytest.mark.parametrize("skill", _SKILLS, ids=lambda p: p.name)
def test_poller_entrypoint_imports_from_its_own_directory(skill: Path, tmp_path: Path):
    interpreter = _interpreter_without_mimir()
    if interpreter is None:
        # Loud rather than silent: if every interpreter on this host can already
        # import mimir, the check cannot reproduce production and must say so.
        pytest.fail(
            "no interpreter available that lacks 'mimir' on its path, so this "
            "test cannot reproduce how pollers.json launches a poller. Do not "
            "delete it — fix the environment or the interpreter discovery."
        )

    shim = (
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
    proc = subprocess.run(
        [interpreter, "-c", shim],
        cwd=str(skill),
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "STATE_DIR": str(tmp_path),
             "MIMIR_SOURCE_DIR": str(_ROOT)},
        capture_output=True, text=True, timeout=180,
    )
    # Only the mimir-resolution class is in scope. A probe interpreter may be an
    # older minor version than the project targets (macOS ships 3.9; `datetime.UTC`
    # and `enum.StrEnum` are 3.11+), and those ImportErrors say nothing about the
    # sys.path repair. Production runs a supported interpreter, so narrowing here
    # keeps the check meaningful without making it environment-dependent.
    assert "No module named 'mimir" not in proc.stderr, (
        f"{skill.name}/poller.py cannot resolve `mimir` when imported from its own "
        f"directory by an interpreter without mimir on its path — the way "
        f"pollers.json runs it. A poller importing mimir at module scope must call "
        f"_ensure_mimir_import_path() first; PYTHONPATH is denied to poller "
        f"subprocesses.\n{proc.stderr[-900:]}"
    )
