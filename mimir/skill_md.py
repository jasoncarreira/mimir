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
* :func:`strip_frontmatter` — return the body (everything after the
  closing ``---`` delimiter).
* :func:`parse_env_block` — return ``(required, optional)`` env-var
  specs from a nested ``env:`` block (uses ``yaml.safe_load``).
"""

from __future__ import annotations

import re

_FRONTMATTER_DELIM = re.compile(r"^---\s*$")
_KEY_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*)$")


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


def strip_frontmatter(text: str) -> str:
    """Return the body — everything after the closing ``---`` delimiter.

    Returns the input unchanged if there is no opening frontmatter
    delimiter **or** if the opening delimiter is present but the closing
    one is missing (malformed / unterminated). The "return unchanged on
    malformed" choice is intentional: callers that inject the body into
    a prompt should see the raw content rather than an empty string —
    losing content silently is worse than displaying messy YAML. (Prior
    to chainlink #212 this returned ``""`` for malformed files; changed
    to match the resolver's prompt-injection-safe behavior.)

    Companion to :func:`parse_frontmatter` for callers that need to scan
    the body.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "".join(lines[i + 1 :]).lstrip()
    # Unterminated frontmatter — return whole text rather than swallowing
    # the file body; safer for prompt injection use cases.
    return text


# ─── Nested env block (yaml.safe_load) ───────────────────────────────


def _raw_frontmatter_yaml(text: str) -> str:
    """Extract the raw YAML string between the opening and closing ``---``
    delimiters. Raises ``ValueError`` if either delimiter is missing."""
    lines = text.splitlines()
    if not lines or not _FRONTMATTER_DELIM.match(lines[0]):
        raise ValueError("missing opening '---' delimiter")
    raw_lines: list[str] = []
    for raw in lines[1:]:
        if _FRONTMATTER_DELIM.match(raw):
            return "\n".join(raw_lines)
        raw_lines.append(raw)
    raise ValueError("missing closing '---' delimiter")


def _extract_env_yaml(raw: str) -> str | None:
    """Extract just the ``env:`` block from raw frontmatter YAML.

    Rather than parsing the entire frontmatter with ``yaml.safe_load``
    (which fails on SKILL.md descriptions that contain bare colons),
    this function extracts only the ``env:`` key and its indented children,
    returning a YAML string suitable for ``yaml.safe_load``.

    Returns ``None`` if no ``env:`` top-level key is present.
    """
    lines = raw.splitlines()
    env_start: int | None = None
    for i, line in enumerate(lines):
        # Top-level key: ``env:`` with no leading whitespace.
        if re.match(r"^env:\s*$", line):
            env_start = i
            break
    if env_start is None:
        return None

    # Collect ``env:`` line plus all indented (child) lines.
    env_lines = [lines[env_start]]
    for line in lines[env_start + 1 :]:
        # Next non-blank top-level key → stop.
        if line and not line[0].isspace():
            break
        env_lines.append(line)
    return "\n".join(env_lines)


def parse_env_block(text: str) -> tuple[list[dict], list[dict]]:
    """Return ``(required, optional)`` env-var spec dicts from the frontmatter.

    Each dict has keys:
    * ``name`` (str)
    * ``description`` (str, empty-string default)
    * ``example`` (str, empty-string default)
    * ``only_if`` (str ``"VAR=value"`` or ``None``)
    * ``required`` (bool — ``True`` when from the ``required:`` sublist)

    Uses ``yaml.safe_load`` on the ``env:`` sub-block only, so SKILL.md
    descriptions that contain bare colons (a common pattern) don't break
    the parse. Called by ``mimir.skill_install`` to drive interactive
    env-var prompting (``mimir skills install --configure``).

    Returns ``([], [])`` when no ``env:`` block is present or on any parse
    error — never raises.
    """
    import yaml  # lazy: only needed for skills with an env: block

    try:
        raw = _raw_frontmatter_yaml(text)
        env_yaml = _extract_env_yaml(raw)
        if env_yaml is None:
            return [], []
        data: dict = yaml.safe_load(env_yaml) or {}
    except Exception:
        return [], []

    env = data.get("env") or {}
    if not isinstance(env, dict):
        return [], []

    def _normalise(items: object, is_required: bool) -> list[dict]:
        if not isinstance(items, list):
            return []
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict) or "name" not in item:
                continue
            out.append(
                {
                    "name": str(item["name"]),
                    "description": str(item.get("description") or ""),
                    "example": str(item.get("example") or ""),
                    "only_if": str(item["only_if"]) if "only_if" in item else None,
                    "required": is_required,
                }
            )
        return out

    return (
        _normalise(env.get("required"), True),
        _normalise(env.get("optional"), False),
    )
