from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mimir.acp.hands_contract import (
    HANDS_PROVIDER_TO_WRAPPER,
    HANDS_V1_WIRE_TOOLS,
    HANDS_WRAPPER_TO_PROVIDER,
    HandsContractError,
    hands_v1_wire_descriptors,
    validate_tool_arguments,
    validate_tool_result,
)


ROOT = Path(__file__).resolve().parents[1]


def test_shared_wire_contract_is_stdlib_only_and_exact() -> None:
    assert hands_v1_wire_descriptors() == [
        {
            "name": "read",
            "description": "Read a file from the admitted client-hosted hands provider.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
                "additionalProperties": False,
            },
        },
        {
            "name": "edit",
            "description": "Replace exact text through the admitted client-hosted hands provider.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "oldText": {"type": "string"},
                    "newText": {"type": "string"},
                },
                "required": ["path", "oldText", "newText"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"changed": {"type": "boolean"}},
                "required": ["changed"],
                "additionalProperties": False,
            },
        },
        {
            "name": "shell",
            "description": "Run a command through the admitted client-hosted hands provider.",
            "inputSchema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exitCode": {"type": "integer"},
                },
                "required": ["stdout", "stderr", "exitCode"],
                "additionalProperties": False,
            },
        },
        {
            "name": "python",
            "description": "Execute Python code in a persistent per-session namespace on the client host. Returns stdout, stderr, the final expression repr, exception, timeout status, and kernel state.",
            "inputSchema": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "value": {"type": "string"},
                    "exception": {"type": "string"},
                    "timedOut": {"type": "boolean"},
                    "kernel": {
                        "type": "string",
                        "enum": ["fresh", "reused", "timed_out", "crashed"],
                    },
                },
                "required": [
                    "ok", "stdout", "stderr", "value", "exception", "timedOut", "kernel"
                ],
                "additionalProperties": False,
            },
        },
    ]
    tree = ast.parse((ROOT / "mimir" / "acp" / "hands_contract.py").read_text())
    imports = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imports == {"__future__", "collections", "copy", "types", "typing"}


def test_wire_contract_is_immutable_and_descriptors_are_defensive_copies() -> None:
    assert HANDS_PROVIDER_TO_WRAPPER == {
        "read": "hands_read",
        "edit": "hands_edit",
        "shell": "hands_shell",
        "python": "hands_python",
    }
    assert HANDS_WRAPPER_TO_PROVIDER == {
        "hands_read": "read",
        "hands_edit": "edit",
        "hands_shell": "shell",
        "hands_python": "python",
    }
    with pytest.raises(TypeError):
        HANDS_V1_WIRE_TOOLS[0]["description"] = "changed"
    with pytest.raises(TypeError):
        HANDS_PROVIDER_TO_WRAPPER["other"] = "hands_other"

    first = hands_v1_wire_descriptors()
    first[0]["inputSchema"]["properties"]["path"]["type"] = "integer"

    assert hands_v1_wire_descriptors()[0]["inputSchema"]["properties"]["path"] == {
        "type": "string"
    }


@pytest.mark.parametrize(
    ("name", "arguments", "result"),
    [
        ("read", {"path": "notes.txt"}, {"content": "text"}),
        (
            "edit",
            {"path": "notes.txt", "oldText": "old", "newText": "new"},
            {"changed": True},
        ),
        (
            "shell",
            {"command": "pwd"},
            {"stdout": "", "stderr": "", "exitCode": 0},
        ),
        (
            "python",
            {"code": "1 + 1"},
            {
                "ok": True,
                "stdout": "",
                "stderr": "",
                "value": "2",
                "exception": "",
                "timedOut": False,
                "kernel": "fresh",
            },
        ),
    ],
)
def test_argument_and_result_validators_accept_only_exact_shapes(
    name: str, arguments: dict[str, object], result: dict[str, object]
) -> None:
    assert validate_tool_arguments(name, arguments) == arguments
    assert validate_tool_result(name, result) == result

    for value in ({}, {**arguments, "extra": True}):
        with pytest.raises(HandsContractError, match="malformed Hands arguments"):
            validate_tool_arguments(name, value)
    for value in ({}, {**result, "extra": True}):
        with pytest.raises(HandsContractError, match="malformed Hands result"):
            validate_tool_result(name, value)


@pytest.mark.parametrize(
    ("name", "value", "result"),
    [
        ("read", {"path": 1}, False),
        ("edit", {"path": "p", "oldText": "x", "newText": 1}, False),
        ("shell", {"command": False}, False),
        ("read", {"content": 1}, True),
        ("edit", {"changed": 1}, True),
        ("shell", {"stdout": "", "stderr": "", "exitCode": True}, True),
        ("python", {"code": 1}, False),
    ],
)
def test_argument_and_result_validators_reject_wrong_types(
    name: str, value: dict[str, object], result: bool
) -> None:
    validator = validate_tool_result if result else validate_tool_arguments
    with pytest.raises(HandsContractError):
        validator(name, value)


def test_validators_reject_unknown_tools() -> None:
    with pytest.raises(HandsContractError, match="unknown Hands provider tool"):
        validate_tool_arguments("other", {"code": "pass"})
    with pytest.raises(HandsContractError, match="unknown Hands provider tool"):
        validate_tool_result("other", {})


def test_python_kernel_schema_enum_is_exact() -> None:
    descriptor = hands_v1_wire_descriptors()[3]
    assert descriptor["name"] == "python"
    assert descriptor["outputSchema"]["properties"]["kernel"] == {
        "type": "string",
        "enum": ["fresh", "reused", "timed_out", "crashed"],
    }
    result = {
        "ok": True, "stdout": "", "stderr": "", "value": "", "exception": "",
        "timedOut": False, "kernel": "unknown",
    }
    with pytest.raises(HandsContractError, match="malformed Hands result"):
        validate_tool_result("python", result)
