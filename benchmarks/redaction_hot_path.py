"""Benchmark durable text and nested-payload redaction.

Run from the repository root with:

    uv run --extra dev python benchmarks/redaction_hot_path.py
"""

from __future__ import annotations

import re
import statistics
import time
from collections.abc import Callable, Mapping
from typing import Any

from mimir.redaction import (
    _COLON_CREDENTIAL_PATTERNS,
    _TOKEN_PATTERNS,
    _mask_block_scalar_lines,
    redact_payload,
    redact_text,
)

ITERATIONS = 400
REPEATS = 7
PRE_1539_BASELINE_US = 66.5

_LEGACY_COLON_PATTERNS = (
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


def _legacy_redact_text(text: str) -> str:
    """Run the current pipeline with the three pre-optimization colon scans."""
    if not text:
        return text
    out = _mask_block_scalar_lines(text)
    for pattern in _TOKEN_PATTERNS:
        if pattern is _COLON_CREDENTIAL_PATTERNS[0]:
            for legacy_pattern in _LEGACY_COLON_PATTERNS:
                out = legacy_pattern.sub(r"\1[REDACTED]", out)
        elif (
            pattern is _COLON_CREDENTIAL_PATTERNS[1]
            or pattern is _COLON_CREDENTIAL_PATTERNS[2]
        ):
            continue
        elif pattern.groups == 2:
            out = pattern.sub(r"\1[REDACTED]", out)
        else:
            out = pattern.sub("[REDACTED]", out)
    return out


def _legacy_redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _legacy_redact_text(value)
    if isinstance(value, Mapping):
        return {key: _legacy_redact_payload(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_legacy_redact_payload(item) for item in value)
    if isinstance(value, list):
        return [_legacy_redact_payload(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _legacy_redact_text(str(value))


def _samples() -> tuple[str, ...]:
    secret = "runtime-" + "credential-" + "value"
    plain_tool_output = "Read 37 records from /srv/mimir/data and retained context. " * 6
    json_tool_call = (
        '{"type":"tool_call","name":"memory_query","args":'
        '{"query":"release status","limit":20},"id":"toolu_1276"}'
    )
    error_line = (
        "2026-08-17T12:30:41Z ERROR subprocess failed: permission denied "
        "for /srv/mimir/worktree/output.json (exit status 1)"
    )
    blob_2k = ("record=accepted path=/srv/mimir/data item=ordinary-context\n" * 40)[:2048]
    multiline_record = "\n".join(
        f"line={line:02d} status=ok detail=durable tool result retained"
        for line in range(49)
    ) + f"\nMIMIR_API_KEY: {secret}"
    return plain_tool_output, json_tool_call, error_line, blob_2k, multiline_record


def _median_us(operation: Callable[[], object], calls_per_repeat: int) -> float:
    samples: list[float] = []
    for _ in range(REPEATS):
        start = time.perf_counter_ns()
        for _ in range(ITERATIONS):
            operation()
        elapsed = time.perf_counter_ns() - start
        samples.append(elapsed / (ITERATIONS * calls_per_repeat * 1_000))
    return statistics.median(samples)


def main() -> None:
    samples = _samples()
    payload = {
        "event": "tool_result",
        "content": samples[0],
        "metadata": {"call": samples[1], "diagnostics": [samples[2], samples[3]]},
        "records": (samples[4], "safe context"),
    }

    current_text_us = _median_us(
        lambda: tuple(redact_text(sample) for sample in samples), len(samples)
    )
    legacy_text_us = _median_us(
        lambda: tuple(_legacy_redact_text(sample) for sample in samples), len(samples)
    )
    current_payload_us = _median_us(lambda: redact_payload(payload), 1)
    legacy_payload_us = _median_us(lambda: _legacy_redact_payload(payload), 1)

    print(f"redact_text current: {current_text_us:6.1f} us/call")
    print(f"redact_text 3-scan:  {legacy_text_us:6.1f} us/call")
    print(f"pre-#1539 baseline:   {PRE_1539_BASELINE_US:6.1f} us/call")
    print(f"baseline ratio:       {current_text_us / PRE_1539_BASELINE_US:6.2f}x")
    print(f"redact_payload current: {current_payload_us:6.1f} us/call")
    print(f"redact_payload 3-scan:  {legacy_payload_us:6.1f} us/call")


if __name__ == "__main__":
    main()
