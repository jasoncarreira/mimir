"""Shared chunking contract exercised through both bridge entry points."""

from __future__ import annotations

import re

import pytest

from mimir.bridges.discord import _chunk_message as discord_chunk
from mimir.bridges.slack import _chunk_message as slack_chunk


@pytest.fixture(params=[discord_chunk, slack_chunk], ids=["discord", "slack"])
def chunk(request):
    return request.param


@pytest.mark.parametrize("limit", [32, 60, 100, 2000, 3500])
@pytest.mark.parametrize("language", ["", "python", "javascript"])
def test_fences_balanced_and_language_preserved(chunk, limit, language):
    code = "x = 1234567890\n\n" * 600
    text = f"```{language}\n{code}```"
    chunks = chunk(text, limit)
    assert len(chunks) > 1
    restored = ""
    for index, part in enumerate(chunks):
        assert len(part) <= limit
        assert part.startswith(f"```{language}\n")
        assert part.endswith("```")
        assert part.count("```") == 2
        if index:
            part = part[len(f"```{language}\n"):]
        if index < len(chunks) - 1:
            part = part[:-4]
        restored += part
    assert restored == text


def test_long_code_line_preserved(chunk):
    text = "```python\n" + "x" * 501 + "\n```"
    chunks = chunk(text, 40)
    assert all(len(part) <= 40 and part.count("```") == 2 for part in chunks)
    assert sum(part.count("x") for part in chunks) == 501
    assert all(part.startswith("```python\n") for part in chunks)


def test_multiple_fence_languages_and_prose(chunk):
    text = "intro\n\n```python\n" + "x\n" * 60 + "```\nprose\n```js\n" + "y\n" * 60 + "```\noutro"
    chunks = chunk(text, 50)
    for part in chunks:
        assert len(part) <= 50
        fences = re.findall(r"^```([^\n]*)", part, re.M)
        assert len(fences) % 2 == 0
        assert all(not closing for closing in fences[1::2])
        if "x\n" in part:
            assert "```python\n" in part
        if "y\n" in part:
            assert "```js\n" in part
    assert chunks[0].startswith("intro")
    assert chunks[-1].endswith("outro")


def test_plain_text_boundaries_and_preservation(chunk):
    text = "first paragraph\n\nsecond paragraph\n\nthird paragraph"
    assert chunk(text, 36) == ["first paragraph\n\nsecond paragraph\n\n", "third paragraph"]
    assert "".join(chunk(text, 8)) == text
    assert chunk("") == [""]


def test_unclosed_fence_is_closed(chunk):
    assert chunk("```python\nx", 30) == ["```python\nx\n```"]


@pytest.mark.parametrize("marker", ["```", "````", "~~~"])
@pytest.mark.parametrize("padding", range(25, 45))
def test_boundary_does_not_split_fence_delimiters(chunk, marker, padding):
    text = "p" * padding + f"\n{marker}python\n" + "x" * 80 + f"\n{marker}\nend"
    chunks = chunk(text, 40)
    assert all(len(part) <= 40 for part in chunks)
    for part in chunks:
        assert part.count(marker) % 2 == 0
        if "x" in part:
            assert f"{marker}python\n" in part
    assert sum(part.count("x") for part in chunks) == 80
    assert sum(part.count("p") for part in chunks) == padding + sum(
        part.count("python") for part in chunks
    )
    assert chunks[-1].endswith("end")


def test_inline_backticks_are_not_fences(chunk):
    text = "inline ``` example " * 30
    assert "".join(chunk(text, 40)) == text


def test_impossible_fence_budget_fails_promptly(chunk):
    with pytest.raises(ValueError, match="fence overhead"):
        chunk("```python\n" + "x" * 100, 10)
