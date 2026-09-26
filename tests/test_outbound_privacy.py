from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

from mimir._langchain_claude_code_patches import _claude_code_pre_tool_enforcement
from mimir.access_control import (
    DeclaredShellCommand,
    get_service_principal,
    parse_declared_shell_commands,
)
from mimir.models import AuthContext, InformationFlowLabels
from mimir import outbound_privacy
from mimir.outbound_privacy import scan_outbound
from mimir.read_policy import is_protected_read_path
from mimir.tool_descriptors import get_tool_descriptor
from mimir.tools import budget_gate
from mimir.tools.budget_gate import BudgetGateMiddleware, _outbound_privacy_refusal


TOKEN = "sk-" + "A" * 24
PRIVATE_TERM = "42 Maplewood Drive"


def _auth(*, channel_id: str = "discord:channel:1") -> AuthContext:
    return AuthContext(
        principal="admin",
        canonical_principal="admin",
        roles=("user", "admin"),
        event_ingress="discord",
        trigger="user_message",
        channel_id=channel_id,
        interactivity=None,
        enforcement_enabled=False,
        ifc_labels=InformationFlowLabels(),
    )


def _request(tool: str, arguments: dict[str, Any], auth: AuthContext) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": tool, "args": arguments, "id": "privacy-1", "type": "tool_call"},
        tool=None,
        state=None,
        runtime=Runtime(context=auth),
    )


def _social_service_auth(tmp_path: Path) -> tuple[AuthContext, Path]:
    script = tmp_path / "run-social-cli.sh"
    script.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    declaration = DeclaredShellCommand(
        executable="bash",
        path=Path("/usr/bin/bash"),
        script=script.resolve(),
        options=("--platform", "--dry-run", "--since"),
    )
    base_service = get_service_principal("scheduled_tick")
    assert base_service is not None
    service = replace(base_service, declared_shell_commands=(declaration,))
    return AuthContext(
        principal=f"service:{service.canonical}",
        canonical_principal=service.canonical,
        roles=("service",),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id="scheduler:social-test",
        interactivity=None,
        is_service=True,
        service_authority=service,
        enforcement_enabled=False,
        ifc_labels=InformationFlowLabels(),
    ), script


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync",
        lambda event, **fields: events.append((event, fields)),
    )
    return events


def _run_sync(
    tool: str,
    arguments: dict[str, Any],
    auth: AuthContext,
    executed: list[bool],
) -> ToolMessage:
    def handler(request: ToolCallRequest) -> ToolMessage:
        executed.append(True)
        return ToolMessage(
            content="executed", tool_call_id=request.tool_call["id"], name=tool,
        )

    return BudgetGateMiddleware().wrap_tool_call(_request(tool, arguments, auth), handler)


def test_scan_private_terms_normalizes_whitespace_digits_and_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    assert scan_outbound([PRIVATE_TERM], tool="web_search", sink_category="network") == []

    terms = tmp_path / "private-terms.txt"
    terms.write_text(
        "# operator terms\n(585) 555-0142\n42 Maplewood Drive\n",
        encoding="utf-8",
    )
    findings = scan_outbound(
        ["Call 5855550142 or visit 42  maplewood   drive"],
        tool="web_search",
        sink_category="network",
    )

    assert [finding.detector for finding in findings] == ["private_term", "private_term"]
    assert PRIVATE_TERM.casefold() not in repr(findings).casefold()


def test_credential_finding_contains_only_match_metadata() -> None:
    findings = scan_outbound(
        [f"prefix {TOKEN} suffix"], tool="fetch_url", sink_category="network",
    )
    assert len(findings) == 1
    assert findings[0].detector == "credential"
    assert findings[0].match_length == len(TOKEN)
    assert TOKEN not in repr(findings)


def test_fetch_url_credential_is_always_refused_without_value_in_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMIR_ACCESS_CONTROL_ENFORCED", raising=False)
    events = _capture_events(monkeypatch)
    executed: list[bool] = []
    url = f"https://example.test/path?credential={TOKEN}"

    result = _run_sync("fetch_url", {"url": url}, _auth(), executed)

    assert result.status == "error"
    assert "credential detector" in str(result.content)
    assert TOKEN not in str(result.content)
    assert executed == []
    hard = next(fields for event, fields in events if event == "hard_boundary_denied")
    assert hard["reason"] == "outbound_credential"
    assert TOKEN not in json.dumps(events)


@pytest.mark.parametrize("enforced", [False, True])
def test_web_search_private_term_shadows_then_enforces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforced: bool,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    (tmp_path / "private-terms.txt").write_text(PRIVATE_TERM + "\n", encoding="utf-8")
    monkeypatch.setenv("MIMIR_OUTBOUND_PRIVACY_ENFORCE", "1" if enforced else "0")
    events = _capture_events(monkeypatch)
    executed: list[bool] = []

    result = _run_sync("web_search", {"query": PRIVATE_TERM}, _auth(), executed)

    assert executed == ([] if enforced else [True])
    assert result.status == ("error" if enforced else "success")
    expected_event = "hard_boundary_denied" if enforced else "shadow_tool_decision"
    decision = next(fields for event, fields in events if event == expected_event)
    assert decision["reason"] == "outbound_private_term"
    assert PRIVATE_TERM.casefold() not in json.dumps(events).casefold()
    assert PRIVATE_TERM.casefold() not in str(result.content).casefold()


def test_send_message_scans_only_cross_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_OUTBOUND_PRIVACY_ENFORCE", "1")
    (tmp_path / "private-terms.txt").write_text(PRIVATE_TERM + "\n", encoding="utf-8")
    auth = _auth()

    same = _outbound_privacy_refusal(
        "send_message", {"channel_id": auth.channel_id, "text": PRIVATE_TERM}, auth,
    )
    cross = _outbound_privacy_refusal(
        "send_message", {"channel_id": "discord:channel:2", "text": PRIVATE_TERM}, auth,
    )

    assert same is None
    assert cross is not None
    assert "private_term detector" in cross
    assert PRIVATE_TERM.casefold() not in cross.casefold()


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp_remote_publish",
        "mcp__langchain-tools__mcp_github_create_issue",
    ],
)
def test_nested_external_mcp_credential_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    events = _capture_events(monkeypatch)
    refusal = _outbound_privacy_refusal(
        tool_name,
        {"outer": {"items": ["safe", {"body": TOKEN}]}},
        _auth(),
    )

    assert refusal is not None
    assert "credential detector" in refusal
    assert TOKEN not in refusal
    assert TOKEN not in json.dumps(events)


def test_credential_wins_when_private_term_also_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MIMIR_OUTBOUND_PRIVACY_ENFORCE", raising=False)
    (tmp_path / "private-terms.txt").write_text(PRIVATE_TERM + "\n", encoding="utf-8")
    outbound = f"{PRIVATE_TERM}: {TOKEN}"
    events = _capture_events(monkeypatch)
    executed: list[bool] = []

    middleware_result = _run_sync(
        "web_search", {"query": outbound}, _auth(), executed,
    )
    claude_result = _claude_code_pre_tool_enforcement(
        "WebSearch", {"query": outbound}, "toolu_mixed_privacy", auth_context=_auth(),
    )

    assert middleware_result.status == "error"
    assert executed == []
    assert claude_result["hookSpecificOutput"]["permissionDecision"] == "deny"
    hard_denials = [
        fields for event, fields in events
        if event == "hard_boundary_denied"
    ]
    assert len(hard_denials) == 2
    assert all(fields["reason"] == "outbound_credential" for fields in hard_denials)
    assert not any(event == "shadow_tool_decision" for event, _fields in events)
    assert TOKEN not in str(middleware_result.content)
    assert TOKEN not in json.dumps(claude_result)
    assert TOKEN not in json.dumps(events)


def test_outbox_write_credential_is_refused_without_access_control_enforcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MIMIR_ACCESS_CONTROL_ENFORCED", raising=False)
    events = _capture_events(monkeypatch)
    executed: list[bool] = []

    result = _run_sync(
        "write_file",
        {
            "file_path": "state/pollers/social-cli-bsky/outbox-bsky.yaml",
            "content": f"dispatch:\n  - post:\n      text: {TOKEN}\n",
        },
        _auth(),
        executed,
    )

    assert result.status == "error"
    assert executed == []
    assert "credential detector" in str(result.content)
    assert TOKEN not in str(result.content)
    assert TOKEN not in json.dumps(events)


@pytest.mark.parametrize("enforced", [False, True])
def test_outbox_write_private_term_shadows_then_enforces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforced: bool,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_OUTBOUND_PRIVACY_ENFORCE", "1" if enforced else "0")
    (tmp_path / "private-terms.txt").write_text(PRIVATE_TERM + "\n", encoding="utf-8")
    events = _capture_events(monkeypatch)
    executed: list[bool] = []

    result = _run_sync(
        "write_file",
        {
            "file_path": "state/pollers/social-cli-feed/outbox-bsky.yaml",
            "content": f"dispatch:\n  - post:\n      text: {PRIVATE_TERM}\n",
        },
        _auth(),
        executed,
    )

    assert executed == ([] if enforced else [True])
    assert result.status == ("error" if enforced else "success")
    expected_event = "hard_boundary_denied" if enforced else "shadow_tool_decision"
    decision = next(fields for event, fields in events if event == expected_event)
    assert decision["reason"] == "outbound_private_term"
    assert decision["sink_category"] == "network"
    assert PRIVATE_TERM.casefold() not in json.dumps(events).casefold()


@pytest.mark.parametrize(
    "path",
    [
        "state/notes.yaml",
        "state/pollers/social-cli-bsky/outbox-bsky.yaml.bak",
    ],
)
def test_non_outbox_write_is_not_scanned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    events = _capture_events(monkeypatch)
    executed: list[bool] = []

    result = _run_sync(
        "write_file", {"file_path": path, "content": TOKEN}, _auth(), executed,
    )

    assert result.status == "success"
    assert executed == [True]
    assert not any(
        fields.get("boundary") == "outbound_privacy" for _event, fields in events
    )


def test_outbox_registry_extension_scans_writes_without_gate_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        outbound_privacy,
        "OUTBOX_PATTERNS",
        (*outbound_privacy.OUTBOX_PATTERNS, "state/outbox-test/*.yaml"),
    )
    executed: list[bool] = []

    result = _run_sync(
        "write_file",
        {"file_path": "state/outbox-test/post.yaml", "content": TOKEN},
        _auth(),
        executed,
    )

    assert result.status == "error"
    assert executed == []


def test_edit_file_new_string_is_scanned_for_outbox_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    executed: list[bool] = []

    result = _run_sync(
        "edit_file",
        {
            "file_path": "state/pollers/social-cli-feed/outbox-x.yaml",
            "old_string": "safe",
            "new_string": TOKEN,
        },
        _auth(),
        executed,
    )

    assert result.status == "error"
    assert executed == []


def test_shared_outbox_write_is_scanned_for_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    executed: list[bool] = []

    result = _run_sync(
        "write_file",
        {
            "file_path": "state/pollers/social-cli-feed/outbox.yaml",
            "content": TOKEN,
        },
        _auth(),
        executed,
    )

    assert result.status == "error"
    assert executed == []


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        (
            "Write",
            {
                "file_path": "state/pollers/social-cli-feed/outbox-bsky.yaml",
                "content": TOKEN,
            },
        ),
        (
            "Edit",
            {
                "file_path": "state/pollers/social-cli-feed/outbox-bsky.yaml",
                "old_string": "safe",
                "new_string": TOKEN,
            },
        ),
        (
            "MultiEdit",
            {
                "file_path": "state/pollers/social-cli-feed/outbox-bsky.yaml",
                "edits": [{"old_string": "safe", "new_string": TOKEN}],
            },
        ),
    ],
)
def test_claude_code_native_outbox_writes_are_scanned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    tool_input: dict[str, Any],
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))

    result = _claude_code_pre_tool_enforcement(
        tool_name,
        tool_input,
        "toolu_outbox_write",
        auth_context=_auth(),
    )

    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "credential detector" in output["permissionDecisionReason"]
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize("credential", [False, True])
def test_declared_social_dispatch_scans_seeded_outbox_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential: bool,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)
    outbox = tmp_path / "state/pollers/social-cli-notifications/outbox-bsky.yaml"
    outbox.parent.mkdir(parents=True)
    text = TOKEN if credential else "a clean post"
    outbox.write_text(
        f"dispatch:\n  - post:\n      platform: bsky\n      text: {text}\n",
        encoding="utf-8",
    )
    events = _capture_events(monkeypatch)
    executed: list[bool] = []

    result = _run_sync(
        "shell_exec",
        {
            "command": (
                f"bash {script} social-cli-notifications dispatch --platform bsky"
            ),
        },
        auth,
        executed,
    )

    assert executed == ([] if credential else [True])
    assert result.status == ("error" if credential else "success")
    if credential:
        assert "platform bsky" in str(result.content)
        assert "credential detector" in str(result.content)
        assert TOKEN not in str(result.content)
        assert TOKEN not in json.dumps(events)


@pytest.mark.parametrize(
    "outbox_name",
    ["outbox-bsky.yaml", "outbox.yaml"],
)
def test_bare_social_dispatch_scans_discovered_and_shared_outboxes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outbox_name: str,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)
    outbox = tmp_path / "state/pollers/social-cli-notifications" / outbox_name
    outbox.parent.mkdir(parents=True)
    outbox.write_text(
        f"dispatch:\n  - post:\n      text: {TOKEN}\n", encoding="utf-8",
    )
    executed: list[bool] = []

    result = _run_sync(
        "shell_exec",
        {"command": f"bash {script} social-cli-notifications dispatch"},
        auth,
        executed,
    )

    assert result.status == "error"
    assert executed == []
    assert "credential detector" in str(result.content)
    assert TOKEN not in str(result.content)


def test_bare_social_dispatch_dry_run_is_also_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)
    outbox = tmp_path / "state/pollers/social-cli-notifications/outbox-bsky.yaml"
    outbox.parent.mkdir(parents=True)
    outbox.write_text(
        f"dispatch:\n  - post:\n      text: {TOKEN}\n", encoding="utf-8",
    )

    refusal = _outbound_privacy_refusal(
        "shell_exec",
        {"command": f"bash {script} social-cli-notifications dispatch --dry-run"},
        auth,
    )

    assert refusal is not None
    assert "credential detector" in refusal
    assert TOKEN not in refusal


def test_outbox_symlink_to_non_outbox_is_scanned_on_write_and_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)
    state_dir = tmp_path / "state/pollers/social-cli-notifications"
    target = tmp_path / "state/notes/draft.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        f"dispatch:\n  - post:\n      text: {TOKEN}\n", encoding="utf-8",
    )
    state_dir.mkdir(parents=True)
    outbox = state_dir / "outbox-bsky.yaml"
    outbox.symlink_to(target)

    executed: list[bool] = []
    write_result = _run_sync(
        "write_file",
        {"file_path": str(outbox), "content": TOKEN},
        _auth(),
        executed,
    )
    dispatch_refusal = _outbound_privacy_refusal(
        "shell_exec",
        {
            "command": (
                f"bash {script} social-cli-notifications dispatch --platform bsky"
            ),
        },
        auth,
    )

    assert write_result.status == "error"
    assert executed == []
    assert dispatch_refusal is not None
    assert "credential detector" in dispatch_refusal
    assert TOKEN not in dispatch_refusal


def test_non_outbox_symlink_to_outbox_is_scanned_on_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    outbox = tmp_path / "state/pollers/social-cli-feed/outbox-bsky.yaml"
    outbox.parent.mkdir(parents=True)
    outbox.write_text("safe", encoding="utf-8")
    alias = tmp_path / "state/notes/draft.yaml"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(outbox)
    executed: list[bool] = []

    result = _run_sync(
        "write_file",
        {"file_path": str(alias), "content": TOKEN},
        _auth(),
        executed,
    )

    assert result.status == "error"
    assert executed == []


def test_social_dispatch_detection_does_not_search_command_substrings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)
    outbox = tmp_path / "state/pollers/social-cli-notifications/outbox-bsky.yaml"
    outbox.parent.mkdir(parents=True)
    outbox.write_text(
        f"dispatch:\n  - post:\n      text: {TOKEN}\n", encoding="utf-8",
    )

    refusal = _outbound_privacy_refusal(
        "shell_exec",
        {
            "command": (
                f"bash {script} social-cli-notifications count "
                "--since dispatch --platform bsky"
            ),
        },
        auth,
    )

    assert refusal is None


def test_missing_social_outbox_has_no_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)

    refusal = _outbound_privacy_refusal(
        "shell_exec",
        {
            "command": (
                f"bash {script} social-cli-notifications dispatch --platform bsky"
            ),
        },
        auth,
    )

    assert refusal is None


def test_tagged_social_outbox_is_scanned_as_raw_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)
    outbox = tmp_path / "state/pollers/social-cli-notifications/outbox-bsky.yaml"
    outbox.parent.mkdir(parents=True)
    outbox.write_text(
        "dispatch:\n  - !custom {post: {text: " + TOKEN + "}}\n", encoding="utf-8",
    )

    refusal = _outbound_privacy_refusal(
        "shell_exec",
        {
            "command": (
                f"bash {script} social-cli-notifications dispatch --platform bsky"
            ),
        },
        auth,
    )

    assert refusal is not None
    assert "credential detector" in refusal
    assert TOKEN not in refusal


def test_social_config_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    executed: list[bool] = []

    result = _run_sync(
        "write_file",
        {
            "file_path": "state/pollers/social-cli-feed/config.yaml",
            "content": "state:\n  platformIsolation: false\n",
        },
        _auth(),
        executed,
    )

    assert result.status == "error"
    assert executed == []
    assert "controls which outbox is dispatched" in str(result.content)


def test_social_dispatch_refuses_redirected_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)
    state_dir = tmp_path / "state/pollers/social-cli-notifications"
    state_dir.mkdir(parents=True)
    (state_dir / "config.yaml").write_text(
        "state:\n  stateDir: sub\n", encoding="utf-8",
    )

    refusal = _outbound_privacy_refusal(
        "shell_exec",
        {"command": f"bash {script} social-cli-notifications dispatch"},
        auth,
    )

    assert refusal is not None
    assert "stateDir escapes" in refusal


def test_social_dispatch_scans_shared_outbox_with_isolation_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)
    state_dir = tmp_path / "state/pollers/social-cli-notifications"
    state_dir.mkdir(parents=True)
    (state_dir / "config.yaml").write_text(
        "state:\n  platformIsolation: false\n", encoding="utf-8",
    )
    (state_dir / "outbox.yaml").write_text(
        f"dispatch:\n  - post:\n      text: {TOKEN}\n", encoding="utf-8",
    )
    (state_dir / "outbox-bsky.yaml").write_text(
        "dispatch:\n  - post:\n      text: clean\n", encoding="utf-8",
    )

    refusal = _outbound_privacy_refusal(
        "shell_exec",
        {
            "command": (
                f"bash {script} social-cli-notifications dispatch --platform bsky"
            ),
        },
        auth,
    )

    assert refusal is not None
    assert "credential detector" in refusal


def test_social_dispatch_refuses_unparseable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    auth, script = _social_service_auth(tmp_path)
    config = tmp_path / "state/pollers/social-cli-notifications/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("state: [unterminated", encoding="utf-8")

    refusal = _outbound_privacy_refusal(
        "shell_exec",
        {"command": f"bash {script} social-cli-notifications dispatch"},
        auth,
    )

    assert refusal is not None
    assert "unreadable or unparseable" in refusal


def test_declared_external_shell_payload_is_scanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = parse_declared_shell_commands([{
        "exec": "gog",
        "path": "/bin/echo",
        "subcommands": [["gmail", "send"]],
        "options": ["--body"],
        "external_send": True,
        "payload_args": ["--body"],
    }])
    base_service = get_service_principal("scheduled_tick")
    assert base_service is not None
    service = replace(
        base_service,
        declared_shell_commands=declared,
    )
    auth = AuthContext(
        principal=f"service:{service.canonical}",
        canonical_principal=service.canonical,
        roles=("service",),
        event_ingress=None,
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        interactivity=None,
        is_service=True,
        service_authority=service,
        enforcement_enabled=False,
        ifc_labels=InformationFlowLabels(),
    )
    events = _capture_events(monkeypatch)

    refusal = _outbound_privacy_refusal(
        "shell_exec", {"command": f"gog gmail send --body {TOKEN}"}, auth,
    )

    assert refusal is not None
    assert TOKEN not in refusal
    assert TOKEN not in json.dumps(events)


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("WebFetch", {"url": f"https://example.test/?key={TOKEN}"}),
        ("WebSearch", {"query": f"find {TOKEN}"}),
        (
            "mcp__langchain-tools__fetch_url",
            {"url": f"https://example.test/?key={TOKEN}"},
        ),
    ],
)
def test_claude_code_real_network_tool_names_refuse_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    tool_input: dict[str, str],
) -> None:
    events = _capture_events(monkeypatch)
    result = _claude_code_pre_tool_enforcement(
        tool_name,
        tool_input,
        "toolu_privacy",
        auth_context=_auth(),
    )

    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "credential detector" in output["permissionDecisionReason"]
    assert TOKEN not in json.dumps(result)
    assert TOKEN not in json.dumps(events)


def test_claude_code_bridged_local_write_is_not_treated_as_external_mcp() -> None:
    result = _claude_code_pre_tool_enforcement(
        "mcp__langchain-tools__write_file",
        {"file_path": "/tmp/local.txt", "content": TOKEN},
        "toolu_local_write",
        auth_context=_auth(),
    )

    assert result == {}


@pytest.mark.parametrize("failing_component", ["extractor", "scanner"])
def test_middleware_privacy_errors_return_value_free_refusal(
    monkeypatch: pytest.MonkeyPatch,
    failing_component: str,
) -> None:
    events = _capture_events(monkeypatch)
    if failing_component == "extractor":
        descriptor = get_tool_descriptor("fetch_url")
        assert descriptor is not None

        def fail_extractor(*_args: Any, **_kwargs: Any) -> tuple[str, ...]:
            raise RuntimeError("extractor failed")

        failing_descriptor = replace(descriptor, sink_payload_extractor=fail_extractor)
        monkeypatch.setattr(
            budget_gate,
            "get_tool_descriptor",
            lambda name: failing_descriptor if name == "fetch_url" else get_tool_descriptor(name),
        )
    else:
        monkeypatch.setattr(
            "mimir.outbound_privacy.scan_outbound",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scanner failed")),
        )

    executed: list[bool] = []
    result = _run_sync(
        "fetch_url",
        {"url": f"https://example.test/?value={TOKEN}"},
        _auth(),
        executed,
    )

    assert result.status == "error"
    assert "local content check failed" in str(result.content)
    assert executed == []
    hard = next(fields for event, fields in events if event == "hard_boundary_denied")
    assert hard["boundary"] == "outbound_privacy"
    assert hard["reason"] == "outbound_privacy_check_failed"
    assert TOKEN not in str(result.content)
    assert TOKEN not in json.dumps(events)


@pytest.mark.parametrize("failing_component", ["extractor", "scanner"])
def test_claude_code_privacy_errors_return_value_free_denial(
    monkeypatch: pytest.MonkeyPatch,
    failing_component: str,
) -> None:
    if failing_component == "extractor":
        descriptor = get_tool_descriptor("fetch_url")
        assert descriptor is not None

        def fail_extractor(*_args: Any, **_kwargs: Any) -> tuple[str, ...]:
            raise RuntimeError("extractor failed")

        failing_descriptor = replace(descriptor, sink_payload_extractor=fail_extractor)
        monkeypatch.setattr(
            budget_gate,
            "get_tool_descriptor",
            lambda name: failing_descriptor if name == "fetch_url" else get_tool_descriptor(name),
        )
    else:
        monkeypatch.setattr(
            "mimir.outbound_privacy.scan_outbound",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scanner failed")),
        )

    result = _claude_code_pre_tool_enforcement(
        "WebFetch",
        {"url": f"https://example.test/?value={TOKEN}"},
        f"toolu_{failing_component}_error",
        auth_context=_auth(),
    )

    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "local content check failed" in output["permissionDecisionReason"]
    assert TOKEN not in output["permissionDecisionReason"]


def test_private_terms_file_is_a_protected_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    path = tmp_path / "private-terms.txt"
    path.write_text(PRIVATE_TERM, encoding="utf-8")
    assert is_protected_read_path(path) is True
