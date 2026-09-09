from __future__ import annotations

import json

import pytest

from mimir.acp.audit import MAX_AUDIT_PATH_BYTES, safe_log_event


@pytest.mark.asyncio
async def test_scope_audit_is_bounded_json_on_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    path = "/outside/new\nline\r\t\x00name"
    await safe_log_event(
        "acp_permission_outcome", path=path, outcome="approved",
        resource_resolvable=True, wrapper_name="untrusted-wrapper",
        command="secret-command", code="secret-code", content="secret-content",
        arguments={"path": "untrusted-input"},
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert len(output.err.splitlines()) == 1
    assert json.loads(output.err) == {
        "type": "acp_permission_outcome", "wrapper_name": "hands_request_scope",
        "path": path, "outcome": "approved", "resource_resolvable": True,
    }
    assert "secret" not in output.err
    assert "untrusted" not in output.err


@pytest.mark.asyncio
async def test_scope_audit_bounds_path_and_rejects_unknown_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await safe_log_event("acp_permission_outcome", path="é" * 10000, outcome="secret-command")
    output = capsys.readouterr()
    result = json.loads(output.err)
    assert len(result["path"].encode("utf-8")) <= MAX_AUDIT_PATH_BYTES
    assert result["outcome"] == "denied"
    assert "secret-command" not in output.err
    assert output.out == ""


@pytest.mark.asyncio
async def test_scope_audit_sink_failure_does_not_break_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenStderr:
        def write(self, text: str) -> None:
            raise OSError("closed pipe")

    monkeypatch.setattr("mimir.acp.audit.sys.stderr", BrokenStderr())
    await safe_log_event("acp_permission_outcome", path="/outside", outcome="denied")


@pytest.mark.asyncio
async def test_scope_audit_does_not_emit_arbitrary_event_types(capsys: pytest.CaptureFixture[str]) -> None:
    await safe_log_event("secret-command", path="/outside", outcome="approved")
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.asyncio
async def test_unconfined_risk_audit_has_no_model_path_or_execution_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await safe_log_event(
        "acp_permission_outcome", wrapper_name="hands_unconfined_execution",
        path="secret/path", outcome="approved", resource_resolvable=True,
        command="secret-command", content="secret-content", reason="secret-reason",
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "secret" not in output.err
    assert json.loads(output.err) == {
        "type": "acp_permission_outcome", "wrapper_name": "hands_unconfined_execution",
        "path": "<unconfined>", "outcome": "approved", "resource_resolvable": False,
    }
