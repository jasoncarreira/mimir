"""CI jobs that build the root image must load its base image from the cache.

``tests/test_root_dockerfile.py::test_built_image_provides_process_tools`` runs
``docker build`` against the root Dockerfile. Pulling its base image anonymously
from public.ecr.aws in every parallel Linux job hit 429 rate limits repeatedly,
failing otherwise green runs. The ``prime-base-image`` action loads the image
from the Actions cache first, and a local image satisfies ``FROM`` without a
registry round trip.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/tests.yml"
ACTION = ROOT / ".github/actions/prime-base-image/action.yml"
PRIME_USES = "./.github/actions/prime-base-image"
BUILD_TEST = "test_built_image_provides_process_tools"
# pytest options that consume the following token as their value.
_VALUE_OPTIONS = {"--basetemp", "--deselect", "-p", "-k", "-m", "--tb", "-n", "--durations"}


def _pytest_commands(script: str) -> list[list[str]]:
    joined = re.sub(r"\\\n\s*", " ", script)
    commands = []
    for line in joined.splitlines():
        if "pytest" not in line:
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue  # a fragment of a multi-line string, not a command line
        index = next((i for i, token in enumerate(tokens)
                      if token == "pytest" or token.endswith("/pytest")), None)
        if index is not None:
            commands.append(tokens[index + 1:])
    return commands


def _may_build_root_image(args: list[str]) -> bool:
    """True when this pytest invocation can collect and run the image-build test."""
    positionals: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in _VALUE_OPTIONS:
            skip_next = True
            continue
        if not token.startswith("-"):
            positionals.append(token)
    deselected = [args[i + 1] for i, token in enumerate(args[:-1]) if token == "--deselect"]
    if any(BUILD_TEST in item for item in deselected):
        return False
    if not positionals:  # default testpaths include tests/test_root_dockerfile.py
        return True
    return any(p == "tests" or p.startswith("tests/test_root_dockerfile.py") for p in positionals)


def _jobs_that_build_root_image() -> dict[str, dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    selected = {}
    for name, job in workflow["jobs"].items():
        if "ubuntu" not in str(job.get("runs-on", "")):
            continue  # the macOS legs have no Docker, so the build test skips
        for step in job.get("steps", []):
            if any(_may_build_root_image(args) for args in _pytest_commands(step.get("run", ""))):
                selected[name] = job
                break
    return selected


def test_every_linux_job_that_builds_the_root_image_primes_its_base_image() -> None:
    jobs = _jobs_that_build_root_image()
    assert {"pytest", "pytest-fresh-resolve", "pytest-enforced"} <= set(jobs)
    for name, job in jobs.items():
        steps = job["steps"]
        prime = next((i for i, step in enumerate(steps) if step.get("uses") == PRIME_USES), None)
        assert prime is not None, f"{name} builds the root image without priming its base image"
        first_pytest = next(
            i for i, step in enumerate(steps)
            if any(_may_build_root_image(args) for args in _pytest_commands(step.get("run", "")))
        )
        assert prime < first_pytest, f"{name} primes the base image after its test run"


def test_deselecting_the_build_test_exempts_a_job() -> None:
    assert not _may_build_root_image(
        ["-q", "--deselect", f"tests/test_root_dockerfile.py::{BUILD_TEST}"]
    )
    assert _may_build_root_image(["-q", "--basetemp", "/tmp/x"])
    assert not _may_build_root_image(["-q", "tests/test_skill_conformance.py"])


def test_action_resolves_the_dockerfile_base_image(tmp_path: Path) -> None:
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    resolve = next(step for step in action["runs"]["steps"] if step.get("id") == "ref")
    output = tmp_path / "github-output"
    subprocess.run(
        ["bash", "-c", resolve["run"]], cwd=ROOT, check=True,
        env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output), "RUNNER_OS": "Linux"},
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    declared = re.findall(r"^ARG BASE_IMAGE=(.+)$", (ROOT / "Dockerfile").read_text(), re.M)
    assert values["image"] == declared[0]
    assert values["key"].startswith("dockerfile-base-Linux-")
    assert re.search(r"-\d{4}-W\d{2}$", values["key"]), "the cache key must roll weekly"


def _run_pull_step(tmp_path: Path, *, pull_failures: int, save_ok: bool = True) -> tuple[dict, str, Path]:
    """Run the action's pull step against a fake docker and a no-op sleep."""
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    pull = next(step for step in action["runs"]["steps"] if step.get("id") == "pull")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    (bin_dir / "docker").write_text(
        "#!/bin/bash\n"
        f'echo "$1" >> "{calls}"\n'
        'if [ "$1" = pull ]; then\n'
        f'  n=$(grep -c "^pull$" "{calls}"); [ "$n" -gt {pull_failures} ]; exit $?\n'
        "fi\n"
        'if [ "$1" = save ]; then\n'
        '  out="$3"; echo partial > "$out"\n'
        f"  exit {0 if save_ok else 1}\n"
        "fi\n"
        "exit 2\n"
    )
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for tool in ("docker", "sleep"):
        (bin_dir / tool).chmod(0o755)
    output = tmp_path / "github-output"
    output.touch()
    result = subprocess.run(
        ["bash", "-c", pull["run"]], text=True, capture_output=True, check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "GITHUB_OUTPUT": str(output),
             "RUNNER_TEMP": str(tmp_path), "IMAGE": "example/base:1"},
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return values, calls.read_text(), tmp_path / "dockerfile-base-image.tar"


def test_pull_step_retries_then_exports_for_the_cache(tmp_path: Path) -> None:
    values, calls, archive = _run_pull_step(tmp_path, pull_failures=2)
    assert calls.split() == ["pull", "pull", "pull", "save"]
    assert values == {"pulled": "true"}
    assert archive.exists()


def test_pull_step_never_advertises_a_failed_export(tmp_path: Path) -> None:
    values, calls, archive = _run_pull_step(tmp_path, pull_failures=0, save_ok=False)
    assert calls.split() == ["pull", "save"]
    assert "pulled" not in values, "a partial archive must not be saved to the cache"
    assert not archive.exists()


def test_pull_step_gives_up_after_five_attempts_without_failing(tmp_path: Path) -> None:
    values, calls, archive = _run_pull_step(tmp_path, pull_failures=99)
    assert calls.split() == ["pull"] * 5
    assert "pulled" not in values
    assert not archive.exists()
