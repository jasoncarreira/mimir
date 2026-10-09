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
from pathlib import Path

import pytest

from mimir.worklink.backends.feature_factory import FACTORY_VERSION
from mimir.worklink.tool_pins import OPENCODE_VERSION

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
ROOT = DOCKERFILE.parent


def _text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


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


def _run_provenance_validation(**build_args: str) -> subprocess.CompletedProcess[str]:
    """Execute the canonical stage's actual shell guard without pulling an image.

    A negative Docker build can fail at base-image resolution before reaching
    the guard (Docker Hub HTTP 429 in CI). Do not skip or accept that failure:
    exercise the exact RUN command on every host, with no ambient build args.
    The separate built-image test still exercises the complete Docker build.
    """
    text = _text()
    stage = re.search(
        r"(?m)^FROM python:3\.11-slim AS provenance-validation\n"
        r"(?P<body>[\s\S]*?)^FROM provenance-validation AS base$",
        text,
    )
    assert stage is not None, "runtime base must inherit the provenance stage"
    body = stage.group("body")
    arg_names = re.findall(r"(?m)^ARG (\w+)$", body)
    assert arg_names == [
        "MIMIR_GIT_REF", "MIMIR_CONTROLLER_COMMIT", "MIMIR_EXECUTOR_COMMIT",
    ], "provenance arguments must have no permissive defaults"
    commands = re.findall(r"(?m)^RUN ([\s\S]*?)(?=\n[A-Z]+ |\Z)", body)
    assert len(commands) == 1, "expected one canonical provenance guard"
    assert set(build_args) <= set(arg_names)
    return subprocess.run(
        ["/bin/sh", "-c", commands[0]],
        env={"PATH": os.defpath, **dict.fromkeys(arg_names, ""), **build_args},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=10,
    )


def test_build_without_provenance_args_fails_with_named_error() -> None:
    """The canonical root guard must reject missing provenance, even offline."""
    result = _run_provenance_validation()
    assert result.returncode == 2
    assert "MIMIR_GIT_REF is required" in result.stdout


@pytest.mark.parametrize("overrides,error", [
    ({"MIMIR_GIT_REF": "main"}, "MIMIR_GIT_REF must be a fully qualified refs/* name"),
    ({"MIMIR_CONTROLLER_COMMIT": ""}, "MIMIR_CONTROLLER_COMMIT must be a full lowercase Git SHA"),
    ({"MIMIR_CONTROLLER_COMMIT": "A" * 40}, "MIMIR_CONTROLLER_COMMIT must be a full lowercase Git SHA"),
    ({"MIMIR_EXECUTOR_COMMIT": ""}, "MIMIR_EXECUTOR_COMMIT must be a full lowercase Git SHA"),
    ({"MIMIR_EXECUTOR_COMMIT": "abc"}, "MIMIR_EXECUTOR_COMMIT must be a full lowercase Git SHA"),
    ({"MIMIR_EXECUTOR_COMMIT": "b" * 40}, "MIMIR_EXECUTOR_COMMIT must equal MIMIR_CONTROLLER_COMMIT"),
])
def test_provenance_guard_rejects_invalid_args(overrides, error) -> None:
    args = {
        "MIMIR_GIT_REF": "refs/heads/main",
        "MIMIR_CONTROLLER_COMMIT": "a" * 40,
        "MIMIR_EXECUTOR_COMMIT": "a" * 40,
    }
    result = _run_provenance_validation(**{**args, **overrides})
    assert result.returncode == 2
    assert error in result.stdout


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/pull/2326/merge"])
def test_provenance_guard_accepts_matching_full_shas(ref) -> None:
    result = _run_provenance_validation(
        MIMIR_GIT_REF=ref,
        MIMIR_CONTROLLER_COMMIT="a" * 40,
        MIMIR_EXECUTOR_COMMIT="a" * 40,
    )
    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


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
    assert "            feature-factory@${FACTORY_VERSION} \\" in text
    assert "            opencode-feature-factory@${FACTORY_VERSION} \\" in text
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
