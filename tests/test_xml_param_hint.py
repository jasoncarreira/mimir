"""chainlink #21 — Better MCP tool-call validation error.

When the model malforms a tool call by smuggling XML-style
``<parameter name="...">`` syntax inside a JSON string field, the
underlying mcp library returns a misleading "is a required property"
error. We wrap the CallToolRequest handler so that validation errors
get an embedded-XML hint appended whenever the original args contain
the giveaway pattern.

Tests exercise:
1. Detector matches the param-tag shapes and ignores non-matching prose.
2. Validation error + embedded XML → hint appended.
3. Validation error + no embedded XML → standard message unchanged.
4. Successful tool call → handler unchanged (no hint, no munging).
5. Non-validation tool error → no hint (preserves existing semantics).
"""

from __future__ import annotations

from typing import Any

import pytest
from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp import types as mcp_types

from mimir.tools import (
    _XML_PARAM_RE,
    _detect_embedded_xml,
    _install_xml_hint_wrapper,
)


# ─── Detector ────────────────────────────────────────────────────────


def test_detector_matches_param_opening_tag():
    assert (
        _detect_embedded_xml({"x": 'pre <parameter name="topics">[1]</parameter>'})
        is True
    )
    # Tab/newline whitespace between `parameter` and `name="` also matches.
    assert _detect_embedded_xml({"x": '<parameter\tname="t">v'}) is True
    assert _detect_embedded_xml({"x": '<parameter\n  name="t">v'}) is True


def test_detector_ignores_closing_tags_alone():
    # Closing tags alone are too generic — they collide with HTML
    # (``</a>``, ``</span>``) — so we don't fire on them. The original
    # failing turn 9c8921ea286c had the opening `<parameter name="...">`
    # too; that's the load-bearing signal.
    assert _detect_embedded_xml({"summary": "text </topics_discussed>\n"}) is False


def test_detector_ignores_clean_strings():
    assert _detect_embedded_xml({"summary": "fine prose, no tags"}) is False


def test_detector_ignores_generic_html_tags():
    assert _detect_embedded_xml({"x": '<a href="x">link</a>'}) is False
    assert _detect_embedded_xml({"x": "<DIV>html</DIV>"}) is False
    assert _detect_embedded_xml({"x": '<input type="text" name="foo">'}) is False


def test_detector_walks_lists_and_dicts():
    assert (
        _detect_embedded_xml({"items": ["clean", '<parameter name="x">v']}) is True
    )
    assert (
        _detect_embedded_xml({"nested": {"k": ["one", '<parameter name="x">']}}) is True
    )


def test_detector_handles_non_dict_input():
    assert _detect_embedded_xml(None) is False  # type: ignore[arg-type]
    assert _detect_embedded_xml("string") is False  # type: ignore[arg-type]


def test_xml_param_re_compiles():
    assert _XML_PARAM_RE.search('<parameter name="x">') is not None
    assert _XML_PARAM_RE.search("plain prose") is None
    assert _XML_PARAM_RE.search("</summary>") is None  # closing-only doesn't trip


# ─── Wrapper integration ─────────────────────────────────────────────


@tool(
    "saga_session_end_stub",
    "Stub mirroring saga_session_end's required-fields shape for the test.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "topics_discussed": {"type": "array"},
        },
        "required": ["summary", "topics_discussed"],
    },
)
async def _stub_handler(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "ok"}]}


@tool(
    "always_errors",
    "Stub that always returns a non-validation error from the handler.",
    {"type": "object", "properties": {"x": {"type": "string"}}},
)
async def _always_errors_handler(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": "tool failed for reasons"}],
        "is_error": True,
    }


def _make_server():
    config = create_sdk_mcp_server(
        name="test-xml-hint",
        version="0.1.0",
        tools=[_stub_handler, _always_errors_handler],
    )
    # McpSdkServerConfig is a TypedDict; the live server instance lives
    # under the ``instance`` key (mirrors mimir/tools.py:build_mcp_server).
    instance = config["instance"]
    _install_xml_hint_wrapper(instance)
    return instance


async def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    handler = server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )
    return await handler(req)


def _result_text(result: Any) -> str:
    cr = result.root
    return cr.content[0].text


def _result_is_error(result: Any) -> bool:
    return bool(result.root.isError)


@pytest.mark.asyncio
async def test_validation_error_with_embedded_xml_gets_hint():
    server = _make_server()
    result = await _call(
        server,
        "saga_session_end_stub",
        {
            "summary": (
                "did stuff </summary>\n"
                '<parameter name="topics_discussed">[\"a\"]</parameter>'
            ),
        },
    )
    assert _result_is_error(result) is True
    text = _result_text(result)
    assert text.startswith("Input validation error:")
    assert "Hint: one of the string args contains" in text
    assert "JSON field" in text


@pytest.mark.asyncio
async def test_validation_error_without_embedded_xml_unchanged():
    server = _make_server()
    result = await _call(
        server,
        "saga_session_end_stub",
        {"summary": "clean text, no embedded xml"},
        # missing required topics_discussed
    )
    assert _result_is_error(result) is True
    text = _result_text(result)
    assert text.startswith("Input validation error:")
    assert "Hint: " not in text


@pytest.mark.asyncio
async def test_successful_call_unchanged():
    server = _make_server()
    result = await _call(
        server,
        "saga_session_end_stub",
        {"summary": "ok", "topics_discussed": ["one"]},
    )
    assert _result_is_error(result) is False
    text = _result_text(result)
    assert text == "ok"
    assert "Hint:" not in text


@pytest.mark.asyncio
async def test_non_validation_tool_error_unchanged_even_with_xml():
    """Tool returns is_error=True but the message isn't ``Input validation
    error:`` — the wrapper must not append the hint, or it would mislead
    on every unrelated tool failure."""
    server = _make_server()
    result = await _call(
        server,
        "always_errors",
        {"x": 'this contains <parameter name="t">v as a payload'},
    )
    assert _result_is_error(result) is True
    text = _result_text(result)
    assert text == "tool failed for reasons"
    assert "Hint:" not in text


@pytest.mark.asyncio
async def test_wrapper_idempotent():
    """Re-installing the wrapper on the same server must not double-wrap.
    Wraps go through a sentinel attribute to detect already-wrapped
    handlers."""
    server = _make_server()
    handler_after_first = server.request_handlers[mcp_types.CallToolRequest]
    _install_xml_hint_wrapper(server)
    handler_after_second = server.request_handlers[mcp_types.CallToolRequest]
    assert handler_after_first is handler_after_second


@pytest.mark.asyncio
async def test_wrapper_no_op_on_missing_handlers_dict():
    """If somehow the server doesn't expose ``request_handlers`` (future
    library shape change), the install function returns silently rather
    than crashing tool registration."""

    class Bare:
        pass

    # Should not raise.
    _install_xml_hint_wrapper(Bare())
    _install_xml_hint_wrapper(None)
