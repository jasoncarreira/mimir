from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any


class HandsContractError(ValueError):
    pass


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def _object_schema(
    properties: dict[str, dict[str, Any]], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_STRING = {"type": "string"}

HANDS_V1_WIRE_TOOLS = tuple(
    _freeze(descriptor)
    for descriptor in (
        {
            "name": "read",
            "description": "Read a file from the admitted client-hosted hands provider.",
            "inputSchema": _object_schema({"path": _STRING}, ["path"]),
            "outputSchema": _object_schema({"content": _STRING}, ["content"]),
        },
        {
            "name": "edit",
            "description": "Replace exact text through the admitted client-hosted hands provider.",
            "inputSchema": _object_schema(
                {"path": _STRING, "oldText": _STRING, "newText": _STRING},
                ["path", "oldText", "newText"],
            ),
            "outputSchema": _object_schema(
                {"changed": {"type": "boolean"}}, ["changed"]
            ),
        },
        {
            "name": "shell",
            "description": "Run a command through the admitted client-hosted hands provider.",
            "inputSchema": _object_schema({"command": _STRING}, ["command"]),
            "outputSchema": _object_schema(
                {
                    "stdout": _STRING,
                    "stderr": _STRING,
                    "exitCode": {"type": "integer"},
                },
                ["stdout", "stderr", "exitCode"],
            ),
        },
        {
            "name": "python",
            "description": "Execute Python code in a persistent per-session namespace on the client host. Returns stdout, stderr, the final expression repr, exception, timeout status, and kernel state.",
            "inputSchema": _object_schema({"code": _STRING}, ["code"]),
            "outputSchema": _object_schema(
                {
                    "ok": {"type": "boolean"},
                    "stdout": _STRING,
                    "stderr": _STRING,
                    "value": _STRING,
                    "exception": _STRING,
                    "timedOut": {"type": "boolean"},
                    "kernel": {
                        "type": "string",
                        "enum": ["fresh", "reused", "timed_out", "crashed"],
                    },
                },
                ["ok", "stdout", "stderr", "value", "exception", "timedOut", "kernel"],
            ),
        },
    )
)

HANDS_PROVIDER_TO_WRAPPER = MappingProxyType(
    {
        "read": "hands_read",
        "edit": "hands_edit",
        "shell": "hands_shell",
        "python": "hands_python",
    }
)
HANDS_WRAPPER_TO_PROVIDER = MappingProxyType(
    {wrapper: provider for provider, wrapper in HANDS_PROVIDER_TO_WRAPPER.items()}
)

_ARGUMENT_TYPES = MappingProxyType({
    "read": MappingProxyType({"path": str}),
    "edit": MappingProxyType({"path": str, "oldText": str, "newText": str}),
    "shell": MappingProxyType({"command": str}),
    "python": MappingProxyType({"code": str}),
})
_RESULT_TYPES = MappingProxyType({
    "read": MappingProxyType({"content": str}),
    "edit": MappingProxyType({"changed": bool}),
    "shell": MappingProxyType({"stdout": str, "stderr": str, "exitCode": int}),
    "python": MappingProxyType({
        "ok": bool,
        "stdout": str,
        "stderr": str,
        "value": str,
        "exception": str,
        "timedOut": bool,
        "kernel": str,
    }),
})


def hands_v1_wire_descriptors() -> list[dict[str, Any]]:
    return deepcopy([_thaw(descriptor) for descriptor in HANDS_V1_WIRE_TOOLS])


def _validate_exact_object(
    provider_name: str,
    value: object,
    specifications: Mapping[str, Mapping[str, type]],
    kind: str,
) -> dict[str, Any]:
    expected = specifications.get(provider_name)
    if expected is None:
        raise HandsContractError(f"unknown Hands provider tool {provider_name!r}")
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise HandsContractError(f"malformed Hands {kind}")
    result: dict[str, Any] = {}
    for key, expected_type in expected.items():
        item = value[key]
        if expected_type is int:
            valid = isinstance(item, int) and not isinstance(item, bool)
        else:
            valid = type(item) is expected_type
        if not valid:
            raise HandsContractError(f"malformed Hands {kind}")
        result[key] = item
    if provider_name == "python" and kind == "result" and result["kernel"] not in {
        "fresh", "reused", "timed_out", "crashed"
    }:
        raise HandsContractError("malformed Hands result")
    return result


def validate_tool_arguments(
    provider_name: str, arguments: object
) -> dict[str, Any]:
    return _validate_exact_object(
        provider_name, arguments, _ARGUMENT_TYPES, "arguments"
    )


def validate_tool_result(provider_name: str, result: object) -> dict[str, Any]:
    return _validate_exact_object(provider_name, result, _RESULT_TYPES, "result")
