"""Streaming tail-reader for JSONL files.

Used by ``feedback.py`` (recent feedback signals from events.jsonl /
turns.jsonl) and ``session_boundary_log.py`` (local mirror tail). Both
read newest-first up to a small bound — typically ≤ 20 records — but
the underlying file can grow unbounded (events.jsonl is the firehose).
Loading the whole file into memory per turn is an O(file_size) memory
spike on every prompt assembly; this module reads from the tail in
chunks so memory use stays O(chunk_size) regardless of file length.

Usage::

    for rec in tail_jsonl_records(path):
        if too_old(rec):
            break
        consume(rec)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# 8 KiB per chunk. Typical JSONL records are 100 B–4 KB, so each chunk
# yields multiple records; a small bound (≤ ~20 records) almost always
# resolves in one chunk read. Bigger chunks waste memory on small files;
# smaller chunks waste seeks.
_CHUNK_BYTES = 8192


def tail_jsonl_records(path: Path) -> Iterator[dict]:
    """Yield JSON-decoded records from ``path`` newest-first.

    Streams chunks from the end of the file rather than reading the
    whole file into memory. Skips lines that fail to JSON-decode (the
    firehose may have torn lines from a crash). Yields nothing when the
    file is missing or unreadable.
    """
    try:
        for line in _tail_lines(path):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    except OSError:
        return


def count_lines_chunked(path: Path, *, chunk_bytes: int = 65536) -> int:
    """Count newline-terminated lines in ``path`` without reading the
    whole file into memory.

    Streams the file in 64 KiB chunks and counts ``\\n`` bytes. Used
    by ``EventLogger`` and ``TurnLogger`` at startup to learn how many
    records the existing log holds (so the trim hysteresis knows when
    to fire) without a multi-hundred-MB memory spike on a hot log.

    A trailing line without a final newline is counted as one more
    record — that's the shape of a torn write that left the file
    without a final ``\\n``. ``wc -l`` would *miss* this one; we don't.

    **Behavior note (vs. previous splitlines+strip implementation).**
    The old code dropped blank lines (``if line.strip()``); this
    counter does not — every ``\\n`` is one record. On a clean
    firehose those produce identical counts, but a torn write that
    leaves a bare ``\\n`` in the middle, or an external process
    appending an empty line, will over-count by one here. The trim
    hysteresis absorbs over-counts (a one-line drift just makes the
    next trim fire a hair sooner — not load-bearing).

    Returns 0 for missing or unreadable files.
    """
    try:
        with path.open("rb") as f:
            count = 0
            had_data = False
            last_byte: bytes = b""
            while True:
                chunk = f.read(chunk_bytes)
                if not chunk:
                    break
                had_data = True
                count += chunk.count(b"\n")
                last_byte = chunk[-1:]
            # File ended with content but no trailing newline → that's
            # one more record (the pattern most JSONL appenders never
            # produce, but a torn write can leave one behind).
            if had_data and last_byte != b"\n":
                count += 1
            return count
    except OSError:
        return 0


def _tail_lines(path: Path) -> Iterator[str]:
    """Yield lines from ``path`` newest-first, reading in fixed-size
    chunks from the end. Lines that span chunk boundaries are stitched
    correctly via a leading-fragment buffer.

    OSError is propagated to the caller (``tail_jsonl_records`` swallows
    it); we don't paper over a deleted file mid-iteration here.
    """
    with path.open("rb") as f:
        f.seek(0, 2)  # SEEK_END
        pos = f.tell()
        leading_fragment = b""
        while pos > 0:
            read_size = min(_CHUNK_BYTES, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size) + leading_fragment
            lines = chunk.split(b"\n")
            # If we haven't reached BOF, the first split is a partial
            # line whose head lives in an earlier chunk — defer it.
            if pos > 0:
                leading_fragment = lines[0]
                lines = lines[1:]
            else:
                leading_fragment = b""
            # Yield in reverse so the caller sees newest-first.
            for raw in reversed(lines):
                if raw:
                    yield raw.decode("utf-8", errors="replace")
        # Once we've reached BOF, the leading fragment is the very first
        # line of the file — yield it last (it's the oldest record).
        if leading_fragment:
            yield leading_fragment.decode("utf-8", errors="replace")
