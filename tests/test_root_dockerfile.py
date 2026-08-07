"""Validation for the published runtime image (``./Dockerfile``).

The repo root ``Dockerfile`` is the canonical mimir image. These checks keep
operationally-relied-on CLI tools present and the apt layer hygienic so a clean
rebuild can't silently drop them.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


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


def test_apt_layer_keeps_cache_hygiene() -> None:
    """The package layer still cleans apt caches (no image bloat regression)."""
    text = _text()
    assert "apt-get clean" in text
    assert "rm -rf /var/lib/apt/lists/*" in text


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
    assert "npm install -g opencode-ai@1.18.9" in text
    assert "npm install -g opencode-feature-factory@0.2.1" in text
    assert "npm install -g opencode-project-memory@0.1.0" in text
    assert "npm install -g opencode-openai-codex-auth@4.4.0" in text
    assert "npm install -g opencode-anthropic-auth@0.0.13" in text
    assert 'if [ "$MIMIR_ENABLE_OPENCODE" = "1" ]; then' in text
    assert "mimir opencode-bootstrap --home /home/mimir" in text
    assert "OpenCode reads this XDG-global config" in text


def test_dockerfile_creates_the_worklink_user() -> None:
    """Worklink builds run as an identity that cannot write the agent home.

    A build executes repository-controlled code, and the agent home holds
    `scheduler.yaml` and `skills/*/pollers.json` — the files that grant shell
    authority. Same uid means a build can grant itself any binary.
    """
    text = Path("Dockerfile").read_text()
    assert "useradd" in text and "worklink" in text, "no worklink user is created"
    assert "-u 1001 worklink" in text, "the worklink uid must be pinned and distinct"
    assert "chown worklink:worklink /workspace/.worklink" in text, (
        "the contained identity must own its attempt checkouts, or builds break"
    )


def test_worklink_service_runs_as_the_contained_user() -> None:
    """The privilege boundary is the service definition.

    The agent cannot drop privilege (CapEff=0); s6 runs as root and can. If this
    run script stops using s6-setuidgid, containment silently disappears while
    everything still works.
    """
    run = Path("deploy/s6-overlay/s6-rc.d/worklink/run").read_text()
    assert "s6-setuidgid worklink" in run, "the service must drop to the worklink user"
    assert "s6-setuidgid mimir" not in run, "must not run as the agent user"


def test_worklink_service_is_gated_on_the_coding_flag() -> None:
    """A deployment with no coding tools runs no builds and needs no service."""
    run = Path("deploy/s6-overlay/s6-rc.d/worklink/run").read_text()
    assert "MIMIR_CODING_ENABLED" in run


def test_worklink_service_is_registered_with_s6() -> None:
    """A service s6 never starts contains nothing."""
    assert Path("deploy/s6-overlay/s6-rc.d/user/contents.d/worklink").exists()
    assert Path("deploy/s6-overlay/s6-rc.d/worklink/type").read_text().strip() == "longrun"


def test_worklink_service_invokes_a_command_that_exists() -> None:
    """`mimir worklink` has run/run-epic/status/stop — there is no `poll`.

    An earlier draft of this service invoked `mimir worklink poll`, which would
    have failed at boot. Assert against the invented-command class of error.
    """
    run = Path("deploy/s6-overlay/s6-rc.d/worklink/run").read_text()
    # Check INVOCATION, not mention: the script comments explain why `poll` is
    # not used, and a naive substring match flags its own explanation.
    executable = [
        line for line in run.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any("worklink poll" in line for line in executable), (
        "the service invokes a subcommand that does not exist"
    )
