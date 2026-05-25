"""Frontmatter parser for ``mimir/skills/<name>/SKILL.md`` files.

Single source of truth for the SKILL.md YAML frontmatter shape. Used
by ``tests/test_skill_conformance.py`` (chainlink #80) and the skill
catalog generator (chainlink #81 / module ``mimir.skill_catalog``).

The parser is intentionally a small regex-driven walker rather than
PyYAML — keeps the dep surface narrow and the schema check
transparent. Swap in ``yaml.safe_load`` if the SKILL.md schema ever
grows nested structure beyond ``key: value`` pairs and one-level YAML
lists.

Public API:

* :func:`parse_frontmatter` — flat ``key -> value`` dict for the
  leading ``--- ... ---`` block.
* :func:`extract_list_field` — return a YAML list under ``<key>:``
  in the frontmatter (handles block, inline-array, and
  explicitly-empty forms; rejects scalar form).
* :func:`strip_frontmatter` — return the body (everything after the
  closing ``---`` delimiter).
"""

from __future__ import annotations

import re

_FRONTMATTER_DELIM = re.compile(r"^---\s*$")
_KEY_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*)$")
_LIST_ITEM = re.compile(r"^\s+-\s+(?P<value>.+)$")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Return a flat ``key -> value`` map for the leading ``--- ... ---`` block.

    Raises ``ValueError`` if the block is missing or malformed (no
    closing delimiter). Values are stripped; multi-line folded values
    (``description: >`` with subsequent indented lines) are joined into
    the first key's value.

    Folded-scalar contract (``key: >`` / ``key: |``):
        - Continuation lines MUST start with whitespace (i.e. be indented).
        - A non-empty, non-indented line that does NOT match a key pattern
          raises ``ValueError`` — fail-loudly rather than silently swallowing
          a subsequent key or mis-parsing the value. (chainlink #104)
    """
    lines = text.splitlines()
    if not lines or not _FRONTMATTER_DELIM.match(lines[0]):
        raise ValueError("missing opening '---' delimiter")

    out: dict[str, str] = {}
    current_key: str | None = None
    in_folded_block: bool = False  # True after ``key: >`` / ``key: |``
    closed = False
    for raw in lines[1:]:
        if _FRONTMATTER_DELIM.match(raw):
            closed = True
            break
        match = _KEY_LINE.match(raw)
        if match:
            current_key = match.group("key")
            value = match.group("value").strip()
            # ``description: >`` / ``description: |`` opens a folded
            # multi-line block. Continuation lines are accumulated below.
            if value in {">", "|"}:
                out[current_key] = ""
                in_folded_block = True
            else:
                out[current_key] = value
                in_folded_block = False
        elif in_folded_block and raw[0:1].isspace() and raw.strip():
            # Indented continuation line for a folded block.
            prior = out.get(current_key, "")
            out[current_key] = (prior + " " + raw.strip()).strip()
        elif in_folded_block and raw.strip():
            # Non-empty, non-key-matching, non-indented line inside a folded
            # block. This is malformed YAML — the continuation must be indented.
            # Fail loudly so authors notice the mis-parse instead of silently
            # getting a truncated or wrong value. (chainlink #104)
            raise ValueError(
                f"folded-scalar continuation for key '{current_key}' must be"
                f" indented, but got: {raw!r}"
            )
        elif not in_folded_block and current_key is not None and raw.strip():
            # Legacy plain-value continuation (outside a folded block).
            # In practice this only fires for edge-case trailing content;
            # kept for backward compatibility.
            prior = out.get(current_key, "")
            out[current_key] = (prior + " " + raw.strip()).strip()

    if not closed:
        raise ValueError("missing closing '---' delimiter")
    return out


def extract_list_field(text: str, key: str) -> list[str] | None:
    """Return the YAML-list values under ``<key>:`` in the frontmatter,
    or ``None`` if the field is missing entirely. Returns ``[]`` for
    an explicitly empty list (``<key>: []`` or ``<key>:`` with no
    bullet lines).

    Used for ``allowed-tools:`` which is a list shape that the flat
    :func:`parse_frontmatter` collapses awkwardly. Kept separate so the
    primary parser stays simple.

    Supported shapes:

    * Block form::

        allowed-tools:
          - Read
          - Write

    * Inline array form::

        allowed-tools: [Read, Write]

    * Scalar form (``allowed-tools: Foo``) — **rejected**, returns
      ``None``. Coercion to ``["Foo"]`` was the prior behavior; PR
      #130 review pointed out it hides a schema-shape mistake (the
      writer probably meant bullet form, or wrote Python-list shape
      ``allowed-tools: ['Foo', 'Bar']`` that also gets coerced
      wrong). Loud rejection is safer than silent coercion.

    * Explicitly empty (``allowed-tools: []`` or just ``allowed-tools:``
      with no bullets) — returned as ``[]``.
    """
    lines = text.splitlines()
    if not lines or not _FRONTMATTER_DELIM.match(lines[0]):
        return None

    found = False
    in_block = False
    items: list[str] = []
    for raw in lines[1:]:
        if _FRONTMATTER_DELIM.match(raw):
            break
        match = _KEY_LINE.match(raw)
        if match:
            if match.group("key") == key:
                found = True
                in_block = True
                inline_value = match.group("value").strip()
                # Inline form (``allowed-tools: [Foo, Bar]``) — split.
                if inline_value.startswith("[") and inline_value.endswith("]"):
                    payload = inline_value[1:-1].strip()
                    if not payload:
                        return []
                    return [v.strip() for v in payload.split(",")]
                # ``allowed-tools: Foo`` — scalar form. Reject by
                # returning ``None`` so the conformance test reports
                # it as "missing/malformed" with the YAML-list-required
                # message. PR #130 review feedback.
                if inline_value:
                    return None
                # Empty value followed by bullet lines — fall through.
            else:
                # A different top-level key — close the list block but
                # keep ``found`` so accumulated items are returned.
                in_block = False
        elif in_block:
            list_match = _LIST_ITEM.match(raw)
            if list_match:
                items.append(list_match.group("value").strip())
    if found:
        return items
    return None


def strip_frontmatter(text: str) -> str:
    """Return the body — everything after the closing ``---`` delimiter.

    Returns the input unchanged if there is no opening frontmatter
    delimiter; returns ``""`` if the opening delimiter is present but
    the closing one is not (malformed). Companion to
    :func:`parse_frontmatter` for callers that need to scan the body.
    """
    lines = text.splitlines()
    if not lines or not _FRONTMATTER_DELIM.match(lines[0]):
        return text
    for idx, raw in enumerate(lines[1:], start=1):
        if _FRONTMATTER_DELIM.match(raw):
            return "\n".join(lines[idx + 1 :])
    return ""
