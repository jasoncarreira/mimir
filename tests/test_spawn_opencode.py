"""Tests for ``spawn_open_code`` (chainlink #830 / opencode spec phase 1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.tools.registry import (
    _SPAWN_DEPTH_ENV,
    _spawn_reset_for_tests,
    set_spawn_config,
    spawn_open_code,
)


@pytest.fixture(autouse=True)
def _reset_guard_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _spawn_reset_for_tests()
    monkeypatch.delenv(_SPAWN_DEPTH_ENV, raising=False)
    monkeypatch.delenv("MIMIR_OPENCODE_SPAWN_ARGS", raising=False)


def _capture_run(record: dict):
    def runner(argv, cwd, timeout_s, env=None):
        record["argv"] = list(argv)
        record["cwd"] = cwd
        record["env"] = dict(env or {})
        return record.get("returncode", 0), record.get("stdout", "all done"), record.get("stderr", "")

    return runner


@pytest.mark.asyncio
async def test_completed_run_returns_structured_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_spawn_config({"default_cwd": tmp_path})
    from mimir.tools import registry

    record: dict = {"stdout": "implemented the thing"}
    monkeypatch.setattr(registry, "_run_claude_subprocess", _capture_run(record))

    raw = await spawn_open_code.ainvoke({"prompt": "do the thing", "cwd": str(tmp_path), "model": "anthropic/claude", "agent": "build"})

    payload = json.loads(raw)
    assert payload["status"] == "completed"
    assert payload["result"] == "implemented the thing"
    assert record["argv"][:2] == ["opencode", "run"]
    assert ["--dir", str(tmp_path)] == record["argv"][2:4]
    assert ["-m", "anthropic/claude"] == record["argv"][4:6]
    assert ["--agent", "build"] == record["argv"][6:8]
    # `--` guard so a leading-dash prompt is never parsed as a flag.
    assert record["argv"][-2:] == ["--", "do the thing"]


@pytest.mark.asyncio
async def test_child_env_is_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#494 posture: bridge/operator secrets must NOT reach the child."""
    set_spawn_config({"default_cwd": tmp_path})
    from mimir.tools import registry

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret-bridge-token")
    monkeypatch.setenv("MIMIR_API_KEY", "secret-api-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "oc-pass")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    record: dict = {}
    monkeypatch.setattr(registry, "_run_claude_subprocess", _capture_run(record))

    await spawn_open_code.ainvoke({"prompt": "task", "cwd": str(tmp_path)})

    child = record["env"]
    assert "DISCORD_BOT_TOKEN" not in child
    assert "MIMIR_API_KEY" not in child
    assert child["OPENAI_API_KEY"] == "sk-openai"
    assert child["ANTHROPIC_API_KEY"] == "sk-ant"
    assert child["OPENCODE_SERVER_PASSWORD"] == "oc-pass"
    # opencode is provider-agnostic — the broad provider union must reach it.
    assert child["MINIMAX_API_KEY"] == "mm-key"
    assert child["OPENROUTER_API_KEY"] == "or-key"
    assert child[_SPAWN_DEPTH_ENV] == "1"


@pytest.mark.asyncio
async def test_auth_failure_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_spawn_config({"default_cwd": tmp_path})
    from mimir.tools import registry

    record: dict = {"returncode": 1, "stderr": "provider error: 401 Unauthorized"}
    monkeypatch.setattr(registry, "_run_claude_subprocess", _capture_run(record))

    payload = json.loads(await spawn_open_code.ainvoke({"prompt": "task", "cwd": str(tmp_path)}))
    assert payload["status"] == "auth_failed"
    assert "401" in payload["stderr"]


@pytest.mark.asyncio
async def test_work_failure_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_spawn_config({"default_cwd": tmp_path})
    from mimir.tools import registry

    record: dict = {"returncode": 2, "stderr": "could not apply patch"}
    monkeypatch.setattr(registry, "_run_claude_subprocess", _capture_run(record))

    payload = json.loads(await spawn_open_code.ainvoke({"prompt": "task", "cwd": str(tmp_path)}))
    assert payload["status"] == "work_failed"


@pytest.mark.asyncio
async def test_spawn_failed_when_cli_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_spawn_config({"default_cwd": tmp_path})
    from mimir.tools import registry

    def missing(argv, cwd, timeout_s, env=None):
        raise FileNotFoundError("opencode")

    monkeypatch.setattr(registry, "_run_claude_subprocess", missing)

    payload = json.loads(await spawn_open_code.ainvoke({"prompt": "task", "cwd": str(tmp_path)}))
    assert payload["status"] == "spawn_failed"
    assert "not on PATH" in payload["stderr"]
    assert payload["exit_code"] is None


@pytest.mark.asyncio
async def test_artifacts_written_with_labels_only_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_spawn_config({"default_cwd": tmp_path})
    from mimir.tools import registry

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    record: dict = {"stdout": "done"}
    monkeypatch.setattr(registry, "_run_claude_subprocess", _capture_run(record))

    payload = json.loads(
        await spawn_open_code.ainvoke(
            {"prompt": "build it", "cwd": str(tmp_path), "name": "test-run", "artifact_root": str(tmp_path)}
        )
    )

    run_dir = Path(payload["artifact_dir"])
    assert run_dir.is_dir()
    assert (run_dir / "prompt.md").read_text() == "build it"
    assert (run_dir / "stdout.txt").read_text() == "done"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["status"] == "completed"
    assert manifest["launcher"] == "mimir.spawn_open_code"
    # Labels only — no secret values anywhere in the manifest.
    assert "sk-secret-value" not in (run_dir / "manifest.json").read_text()


def test_confined_artifact_base_confines_to_home(tmp_path: Path) -> None:
    from mimir.tools.registry import _confined_artifact_base

    home = tmp_path / "home"
    home.mkdir()
    (home / "sub").mkdir()

    # home itself and paths under it resolve; escaping paths raise.
    assert _confined_artifact_base(str(home), home) == home.resolve()
    assert _confined_artifact_base(str(home / "sub"), home) == (home / "sub").resolve()

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError):
        _confined_artifact_base(str(outside), home)
    with pytest.raises(ValueError):
        _confined_artifact_base(str(home / ".." / "outside"), home)


@pytest.mark.asyncio
async def test_artifact_root_outside_home_refused_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A model-controlled artifact_root outside the home must be refused up
    # front — before a subprocess runs and before any file is written.
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    set_spawn_config({"default_cwd": home})
    from mimir.tools import registry

    ran = {"spawned": False}

    def runner(argv, cwd, timeout_s, env=None):
        ran["spawned"] = True
        return 0, "done", ""

    monkeypatch.setattr(registry, "_run_claude_subprocess", runner)

    raw = await spawn_open_code.ainvoke(
        {"prompt": "leak it", "artifact_root": str(outside)}
    )
    assert raw.startswith("spawn_open_code refused")
    assert ran["spawned"] is False
    assert not (outside / ".factory").exists()


@pytest.mark.asyncio
async def test_artifact_root_symlink_escape_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside, target_is_directory=True)
    set_spawn_config({"default_cwd": home})
    from mimir.tools import registry

    ran = {"spawned": False}

    def runner(argv, cwd, timeout_s, env=None):
        ran["spawned"] = True
        return 0, "done", ""

    monkeypatch.setattr(registry, "_run_claude_subprocess", runner)

    raw = await spawn_open_code.ainvoke(
        {"prompt": "leak it", "artifact_root": str(home / "escape")}
    )
    assert raw.startswith("spawn_open_code refused")
    assert ran["spawned"] is False
    assert not (outside / ".factory").exists()


@pytest.mark.asyncio
async def test_artifact_root_within_home_subdir_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    sub = home / "artifacts"
    sub.mkdir()
    set_spawn_config({"default_cwd": home})
    from mimir.tools import registry

    monkeypatch.setattr(
        registry, "_run_claude_subprocess", _capture_run({"stdout": "done"})
    )

    payload = json.loads(
        await spawn_open_code.ainvoke(
            {"prompt": "build it", "artifact_root": str(sub)}
        )
    )
    run_dir = Path(payload["artifact_dir"])
    assert run_dir.is_dir()
    assert sub.resolve() in run_dir.resolve().parents
    assert (run_dir / "prompt.md").read_text() == "build it"


@pytest.mark.asyncio
async def test_artifact_write_refuses_symlink_planted_during_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TOCTOU: artifact_root is validated BEFORE the spawn, but the untrusted
    # subprocess runs before the writes and can plant a symlink at the
    # predictable `.factory` prefix. The writes must not follow it out of home.
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    set_spawn_config({"default_cwd": home})
    from mimir.tools import registry

    def runner(argv, cwd, timeout_s, env=None):
        # Simulate the spawned process planting the symlink during its run.
        (home / ".factory").symlink_to(outside, target_is_directory=True)
        return 0, "secret model output", ""

    monkeypatch.setattr(registry, "_run_claude_subprocess", runner)

    payload = json.loads(
        await spawn_open_code.ainvoke(
            {"prompt": "leak me", "artifact_root": str(home)}
        )
    )
    # The spawn still returns a structured result...
    assert payload["status"] == "completed"
    # ...but nothing was written through the planted symlink to outside the home,
    # and no artifact dir is reported (the write was refused, not silently
    # redirected).
    assert list(outside.rglob("*")) == []
    assert payload["artifact_dir"] is None


@pytest.mark.asyncio
async def test_empty_prompt_rejected(tmp_path: Path) -> None:
    set_spawn_config({"default_cwd": tmp_path})
    assert await spawn_open_code.ainvoke({"prompt": "  "}) == "spawn_open_code failed: prompt is required"


def test_registration_gated_on_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir import providers

    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    assert providers.opencode_available() is False
    monkeypatch.setattr(providers.shutil, "which", lambda name: "/usr/bin/opencode")
    assert providers.opencode_available() is True


@pytest.mark.asyncio
async def test_timeout_returns_structured_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as _subprocess

    set_spawn_config({"default_cwd": tmp_path})
    from mimir.tools import registry

    def hangs(argv, cwd, timeout_s, env=None):
        raise _subprocess.TimeoutExpired(argv, timeout_s)

    monkeypatch.setattr(registry, "_run_claude_subprocess", hangs)

    payload = json.loads(
        await spawn_open_code.ainvoke({"prompt": "task", "cwd": str(tmp_path), "timeout_s": 7})
    )
    assert payload["status"] == "timeout"
    assert "7s" in payload["stderr"]
    assert payload["exit_code"] is None
