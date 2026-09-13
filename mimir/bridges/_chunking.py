"""Length-bounded chat messages with independently renderable code fences."""

from __future__ import annotations

import re
from bisect import bisect_right


def chunk_message(text: str, limit: int) -> list[str]:
    """Prefer paragraphs, then lines, then hard splits without losing text.

    Fenced code is closed and reopened with its original info string at every
    split. Raise ValueError if the limit cannot accommodate the fence overhead
    and any content, rather than producing oversized messages or looping.
    """
    if limit <= 0:
        raise ValueError("message limit must be positive")
    # Only line-start fences count; shorter/other fences inside code are literal.
    fences: list[tuple[int, int]] = []
    ends = [0]
    states = [("", "")]
    marker = opener = ""
    for match in re.finditer(r"^ {0,3}(`{3,}|~{3,})([^\n]*)(?:\n|$)", text, re.M):
        run, info = match.groups()
        if not marker:
            if run[0] == "`" and "`" in info:
                continue
            marker = run
            opener = match.group().rstrip("\r\n") + "\n"
        elif run[0] == marker[0] and len(run) >= len(marker) and not info.strip():
            marker = opener = ""
        else:
            continue
        fences.append((match.start(), match.end()))
        ends.append(match.end())
        states.append((marker, opener))

    chunks: list[str] = []
    start = 0
    prefix = ""
    while start < len(text):
        end = min(len(text), start + limit - len(prefix))
        prefer_boundary = True
        while end > start:
            # Never bisect an opening info string or a closing delimiter.
            for fence_start, fence_end in fences:
                if fence_start < end < fence_end:
                    end = fence_start
                    break
            marker, next_prefix = states[bisect_right(ends, end) - 1]
            suffix = "\n" + marker if marker else ""
            excess = len(prefix) + end - start + len(suffix) - limit
            if excess > 0:
                end -= excess
                continue
            if prefer_boundary and end < len(text):
                prefer_boundary = False
                section = text[start:end]
                paragraphs = list(re.finditer(r"\n\s*\n+", section))
                boundary = paragraphs[-1].end() if paragraphs else section.rfind("\n") + 1
                if boundary and start + boundary < end:
                    end = start + boundary
                    continue
            break
        if end <= start:
            raise ValueError("message limit is too small for code fence overhead")
        chunks.append(prefix + text[start:end] + suffix)
        prefix = next_prefix
        start = end
    return chunks or [""]
