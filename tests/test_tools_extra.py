from __future__ import annotations

import errno
import io
import os
import signal
import subprocess
from unittest.mock import Mock, call

import pytest

from mimir.tools import extra


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("error", [PermissionError, ProcessLookupError, OSError],
                         ids=["EPERM", "ESRCH", "EINVAL"])
def test_bounded_project_test_killpg_guard(monkeypatch, error):
    timeout = subprocess.TimeoutExpired(["pytest"], 1)
    proc = Mock(pid=12345, stdout=io.BytesIO(b"out"), stderr=io.BytesIO(b"err"))
    proc.wait.side_effect = [timeout, -signal.SIGKILL]
    monkeypatch.setattr(extra.subprocess, "Popen", Mock(return_value=proc))
    exc = error(errno.EINVAL if error is OSError else
                errno.EPERM if error is PermissionError else errno.ESRCH, "signal failed")
    killpg = Mock(side_effect=exc)
    monkeypatch.setattr(extra.os, "killpg", killpg)

    expected = exc if error is OSError else timeout
    with pytest.raises(type(expected)) as caught:
        extra._run_bounded_project_test(["pytest"], cwd=None, timeout=1, env={})
    assert caught.value is expected
    assert proc.wait.call_args_list == ([call(timeout=1)] if error is OSError else
                                       [call(timeout=1), call()])
    killpg.assert_called_once_with(proc.pid, signal.SIGKILL)
    assert proc.stdout.closed and proc.stderr.closed
@pytest.mark.asyncio
async def test_file_search_fts_error_is_tool_result(monkeypatch):
    import sqlite3
    from mimir.tools import extra

    class FailingIndexer:
        async def search(self, query, **kwargs):
            assert query == "worklink-1978 OR timeout"
            raise sqlite3.OperationalError('fts5: syntax error near "OR"')

    monkeypatch.setitem(extra._SEARCH_STATE, "indexer", FailingIndexer())
    result = await extra.file_search.coroutine(query="worklink-1978 OR timeout")
    assert "file_search failed: invalid search query" in result
    assert "try simpler terms" in result
