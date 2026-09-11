from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mimir.acp import confinement, hosted
from mimir.acp.hosted import HostedHandsProvider, HostedMcpError
from mimir.acp.execution_scope import UNCONFINED_WARNING


@pytest.fixture
def execution_timers(monkeypatch):
    from mimir.acp import python_kernel

    timers = []

    def timeout_at(deadline):
        timer = asyncio.timeout(None)
        timers.append(timer)
        return timer

    for module in (hosted, python_kernel):
        monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{
            **vars(asyncio), "timeout_at": timeout_at,
        }))
    return timers


@pytest.fixture
def unavailable(monkeypatch):
    def missing():
        raise confinement.BackendUnavailable("test backend unavailable")
    monkeypatch.setattr(confinement, "_backend", missing)


def bind(tmp_path, callback=None):
    provider = HostedHandsProvider(request_unconfined_permission=callback)
    provider.bind_session("one", tmp_path)
    return provider, provider._sessions["one"]


@pytest.mark.asyncio
async def test_missing_backend_defaults_blocked_and_query_does_not_prompt(tmp_path, unavailable, monkeypatch):
    provider, session = bind(tmp_path)
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    try:
        query = await provider.request_scope(session, "")
        assert "BLOCKED:" in query["message"]
        for operation in (provider._shell(session, "true"), provider.execute_python(session, "1")):
            with pytest.raises(HostedMcpError, match="Execution blocked"):
                await operation
        spawn.assert_not_awaited()
        assert not session.scope.unconfined_approved
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_operator_acceptance_enables_both_engines_with_hardened_environment(tmp_path, unavailable, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    outside = tmp_path / "outside-fixture"
    outside.touch()
    approval = AsyncMock(return_value=True)
    provider, session = bind(cwd, approval)
    monkeypatch.setenv("MIMIR_FALLBACK_SECRET", "must-not-leak")
    try:
        # A scope query never accepts risk or submits a permission request.
        assert "BLOCKED:" in (await provider.request_scope(session, ""))["message"]
        approval.assert_not_awaited()
        shell = await provider._shell(session, 'printf "%s" "${MIMIR_FALLBACK_SECRET-unset}"')
        assert shell["stdout"] == "unset"
        assert shell["stderr"].startswith(UNCONFINED_WARNING)
        result = await provider.execute_python(session, f"from pathlib import Path\nPath({str(outside)!r}).read_bytes() == b''")
        assert result["value"] == "True"
        assert result["stderr"].startswith(UNCONFINED_WARNING)
        assert (await provider.execute_python(session, "import os\nos.read(0, 10)"))["value"] == "b''"
        approval.assert_awaited_once_with("one")
        query = await provider.request_scope(session, "")
        assert query["message"].startswith("UNCONFINED:")
        scope = await provider.request_scope(session, str(outside))
        assert not scope["approved"]
        assert "cannot constrain" in scope["message"]
        assert not session.scope.approved
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [False, None, "true", {"approved": True}])
async def test_refusal_or_non_bool_is_final_for_session(tmp_path, unavailable, answer):
    callback = AsyncMock(return_value=answer)
    provider, session = bind(tmp_path, callback)
    try:
        for unused in range(2):
            with pytest.raises(HostedMcpError):
                await provider._shell(session, "true")
        callback.assert_awaited_once()
        assert not session.scope.unconfined_approved
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_pending_risk_prompt_bounded_and_cancelled_reply_cannot_install(tmp_path, unavailable):
    entered, answer = asyncio.Event(), asyncio.Event()
    async def callback(session_id):
        entered.set()
        try:
            await answer.wait()
        except asyncio.CancelledError:
            return True  # An invalid late broker answer must not install authority.
        return True
    provider, session = bind(tmp_path, callback)
    task = asyncio.create_task(provider._shell(session, "true"))
    try:
        await entered.wait()
        with pytest.raises(HostedMcpError, match="pending"):
            await provider._shell(session, "true")
        task.cancel()
        with pytest.raises(HostedMcpError, match="expired"):
            await task
        assert not session.scope.unconfined_approved
        assert session.scope.risk_requested and not session.scope.risk_pending
    finally:
        answer.set()
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["rebind", "cancel", "revoke"])
async def test_stale_risk_approval_never_installs(tmp_path, unavailable, boundary):
    entered, answer = asyncio.Event(), asyncio.Event()
    async def callback(session_id):
        entered.set()
        await answer.wait()
        return True
    provider, session = bind(tmp_path, callback)
    task = asyncio.create_task(provider._shell(session, "true"))
    try:
        await entered.wait()
        if boundary == "rebind":
            provider.bind_session("one", tmp_path)
        elif boundary == "cancel":
            await provider.cancel_session("one")
        else:
            provider.revoke_session("one")
        answer.set()
        with pytest.raises(HostedMcpError):
            await task
        assert not session.scope.unconfined_approved
        if "one" in provider._sessions:
            assert not provider._sessions["one"].scope.unconfined_approved
    finally:
        answer.set()
        await provider.close()


@pytest.mark.asyncio
async def test_risk_grant_is_not_shared_with_second_session_or_rebind(tmp_path, unavailable):
    callback = AsyncMock(return_value=True)
    provider, one = bind(tmp_path, callback)
    try:
        await provider._shell(one, "true")
        callback.return_value = False
        provider.bind_session("two", tmp_path)
        with pytest.raises(HostedMcpError):
            await provider._shell(provider._sessions["two"], "true")
        provider.bind_session("one", tmp_path)
        with pytest.raises(HostedMcpError):
            await provider._shell(provider._sessions["one"], "true")
        assert callback.await_count == 3
    finally:
        await provider.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["1/0", "import os; os._exit(31)", "timeout"])
async def test_every_unconfined_python_result_labels_mode(tmp_path, unavailable, code, execution_timers):
    provider, session = bind(tmp_path, AsyncMock(return_value=True))
    task = None
    try:
        await provider.execute_python(session, "1")
        session.timeout_seconds = 1
        source = ("from pathlib import Path\nimport signal\nPath('entered').touch()\nsignal.pause()"
                  if code == "timeout" else code)
        task = asyncio.create_task(provider.execute_python(session, source))
        if code == "timeout":
            while not (tmp_path / "entered").exists():
                await asyncio.sleep(0)
            assert not task.done()
            execution_timers[-1].reschedule(0)
        result = await task
        assert result["stderr"].startswith(UNCONFINED_WARNING)
        assert not result["ok"]
        assert result["kernel"] == {"1/0": "reused", "import os; os._exit(31)": "crashed",
                                    "timeout": "timed_out"}[code]
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
@pytest.mark.parametrize("modes", [("unconfined", "unconfined"), ("unconfined", "confined"), ("confined", "unconfined")])
@pytest.mark.parametrize("ending", ["success", "crash", "timeout", "startup"])
async def test_adopted_kernel_warning_uses_this_calls_mode(tmp_path, monkeypatch, modes, ending):
    import os
    from mimir.acp import python_kernel as kernels

    mode = modes[0]
    def prepare(argv, **kwargs):
        return confinement.PreparedCommand(tuple(argv), dict(os.environ), execution_mode=mode)
    monkeypatch.setattr(kernels, "prepare_command", prepare)
    manager = kernels.PythonKernelManager()
    task = None
    timers = []

    def timeout_at(deadline):
        timer = asyncio.timeout(None)
        timers.append(timer)
        return timer

    try:
        await manager.execute("a", tmp_path, "kept = 42")
        await manager.release("a")
        mode = modes[1]
        if ending == "startup":
            await manager.retire(tmp_path)
            async def fail(*args):
                raise kernels.PythonKernelUnavailable("startup fixture")
            monkeypatch.setattr(manager, "_spawn", fail)
            with pytest.raises(kernels.PythonKernelUnavailable) as error:
                await manager.execute("b", tmp_path, "1")
            assert str(error.value).count(UNCONFINED_WARNING) == (mode == "unconfined")
        else:
            code = {"success": "1", "crash": "import os; os._exit(31)",
                    "timeout": "import pathlib,time\npathlib.Path('executing').touch()\nwhile True: time.sleep(1)"}[ending]
            if ending == "timeout":
                # Allow adoption/respawn and handshake to finish before expiring
                # the real response timeout, without changing the event-loop clock.
                monkeypatch.setattr(kernels, "asyncio", SimpleNamespace(**{
                    **vars(asyncio), "timeout_at": timeout_at,
                }))
            task = asyncio.create_task(manager.execute("b", tmp_path, code, timeout=120))
            if ending == "timeout":
                while not (tmp_path / "executing").exists():
                    await asyncio.sleep(0)
                state = manager._kernels[str(tmp_path.resolve())]
                assert state.worker.execution_mode == mode
                assert not task.done()
                timers[-1].reschedule(0)
            result = await task
            assert result["stderr"].count(UNCONFINED_WARNING) == (mode == "unconfined")
            if ending == "success":
                assert result["ok"]
                assert result["kernel"] == ("reused" if modes[0] == mode else "fresh")
            else:
                assert not result["ok"]
                assert result["kernel"] == ("crashed" if ending == "crash" else "timed_out")
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_unconfined_shell_failure_and_timeout_label_mode(tmp_path, unavailable, execution_timers):
    provider, session = bind(tmp_path, AsyncMock(return_value=True))
    task = None
    try:
        failed = await provider._shell(session, "exit 3")
        assert failed["exitCode"] == 3 and failed["stderr"].startswith(UNCONFINED_WARNING)
        session.timeout_seconds = 1
        source = "from pathlib import Path; import signal; Path('entered').touch(); signal.pause()"
        task = asyncio.create_task(provider._shell(
            session, f"exec {shlex.quote(sys.executable)} -c {shlex.quote(source)}",
        ))
        while not (tmp_path / "entered").exists():
            await asyncio.sleep(0)
        assert not task.done()
        execution_timers[-1].reschedule(0)
        timeout = await task
        assert timeout["exitCode"] == -1 and timeout["stderr"].startswith(UNCONFINED_WARNING)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.mark.asyncio
async def test_available_but_failed_policy_never_offers_or_uses_fallback(tmp_path, monkeypatch):
    class BadBackend:
        def prepare(self, *args, **kwargs):
            raise confinement.ConfinementUnavailable("malformed policy")
    monkeypatch.setattr(confinement, "_backend", BadBackend)
    callback = AsyncMock(return_value=True)
    provider, session = bind(tmp_path, callback)
    session.scope.unconfined_approved = True
    try:
        with pytest.raises(HostedMcpError, match="malformed policy"):
            await provider._shell(session, "true")
        with pytest.raises(HostedMcpError, match="malformed policy"):
            await provider.execute_python(session, "1")
        callback.assert_not_awaited()
    finally:
        await provider.close()


def test_backend_fallback_only_handles_typed_absence(tmp_path, unavailable, monkeypatch):
    with pytest.raises(confinement.BackendUnavailable):
        confinement.prepare_command(("/bin/true",), cwd=tmp_path)
    allowed = confinement.prepare_command(("/bin/true",), cwd=tmp_path, allow_unconfined=True)
    assert allowed.argv == ("/bin/true",) and allowed.execution_mode == "unconfined"
    class Available:
        def prepare(self, argv, **kwargs):
            return confinement.PreparedCommand(("sandbox", *argv), {})
    monkeypatch.setattr(confinement, "_backend", Available)
    confined = confinement.prepare_command(("/bin/true",), cwd=tmp_path, allow_unconfined=True)
    assert confined.argv[0] == "sandbox" and confined.execution_mode == "confined"


@pytest.mark.asyncio
async def test_risk_rejection_remains_final_across_connection_reset(tmp_path, unavailable):
    callback = AsyncMock(return_value=False)
    provider, session = bind(tmp_path, callback)
    try:
        connection = provider.connect("one")
        with pytest.raises(HostedMcpError):
            await provider._shell(session, "true")
        await provider.disconnect(connection)
        provider.connect("one")
        callback.return_value = True
        with pytest.raises(HostedMcpError, match="final"):
            await provider._shell(session, "true")
        callback.assert_awaited_once()
        assert not session.scope.unconfined_approved
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_risk_timeout_is_final_and_audited_without_command(tmp_path, unavailable, monkeypatch):
    callback = AsyncMock(side_effect=TimeoutError)
    audit = AsyncMock()
    monkeypatch.setattr(hosted, "safe_log_event", audit)
    provider, session = bind(tmp_path, callback)
    try:
        with pytest.raises(HostedMcpError, match="timed out"):
            await provider._shell(session, "secret-command-never-audited")
        with pytest.raises(HostedMcpError, match="final"):
            await provider.execute_python(session, "secret-code-never-audited")
        callback.assert_awaited_once()
        assert "secret-" not in repr(audit.await_args_list)
        assert all(call.kwargs["outcome"] == "denied" for call in audit.await_args_list)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_child_runtime_failure_never_downgrades(tmp_path, monkeypatch):
    class RuntimeFailure:
        def prepare(self, argv, **kwargs):
            return confinement.PreparedCommand(("/bin/sh", "-c", "exit 78"), {})
    monkeypatch.setattr(confinement, "_backend", RuntimeFailure)
    callback = AsyncMock(return_value=True)
    provider, session = bind(tmp_path, callback)
    try:
        shell = await provider._shell(session, "true")
        assert shell["exitCode"] == 78 and "UNCONFINED" not in shell["stderr"]
        with pytest.raises(HostedMcpError, match="kernel unavailable"):
            await provider.execute_python(session, "1")
        callback.assert_not_awaited()
        assert not session.scope.unconfined_approved
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_unconfined_startup_errors_are_labelled(tmp_path, unavailable, monkeypatch):
    provider, session = bind(tmp_path, AsyncMock(return_value=True))
    async def failed_spawn(*args, **kwargs):
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        raise OSError("test child spawn failed")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_spawn)
    try:
        with pytest.raises(HostedMcpError, match="UNCONFINED:.*test child spawn failed"):
            await provider._shell(session, "true")
        with pytest.raises(HostedMcpError, match="UNCONFINED:.*test child spawn failed"):
            await provider.execute_python(session, "1")
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("accept", [False, True])
async def test_backend_loss_retires_existing_kernel_only_after_consent(tmp_path, monkeypatch, accept):
    missing = False
    class Available:
        def prepare(self, argv, **kwargs):
            return confinement.PreparedCommand(tuple(argv), confinement._environment())
    def backend():
        if missing:
            raise confinement.BackendUnavailable("test backend disappeared")
        return Available()
    monkeypatch.setattr(confinement, "_backend", backend)
    callback = AsyncMock(return_value=accept)
    provider, session = bind(tmp_path, callback)
    try:
        await provider.execute_python(session, "kept = 42")
        worker = provider._python_kernels._kernels[str(session.cwd.resolve())].worker
        missing = True
        assert "Next execution" in (await provider.request_scope(session, ""))["message"]
        callback.assert_not_awaited()
        if accept:
            result = await provider.execute_python(session, "'kept' in globals()")
            assert result["value"] == "False" and result["kernel"] == "fresh"
            assert result["stderr"].startswith(UNCONFINED_WARNING)
        else:
            with pytest.raises(HostedMcpError):
                await provider.execute_python(session, "kept")
            assert provider._python_kernels._kernels[str(session.cwd.resolve())].worker is worker
            missing = False
            result = await provider.execute_python(session, "kept")
            assert result["value"] == "42" and result["kernel"] == "reused"
        callback.assert_awaited_once()
    finally:
        await provider.close()


def test_approved_fallback_cannot_bypass_backend_policy_failure(tmp_path, monkeypatch):
    class FailedPolicy:
        def prepare(self, *args, **kwargs):
            raise confinement.ConfinementUnavailable("policy validation failed")
    monkeypatch.setattr(confinement, "_backend", FailedPolicy)
    with pytest.raises(confinement.ConfinementUnavailable, match="policy validation failed"):
        confinement.prepare_command(("/bin/true",), cwd=tmp_path, allow_unconfined=True)


@pytest.mark.parametrize("truthy", [1, "true", "false", ["approved"]])
def test_only_literal_true_authorizes_the_unconfined_fallback(tmp_path, unavailable, truthy):
    """Risk authority is a boolean the host sets after operator consent. A merely
    truthy value is not consent, so the guard is an identity check, not a test for
    truthiness that a future config string would satisfy."""
    with pytest.raises(confinement.BackendUnavailable):
        confinement.prepare_command(("/bin/true",), cwd=tmp_path, allow_unconfined=truthy)
    prepared = confinement.prepare_command(("/bin/true",), cwd=tmp_path, allow_unconfined=True)
    assert prepared.execution_mode == "unconfined"
