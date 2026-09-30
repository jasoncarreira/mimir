"""Merged-file boundary and once-only dispatch through a fake social-cli."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


POLLERS = ("social-cli-notifications", "social-cli-feed")
POST = "dispatch:\n  - action: post\n    text: A public update\n"


def git(home: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=home, check=True, capture_output=True)


@pytest.fixture(params=POLLERS)
def setup(tmp_path: Path, monkeypatch, request):
    home = tmp_path / "home"
    home.mkdir()
    poller = request.param
    state = home / "state/pollers" / poller
    state.mkdir(parents=True)
    outbox = home / "state/social-outbox" / poller
    outbox.mkdir(parents=True)
    git(home, "init", "-q")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.org")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.org")
    (home / "seed").write_text("base")
    git(home, "add", "seed")
    git(home, "commit", "-qm", "base")
    binary = tmp_path / "social-cli"
    binary.write_text(f"#!{sys.executable}\n"
                      "import json, os, sys\n"
                      "from pathlib import Path\n"
                      "with open(os.environ['CALLS'], 'a') as f:\n"
                      "    f.write(json.dumps({'argv': sys.argv[1:], 'ledger': Path(os.environ['LEDGER']).read_text() if Path(os.environ['LEDGER']).exists() else ''}) + '\\n')\n"
                      "if os.environ.get('CRASH') and sys.argv[1] == 'dispatch': os._exit(7)\n")
    binary.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("LEDGER", str(state / "dispatched-ledger.jsonl"))
    monkeypatch.setenv("STATE_DIR", str(state))
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("POLLER_NAME", poller)
    monkeypatch.setenv("SOCIAL_CLI_BIN", str(binary))
    monkeypatch.setenv("MIMIR_SOCIAL_PLATFORMS", "bsky")
    module = importlib.import_module("poller" if poller == POLLERS[0] else "feed_poller")
    monkeypatch.setattr(module, "STATE_DIR", state)
    monkeypatch.setattr(module, "CURSOR_FILE", state / "emitted.json")
    monkeypatch.setattr(module, "POLLER_NAME", poller)
    return home, state, outbox, calls, module


def commit_file(home: Path, path: Path, text: str = POST) -> None:
    path.write_text(text)
    git(home, "add", str(path.relative_to(home)))
    git(home, "commit", "-qm", "merge outbox")


def dispatches(calls: Path) -> list[dict]:
    return [record for line in calls.read_text().splitlines()
            if (record := json.loads(line))["argv"][0] == "dispatch"] if calls.exists() else []


def test_merged_clean_once_and_before_sync(setup):
    home, state, root, calls, module = setup
    path = root / "outbox-one.yaml"
    commit_file(home, path)
    assert module.main() == 0
    assert module.main() == 0
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(dispatches(calls)) == 1
    assert records[0]["argv"] == ["dispatch", "--file", str(path)]
    assert len(state.joinpath("dispatched-ledger.jsonl").read_text().splitlines()) == 1
    assert hashlib.sha256(POST.encode()).hexdigest() in records[0]["ledger"]


@pytest.mark.parametrize("case", ["untracked", "dirty", "staged"])
def test_unmerged_files_never_dispatch(setup, case, capsys):
    home, _, root, calls, module = setup
    path = root / "outbox-one.yaml"
    if case in {"dirty", "staged"}:
        commit_file(home, path)
        path.write_text(POST.replace("public", "changed"))
        if case == "staged":
            git(home, "add", str(path.relative_to(home)))
            path.write_text(POST)
    else:
        path.write_text(POST)
    module.main()
    assert dispatches(calls) == []
    assert "unmerged or dirty" in capsys.readouterr().err


@pytest.mark.parametrize("case", ["privacy", "cap"])
def test_scan_and_cap_refuse(setup, case, capsys):
    home, state, root, calls, module = setup
    path = root / "outbox-one.yaml"
    if case == "privacy":
        commit_file(home, path, POST.replace("A public update", "ghp_" + "A" * 36))
    else:
        commit_file(home, path)
        from datetime import datetime, timezone
        import yaml
        now = datetime.now(timezone.utc).isoformat()
        (state / "sent_ledger-bsky.yaml").write_text(yaml.safe_dump([
            {"action": "post", "platform": "bsky", "timestamp": now, "createdId": str(i)}
            for i in range(5)
        ]))
    module.main()
    assert not dispatches(calls)
    assert ("privacy scan refused" if case == "privacy" else "cap check refused") in capsys.readouterr().err


def test_crash_after_ledger_write_cannot_retry(setup, monkeypatch, capsys):
    home, state, root, calls, module = setup
    path = root / "outbox-one.yaml"
    commit_file(home, path)
    monkeypatch.setenv("CRASH", "1")
    module.main()
    assert "dispatch failed" in capsys.readouterr().err
    monkeypatch.delenv("CRASH")
    module.main()
    assert len(dispatches(calls)) == 1
    assert hashlib.sha256(POST.encode()).hexdigest() in dispatches(calls)[0]["ledger"]


def test_cap_reserves_attempted_posts_across_files(setup, capsys):
    home, state, root, calls, module = setup
    for index in range(6):
        commit_file(home, root / f"outbox-{index}.yaml", POST.replace("public", f"public {index}"))
    module.main()
    assert len(dispatches(calls)) == 5
    assert "cap check refused" in capsys.readouterr().err
    module.main()
    assert len(dispatches(calls)) == 5


def test_corrupt_dispatch_ledger_blocks_all_dispatch(setup, capsys):
    home, state, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    (state / "dispatched-ledger.jsonl").write_text('{"sha256":"bad"}\n')
    module.main()
    assert not dispatches(calls)
    assert "invalid dispatched ledger" in capsys.readouterr().err


def test_cap_check_divergence_blocks_even_with_numeric_headroom(setup, capsys):
    from datetime import datetime, timezone

    home, state, root, calls, module = setup
    commit_file(home, root / "outbox-one.yaml")
    archives = state / "outbox_archive"
    archives.mkdir()
    prefix = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    (archives / f"{prefix}-000Z_outbox-bsky.yaml").write_text(
        "dispatch:\n  - post: {text: first}\n  - post: {text: second}\n")
    module.main()
    assert not dispatches(calls)
    assert "cap check refused" in capsys.readouterr().err


def test_real_manifest_has_no_agent_dispatch_capability():
    path = Path(__file__).resolve().parents[1] / "pollers.json"
    manifest = json.loads(path.read_text())
    for poller in manifest["pollers"]:
        authority = poller["authority"]
        assert authority["proposal_surface"] == "social-outbox"
        assert {"open_proposal", "submit_proposal", "abandon_proposal"} <= set(authority["capabilities"])
        assert not {"shell_exec", "bash_jobs_list", "bash_job_output"} & set(authority["capabilities"])
        assert "shell_commands" not in authority
