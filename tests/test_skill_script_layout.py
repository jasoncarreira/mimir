"""Conformance guards for executable code shipped by skills."""

from __future__ import annotations

import ast
import json
import shlex
from collections import defaultdict
from importlib.resources import files
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOTS = (
    REPO_ROOT / "mimir" / "skills",
    REPO_ROOT / "mimir" / "optional-skills",
)
EXECUTABLE_SUFFIXES = {".py", ".sh", ".js", ".ts"}
OPTIONAL_SKILLS_ROOT = REPO_ROOT / "mimir" / "optional-skills"


def _skill_dirs() -> list[Path]:
    return sorted(
        skill_dir
        for root in SKILL_ROOTS
        for skill_dir in root.iterdir()
        if skill_dir.is_dir()
    )


def _poller_manifests() -> list[Path]:
    return [path for skill_dir in _skill_dirs() if (path := skill_dir / "pollers.json").is_file()]


def _manifest_script(command: str) -> Path:
    scripts = [Path(token) for token in shlex.split(command) if Path(token).suffix in EXECUTABLE_SUFFIXES]
    assert len(scripts) == 1, f"poller command must name exactly one script: {command!r}"
    assert not scripts[0].is_absolute(), f"poller script must be relative to its skill: {command!r}"
    return scripts[0]


class _ModuleScopeImportVisitor(ast.NodeVisitor):
    def __init__(self, modules: set[str]) -> None:
        self.modules = modules
        self.imports: list[tuple[int, str]] = []

    def _record(self, name: str, lineno: int) -> None:
        module = name.partition(".")[0]
        if module in self.modules:
            self.imports.append((lineno, module))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self._record(node.module, node.lineno)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self._record(node.args[0].value, node.lineno)
        self.generic_visit(node)

    # Function bodies do not run while pytest imports the test module.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        pass

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        pass

    def visit_Lambda(self, node: ast.Lambda) -> None:
        pass


def _shared_script_import_violations(optional_skills_root: Path) -> list[str]:
    scripts_by_module: dict[str, set[Path]] = defaultdict(set)
    skill_dirs = sorted(path for path in optional_skills_root.iterdir() if path.is_dir())
    for skill_dir in skill_dirs:
        for script in (skill_dir / "scripts").glob("*.py"):
            if script.name != "__init__.py":
                scripts_by_module[script.stem].add(skill_dir)

    shared_modules = {
        module for module, owners in scripts_by_module.items() if len(owners) >= 2
    }
    violations = []
    for skill_dir in skill_dirs:
        local_modules = {
            script.stem
            for script in (skill_dir / "scripts").glob("*.py")
            if script.name != "__init__.py"
        }
        guarded_modules = shared_modules & local_modules
        if not guarded_modules:
            continue
        for test_path in sorted((skill_dir / "tests").rglob("*.py")):
            visitor = _ModuleScopeImportVisitor(guarded_modules)
            visitor.visit(ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path)))
            violations.extend(
                f"{test_path.relative_to(optional_skills_root)}:{lineno} imports shared "
                f"skill script module {module!r} at module scope"
                for lineno, module in visitor.imports
            )
    return violations


def _assert_no_shared_script_imports(optional_skills_root: Path) -> None:
    violations = _shared_script_import_violations(optional_skills_root)
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("manifest", _poller_manifests(), ids=lambda path: path.parent.name)
def test_poller_manifest_commands_resolve(manifest: Path) -> None:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    pollers = data.get("pollers") or []
    assert pollers, f"{manifest}: no pollers declared"
    for poller in pollers:
        script = _manifest_script(poller["command"])
        assert (manifest.parent / script).is_file(), (
            f"{manifest}: poller {poller.get('name')!r} points at missing script {script}"
        )


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda path: path.name)
def test_skill_root_has_no_executable_code(skill_dir: Path) -> None:
    if skill_dir.parent.name == "optional-skills":
        assert not (skill_dir / "tests" / "__init__.py").exists(), (
            f"{skill_dir}: optional skill test directories must not be packages "
            "named 'tests'"
        )
    root_scripts = sorted(
        path.name
        for path in skill_dir.iterdir()
        if path.is_file()
        and path.suffix in EXECUTABLE_SUFFIXES
        and path.name != "__init__.py"
    )
    assert not root_scripts, (
        f"{skill_dir}: executable code must live under scripts/: {root_scripts}"
    )


def test_skill_scripts_resolve_from_installed_package() -> None:
    package_root = files("mimir")
    source_root = REPO_ROOT / "mimir"
    scripts = sorted(
        path
        for root in SKILL_ROOTS
        for path in root.glob("*/scripts/**/*")
        if path.is_file() and path.suffix in EXECUTABLE_SUFFIXES
    )
    assert scripts
    missing = [
        str(path.relative_to(source_root))
        for path in scripts
        if not package_root.joinpath(*path.relative_to(source_root).parts).is_file()
    ]
    assert not missing, f"skill scripts missing from installed package: {missing}"


def test_optional_skill_tests_do_not_import_shared_scripts_at_module_scope() -> None:
    # tests.yml runs each skill in a separate pytest process, so only a static
    # guard can catch one skill's top-level module shadowing another skill's.
    _assert_no_shared_script_imports(OPTIONAL_SKILLS_ROOT)


def _write_shared_script_fixture(root: Path, test_source: str) -> None:
    for skill in ("first-skill", "second-skill"):
        scripts_dir = root / skill / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "poller.py").write_text("", encoding="utf-8")
    tests_dir = root / "first-skill" / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_collision.py").write_text(test_source, encoding="utf-8")


@pytest.mark.parametrize(
    "test_source",
    [
        "import poller\n",
        "from poller import main\n",
        'import importlib\npoller = importlib.import_module("poller")\n',
    ],
)
def test_shared_script_import_guard_rejects_collision(
    tmp_path: Path, test_source: str
) -> None:
    optional_skills_root = tmp_path / "optional-skills"
    _write_shared_script_fixture(optional_skills_root, test_source)

    with pytest.raises(AssertionError, match="imports shared skill script module 'poller'"):
        _assert_no_shared_script_imports(optional_skills_root)


def test_shared_script_import_guard_accepts_function_scope_and_unique_support(
    tmp_path: Path,
) -> None:
    optional_skills_root = tmp_path / "optional-skills"
    _write_shared_script_fixture(
        optional_skills_root,
        "from unique_test_support import poller\n"
        "\n"
        "def load_poller():\n"
        "    import importlib\n"
        "    import poller\n"
        '    return importlib.import_module("poller")\n',
    )

    _assert_no_shared_script_imports(optional_skills_root)
