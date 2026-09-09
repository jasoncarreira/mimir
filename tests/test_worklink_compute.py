from __future__ import annotations

import errno
import os
from unittest.mock import AsyncMock, Mock

import pytest

from mimir.worklink import compute, run_state


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [errno.EPERM, errno.ESRCH, errno.EINVAL], ids=["EPERM", "ESRCH", "EINVAL"])
async def test_external_process_wait_kill_guard(monkeypatch, error) -> None:
    failure = OSError(error, os.strerror(error))
    kill = Mock(side_effect=failure)
    sleep = AsyncMock(side_effect=AssertionError("wait must stop after signal error"))
    monkeypatch.setattr(compute.os, "kill", kill)
    monkeypatch.setattr(run_state, "process_is_zombie", Mock(return_value=False))
    monkeypatch.setattr(compute.asyncio, "sleep", sleep)

    process = compute._ExternalProcess(4321)
    if error != errno.ESRCH:
        # EPERM must fail loudly, not return a successful wait for a live PID.
        with pytest.raises(PermissionError if error == errno.EPERM else OSError) as raised:
            await process.wait()
        assert raised.value is failure
    else:
        assert await process.wait() is None
    kill.assert_called_once_with(4321, 0)
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_external_process_cancel_propagates_probe_permission_error(monkeypatch) -> None:
    from mimir.worklink import worker_exec

    failure = PermissionError(errno.EPERM, "not our process")
    kill = Mock(side_effect=failure)
    terminate = Mock()
    monkeypatch.setattr(compute.os, "getpgid", Mock(return_value=4321))
    monkeypatch.setattr(compute.os, "kill", kill)
    monkeypatch.setattr(run_state, "process_is_zombie", Mock(return_value=False))
    monkeypatch.setattr(worker_exec, "_terminate_process_group_pid", terminate)

    with pytest.raises(PermissionError) as raised:
        await compute._kill_process_group(compute._ExternalProcess(4321))

    assert raised.value is failure
    terminate.assert_called_once_with(4321)
    kill.assert_called_once_with(4321, 0)
