"""Every optional-skill poller must start the way production starts it.

``pollers.json`` launches these as ``python3 scripts/<entrypoint>.py`` — a
subprocess whose ``sys.path[0]`` is the INSTALLED skill's scripts directory, run
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
    helper's script-relative ``Path(__file__).parents[4]`` candidate resolves to
   the repo root and always succeeds. Deleting the ``MIMIR_SOURCE_DIR`` and
   ``/workspace/mimir`` candidates left the suite fully green, so the test
   asserted nothing about deployment portability. Found in review of #1233 by
   mutation, not by the test.

Copying the skill to a temp directory reproduces the installed shape: no
the source-checkout shortcut, so the deployment locators are the only way through.
"""

from __future__ import annotations

import ast
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SKILL_ROOT = _ROOT / "mimir" / "optional-skills"
_IMPORT_REPAIR_POLLERS = (
    _SKILL_ROOT / "chainlink-orchestrator" / "scripts" / "poller.py",
    _SKILL_ROOT / "github-poller" / "scripts" / "poller.py",
)


def _entrypoints() -> list[tuple[Path, Path]]:
    entries: list[tuple[Path, Path]] = []
    for manifest in sorted(_SKILL_ROOT.glob("*/pollers.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for poller in data.get("pollers") or []:
            scripts = [Path(token) for token in shlex.split(poller["command"]) if token.endswith(".py")]
            assert len(scripts) == 1, f"{manifest}: cannot identify Python entrypoint"
            entries.append((manifest.parent, scripts[0]))
    return entries


_ENTRYPOINTS = _entrypoints()

_IMPORT_SHIM = (
    "import importlib.util, os, sys\n"
    "entrypoint = os.environ['ENTRYPOINT']\n"
    "sys.path.insert(0, os.path.dirname(entrypoint))\n"
    "spec = importlib.util.spec_from_file_location('poller_under_test', entrypoint)\n"
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


def _env_reads(source: str) -> set[str]:
    """Collect literal env reads, resolving name parameters at helper call sites.

    Fail closed on dynamic keys instead of silently dropping a new read from
    coverage. This checks source without importing or executing a poller.
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    names: set[str] = set()

    def resolve(key: ast.expr, node: ast.AST) -> None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.add(key.value)
            return
        owner = node
        while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents[owner]
        assert isinstance(key, ast.Name) and isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)), (
            f"unresolved env key at line {node.lineno}: {ast.unparse(key)}"
        )
        parameters = [arg.arg for arg in owner.args.posonlyargs + owner.args.args]
        assert key.id in parameters, f"unresolved env parameter: {key.id}"
        index = parameters.index(key.id)
        calls = [
            call for call in ast.walk(tree)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            and call.func.id == owner.name
        ]
        assert calls, f"no call sites for env helper {owner.name}"
        for call in calls:
            argument = call.args[index] if len(call.args) > index else next(
                (kw.value for kw in call.keywords if kw.arg == key.id), None,
            )
            assert argument is not None, f"missing env key for {owner.name}"
            assert isinstance(argument, ast.Constant) and isinstance(argument.value, str), (
                f"dynamic env helper argument at line {call.lineno}"
            )
            names.add(argument.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in {"os.getenv", "os.environ.get"}:
            key = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg == "key"), None,
            )
            assert key is not None, f"missing env key at line {node.lineno}"
            resolve(key, node)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if ast.unparse(node.value) == "os.environ":
                resolve(node.slice, node)
    return names


def _assert_manifest_env(skill: Path, entrypoint: Path, poller: dict) -> None:
    from mimir.pollers import _BUILTIN_POLLER_ENV_ALLOWLIST, _POLLER_INJECTED_ENV_KEYS

    # Timeout is injected by _run_one, separately from the discovery-time keys.
    supplied = (
        _BUILTIN_POLLER_ENV_ALLOWLIST | _POLLER_INJECTED_ENV_KEYS
        | {"POLLER_TIMEOUT_SECONDS"}
        | set(poller.get("pass_env", [])) | set(poller.get("env", {}))
    )
    missing = _env_reads((skill / entrypoint).read_text(encoding="utf-8")) - supplied
    assert not missing, f"{skill.name}/{poller['name']}: missing manifest env: {sorted(missing)}"


@pytest.mark.parametrize(("skill", "entrypoint"), _ENTRYPOINTS)
def test_poller_env_reads_are_declared(skill: Path, entrypoint: Path) -> None:
    manifest = json.loads((skill / "pollers.json").read_text(encoding="utf-8"))
    for poller in manifest["pollers"]:
        if str(entrypoint) in shlex.split(poller["command"]):
            _assert_manifest_env(skill, entrypoint, poller)


@pytest.mark.parametrize(("skill_name", "name"), [
    ("github-ci-watch", "GITHUB_CI_MAX_AGE_DAYS_BY_REPO"),
    ("github-ci-watch", "GITHUB_CI_MAX_AGE_DAYS"),
    ("worklink-tool-pins", "WORKLINK_CONFIG"),
    ("worklink-tool-pins", "CHAINLINK_CWD"),
    ("worklink-tool-pins", "CHAINLINK_BIN"),
    ("chainlink-orchestrator", "CHAINLINK_BIN"),
])
def test_manifest_env_check_rejects_missing_passthrough(skill_name: str, name: str) -> None:
    skill = _SKILL_ROOT / skill_name
    poller = json.loads((skill / "pollers.json").read_text(encoding="utf-8"))["pollers"][0]
    poller["pass_env"].remove(name)
    with pytest.raises(AssertionError, match=name):
        _assert_manifest_env(skill, Path("scripts/poller.py"), poller)


@pytest.mark.parametrize("skill_name", ["worklink-tool-pins", "chainlink-orchestrator"])
@pytest.mark.parametrize(("binary", "omit_passthrough", "expected"), [
    pytest.param(None, False, "chainlink", id="unset"),
    pytest.param("", False, "chainlink", id="empty"),
    pytest.param("/custom tools/chainlink", False, "/custom tools/chainlink", id="override"),
    pytest.param("/custom tools/chainlink", True, "chainlink", id="missing-passthrough-control"),
])
def test_chainlink_binary_selection_from_manifest(
    skill_name: str, binary: str | None, omit_passthrough: bool, expected: str,
    tmp_path: Path,
) -> None:
    skill = _SKILL_ROOT / skill_name
    declared = _manifest_pass_env(skill)
    if omit_passthrough:
        declared.remove("CHAINLINK_BIN")
    host_env = {"MIMIR_SOURCE_DIR": str(_ROOT)}
    if binary is not None:
        host_env["CHAINLINK_BIN"] = binary
    env = {name: value for name, value in host_env.items() if name in declared}
    # A child process owns its entire env; no host override or imported poller
    # can mask a missing declaration or alter the binary selection.
    proc = subprocess.run(
        [sys.executable, "-c", (
            "import runpy, sys; "
            "poller = runpy.run_path(sys.argv[1]); "
            "print(poller['_chainlink_bin']())"
        ), str(skill / "scripts" / "poller.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_env_read_detection() -> None:
    assert _env_reads('''
import os
os.environ.get("DIRECT", "fallback")
os.getenv("GETENV")
os.environ["INDEX"]
os.environ["OUTPUT_ONLY"] = "value"
# os.getenv("COMMENT_ONLY")
text = 'os.environ.get("STRING_ONLY")'
def flag(name, default=False):
    return os.environ.get(name, default)
flag("HELPER")
flag(name="KEYWORD")
''') == {"DIRECT", "GETENV", "INDEX", "HELPER", "KEYWORD"}
    with pytest.raises(AssertionError, match="unresolved env key"):
        _env_reads('os.environ.get(prefix + "_TOKEN")')


@pytest.mark.parametrize("poller", _IMPORT_REPAIR_POLLERS, ids=lambda path: path.parents[1].name)
def test_import_repair_detects_symlinked_editable_venv(
    poller: Path, tmp_path: Path,
) -> None:
    """The lexical venv executable must survive a symlink to the base Python."""
    source = tmp_path / "non-default-source"
    (source / "mimir").mkdir(parents=True)
    (source / "mimir" / "__init__.py").write_text("", encoding="utf-8")
    executable = source / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)

    prelude = poller.read_text(encoding="utf-8").split(
        "\n_ensure_mimir_import_path()\n", 1,
    )[0]
    script = (
        prelude
        + f"\nsys.executable = {str(executable)!r}\n"
        + "_ensure_mimir_import_path()\nprint(sys.path[0])\n"
    )
    proc = subprocess.run(
        [sys.executable, "-S", "-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(source)


@pytest.mark.parametrize(
    ("skill", "entrypoint"),
    _ENTRYPOINTS,
    ids=lambda value: value.name if isinstance(value, Path) else str(value),
)
def test_installed_poller_entrypoint_can_resolve_mimir(
    skill: Path, entrypoint: Path, tmp_path: Path,
):
    interpreter = _interpreter_without_mimir()
    if interpreter is None:
        pytest.fail(
            "no interpreter available that lacks 'mimir' on its path, so this test "
            "cannot reproduce how pollers.json launches a poller. Do not delete it — "
            "fix the environment or the interpreter discovery."
        )

    # Reproduce the installed shape: <tmp>/skills/<name>/, far from the checkout,
    # so the helper's script-relative source-checkout candidate cannot resolve.
    installed = tmp_path / "skills" / skill.name
    shutil.copytree(skill, installed)
    assert not (installed.parents[2] / "mimir" / "__init__.py").is_file(), (
        "the temp install must not sit beside a checkout, or the script-relative path would "
        "resolve and this test would go back to proving nothing"
    )

    # Only manifest-declared env, exactly as the runner passes it. Editable
    # deployments need MIMIR_SOURCE_DIR to reach an installed skill launched by a
    # different interpreter; package deployments already import from site-packages.
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "STATE_DIR": str(installed),
        "ENTRYPOINT": str(entrypoint),
    }
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
        f"MIMIR_SOURCE_DIR so the helper can locate an editable checkout.\n"
        f"{proc.stderr[-900:]}"
    )
