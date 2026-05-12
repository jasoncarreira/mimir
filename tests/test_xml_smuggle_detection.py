"""XML close-tag smuggle detection at the MCP arg-validation boundary.

Covers chainlink #131: when the upstream Claude SDK parser folds a
model-emitted name-matched close-tag (``<parameter name="X">VALUE</X>``
instead of the envelope's ``</parameter>``) into args, downstream
``jsonschema`` reports a misleading ``'topics_discussed' is a required
property`` error and the model cycles through surface tweaks chasing
the wrong fingerprint.

The fix at mimir's own validation boundary recognizes three shapes:

- Shape 3 (canonical signature): a string value contains literal
  ``<parameter name=`` text — sibling open-tag leaked.
- Shape 2: a string value contains literal ``</invoke>`` — whole-call
  closer leaked.
- Shape 1: a string value contains ``</NAME>`` where ``NAME`` matches
  a sibling parameter name from the tool's schema.

The detection is wired into ``_safe`` so it short-circuits with a
structural hint before jsonschema runs, fixing the misleading-error
problem at the failing call.
"""

from __future__ import annotations

import pytest

from mimir._tool_helpers import _content_block, _detect_xml_smuggle, _safe


# --- _detect_xml_smuggle direct tests ---------------------------------


def test_detects_invoke_closer_leak():
    """Shape 2: ``</invoke>`` literal inside a string value."""
    args = {"summary": "VALUE</invoke>"}
    hint = _detect_xml_smuggle(args, ["summary", "topics_discussed"])
    assert hint is not None
    assert "</invoke>" in hint
    assert "summary" in hint


def test_detects_name_matched_close_tag():
    """Shape 1: ``</SIBLING>`` inside a string value."""
    args = {"summary": "VALUE</topics>\nmore text"}
    hint = _detect_xml_smuggle(args, ["summary", "topics"])
    assert hint is not None
    assert "</topics>" in hint
    assert "name-matched" in hint


def test_detects_parameter_open_tag_leak():
    """Shape 3 (canonical signature): a sibling ``<parameter name=`` open
    tag leaked into a parameter value. Highest-signal detector — that's
    the actual smuggle byte sequence, not just a downstream symptom."""
    args = {"summary": "VALUE\n<parameter name=\"topics_discussed\">[]"}
    hint = _detect_xml_smuggle(args, ["summary", "topics_discussed"])
    assert hint is not None
    assert "parameter" in hint.lower()


def test_shape_3_takes_priority_over_shape_2():
    """If both Shape 3 and Shape 2 are present, Shape 3 (the canonical
    signature) wins — the hint guides the model to the actual structural
    fix instead of the noisier `</invoke>` symptom."""
    args = {"summary": "VALUE\n<parameter name=\"x\">y</invoke>"}
    hint = _detect_xml_smuggle(args, ["summary", "x"])
    assert hint is not None
    assert "<parameter name=" in hint
    # The Shape 3 hint includes the `<parameter name=` phrase verbatim.


def test_no_false_positive_on_unrelated_close_tag():
    """A ``</tag>`` that doesn't match any sibling param name should not
    trigger Shape 1 — prose like ``<div>...</div>`` is legitimate."""
    args = {"summary": "Here's some HTML: <div>text</div>"}
    hint = _detect_xml_smuggle(
        args, ["summary", "topics_discussed", "unfinished"]
    )
    assert hint is None


def test_no_false_positive_on_clean_args():
    """Fully clean string args produce no hint."""
    args = {
        "summary": "Clean prose, no XML.",
        "topics_discussed": "also clean",
    }
    hint = _detect_xml_smuggle(args, ["summary", "topics_discussed"])
    assert hint is None


def test_self_match_close_tag_ignored():
    """A ``</summary>`` inside the ``summary`` field is skipped (the
    detection only fires on cross-param shapes). The canonical smuggle
    leaves Shape 3 markup behind too, so it gets caught by the
    ``<parameter name=`` scanner regardless — the self-match skip is
    just to avoid noisy false positives from prose like 'see </summary>'
    inside a legitimate ``summary`` field describing XML."""
    args = {"summary": "I will describe </summary> in my doc."}
    # No sibling-name close-tag, no </invoke>, no <parameter name= — none
    # of the three shapes should fire.
    hint = _detect_xml_smuggle(args, ["summary", "topics"])
    assert hint is None


def test_non_string_values_skipped():
    """Lists, ints, bools etc. don't get scanned — the smuggle signature
    only appears in string param values."""
    args = {
        "atom_ids": ["a", "b", "c"],  # list values are skipped
        "top_k": 12,
        "dry_run": True,
        "content": "clean",
    }
    hint = _detect_xml_smuggle(
        args, ["atom_ids", "top_k", "dry_run", "content"]
    )
    assert hint is None


def test_empty_param_names_still_detects_invoke_and_open_tag():
    """Even when the schema list is empty, Shape 2 (`</invoke>`) and
    Shape 3 (`<parameter name=`) are name-independent and still fire."""
    args = {"foo": "VALUE</invoke>"}
    hint = _detect_xml_smuggle(args, [])
    assert hint is not None
    assert "</invoke>" in hint

    args2 = {"foo": "VALUE<parameter name=\"bar\">"}
    hint2 = _detect_xml_smuggle(args2, [])
    assert hint2 is not None


# --- _safe wrapper integration ----------------------------------------


@pytest.mark.asyncio
async def test_safe_short_circuits_on_smuggle():
    """``_safe`` returns the hint as an is_error block BEFORE the inner
    handler runs, so the misleading downstream error never fires."""
    inner_called = False

    @_safe("test_tool", param_names=["foo", "bar"])
    async def handler(args):
        nonlocal inner_called
        inner_called = True
        return _content_block("inner ran")

    result = await handler({"foo": "VALUE</invoke>"})
    assert result.get("is_error") is True
    assert "</invoke>" in result["content"][0]["text"]
    assert inner_called is False


@pytest.mark.asyncio
async def test_safe_short_circuits_on_open_tag_leak():
    """Shape 3 short-circuit via the wrapper."""
    inner_called = False

    @_safe("test_tool", param_names=["summary", "topics_discussed"])
    async def handler(args):
        nonlocal inner_called
        inner_called = True
        return _content_block("inner ran")

    result = await handler(
        {"summary": "VALUE\n<parameter name=\"topics_discussed\">"}
    )
    assert result.get("is_error") is True
    assert "test_tool failed" in result["content"][0]["text"]
    assert inner_called is False


@pytest.mark.asyncio
async def test_safe_passthrough_on_clean_args():
    """Clean args reach the inner handler unmolested."""

    @_safe("test_tool", param_names=["foo"])
    async def handler(args):
        return _content_block("ok")

    result = await handler({"foo": "clean value"})
    assert result.get("is_error") is not True
    assert result["content"][0]["text"] == "ok"


@pytest.mark.asyncio
async def test_safe_no_param_names_skips_detection():
    """Backwards-compat: ``_safe`` without ``param_names`` doesn't check
    anything — pre-chainlink-#131 call sites keep working as-is."""

    @_safe("test_tool")
    async def handler(args):
        return _content_block("ok")

    # Even with smuggle-looking content, no detection runs:
    result = await handler({"foo": "VALUE</invoke>"})
    assert result["content"][0]["text"] == "ok"
    assert result.get("is_error") is not True


@pytest.mark.asyncio
async def test_safe_empty_param_names_skips_detection():
    """``param_names=[]`` is treated like the no-param-names case (the
    detection would be a no-op for Shape 1 with an empty list, and we
    don't want to false-positive zero-arg tools on Shape 2/3 from
    callers who consciously opted in with an empty list)."""

    @_safe("test_tool", param_names=[])
    async def handler(args):
        return _content_block("ok")

    result = await handler({"foo": "VALUE</invoke>"})
    # Empty list is falsy, so the detection branch is skipped.
    assert result["content"][0]["text"] == "ok"
    assert result.get("is_error") is not True


@pytest.mark.asyncio
async def test_safe_hint_includes_tool_name():
    """The is_error hint is prefixed with ``<tool_name> failed:`` so the
    model can tell which call produced the structural error."""

    @_safe("saga_end_session", param_names=["summary", "topics"])
    async def handler(args):
        return _content_block("inner ran")

    result = await handler(
        {"summary": "VALUE\n<parameter name=\"topics\">"}
    )
    assert result.get("is_error") is True
    assert result["content"][0]["text"].startswith("saga_end_session failed:")
