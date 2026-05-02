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
    # Identity reconciliation starter (FUTURE_WORK §6.1).
    identities_yaml = home / "state" / "identities.yaml"
    assert identities_yaml.is_file()
    body = identities_yaml.read_text()
    # Schema example covers all the documented alias prefixes.
    for hint in ("slack-", "discord-", "bsky:", "email:"):
        assert hint in body, f"identities.yaml missing schema hint: {hint}"
    # Empty by default — operator adds entries.
    assert "people: []" in body
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
    # User edits the .env to a minimal version (and includes their own key).
    user_env = "ANTHROPIC_API_KEY=user-key\nMIMIR_API_KEY=user-token\n"
    (home / ".env").write_text(user_env)
    # User adds a custom skill.
    custom = home / ".claude" / "skills" / "my-skill"
    custom.mkdir(parents=True)
    (custom / "SKILL.md").write_text("custom")
    # Re-run setup — must not clobber existing values.
    setup_home(home)
    assert (home / ".env").read_text() == user_env
    assert (custom / "SKILL.md").read_text() == "custom"


def test_setup_env_template_lists_main_keys(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    env_text = (home / ".env").read_text()
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "SAGA_ENDPOINT", "DISCORD_TOKEN"):
        assert key in env_text


def test_setup_generates_api_key_on_first_run(tmp_path: Path):
    home = tmp_path / "agent"
    status = setup_home(home)
    env_text = (home / ".env").read_text()
    line = next(l for l in env_text.splitlines() if l.startswith("MIMIR_API_KEY="))
    value = line.split("=", 1)[1]
    assert len(value) >= 30, f"expected ≥30-char token, got: {value!r}"
    assert status.get("api_key_action") == "generated"


def test_setup_preserves_existing_api_key(tmp_path: Path):
    """Re-running setup mustn't rotate the key — operator may have
    copied it into deployment configs."""
    home = tmp_path / "agent"
    setup_home(home)
    first = next(
        l.split("=", 1)[1]
        for l in (home / ".env").read_text().splitlines()
        if l.startswith("MIMIR_API_KEY=")
    )
    status = setup_home(home)
    second = next(
        l.split("=", 1)[1]
        for l in (home / ".env").read_text().splitlines()
        if l.startswith("MIMIR_API_KEY=")
    )
    assert first == second
    assert status.get("api_key_action") is None


def test_setup_fills_in_blank_api_key(tmp_path: Path):
    """If the operator wipes the key (or pulls a stale .env from git),
    the next setup must fill it in — otherwise the server runs
    unauthenticated."""
    home = tmp_path / "agent"
    setup_home(home)
    env_path = home / ".env"
    body = env_path.read_text()
    blanked = "\n".join(
        ("MIMIR_API_KEY=" if l.startswith("MIMIR_API_KEY=") else l)
        for l in body.splitlines()
    )
    env_path.write_text(blanked + "\n")

    status = setup_home(home)
    line = next(
        l for l in env_path.read_text().splitlines() if l.startswith("MIMIR_API_KEY=")
    )
    assert line.split("=", 1)[1] != ""
    assert status.get("api_key_action") == "generated"


def test_regenerate_api_key_rotates_and_preserves_others(tmp_path: Path):
    from mimir.cli import regenerate_api_key

    home = tmp_path / "agent"
    setup_home(home)
    env_path = home / ".env"
    before = next(
        l.split("=", 1)[1]
        for l in env_path.read_text().splitlines()
        if l.startswith("MIMIR_API_KEY=")
    )
    new_key = regenerate_api_key(home)
    after = env_path.read_text()
    after_key = next(
        l.split("=", 1)[1]
        for l in after.splitlines()
        if l.startswith("MIMIR_API_KEY=")
    )
    assert new_key == after_key
    assert new_key != before
    for unrelated in ("ANTHROPIC_API_KEY=", "MIMIR_WEB_PORT=", "SAGA_ENDPOINT="):
        assert unrelated in after


def test_regenerate_api_key_cli_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture):
    home = tmp_path / "agent"
    setup_home(home)
    capsys.readouterr()  # discard setup output
    main(["regenerate-api-key", "--home", str(home)])
    out = capsys.readouterr()
    # Stdout is the new key alone (pipes cleanly).
    new_key = out.out.strip()
    assert len(new_key) >= 30
    # Stderr carries the explanation.
    assert "Wrote to" in out.err
    # File matches stdout.
    line = next(
        l
        for l in (home / ".env").read_text().splitlines()
        if l.startswith("MIMIR_API_KEY=")
    )
    assert line.split("=", 1)[1] == new_key


def test_regenerate_api_key_cli_errors_when_no_env(
    tmp_path: Path, capsys: pytest.CaptureFixture,
):
    home = tmp_path / "agent"
    home.mkdir()
    with pytest.raises(SystemExit) as exc_info:
        main(["regenerate-api-key", "--home", str(home)])
    assert exc_info.value.code == 1
    assert "no .env" in capsys.readouterr().err


def test_stats_cli_reports_no_turns_for_empty_home(
    tmp_path: Path, capsys: pytest.CaptureFixture,
):
    home = tmp_path / "agent"
    setup_home(home)
    capsys.readouterr()  # discard setup output
    main(["stats", "--home", str(home)])
    assert "no turns recorded" in capsys.readouterr().out


def test_stats_cli_renders_recent_data(tmp_path: Path, capsys: pytest.CaptureFixture):
    import json
    from datetime import datetime, timedelta, timezone

    home = tmp_path / "agent"
    setup_home(home)
    turns = home / "logs" / "turns.jsonl"
    turns.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=timezone.utc)
    rec = {
        "ts": (now - timedelta(minutes=5)).isoformat(),
        "total_cost_usd": 0.42,
        "usage": {
            "input_tokens": 1000,
            "cache_read_input_tokens": 9000,
            "cache_creation_input_tokens": 0,
            "output_tokens": 500,
        },
    }
    turns.write_text(json.dumps(rec) + "\n")

    capsys.readouterr()
    main(["stats", "--home", str(home)])
    out = capsys.readouterr().out
    assert "Last turn:" in out
    assert "Last 5h:" in out
    assert "$0.42" in out
    assert "cache hit 90%" in out


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


# ---- mimir identities {list,add,remove,resolve} ----------------------


def test_identities_list_empty(tmp_path: Path, capsys: pytest.CaptureFixture):
    home = tmp_path
    main(["identities", "list", "--home", str(home)])
    out = capsys.readouterr().out
    assert "(no identities defined)" in out


def test_identities_add_creates_new_canonical(tmp_path: Path, capsys: pytest.CaptureFixture):
    home = tmp_path
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "slack-U05ALICE",
        "--display-name", "Alice Smith",
        "--notes", "Eng team lead",
    ])
    out = capsys.readouterr().out
    assert "added: alice ← slack-U05ALICE" in out

    # File written; resolver picks it up.
    yaml_path = home / "state" / "identities.yaml"
    assert yaml_path.is_file()
    body = yaml_path.read_text()
    assert "alice" in body
    assert "slack-U05ALICE" in body
    assert "Alice Smith" in body
    assert "Eng team lead" in body


def test_identities_add_extends_existing(tmp_path: Path):
    home = tmp_path
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "slack-U05ALICE",
    ])
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "discord-456789",
    ])

    from mimir.identities import IdentityResolver
    r = IdentityResolver(home=home)
    r.reload()
    assert r.resolve("slack-U05ALICE") == "alice"
    assert r.resolve("discord-456789") == "alice"
    # Single canonical entry, two aliases.
    identities = r.all_identities()
    assert len(identities) == 1
    assert set(identities[0].aliases) == {"slack-U05ALICE", "discord-456789"}


def test_identities_add_rejects_alias_collision(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Adding an alias already claimed by a different canonical exits non-zero
    with a clear error — last-wins works at load but the CLI surfaces the
    conflict so the operator notices."""
    home = tmp_path
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "slack-shared",
    ])
    capsys.readouterr()  # drain
    with pytest.raises(SystemExit) as exc_info:
        main([
            "identities", "add", "--home", str(home),
            "--canonical", "bob",
            "--alias", "slack-shared",
        ])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "already maps to canonical 'alice'" in err


def test_identities_remove_alias(tmp_path: Path):
    home = tmp_path
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "slack-U05ALICE",
    ])
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "discord-456",
    ])
    main([
        "identities", "remove", "--home", str(home),
        "--alias", "discord-456",
    ])

    from mimir.identities import IdentityResolver
    r = IdentityResolver(home=home)
    r.reload()
    # Slack alias survives; discord alias is gone.
    assert r.resolve("slack-U05ALICE") == "alice"
    assert r.resolve("discord-456") == "discord-456"  # falls through


def test_identities_remove_canonical(tmp_path: Path):
    home = tmp_path
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "slack-U05ALICE",
    ])
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "bob",
        "--alias", "slack-U07BOB",
    ])
    main([
        "identities", "remove", "--home", str(home),
        "--canonical", "alice",
    ])

    from mimir.identities import IdentityResolver
    r = IdentityResolver(home=home)
    r.reload()
    # Alice gone entirely; bob unchanged.
    assert r.resolve("slack-U05ALICE") == "slack-U05ALICE"
    assert r.resolve("slack-U07BOB") == "bob"


def test_identities_resolve_known(tmp_path: Path, capsys: pytest.CaptureFixture):
    home = tmp_path
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "slack-U05ALICE",
        "--display-name", "Alice Smith",
    ])
    capsys.readouterr()
    main([
        "identities", "resolve", "--home", str(home),
        "slack-U05ALICE",
    ])
    out = capsys.readouterr().out
    assert "slack-U05ALICE → alice" in out
    assert "Alice Smith" in out


def test_identities_resolve_unknown(tmp_path: Path, capsys: pytest.CaptureFixture):
    home = tmp_path
    main(["identities", "resolve", "--home", str(home), "slack-UUNKNOWN"])
    out = capsys.readouterr().out
    assert "slack-UUNKNOWN" in out
    assert "no identity record" in out


def test_identities_list_after_adds(tmp_path: Path, capsys: pytest.CaptureFixture):
    home = tmp_path
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "slack-U05ALICE",
        "--display-name", "Alice Smith",
    ])
    main([
        "identities", "add", "--home", str(home),
        "--canonical", "alice",
        "--alias", "discord-456",
    ])
    capsys.readouterr()
    main(["identities", "list", "--home", str(home)])
    out = capsys.readouterr().out
    assert "alice" in out
    assert "Alice Smith" in out
    assert "slack-U05ALICE" in out
    assert "discord-456" in out
