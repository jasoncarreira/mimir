"""``mimir setup`` and CLI argument plumbing."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mimir.cli import main, setup_home


def test_setup_creates_home_layout(tmp_path: Path):
    home = tmp_path / "agent"
    status = setup_home(home)
    assert (home / "logs").is_dir()
    assert (home / "memory" / "core").is_dir()
    assert (home / "memory" / "channels").is_dir()
    assert (home / "memory" / "shared").is_dir()
    assert (home / "state").is_dir()
    # Wiki layer + raw source store (Karpathy's LLM Wiki pattern).
    assert (home / "state" / "raw").is_dir()
    assert (home / "state" / "wiki" / "entities").is_dir()
    assert (home / "state" / "wiki" / "concepts").is_dir()
    assert (home / "state" / "wiki" / "topics").is_dir()
    assert (home / "messages").is_dir()
    assert (home / ".claude" / "agents").is_dir()
    assert (home / ".claude" / "skills").is_dir()
    # Templates landed.
    assert (home / ".env").is_file()
    assert (home / "scheduler.yaml").is_file()
    assert (home / "memory" / "core" / "identity.md").is_file()
    assert (home / "state" / "wiki" / "AGENTS.md").is_file()
    assert (home / "state" / "wiki" / "index.md").is_file()
    assert (home / "state" / "wiki" / "log.md").is_file()
    # Skills + subagents got seeded.
    assert (home / ".claude" / "skills" / "memory" / "SKILL.md").is_file()
    assert (home / ".claude" / "skills" / "wiki" / "SKILL.md").is_file()
    # Status report covers what we did.
    assert status["home"] == str(home.resolve())
    assert "memory/core" in status["dirs_created"]
    assert "state/wiki/entities" in status["dirs_created"]


def test_setup_is_idempotent_and_preserves_user_edits(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    # User edits the .env.
    user_env = "ANTHROPIC_API_KEY=user-key\n"
    (home / ".env").write_text(user_env)
    # User adds a custom skill.
    custom = home / ".claude" / "skills" / "my-skill"
    custom.mkdir(parents=True)
    (custom / "SKILL.md").write_text("custom")
    # Re-run setup — must not clobber.
    setup_home(home)
    assert (home / ".env").read_text() == user_env
    assert (custom / "SKILL.md").read_text() == "custom"


def test_setup_env_template_lists_main_keys(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    env_text = (home / ".env").read_text()
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "MSAM_ENDPOINT", "DISCORD_TOKEN"):
        assert key in env_text


def test_main_setup_subcommand_runs(tmp_path: Path, capsys: pytest.CaptureFixture):
    home = tmp_path / "agent"
    main(["setup", "--home", str(home)])
    assert (home / ".env").is_file()
    out = capsys.readouterr().out
    assert "mimir home ready at" in out
    assert str(home.resolve()) in out


def test_main_run_subcommand_exports_home_env(tmp_path: Path):
    """``mimir run --home X`` sets ``MIMIR_HOME=X`` before launching the server."""
    home = tmp_path / "agent"
    home.mkdir()
    captured: dict[str, str] = {}

    def fake_run_server() -> None:
        captured["MIMIR_HOME"] = os.environ.get("MIMIR_HOME", "")

    with patch("mimir.server.main", new=fake_run_server):
        main(["run", "--home", str(home)])

    assert captured["MIMIR_HOME"] == str(home.resolve())


def test_main_no_args_runs_server(tmp_path: Path):
    """Bare ``mimir`` — no subcommand — defaults to running the server."""
    called = {"yes": False}

    def fake_run_server() -> None:
        called["yes"] = True

    with patch("mimir.server.main", new=fake_run_server):
        main([])

    assert called["yes"] is True


def test_setup_rejects_non_directory_home(tmp_path: Path):
    """``mimir setup --home <some-file>`` refuses to scaffold over a regular file."""
    target = tmp_path / "not-a-dir"
    target.write_text("i am a file, not a directory")
    with pytest.raises(ValueError, match="not a directory"):
        setup_home(target)
    # The original file is untouched.
    assert target.read_text() == "i am a file, not a directory"
