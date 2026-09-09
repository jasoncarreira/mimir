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
    if error == errno.EINVAL:
        with pytest.raises(OSError) as raised:
            await process.wait()
        assert raised.value is failure
    else:
        assert await process.wait() is None
    kill.assert_called_once_with(4321, 0)
    sleep.assert_not_called()
