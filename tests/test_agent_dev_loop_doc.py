"""Executable checks for the copy-paste examples in docs/agent-dev-loop.md.

The runbook is meant to be followed verbatim by an operator's coding agent, so its
configuration examples and PR-watcher scripts are extracted and run here: the
repositories.yaml + worklink.yaml examples must load through mimir's real loaders and
build the OpenCode backend, and the watcher must not lose a PR that arrives between a
notification and its acknowledgement.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from mimir.repository_config import RepositoryInventory
from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig

DOC = Path(__file__).resolve().parents[1] / "docs" / "agent-dev-loop.md"
SLUG = "acme/widget"


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _yaml_block_after(marker: str) -> str:
    text = _doc()
    match = re.search(r"```yaml\n(.*?)```", text[text.index(marker):], re.S)
    assert match, f"no yaml block after {marker!r}"
    return match.group(1)


def _script(name: str) -> str:
    match = re.search(rf"```bash\n(#!/bin/bash\n# {name}\.sh.*?)```", _doc(), re.S)
    assert match, f"{name}.sh not found in the runbook"
    return match.group(1)


def _home_with_examples(tmp_path: Path, *, bash_allowlist: list[str] | None = None) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    root = tmp_path / "workspace" / "widget"
    if not root.exists():
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"https://github.com/{SLUG}.git"],
            check=True,
        )

    def fill(text: str) -> str:
        return text.replace("<owner/repo>", SLUG).replace("/workspace/<repo>", str(root))

    (home / "repositories.yaml").write_text(fill(_yaml_block_after("Describe the repo")))
    worklink = yaml.safe_load(fill(_yaml_block_after("Configure builds")))
    if bash_allowlist is not None:
        worklink.setdefault("backends", {})["opencode"] = {"bash_allowlist": bash_allowlist}
    (home / "worklink.yaml").write_text(yaml.safe_dump(worklink))
    return home


def _opencode(home: Path):
    config = WorklinkConfig.load(home / "worklink.yaml")
    return config, BackendRegistry(config).get("opencode")


def test_documented_repository_and_worklink_examples_load(tmp_path: Path) -> None:
    home = _home_with_examples(tmp_path)
    inventory = RepositoryInventory.load(home / "repositories.yaml")
    assert [repo.slug for repo in inventory.repositories] == [SLUG]

    config, backend = _opencode(home)
    assert config.defaults.test_command == "uv run pytest -q"
    # The doc says the derived allowlist for this test command is ["git *", "uv *"].
    assert tuple(backend.bash_allowlist) == ("git *", "uv *")
    assert '`["git *", "uv *"]`' in _doc()


def test_documented_custom_allowlist_is_accepted(tmp_path: Path) -> None:
    _, backend = _opencode(_home_with_examples(tmp_path, bash_allowlist=["git *", "uv run pytest *"]))
    assert tuple(backend.bash_allowlist) == ("git *", "uv run pytest *")


def test_documented_anchored_pattern_pitfall_is_refused(tmp_path: Path) -> None:
    home = _home_with_examples(tmp_path, bash_allowlist=["uv run pytest"])
    with pytest.raises(ValueError, match="test_command is refused by backends.opencode.bash_allowlist"):
        _opencode(home)


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("comm") is None, reason="needs bash and comm")
def test_watcher_reports_a_pr_that_arrives_before_the_ack(tmp_path: Path) -> None:
    for name in ("watch_prs", "ack_prs"):
        path = tmp_path / f"{name}.sh"
        path.write_text(_script(name))
        path.chmod(0o755)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    prs = tmp_path / "prs.txt"
    # Fake `gh pr list ... --jq`: emit "number=sha" lines from prs.txt.
    (fakebin / "gh").write_text(
        '#!/bin/bash\nwhile read -r n sha; do [ -n "$n" ] && echo "$n=$sha"; done < "$FAKE_PRS"\n'
    )
    (fakebin / "gh").chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(fake_home),
        "FAKE_PRS": str(prs),
    }
    sha_a, sha_b = "a" * 40, "b" * 40

    def watch() -> str:
        result = subprocess.run(
            [str(tmp_path / "watch_prs.sh"), SLUG, "bot"],
            env=env, capture_output=True, text=True, timeout=10, check=True,
        )
        return result.stdout.strip()

    prs.write_text(f"1 {sha_a}\n")
    reported = watch()
    assert reported == f"1={sha_a}"

    # PR B opens after the notification but before the acknowledgement.
    prs.write_text(f"1 {sha_a}\n2 {sha_b}\n")
    subprocess.run([str(tmp_path / "ack_prs.sh"), SLUG, reported], env=env, check=True, timeout=10)

    # The re-armed watcher must report B at once rather than treat it as handled.
    assert watch() == f"2={sha_b}"
    state = fake_home / ".cache" / "pr-watch" / "acme_widget.acked"
    assert state.read_text().split() == [f"1={sha_a}"]
