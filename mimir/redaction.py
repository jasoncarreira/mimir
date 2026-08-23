"""Shared best-effort redaction helpers for durable logs.

The patterns here intentionally cover broad token-shaped credentials before
strings land in durable state such as ``turns.jsonl`` or ``events.jsonl``.
False positives are acceptable: the redactor masks values, it never refuses to
log the surrounding context.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import yaml


_CREDENTIAL_KEY = re.compile(
    r"[A-Za-z0-9_.:-]*(?:token|api[_-]?key|password|passwd|secret)", re.IGNORECASE
)
_CREDENTIAL_WORD = re.compile(
    r"(?:token|api[_-]?key|password|passwd|secret)", re.IGNORECASE
)
_CREDENTIAL_WORD_LITERALS = (
    "token",
    "apikey",
    "api_key",
    "api-key",
    "password",
    "passwd",
    "secret",
)
_IGNORECASE_LITERAL_TRANSLATION = {0x017F: "s", 0x0131: "i"}
_BLOCK_HEADER = re.compile(
    r"(?:(?:[!&*](?:<[^>\r\n]+>|[^\s#]+))[ \t]+)*"
    r"(?P<style>[|>])(?P<mods>(?:[1-9][+-]?|[+-][1-9]?)?)"
    r"[ \t]*(?:#.*)?"
)
_NODE_PROPERTIES = re.compile(
    r"(?:(?:[!&*](?:<[^>\r\n]+>|[^\s#]+))[ \t]*)+(?:#.*)?"
)

_COLON_KEY_CHAR = re.compile(r"[A-Za-z0-9_.-]", re.IGNORECASE)
_COLON_CREDENTIAL_PATTERNS = (
    re.compile(
        r"(?i)(?<![A-Za-z0-9_./-])"
        r"(['\"]?[A-Za-z0-9_.-]*(?:token|api[_-]?key|password|passwd|secret)"
        r"['\"]?[ \t]*:[ \t]*\")((?:\\[\s\S]|[^\"\\\n])*)(?=\")"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z0-9_./-])"
        r"(['\"]?[A-Za-z0-9_.-]*(?:token|api[_-]?key|password|passwd|secret)"
        r"['\"]?[ \t]*:[ \t]*')((?:''|[^'\\\n])*)(?=')"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z0-9_./-])"
        r"(['\"]?[A-Za-z0-9_.-]*(?:token|api[_-]?key|password|passwd|secret)"
        r"['\"]?[ \t]*:[ \t]*)"
        r"(?![#!&*]|[|>][-+0-9]*(?:\s|$))([^\s\"',&}]+)"
    ),
)


def _credential_word_starts(text: str, lowered: str) -> list[int]:
    # A few Unicode characters expand when lowercased. Use the regex in that
    # uncommon case so offsets and Unicode IGNORECASE behavior remain exact.
    if len(lowered) != len(text):
        return [match.start() for match in _CREDENTIAL_WORD.finditer(text)]

    starts: set[int] = set()
    for word in _CREDENTIAL_WORD_LITERALS:
        start = lowered.find(word)
        while start != -1:
            starts.add(start)
            start = lowered.find(word, start + 1)
    return sorted(starts)


def _sub_colon_credentials(
    text: str, pattern: re.Pattern[str], lowered: str
) -> str:
    parts: list[str] = []
    copied_to = 0
    for word_start in _credential_word_starts(text, lowered):
        if word_start < copied_to:
            continue

        key_start = word_start
        while key_start and _COLON_KEY_CHAR.fullmatch(text[key_start - 1]) is not None:
            key_start -= 1
        starts = [key_start]
        if key_start and text[key_start - 1] in {'"', "'"}:
            starts.insert(0, key_start - 1)

        match = next(
            (candidate for start in starts if (candidate := pattern.match(text, start))),
            None,
        )
        if match is None or match.end() <= copied_to:
            continue
        parts.append(text[copied_to : match.start()])
        parts.append(match.expand(r"\1[REDACTED]"))
        copied_to = match.end()

    if not parts:
        return text
    parts.append(text[copied_to:])
    return "".join(parts)


def _mask_colon_credentials(text: str) -> str:
    """Run the three colon grammars locally, in their original order."""
    lowered = text.lower()
    if len(lowered) != len(text):
        if _CREDENTIAL_WORD.search(text) is None:
            return text
    else:
        lowered = lowered.translate(_IGNORECASE_LITERAL_TRANSLATION)
        if not any(word in lowered for word in _CREDENTIAL_WORD_LITERALS):
            return text
    for pattern in _COLON_CREDENTIAL_PATTERNS:
        lowered = text.lower()
        if len(lowered) == len(text):
            lowered = lowered.translate(_IGNORECASE_LITERAL_TRANSLATION)
        text = _sub_colon_credentials(text, pattern, lowered)
    return text


# Token-shape redaction for subprocess / event output (pre-OSS hardening,
# review item #8, extended by chainlink #370). Anything a subprocess or event
# payload emits can land in durable JSONL logs, so broad masking is preferable
# to call-site-specific best effort.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:github_pat_|ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9_]+"),
    # Anthropic API keys. Prefix ``sk-ant-`` is stable across the API and
    # Claude Code provisioning paths. Allow the underscore / hyphen alphabet
    # observed in issued keys.
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    # Slack bot, user, app, refresh, and config tokens. Prefix is the
    # disambiguator; payload alphabet is base62-ish + dashes.
    re.compile(r"xox[bpasr]-[A-Za-z0-9-]+"),
    re.compile(r"xapp-[0-9A-Za-z-]{20,}"),
    # Provider keys that commonly appear as bare values in subprocess output.
    re.compile(r"tvly-[A-Za-z0-9_-]{20,}"),
    re.compile(r"pa-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{35}"),
    # OpenAI-style secret keys (``sk-…`` and ``sk-proj-…``). The ``sk-ant-``
    # case is already covered above; this matches the OpenAI shapes without
    # colliding.
    re.compile(r"sk-(?!ant-)[A-Za-z0-9_-]{20,}"),
    # ``Authorization: Bearer <token>`` headers in dumped HTTP traces.
    # Case-insensitive; captures the value through whitespace / quote boundary.
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
    # ``token=…``, ``api_key=…``, ``password=…`` value-style fields (URL query,
    # env var dumps, JSON pretty-prints with bareword keys). The value alphabet
    # stops at common delimiters so the regex doesn't eat the rest of the line.
    re.compile(r"(?i)(token=|api[_-]?key=|password=|passwd=|secret=)([^\s\"',&]+)"),
    # Credential fields in header, YAML, JSON, and Python-repr colon forms.
    # Requiring a credential-like key and a non-path boundary keeps ordinary
    # prose, timestamps, unrelated mappings, and URL userinfo intact.
    #
    # Quoted values run to the CLOSING QUOTE, not to the first space. A
    # passphrase is a credential and contains whitespace, so stopping at the
    # space masks one word and persists the rest. Candidate positions are found
    # cheaply, then each original grammar is checked only at local keys.
    # Double quotes: JSON and YAML both escape with a backslash, and YAML also
    # allows a backslash before a physical newline as a line continuation, so an
    # escape consumes ANY next character.
    #
    # Two guards keep an UNTERMINATED quote from consuming the rest of the log,
    # and they are deliberately redundant: the excluded raw newline bounds a
    # match to its own line, and the lookahead additionally requires a real
    # closing delimiter. Removing either alone changes nothing; removing both
    # lets ``password: "abc`` swallow every following line.
    # Single quotes carry two INCOMPATIBLE grammars and a backslash is exactly
    # where they disagree:
    #
    #   Python-repr — ``\`` escapes the next character, so ``'has \' and "'``
    #                 continues past that apostrophe.
    #   YAML        — ``\`` is literal and ``''`` is the only escape, so
    #                 ``'ends in \'`` genuinely ends at that apostrophe.
    #
    # The same bytes are a different value under each, and nothing local to the
    # match resolves it: deciding by what follows the quote is wrong for a repr
    # whose credential contains ``',``, and preferring either reading in turn
    # produced five successive defects — a leaked tail one way, an erased
    # delimiter or following line the other.
    #
    # So this rule DECLINES the ambiguity instead of guessing. A value holding
    # no backslash is unambiguous under both grammars and is masked; a value
    # containing one is left alone. That is an honest miss, and it can neither
    # leak half a credential nor corrupt the surrounding record. Full
    # arbitration needs a parser rather than a pattern and is tracked with the
    # block-scalar work.
    # Multiline YAML block scalars are handled by the grammar-aware pass below,
    # not by this tuple. The bare-value rule must still DECLINE their indicator
    # and any preceding node properties. Otherwise a parser miss could print
    # ``password: [REDACTED]`` above an untouched body, which is worse for an
    # audit trail than an honest miss.
    #
    # Bare (unquoted) values. The alphabet stops at common delimiters so the
    # regex doesn't eat the rest of the line, and a block-scalar indicator is
    # excluded so it can never be reported as a redacted value.
    *_COLON_CREDENTIAL_PATTERNS,
    # AWS access-key IDs (chainlink #499 — sync with templates/git/pre-commit).
    # The long-lived ``AKIA`` and STS-temp ``ASIA`` prefixes + 16 upper/digit
    # chars are a high-confidence shape; mask the whole value.
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    # ``AWS_SECRET_ACCESS_KEY=…`` / ``AWS_ACCESS_KEY_ID=…`` env-dump forms. The
    # secret-access-key *value* has no fixed shape, so it can only be caught via
    # its variable name (the bare ``secret=``/``api_key=`` group above misses
    # ``..._ACCESS_KEY=`` — no ``api`` prefix, no bare ``secret=``).
    re.compile(r"(?i)(aws_[a-z0-9_]*key(?:_id)?\s*=)([^\s\"',&]+)"),
    # JSON OAuth-token value forms (``"refresh_token": "…"`` etc.) the sibling
    # pre-commit hook already catches; keep the redactor in sync so a token in a
    # subprocess stderr / event payload doesn't land cleartext in events.jsonl.
    re.compile(r'(?i)("(?:access_token|refresh_token|client_secret)"\s*:\s*")([^"]{6,})'),
    # Generic JWTs with long first segments. Preserve the header for diagnosis
    # while masking the payload and signature.
    re.compile(r"(\b[A-Za-z0-9_-]{25,}\.)([A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}\b)"),
    # Discord bot tokens — JWT-shaped with ``MTk…`` / ``MzU…`` prefix for many
    # of them. Use the well-documented 24+.6+27 segment shape.
    re.compile(r"\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b"),
)


def _credential_key(value: object) -> bool:
    return isinstance(value, str) and _CREDENTIAL_KEY.fullmatch(value) is not None


def _yaml_block_scalar_lines(text: str) -> list[int] | None:
    """Return credential block-scalar body lines for a valid YAML document.

    ``None`` asks the caller to use the log-text scanner. A scalar-only YAML
    parse is not considered a document here: arbitrary log text commonly
    parses as one plain scalar while still containing YAML snippets.
    """
    try:
        documents = list(yaml.compose_all(text))
    except Exception:
        return None

    roots = [node for node in documents if node is not None]
    if not roots or not any(
        isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) for node in roots
    ):
        return None

    body_lines: set[int] = set()
    seen: set[int] = set()
    lines = text.splitlines(keepends=True)

    def header_line(node: yaml.ScalarNode) -> int | None:
        for line_number in range(node.start_mark.line, node.end_mark.line + 1):
            if line_number >= len(lines):
                break
            content = _line_content(lines[line_number])
            if line_number == node.start_mark.line:
                content = content[node.start_mark.column :]
            if _BLOCK_HEADER.fullmatch(content.strip()) is not None:
                return line_number
        return None

    def walk(node: yaml.Node) -> None:
        node_id = id(node)
        if node_id in seen:
            return
        seen.add(node_id)
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if (
                    isinstance(key, yaml.ScalarNode)
                    and _credential_key(key.value)
                    and isinstance(value, yaml.ScalarNode)
                    and value.style in {"|", ">"}
                ):
                    start = header_line(value)
                    if start is not None:
                        stop = value.end_mark.line + (value.end_mark.column > 0)
                        body_lines.update(range(start + 1, stop))
                walk(key)
                walk(value)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)

    for root in roots:
        walk(root)
    return sorted(body_lines)


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _line_content(line: str) -> str:
    return line.rstrip("\r\n")


def _sequence_prefix_end(line: str) -> int:
    pos = _line_indent(line)
    while line[pos : pos + 1] == "-" and line[pos + 1 : pos + 2] in {" ", "\t"}:
        pos += 1
        while line[pos : pos + 1] in {" ", "\t"}:
            pos += 1
    return pos


def _mapping_colons(line: str, start: int) -> list[int]:
    """Find mapping separators while ignoring colons inside quoted keys."""
    colons: list[int] = []
    quote: str | None = None
    escaped = False
    pos = start
    while pos < len(line):
        char = line[pos]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == "'" and line[pos + 1 : pos + 2] == "'":
                pos += 1
            elif char == "'":
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == "#":
            break
        elif char == ":":
            colons.append(pos)
        pos += 1
    return colons


def _scanner_key(raw: str) -> str:
    key = raw.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {'"', "'"}:
        key = key[1:-1]
    return key


def _header_indent(header: re.Match[str], base: int) -> int | None:
    digits = "".join(char for char in header.group("mods") if char.isdigit())
    return base + int(digits) if digits else None


def _awaits_node(value: str) -> bool:
    return (
        not value
        or value.startswith("#")
        or _NODE_PROPERTIES.fullmatch(value) is not None
    )


def _scanned_block_scalar_lines(text: str) -> list[int]:
    """Recognize block headers in non-document log text and bound their bodies."""
    lines = text.splitlines(keepends=True)
    body_lines: set[int] = set()
    pending_explicit: int | None = None
    pending_value: int | None = None
    index = 0

    while index < len(lines):
        content = _line_content(lines[index])
        stripped = content.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue

        start = _sequence_prefix_end(content)
        header: re.Match[str] | None = None
        base: int | None = None
        deferred_here = False

        if pending_explicit is not None and start == pending_explicit:
            explicit_value = content[start:]
            if explicit_value.startswith(":"):
                value = explicit_value[1:].strip()
                header = _BLOCK_HEADER.fullmatch(value)
                if header is not None:
                    base = pending_explicit
                elif _awaits_node(value):
                    pending_value = pending_explicit
                    deferred_here = True
            pending_explicit = None

        if header is None and pending_value is not None and not deferred_here:
            candidate = content.strip()
            if _line_indent(content) > pending_value:
                header = _BLOCK_HEADER.fullmatch(candidate)
                if header is not None:
                    base = pending_value
                    pending_value = None
                elif _NODE_PROPERTIES.fullmatch(candidate) is not None:
                    index += 1
                    continue
                else:
                    pending_value = None
            else:
                pending_value = None

        if header is None and content[start : start + 1] == "?":
            key = _scanner_key(content[start + 1 :])
            pending_explicit = start if _credential_key(key) else None
            index += 1
            continue

        if header is None:
            for colon in _mapping_colons(content, start):
                if _credential_key(_scanner_key(content[start:colon])):
                    value = content[colon + 1 :].strip()
                    candidate = _BLOCK_HEADER.fullmatch(value)
                    if candidate is not None:
                        header = candidate
                        base = start
                        break
                    if _awaits_node(value):
                        pending_value = start
                        break
            else:
                pending_explicit = None

        if header is None or base is None:
            index += 1
            continue

        required_indent = _header_indent(header, base)
        first = index + 1
        while first < len(lines) and not _line_content(lines[first]).strip():
            first += 1
        if first >= len(lines):
            break

        content_indent = _line_indent(_line_content(lines[first]))
        if required_indent is not None:
            content_indent = required_indent
        elif content_indent <= base:
            index += 1
            continue
        if _line_indent(_line_content(lines[first])) < content_indent:
            index += 1
            continue

        end = first
        while end < len(lines):
            body = _line_content(lines[end])
            if body.strip() and _line_indent(body) < content_indent:
                break
            if body.strip():
                body_lines.add(end)
            end += 1
        index = end

    return sorted(body_lines)


def _mask_block_scalar_lines(text: str) -> str:
    # A YAML block scalar must contain one of its two style indicators. Keep
    # parser construction off the common durable-log path, where almost every
    # string is ordinary prose, JSON, or a single-line diagnostic.
    if "|" not in text and ">" not in text:
        return text

    try:
        line_numbers = _yaml_block_scalar_lines(text)
        if line_numbers is None:
            line_numbers = _scanned_block_scalar_lines(text)
        if not line_numbers:
            return text
        lines = text.splitlines(keepends=True)
        for line_number in line_numbers:
            if line_number >= len(lines):
                continue
            line = lines[line_number]
            content = _line_content(line)
            if not content.strip():
                continue
            ending = line[len(content) :]
            indent = content[: len(content) - len(content.lstrip(" \t"))]
            lines[line_number] = f"{indent}[REDACTED]{ending}"
        return "".join(lines)
    except Exception:
        # Durable logging must remain best-effort and non-raising for arbitrary
        # subprocess output, even if the parser encounters an unforeseen shape.
        return text


def redact_text(text: str) -> str:
    """Strip token-shaped secrets out of text before it lands in logs.

    Replacement is ``[REDACTED]`` so logs still indicate "something matched a
    token shape here" without exposing the value. YAML credential block bodies
    are handled first with parser marks (or an indentation-aware fallback for
    non-document log text). For the ``bearer …`` and ``token=…`` patterns, the
    prefix is preserved so surrounding context stays readable.
    """
    if not text:
        return text
    out = _mask_block_scalar_lines(text)
    for pat in _TOKEN_PATTERNS:
        if pat is _COLON_CREDENTIAL_PATTERNS[0]:
            out = _mask_colon_credentials(out)
            continue
        if (
            pat is _COLON_CREDENTIAL_PATTERNS[1]
            or pat is _COLON_CREDENTIAL_PATTERNS[2]
        ):
            continue
        # Patterns with capture groups preserve the prefix; the others mask the
        # whole match. The locally anchored colon rules above keep this same
        # two-capture contract.
        if pat.groups == 2:
            out = pat.sub(r"\1[REDACTED]", out)
        else:
            out = pat.sub("[REDACTED]", out)
    return out


def redact_payload(value: Any) -> Any:
    """Recursively redact strings in a JSON-ish payload.

    The event sink accepts arbitrary payload values and serializes with
    ``json.dumps(..., default=str)``. Preserve container shape for normal JSON
    values while redacting token-shaped substrings before serialization. Exotic
    objects are stringified early so ``json.dumps(default=str)`` cannot bypass
    redaction for an object whose ``__str__`` contains a token-shaped value.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {key: redact_payload(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))
