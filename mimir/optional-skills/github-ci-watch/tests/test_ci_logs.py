"""Streaming control removal shared with github-activity's log ingestion."""
from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest

from github_ci_test_support import poller


@pytest.mark.parametrize("sequence", [
    "\u009b31m", "\u009dhidden\u009c", "\u0090hidden\u009c",
    "\x1bPhidden\x1b\\", "\u202e\u200b\x00\x7f",
    "\x1b\u202e", "\x1b]hidden\x1bXhidden\x07",
])
def test_shared_sanitizer_removes_unicode_and_split_controls(sequence):
    # Split inside the UTF-8 encoding of a C1 control, not just at ESC.
    prefix = b"a" * (64 * 1024 - 1)
    payload = prefix + sequence.encode() + b"FAILED\t\n"
    assert poller._clean_log_tail(BytesIO(payload)) == (
        prefix + b"FAILED\t\n"
    )[-poller.LOG_EXCERPT_BYTES:]


@pytest.mark.parametrize("timeout", [0, -1])
def test_capture_refuses_exhausted_budget_before_subprocess(monkeypatch, timeout):
    calls = []

    def download(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(poller.subprocess, "run", download)
    tail, error = poller.capture_job_log("o/r", 101, token="test-token", timeout=timeout)
    assert not tail and error
    assert calls == []
