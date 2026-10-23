"""Validation for the published runtime image (``./Dockerfile``).

The repo root ``Dockerfile`` is the canonical mimir image. These checks keep
operationally-relied-on CLI tools present and the apt layer hygienic so a clean
rebuild can't silently drop them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

FACTORY_VERSION = "0.12.1"
OPENCODE_VERSION = "1.18.21"
from mimir.plugin_dockerfile import main as assemble_main, verify_pins

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
ROOT = DOCKERFILE.parent


def _text() -> str:
    root = DOCKERFILE.read_text(encoding="utf-8")
    fragments = ROOT / "plugins/cultivate/mimir_cultivate/docker"
    return root + "\n" + "\n".join((fragments / name).read_text(encoding="utf-8") for name in (
        "base.sh", "users.sh", "executor.sh", "controller.sh", "bootstrap.sh", "service.sh"
    ))


def test_trusted_fragment_preserves_pre_extraction_instruction_contract() -> None:
    fixture = (ROOT / "tests/fixtures/cultivate_docker_pre_extraction.txt").read_text()
    assert "b421db2550853af57ba7c579d358fd62b036977c05ac177a4bd653d32d3b16f4" in fixture
    blocks = fixture.split("\n\n")
    assert len(blocks) == 6
    source = ROOT / "plugins/cultivate/mimir_cultivate/docker"
    root = DOCKERFILE.read_text()
    assert "COPY plugins/cultivate/mimir_cultivate/docker/ /opt/cultivate-docker/" in root
    names = ("base.sh", "users.sh", "executor.sh", "bootstrap.sh", "service.sh")
    invocations = [root.index(f"sh /opt/cultivate-docker/{name}") for name in names]
    assert invocations == sorted(invocations)
    assert root.index("USER root\nARG MIMIR_GIT_URL") < invocations[2] < root.index("USER mimir", invocations[2])
    # controller.sh is new since the extraction: it runs as mimir, after the
    # root worker build and before the bootstrap that needs the plugin installed.
    controller = root.index("sh /opt/cultivate-docker/controller.sh")
    assert root.index("USER mimir", invocations[2]) < controller < invocations[3]
    assert root.index("COPY deploy/s6-overlay/s6-rc.d/") < invocations[4] < root.index('ENTRYPOINT ["/init"]')
    assert f"ARG FACTORY_VERSION={FACTORY_VERSION}" in root
    assert "ARG FACTORY_VERSION=0.10.16" in fixture  # The only version drift in the snapshot.

    def normal(text: str) -> str:
        return " ".join(text.replace("\\\n", " ").replace('"', "").replace("'", "").split())

    baseline = (
        blocks[0], blocks[1], blocks[2] + "\n" + blocks[3], blocks[4], blocks[5]
    )
    # Each entry is an instruction or complete command/argument sequence, not a
    # bag of keywords: its presence and order must hold in both the original
    # inlined block and the build script invoked at that root stage.
    operations = (
        ("apt-get update", "apt-get install -y --no-install-recommends",
         "ca-certificates curl gnupg git jq procps ripgrep xz-utils",
         "poppler-utils tesseract-ocr tesseract-ocr-eng libjemalloc2",
         "curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash -",
         "apt-get install -y --no-install-recommends nodejs",
         "npm install --global --prefix /opt/mimir-opencode", "opencode-ai@1.18.21",
         "feature-factory@${FACTORY_VERSION}", "opencode-feature-factory@${FACTORY_VERSION}",
         "opencode-project-memory@0.1.0", "opencode-openai-codex-auth@4.4.0",
         "opencode-anthropic-auth@0.0.13", "apt-get clean", "rm -rf /var/lib/apt/lists/*"),
        ("groupadd --gid 1001 mimir", "groupadd --gid 1002 worklink",
         "useradd --create-home --uid 1001 --gid mimir --groups worklink --shell /bin/bash mimir",
         "useradd --no-create-home --uid 1002 --gid worklink --home-dir /nonexistent --shell /usr/sbin/nologin worklink",
         "chmod 0700 /home/mimir", "install -d -o root -g root -m 0711 /var/lib/mimir-worklink",
         "install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/checkouts",
         "install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/repo-test-checkouts",
         "install -d -o root -g mimir -m 0771 /var/lib/mimir-worklink/opencode-checkouts",
         "install -d -o root -g worklink -m 0710 /var/lib/mimir-worklink/homes"),
        ("git check-ref-format $MIMIR_GIT_REF", "git init /opt/mimir-worklink/source",
         "git -C /opt/mimir-worklink/source remote add origin $MIMIR_GIT_URL",
         "git -C /opt/mimir-worklink/source fetch --no-tags --depth=1 origin $MIMIR_EXECUTOR_COMMIT",
         "git -C /opt/mimir-worklink/source fetch --no-tags --depth=1 origin $MIMIR_GIT_REF",
         "git -C /opt/mimir-worklink/source rev-parse FETCH_HEAD",
         "git -C /opt/mimir-worklink/source checkout --detach FETCH_HEAD",
         "git -C /opt/mimir-worklink/source rev-parse HEAD",
         "git -C /opt/mimir-worklink/source status --porcelain=v1",
         "printf %s\\n $MIMIR_EXECUTOR_COMMIT > /opt/mimir-worklink/executor-source-commit",
         "rm -rf /opt/mimir-worklink/source/.git", "python3 -m venv /opt/mimir-worklink/venv",
         "/opt/mimir-worklink/venv/bin/pip install --no-cache-dir --upgrade pip",
         "/opt/mimir-worklink/venv/bin/pip install --no-cache-dir mimir-agent[${MIMIR_EXTRAS}]",
         "/opt/mimir-worklink/venv/bin/pip install --no-cache-dir pypdf>=6.16",
         "/opt/mimir-worklink/venv/bin/pip install --no-cache-dir --no-deps /opt/mimir-worklink/source",
         "/opt/mimir-worklink/venv/bin/pip install --no-cache-dir uv",
         "ln -s",
         "UV_CACHE_DIR=/opt/mimir-worklink/uv-cache uv sync --directory /opt/mimir-worklink/source",
         "--locked --extra dev --extra bench --no-install-workspace",
         "rm -rf /opt/mimir-worklink/source/.venv", "rm -rf /opt/mimir-worklink/source",
         "chown -R root:root /opt/mimir-worklink", "chmod -R go-w /opt/mimir-worklink"),
        ("mimir opencode-bootstrap --home /home/mimir",),
        ("install -d -o root -g root -m 0755 /usr/local/libexec",
         "chmod 0755 /usr/local/libexec/worklink-execd",
         "/etc/s6-overlay/s6-rc.d/mimir/run"),
    )
    for name, original, expected in zip(names, baseline, operations, strict=True):
        original_text = normal(original).replace("mimir.worklink.worker_exec", "mimir_cultivate.worklink.worker_exec")
        script_text = normal((source / name).read_text())
        for text in (original_text, script_text):
            start = 0
            for operation in expected:
                found = text.find(operation, start)
                assert found >= 0, f"{name}: missing or reordered instruction {operation!r}"
                start = found + len(operation)
        if name == "service.sh":
            for path in ("watchdog", "worklink-execd"):
                assert f"/etc/s6-overlay/s6-rc.d/{path}/run" in original_text
                assert f"/etc/s6-overlay/s6-rc.d/{path}/run" in script_text
            assert "exec /opt/mimir-worklink/venv/bin/python -m mimir_cultivate.worklink.worker_exec $@" in original_text
            assert "exec /opt/mimir-worklink/venv/bin/python -m mimir_cultivate.worklink.worker_exec $@" in script_text

    def steps(text: str) -> list[str]:
        text = re.sub(r"\\\n\s*", " ", text)
        text = text.replace("mimir.worklink.worker_exec", "mimir_cultivate.worklink.worker_exec")
        text = re.sub(r"(?m)^\s*(?:#.*|ENV .*|ARG .*)$", "", text)
        text = text.replace("&&", "\n").replace("||", "\n").replace(";", "\n")
        commands = []
        for line in text.splitlines():
            command = re.sub(r"^\s*RUN\s+", "", line).strip().strip("{} ")
            command = command.replace('"', "").replace("'", "")
            command = command.removeprefix("then ")
            if not command or command in {"set -eu", "fi"} or command.startswith("#!/"):
                continue
            if command.startswith("if [ ") or command == "then":
                continue
            commands.append(" ".join(command.split()))
        return commands

    for name, original in zip(names, baseline, strict=True):
        expected_steps = steps(original)
        actual_steps = steps((source / name).read_text())
        if name == "executor.sh":
            # Intentional post-extraction changes, each pinned exactly:
            # 1. Cultivate is overlaid from the same verified tree as core, with
            #    --no-deps so its exact mimir-agent pin cannot replace the overlay.
            overlay = "/opt/mimir-worklink/venv/bin/pip install --no-cache-dir --no-deps /opt/mimir-worklink/source"
            expected_steps.insert(expected_steps.index(overlay) + 1, overlay + "/plugins/cultivate")
            # 2. The uv link replaces a uv the scaffold image already installed.
            link = "ln -s /opt/mimir-worklink/venv/bin/uv /usr/local/bin/uv"
            expected_steps[expected_steps.index(link)] = link.replace("ln -s", "ln -sf")
        if name == "service.sh":
            # The original single chmod is intentionally split so scaffold's
            # absent watchdog is optional. Both present services remain required.
            original_chmod = expected_steps.pop()
            assert original_chmod == (
                "chmod +x /etc/s6-overlay/s6-rc.d/mimir/run "
                "/etc/s6-overlay/s6-rc.d/watchdog/run /etc/s6-overlay/s6-rc.d/worklink-execd/run"
            )
            expected_steps += [
                "chmod +x /etc/s6-overlay/s6-rc.d/mimir/run /etc/s6-overlay/s6-rc.d/worklink-execd/run",
                "chmod +x /etc/s6-overlay/s6-rc.d/watchdog/run",
            ]
        assert actual_steps == expected_steps, name


def _run_instructions(text: str) -> list[str]:
    """Each ``RUN`` instruction's shell program, continuation lines kept."""
    programs, current = [], None
    for line in text.splitlines():
        if current is not None:
            current.append(line)
            if not line.rstrip().endswith("\\"):
                programs.append("\n".join(current))
                current = None
        elif line.startswith("RUN "):
            current = [line[4:]]
            if not line.rstrip().endswith("\\"):
                programs.append(current[0])
                current = None
    return programs


@pytest.mark.parametrize("selection,packages,selected,success", [
    ("", "", "0", True),
    ("other", "example-provider==1.0", "0", True),
    ("cultivate", "", "1", True),
    (" cultivate , other", "", "1", True),
    ("all", "", "1", True),
    ("cultivate", "mimir-cultivate==0.9.10", None, False),
    ("", "Mimir_Cultivate==0.9.10", None, False),
])
def test_root_cultivate_selection_executes_verified_build_branches(
    tmp_path, selection, packages, selected, success,
) -> None:
    root = DOCKERFILE.read_text()
    marker_dir = tmp_path / "cultivate-docker"
    marker_dir.mkdir()
    s6 = tmp_path / "s6-rc.d"
    (s6 / "worklink-execd").mkdir(parents=True)
    bundle = s6 / "user/contents.d/worklink-execd"
    bundle.parent.mkdir(parents=True)
    bundle.touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    for command in ("sh", "groupadd", "useradd", "chmod"):
        (bin_dir / command).write_text(f'#!/bin/sh\nprintf "{command} %s\\n" "$*" >> "$COMMAND_LOG"\n')
        (bin_dir / command).chmod(0o755)
    env = {**os.environ, "MIMIR_PLUGIN_SELECTION": selection, "MIMIR_PLUGIN_PACKAGES": packages,
           "COMMAND_LOG": str(log)}

    def run(program: str, *, stubbed: bool) -> subprocess.CompletedProcess[str]:
        program = program.replace("/opt/cultivate-docker", str(marker_dir)).replace(
            "/etc/s6-overlay/s6-rc.d", str(s6))
        path = f"{bin_dir}:{os.environ['PATH']}" if stubbed else os.environ["PATH"]
        return subprocess.run(["/bin/sh", "-c", program], capture_output=True, text=True,
                              env={**env, "PATH": path})

    programs = _run_instructions(root)
    selector, = (p for p in programs if "/opt/cultivate-docker/selected" in p and "printf" in p)
    result = run(selector, stubbed=False)
    assert (result.returncode == 0) is success, result.stderr
    if not success:
        assert "remove it from MIMIR_PLUGIN_PACKAGES and set MIMIR_PLUGIN_SELECTION=cultivate" in result.stderr
        return
    assert (marker_dir / "selected").read_text() == selected + "\n"

    gated = [p for p in programs if "cat /opt/cultivate-docker/selected" in p]
    # users, executor, controller, bootstrap, worker-service removal, assembler pin.
    assert len(gated) == 6
    for program in gated:
        if "plugin_dockerfile import main" in program:
            continue
        assert run(program, stubbed=True).returncode == 0
    calls = log.read_text().splitlines() if log.exists() else []
    scripts = [line.split()[1].rsplit("/", 1)[1] for line in calls if line.startswith("sh ")]
    if selected == "1":
        assert scripts == ["users.sh", "executor.sh", "controller.sh", "bootstrap.sh"]
        assert not any(line.startswith(("groupadd", "useradd")) for line in calls)
    else:
        assert scripts == []
        assert "groupadd --gid 1001 mimir" in calls
        assert "useradd --create-home --uid 1001 --gid mimir --shell /bin/bash mimir" in calls
        assert not any("worklink" in line for line in calls)
    assert (s6 / "worklink-execd").exists() is (selected == "1")
    assert bundle.exists() is (selected == "1")


def test_root_assembler_pins_selected_cultivate_to_the_built_version(tmp_path) -> None:
    program, = (p for p in _run_instructions(DOCKERFILE.read_text())
                if "plugin_dockerfile import main" in p)
    marker_dir = tmp_path / "cultivate-docker"
    marker_dir.mkdir()
    python = tmp_path / "python"
    python.write_text('#!/bin/sh\nif [ "$1" = -c ] && [ "${3:-}" = "" ] && printf %s "$2" | grep -q metadata; '
                      'then echo 9.8.7; else shift 2; printf "%s\\n" "$@" > "$ARGS_LOG"; fi\n')
    python.chmod(0o755)
    for selected, packages, expected in (("1", "", "mimir-cultivate==9.8.7"),
                                         ("1", "third==1.0", "third==1.0,mimir-cultivate==9.8.7"),
                                         ("0", "third==1.0", "third==1.0")):
        (marker_dir / "selected").write_text(selected + "\n")
        log = tmp_path / f"args-{selected}-{packages}"
        local = program.replace("/opt/cultivate-docker", str(marker_dir)).replace(
            "/home/mimir/venv/bin/python", str(python)).replace("&& rm -rf /opt/mimir-image-assembly", "")
        subprocess.run(["/bin/sh", "-c", local], check=True, env={
            **os.environ, "MIMIR_PLUGIN_PACKAGES": packages, "MIMIR_PLUGIN_SELECTION": "",
            "MIMIR_PLUGIN_ALL_INSTALLED": "0", "ARGS_LOG": str(log)})
        args = log.read_text().splitlines()
        assert args[args.index("--packages") + 1] == expected


def test_root_image_never_installs_cultivate_from_an_index() -> None:
    text = _text()
    assert '"mimir-cultivate==' not in text
    assert "pip install --no-cache-dir --no-deps /opt/mimir-worklink/source/plugins/cultivate" in text
    controller = (ROOT / "plugins/cultivate/mimir_cultivate/docker/controller.sh").read_text()
    assert 'origin "$MIMIR_CONTROLLER_COMMIT"' in controller
    assert 'test "$(git -C "$source" rev-parse HEAD)" = "$MIMIR_CONTROLLER_COMMIT"' in controller
    assert 'pip install --no-cache-dir "$core" "$source/plugins/cultivate"' in controller
    assert "npm run build" in controller


def test_apt_install_layer_includes_jq() -> None:
    """jq must be installed in the same ``--no-install-recommends`` apt layer
    that also cleans the cache, so it ships without a sloppy extra layer (#560)."""
    text = _text()
    block = re.search(
        r"apt-get install -y --no-install-recommends(?P<body>[\s\S]*?)apt-get clean",
        text,
    )
    assert block is not None, "could not find the apt install -> clean layer"
    assert re.search(r"(?m)^\s*ca-certificates\b.*\bjq\b", block.group("body")) or re.search(
        r"(?m)^\s+jq\b", block.group("body")
    ), "jq is not listed in the apt install layer"


def test_apt_install_layer_includes_ripgrep() -> None:
    """ripgrep (rg) must be installed so the agent's grep tool uses the fast,
    GIL-free, .gitignore-respecting subprocess rather than deepagents' unbounded
    pure-Python os.walk+regex fallback — which, on large file-tool roots, ran
    for minutes and starved the event loop into an unclean restart (#673)."""
    text = _text()
    block = re.search(
        r"apt-get install -y --no-install-recommends(?P<body>[\s\S]*?)apt-get clean",
        text,
    )
    assert block is not None, "could not find the apt install -> clean layer"
    assert re.search(r"(?m)^\s*ca-certificates\b.*\bripgrep\b", block.group("body")) or re.search(
        r"(?m)^\s+ripgrep\b", block.group("body")
    ), "ripgrep is not listed in the apt install layer"


def test_runtime_preloads_jemalloc_via_arch_independent_symlink() -> None:
    """glibc malloc fragmentation grew the long-running agent to ~2 GB RSS;
    jemalloc held ~550 MB (muninn, 2026-10-06). The package must ship in the
    cleaned apt layer, and LD_PRELOAD must name the build-time symlink rather
    than an arch-specific path (amd64 and arm64 install it in different dirs)."""
    text = _text()
    block = re.search(
        r"apt-get install -y --no-install-recommends(?P<body>[\s\S]*?)apt-get clean",
        text,
    )
    assert block is not None, "could not find the apt install -> clean layer"
    assert re.search(r"\blibjemalloc2\b", block.group("body")), (
        "libjemalloc2 is not listed in the apt install layer"
    )
    assert re.search(
        r"dpkg -L libjemalloc2[^\n]*\n[\s\S]*?ln -sf \"\$lib\" /usr/local/lib/libjemalloc\.so\.2",
        text,
    ), "the jemalloc symlink is not resolved from the installed package"
    assert re.search(r"(?m)^ENV LD_PRELOAD=/usr/local/lib/libjemalloc\.so\.2$", text), (
        "LD_PRELOAD must point at the arch-independent jemalloc symlink"
    )
    assert "-linux-gnu/libjemalloc" not in text, "LD_PRELOAD must not hardcode an arch path"


def _assert_provenance_stage_ancestry(text: str) -> None:
    """Every build target must inherit the validation RUN, not merely copy it."""
    # Join Docker continuation lines before reading instructions. FROM supports
    # an optional platform flag and optional alias; unnamed stages still have
    # numeric indexes that later FROM instructions may inherit.
    logical_lines = re.sub(r"\\\r?\n", " ", text).splitlines()
    stages: list[tuple[str, str | None]] = []
    for line in logical_lines:
        if not re.match(r"^\s*FROM\b", line, re.IGNORECASE):
            continue
        match = re.fullmatch(
            r"\s*FROM\s+(?:--platform=\S+\s+)?(?P<parent>\S+)"
            r"(?:\s+AS\s+(?P<alias>[\w.-]+))?\s*",
            line, re.IGNORECASE,
        )
        assert match is not None, f"unparsed FROM instruction: {line}"
        stages.append((match["parent"], match["alias"]))

    assert stages, "Dockerfile has no image stages"
    assert stages[0] == ("${BASE_IMAGE}", "provenance-validation"), (
        "provenance-validation must be the sole external-image root"
    )
    validated: set[str] = set()
    aliases: set[str] = set()
    for index, (parent, alias) in enumerate(stages):
        if index:
            assert parent.lower() in validated, (
                f"stage {alias or index} bypasses provenance-validation: FROM {parent}"
            )
        if alias is not None:
            normalized = alias.lower()
            assert normalized not in aliases, f"duplicate stage alias: {alias}"
            aliases.add(normalized)
            validated.add(normalized)
        validated.add(str(index))
    # The final stage is Docker's default build target, including when unnamed.
    assert str(len(stages) - 1) in validated, "default target bypasses provenance-validation"


def test_every_image_stage_inherits_provenance_validation() -> None:
    _assert_provenance_stage_ancestry(_text())


def test_provenance_stage_ancestry_accepts_transitive_and_numeric_parents() -> None:
    _assert_provenance_stage_ancestry(
        "FROM ${BASE_IMAGE} AS provenance-validation\n"
        "from --platform=$BUILDPLATFORM provenance-validation as base\n"
        "FROM base AS runtime\n"
        "FROM 2\n"
    )


@pytest.mark.parametrize("mutation", [
    "appended_base_image", "appended_external_image", "appended_scratch",
    "detached_intermediate", "copy_only", "missing_validation", "duplicate_alias",
])
def test_provenance_stage_ancestry_rejects_bypass_mutations(monkeypatch, mutation) -> None:
    text = _text()
    if mutation == "appended_base_image":
        text += "\nFROM ${BASE_IMAGE} AS unvalidated-runtime\nRUN true\n"
    elif mutation == "appended_external_image":
        text += "\nFROM python:3.11-slim AS unvalidated-runtime\nRUN true\n"
    elif mutation == "appended_scratch":
        text += "\nFROM scratch\n"
    elif mutation == "detached_intermediate":
        text = text.replace("FROM provenance-validation AS base", "FROM ${BASE_IMAGE} AS base")
    elif mutation == "copy_only":
        text += "\nFROM scratch AS copied-runtime\nCOPY --from=base /opt /opt\n"
    elif mutation == "missing_validation":
        text = text.replace("AS provenance-validation", "AS unvalidated")
    else:
        text += "\nFROM base AS provenance-validation\n"
    monkeypatch.setattr(f"{__name__}._text", lambda: text)
    with pytest.raises(AssertionError, match="provenance-validation|duplicate stage alias"):
        test_every_image_stage_inherits_provenance_validation()


def _run_provenance_validation(**build_args: str) -> subprocess.CompletedProcess[str]:
    """Execute the actual early-stage shell program, without a registry pull.

    Docker can fail fetching its base before executing any RUN instruction.
    Keep the validation proof offline; the positive built-image test below
    remains the end-to-end Docker integration proof.
    """
    stage = _text().split("FROM ${BASE_IMAGE} AS provenance-validation\n", 1)[1]
    stage = stage.split("FROM provenance-validation AS base\n", 1)[0]
    names = ("MIMIR_GIT_REF", "MIMIR_CONTROLLER_COMMIT", "MIMIR_EXECUTOR_COMMIT")
    for name in names:
        assert f"ARG {name}\n" in stage
    program = stage.split("RUN ", 1)[1].strip().replace("\\\n", "\n")
    # Docker's unset ARGs are empty, regardless of the test process environment.
    env = {**os.environ, **dict.fromkeys(names, ""), **build_args}
    return subprocess.run(
        ["/bin/sh", "-c", program], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=30,
    )


def test_build_without_provenance_args_fails_with_named_error() -> None:
    """The canonical root image must never build without pinned provenance."""
    result = _run_provenance_validation()
    assert result.returncode == 2
    assert "MIMIR_GIT_REF is required" in result.stdout


@pytest.mark.parametrize("build_args,error", [
    ({"MIMIR_GIT_REF": "main"}, "MIMIR_GIT_REF must be a fully qualified"),
    ({"MIMIR_GIT_REF": "refs/heads/main"}, "MIMIR_CONTROLLER_COMMIT must be a full"),
    ({"MIMIR_GIT_REF": "refs/heads/main", "MIMIR_CONTROLLER_COMMIT": "a" * 40},
     "MIMIR_EXECUTOR_COMMIT must be a full"),
    ({"MIMIR_GIT_REF": "refs/heads/main", "MIMIR_CONTROLLER_COMMIT": "a" * 40,
      "MIMIR_EXECUTOR_COMMIT": "b" * 40}, "MIMIR_EXECUTOR_COMMIT must equal"),
])
def test_provenance_validation_rejects_invalid_args(build_args, error) -> None:
    result = _run_provenance_validation(**build_args)
    assert result.returncode == 2
    assert error in result.stdout


def test_provenance_validation_accepts_pinned_args() -> None:
    result = _run_provenance_validation(
        MIMIR_GIT_REF="refs/heads/main", MIMIR_CONTROLLER_COMMIT="a" * 40,
        MIMIR_EXECUTOR_COMMIT="a" * 40,
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
# Hang guard above the subprocess bounds this test already owns (60s ls-remote,
# 1200s build, 60s run). The suite-wide 300s default killed a cold-cache image
# build that was still inside its own 1200s bound.
@pytest.mark.timeout(1500)
def test_built_image_provides_process_tools() -> None:
    """The shipped artifact must provide the process tools used by runbooks."""
    image = f"mimir-process-tools-test:{os.getpid()}"
    try:
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            stdout=subprocess.PIPE, check=True,
        ).stdout.strip()
        source_ref = os.environ.get("MIMIR_GIT_REF") or os.environ.get("GITHUB_REF")
        if not source_ref:
            branch = subprocess.run(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=ROOT,
                text=True, stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            source_ref = subprocess.run(
                ["git", "config", "--get", f"branch.{branch}.merge"], cwd=ROOT,
                text=True, stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
        remote_commit = subprocess.run(
            ["git", "ls-remote", "--exit-code", "origin", source_ref], cwd=ROOT,
            text=True, stdout=subprocess.PIPE, check=True, timeout=60,
        ).stdout.split()[0]
        if remote_commit != source_commit:
            pytest.skip(f"HEAD is not published at {source_ref}")
        subprocess.run(
            [
                "docker", "build",
                "--build-arg", f"MIMIR_GIT_REF={source_ref}",
                "--build-arg", f"MIMIR_CONTROLLER_COMMIT={source_commit}",
                "--build-arg", f"MIMIR_EXECUTOR_COMMIT={source_commit}",
                "--tag", image, ".",
            ],
            cwd=ROOT,
            check=True,
            timeout=1200,
        )
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/sh",
                image,
                "-ceu",
                "command -v ps && command -v pgrep"
                " && test -e /usr/local/lib/libjemalloc.so.2"
                " && python3 -c \"import sys; sys.exit(0 if any('libjemalloc' in l"
                " for l in open('/proc/self/maps')) else 1)\"",
            ],
            check=True,
            timeout=60,
        )
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def test_apt_layer_keeps_cache_hygiene() -> None:
    """The package layer still cleans apt caches (no image bloat regression)."""
    text = _text()
    assert "apt-get clean" in text
    assert "rm -rf /var/lib/apt/lists/*" in text


def test_s6_release_downloads_retry_transient_http_errors() -> None:
    """A transient GitHub release error must not fail the published image build."""
    text = _text()
    block = re.search(
        r'base="https://github.com/just-containers/s6-overlay(?P<body>[\s\S]*?)tar -C /',
        text,
    )
    assert block is not None, "could not find the s6-overlay download layer"
    body = block.group("body")
    assert "--fail" in body
    assert "--show-error" in body
    assert "--retry 5" in body
    assert "--retry-all-errors" in body
    assert body.count("curl ${curl_args}") == 2


def test_claude_code_build_arg_installs_only_adapter_extra() -> None:
    text = _text()
    assert "ARG MIMIR_ENABLE_CLAUDE_CODE=0" in text
    assert "npm install -g @anthropic-ai/claude-code" not in text
    assert 'pip install --no-cache-dir "mimir-agent[claude-code]"' in text
    assert "git+https://github.com/jasoncarreira/langchain-claude-code" not in text


def test_opencode_build_arg_installs_pinned_runtime() -> None:
    """One root-image switch should install OpenCode runtime with pinned plugins."""
    text = _text()
    assert "ARG MIMIR_ENABLE_OPENCODE=0" in text
    assert f"ARG FACTORY_VERSION={FACTORY_VERSION}" in text
    assert "npm install --global --prefix /opt/mimir-opencode" in text
    assert f"opencode-ai@{OPENCODE_VERSION}" in text
    assert '"feature-factory@${FACTORY_VERSION}"' in text
    assert '"opencode-feature-factory@${FACTORY_VERSION}"' in text
    assert "opencode-project-memory@0.1.0" in text
    assert "opencode-openai-codex-auth@4.4.0" in text
    assert "opencode-anthropic-auth@0.0.13" in text
    assert "MIMIR_FACTORY_ENTRYPOINT=/opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js" in text
    assert "prime" not in text.lower()
    assert 'if [ "$MIMIR_ENABLE_OPENCODE" = "1" ]; then' in text
    assert "mimir opencode-bootstrap --home /home/mimir" in text
    assert "OpenCode reads this XDG-global config" in text


def test_worklink_service_run_script_is_made_executable() -> None:
    text = _text()
    assert "/etc/s6-overlay/s6-rc.d/worklink-execd/run" in text


def test_provider_services_are_build_time_opt_in_and_preserve_core_bundle() -> None:
    text = _text()
    assert 'ARG MIMIR_PLUGIN_PACKAGES=""' in text
    assert 'ARG MIMIR_PLUGIN_SELECTION=""' in text
    assert 'ARG MIMIR_PLUGIN_ALL_INSTALLED=0' in text
    assert 'pip install --no-cache-dir "$package"' in text
    assert '--enable "$MIMIR_PLUGIN_SELECTION"' in text
    assert '--all-installed "$MIMIR_PLUGIN_ALL_INSTALLED"' in text
    assert 'sys.path.insert(0, "/opt/mimir-image-assembly"); from mimir.plugin_dockerfile import main; main()' in text
    assert '--destination /etc/s6-overlay/s6-rc.d' in text
    assert '--scripts /usr/local/libexec/mimir-plugin-services' in text
    assert text.index('pip install --no-cache-dir "mimir-agent[${MIMIR_EXTRAS}]"') < text.index('pip install --no-cache-dir "$package"') < text.index('COPY deploy/s6-overlay/s6-rc.d/')
    assert 'COPY deploy/s6-overlay/s6-rc.d/ /etc/s6-overlay/s6-rc.d/' in text
    for service in ("mimir", "watchdog", "worklink-execd"):
        assert f"/etc/s6-overlay/s6-rc.d/{service}/run" in text or service == "mimir" and "deploy/s6-overlay/s6-rc.d/" in text


def test_image_assembly_rejects_missing_selected_package() -> None:
    with pytest.raises(ValueError, match="selected provider packages differ"):
        verify_pins(("missing-provider==1.0",), ())


def test_production_assembler_selects_verified_services_without_replacing_core(tmp_path, monkeypatch) -> None:
    import mimir.plugin_dockerfile as assembly

    package = tmp_path / "package"
    service = package / "services" / "worker"
    service.mkdir(parents=True)
    (package / "mimir-plugin.toml").write_text("test")
    (service / "type").write_text("longrun\n")
    (service / "run").write_text('#!/bin/sh\nprintf "%s" "$MESSAGE" > "$OUTPUT"\nexit 19\n')
    (service / "run").chmod(0o755)
    dist = SimpleNamespace(version="1.4", files=[Path("mimir-plugin.toml")],
                           locate_file=lambda path: package / path)
    candidate = SimpleNamespace(name="sample", distribution="example-provider", enabled=False,
        diagnostic=None, manifest=SimpleNamespace(dockerfile={"s6_services": ["services/worker"]}),
        entry_point=SimpleNamespace(dist=dist, load=lambda: pytest.fail("disabled provider imported")))
    monkeypatch.setattr(assembly, "discover_plugins", lambda *, enable: (
        (SimpleNamespace(**{**vars(candidate), "enabled": enable == "sample"}),)
    ))

    def run(*, pins="", enable="", all_installed="0", destination=None, scripts=None):
        destination = destination or tmp_path / "s6-rc.d"
        scripts = scripts or tmp_path / "scripts"
        bundle = destination / "user/contents.d"
        bundle.mkdir(parents=True, exist_ok=True)
        for name in ("mimir", "watchdog", "worklink-execd"):
            (destination / name).mkdir(exist_ok=True)
            (destination / name / "run").write_text(f"core-{name}\n")
            (bundle / name).touch()
        monkeypatch.setattr(sys, "argv", ["image-assembler", "--packages", pins,
            "--enable", enable, "--all-installed", all_installed,
            "--destination", str(destination), "--scripts", str(scripts)])
        return assemble_main(), destination, scripts

    _, destination, scripts = run()
    assert {p.name for p in (destination / "user/contents.d").iterdir()} == {
        "mimir", "watchdog", "worklink-execd"}
    _, destination, scripts = run(pins="example-provider==1.4")
    assert not (destination / "sample-worker").exists(), "disabled pinned plugin must not install services"
    with pytest.raises(ValueError, match="missing pinned distribution"):
        run(enable="sample")
    with pytest.raises(ValueError, match="selected provider packages differ"):
        run(pins="example-provider==1.5", enable="sample")
    _, destination, scripts = run(pins="example-provider==1.4", enable="sample")
    assert (destination / "sample-worker/type").read_text() == "longrun\n"
    assert (destination / "user/contents.d/sample-worker").exists()
    for name in ("mimir", "watchdog", "worklink-execd"):
        assert (destination / name / "run").read_text() == f"core-{name}\n"
    assert (scripts / "sample/worker/run").stat().st_mode & 0o111
    wrapper = (destination / "sample-worker/run").read_text()
    assert "exec s6-setuidgid mimir /usr/local/libexec/mimir-plugin-services/sample/worker/run" in wrapper

    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "s6-setuidgid").write_text('#!/bin/sh\nprintf "%s" "$1" > "$USER_LOG"\nshift\nexec "$@"\n')
    (shim / "s6-setuidgid").chmod(0o755)
    local_wrapper = tmp_path / "wrapper"
    local_wrapper.write_text(wrapper.replace(
        "/usr/local/libexec/mimir-plugin-services/sample/worker/run", str(scripts / "sample/worker/run")))
    local_wrapper.chmod(0o755)
    output = tmp_path / "output"
    user_log = tmp_path / "user-log"
    result = subprocess.run([str(local_wrapper)], env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}",
        "MESSAGE": "hello", "OUTPUT": str(output), "USER_LOG": str(user_log)}, check=False)
    assert result.returncode == 19
    assert output.read_text() == "hello"
    assert user_log.read_text() == "mimir"

    other_destination = tmp_path / "all/s6-rc.d"
    _, other_destination, _ = run(pins="example-provider==1.4", all_installed="1",
                                  destination=other_destination, scripts=tmp_path / "all/scripts")
    assert (other_destination / "user/contents.d/sample-worker").exists()
