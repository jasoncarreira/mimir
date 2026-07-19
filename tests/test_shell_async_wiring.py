"""181-K regression: ``bash_async`` / ``bash_jobs_list`` / ``bash_job_output``.

The SDK build had three async-shell tools backed by ``ShellJobRegistry``
for long-running subprocesses (CI waits, webhook listeners, multi-
hour climbs). The deepagents cutover dropped the @mcp_tool registrations
without re-wiring them as @tool callables — only the synchronous
``shell_exec`` survived, which blocks the dispatcher for the entire
subprocess lifetime.

181-K ports the three back as native langchain ``@tool``s in
``mimir/tools/shell_async.py``, wires them in via
``set_shell_job_registry(...)`` from ``server.py``, and restores
``Agent._handle_shell_job_complete`` / ``_on_shell_job_complete``
so the spawning channel wakes when a job exits.

These tests exercise the surface end-to-end with a real
``ShellJobRegistry`` (no subprocess spawn — we monkey-patch
``ShellJobRegistry.spawn`` to a controllable stand-in) so we cover
the wiring without taking on subprocess flakiness in CI.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mimir.shell_jobs import ShellJobRegistry
from mimir.tools import shell_async


class _FakeJob:
    """Stand-in for the ShellJob dataclass — just enough fields for
    the tool surface + completion-bridge tests."""

    def __init__(
        self,
        job_id: str = "job-1",
        pid: int = 12345,
        command: str = "echo hi",
        channel_id: str | None = "ch-1",
        status: str = "running",
        exit_code: int | None = None,
        started_at: float = 0.0,
    ) -> None:
        self.job_id = job_id
        self.pid = pid
        self.command = command
        self.channel_id = channel_id
        self.status = status
        self.exit_code = exit_code
        self.elapsed_seconds = 1.5
        self.started_at = started_at


@pytest.fixture
def fake_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ShellJobRegistry:
    """Wire a real ShellJobRegistry into ``shell_async`` but patch
    ``spawn`` to return a deterministic _FakeJob without touching the OS."""
    reg = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    spawned: list[dict] = []

    def _fake_spawn(
        command: str, *, argv: list[str], channel_id: str | None,
        on_complete=None, auth_context=None,
    ) -> _FakeJob:
        spawned.append({"command": command, "argv": argv, "channel_id": channel_id})
        return _FakeJob(command=command, channel_id=channel_id)

    monkeypatch.setattr(reg, "spawn", _fake_spawn)
    monkeypatch.setattr(reg, "_spawned_log", spawned, raising=False)
    shell_async.set_shell_job_registry(reg, on_complete=None)
    yield reg
    shell_async.set_shell_job_registry(None, on_complete=None)  # type: ignore[arg-type]


# ─── bash_async ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_async_spawns_and_returns_job_id(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_async.ainvoke({"command": "sleep 5"})
    assert "Spawned job" in out
    assert "job-1" in out
    # Routed via ``bash -lc`` with the venv-bin PATH export prepended so the
    # job can find ``mimir`` / the venv ``python`` (login shell resets PATH).
    argv = fake_registry._spawned_log[0]["argv"]  # type: ignore[attr-defined]
    assert argv[:2] == ["bash", "-lc"]
    assert "export PATH=" in argv[2]
    assert argv[2].endswith("\nsleep 5")  # original command preserved verbatim
    # The recorded command (for display) stays the clean original.
    assert fake_registry._spawned_log[0]["command"] == "sleep 5"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_bash_async_rejects_empty_command(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_async.ainvoke({"command": "  "})
    assert "command is required" in out


@pytest.mark.asyncio
async def test_bash_async_no_registry_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    shell_async.set_shell_job_registry(None, on_complete=None)  # type: ignore[arg-type]
    out = await shell_async.bash_async.ainvoke({"command": "echo hi"})
    assert "no shell-job registry" in out


@pytest.mark.asyncio
async def test_job_complete_inherits_enforced_auth_for_same_channel_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import agent as agent_module
    from mimir.access_control import SinkGate
    from mimir.agent import Agent, _create_turn_auth_context, _initialize_ifc_labels
    from mimir.models import (
        AuthContext,
        InformationFlowLabels,
        InformationFlowState,
        SourceLabel,
        TurnInteractivity,
    )

    channel_id = "slack-C1"
    source = SourceLabel(
        principal="alice",
        domain="channel:private",
        resource_id=channel_id,
        bridge_instance="slack",
        sensitivity="private",
        authorized_principals=frozenset({"alice"}),
    )
    origin_labels = InformationFlowLabels().with_source(source)
    origin_auth = AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=("admin",),
        event_ingress=None,
        trigger="user_message",
        channel_id=channel_id,
        interactivity=TurnInteractivity.INTERACTIVE,
        enforcement_enabled=True,
        ifc_labels=origin_labels,
        ifc_state=InformationFlowState(labels=origin_labels),
        domain="channel:private",
        resource_id=channel_id,
        bridge_instance="slack",
    )
    job = _FakeJob(channel_id=channel_id, status="exited_ok", exit_code=0)
    job.ifc_labels = origin_labels
    job.auth_context = origin_auth
    enqueued: list[Any] = []

    async def _enqueue(event: Any) -> bool:
        enqueued.append(event)
        return True

    async def _log_event(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(agent_module, "log_event", _log_event)
    agent = SimpleNamespace(
        _shell_jobs=SimpleNamespace(
            read_output=lambda *args, **kwargs: {"stdout_tail": "ok", "stderr_tail": ""}
        ),
        _dispatcher=SimpleNamespace(enqueue=_enqueue),
    )

    await Agent._on_shell_job_complete(agent, job)

    event = enqueued[0]
    labels = _initialize_ifc_labels(event)
    auth = _create_turn_auth_context(
        event,
        None,
        policy_version="test",
        enforce=True,
        ifc_labels=labels,
    )
    same_channel = SinkGate.check_sink_flow(
        "send_message", channel_id, labels, auth, enforce=True,
    )
    cross_channel = SinkGate.check_sink_flow(
        "send_message", "slack-C2", labels, auth, enforce=True,
    )

    assert auth.principal == origin_auth.principal
    assert auth.canonical_principal == origin_auth.canonical_principal
    assert auth.roles == origin_auth.roles
    assert same_channel.allowed is True
    assert same_channel.reason != "ifc_label_blocked:same_channel"
    assert cross_channel.allowed is False


def test_job_complete_preserves_registered_service_provenance() -> None:
    from mimir.access_control import SinkGate
    from mimir.agent import _create_turn_auth_context, _initialize_ifc_labels
    from mimir.models import AgentEvent, AuthContext, TurnInteractivity

    channel_id = "scheduler:heartbeat"
    origin_auth = AuthContext(
        principal=None,
        canonical_principal="scheduler",
        roles=(),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id=channel_id,
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        enforcement_enabled=True,
        domain="channel",
        resource_id=channel_id,
        bridge_instance="service:scheduler",
    )
    event = AgentEvent(
        trigger="shell_job_complete",
        channel_id=channel_id,
        source="system",
        continuation_auth_context=origin_auth,
    )

    labels = _initialize_ifc_labels(event)
    auth = _create_turn_auth_context(
        event, None, policy_version="test", enforce=True, ifc_labels=labels,
    )
    decision = SinkGate.check_sink_flow(
        "send_message", channel_id, labels, auth, enforce=True,
    )

    assert auth.trigger == "scheduled_tick"
    assert decision.allowed is True


# ─── bash_jobs_list ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_jobs_list_empty_scope(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_jobs_list.ainvoke({})
    assert "No jobs" in out


@pytest.mark.asyncio
async def test_bash_jobs_list_invalid_scope(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_jobs_list.ainvoke({"scope": "garbage"})
    assert "bash_jobs_list failed" in out


# ─── bash_job_output ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_job_output_requires_job_id(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_job_output.ainvoke({"job_id": ""})
    assert "job_id is required" in out


@pytest.mark.asyncio
async def test_bash_job_output_unknown_job_propagates_error(
    fake_registry: ShellJobRegistry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _err_read(job_id: str, *, tail_lines: int, stream: str) -> dict:
        return {"error": f"unknown job {job_id}"}

    monkeypatch.setattr(fake_registry, "read_output", _err_read)
    out = await shell_async.bash_job_output.ainvoke({"job_id": "missing-job"})
    assert "unknown job missing-job" in out


@pytest.mark.asyncio
async def test_bash_job_output_renders_tail(
    fake_registry: ShellJobRegistry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify both stdout + stderr blocks surface when stream=both."""

    def _ok_read(job_id: str, *, tail_lines: int, stream: str) -> dict:
        return {
            "job_id": job_id,
            "status": "complete",
            "elapsed_seconds": 4.2,
            "pid": 99,
            "exit_code": 0,
            "command": "echo hi",
            "stdout_tail": "line1\nline2",
            "stderr_tail": "warning1",
        }

    monkeypatch.setattr(fake_registry, "read_output", _ok_read)
    out = await shell_async.bash_job_output.ainvoke({"job_id": "job-1"})
    assert "--- stdout tail ---" in out
    assert "line1" in out
    assert "--- stderr tail ---" in out
    assert "warning1" in out
    assert "exit_code=0" in out


# ─── Registry tool list inclusion ─────────────────────────────────


def test_all_mimir_tools_includes_shell_async_trio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three async-shell tools must be unconditionally present in
    ``all_mimir_tools()`` — they're orthogonal to the provider /
    Tavily / MCP gates that other tools use."""
    from mimir.tools import all_mimir_tools

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:foo")
    names = {t.name for t in all_mimir_tools()}
    assert {"bash_async", "bash_jobs_list", "bash_job_output"}.issubset(names)


# ─── _intent_prefix unit tests ────────────────────────────────────────


def test_intent_prefix_plain_command() -> None:
    from mimir.tools.shell_async import _intent_prefix

    result = _intent_prefix("social-cli dispatch /path/to/file.yaml")
    assert result == "social-cli dispatch /path/to/file.yaml"


def test_intent_prefix_strips_env_export() -> None:
    from mimir.tools.shell_async import _intent_prefix

    base = "social-cli dispatch /path/to/file.yaml"
    with_exports = f"export ATPROTO_HANDLE=alice ATPROTO_APP_PASSWORD=secret {base}"
    assert _intent_prefix(with_exports) == _intent_prefix(base)


def test_intent_prefix_strips_bare_env_assignment() -> None:
    from mimir.tools.shell_async import _intent_prefix

    base = "social-cli dispatch /file.yaml"
    variant = f"STATE_DIR=/tmp {base}"
    assert _intent_prefix(variant) == _intent_prefix(base)


def test_intent_prefix_truncates_to_max_chars() -> None:
    from mimir.tools.shell_async import _INTENT_PREFIX_MAX_CHARS, _intent_prefix

    long_cmd = "a" * (_INTENT_PREFIX_MAX_CHARS + 50)
    result = _intent_prefix(long_cmd)
    assert len(result) == _INTENT_PREFIX_MAX_CHARS


def test_intent_prefix_different_commands_differ() -> None:
    from mimir.tools.shell_async import _intent_prefix

    assert _intent_prefix("git fetch origin") != _intent_prefix("social-cli dispatch x")


# chainlink #192 — wrapper-invariance unit tests


def test_intent_prefix_strips_cd_chain() -> None:
    """cd path && cmd should reduce to cmd (case 2 from chainlink #192)."""
    from mimir.tools.shell_async import _intent_prefix

    base = "social-cli like at://did:plc:xxx/app.bsky.feed.post/yyy"
    with_cd = f"cd /mimir-home/state/pollers/social-cli-feed && {base}"
    assert _intent_prefix(with_cd) == _intent_prefix(base)


def test_intent_prefix_strips_bash_c_wrapper_single_quoted() -> None:
    """bash -c '...' wrapper is unwrapped before normalisation."""
    from mimir.tools.shell_async import _intent_prefix

    inner = "social-cli like at://did:plc:xxx/app.bsky.feed.post/yyy"
    wrapped = f"/bin/bash -c '{inner}'"
    assert _intent_prefix(wrapped) == _intent_prefix(inner)


def test_intent_prefix_strips_bash_c_wrapper_double_quoted() -> None:
    """bash -c \"...\" (double-quoted body) is also unwrapped."""
    from mimir.tools.shell_async import _intent_prefix

    inner = "social-cli like at://did:plc:xxx/app.bsky.feed.post/yyy"
    wrapped = f'/bin/bash -c "{inner}"'
    assert _intent_prefix(wrapped) == _intent_prefix(inner)


def test_intent_prefix_normalises_executable_basename() -> None:
    """Absolute-path executables are normalised to their basename."""
    from mimir.tools.shell_async import _intent_prefix

    full = "/usr/bin/node /mimir-home/.local/social-cli/dist/cli.js dispatch /file"
    bare = "node /mimir-home/.local/social-cli/dist/cli.js dispatch /file"
    assert _intent_prefix(full) == _intent_prefix(bare)


def test_intent_prefix_complex_bash_c_chain() -> None:
    """Full case-4 shape: bash -c 'cd && export && node cli.js …'
    After wrapper-strip + chain-strip + env-strip + basename-norm the intent
    should be 'node ... cli.js dispatch at://...'."""
    from mimir.tools.shell_async import _intent_prefix

    cmd4 = (
        "/bin/bash -c 'cd /mimir-home/state/pollers/social-cli-feed "
        "&& export ATPROTO_HANDLE=alice ATPROTO_APP_PASSWORD=secret "
        "&& /usr/bin/node /mimir-home/.local/social-cli/dist/cli.js "
        "dispatch at://did:plc:xxx/app.bsky.feed.post/yyy'"
    )
    # After normalisation: node ... cli.js dispatch at://...
    result = _intent_prefix(cmd4)
    assert "node" in result
    assert "dispatch" in result
    assert "at://did:plc:xxx/app.bsky.feed.post/yyy" in result


# ─── _intent_suffix_key unit tests ──────────────────────────────────────


def test_intent_suffix_key_extracts_at_uri() -> None:
    from mimir.tools.shell_async import _intent_suffix_key

    cmd = "social-cli like at://did:plc:xxx/app.bsky.feed.post/yyy"
    assert _intent_suffix_key(cmd) == "at://did:plc:xxx/app.bsky.feed.post/yyy"


def test_intent_suffix_key_extracts_https_uri() -> None:
    from mimir.tools.shell_async import _intent_suffix_key

    cmd = "curl -X POST https://api.example.com/data"
    assert _intent_suffix_key(cmd) == "https://api.example.com/data"


def test_intent_suffix_key_returns_rightmost_uri() -> None:
    from mimir.tools.shell_async import _intent_suffix_key

    cmd = "curl https://api.example.com/auth | curl https://api.example.com/data"
    assert _intent_suffix_key(cmd) == "https://api.example.com/data"


def test_intent_suffix_key_strips_trailing_quote() -> None:
    """Trailing single-quote from bash -c wrappers is stripped."""
    from mimir.tools.shell_async import _intent_suffix_key

    cmd = "node cli.js dispatch at://did:plc:xxx/app.bsky.feed.post/yyy'"
    assert _intent_suffix_key(cmd) == "at://did:plc:xxx/app.bsky.feed.post/yyy"


def test_intent_suffix_key_returns_none_for_no_uri() -> None:
    from mimir.tools.shell_async import _intent_suffix_key

    assert _intent_suffix_key("git fetch origin") is None
    assert _intent_suffix_key("social-cli dispatch /path/file.yaml") is None


def test_intent_suffix_key_different_uris_differ() -> None:
    from mimir.tools.shell_async import _intent_suffix_key

    uri1 = _intent_suffix_key("social-cli like at://did:plc:aaa/app.bsky.feed.post/111")
    uri2 = _intent_suffix_key("social-cli like at://did:plc:bbb/app.bsky.feed.post/222")
    assert uri1 != uri2


# ─── wait-on-pending guard — wrapper escalation integration tests ──────────


@pytest.mark.asyncio
async def test_bash_async_refuses_via_suffix_key_wrapper_escalation(
    fake_registry_with_channel: ShellJobRegistry,
) -> None:
    """Guard fires via URI suffix when the agent escalates to a bash -c wrapper.

    Observed failure mode (chainlink #192, turn 201c 2026-05-25):
      Step 3 (running): export ATPROTO_HANDLE=alice social-cli like at://X
      Step 4 (retry):   /bin/bash -c '… /usr/bin/node cli.js dispatch at://X'
    The prefix keys differ (social-cli vs node/cli.js, like vs dispatch), but
    the AT-URI ``at://X`` is identical — the suffix key fires the guard.
    """
    at_uri = "at://did:plc:xxx/app.bsky.feed.post/yyy"
    running = _FakeJob(
        job_id="j_step3",
        pid=333,
        command=f"export ATPROTO_HANDLE=alice social-cli like {at_uri}",
        channel_id="test-ch",
        exit_code=None,
    )
    fake_registry_with_channel._jobs["j_step3"] = running

    # Step 4 — bash wrapper with node invoking cli.js + same AT-URI
    cmd_step4 = (
        f"/bin/bash -c 'cd /mimir-home/state/pollers/social-cli-feed "
        f"&& export ATPROTO_HANDLE=alice ATPROTO_APP_PASSWORD=secret "
        f"&& /usr/bin/node /mimir-home/.local/social-cli/dist/cli.js dispatch {at_uri}'"
    )
    out = await shell_async.bash_async.ainvoke({"command": cmd_step4})
    assert "bash_async refused" in out
    assert "j_step3" in out
    assert "uri-target" in out  # match_kind should indicate suffix match


@pytest.mark.asyncio
async def test_bash_async_refuses_via_prefix_after_cd_strip(
    fake_registry_with_channel: ShellJobRegistry,
) -> None:
    """Guard fires via prefix when the retry prepends cd /path && (case 2)."""
    base_cmd = "social-cli like at://did:plc:xxx/app.bsky.feed.post/yyy"
    running = _FakeJob(
        job_id="j_step1",
        pid=111,
        command=base_cmd,
        channel_id="test-ch",
        exit_code=None,
    )
    fake_registry_with_channel._jobs["j_step1"] = running

    retry = f"cd /mimir-home/state/pollers/social-cli-feed && {base_cmd}"
    out = await shell_async.bash_async.ainvoke({"command": retry})
    assert "bash_async refused" in out
    assert "j_step1" in out


@pytest.mark.asyncio
async def test_bash_async_allows_different_uri_same_executable(
    fake_registry_with_channel: ShellJobRegistry,
) -> None:
    """Guard does not fire when two commands use the same executable but different URIs."""
    running = _FakeJob(
        job_id="j_uri1",
        pid=777,
        command="social-cli like at://did:plc:aaa/app.bsky.feed.post/111",
        channel_id="test-ch",
        exit_code=None,
    )
    fake_registry_with_channel._jobs["j_uri1"] = running

    out = await shell_async.bash_async.ainvoke(
        {"command": "social-cli like at://did:plc:bbb/app.bsky.feed.post/222"}
    )
    assert "Spawned job" in out  # different URI → not blocked


# ─── wait-on-pending guard ────────────────────────────────────────────


class _FakeTurnCtx:
    """Minimal turn context stub for channel_id injection."""

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id


@pytest.fixture
def fake_registry_with_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ShellJobRegistry:
    """Like fake_registry but with a channel_id=test-ch wired via turn context."""
    import mimir._context as ctx_mod

    reg = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    spawned: list[dict] = []

    def _fake_spawn(
        command: str, *, argv: list[str], channel_id: str | None,
        on_complete=None, auth_context=None,
    ) -> _FakeJob:
        spawned.append({"command": command, "argv": argv, "channel_id": channel_id})
        return _FakeJob(command=command, channel_id=channel_id, job_id="j_new")

    monkeypatch.setattr(reg, "spawn", _fake_spawn)
    monkeypatch.setattr(reg, "_spawned_log", spawned, raising=False)
    shell_async.set_shell_job_registry(reg, on_complete=None)

    # Wire a channel_id so the guard is in scope.
    monkeypatch.setattr(ctx_mod, "get_current_turn", lambda: _FakeTurnCtx("test-ch"))

    yield reg
    shell_async.set_shell_job_registry(None, on_complete=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bash_async_refuses_same_intent_running_job(
    fake_registry_with_channel: ShellJobRegistry,
) -> None:
    """Guard refuses when a same-intent job is already running on this channel."""
    # Seed a running job with the same intent.
    running = _FakeJob(
        job_id="j_running",
        pid=999,
        command="social-cli dispatch /path/to/file.yaml",
        channel_id="test-ch",
        exit_code=None,
    )
    fake_registry_with_channel._jobs["j_running"] = running

    out = await shell_async.bash_async.ainvoke(
        {"command": "social-cli dispatch /path/to/file.yaml"}
    )
    assert "bash_async refused" in out
    assert "j_running" in out
    assert "bash_job_output" in out


@pytest.mark.asyncio
async def test_bash_async_refuses_after_env_export_stripping(
    fake_registry_with_channel: ShellJobRegistry,
) -> None:
    """Guard fires even when the retry wraps the same command in env exports."""
    running = _FakeJob(
        job_id="j_orig",
        pid=100,
        command="social-cli dispatch /file.yaml",
        channel_id="test-ch",
        exit_code=None,
    )
    fake_registry_with_channel._jobs["j_orig"] = running

    retry = "export ATPROTO_HANDLE=alice STATE_DIR=/tmp social-cli dispatch /file.yaml"
    out = await shell_async.bash_async.ainvoke({"command": retry})
    assert "bash_async refused" in out
    assert "j_orig" in out


@pytest.mark.asyncio
async def test_bash_async_allows_different_intent(
    fake_registry_with_channel: ShellJobRegistry,
) -> None:
    """Guard does not fire when the running job has a different intent."""
    running = _FakeJob(
        job_id="j_other",
        pid=200,
        command="git fetch origin",
        channel_id="test-ch",
        exit_code=None,
    )
    fake_registry_with_channel._jobs["j_other"] = running

    out = await shell_async.bash_async.ainvoke(
        {"command": "social-cli dispatch /file.yaml"}
    )
    assert "Spawned job" in out  # allowed through


@pytest.mark.asyncio
async def test_bash_async_allows_no_running_jobs(
    fake_registry_with_channel: ShellJobRegistry,
) -> None:
    """Guard does not fire when no jobs are running."""
    out = await shell_async.bash_async.ainvoke({"command": "social-cli dispatch /file.yaml"})
    assert "Spawned job" in out


@pytest.mark.asyncio
async def test_bash_async_allows_different_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard does not fire for a running job on a DIFFERENT channel."""
    import mimir._context as ctx_mod

    reg = ShellJobRegistry(jobs_dir=tmp_path / "jobs")

    def _fake_spawn(
        command: str, *, argv: list[str], channel_id: str | None,
        on_complete=None, auth_context=None,
    ) -> _FakeJob:
        return _FakeJob(command=command, channel_id=channel_id)

    monkeypatch.setattr(reg, "spawn", _fake_spawn)
    shell_async.set_shell_job_registry(reg, on_complete=None)
    monkeypatch.setattr(ctx_mod, "get_current_turn", lambda: _FakeTurnCtx("channel-A"))

    # Running job is on channel-B.
    running = _FakeJob(
        job_id="j_other_ch",
        pid=300,
        command="social-cli dispatch /file.yaml",
        channel_id="channel-B",
        exit_code=None,
    )
    reg._jobs["j_other_ch"] = running

    out = await shell_async.bash_async.ainvoke({"command": "social-cli dispatch /file.yaml"})
    assert "Spawned job" in out  # different channel → allowed

    shell_async.set_shell_job_registry(None, on_complete=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bash_async_skips_guard_when_finished_job(
    fake_registry_with_channel: ShellJobRegistry,
) -> None:
    """Guard ignores finished (non-running) jobs — exit_code set."""
    finished = _FakeJob(
        job_id="j_done",
        pid=400,
        command="social-cli dispatch /file.yaml",
        channel_id="test-ch",
        exit_code=0,  # finished
    )
    fake_registry_with_channel._jobs["j_done"] = finished

    out = await shell_async.bash_async.ainvoke(
        {"command": "social-cli dispatch /file.yaml"}
    )
    assert "Spawned job" in out  # finished job → guard skips it


# ─── chainlink #193: algedonic event on refusal ───────────────────────


@pytest.mark.asyncio
async def test_bash_async_refused_emits_algedonic_event(
    fake_registry_with_channel: ShellJobRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard emits bash_async_refused_same_intent event when refusing a respawn.

    The tool's return-string is the agent-visible signal; the event is the
    operator/introspection-visible audit trail (chainlink #193).
    """
    emitted: list[dict] = []

    async def _fake_safe_log_event(event_type: str, **payload: Any) -> None:
        emitted.append({"type": event_type, **payload})

    monkeypatch.setattr(
        "mimir.event_logger.safe_log_event",
        _fake_safe_log_event,
        raising=False,
    )

    # Seed a running job with the same intent.
    running = _FakeJob(
        job_id="j_running",
        pid=999,
        command="social-cli dispatch /path/to/file.yaml",
        channel_id="test-ch",
        exit_code=None,
    )
    fake_registry_with_channel._jobs["j_running"] = running

    out = await shell_async.bash_async.ainvoke(
        {"command": "social-cli dispatch /path/to/file.yaml"}
    )

    # Tool return should still be the refusal message.
    assert "bash_async refused" in out

    # Exactly one event should have been emitted.
    assert len(emitted) == 1, f"Expected 1 event, got {emitted}"
    ev = emitted[0]
    assert ev["type"] == "bash_async_refused_same_intent"
    assert ev["channel_id"] == "test-ch"
    assert ev["running_job_id"] == "j_running"
    assert "social-cli dispatch" in ev["new_command"]
    assert isinstance(ev["intent_prefix"], str)


@pytest.mark.asyncio
async def test_bash_async_resolves_channel_from_live_contextvar(
    fake_registry: ShellJobRegistry,
) -> None:
    """chainlink #392: with no active-turn context (the SDK/MCP forked-task
    case), bash_async must resolve the routing channel from the live per-task
    ContextVar — NOT the dead _STATE key, which left channel_id=None and dropped
    the shell_job_complete wake-up."""
    from mimir.tools import registry as tool_registry

    token = tool_registry.set_current_channel_id("ch-live")
    try:
        await shell_async.bash_async.ainvoke({"command": "echo hi"})
    finally:
        tool_registry.reset_current_channel_id(token)
    assert fake_registry._spawned_log[0]["channel_id"] == "ch-live"  # type: ignore[attr-defined]
