"""Small shared helpers for tool handlers (MCP-side error formatting).

Used by saga, search, and schedule MCP tools — kept generic so any future
in-process tool can wrap arg validation and consistent error responses.
"""

from __future__ import annotations

from typing import Any


class _ArgError(ValueError):
    """Raised by ``_need`` and converted to is_error responses by ``_safe``."""


def _content_block(text: str, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


def _need(args: dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or val == "":
        raise _ArgError(f"argument {key!r} is required and must be a non-empty string")
    return val


def _detect_xml_smuggle(
    args: dict[str, Any], param_names: list[str]
) -> str | None:
    """Return a structural hint if args show XML close-tag smuggle, else None.

    Three recognizable fingerprints (chainlink #131):

    - **Shape 3 (highest signal)**: a string value contains literal
      ``<parameter name=`` — that's an open-tag for a sibling parameter
      leaking into the prior parameter's value, which is the canonical
      signature of the SDK having folded a name-matched close-tag and
      slurped subsequent params into the prior string.
    - **Shape 2**: a string value contains literal ``</invoke>`` — the
      whole-tool-call closer leaked into a parameter value.
    - **Shape 1**: a string value contains ``</NAME>`` where ``NAME``
      matches another expected sibling parameter name from the schema
      (name-matched close-tag confabulation that slurped subsequent
      params into this string).

    Detection runs against the post-parse args dict that mimir's MCP
    handler receives — by then the SDK has already folded the bad bytes
    into JSON. Returning a structural hint here means the hint reaches
    the model on the failing call, so it fixes on retry #1 instead of
    cycling through surface tweaks for 3-5 retries chasing a misleading
    ``'topics_discussed' is a required property`` message.
    """
    sibling_set = set(param_names)
    for key, val in args.items():
        if not isinstance(val, str):
            continue
        # Shape 3 — most reliable, scan first.
        if "<parameter name=" in val:
            return (
                f"parameter `{key}` value contains a literal "
                f"`<parameter name=` open-tag — looks like the next "
                f"parameter's markup leaked into this string. Close every "
                f"`<parameter name=\"X\">` with `</parameter>` (NOT "
                f"`</X>`); the name-matched variant slurps every "
                f"following sibling into this field's value."
            )
        # Shape 2 — whole-call closer leak.
        if "</invoke>" in val:
            return (
                f"parameter `{key}` contains literal `</invoke>` text — "
                f"that's the whole-tool-call closer leaking into a "
                f"parameter value. Per-parameter close is `</parameter>`; "
                f"`</invoke>` appears exactly once at the very end of the "
                f"tool call, after all parameters close."
            )
        # Shape 1 — sibling-name close-tag.
        for sibling in sibling_set:
            if sibling == key:
                continue
            if f"</{sibling}>" in val:
                return (
                    f"parameter `{key}` value contains literal "
                    f"`</{sibling}>` — looks like a name-matched "
                    f"close-tag confabulation. Close-tags are NOT "
                    f"name-matched in this envelope: every "
                    f"`<parameter name=\"X\">` closes with "
                    f"`</parameter>`, regardless of X."
                )
    return None


def _safe(tool_name: str, param_names: list[str] | None = None):
    """Wrap a tool handler so ``_ArgError`` is converted to is_error blocks.

    When ``param_names`` is provided (the list of expected schema
    parameter names for this tool), pre-checks args for XML close-tag
    smuggle (chainlink #131) and short-circuits with a structural hint
    if detected — this replaces what would otherwise be a misleading
    jsonschema 'X is a required property' error from inner validation.
    """

    def deco(fn):
        async def wrapper(args: dict[str, Any]) -> dict[str, Any]:
            if param_names:
                hint = _detect_xml_smuggle(args, param_names)
                if hint:
                    return _content_block(
                        f"{tool_name} failed: {hint}", is_error=True
                    )
            try:
                return await fn(args)
            except _ArgError as exc:
                return _content_block(f"{tool_name} failed: {exc}", is_error=True)

        wrapper.__name__ = fn.__name__
        wrapper.__qualname__ = fn.__qualname__
        return wrapper

    return deco
