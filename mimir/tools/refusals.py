"""Typed model-facing tool refusals.

A policy refusal happens before a tool exposes protected result data.  It is
returned to the model like other ``ToolException`` failures, but must not taint
the turn as though protected content had been ingested.
"""

from __future__ import annotations

from langchain_core.tools import ToolException


class ToolPolicyRefusal(ToolException):
    """A policy or input refusal raised before protected result data flows."""


__all__ = ["ToolPolicyRefusal"]
