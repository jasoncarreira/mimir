"""Keep root-image Cultivate build invocations and operator hints complete.

Test/fixture trees deliberately contain isolated assertion strings and invalid
examples; production Python, scripts, workflows, docs and dotfiles are scanned.
Python is checked per literal/container, not per file (another correct caller
must not mask an incomplete one). Text is checked per command or prose block.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SELECTION = re.compile(r"MIMIR_PLUGIN_SELECTION=(?:cultivate|all)\b")
ARTIFACT = "CHAINLINK_ARTIFACT_STAGE=chainlink-build"


def _missing_arg_units(text: str, suffix: str) -> list[int]:
    if suffix == ".py":
        tree = ast.parse(text)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if not SELECTION.search(node.value):
                continue
            unit = node
            while isinstance(parents.get(unit), (ast.JoinedStr, ast.BinOp)):
                unit = parents[unit]
            if isinstance(parents.get(unit), (ast.List, ast.Tuple)):
                unit = parents[unit]
            literals = [n.value for n in ast.walk(unit)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if ARTIFACT not in " ".join(literals):
                missing.append(node.lineno)
        return missing

    # Join shell continuations before checking individual commands. Outside
    # fenced snippets, a paragraph is the unit for line-wrapped operator hints.
    text = re.sub(r"\\\n[ \t]*", " ", text)
    blocks = []
    prose = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            if prose:
                blocks.append(prose)
                prose = []
            fenced = not fenced
        elif fenced or suffix in {".sh", ".yml", ".yaml"}:
            blocks.append([(number, line)])
        elif line.strip():
            prose.append((number, line))
        elif prose:
            blocks.append(prose)
            prose = []
    if prose:
        blocks.append(prose)
    missing = []
    for block in blocks:
        content = " ".join(line for _, line in block)
        matches = list(SELECTION.finditer(content))
        for index, match in enumerate(matches):
            # Do not let the next invocation's correct arg satisfy this one.
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            if ARTIFACT not in content[match.start():end]:
                missing.append(block[0][0])
    return missing


def _build_sources(root: Path):
    ignored = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
               "tests", "fixtures", "dist", "build", ".worklink"}
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ignored and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if name == "Dockerfile" or name.startswith("test_") or path.is_symlink():
                continue
            if path.suffix not in {".py", ".sh", ".yaml", ".yml", ".md", ".rst", ".txt", ""} and name != ".env.example":
                continue
            yield path


def test_every_cultivate_build_invocation_and_hint_selects_chainlink_artifact():
    checked = set()
    failures = []
    for path in _build_sources(ROOT):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # Binary assets are not build invocations or operator hints.
        if SELECTION.search(text):
            checked.add(path.relative_to(ROOT).as_posix())
            failures.extend(f"{path.relative_to(ROOT)}:{line}" for line in _missing_arg_units(text, path.suffix))
    # Pin production coverage as well as failures, so an empty scan cannot pass.
    assert {"plugins/cultivate/mimir_cultivate/setup.py", "mimir/config.py",
            "mimir/plugins/discovery.py", "scripts/worklink_image_identity.py",
            "docs/plugins.md", ".env.example"} <= checked
    assert not failures, "Cultivate build missing required artifact arg: " + ", ".join(failures)


@pytest.mark.parametrize("relative", [
    "plugins/cultivate/mimir_cultivate/setup.py", "mimir/config.py",
    "mimir/plugins/discovery.py", "scripts/worklink_image_identity.py",
    "docs/plugins.md", "docs/configuration.md", "docs/code-building-pipeline.md",
    "docs/internal/WORKLINK.md", "docker/README.md", ".env.example",
])
def test_guard_detects_missing_arg_in_real_callers(relative):
    path = ROOT / relative
    text = path.read_text()
    assert not _missing_arg_units(text, path.suffix)
    assert ARTIFACT in text
    assert _missing_arg_units(text.replace(ARTIFACT, "REMOVED_ARTIFACT_ARG"), path.suffix)


@pytest.mark.parametrize("suffix,text", [
    (".py", 'run(["docker", "build", "--build-arg", "MIMIR_PLUGIN_SELECTION=cultivate", "--build-arg", "CHAINLINK_ARTIFACT_STAGE=chainlink-build"])'),
    (".py", 'hint = ("MIMIR_PLUGIN_SELECTION=cultivate "\n "CHAINLINK_ARTIFACT_STAGE=chainlink-build")'),
    (".sh", 'docker build --build-arg MIMIR_PLUGIN_SELECTION=cultivate \\\n --build-arg CHAINLINK_ARTIFACT_STAGE=chainlink-build .'),
    (".yml", 'run: docker build --build-arg MIMIR_PLUGIN_SELECTION=cultivate \\\n --build-arg CHAINLINK_ARTIFACT_STAGE=chainlink-build .'),
    (".md", 'Build with `MIMIR_PLUGIN_SELECTION=cultivate` and\n`CHAINLINK_ARTIFACT_STAGE=chainlink-build`.\n'),
])
def test_guard_accepts_complete_units_and_rejects_missing_arg(suffix, text):
    assert not _missing_arg_units(text, suffix)
    assert _missing_arg_units(text.replace(ARTIFACT, "REMOVED"), suffix)


def test_other_correct_python_caller_cannot_mask_incomplete_one():
    text = ('one = ["MIMIR_PLUGIN_SELECTION=cultivate"]\n'
            'two = ["MIMIR_PLUGIN_SELECTION=cultivate", "CHAINLINK_ARTIFACT_STAGE=chainlink-build"]\n')
    assert _missing_arg_units(text, ".py") == [1]


def test_repository_scan_discovers_new_script_workflow_and_doc(tmp_path):
    paths = ["new.py", "scripts/build.sh", ".github/workflows/new.yml", "docs/new.md", ".env.example"]
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('hint = "MIMIR_PLUGIN_SELECTION=cultivate"' if path.suffix == ".py"
                        else "MIMIR_PLUGIN_SELECTION=cultivate\n")
    found = {path.relative_to(tmp_path).as_posix() for path in _build_sources(tmp_path)
             if _missing_arg_units(path.read_text(), path.suffix)}
    assert found == set(paths)
