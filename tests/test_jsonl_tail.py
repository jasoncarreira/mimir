"""Streaming tail-reader for JSONL files (mimir/_jsonl_tail.py).

The tricky cases are chunk boundaries: lines that span a chunk read,
empty lines, files smaller than one chunk, missing files, lines that
fail to JSON-decode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir._jsonl_tail import _CHUNK_BYTES, tail_jsonl_records


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_returns_records_newest_first(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    _write_jsonl(path, [{"i": 0}, {"i": 1}, {"i": 2}])
    out = list(tail_jsonl_records(path))
    assert [r["i"] for r in out] == [2, 1, 0]


def test_handles_lines_spanning_chunk_boundaries(tmp_path: Path):
    """Force lines large enough that the file requires multiple chunk
    reads, ensuring the leading-fragment stitching works."""
    path = tmp_path / "log.jsonl"
    pad = "x" * (_CHUNK_BYTES // 4)  # ~2 KB padding per record
    records = [{"i": i, "pad": pad} for i in range(10)]
    _write_jsonl(path, records)
    out = list(tail_jsonl_records(path))
    assert [r["i"] for r in out] == list(range(9, -1, -1))


def test_returns_nothing_for_missing_file(tmp_path: Path):
    out = list(tail_jsonl_records(tmp_path / "nope.jsonl"))
    assert out == []


def test_skips_corrupt_lines(tmp_path: Path):
    """Power-loss can leave a half-written line. Tail-reader skips
    rather than raising — the rest of the log still surfaces."""
    path = tmp_path / "log.jsonl"
    path.write_text(
        json.dumps({"i": 0}) + "\n"
        + "{not json\n"
        + json.dumps({"i": 2}) + "\n"
    )
    out = list(tail_jsonl_records(path))
    assert [r["i"] for r in out] == [2, 0]


def test_handles_empty_file(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_text("")
    assert list(tail_jsonl_records(path)) == []


def test_handles_file_with_only_blank_lines(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_text("\n\n\n")
    assert list(tail_jsonl_records(path)) == []


def test_consumer_can_break_early(tmp_path: Path):
    """Generator-shape — caller can stop after a window cutoff without
    paying for the rest of the file."""
    path = tmp_path / "log.jsonl"
    _write_jsonl(path, [{"i": i} for i in range(1000)])
    out = []
    for rec in tail_jsonl_records(path):
        out.append(rec)
        if len(out) == 3:
            break
    assert [r["i"] for r in out] == [999, 998, 997]
