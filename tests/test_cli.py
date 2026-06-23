"""``mimir setup`` and CLI argument plumbing."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mimir.cli import _print_setup_report, main, setup_home


@pytest.fixture(autouse=True)
def _clear_ambient_model_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep setup-route tests independent from deployment MIMIR_MODEL_SPEC."""
    monkeypatch.delenv("MIMIR_MODEL_SPEC", raising=False)


def test_setup_creates_home_layout(tmp_path: Path):
    home = tmp_path / "agent"
    status = setup_home(home)
    assert (home / "logs").is_dir()
    assert (home / "memory" / "core").is_dir()
    assert (home / "memory" / "channels").is_dir()
    assert (home / "memory" / "issues").is_dir()
    assert (home / "state").is_dir()
    # Wiki layer + raw source store (Karpathy's LLM Wiki pattern).
    assert (home / "state" / "raw").is_dir()
    assert (home / "state" / "wiki" / "entities").is_dir()
    assert (home / "state" / "wiki" / "concepts").is_dir()
    assert (home / "state" / "wiki" / "topics").is_dir()
    assert (home / "messages").is_dir()
    assert (home / ".claude" / "agents").is_dir()
    # Post-2026-05-22: bundled skills live under .mimir_builtin_skills/
    # (read-only refresh target) and operator-installed skills under
    # skills/ (tracked/writable). The legacy .claude/skills/ path is
    # only created as a side-effect of legacy-migration, not by setup.
    assert (home / ".mimir_builtin_skills").is_dir()
    # Templates landed.
    assert (home / ".env").is_file()
    assert (home / "scheduler.yaml").is_file()
    # Numeric prefix matches the convention used by the other core
    # blocks (20-vsm-terms, 30-reflection-policy, 40-learned-behaviors,
    # 50-heartbeat-patterns, 60-filing-rules). load_core renders in
    # filename order, so identity reads first.
    assert (home / "memory" / "core" / "00-identity.md").is_file()
    # 06-action-boundaries seeds the tri-zone action-policy model
    # (autonomous / escalate-first / prohibited). Pairs with the
    # WriteGuardBackend's core-memory read-only gate.
    assert (home / "memory" / "core" / "06-action-boundaries.md").is_file()
    assert (home / "state" / "wiki" / "AGENTS.md").is_file()
    assert (home / "state" / "wiki" / "index.md").is_file()
    assert (home / "state" / "wiki" / "log.md").is_file()
    # Default scheduled-task prompt templates seeded (heartbeat + reflect).
    assert (home / "prompts" / "heartbeat.md").is_file()
    assert (home / "prompts" / "reflect.md").is_file()
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
    assert (home / ".mimir_builtin_skills" / "memory" / "SKILL.md").is_file()
    assert (home / ".mimir_builtin_skills" / "wiki" / "SKILL.md").is_file()
    # Status report covers what we did.
    assert status["home"] == str(home.resolve())
    assert "memory/core" in status["dirs_created"]
    assert "state/wiki/entities" in status["dirs_created"]


def test_setup_banner_reports_effective_env_model_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """chainlink #447: exported MIMIR_MODEL_SPEC beats <home>/.env defaults.

    When it is set before setup, setup's status/banner must report THAT, or it
    contradicts what the agent actually runs. (The mimirbot Codex cutover hit
    exactly this: banner said anthropic while the agent ran codex-plus.)"""
    from mimir.model_registry import PROVIDER_OPENAI

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:gpt-5.4")
    status = setup_home(tmp_path / "agent")  # no --model passed
    assert status["model_spec"] == "codex-plus:gpt-5.4"
    assert status["provider_name"] == PROVIDER_OPENAI
    assert status["billing_mode"] == "subscription"
    assert status["model_spec_from_env"] is True
    # The --model/default route is still surfaced (banner note + the
    # <home>/.env template scaffold).
    assert str(status["setup_default_spec"]).startswith("anthropic:")


def test_setup_banner_uses_default_route_without_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """No MIMIR_MODEL_SPEC in the env → banner reports setup's
    --model/default route (unchanged behavior)."""
    monkeypatch.delenv("MIMIR_MODEL_SPEC", raising=False)
    status = setup_home(tmp_path / "agent")
    assert str(status["model_spec"]).startswith("anthropic:")
    assert status["model_spec_from_env"] is False


def test_setup_is_idempotent_and_preserves_user_edits(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    # User edits the .env to a minimal version (and includes their own keys).
    user_env = (
        "ANTHROPIC_API_KEY=user-key\n"
        "MIMIR_API_KEY=user-token\n"
    )
    (home / ".env").write_text(user_env)
    # User adds a custom skill at the operator-writable location.
    custom = home / "skills" / "my-skill"
    custom.mkdir(parents=True)
    (custom / "SKILL.md").write_text("custom")
    # Re-run setup — must not clobber existing values.
    setup_home(home)
    body = (home / ".env").read_text()
    # Operator's set values must survive untouched.
    assert "ANTHROPIC_API_KEY=user-key" in body
    assert "MIMIR_API_KEY=user-token" in body
    # Setup IS allowed to append missing keys that the agent needs
    # (MIMIR_MODEL_SPEC, MIMIR_COST_HOURLY_LIMIT_USD for API-mode
    # cost monitoring). Operators get a working agent on re-run
    # rather than a silently-broken one that depends on env vars the
    # template forgot to seed.
    assert (custom / "SKILL.md").read_text() == "custom"


def test_setup_writes_saga_toml(tmp_path: Path):
    """v0.5 §2: setup writes a saga.toml with mimir-prod overrides."""
    home = tmp_path / "agent"
    status = setup_home(home)
    saga_toml = home / "saga.toml"
    assert saga_toml.is_file()
    assert "saga.toml" in (status["files_created"] or [])
    body = saga_toml.read_text()
    # mimir-prod overrides — contextual rewrite + two-tier + extraction on.
    assert "enable_contextual_rewrite = true" in body
    assert "two_tier_enabled = true" in body
    assert "enable_extraction = true" in body
    # db_path lives under <home>/.mimir/.
    assert str(home / ".mimir" / "saga.db") in body


# ── --embedding preset tests (PR #144 review nit #4) ─────────────────


def test_setup_embedding_default_is_voyage(tmp_path: Path):
    """Per the Phase 3 LongMemEval cross-bench result, voyage is the
    new canonical default. Status dict records the chosen preset."""
    home = tmp_path / "agent"
    status = setup_home(home)
    assert status["embedding_preset"] == "voyage"
    body = (home / "saga.toml").read_text()
    assert 'provider = "voyage"' in body
    assert 'model = "voyage-4-lite"' in body
    assert 'api_key_env = "VOYAGE_API_KEY"' in body


def test_setup_embedding_openai_preset(tmp_path: Path):
    home = tmp_path / "agent"
    status = setup_home(home, embedding="openai")
    assert status["embedding_preset"] == "openai"
    body = (home / "saga.toml").read_text()
    assert 'provider = "openai"' in body
    assert 'model = "text-embedding-3-small"' in body
    assert 'api_key_env = "OPENAI_API_KEY"' in body


def test_setup_embedding_fastembed_preset(tmp_path: Path):
    """Local-only preset — no api_key_env, fully offline-capable."""
    home = tmp_path / "agent"
    status = setup_home(home, embedding="fastembed")
    assert status["embedding_preset"] == "fastembed"
    body = (home / "saga.toml").read_text()
    assert 'provider = "onnx"' in body
    assert 'model = "BAAI/bge-small-en-v1.5"' in body
    # No api_key_env line — local provider doesn't need one.
    assert "api_key_env" not in body.split("[llm]")[0]


def test_setup_all_presets_write_auto_threshold_sentinel(tmp_path: Path):
    """Regardless of preset, [consolidation] similarity_threshold should
    be the "auto" sentinel — saga resolves per-provider at boot."""
    from mimir.cli import EMBEDDING_PRESETS
    for preset in EMBEDDING_PRESETS:
        home = tmp_path / f"agent_{preset}"
        setup_home(home, embedding=preset)
        body = (home / "saga.toml").read_text()
        assert 'similarity_threshold = "auto"' in body, \
            f"preset {preset} missing auto-threshold sentinel"


def test_setup_invalid_preset_raises(tmp_path: Path):
    home = tmp_path / "agent"
    with pytest.raises(ValueError, match="unknown embedding preset"):
        setup_home(home, embedding="does-not-exist")


def test_setup_writes_no_saga_credentials_or_endpoint(tmp_path: Path):
    """saga runs in-process: setup writes no SAGA_API_KEY anywhere (not the
    .env, not the git-tracked saga.toml — the dead, unread [server] api_key is
    gone), and no longer emits the retired SAGA_ENDPOINT either."""
    import re
    home = tmp_path / "agent"
    setup_home(home)
    env_text = (home / ".env").read_text()
    saga_text = (home / "saga.toml").read_text()
    assert re.search(r"^SAGA_API_KEY=\S", env_text, re.MULTILINE) is None
    assert "SAGA_ENDPOINT" not in env_text
    assert 'api_key = "' not in saga_text
    assert "[server]" not in saga_text


def test_setup_does_not_clobber_existing_saga_toml(tmp_path: Path):
    """Operator edits to saga.toml are preserved on re-run."""
    home = tmp_path / "agent"
    setup_home(home)
    custom_body = "# operator-edited\n[storage]\ndb_path = \"/custom/path\"\n"
    (home / "saga.toml").write_text(custom_body)
    setup_home(home)
    assert (home / "saga.toml").read_text() == custom_body


def test_setup_env_template_lists_main_keys(tmp_path: Path):
    home = tmp_path / "agent"
    setup_home(home)
    env_text = (home / ".env").read_text()
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "DISCORD_TOKEN"):
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


def test_setup_env_is_owner_readable_only(tmp_path: Path):
    """setup_home must create .env with mode 0o600 (not world-readable)."""
    home = tmp_path / "agent"
    setup_home(home)
    mode = (home / ".env").stat().st_mode & 0o777
    assert mode == 0o600, f".env mode {oct(mode)} != 0o600"


def test_setup_env_perms_tightened_on_rerun(tmp_path: Path):
    """setup_home re-run must tighten an existing .env that had wide perms."""
    home = tmp_path / "agent"
    setup_home(home)
    env_path = home / ".env"
    env_path.chmod(0o644)  # simulate wide perms from a prior run or cp
    setup_home(home)
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600, f".env mode {oct(mode)} after re-run != 0o600"


def test_regenerate_api_key_tightens_env_perms(tmp_path: Path):
    """regenerate_api_key must tighten .env to 0o600 after rotating the key."""
    from mimir.cli import regenerate_api_key

    home = tmp_path / "agent"
    setup_home(home)
    env_path = home / ".env"
    env_path.chmod(0o644)  # simulate wide perms
    regenerate_api_key(home)
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600, f".env mode {oct(mode)} after key rotation != 0o600"


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
    for unrelated in ("ANTHROPIC_API_KEY=", "MIMIR_WEB_PORT=", "OPENAI_API_KEY="):
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


def test_run_refuses_without_explicit_home(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
):
    """`mimir run` must refuse to start (exit 1) when neither --home nor
    MIMIR_HOME is set, rather than silently homing on the cwd."""
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main(["run"])
    assert exc_info.value.code == 1
    assert "MIMIR_HOME is not set" in capsys.readouterr().err


def test_run_proceeds_with_home_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """--home sets MIMIR_HOME and the server starts (mocked)."""
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    called: dict[str, bool] = {}
    monkeypatch.setattr("mimir.server.main", lambda: called.setdefault("ran", True))
    main(["run", "--home", str(tmp_path)])
    assert called.get("ran") is True
    assert os.environ["MIMIR_HOME"] == str(tmp_path.resolve())


def test_run_proceeds_with_mimir_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """An explicit MIMIR_HOME (no --home) is accepted."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    called: dict[str, bool] = {}
    monkeypatch.setattr("mimir.server.main", lambda: called.setdefault("ran", True))
    main(["run"])
    assert called.get("ran") is True


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


def test_print_setup_report_surfaces_credential_helper_fields(capsys):
    """PR 4d added credentials_written + legacy_token_url_migrated to
    BootstrapResult. The setup report must surface both so the operator
    can see whether the credential helper was installed and whether a
    legacy in-URL token was stripped during migration."""
    status = {
        "home": "/tmp/test-home",
        "dirs_created": [],
        "files_created": [],
        "skills": {},
        "subagents": {},
        "git_bootstrap": {
            "initialized": False,
            "cloned": False,
            "pulled": True,
            "pull_blocked": False,
            "bootstrap_commit": False,
            "gitignore_written": False,
            "hook_written": True,
            "remote_configured": True,
            "credentials_written": True,
            "legacy_token_url_migrated": True,
        },
    }
    _print_setup_report(status)
    out = capsys.readouterr().out
    assert "credential helper installed" in out
    assert "legacy in-URL token stripped" in out


def test_print_setup_report_omits_credential_helper_lines_when_absent(capsys):
    """When credentials_written / legacy_token_url_migrated are False
    (existing repo, helper already in place, no migration needed) the
    report should stay quiet about them — only positive actions print."""
    status = {
        "home": "/tmp/test-home",
        "dirs_created": [],
        "files_created": [],
        "skills": {},
        "subagents": {},
        "git_bootstrap": {
            "initialized": False,
            "cloned": False,
            "pulled": True,
            "pull_blocked": False,
            "bootstrap_commit": False,
            "gitignore_written": False,
            "hook_written": True,
            "remote_configured": True,
            "credentials_written": False,
            "legacy_token_url_migrated": False,
        },
    }
    _print_setup_report(status)
    out = capsys.readouterr().out
    assert "credential helper" not in out
    assert "legacy in-URL token" not in out


def test_print_setup_report_surfaces_initial_push(capsys):
    """PR 4e added initial_push to BootstrapResult. When True, the
    report should mention that the remote main was created via the
    initial ``git push -u``."""
    status = {
        "home": "/tmp/test-home",
        "dirs_created": [],
        "files_created": [],
        "skills": {},
        "subagents": {},
        "git_bootstrap": {
            "initialized": True,
            "cloned": False,
            "pulled": False,
            "pull_blocked": False,
            "bootstrap_commit": True,
            "gitignore_written": True,
            "hook_written": True,
            "remote_configured": True,
            "credentials_written": True,
            "legacy_token_url_migrated": False,
            "upstream_set": True,
            "initial_push": True,
        },
    }
    _print_setup_report(status)
    out = capsys.readouterr().out
    assert "initial push" in out
    assert "created remote main" in out
    # initial_push subsumes upstream_set — only the more specific
    # phrase prints.
    assert "upstream tracking set" not in out


def test_print_setup_report_surfaces_upstream_set_without_push(capsys):
    """When upstream_set is True but initial_push is False (case 2:
    remote already had main, just set tracking), the report shows the
    less specific phrase."""
    status = {
        "home": "/tmp/test-home",
        "dirs_created": [],
        "files_created": [],
        "skills": {},
        "subagents": {},
        "git_bootstrap": {
            "initialized": False,
            "cloned": False,
            "pulled": True,
            "pull_blocked": False,
            "bootstrap_commit": False,
            "gitignore_written": False,
            "hook_written": True,
            "remote_configured": True,
            "credentials_written": True,
            "legacy_token_url_migrated": False,
            "upstream_set": True,
            "initial_push": False,
        },
    }
    _print_setup_report(status)
    out = capsys.readouterr().out
    assert "upstream tracking set" in out
    assert "initial push" not in out


def test_print_setup_report_omits_upstream_lines_when_absent(capsys):
    """When upstream_set / initial_push are both False, neither phrase
    appears (e.g. tracking already configured by an earlier run)."""
    status = {
        "home": "/tmp/test-home",
        "dirs_created": [],
        "files_created": [],
        "skills": {},
        "subagents": {},
        "git_bootstrap": {
            "initialized": False,
            "cloned": False,
            "pulled": True,
            "pull_blocked": False,
            "bootstrap_commit": False,
            "gitignore_written": False,
            "hook_written": True,
            "remote_configured": True,
            "credentials_written": False,
            "legacy_token_url_migrated": False,
            "upstream_set": False,
            "initial_push": False,
        },
    }
    _print_setup_report(status)
    out = capsys.readouterr().out
    assert "upstream tracking" not in out
    assert "initial push" not in out




# ─── --model flag (auto-routing via model_registry) ─────────────────────


def test_setup_default_model_routes_to_anthropic_api(tmp_path: Path):
    """No ``--model`` → default to direct Anthropic API
    (``anthropic:claude-sonnet-4-6``). Forward-looking default since
    Anthropic is sunsetting claude-code subscription plans."""
    home = tmp_path / "h"
    status = setup_home(home)
    env = (home / ".env").read_text()
    assert "MIMIR_MODEL_SPEC=anthropic:claude-sonnet-4-6" in env
    assert status["model_spec"] == "anthropic:claude-sonnet-4-6"
    assert status["provider_name"] == "anthropic-api"
    assert status["billing_mode"] == "api"


def test_setup_max_oauth_routes_claude_to_claude_code(tmp_path: Path):
    """``--subscription`` opts INTO the legacy Max OAuth path for
    operators with active Max plans."""
    home = tmp_path / "h"
    status = setup_home(home, subscription=True)
    env = (home / ".env").read_text()
    assert "MIMIR_MODEL_SPEC=claude-code:claude-sonnet-4-6" in env
    assert status["model_spec"] == "claude-code:claude-sonnet-4-6"
    assert status["provider_name"] == "anthropic-max"
    assert status["billing_mode"] == "subscription"


def test_setup_minimax_model_injects_anthropic_compat_base_url(tmp_path: Path):
    home = tmp_path / "h"
    status = setup_home(home, model="MiniMax-M2.7")
    env = (home / ".env").read_text()
    assert "MIMIR_MODEL_SPEC=anthropic:MiniMax-M2.7" in env
    assert "ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic" in env
    assert status["provider_name"] == "minimax"
    assert status["billing_mode"] == "api"


def test_setup_kimi_model_injects_moonshot_base_url(tmp_path: Path):
    home = tmp_path / "h"
    status = setup_home(home, model="kimi-k2-0905-preview")
    env = (home / ".env").read_text()
    assert "ANTHROPIC_BASE_URL=https://api.moonshot.ai/anthropic" in env
    assert status["provider_name"] == "moonshot"


def test_setup_openai_model_no_base_url_override(tmp_path: Path):
    home = tmp_path / "h"
    status = setup_home(home, model="gpt-4.1-mini")
    env = (home / ".env").read_text()
    assert "MIMIR_MODEL_SPEC=openai:gpt-4.1-mini" in env
    assert status["provider_name"] == "openai"


def test_setup_model_preserves_operator_value_on_rerun(tmp_path: Path):
    """Idempotent — operator's manual MIMIR_MODEL_SPEC edit survives
    a re-run with the original ``--model``."""
    home = tmp_path / "h"
    setup_home(home, model="claude-sonnet-4-6")
    env_path = home / ".env"
    body = env_path.read_text().replace(
        "MIMIR_MODEL_SPEC=anthropic:claude-sonnet-4-6",
        "MIMIR_MODEL_SPEC=openai:gpt-5-nano",
    )
    env_path.write_text(body)
    setup_home(home, model="claude-sonnet-4-6")
    final = env_path.read_text()
    assert "MIMIR_MODEL_SPEC=openai:gpt-5-nano" in final
    assert "MIMIR_MODEL_SPEC=anthropic:claude-sonnet-4-6" not in final


# ─── usage-monitor auto-wiring (no --quota flag needed) ─────────────────


def test_setup_api_route_writes_cost_ceiling(tmp_path: Path):
    """API-mode routes get the default ``MIMIR_COST_HOURLY_LIMIT_USD``
    written automatically (no --quota flag needed). Spike-ratio check
    is always-on by default in cost_tracking.py — this gives operators
    a sensible alert threshold so unexpected $/hr spikes fire."""
    home = tmp_path / "h"
    status = setup_home(home, model="MiniMax-M2.7")
    env = (home / ".env").read_text()
    assert "MIMIR_COST_HOURLY_LIMIT_USD=5.0" in env
    assert "cost monitoring" in status["monitor_status"]


def test_setup_subscription_route_writes_quota_poll_flag(tmp_path: Path):
    """Subscription-mode routes get ``MIMIR_QUOTA_POLL_ENABLED=1`` so
    the runtime registers the OAuth usage poller at boot."""
    home = tmp_path / "h"
    status = setup_home(home, subscription=True)
    env = (home / ".env").read_text()
    assert "MIMIR_QUOTA_POLL_ENABLED=1" in env
    assert "quota poller" in status["monitor_status"]


def test_setup_preserves_operator_monitor_override_on_rerun(tmp_path: Path):
    """Operator manually set MIMIR_COST_HOURLY_LIMIT_USD=25.0 to a
    higher ceiling. A re-run mustn't clobber that operator value."""
    home = tmp_path / "h"
    setup_home(home, model="claude-sonnet-4-6")
    env_path = home / ".env"
    body = env_path.read_text().replace(
        "MIMIR_COST_HOURLY_LIMIT_USD=5.0",
        "MIMIR_COST_HOURLY_LIMIT_USD=25.0",
    )
    env_path.write_text(body)
    setup_home(home, model="claude-sonnet-4-6")
    final = env_path.read_text()
    assert "MIMIR_COST_HOURLY_LIMIT_USD=25.0" in final
    assert "MIMIR_COST_HOURLY_LIMIT_USD=5.0" not in final


def test_setup_overrides_zero_default_to_active_ceiling(tmp_path: Path):
    """The .env template ships with ``MIMIR_COST_HOURLY_LIMIT_USD=`` empty.
    Setup treats empty / 0 / 0.0 as "not set" and writes the active
    default — otherwise operators would have to manually bump it from
    zero (which disables the alert)."""
    home = tmp_path / "h"
    # Empty .env (post-template) has MIMIR_COST_HOURLY_LIMIT_USD= empty.
    setup_home(home, model="claude-sonnet-4-6")
    env = (home / ".env").read_text()
    # Setup wrote the active default.
    assert "MIMIR_COST_HOURLY_LIMIT_USD=5.0" in env


# ─── dead-env removal sanity ────────────────────────────────────────────


def test_setup_does_not_write_dead_MIMIR_MODEL_env_var(tmp_path: Path):
    """``MIMIR_MODEL`` is SDK-era; deepagents path reads only
    ``MIMIR_MODEL_SPEC``. The .env template used to write
    ``MIMIR_MODEL=claude-opus-4-7`` which silently confused operators
    (the value didn't do anything). Make sure it's gone."""
    home = tmp_path / "h"
    setup_home(home)
    env = (home / ".env").read_text()
    for line in env.splitlines():
        stripped = line.strip()
        if stripped.startswith("MIMIR_MODEL=") and not stripped.startswith(
            "MIMIR_MODEL_SPEC"
        ):
            pytest.fail(f"unexpected dead env line: {stripped!r}")


# ---------------------------------------------------------------------------
# ``mimir feedback mark-resolved`` CLI (chainlink #198)
# ---------------------------------------------------------------------------

import json  # noqa: E402 — already imported by helpers above; fine here


def _run_feedback_cmd(argv: list[str]) -> int:
    """Call ``main(argv)``; return the SystemExit code (0 on success)."""
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    return exc_info.value.code


def test_feedback_mark_resolved_writes_jsonl(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``mimir feedback mark-resolved`` appends a valid JSONL rule and
    prints a confirmation."""
    incidents = tmp_path / "resolved-incidents.jsonl"
    rc = _run_feedback_cmd(
        [
            "feedback",
            "mark-resolved",
            "--type", "error",
            "--pattern", "langchain-claude-code",
            "--reason", "start.sh now installs the package",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "marked resolved" in captured.out
    assert incidents.exists()
    rules = [json.loads(line) for line in incidents.read_text().splitlines() if line.strip()]
    assert len(rules) == 1
    assert rules[0]["event_type"] == "error"
    assert rules[0]["pattern"] == "langchain-claude-code"
    assert rules[0]["reason"] == "start.sh now installs the package"
    assert "resolved_at" in rules[0]


def test_feedback_mark_resolved_custom_resolved_at(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``--resolved-at`` stores the supplied (normalised) timestamp."""
    _run_feedback_cmd(
        [
            "feedback",
            "mark-resolved",
            "--type", "error",
            "--pattern", "",
            "--reason", "test fix",
            "--resolved-at", "2026-05-25T12:00:00",
            "--home", str(tmp_path),
        ]
    )
    incidents = tmp_path / "resolved-incidents.jsonl"
    rules = [json.loads(l) for l in incidents.read_text().splitlines() if l.strip()]
    # Naive stamp should be stored as UTC-aware
    assert rules[0]["resolved_at"] == "2026-05-25T12:00:00+00:00"


def test_feedback_mark_resolved_invalid_resolved_at_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A non-ISO --resolved-at exits with code 1 and doesn't write."""
    rc = _run_feedback_cmd(
        [
            "feedback",
            "mark-resolved",
            "--type", "error",
            "--reason", "test",
            "--resolved-at", "not-a-date",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 1
    assert not (tmp_path / "resolved-incidents.jsonl").exists()


def test_feedback_mark_resolved_dry_run_no_write(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``--dry-run`` prints preview but does not write the file."""
    rc = _run_feedback_cmd(
        [
            "feedback",
            "mark-resolved",
            "--type", "*",
            "--reason", "test",
            "--dry-run",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "dry-run" in captured.out
    assert not (tmp_path / "resolved-incidents.jsonl").exists()


def test_feedback_mark_resolved_unknown_type_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """An unknown --type emits a warning but still writes the rule."""
    rc = _run_feedback_cmd(
        [
            "feedback",
            "mark-resolved",
            "--type", "totally_unknown_event",
            "--reason", "test",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower() or "warning" in captured.out.lower()
    incidents = tmp_path / "resolved-incidents.jsonl"
    assert incidents.exists()


# ``mimir feedback emit`` CLI (chainlink #218)
# ---------------------------------------------------------------------------


def test_feedback_emit_writes_event_to_jsonl(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``mimir feedback emit`` appends one valid JSON record to events.jsonl."""
    rc = _run_feedback_cmd(
        [
            "feedback",
            "emit",
            "pr_merge_blocked_by_changes_requested",
            "pr=42",
            "author=mimir-bot",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "emitted" in captured.out
    assert "pr_merge_blocked_by_changes_requested" in captured.out

    events_path = tmp_path / "logs" / "events.jsonl"
    assert events_path.exists()
    records = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["type"] == "pr_merge_blocked_by_changes_requested"
    assert rec["pr"] == "42"
    assert rec["author"] == "mimir-bot"
    assert rec["session_id"] == "cli"
    assert "timestamp" in rec


def test_feedback_emit_no_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``mimir feedback emit`` works with no KEY=VALUE pairs."""
    rc = _run_feedback_cmd(
        [
            "feedback",
            "emit",
            "git_push_ok",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 0
    events_path = tmp_path / "logs" / "events.jsonl"
    records = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["type"] == "git_push_ok"
    # No extra keys beyond the standard envelope fields
    assert "pr" not in records[0]


def test_feedback_emit_invalid_pair_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A KEY=VALUE pair missing '=' exits with code 1 and writes nothing."""
    rc = _run_feedback_cmd(
        [
            "feedback",
            "emit",
            "git_push_ok",
            "bad-pair",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "error" in captured.err.lower()
    assert not (tmp_path / "logs" / "events.jsonl").exists()


def test_feedback_emit_unknown_type_warns_but_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """An unrecognised event type emits a warning but still writes the event."""
    rc = _run_feedback_cmd(
        [
            "feedback",
            "emit",
            "totally_custom_event_type",
            "key=val",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    events_path = tmp_path / "logs" / "events.jsonl"
    assert events_path.exists()
    records = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert records[0]["type"] == "totally_custom_event_type"
    assert records[0]["key"] == "val"


def test_feedback_emit_json_values_parses_structured_data(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``--json-values`` JSON-parses each value so lists and ints are stored
    as proper JSON types, not strings."""
    rc = _run_feedback_cmd(
        [
            "feedback",
            "emit",
            "pr_merge_blocked_by_changes_requested",
            'blocking_reviewers=["jasoncarreira"]',
            "pr=42",
            "--json-values",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 0
    events_path = tmp_path / "logs" / "events.jsonl"
    records = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["blocking_reviewers"] == ["jasoncarreira"]  # list, not string
    assert rec["pr"] == 42  # int, not string


def test_feedback_emit_json_values_rejects_malformed_json(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``--json-values`` exits 1 and prints an error when a value is not valid JSON.

    Silently falling back to a string would hide bugs, so we reject explicitly.
    """
    rc = _run_feedback_cmd(
        [
            "feedback",
            "emit",
            "pr_merge_blocked_by_changes_requested",
            "blocking_reviewers=jasoncarreira,bob",  # not JSON
            "--json-values",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "json-values" in captured.err.lower() or "json" in captured.err.lower()
    assert not (tmp_path / "logs" / "events.jsonl").exists()


def test_feedback_emit_without_json_values_stores_strings(
    tmp_path: Path,
) -> None:
    """Without ``--json-values``, values are stored as plain strings (backwards compat)."""
    rc = _run_feedback_cmd(
        [
            "feedback",
            "emit",
            "pr_merge_blocked_by_changes_requested",
            "pr=42",
            "--home", str(tmp_path),
        ]
    )
    assert rc == 0
    events_path = tmp_path / "logs" / "events.jsonl"
    records = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    # Without --json-values, "42" stays a string
    assert records[0]["pr"] == "42"


def test_notify_restart_cli_dispatches(monkeypatch):
    """`mimir notify-restart` wires --unit/--detail through to
    notify_service_event and runs it (systemd OnFailure= hook path)."""
    import mimir.liveness as liveness

    recorded: dict = {}

    async def fake_notify(*, unit=None, detail=None, _post=None):
        recorded["unit"] = unit
        recorded["detail"] = detail

    monkeypatch.setattr(liveness, "notify_service_event", fake_notify)
    main(["notify-restart", "--unit", "mimir.service", "--detail", "exit 137"])
    assert recorded == {"unit": "mimir.service", "detail": "exit 137"}
