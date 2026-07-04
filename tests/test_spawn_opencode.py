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
    record: dict = {}
    monkeypatch.setattr(registry, "_run_claude_subprocess", _capture_run(record))

    await spawn_open_code.ainvoke({"prompt": "task", "cwd": str(tmp_path)})

    child = record["env"]
    assert "DISCORD_BOT_TOKEN" not in child
    assert "MIMIR_API_KEY" not in child
    assert child["OPENAI_API_KEY"] == "sk-openai"
    assert child["ANTHROPIC_API_KEY"] == "sk-ant"
    assert child["OPENCODE_SERVER_PASSWORD"] == "oc-pass"
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

    result = await spawn_open_code.ainvoke({"prompt": "task", "cwd": str(tmp_path)})
    assert result == "spawn_open_code failed: 'opencode' CLI not on PATH"


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
