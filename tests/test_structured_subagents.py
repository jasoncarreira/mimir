from __future__ import annotations

import json

from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.subagents import _build_task_tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain.tools import ToolRuntime
import pytest

from mimir.subagents import (
    CriticFinding,
    CriticFindings,
    build_mimir_subagents,
    readonly_filesystem_permissions,
)


def test_build_mimir_subagents_registers_structured_critic_without_replacing_gp() -> None:
    specs = build_mimir_subagents()

    # The Worklink epic roles are per-run tool-armed agents constructed inside
    # mimir.worklink.epic_roles — they are intentionally NOT registered here.
    assert [spec["name"] for spec in specs] == ["critic-structured"]
    assert "general-purpose" not in {spec["name"] for spec in specs}
    assert specs[0]["response_format"] is CriticFindings
    assert all(spec["tools"] == [] for spec in specs)
    assert all("Read-only" in spec["description"] for spec in specs)


def test_readonly_permissions_deny_write_catchall() -> None:
    rules = readonly_filesystem_permissions()

    assert len(rules) == 1
    assert rules[0].operations == ["write"]
    assert rules[0].paths == ["/**"]
    assert rules[0].mode == "deny"


def test_readonly_permissions_block_filesystem_write_tool() -> None:
    middleware = FilesystemMiddleware(
        backend=StateBackend(),
        _permissions=readonly_filesystem_permissions(),
    )
    write_tool = next(tool for tool in middleware.tools if tool.name == "write_file")
    runtime = ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="toolu-write",
        store=None,
    )

    result = write_tool.func(
        file_path="/blocked.txt",
        content="should not be written",
        runtime=runtime,
    )

    assert result.status == "error"
    assert result.content == "Error: permission denied for write on /blocked.txt"


def test_task_tool_returns_structured_response_as_json_tool_message() -> None:
    finding = CriticFinding(
        title="Missing regression test",
        severity="important",
        evidence="tests/test_example.py has no coverage for the new branch",
        recommendation="Add a focused test before shipping.",
    )
    structured = CriticFindings(
        verdict="important",
        summary="One test gap remains.",
        findings=[finding],
        open_questions=[],
    )
    runnable = RunnableLambda(
        lambda state: {
            "messages": [AIMessage(content="unstructured fallback should not leak")],
            "structured_response": structured,
        }
    )
    tool = _build_task_tool(
        [
            {
                "name": "critic-structured",
                "description": "returns schema JSON",
                "runnable": runnable,
            }
        ]
    )
    runtime = ToolRuntime(
        state={"messages": [HumanMessage(content="parent prompt")]},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="toolu-test",
        store=None,
    )

    result = tool.func(
        description="review this",
        subagent_type="critic-structured",
        runtime=runtime,
    )

    assert result.update["messages"][0] == ToolMessage(
        json.dumps(structured.model_dump(), separators=(",", ":")),
        tool_call_id="toolu-test",
    )
    payload = json.loads(result.update["messages"][0].content)
    assert payload["verdict"] == "important"
    assert payload["findings"][0]["severity"] == "important"
    assert "unstructured fallback" not in result.update["messages"][0].content


def test_task_tool_validation_failure_surfaces_instead_of_falling_back_to_prose() -> None:
    def invalid_structured_response(_state):
        # Simulate a child graph whose structured-output strategy rejected the model output.
        raise ValueError("structured response validation failed: missing verdict")

    tool = _build_task_tool(
        [
            {
                "name": "critic-structured",
                "description": "returns schema JSON",
                "runnable": RunnableLambda(invalid_structured_response),
            }
        ]
    )
    runtime = ToolRuntime(
        state={"messages": [HumanMessage(content="parent prompt")]},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="toolu-test",
        store=None,
    )

    with pytest.raises(ValueError, match="structured response validation failed"):
        tool.func(
            description="review this",
            subagent_type="critic-structured",
            runtime=runtime,
        )
